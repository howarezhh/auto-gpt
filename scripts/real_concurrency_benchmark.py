from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import statistics
import subprocess
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Any

import httpx

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.real_upstream_functional_test import choose_chat_model, ensure_fixture, query_upstream_models


DEFAULT_PROXY_BASE_URL = "http://127.0.0.1:8000"
DEFAULT_UPSTREAM_BASE_URL = "https://aijh.huanmin.top/v1"
DEFAULT_REPORT_DIR = PROJECT_ROOT / "data" / "benchmark-reports"
DEFAULT_PROVIDER_NAME = "真实并发探测中转站"
DEFAULT_API_KEY_NAME = "真实并发探测密钥"
DEFAULT_USER_NAME = "真实并发测试用户"
DEFAULT_USER_EMAIL = "real-benchmark-user@example.com"
DEFAULT_CANDIDATE_MODELS = ("gpt-5.5", "gpt-5.4", "gpt-4o-mini")
DEFAULT_TEXT_PROMPT = "请用两句话简洁回答：并发压测探针。"


@dataclass(slots=True)
class RequestResult:
    ok: bool
    status_code: int | None
    total_latency_ms: float
    first_byte_latency_ms: float | None
    bytes_read: int
    error: str | None


@dataclass(slots=True)
class StageMetrics:
    mode: str
    endpoint: str
    concurrency: int
    total_requests: int
    wall_time_s: float
    success_requests: int
    failed_requests: int
    success_rate: float
    throughput_rps: float
    total_bytes: int
    latency_avg_ms: float
    latency_p50_ms: float
    latency_p95_ms: float
    latency_p99_ms: float
    latency_max_ms: float
    first_byte_avg_ms: float | None
    first_byte_p50_ms: float | None
    first_byte_p95_ms: float | None
    first_byte_max_ms: float | None
    status_counts: dict[str, int]
    error_counts: dict[str, int]
    stable: bool
    unstable_reasons: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ModeReport:
    mode: str
    endpoint: str
    stage_results: list[StageMetrics]
    stable_concurrency_upper_limit: int | None
    first_unstable_concurrency: int | None
    recommended_concurrency: int | None
    summary: str


@dataclass(slots=True)
class BenchmarkReport:
    started_at: str
    finished_at: str
    proxy_base_urls: list[str]
    endpoint: str
    model_name: str
    used_fixture: bool
    stage_request_floor: int
    requests_per_concurrency: int
    success_rate_threshold: float
    timeout_error_threshold: int
    busy_error_threshold: int
    mode_reports: list[ModeReport]
    html_report_path: str
    json_report_path: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="真实并发探测脚本：对项目的流式/非流式文本请求做分阶段并发测试，并输出可视化报告。"
    )
    parser.add_argument("--proxy-base-url", default=DEFAULT_PROXY_BASE_URL, help="代理服务地址，支持逗号分隔多个地址。")
    parser.add_argument("--endpoint", choices=["chat", "responses"], default="chat", help="压测入口类型。")
    parser.add_argument(
        "--modes",
        default="json,stream",
        help="测试模式，支持 json、stream，逗号分隔；默认同时测试非流式和流式。",
    )
    parser.add_argument("--concurrency", type=int, default=100, help="基准并发数，默认 100。")
    parser.add_argument(
        "--probe-min-concurrency",
        type=int,
        default=0,
        help="探测起始并发，默认自动取 max(10, 基准并发/4)。",
    )
    parser.add_argument(
        "--probe-max-concurrency",
        type=int,
        default=0,
        help="探测最大并发，默认自动取 max(基准并发*8, 400)。",
    )
    parser.add_argument("--max-probe-rounds", type=int, default=10, help="每个模式最多探测轮数。")
    parser.add_argument("--probe-scale", type=float, default=1.5, help="逐轮放大倍率，默认 1.5。")
    parser.add_argument("--sample-requests", type=int, default=300, help="每轮最少请求数。")
    parser.add_argument(
        "--requests-per-concurrency",
        type=int,
        default=2,
        help="每轮至少按 并发数 * 该系数 发请求，避免高并发下样本不足。",
    )
    parser.add_argument("--warmup-requests", type=int, default=12, help="正式探测前的预热请求数。")
    parser.add_argument("--client-timeout-s", type=float, default=180.0, help="单请求客户端超时秒数。")
    parser.add_argument("--request-timeout-s", type=float, default=180.0, help="httpx 超时秒数。")
    parser.add_argument("--boundary-max-rounds", type=int, default=8, help="首次失稳后继续细化边界的最大轮数。")
    parser.add_argument("--progress-interval-s", type=float, default=0.5, help="进度刷新间隔秒数。")
    parser.add_argument("--success-rate-threshold", type=float, default=0.99, help="判定稳定的最低成功率。")
    parser.add_argument("--timeout-error-threshold", type=int, default=0, help="判定稳定时允许的超时错误个数。")
    parser.add_argument("--busy-error-threshold", type=int, default=0, help="判定稳定时允许的 429/503/504 个数。")
    parser.add_argument(
        "--raw-api-key",
        default=os.environ.get("REAL_BENCHMARK_RAW_API_KEY", ""),
        help="若已存在可用 API Key，可直接传入；为空时优先读取 REAL_BENCHMARK_RAW_API_KEY，否则脚本会尝试自动创建夹具。",
    )
    parser.add_argument("--upstream-base-url", default=DEFAULT_UPSTREAM_BASE_URL, help="自动创建夹具时使用的上游地址。")
    parser.add_argument("--upstream-api-key", default=os.environ.get("REAL_UPSTREAM_API_KEY", ""), help="自动创建夹具时使用的真实上游 Key。")
    parser.add_argument("--provider-name", default=DEFAULT_PROVIDER_NAME)
    parser.add_argument("--api-key-name", default=DEFAULT_API_KEY_NAME)
    parser.add_argument("--user-name", default=DEFAULT_USER_NAME)
    parser.add_argument("--user-email", default=DEFAULT_USER_EMAIL)
    parser.add_argument("--model-name", default="", help="指定压测模型；为空时自动从上游模型列表中挑选。")
    parser.add_argument("--candidate-models", default=",".join(DEFAULT_CANDIDATE_MODELS))
    parser.add_argument("--prompt", default=DEFAULT_TEXT_PROMPT, help="纯文本压测提示词，不包含工具和图片。")
    parser.add_argument("--report-dir", default=str(DEFAULT_REPORT_DIR), help="报告输出目录。")
    parser.add_argument("--report-prefix", default="real-concurrency-benchmark", help="报告文件名前缀。")
    parser.add_argument("--spawn-local-proxy", action="store_true", help="若需要，可临时拉起本地 uvicorn 进行压测。")
    parser.add_argument("--proxy-port", type=int, default=8068, help="临时拉起本地代理时使用的端口。")
    return parser.parse_args()


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round((pct / 100) * (len(ordered) - 1)))))
    return ordered[index]


def parse_modes(raw_modes: str) -> list[str]:
    items = [item.strip().lower() for item in raw_modes.split(",") if item.strip()]
    valid = []
    for item in items:
        normalized = "json" if item in {"json", "non-stream", "nonstream"} else "stream" if item == "stream" else ""
        if normalized and normalized not in valid:
            valid.append(normalized)
    if not valid:
        raise ValueError("至少需要一个有效测试模式：json 或 stream")
    return valid


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


def build_payload(*, endpoint: str, model_name: str, stream: bool, prompt: str) -> dict[str, Any]:
    if endpoint == "responses":
        return {
            "model": model_name,
            "input": prompt,
            "stream": stream,
            "max_output_tokens": 128,
            "temperature": 0,
        }
    return {
        "model": model_name,
        "messages": [{"role": "user", "content": prompt}],
        "stream": stream,
        "max_tokens": 128,
        "temperature": 0,
    }


def resolve_urls(proxy_base_url: str, endpoint: str) -> list[str]:
    path = "/v1/responses" if endpoint == "responses" else "/v1/chat/completions"
    bases = [item.strip().rstrip("/") for item in proxy_base_url.split(",") if item.strip()]
    return [f"{base}{path}" for base in (bases or [DEFAULT_PROXY_BASE_URL])]


def classify_error(result: RequestResult) -> tuple[bool, bool]:
    if result.error is None:
        return False, False
    text = result.error.lower()
    is_timeout = "timeout" in text
    is_busy = result.status_code in {429, 503, 504} or any(code in text for code in ("http_429", "http_503", "http_504"))
    return is_timeout, is_busy


async def run_single_request(
    client: httpx.AsyncClient,
    *,
    url: str,
    headers: dict[str, str],
    payload: dict[str, Any],
    stream: bool,
) -> RequestResult:
    started = time.perf_counter()
    bytes_read = 0
    first_byte_latency_ms: float | None = None
    status_code: int | None = None
    error: str | None = None
    try:
        if stream:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                status_code = response.status_code
                async for chunk in response.aiter_bytes():
                    if first_byte_latency_ms is None:
                        first_byte_latency_ms = (time.perf_counter() - started) * 1000
                    bytes_read += len(chunk)
                if response.status_code >= 400:
                    error = f"http_{response.status_code}"
                elif bytes_read <= 0:
                    error = "empty_stream"
        else:
            response = await client.post(url, headers=headers, json=payload)
            status_code = response.status_code
            first_byte_latency_ms = (time.perf_counter() - started) * 1000
            bytes_read = len(response.content)
            if response.status_code >= 400:
                error = f"http_{response.status_code}"
            elif bytes_read <= 0:
                error = "empty_response"
            else:
                try:
                    response.json()
                except json.JSONDecodeError as exc:
                    error = f"invalid_json: {exc.msg}"
        total_latency_ms = (time.perf_counter() - started) * 1000
        return RequestResult(
            ok=(status_code is not None and status_code < 400 and error is None),
            status_code=status_code,
            total_latency_ms=total_latency_ms,
            first_byte_latency_ms=first_byte_latency_ms,
            bytes_read=bytes_read,
            error=error,
        )
    except Exception as exc:
        total_latency_ms = (time.perf_counter() - started) * 1000
        return RequestResult(
            ok=False,
            status_code=status_code,
            total_latency_ms=total_latency_ms,
            first_byte_latency_ms=first_byte_latency_ms,
            bytes_read=bytes_read,
            error=f"{type(exc).__name__}: {exc}",
        )


async def execute_stage(
    *,
    endpoint: str,
    mode: str,
    urls: list[str],
    raw_api_key: str,
    model_name: str,
    prompt: str,
    concurrency: int,
    total_requests: int,
    client_timeout_s: float,
    request_timeout_s: float,
    progress_interval_s: float,
) -> tuple[list[RequestResult], float]:
    stream = mode == "stream"
    headers = {"Authorization": f"Bearer {raw_api_key}"}
    payload = build_payload(endpoint=endpoint, model_name=model_name, stream=stream, prompt=prompt)
    queue: asyncio.Queue[int] = asyncio.Queue()
    for index in range(total_requests):
        queue.put_nowait(index)

    stats = {
        "done": 0,
        "success": 0,
        "failed": 0,
        "busy": 0,
        "timeout": 0,
    }
    results: list[RequestResult] = []
    limits = httpx.Limits(
        max_connections=max(concurrency * 2, 100),
        max_keepalive_connections=max(concurrency, 20),
    )
    timeout = httpx.Timeout(request_timeout_s)
    started = time.perf_counter()

    async def reporter() -> None:
        while stats["done"] < total_requests:
            elapsed = max(0.001, time.perf_counter() - started)
            rps = stats["done"] / elapsed
            percent = (stats["done"] / total_requests) * 100
            line = (
                f"\r[{mode} c={concurrency}] "
                f"{stats['done']}/{total_requests} "
                f"({percent:5.1f}%) "
                f"ok={stats['success']} fail={stats['failed']} "
                f"busy={stats['busy']} timeout={stats['timeout']} "
                f"rps={rps:7.2f}"
            )
            print(line, end="", flush=True)
            await asyncio.sleep(max(0.1, progress_interval_s))
        elapsed = max(0.001, time.perf_counter() - started)
        final_line = (
            f"\r[{mode} c={concurrency}] "
            f"{total_requests}/{total_requests} "
            f"(100.0%) "
            f"ok={stats['success']} fail={stats['failed']} "
            f"busy={stats['busy']} timeout={stats['timeout']} "
            f"rps={total_requests / elapsed:7.2f}"
        )
        print(final_line)

    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        async def worker() -> None:
            while True:
                try:
                    index = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    started = time.perf_counter()
                    try:
                        result = await asyncio.wait_for(
                            run_single_request(
                                client,
                                url=urls[index % len(urls)],
                                headers=headers,
                                payload=payload,
                                stream=stream,
                            ),
                            timeout=max(1.0, client_timeout_s),
                        )
                    except asyncio.TimeoutError:
                        result = RequestResult(
                            ok=False,
                            status_code=None,
                            total_latency_ms=(time.perf_counter() - started) * 1000,
                            first_byte_latency_ms=None,
                            bytes_read=0,
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

    return results, time.perf_counter() - started


async def run_stage_probe(
    *,
    endpoint: str,
    mode: str,
    urls: list[str],
    raw_api_key: str,
    model_name: str,
    prompt: str,
    concurrency: int,
    sample_requests: int,
    requests_per_concurrency: int,
    client_timeout_s: float,
    request_timeout_s: float,
    progress_interval_s: float,
    success_rate_threshold: float,
    timeout_error_threshold: int,
    busy_error_threshold: int,
) -> StageMetrics:
    total_requests = max(sample_requests, concurrency * max(1, requests_per_concurrency))
    results, duration_s = await execute_stage(
        endpoint=endpoint,
        mode=mode,
        urls=urls,
        raw_api_key=raw_api_key,
        model_name=model_name,
        prompt=prompt,
        concurrency=concurrency,
        total_requests=total_requests,
        client_timeout_s=client_timeout_s,
        request_timeout_s=request_timeout_s,
        progress_interval_s=progress_interval_s,
    )
    metrics = summarize_stage(
        endpoint=endpoint,
        mode=mode,
        concurrency=concurrency,
        total_requests=total_requests,
        duration_s=duration_s,
        results=results,
        success_rate_threshold=success_rate_threshold,
        timeout_error_threshold=timeout_error_threshold,
        busy_error_threshold=busy_error_threshold,
    )
    print_stage_summary(metrics)
    return metrics


def summarize_stage(
    *,
    endpoint: str,
    mode: str,
    concurrency: int,
    total_requests: int,
    duration_s: float,
    results: list[RequestResult],
    success_rate_threshold: float,
    timeout_error_threshold: int,
    busy_error_threshold: int,
) -> StageMetrics:
    latencies = [item.total_latency_ms for item in results]
    first_bytes = [item.first_byte_latency_ms for item in results if item.first_byte_latency_ms is not None]
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
    if success_rate < success_rate_threshold:
        unstable_reasons.append(f"成功率 {success_rate:.2%} 低于阈值 {success_rate_threshold:.2%}")
    if timeout_errors > timeout_error_threshold:
        unstable_reasons.append(f"超时错误 {timeout_errors} 超过阈值 {timeout_error_threshold}")
    if busy_errors > busy_error_threshold:
        unstable_reasons.append(f"429/503/504 错误 {busy_errors} 超过阈值 {busy_error_threshold}")

    return StageMetrics(
        mode=mode,
        endpoint=endpoint,
        concurrency=concurrency,
        total_requests=total_requests,
        wall_time_s=duration_s,
        success_requests=success_requests,
        failed_requests=failed_requests,
        success_rate=success_rate,
        throughput_rps=(len(results) / duration_s) if duration_s > 0 else 0.0,
        total_bytes=sum(item.bytes_read for item in results),
        latency_avg_ms=statistics.mean(latencies) if latencies else 0.0,
        latency_p50_ms=percentile(latencies, 50),
        latency_p95_ms=percentile(latencies, 95),
        latency_p99_ms=percentile(latencies, 99),
        latency_max_ms=max(latencies) if latencies else 0.0,
        first_byte_avg_ms=(statistics.mean(first_bytes) if first_bytes else None),
        first_byte_p50_ms=(percentile(first_bytes, 50) if first_bytes else None),
        first_byte_p95_ms=(percentile(first_bytes, 95) if first_bytes else None),
        first_byte_max_ms=(max(first_bytes) if first_bytes else None),
        status_counts=dict(status_counts),
        error_counts={key: value for key, value in error_counts.items() if key},
        stable=(not unstable_reasons),
        unstable_reasons=unstable_reasons,
    )


def print_stage_summary(metrics: StageMetrics) -> None:
    status_text = "稳定" if metrics.stable else "不稳定"
    print(
        f"[{metrics.mode} c={metrics.concurrency}] "
        f"{status_text} "
        f"success={metrics.success_requests}/{metrics.total_requests} "
        f"success_rate={metrics.success_rate:.2%} "
        f"rps={metrics.throughput_rps:.2f} "
        f"p95={metrics.latency_p95_ms:.2f}ms "
        f"max={metrics.latency_max_ms:.2f}ms"
    )
    if metrics.mode == "stream" and metrics.first_byte_p95_ms is not None:
        print(
            f"  first_byte_avg={metrics.first_byte_avg_ms:.2f}ms "
            f"first_byte_p95={metrics.first_byte_p95_ms:.2f}ms"
        )
    if metrics.status_counts:
        print(f"  status_counts={json.dumps(metrics.status_counts, ensure_ascii=False)}")
    if metrics.error_counts:
        print(f"  errors={json.dumps(metrics.error_counts, ensure_ascii=False)}")
    if metrics.unstable_reasons:
        print(f"  unstable_reasons={'；'.join(metrics.unstable_reasons)}")


async def run_mode_probe(
    *,
    endpoint: str,
    mode: str,
    urls: list[str],
    raw_api_key: str,
    model_name: str,
    prompt: str,
    concurrency_plan: list[int],
    sample_requests: int,
    requests_per_concurrency: int,
    client_timeout_s: float,
    request_timeout_s: float,
    progress_interval_s: float,
    success_rate_threshold: float,
    timeout_error_threshold: int,
    busy_error_threshold: int,
    boundary_max_rounds: int,
) -> ModeReport:
    stage_by_concurrency: dict[int, StageMetrics] = {}
    highest_stable: int | None = None
    first_unstable: int | None = None

    for concurrency in concurrency_plan:
        metrics = await run_stage_probe(
            endpoint=endpoint,
            mode=mode,
            urls=urls,
            raw_api_key=raw_api_key,
            model_name=model_name,
            prompt=prompt,
            concurrency=concurrency,
            sample_requests=sample_requests,
            requests_per_concurrency=requests_per_concurrency,
            client_timeout_s=client_timeout_s,
            request_timeout_s=request_timeout_s,
            progress_interval_s=progress_interval_s,
            success_rate_threshold=success_rate_threshold,
            timeout_error_threshold=timeout_error_threshold,
            busy_error_threshold=busy_error_threshold,
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
        print(f"[refine] mode={mode} boundary={highest_stable}-{first_unstable} try={concurrency}")
        metrics = await run_stage_probe(
            endpoint=endpoint,
            mode=mode,
            urls=urls,
            raw_api_key=raw_api_key,
            model_name=model_name,
            prompt=prompt,
            concurrency=concurrency,
            sample_requests=sample_requests,
            requests_per_concurrency=requests_per_concurrency,
            client_timeout_s=client_timeout_s,
            request_timeout_s=request_timeout_s,
            progress_interval_s=progress_interval_s,
            success_rate_threshold=success_rate_threshold,
            timeout_error_threshold=timeout_error_threshold,
            busy_error_threshold=busy_error_threshold,
        )
        stage_by_concurrency[concurrency] = metrics
        if metrics.stable:
            highest_stable = concurrency
        else:
            first_unstable = concurrency
        refine_rounds += 1

    stage_results = [stage_by_concurrency[key] for key in sorted(stage_by_concurrency)]
    if highest_stable is None:
        summary = "未找到满足稳定阈值的并发档位。"
        recommended = None
    elif first_unstable is None:
        summary = f"在当前探测范围内，最高稳定并发达到 {highest_stable}，尚未触达真实上限。"
        recommended = highest_stable
    else:
        recommended = max(1, int(math.floor(highest_stable * 0.8)))
        summary = (
            f"最高稳定并发为 {highest_stable}，首次不稳定档位为 {first_unstable}，"
            f"经边界细化后当前可确认稳定上限约为 {highest_stable}，"
            f"建议日常保守运行并发约 {recommended}。"
        )

    return ModeReport(
        mode=mode,
        endpoint=endpoint,
        stage_results=stage_results,
        stable_concurrency_upper_limit=highest_stable,
        first_unstable_concurrency=first_unstable,
        recommended_concurrency=recommended,
        summary=summary,
    )


def wait_for_live(base_url: str, *, timeout_s: float = 45.0) -> bool:
    deadline = time.time() + timeout_s
    url = base_url.rstrip("/") + "/live"
    while time.time() < deadline:
        try:
            response = httpx.get(url, timeout=2.0)
            if response.status_code == 200:
                return True
        except Exception:
            pass
        time.sleep(0.5)
    return False


def terminate_process(process: subprocess.Popen[bytes] | None, *, timeout_s: float = 10.0) -> None:
    if process is None or process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=timeout_s)
    except Exception:
        try:
            process.kill()
        except Exception:
            pass


def spawn_local_proxy(port: int) -> subprocess.Popen[bytes]:
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", str(PROJECT_ROOT))
    env.setdefault("ENABLE_BACKGROUND_WORKERS", "true")
    env.setdefault("ENABLE_STARTUP_DB_INIT", "false")
    env.setdefault("ENABLE_SCHEDULER", "false")
    env.setdefault("ASYNC_REQUEST_LOG_ENABLED", "false")
    log_dir = PROJECT_ROOT / "data" / "benchmark-reports"
    log_dir.mkdir(parents=True, exist_ok=True)
    stdout_file = (log_dir / "real-concurrency-proxy.out.log").open("wb")
    stderr_file = (log_dir / "real-concurrency-proxy.err.log").open("wb")
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--no-access-log",
            "--log-level",
            "warning",
        ],
        cwd=PROJECT_ROOT,
        env=env,
        stdout=stdout_file,
        stderr=stderr_file,
        creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0,
    )
    process._codex_stdout_file = stdout_file  # type: ignore[attr-defined]
    process._codex_stderr_file = stderr_file  # type: ignore[attr-defined]
    return process


def close_spawned_log_handles(process: subprocess.Popen[bytes] | None) -> None:
    for attr in ("_codex_stdout_file", "_codex_stderr_file"):
        handle = getattr(process, attr, None)
        if handle:
            try:
                handle.close()
            except Exception:
                pass


def resolve_model_name(args: argparse.Namespace) -> str:
    if args.model_name.strip():
        return args.model_name.strip()
    upstream_api_key = args.upstream_api_key.strip()
    if not upstream_api_key:
        return DEFAULT_CANDIDATE_MODELS[-1]
    upstream_models = query_upstream_models(args.upstream_base_url, upstream_api_key)
    candidate_models = tuple(item.strip() for item in args.candidate_models.split(",") if item.strip())
    model_name = choose_chat_model(upstream_models, candidate_models=candidate_models)
    if model_name:
        return model_name
    if upstream_models:
        return upstream_models[0]
    return DEFAULT_CANDIDATE_MODELS[-1]


def prepare_api_key_and_model(args: argparse.Namespace) -> tuple[str, str, bool]:
    model_name = resolve_model_name(args)
    if args.raw_api_key.strip():
        return args.raw_api_key.strip(), model_name, False
    upstream_api_key = args.upstream_api_key.strip()
    if not upstream_api_key:
        raise RuntimeError("未提供 --raw-api-key，且 REAL_UPSTREAM_API_KEY / --upstream-api-key 也为空，无法自动创建真实夹具。")
    fixture = ensure_fixture(
        upstream_base_url=args.upstream_base_url,
        upstream_api_key=upstream_api_key,
        provider_name=args.provider_name,
        api_key_name=args.api_key_name,
        user_name=args.user_name,
        user_email=args.user_email,
        model_name=model_name,
    )
    return str(fixture["raw_api_key"]), model_name, True


def as_serializable(report: BenchmarkReport) -> dict[str, Any]:
    return {
        "started_at": report.started_at,
        "finished_at": report.finished_at,
        "proxy_base_urls": report.proxy_base_urls,
        "endpoint": report.endpoint,
        "model_name": report.model_name,
        "used_fixture": report.used_fixture,
        "stage_request_floor": report.stage_request_floor,
        "requests_per_concurrency": report.requests_per_concurrency,
        "success_rate_threshold": report.success_rate_threshold,
        "timeout_error_threshold": report.timeout_error_threshold,
        "busy_error_threshold": report.busy_error_threshold,
        "html_report_path": report.html_report_path,
        "json_report_path": report.json_report_path,
        "mode_reports": [
            {
                "mode": item.mode,
                "endpoint": item.endpoint,
                "stable_concurrency_upper_limit": item.stable_concurrency_upper_limit,
                "first_unstable_concurrency": item.first_unstable_concurrency,
                "recommended_concurrency": item.recommended_concurrency,
                "summary": item.summary,
                "stage_results": [asdict(stage) for stage in item.stage_results],
            }
            for item in report.mode_reports
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
    width: int = 680,
    height: int = 240,
    stroke: str = "#0f766e",
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
    point_dots = "".join(
        f'<circle cx="{x:.2f}" cy="{y:.2f}" r="3.5" fill="{stroke}" />' for x, y in points
    )
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
        first_byte_cell = (
            f"<td>{item.first_byte_p95_ms:.2f}</td>"
            if item.first_byte_p95_ms is not None
            else "<td>-</td>"
        )
        rows.append(
            "<tr>"
            f"<td>{item.concurrency}</td>"
            f"<td>{item.total_requests}</td>"
            f"<td>{item.success_rate:.2%}</td>"
            f"<td>{item.throughput_rps:.2f}</td>"
            f"<td>{item.latency_p95_ms:.2f}</td>"
            f"<td>{item.latency_max_ms:.2f}</td>"
            f"{first_byte_cell}"
            f"<td>{'稳定' if item.stable else '不稳定'}</td>"
            f"<td>{escape(reason)}</td>"
            "</tr>"
        )
    return (
        "<table>"
        "<thead><tr><th>并发</th><th>请求数</th><th>成功率</th><th>RPS</th><th>P95(ms)</th><th>最大延迟(ms)</th><th>首包 P95(ms)</th><th>状态</th><th>备注</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody>"
        "</table>"
    )


def build_html_report(report: BenchmarkReport) -> str:
    sections = []
    for item in report.mode_reports:
        throughput_chart = build_line_chart_svg(
            item.stage_results,
            value_getter=lambda stage: stage.throughput_rps,
            stroke="#0f766e",
            label=f"{item.mode} - 吞吐 RPS",
        )
        success_chart = build_line_chart_svg(
            item.stage_results,
            value_getter=lambda stage: stage.success_rate * 100,
            stroke="#2563eb",
            label=f"{item.mode} - 成功率(%)",
        )
        p95_chart = build_line_chart_svg(
            item.stage_results,
            value_getter=lambda stage: stage.latency_p95_ms,
            stroke="#d97706",
            label=f"{item.mode} - P95 延迟(ms)",
        )
        sections.append(
            "<section class='mode-section'>"
            f"<h2>{escape(item.mode)} 模式</h2>"
            f"<p class='summary'>{escape(item.summary)}</p>"
            "<div class='stat-grid'>"
            f"<div class='stat-card'><span>最高稳定并发</span><strong>{item.stable_concurrency_upper_limit or '-'}</strong></div>"
            f"<div class='stat-card'><span>首次不稳定并发</span><strong>{item.first_unstable_concurrency or '-'}</strong></div>"
            f"<div class='stat-card'><span>建议运行并发</span><strong>{item.recommended_concurrency or '-'}</strong></div>"
            "</div>"
            "<div class='chart-grid'>"
            f"<div class='chart-card'>{throughput_chart}</div>"
            f"<div class='chart-card'>{success_chart}</div>"
            f"<div class='chart-card chart-card-wide'>{p95_chart}</div>"
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
      font-family: "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif;
    }}
    .page {{
      width: min(1400px, 100%);
      margin: 0 auto;
      display: grid;
      gap: 20px;
    }}
    .hero, .mode-section {{
      border: 1px solid var(--line);
      border-radius: 24px;
      background: var(--surface);
      padding: 24px;
      backdrop-filter: blur(18px);
    }}
    h1, h2 {{ margin: 0 0 12px; letter-spacing: -0.03em; }}
    p {{ margin: 0; line-height: 1.7; color: var(--muted); }}
    .meta-grid, .stat-grid, .chart-grid {{
      display: grid;
      gap: 14px;
    }}
    .meta-grid, .stat-grid {{
      grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
      margin-top: 18px;
    }}
    .chart-grid {{
      grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
      margin-top: 18px;
    }}
    .chart-card, .stat-card {{
      border: 1px solid var(--line);
      border-radius: 18px;
      padding: 16px;
      background: rgba(255,255,255,0.78);
    }}
    .chart-card-wide {{ grid-column: 1 / -1; }}
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
    .summary {{
      margin-top: 4px;
      font-size: 15px;
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
    td {{
      color: var(--text);
      line-height: 1.6;
    }}
    tr:last-child td {{ border-bottom: 0; }}
    @media (max-width: 768px) {{
      body {{ padding: 12px; }}
      .hero, .mode-section {{ padding: 16px; border-radius: 20px; }}
      .chart-grid {{ grid-template-columns: 1fr; }}
      .chart-card-wide {{ grid-column: auto; }}
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
      <p>面向纯文本流式 / 非流式请求的真实上游并发压测结果。脚本会按并发阶梯逐步探测，找到当前稳定并发上限并给出建议运行值。</p>
      <div class="meta-grid">
        <div class="stat-card"><span>开始时间</span><strong>{escape(report.started_at)}</strong></div>
        <div class="stat-card"><span>结束时间</span><strong>{escape(report.finished_at)}</strong></div>
        <div class="stat-card"><span>代理入口</span><strong>{escape(", ".join(report.proxy_base_urls))}</strong></div>
        <div class="stat-card"><span>压测端点</span><strong>{escape(report.endpoint)}</strong></div>
        <div class="stat-card"><span>模型</span><strong>{escape(report.model_name)}</strong></div>
        <div class="stat-card"><span>自动夹具</span><strong>{"是" if report.used_fixture else "否"}</strong></div>
      </div>
    </section>
    {''.join(sections)}
  </div>
</body>
</html>"""


def save_report_files(report_dir: Path, report_prefix: str, report: BenchmarkReport) -> tuple[Path, Path]:
    report_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base_name = f"{report_prefix}-{timestamp}"
    json_path = report_dir / f"{base_name}.json"
    html_path = report_dir / f"{base_name}.html"
    json_path.write_text(json.dumps(as_serializable(report), ensure_ascii=False, indent=2), encoding="utf-8")
    html_path.write_text(build_html_report(report), encoding="utf-8")
    return json_path, html_path


async def main_async(args: argparse.Namespace) -> int:
    modes = parse_modes(args.modes)
    raw_api_key, model_name, used_fixture = prepare_api_key_and_model(args)
    base_url = args.proxy_base_url
    proxy_process: subprocess.Popen[bytes] | None = None
    if args.spawn_local_proxy:
        base_url = f"http://127.0.0.1:{args.proxy_port}"
        proxy_process = spawn_local_proxy(args.proxy_port)
        if not wait_for_live(base_url, timeout_s=45.0):
            terminate_process(proxy_process)
            close_spawned_log_handles(proxy_process)
            raise RuntimeError(f"本地代理未在 {base_url} 成功启动。")

    try:
        urls = resolve_urls(base_url, args.endpoint)
        concurrency_plan = build_concurrency_plan(args)
        print(f"proxy_urls={json.dumps(urls, ensure_ascii=False)}")
        print(f"endpoint={args.endpoint}")
        print(f"model_name={model_name}")
        print(f"modes={json.dumps(modes, ensure_ascii=False)}")
        print(f"concurrency_plan={json.dumps(concurrency_plan, ensure_ascii=False)}")
        print(f"used_fixture={used_fixture}")

        if args.warmup_requests > 0:
            warmup_mode = modes[0]
            warmup_concurrency = min(max(1, args.concurrency // 5), args.warmup_requests, 20)
            print(f"\n[warmup] mode={warmup_mode} concurrency={warmup_concurrency} requests={args.warmup_requests}")
            warmup_results, warmup_duration = await execute_stage(
                endpoint=args.endpoint,
                mode=warmup_mode,
                urls=urls,
                raw_api_key=raw_api_key,
                model_name=model_name,
                prompt=args.prompt,
                concurrency=warmup_concurrency,
                total_requests=max(args.warmup_requests, warmup_concurrency),
                client_timeout_s=max(1.0, args.client_timeout_s),
                request_timeout_s=args.request_timeout_s,
                progress_interval_s=args.progress_interval_s,
            )
            warmup_metrics = summarize_stage(
                endpoint=args.endpoint,
                mode=warmup_mode,
                concurrency=warmup_concurrency,
                total_requests=max(args.warmup_requests, warmup_concurrency),
                duration_s=warmup_duration,
                results=warmup_results,
                success_rate_threshold=max(0.5, args.success_rate_threshold * 0.8),
                timeout_error_threshold=max(1, args.timeout_error_threshold),
                busy_error_threshold=max(1, args.busy_error_threshold),
            )
            print_stage_summary(warmup_metrics)

        started_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        mode_reports: list[ModeReport] = []
        for mode in modes:
            print(f"\n[probe] mode={mode}")
            mode_report = await run_mode_probe(
                endpoint=args.endpoint,
                mode=mode,
                urls=urls,
                raw_api_key=raw_api_key,
                model_name=model_name,
                prompt=args.prompt,
                concurrency_plan=concurrency_plan,
                sample_requests=max(1, args.sample_requests),
                requests_per_concurrency=max(1, args.requests_per_concurrency),
                client_timeout_s=max(1.0, args.client_timeout_s),
                request_timeout_s=max(1.0, args.request_timeout_s),
                progress_interval_s=max(0.1, args.progress_interval_s),
                success_rate_threshold=args.success_rate_threshold,
                timeout_error_threshold=max(0, args.timeout_error_threshold),
                busy_error_threshold=max(0, args.busy_error_threshold),
                boundary_max_rounds=max(0, args.boundary_max_rounds),
            )
            mode_reports.append(mode_report)

        report = BenchmarkReport(
            started_at=started_at,
            finished_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            proxy_base_urls=[item.strip().rstrip("/") for item in base_url.split(",") if item.strip()],
            endpoint=args.endpoint,
            model_name=model_name,
            used_fixture=used_fixture,
            stage_request_floor=max(1, args.sample_requests),
            requests_per_concurrency=max(1, args.requests_per_concurrency),
            success_rate_threshold=args.success_rate_threshold,
            timeout_error_threshold=max(0, args.timeout_error_threshold),
            busy_error_threshold=max(0, args.busy_error_threshold),
            mode_reports=mode_reports,
            html_report_path="",
            json_report_path="",
        )
        json_path, html_path = save_report_files(Path(args.report_dir), args.report_prefix, report)
        report.json_report_path = str(json_path)
        report.html_report_path = str(html_path)
        json_path.write_text(json.dumps(as_serializable(report), ensure_ascii=False, indent=2), encoding="utf-8")
        html_path.write_text(build_html_report(report), encoding="utf-8")

        print("\n[result-summary]")
        for item in mode_reports:
            print(f"- {item.mode}: {item.summary}")
        print(f"json_report={json_path}")
        print(f"html_report={html_path}")
        return 0
    finally:
        terminate_process(proxy_process)
        close_spawned_log_handles(proxy_process)


def main() -> int:
    args = parse_args()
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
