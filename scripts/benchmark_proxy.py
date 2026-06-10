import argparse
import asyncio
import json
import os
import sys
import statistics
import time
from collections import Counter
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import delete, func, select

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from app.database import SessionLocal
from app.models.api_client_billing_record import ApiClientBillingRecord
from app.models.api_client_key import ApiClientKey
from app.models.api_client_key_provider_binding import ApiClientKeyProviderBinding
from app.models.model_catalog import ModelCatalog
from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.models.request_log import RequestLog
from app.models.user_account import UserAccount
from app.models.user_account_billing_record import UserAccountBillingRecord
from app.services.api_key_service import ApiKeyService
from app.services.log_service import LogService
from app.services.request_log_queue_service import RequestLogQueueService


DEFAULT_PROVIDER_NAME = "benchmark-mock-provider"
DEFAULT_MODEL_NAME = "gpt-4o-mini"
DEFAULT_KEY_NAME = "benchmark-mock-key"
DEFAULT_USER_NAME = "benchmark_user"
DEFAULT_USER_EMAIL = "benchmark_user@example.com"
DEFAULT_RAW_KEY = "sk-aotu-benchmark-0123456789abcdefghijklmnopqrstuvwxyz"
MAX_BENCHMARK_REQUESTS = 10000
MAX_BENCHMARK_CONCURRENCY = 500
MAX_BENCHMARK_TIMEOUT_S = 600
MAX_BENCHMARK_WARMUP = 1000
MAX_BENCHMARK_RESPONSE_BYTES = 1 * 1024 * 1024
MAX_HTTP_CONNECTIONS = 1000
MAX_HTTP_KEEPALIVE_CONNECTIONS = 500
DEFAULT_FIXTURE_CAPACITY = 500
BENCHMARK_DELETE_BATCH_SIZE = 1000
BENCHMARK_BACKGROUND_IDLE_TIMEOUT_S = 10.0


@dataclass(slots=True)
class RequestResult:
    ok: bool
    status_code: int | None
    latency_ms: float
    bytes_read: int
    error: str | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a local load test against the aotu-gpt proxy.")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument("--mock-base-url", default="http://127.0.0.1:18081/v1")
    parser.add_argument("--endpoint", choices=["chat", "responses"], default="chat")
    parser.add_argument("--stream", action="store_true")
    parser.add_argument("--requests", type=int, default=200)
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--timeout-s", type=float, default=180.0)
    parser.add_argument("--warmup", type=int, default=10)
    parser.add_argument("--model-name", default=DEFAULT_MODEL_NAME)
    parser.add_argument("--provider-name", default=DEFAULT_PROVIDER_NAME)
    parser.add_argument("--api-key-name", default=DEFAULT_KEY_NAME)
    parser.add_argument("--raw-api-key", default=DEFAULT_RAW_KEY)
    return parser.parse_args()


def clamp_int(value: int, minimum: int, maximum: int) -> int:
    return max(minimum, min(maximum, int(value)))


def clamp_float(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, float(value)))


def normalize_args(args: argparse.Namespace) -> argparse.Namespace:
    args.requests = clamp_int(args.requests, 1, MAX_BENCHMARK_REQUESTS)
    args.concurrency = clamp_int(args.concurrency, 1, MAX_BENCHMARK_CONCURRENCY)
    args.timeout_s = clamp_float(args.timeout_s, 1.0, MAX_BENCHMARK_TIMEOUT_S)
    args.warmup = clamp_int(args.warmup, 0, MAX_BENCHMARK_WARMUP)
    return args


def delete_benchmark_rows_in_batches(db, model: type[Any], condition: Any, *, batch_size: int = BENCHMARK_DELETE_BATCH_SIZE) -> int:
    deleted = 0
    while True:
        ids = list(db.scalars(select(model.id).where(condition).limit(batch_size)))
        if not ids:
            return deleted
        result = db.execute(delete(model).where(model.id.in_(ids)))
        db.commit()
        deleted += int(result.rowcount or 0)
        if len(ids) < batch_size:
            return deleted


def ensure_benchmark_data(
    *,
    mock_base_url: str,
    model_name: str,
    provider_name: str,
    api_key_name: str,
    raw_api_key: str,
    capacity_limit: int = DEFAULT_FIXTURE_CAPACITY,
) -> dict[str, Any]:
    db = SessionLocal()
    try:
        user = (
            db.query(UserAccount)
            .filter(
                (UserAccount.username == DEFAULT_USER_NAME)
                | (UserAccount.email == DEFAULT_USER_EMAIL)
            )
            .first()
        )
        if user is None:
            user = UserAccount(
                username=DEFAULT_USER_NAME,
                email=DEFAULT_USER_EMAIL,
                password_hash="benchmark-only",
                role="user",
                enabled=True,
                balance_amount=Decimal("1000"),
                frozen_amount=Decimal("0"),
                total_recharge_amount=Decimal("1000"),
            )
            db.add(user)
            db.flush()

        provider = db.query(Provider).filter(Provider.name == provider_name).first()
        if provider is None:
            provider = Provider(name=provider_name, base_url=mock_base_url, api_key="mock-upstream-key")
            db.add(provider)
            db.flush()
        provider.base_url = mock_base_url
        provider.api_key = "mock-upstream-key"
        provider.provider_type = "openai_compatible"
        provider.enabled = True
        provider.priority = 1
        provider.timeout_ms = 30000
        provider.max_retries = 1
        provider.max_active_requests = capacity_limit
        provider.max_active_streams = capacity_limit
        provider.max_qps = capacity_limit
        provider.health_status = "healthy"
        provider.circuit_state = "closed"
        provider.failure_count = 0

        provider_model = (
            db.query(ProviderModel)
            .filter(ProviderModel.provider_id == provider.id, ProviderModel.model_name == model_name)
            .first()
        )
        if provider_model is None:
            provider_model = ProviderModel(provider_id=provider.id, model_name=model_name)
            db.add(provider_model)
            db.flush()
        provider_model.enabled = True
        provider_model.priority = 1
        provider_model.supports_stream = True
        provider_model.supports_vision = True
        provider_model.supports_tools = False
        provider_model.supports_chat_completions = True
        provider_model.supports_responses = True
        provider_model.health_status = "healthy"
        provider_model.circuit_state = "closed"
        provider_model.failure_count = 0
        provider_model.success_count = 0
        provider_model.context_window_tokens = 128000
        provider_model.max_input_tokens = 64000
        provider_model.max_output_tokens = 16384
        provider_model.price_multiplier = Decimal("1")
        provider_model.input_price_per_1k = Decimal("0")
        provider_model.output_price_per_1k = Decimal("0")
        provider_model.cache_price_per_1k = Decimal("0")

        model_catalog = db.query(ModelCatalog).filter(ModelCatalog.model_name == model_name).first()
        if model_catalog is None:
            model_catalog = ModelCatalog(model_name=model_name)
            db.add(model_catalog)
            db.flush()
        model_catalog.display_name = model_name
        model_catalog.enabled = True
        model_catalog.supports_stream = True
        model_catalog.supports_vision = True
        model_catalog.supports_tools = False
        model_catalog.supports_chat_completions = True
        model_catalog.supports_responses = True
        model_catalog.context_window_tokens = 128000
        model_catalog.max_input_tokens = 64000
        model_catalog.max_output_tokens = 16384
        model_catalog.input_price_per_1k = Decimal("0")
        model_catalog.output_price_per_1k = Decimal("0")
        model_catalog.cache_price_per_1k = Decimal("0")

        key_hash = ApiKeyService.hash_api_key(raw_api_key)
        api_key = db.query(ApiClientKey).filter(ApiClientKey.name == api_key_name).first()
        if api_key is None:
            api_key = db.query(ApiClientKey).filter(ApiClientKey.key_hash == key_hash).first()
        if api_key is None:
            api_key = ApiClientKey(
                name=api_key_name,
                key_prefix=ApiKeyService.extract_key_prefix(raw_api_key),
                key_hash=key_hash,
                raw_key_encrypted=ApiKeyService.encrypt_raw_api_key(raw_api_key),
            )
            db.add(api_key)
        api_key.name = api_key_name
        api_key.key_prefix = ApiKeyService.extract_key_prefix(raw_api_key)
        api_key.key_hash = key_hash
        api_key.raw_key_encrypted = ApiKeyService.encrypt_raw_api_key(raw_api_key)
        api_key.enabled = True
        api_key.owner_user_id = user.id
        api_key.balance_amount = Decimal("1000")
        api_key.total_recharge_amount = Decimal("1000")
        api_key.allowed_model_names_json = json.dumps([model_name], ensure_ascii=False)
        api_key.allowed_endpoint_paths_json = json.dumps(
            ["/v1/chat/completions", "/v1/responses", "/v1/models"],
            ensure_ascii=False,
        )
        api_key.allowed_source_ips_json = "[]"
        api_key.preferred_provider_ids_json = json.dumps([provider.id], ensure_ascii=False)
        api_key.preferred_region_tags_json = "[]"
        api_key.max_candidate_count = 1
        api_key.latency_bias = 1
        api_key.success_rate_bias = 1
        api_key.cost_bias = 0
        api_key.qps_limit = capacity_limit
        api_key.rpm_limit = capacity_limit * 60
        api_key.tpm_limit = None
        db.flush()

        binding = (
            db.query(ApiClientKeyProviderBinding)
            .filter(
                ApiClientKeyProviderBinding.api_client_key_id == api_key.id,
                ApiClientKeyProviderBinding.provider_id == provider.id,
            )
            .first()
        )
        if binding is None:
            db.add(ApiClientKeyProviderBinding(api_client_key_id=api_key.id, provider_id=provider.id))

        db.flush()
        db.commit()
        delete_benchmark_rows_in_batches(
            db,
            ApiClientBillingRecord,
            ApiClientBillingRecord.api_client_key_id == api_key.id,
        )
        delete_benchmark_rows_in_batches(
            db,
            UserAccountBillingRecord,
            UserAccountBillingRecord.api_client_key_id == api_key.id,
        )
        delete_benchmark_rows_in_batches(db, RequestLog, RequestLog.api_client_key_id == api_key.id)
        return {
            "provider_id": provider.id,
            "api_key_id": api_key.id,
            "raw_api_key": raw_api_key,
            "model_name": model_name,
        }
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def build_payload(*, endpoint: str, model_name: str, stream: bool) -> dict[str, Any]:
    if endpoint == "responses":
        return {
            "model": model_name,
            "input": "benchmark ping",
            "stream": stream,
            "max_output_tokens": 128,
        }
    return {
        "model": model_name,
        "messages": [{"role": "user", "content": "benchmark ping"}],
        "stream": stream,
        "max_tokens": 128,
    }


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
    status_code: int | None = None
    error: str | None = None
    try:
        if stream:
            async with client.stream("POST", url, headers=headers, json=payload) as response:
                status_code = response.status_code
                async for chunk in response.aiter_bytes():
                    bytes_read += len(chunk)
                    if bytes_read > MAX_BENCHMARK_RESPONSE_BYTES:
                        error = "response_too_large"
                        break
                if response.status_code >= 400:
                    error = f"http_{response.status_code}"
        else:
            response = await client.post(url, headers=headers, json=payload)
            status_code = response.status_code
            bytes_read = len(response.content)
            if response.status_code >= 400:
                error = f"http_{response.status_code}"
        latency_ms = (time.perf_counter() - started) * 1000
        return RequestResult(
            ok=(status_code is not None and status_code < 400 and error is None),
            status_code=status_code,
            latency_ms=latency_ms,
            bytes_read=bytes_read,
            error=error,
        )
    except Exception as exc:
        latency_ms = (time.perf_counter() - started) * 1000
        return RequestResult(ok=False, status_code=status_code, latency_ms=latency_ms, bytes_read=bytes_read, error=f"{type(exc).__name__}: {exc}")


def percentile(values: list[float], pct: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round((pct / 100) * (len(ordered) - 1)))))
    return ordered[index]


async def run_load(
    *,
    base_url: str,
    raw_api_key: str,
    endpoint: str,
    model_name: str,
    stream: bool,
    total_requests: int,
    concurrency: int,
    timeout_s: float,
) -> list[RequestResult]:
    path = "/v1/responses" if endpoint == "responses" else "/v1/chat/completions"
    base_urls = [
        item.strip().rstrip("/")
        for item in str(base_url).split(",")
        if str(item).strip()
    ]
    urls = [f"{item}{path}" for item in (base_urls or [base_url.rstrip("/")])]
    headers = {"Authorization": f"Bearer {raw_api_key}"}
    payload = build_payload(endpoint=endpoint, model_name=model_name, stream=stream)
    results: list[RequestResult] = []
    queue: asyncio.Queue[int] = asyncio.Queue()
    for index in range(total_requests):
        queue.put_nowait(index)

    max_connections = min(max(concurrency * 2, 100), MAX_HTTP_CONNECTIONS)
    max_keepalive_connections = min(max(concurrency, 20), MAX_HTTP_KEEPALIVE_CONNECTIONS)
    limits = httpx.Limits(max_connections=max_connections, max_keepalive_connections=max_keepalive_connections)
    timeout = httpx.Timeout(timeout_s)
    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        async def worker() -> None:
            while True:
                try:
                    index = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    result = await run_single_request(
                        client,
                        url=urls[index % len(urls)],
                        headers=headers,
                        payload=payload,
                        stream=stream,
                    )
                    results.append(result)
                finally:
                    queue.task_done()

        tasks = [asyncio.create_task(worker()) for _ in range(max(1, concurrency))]
        await queue.join()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return results


def print_summary(*, title: str, duration_s: float, results: list[RequestResult]) -> None:
    latencies = [item.latency_ms for item in results]
    ok_results = [item for item in results if item.ok]
    status_counts = Counter(item.status_code for item in results)
    error_counts = Counter(item.error for item in results if item.error)
    total_bytes = sum(item.bytes_read for item in results)
    rps = (len(results) / duration_s) if duration_s > 0 else 0.0

    print(f"\n[{title}]")
    print(f"total_requests={len(results)}")
    print(f"success_requests={len(ok_results)}")
    print(f"failed_requests={len(results) - len(ok_results)}")
    print(f"wall_time_s={duration_s:.3f}")
    print(f"throughput_rps={rps:.2f}")
    print(f"latency_avg_ms={statistics.mean(latencies):.2f}" if latencies else "latency_avg_ms=0.00")
    print(f"latency_p50_ms={percentile(latencies, 50):.2f}")
    print(f"latency_p95_ms={percentile(latencies, 95):.2f}")
    print(f"latency_max_ms={max(latencies):.2f}" if latencies else "latency_max_ms=0.00")
    print(f"total_bytes={total_bytes}")
    print(f"status_counts={json.dumps({str(k): v for k, v in status_counts.items()}, ensure_ascii=False)}")
    if error_counts:
        print(f"errors={json.dumps(dict(error_counts), ensure_ascii=False)}")


def _count_pending_benchmark_token_logs(api_key_id: int) -> int:
    db = SessionLocal()
    try:
        return int(
            db.scalar(
                select(func.count())
                .select_from(RequestLog)
                .where(
                    RequestLog.api_client_key_id == api_key_id,
                    LogService._pending_token_billing_finalize_expr(),
                )
            )
            or 0
        )
    finally:
        db.close()


async def wait_for_benchmark_background_idle(api_key_id: int, *, timeout_s: float = BENCHMARK_BACKGROUND_IDLE_TIMEOUT_S) -> None:
    queue_state = await RequestLogQueueService.wait_until_idle(timeout_seconds=timeout_s)
    deadline = time.monotonic() + timeout_s
    pending_token_logs = _count_pending_benchmark_token_logs(api_key_id)
    while pending_token_logs > 0 and time.monotonic() < deadline:
        await asyncio.sleep(0.1)
        pending_token_logs = _count_pending_benchmark_token_logs(api_key_id)
    print(
        "benchmark_background_idle="
        + json.dumps(
            {
                "request_log_queue": queue_state,
                "pending_token_logs": pending_token_logs,
            },
            ensure_ascii=False,
        )
    )


async def main_async() -> None:
    args = normalize_args(parse_args())
    capacity_limit = max(args.concurrency, min(DEFAULT_FIXTURE_CAPACITY, MAX_BENCHMARK_CONCURRENCY))
    prepared = ensure_benchmark_data(
        mock_base_url=args.mock_base_url,
        model_name=args.model_name,
        provider_name=args.provider_name,
        api_key_name=args.api_key_name,
        raw_api_key=args.raw_api_key,
        capacity_limit=capacity_limit,
    )

    if args.warmup > 0:
        warmup_started = time.perf_counter()
        warmup_results = await run_load(
            base_url=args.base_url,
            raw_api_key=prepared["raw_api_key"],
            endpoint=args.endpoint,
            model_name=prepared["model_name"],
            stream=args.stream,
            total_requests=args.warmup,
            concurrency=min(args.concurrency, args.warmup),
            timeout_s=args.timeout_s,
        )
        print_summary(title="warmup", duration_s=time.perf_counter() - warmup_started, results=warmup_results)

    started = time.perf_counter()
    results = await run_load(
        base_url=args.base_url,
        raw_api_key=prepared["raw_api_key"],
        endpoint=args.endpoint,
        model_name=prepared["model_name"],
        stream=args.stream,
        total_requests=args.requests,
        concurrency=args.concurrency,
        timeout_s=args.timeout_s,
    )
    duration_s = time.perf_counter() - started
    await wait_for_benchmark_background_idle(int(prepared["api_key_id"]))

    print(f"benchmark_provider_id={prepared['provider_id']}")
    print(f"benchmark_api_key_id={prepared['api_key_id']}")
    print(f"benchmark_raw_api_key={prepared['raw_api_key']}")
    print_summary(title="benchmark", duration_s=duration_s, results=results)


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
