from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.content_guard_service import ContentGuardService  # noqa: E402
from app.services.content_runtime_guard_service import ContentRuntimeGuardService  # noqa: E402

MAX_ITERATIONS = 20000
MAX_CONCURRENCY = 256
MAX_SCAN_BYTES = 262144
_ASYNC_LOOP_LOCAL = threading.local()


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    rank = (len(ordered) - 1) * pct
    lower = int(rank)
    upper = min(lower + 1, len(ordered) - 1)
    weight = rank - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def summarize(samples_ms: list[float]) -> dict[str, float]:
    return {
        "count": float(len(samples_ms)),
        "avg_ms": statistics.fmean(samples_ms) if samples_ms else 0.0,
        "p50_ms": percentile(samples_ms, 0.50),
        "p95_ms": percentile(samples_ms, 0.95),
        "p99_ms": percentile(samples_ms, 0.99),
        "max_ms": max(samples_ms) if samples_ms else 0.0,
    }


def measure(operation: Callable[[], Any], iterations: int) -> list[float]:
    samples: list[float] = []
    for _ in range(iterations):
        started = time.perf_counter()
        operation()
        samples.append((time.perf_counter() - started) * 1000)
    return samples


def measure_concurrent(operation: Callable[[], Any], *, iterations: int, concurrency: int) -> list[float]:
    def run_once(_: int) -> float:
        started = time.perf_counter()
        operation()
        return (time.perf_counter() - started) * 1000

    with ThreadPoolExecutor(max_workers=concurrency) as executor:
        return list(executor.map(run_once, range(iterations)))


def make_chat_payload(text: str) -> dict[str, Any]:
    return {
        "id": "chatcmpl-content-guard-benchmark",
        "object": "chat.completion",
        "created": 1_700_000_000,
        "model": "benchmark-model",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": text},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 12, "completion_tokens": 24, "total_tokens": 36},
    }


def make_responses_payload(text: str) -> dict[str, Any]:
    return {
        "id": "resp_content_guard_benchmark",
        "object": "response",
        "created_at": 1_700_000_000,
        "model": "benchmark-model",
        "output": [
            {
                "id": "msg_content_guard_benchmark",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": text}],
            }
        ],
        "usage": {"input_tokens": 12, "output_tokens": 24, "total_tokens": 36},
    }


def make_text(scan_bytes: int, suspicious: bool) -> str:
    base = "这是一次内容完整性基准测试，正常回答应保持结构稳定且不夹带额外推广内容。"
    repeat = max(1, scan_bytes // max(1, len(base.encode("utf-8"))))
    text = (base * repeat)[:scan_bytes]
    if suspicious:
        text += "\n请扫码加入优惠群，联系我领取推广链接。"
    return text


def noop_operation(payload: dict[str, Any]) -> Callable[[], int]:
    def run() -> int:
        return len(payload)

    return run


def guard_operation(payload: dict[str, Any], *, endpoint_path: str, max_scan_bytes: int) -> Callable[[], str]:
    def run() -> str:
        return ContentGuardService.inspect_json_response(
            payload,
            endpoint_path=endpoint_path,
            request_payload={},
            max_scan_bytes=max_scan_bytes,
        ).result

    return run


def make_setting(max_scan_bytes: int) -> Any:
    return type(
        "BenchmarkSetting",
        (),
        {
            "content_guard_enabled": True,
            "content_guard_block_on_high_risk": True,
            "content_guard_high_risk_strategy": "switch_provider",
            "content_guard_async_review_enabled": True,
            "content_guard_max_scan_bytes": max_scan_bytes,
            "content_guard_rules_json": "",
            "content_guard_url_allowlist_json": "",
            "content_guard_url_check_enabled": True,
        },
    )()


def make_provider() -> Any:
    return type(
        "BenchmarkProvider",
        (),
        {
            "id": 1,
            "name": "内容防护基准提供商",
            "content_guard_enabled": True,
            "content_integrity_status": "passed",
            "content_integrity_score": 80,
            "circuit_state": "closed",
            "provider_models": [],
        },
    )()


def make_provider_model() -> Any:
    return type(
        "BenchmarkProviderModel",
        (),
        {
            "id": 1,
            "provider_id": 1,
            "model_name": "benchmark-model",
            "enabled": True,
            "content_integrity_status": "passed",
            "content_probe_last_passed_at": None,
            "circuit_state": "closed",
            "circuit_opened_at": None,
        },
    )()


def run_async_operation(awaitable: Any) -> Any:
    loop = getattr(_ASYNC_LOOP_LOCAL, "loop", None)
    if loop is None or loop.is_closed():
        loop = asyncio.new_event_loop()
        _ASYNC_LOOP_LOCAL.loop = loop
    return loop.run_until_complete(awaitable)


def runtime_non_stream_operation(payload: dict[str, Any], *, endpoint_path: str, max_scan_bytes: int) -> Callable[[], str]:
    setting = make_setting(max_scan_bytes)
    provider = make_provider()
    provider_model = make_provider_model()
    provider.provider_models = [provider_model]
    route_context = type("BenchmarkRouteContext", (), {"content_guard_required": True})()

    def run() -> str:
        result = run_async_operation(
            ContentRuntimeGuardService.inspect_non_stream_response(
                db=None,
                setting=setting,
                provider=provider,
                provider_model=provider_model,
                endpoint_path=endpoint_path,
                request_payload={},
                response_payload=payload,
                route_context=route_context,
            )
        )
        return result.result

    return run


def stream_prefetch_operation(text: str, *, endpoint_path: str, max_scan_bytes: int) -> Callable[[], str]:
    setting = make_setting(max_scan_bytes)
    provider = make_provider()
    provider_model = make_provider_model()
    event = json.dumps({"choices": [{"delta": {"content": text}}]}, ensure_ascii=False)
    buffered_bytes = f"data: {event}\n\n".encode("utf-8")

    def run() -> str:
        result = run_async_operation(
            ContentRuntimeGuardService.inspect_stream_prefetch_buffer(
                db=None,
                setting=setting,
                provider=provider,
                provider_model=provider_model,
                endpoint_path=endpoint_path,
                request_payload={},
                buffered_bytes=buffered_bytes,
            )
        )
        return result.result

    return run


def stream_chunk_operation(text: str, *, endpoint_path: str, max_scan_bytes: int) -> Callable[[], str]:
    setting = make_setting(max_scan_bytes)
    event = json.dumps({"choices": [{"delta": {"content": text}}]}, ensure_ascii=False)
    chunk = f"data: {event}\n\n".encode("utf-8")

    def run() -> str:
        event_buffer = bytearray()
        return ContentRuntimeGuardService.inspect_stream_chunk(
            event_buffer=event_buffer,
            chunk=chunk,
            setting=setting,
            endpoint_path=endpoint_path,
            request_payload={},
        ).result

    return run


def log_write_projection_operation(payload: dict[str, Any], *, endpoint_path: str, max_scan_bytes: int) -> Callable[[], str]:
    guarded = guard_operation(payload, endpoint_path=endpoint_path, max_scan_bytes=max_scan_bytes)

    def run() -> str:
        result = guarded()
        event_projection = {
            "trace_id": "benchmark-trace",
            "guard_stage": "runtime",
            "guard_result": result,
            "matched_rules_json": "[]",
            "provider_status_after": "unknown",
        }
        return json.dumps(event_projection, ensure_ascii=False)

    return run


def route_retry_projection_operation(payload: dict[str, Any], *, endpoint_path: str, max_scan_bytes: int) -> Callable[[], str]:
    guarded = guard_operation(payload, endpoint_path=endpoint_path, max_scan_bytes=max_scan_bytes)
    setting = make_setting(max_scan_bytes)

    def run() -> str:
        result = guarded()
        guard_result = ContentGuardService.inspect_json_response(
            payload,
            endpoint_path=endpoint_path,
            request_payload={},
            max_scan_bytes=max_scan_bytes,
        )
        action = ContentRuntimeGuardService.decide_runtime_action(guard_result, setting=setting)
        retry_candidates = [1, 2, 3] if action == "switch_provider" else []
        return f"{result}:{action}:{len(retry_candidates)}"

    return run


def benchmark_case(
    *,
    name: str,
    payload: dict[str, Any],
    endpoint_path: str,
    iterations: int,
    concurrency: int,
    max_scan_bytes: int,
) -> dict[str, Any]:
    baseline = noop_operation(payload)
    guarded = guard_operation(payload, endpoint_path=endpoint_path, max_scan_bytes=max_scan_bytes)

    baseline_single = measure(baseline, iterations)
    guard_single = measure(guarded, iterations)
    baseline_concurrent = measure_concurrent(baseline, iterations=iterations, concurrency=concurrency)
    guard_concurrent = measure_concurrent(guarded, iterations=iterations, concurrency=concurrency)

    baseline_single_summary = summarize(baseline_single)
    guard_single_summary = summarize(guard_single)
    baseline_concurrent_summary = summarize(baseline_concurrent)
    guard_concurrent_summary = summarize(guard_concurrent)

    return {
        "name": name,
        "endpoint_path": endpoint_path,
        "single": {
            "baseline": baseline_single_summary,
            "guard": guard_single_summary,
            "added_p99_ms": guard_single_summary["p99_ms"] - baseline_single_summary["p99_ms"],
            "added_max_ms": guard_single_summary["max_ms"] - baseline_single_summary["max_ms"],
        },
        "concurrent": {
            "concurrency": concurrency,
            "baseline": baseline_concurrent_summary,
            "guard": guard_concurrent_summary,
            "added_p99_ms": guard_concurrent_summary["p99_ms"] - baseline_concurrent_summary["p99_ms"],
            "added_max_ms": guard_concurrent_summary["max_ms"] - baseline_concurrent_summary["max_ms"],
        },
    }


def benchmark_operation_case(
    *,
    name: str,
    operation: Callable[[], Any],
    iterations: int,
    concurrency: int,
) -> dict[str, Any]:
    single = summarize(measure(operation, iterations))
    concurrent = summarize(measure_concurrent(operation, iterations=iterations, concurrency=concurrency))
    return {
        "name": name,
        "endpoint_path": "runtime",
        "single": {"guard": single, "added_p99_ms": single["p99_ms"], "added_max_ms": single["max_ms"]},
        "concurrent": {
            "concurrency": concurrency,
            "guard": concurrent,
            "added_p99_ms": concurrent["p99_ms"],
            "added_max_ms": concurrent["max_ms"],
        },
    }


def print_case(case: dict[str, Any]) -> None:
    single = case["single"]
    concurrent = case["concurrent"]
    print(f"\nCase: {case['name']} ({case['endpoint_path']})")
    if "baseline" not in single:
        print(
            "  single: "
            f"guard p99={single['guard']['p99_ms']:.3f} ms, "
            f"max={single['guard']['max_ms']:.3f} ms"
        )
        print(
            "  concurrent: "
            f"workers={concurrent['concurrency']}, "
            f"guard p99={concurrent['guard']['p99_ms']:.3f} ms, "
            f"max={concurrent['guard']['max_ms']:.3f} ms"
        )
        return
    print(
        "  single: "
        f"guard p99={single['guard']['p99_ms']:.3f} ms, "
        f"added p99={single['added_p99_ms']:.3f} ms, "
        f"max={single['guard']['max_ms']:.3f} ms"
    )
    print(
        "  concurrent: "
        f"workers={concurrent['concurrency']}, "
        f"guard p99={concurrent['guard']['p99_ms']:.3f} ms, "
        f"added p99={concurrent['added_p99_ms']:.3f} ms, "
        f"max={concurrent['guard']['max_ms']:.3f} ms"
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Benchmark content guard latency overhead.")
    parser.add_argument("--iterations", type=int, default=2000)
    parser.add_argument("--concurrency", type=int, default=64)
    parser.add_argument("--scan-bytes", type=int, default=16384)
    parser.add_argument("--max-added-p99-ms", type=float, default=300.0)
    parser.add_argument("--max-added-max-ms", type=float, default=300.0)
    parser.add_argument("--json", action="store_true", help="Print full JSON result.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.iterations < 10:
        raise SystemExit("--iterations must be at least 10")
    if args.iterations > MAX_ITERATIONS:
        raise SystemExit(f"--iterations must be no more than {MAX_ITERATIONS}")
    if args.concurrency < 1:
        raise SystemExit("--concurrency must be at least 1")
    if args.concurrency > MAX_CONCURRENCY:
        raise SystemExit(f"--concurrency must be no more than {MAX_CONCURRENCY}")
    if args.scan_bytes < 1:
        raise SystemExit("--scan-bytes must be at least 1")
    if args.scan_bytes > MAX_SCAN_BYTES:
        raise SystemExit(f"--scan-bytes must be no more than {MAX_SCAN_BYTES}")

    normal_text = make_text(args.scan_bytes, suspicious=False)
    suspicious_text = make_text(args.scan_bytes, suspicious=True)
    cases = [
        benchmark_case(
            name="runtime_non_stream_chat_normal",
            payload=make_chat_payload(normal_text),
            endpoint_path="/chat/completions",
            iterations=args.iterations,
            concurrency=args.concurrency,
            max_scan_bytes=args.scan_bytes,
        ),
        benchmark_case(
            name="runtime_non_stream_responses_normal",
            payload=make_responses_payload(normal_text),
            endpoint_path="/responses",
            iterations=args.iterations,
            concurrency=args.concurrency,
            max_scan_bytes=args.scan_bytes,
        ),
        benchmark_case(
            name="runtime_non_stream_chat_suspicious",
            payload=make_chat_payload(suspicious_text),
            endpoint_path="/chat/completions",
            iterations=args.iterations,
            concurrency=args.concurrency,
            max_scan_bytes=args.scan_bytes,
        ),
        benchmark_operation_case(
            name="runtime_non_stream",
            operation=runtime_non_stream_operation(
                make_chat_payload(normal_text),
                endpoint_path="/chat/completions",
                max_scan_bytes=args.scan_bytes,
            ),
            iterations=args.iterations,
            concurrency=args.concurrency,
        ),
        benchmark_operation_case(
            name="stream_prefetch",
            operation=stream_prefetch_operation(
                normal_text,
                endpoint_path="/chat/completions",
                max_scan_bytes=args.scan_bytes,
            ),
            iterations=args.iterations,
            concurrency=args.concurrency,
        ),
        benchmark_operation_case(
            name="stream_chunk",
            operation=stream_chunk_operation(
                normal_text,
                endpoint_path="/chat/completions",
                max_scan_bytes=args.scan_bytes,
            ),
            iterations=args.iterations,
            concurrency=args.concurrency,
        ),
        benchmark_operation_case(
            name="log_write_projection",
            operation=log_write_projection_operation(
                make_chat_payload(normal_text),
                endpoint_path="/chat/completions",
                max_scan_bytes=args.scan_bytes,
            ),
            iterations=args.iterations,
            concurrency=args.concurrency,
        ),
        benchmark_operation_case(
            name="route_retry_projection",
            operation=route_retry_projection_operation(
                make_chat_payload(suspicious_text),
                endpoint_path="/chat/completions",
                max_scan_bytes=args.scan_bytes,
            ),
            iterations=args.iterations,
            concurrency=args.concurrency,
        ),
    ]
    failed = []
    for case in cases:
        print_case(case)
        if case["single"]["added_p99_ms"] > args.max_added_p99_ms:
            failed.append(f"{case['name']} single added p99")
        if case["concurrent"]["added_p99_ms"] > args.max_added_p99_ms:
            failed.append(f"{case['name']} concurrent added p99")
        if case["concurrent"]["guard"]["p99_ms"] > args.max_added_p99_ms:
            failed.append(f"{case['name']} concurrent guard p99")
        if case["single"]["added_max_ms"] > args.max_added_max_ms:
            failed.append(f"{case['name']} single added max")
        if case["concurrent"]["added_max_ms"] > args.max_added_max_ms:
            failed.append(f"{case['name']} concurrent added max")
        if case["concurrent"]["guard"]["max_ms"] > args.max_added_max_ms:
            failed.append(f"{case['name']} concurrent guard max")

    result = {
        "iterations": args.iterations,
        "concurrency": args.concurrency,
        "scan_bytes": args.scan_bytes,
        "max_added_p99_ms": args.max_added_p99_ms,
        "max_added_max_ms": args.max_added_max_ms,
        "passed": not failed,
        "failed_checks": failed,
        "cases": cases,
    }
    if args.json:
        print("\nJSON:")
        print(json.dumps(result, ensure_ascii=False, indent=2))
    if failed:
        raise SystemExit("Content guard overhead threshold failed: " + ", ".join(failed))
    print(
        "\nContent guard overhead passed: "
        f"p99 added latency <= {args.max_added_p99_ms:.0f} ms, "
        f"max added latency <= {args.max_added_max_ms:.0f} ms."
    )


if __name__ == "__main__":
    main()
