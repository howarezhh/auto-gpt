from __future__ import annotations

import argparse
import asyncio
import json
import math
import statistics
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

import httpx

from app.utils.timezone import now_beijing

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROXY_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_REPORT_DIR = PROJECT_ROOT / "data" / "benchmark-reports"
DEFAULT_TEXT_PROMPT = "请用两句话简洁回答：并发压测探针。"
BENCH_EVENT_PREFIX = "@@BENCH@@"
MAX_BENCHMARK_CONCURRENCY = 1000
MAX_BENCHMARK_PROBE_CONCURRENCY = 2000
MAX_BENCHMARK_SAMPLE_REQUESTS = 10000
MAX_BENCHMARK_REQUESTS_PER_CONCURRENCY = 20
MAX_BENCHMARK_STAGE_REQUESTS = 10000
MAX_BENCHMARK_WARMUP_REQUESTS = 1000
MAX_BENCHMARK_TIMEOUT_S = 600
MAX_HTTP_CONNECTIONS = 2000
MAX_HTTP_KEEPALIVE_CONNECTIONS = 1000


@dataclass(slots=True)
class RequestResult:
    ok: bool
    status_code: int | None
    total_latency_ms: float
    first_event_latency_ms: float | None
    bytes_read: int
    event_count: int
    done_received: bool
    error: str | None


@dataclass(slots=True)
class StageMetrics:
    model_name: str
    endpoint: str
    concurrency: int
    total_requests: int
    wall_time_s: float
    success_requests: int
    failed_requests: int
    success_rate: float
    throughput_rps: float
    total_bytes: int
    total_events: int
    latency_avg_ms: float
    latency_p50_ms: float
    latency_p95_ms: float
    latency_p99_ms: float
    latency_max_ms: float
    first_event_avg_ms: float | None
    first_event_p50_ms: float | None
    first_event_p95_ms: float | None
    first_event_max_ms: float | None
    status_counts: dict[str, int]
    error_counts: dict[str, int]
    stable: bool
    unstable_reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ModelReport:
    model_name: str
    endpoint: str
    stage_results: list[StageMetrics]
    stable_concurrency_upper_limit: int | None
    first_unstable_concurrency: int | None
    recommended_concurrency: int | None
    best_throughput_rps: float | None
    recommended_latency_p95_ms: float | None
    recommended_first_event_p95_ms: float | None
    summary: str


@dataclass(slots=True)
class BenchmarkReport:
    started_at: str
    finished_at: str
    proxy_base_url: str
    endpoint: str
    model_names: list[str]
    max_output_tokens: int
    stage_request_floor: int
    requests_per_concurrency: int
    success_rate_threshold: float
    timeout_error_threshold: int
    busy_error_threshold: int
    latency_p95_threshold_ms: float
    first_event_p95_threshold_ms: float
    model_reports: list[ModelReport]
    html_report_path: str
    json_report_path: str


def emit_event(event_type: str, **payload: Any) -> None:
    print(BENCH_EVENT_PREFIX + json.dumps({"type": event_type, **payload}, ensure_ascii=False), flush=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="真实并发探测脚本：仅通过外部流式文本接口，对一个或多个模型执行真实压测并输出可视化报告。"
    )
    parser.add_argument("--proxy-base-url", default=DEFAULT_PROXY_BASE_URL, help="项目通用代理地址。")
    parser.add_argument("--raw-api-key", required=True, help="外部用户实际调用项目时使用的 API Key。")
    parser.add_argument("--endpoint", choices=["chat", "responses"], default="chat", help="流式文本入口类型。")
    parser.add_argument("--model-names", required=True, help="要探测的模型名列表，逗号分隔。")
    parser.add_argument("--concurrency", type=int, default=100, help="基准并发数。")
    parser.add_argument("--probe-min-concurrency", type=int, default=0, help="探测起始并发。")
    parser.add_argument("--probe-max-concurrency", type=int, default=0, help="探测最大并发。")
    parser.add_argument("--max-probe-rounds", type=int, default=10, help="每个模型最多探测轮数。")
    parser.add_argument("--probe-scale", type=float, default=1.5, help="逐轮放大倍率。")
    parser.add_argument("--sample-requests", type=int, default=300, help="每轮最少请求数。")
    parser.add_argument("--requests-per-concurrency", type=int, default=2, help="每轮至少按并发倍数发送的请求数。")
    parser.add_argument("--warmup-requests", type=int, default=12, help="每个模型探测前的预热请求数。")
    parser.add_argument("--max-output-tokens", type=int, default=128, help="单请求输出 Token 上限。")
    parser.add_argument("--client-timeout-s", type=float, default=180.0, help="单请求客户端超时秒数。")
    parser.add_argument("--request-timeout-s", type=float, default=180.0, help="httpx 请求超时秒数。")
    parser.add_argument("--boundary-max-rounds", type=int, default=8, help="首次失稳后继续细化边界的最大轮数。")
    parser.add_argument("--progress-interval-s", type=float, default=0.5, help="进度刷新秒数。")
    parser.add_argument("--success-rate-threshold", type=float, default=0.99, help="稳定判定的最低成功率。")
    parser.add_argument("--timeout-error-threshold", type=int, default=0, help="稳定判定允许的超时错误个数。")
    parser.add_argument("--busy-error-threshold", type=int, default=0, help="稳定判定允许的 429/503/504 个数。")
    parser.add_argument("--latency-p95-threshold-ms", type=float, default=0, help="稳定判定允许的 P95 总延迟上限，0 表示不限制。")
    parser.add_argument("--first-event-p95-threshold-ms", type=float, default=0, help="稳定判定允许的首事件 P95 上限，0 表示不限制。")
    parser.add_argument("--prompt", default=DEFAULT_TEXT_PROMPT, help="流式文本探测提示词。")
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR), help="报告输出目录。")
    parser.add_argument("--report-prefix", default="real-concurrency-benchmark", help="报告文件名前缀。")
    return parser.parse_args()


def clamp_int(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, int(value)))


def clamp_float(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, float(value)))


def normalize_args(args: argparse.Namespace) -> argparse.Namespace:
    args.concurrency = clamp_int(args.concurrency, 1, MAX_BENCHMARK_CONCURRENCY)
    args.probe_min_concurrency = clamp_int(args.probe_min_concurrency, 0, MAX_BENCHMARK_CONCURRENCY)
    args.probe_max_concurrency = clamp_int(args.probe_max_concurrency, 0, MAX_BENCHMARK_PROBE_CONCURRENCY)
    args.sample_requests = clamp_int(args.sample_requests, 1, MAX_BENCHMARK_SAMPLE_REQUESTS)
    args.requests_per_concurrency = clamp_int(
        args.requests_per_concurrency,
        1,
        MAX_BENCHMARK_REQUESTS_PER_CONCURRENCY,
    )
    args.warmup_requests = clamp_int(args.warmup_requests, 0, MAX_BENCHMARK_WARMUP_REQUESTS)
    args.client_timeout_s = clamp_float(args.client_timeout_s, 1.0, MAX_BENCHMARK_TIMEOUT_S)
    args.request_timeout_s = clamp_float(args.request_timeout_s, 1.0, MAX_BENCHMARK_TIMEOUT_S)
    args.sample_requests = min(args.sample_requests, MAX_BENCHMARK_STAGE_REQUESTS)
    return args


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round((pct / 100) * (len(ordered) - 1)))))
    return ordered[index]


def parse_model_names(raw_model_names: str) -> list[str]:
    result: list[str] = []
    for item in raw_model_names.split(","):
        normalized = item.strip()
        if normalized and normalized not in result:
            result.append(normalized)
    if not result:
        raise ValueError("至少需要一个模型名")
    return result


def resolve_probe_min_concurrency(args: argparse.Namespace) -> int:
    if args.probe_min_concurrency > 0:
        return max(1, args.probe_min_concurrency)
    return max(10, args.concurrency // 4)


def resolve_probe_max_concurrency(args: argparse.Namespace) -> int:
    if args.probe_max_concurrency > 0:
        return max(args.concurrency, args.probe_max_concurrency)
    return max(args.concurrency * 8, 400)


def build_concurrency_plan(args: argparse.Namespace) -> list[int]:
    minimum = resolve_probe_min_concurrency(args)
    maximum = resolve_probe_max_concurrency(args)
    scale = max(1.1, float(args.probe_scale))
    values = {minimum, args.concurrency, maximum}
    current = minimum
    for _ in range(max(1, args.max_probe_rounds)):
        values.add(max(1, int(round(current))))
        if current >= maximum:
            break
        current = max(current + 1, int(math.ceil(current * scale)))
    return sorted(value for value in values if minimum <= value <= maximum)


def build_payload(*, endpoint: str, model_name: str, prompt: str, max_output_tokens: int) -> dict[str, Any]:
    if endpoint == "responses":
        return {
            "model": model_name,
            "input": prompt,
            "stream": True,
            "max_output_tokens": max_output_tokens,
            "temperature": 0,
        }
    return {
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}],
        "stream": True,
        "max_tokens": max_output_tokens,
        "temperature": 0,
    }


def resolve_url(proxy_base_url: str, endpoint: str) -> str:
    base = proxy_base_url.strip().rstrip("/") or DEFAULT_PROXY_BASE_URL
    path = "/v1/responses" if endpoint == "responses" else "/v1/chat/completions"
    return base + path


def classify_error(result: RequestResult) -> tuple[bool, bool]:
    if result.error is None:
        return False, False
    text = result.error.lower()
    is_timeout = "timeout" in text
    is_busy = result.status_code in {429, 503, 504} or any(code in text for code in ("http_429", "http_503", "http_504"))
    return is_timeout, is_busy


def validate_sse_payload(endpoint: str, payload: Any) -> str | None:
    if not isinstance(payload, dict):
        return "invalid_sse_payload_type"
    if "error" in payload:
        return "stream_error_payload"
    if endpoint == "responses":
        if not isinstance(payload.get("type"), str):
            return "invalid_responses_event"
        return None
    choices = payload.get("choices")
    if isinstance(choices, list):
        return None
    if payload.get("usage") is not None:
        return None
    return "invalid_chat_event"


async def run_single_request(
    client: httpx.AsyncClient,
    *,
    endpoint: str,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
) -> RequestResult:
    started = time.perf_counter()
    bytes_read = 0
    event_count = 0
    done_received = False
    first_event_latency_ms: float | None = None
    status_code: int | None = None
    error: str | None = None
    current_event = "message"
    try:
        async with client.stream("POST", url, headers=headers, json=payload) as response:
            status_code = response.status_code
            if response.status_code >= 400:
                await response.aread()
                error = f"http_{response.status_code}"
            else:
                async for raw_line in response.aiter_lines():
                    line = raw_line.strip()
                    if not line:
                        continue
                    if line.startswith("event:"):
                        current_event = line[6:].strip() or "message"
                        continue
                    if not line.startswith("data:"):
                        continue
                    data = line[5:].strip()
                    if first_event_latency_ms is None:
                        first_event_latency_ms = (time.perf_counter() - started) * 1000
                    if data == "[DONE]":
                        done_received = True
                        break
                    event_count += 1
                    bytes_read += len(data.encode("utf-8"))
                    try:
                        event_payload = json.loads(data)
                    except json.JSONDecodeError:
                        error = "invalid_sse_json"
                        break
                    if current_event == "error":
                        error = "sse_event_error"
                        break
                    payload_error = validate_sse_payload(endpoint, event_payload)
                    if payload_error:
                        error = payload_error
                        break
                if error is None and event_count <= 0:
                    error = "empty_stream"
                if error is None and not done_received:
                    error = "stream_missing_done"
        total_latency_ms = (time.perf_counter() - started) * 1000
        return RequestResult(
            ok=(status_code is not None and status_code < 400 and error is None),
            status_code=status_code,
            total_latency_ms=total_latency_ms,
            first_event_latency_ms=first_event_latency_ms,
            bytes_read=bytes_read,
            event_count=event_count,
            done_received=done_received,
            error=error,
        )
    except Exception as exc:
        total_latency_ms = (time.perf_counter() - started) * 1000
        return RequestResult(
            ok=False,
            status_code=status_code,
            total_latency_ms=total_latency_ms,
            first_event_latency_ms=first_event_latency_ms,
            bytes_read=bytes_read,
            event_count=event_count,
            done_received=done_received,
            error=f"{type(exc).__name__}: {exc}",
        )


async def execute_stage(
    *,
    model_name: str,
    endpoint: str,
    url: str,
    raw_api_key: str,
    prompt: str,
    max_output_tokens: int,
    concurrency: int,
    total_requests: int,
    client_timeout_s: float,
    request_timeout_s: float,
    progress_interval_s: float,
) -> tuple[list[RequestResult], float]:
    headers = {"Authorization": f"Bearer {raw_api_key}"}
    payload = build_payload(
        endpoint=endpoint,
        model_name=model_name,
        prompt=prompt,
        max_output_tokens=max_output_tokens,
    )
    queue: asyncio.Queue[int] = asyncio.Queue()
    for index in range(total_requests):
        queue.put_nowait(index)

    stats = {"done": 0, "success": 0, "failed": 0, "busy": 0, "timeout": 0}
    results: list[RequestResult] = []
    limits = httpx.Limits(
        max_connections=min(max(concurrency * 2, 200), MAX_HTTP_CONNECTIONS),
        max_keepalive_connections=min(max(concurrency, 50), MAX_HTTP_KEEPALIVE_CONNECTIONS),
    )
    timeout = httpx.Timeout(request_timeout_s)
    stage_started = time.perf_counter()

    async def reporter() -> None:
        while stats["done"] < total_requests:
            elapsed = max(0.001, time.perf_counter() - stage_started)
            emit_event(
                "progress",
                model_name=model_name,
                endpoint=endpoint,
                concurrency=concurrency,
                done=stats["done"],
                total=total_requests,
                percent=round((stats["done"] / total_requests) * 100, 2),
                rps=round(stats["done"] / elapsed, 2),
            )
            await asyncio.sleep(max(0.1, progress_interval_s))
        elapsed = max(0.001, time.perf_counter() - stage_started)
        emit_event(
            "progress",
            model_name=model_name,
            endpoint=endpoint,
            concurrency=concurrency,
            done=total_requests,
            total=total_requests,
            percent=100.0,
            rps=round(total_requests / elapsed, 2),
        )

    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        async def worker() -> None:
            while True:
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    started = time.perf_counter()
                    try:
                        result = await asyncio.wait_for(
                            run_single_request(
                                client,
                                endpoint=endpoint,
                                url=url,
                                headers=headers,
                                payload=payload,
                            ),
                            timeout=max(1.0, client_timeout_s),
                        )
                    except asyncio.TimeoutError:
                        result = RequestResult(
                            ok=False,
                            status_code=None,
                            total_latency_ms=(time.perf_counter() - started) * 1000,
                            first_event_latency_ms=None,
                            bytes_read=0,
                            event_count=0,
                            done_received=False,
                            error="TimeoutError: client timeout reached",
                        )
                    results.append(result)
                    stats["done"] += 1
                    if result.ok:
                        stats["success"] += 1
                    else:
                        stats["failed"] += 1
                        is_timeout, is_busy = classify_error(result)
                        if is_timeout:
                            stats["timeout"] += 1
                        if is_busy:
                            stats["busy"] += 1
                finally:
                    queue.task_done()

        reporter_task = asyncio.create_task(reporter())
        worker_tasks = [asyncio.create_task(worker()) for _ in range(max(1, concurrency))]
        await queue.join()
        for task in worker_tasks:
            task.cancel()
        await asyncio.gather(*worker_tasks, return_exceptions=True)
        await reporter_task

    return results, time.perf_counter() - stage_started


def summarize_stage(
    *,
    model_name: str,
    endpoint: str,
    concurrency: int,
    total_requests: int,
    duration_s: float,
    results: list[RequestResult],
    success_rate_threshold: float,
    timeout_error_threshold: int,
    busy_error_threshold: int,
    latency_p95_threshold_ms: float,
    first_event_p95_threshold_ms: float,
) -> StageMetrics:
    latencies = [item.total_latency_ms for item in results]
    first_events = [item.first_event_latency_ms for item in results if item.first_event_latency_ms is not None]
    success_requests = sum(1 for item in results if item.ok)
    failed_requests = len(results) - success_requests
    status_counts = Counter(str(item.status_code) for item in results if item.status_code is not None)
    error_counts = Counter(item.error for item in results if item.error)
    timeout_errors = 0
    busy_errors = 0
    unstable_reasons: list[str] = []

    for item in results:
        is_timeout, is_busy = classify_error(item)
        timeout_errors += int(is_timeout)
        busy_errors += int(is_busy)

    success_rate = (success_requests / len(results)) if results else 0.0
    latency_p95_ms = percentile(latencies, 95)
    first_event_p95_ms = percentile(first_events, 95) if first_events else None

    if success_rate < success_rate_threshold:
        unstable_reasons.append(f"成功率 {success_rate:.2%} 低于阈值 {success_rate_threshold:.2%}")
    if timeout_errors > timeout_error_threshold:
        unstable_reasons.append(f"超时错误 {timeout_errors} 超过阈值 {timeout_error_threshold}")
    if busy_errors > busy_error_threshold:
        unstable_reasons.append(f"429/503/504 错误 {busy_errors} 超过阈值 {busy_error_threshold}")
    if latency_p95_threshold_ms > 0 and latency_p95_ms > latency_p95_threshold_ms:
        unstable_reasons.append(f"P95 延迟 {latency_p95_ms:.2f}ms 超过阈值 {latency_p95_threshold_ms:.2f}ms")
    if first_event_p95_threshold_ms > 0 and first_event_p95_ms is not None and first_event_p95_ms > first_event_p95_threshold_ms:
        unstable_reasons.append(
            f"首事件 P95 {first_event_p95_ms:.2f}ms 超过阈值 {first_event_p95_threshold_ms:.2f}ms"
        )

    return StageMetrics(
        model_name=model_name,
        endpoint=endpoint,
        concurrency=concurrency,
        total_requests=total_requests,
        wall_time_s=duration_s,
        success_requests=success_requests,
        failed_requests=failed_requests,
        success_rate=success_rate,
        throughput_rps=(len(results) / duration_s) if duration_s > 0 else 0.0,
        total_bytes=sum(item.bytes_read for item in results),
        total_events=sum(item.event_count for item in results),
        latency_avg_ms=statistics.mean(latencies) if latencies else 0.0,
        latency_p50_ms=percentile(latencies, 50),
        latency_p95_ms=latency_p95_ms,
        latency_p99_ms=percentile(latencies, 99),
        latency_max_ms=max(latencies) if latencies else 0.0,
        first_event_avg_ms=(statistics.mean(first_events) if first_events else None),
        first_event_p50_ms=(percentile(first_events, 50) if first_events else None),
        first_event_p95_ms=first_event_p95_ms,
        first_event_max_ms=(max(first_events) if first_events else None),
        status_counts=dict(status_counts),
        error_counts={key: value for key, value in error_counts.items() if key},
        stable=(not unstable_reasons),
        unstable_reasons=unstable_reasons,
    )


def print_stage_summary(metrics: StageMetrics) -> None:
    status_text = "稳定" if metrics.stable else "不稳定"
    print(
        f"[stage] 模型={metrics.model_name} 并发={metrics.concurrency} {status_text} "
        f"success={metrics.success_requests}/{metrics.total_requests} "
        f"success_rate={metrics.success_rate:.2%} "
        f"rps={metrics.throughput_rps:.2f} "
        f"p95={metrics.latency_p95_ms:.2f}ms "
        f"first_event_p95={metrics.first_event_p95_ms or 0:.2f}ms "
        f"max={metrics.latency_max_ms:.2f}ms"
    )
    emit_event(
        "stage",
        model_name=metrics.model_name,
        endpoint=metrics.endpoint,
        concurrency=metrics.concurrency,
        stable=metrics.stable,
        success_requests=metrics.success_requests,
        total_requests=metrics.total_requests,
        success_rate_percent=round(metrics.success_rate * 100, 2),
        throughput_rps=round(metrics.throughput_rps, 2),
        latency_p95_ms=round(metrics.latency_p95_ms, 2),
        latency_max_ms=round(metrics.latency_max_ms, 2),
        first_event_p95_ms=(round(metrics.first_event_p95_ms, 2) if metrics.first_event_p95_ms is not None else None),
        unstable_reasons=list(metrics.unstable_reasons),
    )


async def run_stage_probe(
    *,
    model_name: str,
    endpoint: str,
    url: str,
    raw_api_key: str,
    prompt: str,
    max_output_tokens: int,
    concurrency: int,
    sample_requests: int,
    requests_per_concurrency: int,
    client_timeout_s: float,
    request_timeout_s: float,
    progress_interval_s: float,
    success_rate_threshold: float,
    timeout_error_threshold: int,
    busy_error_threshold: int,
    latency_p95_threshold_ms: float,
    first_event_p95_threshold_ms: float,
) -> StageMetrics:
    total_requests = min(
        MAX_BENCHMARK_STAGE_REQUESTS,
        max(sample_requests, concurrency * max(1, requests_per_concurrency)),
    )
    results, duration_s = await execute_stage(
        model_name=model_name,
        endpoint=endpoint,
        url=url,
        raw_api_key=raw_api_key,
        prompt=prompt,
        max_output_tokens=max_output_tokens,
        concurrency=concurrency,
        total_requests=total_requests,
        client_timeout_s=client_timeout_s,
        request_timeout_s=request_timeout_s,
        progress_interval_s=progress_interval_s,
    )
    metrics = summarize_stage(
        model_name=model_name,
        endpoint=endpoint,
        concurrency=concurrency,
        total_requests=total_requests,
        duration_s=duration_s,
        results=results,
        success_rate_threshold=success_rate_threshold,
        timeout_error_threshold=timeout_error_threshold,
        busy_error_threshold=busy_error_threshold,
        latency_p95_threshold_ms=latency_p95_threshold_ms,
        first_event_p95_threshold_ms=first_event_p95_threshold_ms,
    )
    print_stage_summary(metrics)
    return metrics


async def run_model_probe(
    *,
    model_name: str,
    endpoint: str,
    url: str,
    raw_api_key: str,
    prompt: str,
    max_output_tokens: int,
    concurrency_plan: list[int],
    sample_requests: int,
    requests_per_concurrency: int,
    warmup_requests: int,
    client_timeout_s: float,
    request_timeout_s: float,
    progress_interval_s: float,
    success_rate_threshold: float,
    timeout_error_threshold: int,
    busy_error_threshold: int,
    latency_p95_threshold_ms: float,
    first_event_p95_threshold_ms: float,
    boundary_max_rounds: int,
) -> ModelReport:
    print(f"\n[model-start] {model_name}")
    stage_by_concurrency: dict[int, StageMetrics] = {}
    highest_stable: int | None = None
    first_unstable: int | None = None

    if warmup_requests > 0:
        warmup_concurrency = min(max(1, concurrency_plan[0] // 2), warmup_requests, 20)
        print(f"[warmup] 模型={model_name} 并发={warmup_concurrency} 请求数={max(warmup_requests, warmup_concurrency)}")
        await run_stage_probe(
            model_name=model_name,
            endpoint=endpoint,
            url=url,
            raw_api_key=raw_api_key,
            prompt=prompt,
            max_output_tokens=max_output_tokens,
            concurrency=warmup_concurrency,
            sample_requests=max(warmup_requests, warmup_concurrency),
            requests_per_concurrency=1,
            client_timeout_s=max(1.0, client_timeout_s),
            request_timeout_s=max(1.0, request_timeout_s),
            progress_interval_s=max(0.1, progress_interval_s),
            success_rate_threshold=max(0.5, success_rate_threshold * 0.8),
            timeout_error_threshold=max(1, timeout_error_threshold),
            busy_error_threshold=max(1, busy_error_threshold),
            latency_p95_threshold_ms=0,
            first_event_p95_threshold_ms=0,
        )

    for concurrency in concurrency_plan:
        metrics = await run_stage_probe(
            model_name=model_name,
            endpoint=endpoint,
            url=url,
            raw_api_key=raw_api_key,
            prompt=prompt,
            max_output_tokens=max_output_tokens,
            concurrency=concurrency,
            sample_requests=sample_requests,
            requests_per_concurrency=requests_per_concurrency,
            client_timeout_s=client_timeout_s,
            request_timeout_s=request_timeout_s,
            progress_interval_s=progress_interval_s,
            success_rate_threshold=success_rate_threshold,
            timeout_error_threshold=timeout_error_threshold,
            busy_error_threshold=busy_error_threshold,
            latency_p95_threshold_ms=latency_p95_threshold_ms,
            first_event_p95_threshold_ms=first_event_p95_threshold_ms,
        )
        stage_by_concurrency[concurrency] = metrics
        if metrics.stable:
            highest_stable = concurrency
            continue
        first_unstable = concurrency
        break

    refine_rounds = 0
    while (
        highest_stable is not None
        and first_unstable is not None
        and (first_unstable - highest_stable) > 1
        and refine_rounds < max(0, boundary_max_rounds)
    ):
        concurrency = (highest_stable + first_unstable) // 2
        if concurrency in stage_by_concurrency:
            break
        print(f"[refine] 模型={model_name} 边界={highest_stable}-{first_unstable} 尝试并发={concurrency}")
        metrics = await run_stage_probe(
            model_name=model_name,
            endpoint=endpoint,
            url=url,
            raw_api_key=raw_api_key,
            prompt=prompt,
            max_output_tokens=max_output_tokens,
            concurrency=concurrency,
            sample_requests=sample_requests,
            requests_per_concurrency=requests_per_concurrency,
            client_timeout_s=client_timeout_s,
            request_timeout_s=request_timeout_s,
            progress_interval_s=progress_interval_s,
            success_rate_threshold=success_rate_threshold,
            timeout_error_threshold=timeout_error_threshold,
            busy_error_threshold=busy_error_threshold,
            latency_p95_threshold_ms=latency_p95_threshold_ms,
            first_event_p95_threshold_ms=first_event_p95_threshold_ms,
        )
        stage_by_concurrency[concurrency] = metrics
        if metrics.stable:
            highest_stable = concurrency
        else:
            first_unstable = concurrency
        refine_rounds += 1

    stage_results = [stage_by_concurrency[key] for key in sorted(stage_by_concurrency)]
    best_throughput = max((item.throughput_rps for item in stage_results), default=0.0)
    recommended = None
    recommended_latency_p95_ms = None
    recommended_first_event_p95_ms = None

    if highest_stable is None:
        summary = "未找到满足稳定阈值的并发档位。"
    elif first_unstable is None:
        recommended = highest_stable
        summary = f"当前探测范围内最高稳定并发达到 {highest_stable}，尚未触达上限。"
    else:
        recommended = max(1, int(math.floor(highest_stable * 0.8)))
        summary = (
            f"最高稳定并发 {highest_stable}，首次不稳定并发 {first_unstable}，"
            f"建议日常保守运行并发 {recommended}。"
        )

    recommended_stage = next((item for item in stage_results if item.concurrency == recommended), None)
    if recommended_stage is not None:
        recommended_latency_p95_ms = recommended_stage.latency_p95_ms
        recommended_first_event_p95_ms = recommended_stage.first_event_p95_ms

    print(f"[model-summary] {model_name} {summary}")
    emit_event("model_summary", model_name=model_name, summary=summary)

    return ModelReport(
        model_name=model_name,
        endpoint=endpoint,
        stage_results=stage_results,
        stable_concurrency_upper_limit=highest_stable,
        first_unstable_concurrency=first_unstable,
        recommended_concurrency=recommended,
        best_throughput_rps=round(best_throughput, 2) if best_throughput else None,
        recommended_latency_p95_ms=(round(recommended_latency_p95_ms, 2) if recommended_latency_p95_ms is not None else None),
        recommended_first_event_p95_ms=(
            round(recommended_first_event_p95_ms, 2) if recommended_first_event_p95_ms is not None else None
        ),
        summary=summary,
    )


def as_serializable(report: BenchmarkReport) -> dict[str, Any]:
    return {
        "started_at": report.started_at,
        "finished_at": report.finished_at,
        "proxy_base_url": report.proxy_base_url,
        "endpoint": report.endpoint,
        "model_names": report.model_names,
        "max_output_tokens": report.max_output_tokens,
        "stage_request_floor": report.stage_request_floor,
        "requests_per_concurrency": report.requests_per_concurrency,
        "success_rate_threshold": report.success_rate_threshold,
        "timeout_error_threshold": report.timeout_error_threshold,
        "busy_error_threshold": report.busy_error_threshold,
        "latency_p95_threshold_ms": report.latency_p95_threshold_ms,
        "first_event_p95_threshold_ms": report.first_event_p95_threshold_ms,
        "html_report_path": report.html_report_path,
        "json_report_path": report.json_report_path,
        "model_reports": [
            {
                "model_name": item.model_name,
                "endpoint": item.endpoint,
                "stable_concurrency_upper_limit": item.stable_concurrency_upper_limit,
                "first_unstable_concurrency": item.first_unstable_concurrency,
                "recommended_concurrency": item.recommended_concurrency,
                "best_throughput_rps": item.best_throughput_rps,
                "recommended_latency_p95_ms": item.recommended_latency_p95_ms,
                "recommended_first_event_p95_ms": item.recommended_first_event_p95_ms,
                "summary": item.summary,
                "stage_results": [asdict(stage) for stage in item.stage_results],
            }
            for item in report.model_reports
        ],
    }


def svg_polyline(points: list[tuple[float, float]], *, stroke: str, width: int = 2) -> str:
    if not points:
        return ""
    serialized = " ".join(f"{x:.2f},{y:.2f}" for x, y in points)
    return f'<polyline fill="none" stroke="{stroke}" stroke-width="{width}" points="{serialized}" />'


def build_line_chart_svg(
    stage_results: list[StageMetrics],
    *,
    value_getter: Any,
    width: int = 640,
    height: int = 220,
    stroke: str,
    label: str,
) -> str:
    values = [float(value_getter(item) or 0.0) for item in stage_results]
    if not values:
        return "<div>无数据</div>"
    left = 52
    top = 16
    chart_width = width - left - 16
    chart_height = height - top - 34
    max_value = max(values) or 1.0
    min_concurrency = min(item.concurrency for item in stage_results)
    max_concurrency = max(item.concurrency for item in stage_results)
    concurrency_span = max(1, max_concurrency - min_concurrency)
    points: list[tuple[float, float]] = []
    for item, value in zip(stage_results, values):
        x = left + ((item.concurrency - min_concurrency) / concurrency_span) * chart_width
        y = top + chart_height - ((value / max_value) * chart_height)
        points.append((x, y))

    grid_lines = []
    for index in range(5):
        y = top + (chart_height / 4) * index
        value = max_value * (1 - index / 4)
        grid_lines.append(
            f'<line x1="{left}" y1="{y:.2f}" x2="{left + chart_width}" y2="{y:.2f}" stroke="#d6dde5" stroke-width="1" />'
            f'<text x="6" y="{y + 4:.2f}" font-size="12" fill="#506070">{value:.2f}</text>'
        )
    x_labels = []
    for item in stage_results:
        x = left + ((item.concurrency - min_concurrency) / concurrency_span) * chart_width
        x_labels.append(
            f'<text x="{x:.2f}" y="{height - 8}" text-anchor="middle" font-size="12" fill="#506070">{item.concurrency}</text>'
        )
    point_dots = "".join(f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3.5" fill="{stroke}" />' for x, y in points)
    return (
        f'<svg width="{width}" height="{height}" viewBox="0 0 {width} {height}" role="img" aria-label="{escape(label)}">'
        f'{"".join(grid_lines)}'
        f'{svg_polyline(points, stroke=stroke, width=3)}'
        f"{point_dots}"
        f'{"".join(x_labels)}'
        f'<text x="{left}" y="12" font-size="13" fill="#243547">{escape(label)}</text>'
        f"</svg>"
    )


def build_stage_table(stage_results: list[StageMetrics]) -> str:
    rows = []
    for item in stage_results:
        reason = "；".join(item.unstable_reasons) if item.unstable_reasons else "-"
        rows.append(
            "<tr>"
            f"<td>{item.concurrency}</td>"
            f"<td>{item.total_requests}</td>"
            f"<td>{item.success_rate:.2%}</td>"
            f"<td>{item.throughput_rps:.2f}</td>"
            f"<td>{item.latency_p95_ms:.2f}</td>"
            f"<td>{(item.first_event_p95_ms or 0):.2f}</td>"
            f"<td>{item.latency_max_ms:.2f}</td>"
            f"<td>{'稳定' if item.stable else '不稳定'}</td>"
            f"<td>{escape(reason)}</td>"
            "</tr>"
        )
    return (
        "<table>"
        "<thead><tr><th>并发</th><th>请求数</th><th>成功率</th><th>RPS</th><th>P95(ms)</th><th>首事件 P95(ms)</th><th>最大延迟(ms)</th><th>状态</th><th>备注</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )


def build_html_report(report: BenchmarkReport) -> str:
    model_sections = []
    for item in report.model_reports:
        throughput_chart = build_line_chart_svg(
            item.stage_results,
            value_getter=lambda stage: stage.throughput_rps,
            stroke="#0f766e",
            label=f"{item.model_name} - 吞吐 RPS",
        )
        latency_chart = build_line_chart_svg(
            item.stage_results,
            value_getter=lambda stage: stage.latency_p95_ms,
            stroke="#d97706",
            label=f"{item.model_name} - P95 延迟(ms)",
        )
        first_event_chart = build_line_chart_svg(
            item.stage_results,
            value_getter=lambda stage: stage.first_event_p95_ms or 0.0,
            stroke="#2563eb",
            label=f"{item.model_name} - 首事件 P95(ms)",
        )
        model_sections.append(
            "<section class='model-section'>"
            f"<div class='section-head'><div><h2>{escape(item.model_name)}</h2><p>{escape(item.summary)}</p></div>"
            f"<span class='endpoint-pill'>{escape(item.endpoint)}</span></div>"
            "<div class='stat-grid'>"
            f"<div class='stat-card'><span>最高稳定并发</span><strong>{item.stable_concurrency_upper_limit or '-'}</strong></div>"
            f"<div class='stat-card'><span>首次不稳定并发</span><strong>{item.first_unstable_concurrency or '-'}</strong></div>"
            f"<div class='stat-card'><span>建议运行并发</span><strong>{item.recommended_concurrency or '-'}</strong></div>"
            f"<div class='stat-card'><span>最高吞吐 RPS</span><strong>{item.best_throughput_rps or '-'}</strong></div>"
            f"<div class='stat-card'><span>建议档位 P95</span><strong>{item.recommended_latency_p95_ms or '-'}</strong></div>"
            f"<div class='stat-card'><span>建议档位首事件 P95</span><strong>{item.recommended_first_event_p95_ms or '-'}</strong></div>"
            "</div>"
            "<div class='chart-grid'>"
            f"<div class='chart-card'>{throughput_chart}</div>"
            f"<div class='chart-card'>{latency_chart}</div>"
            f"<div class='chart-card'>{first_event_chart}</div>"
            "</div>"
            f"{build_stage_table(item.stage_results)}"
            "</section>"
        )

    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>真实并发探测报告</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #eef3f6;
      --surface: rgba(255,255,255,0.92);
      --line: rgba(15, 23, 42, 0.10);
      --text: #132030;
      --muted: #58697b;
      --accent: #0f766e;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      padding: 24px;
      background:
        radial-gradient(circle at top left, rgba(15,118,110,0.10), transparent 28%),
        linear-gradient(180deg, #f3f7fa 0%, #edf2f6 100%);
      color: var(--text);
      font-family: "IBM Plex Sans", "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    }}
    .page {{
      width: min(1480px, 100%);
      margin: 0 auto;
      display: grid;
      gap: 20px;
    }}
    .hero, .model-section {{
      border: 1px solid var(--line);
      border-radius: 24px;
      background: var(--surface);
      padding: 24px;
      backdrop-filter: blur(18px);
    }}
    h1, h2 {{ margin: 0 0 10px; letter-spacing: -0.03em; }}
    p {{ margin: 0; line-height: 1.7; color: var(--muted); }}
    .meta-grid, .stat-grid, .chart-grid {{
      display: grid;
      gap: 14px;
      margin-top: 18px;
    }}
    .meta-grid, .stat-grid {{
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    }}
    .chart-grid {{
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
    }}
    .stat-card, .chart-card {{
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 16px;
      background: rgba(255,255,255,0.78);
    }}
    .stat-card span {{
      display: block;
      font-size: 12px;
      color: var(--muted);
      letter-spacing: 0.06em;
      text-transform: uppercase;
    }}
    .stat-card strong {{
      display: block;
      margin-top: 10px;
      font-size: 28px;
      line-height: 1.1;
    }}
    .section-head {{
      display: flex;
      align-items: flex-start;
      justify-content: space-between;
      gap: 16px;
    }}
    .endpoint-pill {{
      display: inline-flex;
      align-items: center;
      justify-content: center;
      min-height: 32px;
      padding: 0 12px;
      border-radius: 999px;
      background: rgba(15,118,110,0.10);
      color: #0f766e;
      font-size: 12px;
      font-weight: 800;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      margin-top: 18px;
      overflow: hidden;
      border-radius: 16px;
      background: rgba(255,255,255,0.82);
    }}
    th, td {{
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      text-align: left;
      vertical-align: top;
      font-size: 14px;
    }}
    th {{
      color: var(--muted);
      font-weight: 700;
      background: rgba(15, 23, 42, 0.03);
    }}
    tr:last-child td {{ border-bottom: 0; }}
    @media (max-width: 768px) {{
      body {{ padding: 12px; }}
      .hero, .model-section {{ padding: 16px; border-radius: 20px; }}
      .section-head {{ flex-direction: column; align-items: stretch; }}
      table, thead, tbody, th, td, tr {{ display: block; }}
      thead {{ display: none; }}
      tr {{
        padding: 12px 0;
        border-bottom: 1px solid var(--line);
      }}
      td {{
        padding: 6px 0;
        border-bottom: 0;
      }}
    }}
  </style>
</head>
<body>
  <div class="page">
    <section class="hero">
      <h1>真实并发探测报告</h1>
      <p>仅通过项目对外流式文本接口，使用真实 API Key 和所选模型完成逐模型探测。报告聚焦成功率、吞吐、P95 延迟与首事件延迟。</p>
      <div class="meta-grid">
        <div class="stat-card"><span>开始时间</span><strong>{escape(report.started_at)}</strong></div>
        <div class="stat-card"><span>结束时间</span><strong>{escape(report.finished_at)}</strong></div>
        <div class="stat-card"><span>代理入口</span><strong>{escape(report.proxy_base_url)}</strong></div>
        <div class="stat-card"><span>流式端点</span><strong>{escape(report.endpoint)}</strong></div>
        <div class="stat-card"><span>模型数量</span><strong>{len(report.model_names)}</strong></div>
        <div class="stat-card"><span>输出上限 tok</span><strong>{report.max_output_tokens}</strong></div>
      </div>
    </section>
    {''.join(model_sections)}
  </div>
</body>
</html>"""


def save_report_files(report_dir: Path, report_prefix: str, report: BenchmarkReport) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    timestamp = now_beijing().strftime("%Y%m%d-%H%M%S")
    base_name = f"{report_prefix}-{timestamp}"
    json_path = report_dir / f"{base_name}.json"
    html_path = report_dir / f"{base_name}.html"
    report.json_report_path = str(json_path)
    report.html_report_path = str(html_path)
    json_path.write_text(json.dumps(as_serializable(report), ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(build_html_report(report), encoding="utf-8")
    return json_path, html_path


async def main_async(args: argparse.Namespace) -> int:
    model_names = parse_model_names(args.model_names)
    url = resolve_url(args.proxy_base_url, args.endpoint)
    concurrency_plan = build_concurrency_plan(args)

    print(f"proxy_url={url}")
    print(f"endpoint={args.endpoint}")
    print(f"model_names={json.dumps(model_names, ensure_ascii=False)}")
    print(f"concurrency_plan={json.dumps(concurrency_plan, ensure_ascii=False)}")
    print("mode=stream-only")

    started_at = now_beijing().strftime("%Y-%m-%d %H:%M:%S")
    model_reports: list[ModelReport] = []
    for model_name in model_names:
        model_report = await run_model_probe(
            model_name=model_name,
            endpoint=args.endpoint,
            url=url,
            raw_api_key=args.raw_api_key.strip(),
            prompt=args.prompt.strip() or DEFAULT_TEXT_PROMPT,
            max_output_tokens=max(1, args.max_output_tokens),
            concurrency_plan=concurrency_plan,
            sample_requests=max(1, args.sample_requests),
            requests_per_concurrency=max(1, args.requests_per_concurrency),
            warmup_requests=max(0, args.warmup_requests),
            client_timeout_s=max(1.0, args.client_timeout_s),
            request_timeout_s=max(1.0, args.request_timeout_s),
            progress_interval_s=max(0.1, args.progress_interval_s),
            success_rate_threshold=args.success_rate_threshold,
            timeout_error_threshold=max(0, args.timeout_error_threshold),
            busy_error_threshold=max(0, args.busy_error_threshold),
            latency_p95_threshold_ms=max(0.0, args.latency_p95_threshold_ms),
            first_event_p95_threshold_ms=max(0.0, args.first_event_p95_threshold_ms),
            boundary_max_rounds=max(0, args.boundary_max_rounds),
        )
        model_reports.append(model_report)

    report = BenchmarkReport(
        started_at=started_at,
        finished_at=now_beijing().strftime("%Y-%m-%d %H:%M:%S"),
        proxy_base_url=args.proxy_base_url.strip().rstrip("/"),
        endpoint=args.endpoint,
        model_names=model_names,
        max_output_tokens=max(1, args.max_output_tokens),
        stage_request_floor=max(1, args.sample_requests),
        requests_per_concurrency=max(1, args.requests_per_concurrency),
        success_rate_threshold=args.success_rate_threshold,
        timeout_error_threshold=max(0, args.timeout_error_threshold),
        busy_error_threshold=max(0, args.busy_error_threshold),
        latency_p95_threshold_ms=max(0.0, args.latency_p95_threshold_ms),
        first_event_p95_threshold_ms=max(0.0, args.first_event_p95_threshold_ms),
        model_reports=model_reports,
        html_report_path="",
        json_report_path="",
    )
    json_path, html_path = save_report_files(Path(args.report_dir), args.report_prefix, report)

    print("\n[result-summary]")
    for item in model_reports:
        print(f"- {item.model_name}: {item.summary}")
    print(f"json_report={json_path}")
    print(f"html_report={html_path}")
    emit_event("report", json_report_path=str(json_path), html_report_path=str(html_path))
    return 0


def main() -> int:
    args = normalize_args(parse_args())
    try:
        return asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("\n已中断并发探测。", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"真实并发探测失败: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
