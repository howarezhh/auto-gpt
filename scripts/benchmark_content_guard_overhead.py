from __future__ import annotations

import argparse
import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app.services.content_guard_service import ContentGuardService  # noqa: E402


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


def print_case(case: dict[str, Any]) -> None:
    single = case["single"]
    concurrent = case["concurrent"]
    print(f"\nCase: {case['name']} ({case['endpoint_path']})")
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
    if args.concurrency < 1:
        raise SystemExit("--concurrency must be at least 1")

    normal_text = make_text(args.scan_bytes, suspicious=False)
    suspicious_text = make_text(args.scan_bytes, suspicious=True)
    cases = [
        benchmark_case(
            name="chat_normal",
            payload=make_chat_payload(normal_text),
            endpoint_path="/chat/completions",
            iterations=args.iterations,
            concurrency=args.concurrency,
            max_scan_bytes=args.scan_bytes,
        ),
        benchmark_case(
            name="responses_normal",
            payload=make_responses_payload(normal_text),
            endpoint_path="/responses",
            iterations=args.iterations,
            concurrency=args.concurrency,
            max_scan_bytes=args.scan_bytes,
        ),
        benchmark_case(
            name="chat_suspicious",
            payload=make_chat_payload(suspicious_text),
            endpoint_path="/chat/completions",
            iterations=args.iterations,
            concurrency=args.concurrency,
            max_scan_bytes=args.scan_bytes,
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
