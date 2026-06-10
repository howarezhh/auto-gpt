from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

import httpx
from sqlalchemy import delete, select
from sqlalchemy.orm import load_only

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
from app.utils.json_utils import dumps_json


DEFAULT_PROVIDER_NAME = "real-upstream-functional-provider"
DEFAULT_USER_NAME = "real_upstream_functional_user"
DEFAULT_USER_EMAIL = "real_upstream_functional_user@example.com"
DEFAULT_API_KEY_NAME = "real-upstream-functional-key"
DEFAULT_CANDIDATE_MODELS = ("gpt-5.5", "gpt-5.4")
FUNCTIONAL_DELETE_BATCH_SIZE = 1000
MAX_UPSTREAM_MODEL_OPTIONS = 1000
MAX_FUNCTIONAL_STREAM_EVENTS = 1024
MAX_FUNCTIONAL_STREAM_BYTES = 1 * 1024 * 1024


@dataclass(slots=True)
class CallResult:
    label: str
    ok: bool
    status_code: int | None
    latency_ms: float
    stream_events: int = 0
    error: str | None = None


def delete_functional_rows_in_batches(db, model: type[Any], condition: Any, *, batch_size: int = FUNCTIONAL_DELETE_BATCH_SIZE) -> int:
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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run a real upstream functional test through the local proxy.")
    parser.add_argument("--proxy-base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--upstream-base-url", default="https://aijh.huanmin.top/v1")
    parser.add_argument("--upstream-api-key", default=os.environ.get("REAL_UPSTREAM_API_KEY", ""))
    parser.add_argument("--provider-name", default=DEFAULT_PROVIDER_NAME)
    parser.add_argument("--api-key-name", default=DEFAULT_API_KEY_NAME)
    parser.add_argument("--user-name", default=DEFAULT_USER_NAME)
    parser.add_argument("--user-email", default=DEFAULT_USER_EMAIL)
    parser.add_argument("--model-name", default="")
    parser.add_argument("--candidate-models", default=",".join(DEFAULT_CANDIDATE_MODELS))
    parser.add_argument("--max-concurrency", type=int, default=20)
    parser.add_argument("--total-requests", type=int, default=30)
    parser.add_argument("--client-timeout-s", type=float, default=90.0)
    return parser.parse_args()


def ensure_fixture(
    *,
    upstream_base_url: str,
    upstream_api_key: str,
    provider_name: str,
    api_key_name: str,
    user_name: str,
    user_email: str,
    model_name: str,
) -> dict[str, Any]:
    db = SessionLocal()
    try:
        user = (
            db.query(UserAccount)
            .filter((UserAccount.username == user_name) | (UserAccount.email == user_email))
            .first()
        )
        if user is None:
            user = UserAccount(
                username=user_name,
                email=user_email,
                password_hash="functional-test-only",
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
            provider = Provider(name=provider_name, base_url=upstream_base_url, api_key=upstream_api_key)
            db.add(provider)
            db.flush()
        provider.base_url = upstream_base_url.rstrip("/")
        provider.api_key = upstream_api_key
        provider.provider_type = "openai_compatible"
        provider.enabled = True
        provider.priority = 1
        provider.timeout_ms = 60000
        provider.max_retries = 1
        provider.max_active_requests = 30
        provider.max_active_streams = 30
        provider.max_qps = None
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

        raw_api_key = ApiKeyService.generate_api_key()
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
        api_key.allowed_model_names_json = dumps_json([model_name])
        api_key.allowed_endpoint_paths_json = dumps_json(["/v1/chat/completions", "/v1/responses", "/v1/models"])
        api_key.allowed_source_ips_json = "[]"
        api_key.preferred_provider_ids_json = dumps_json([provider.id])
        api_key.preferred_region_tags_json = "[]"
        api_key.max_candidate_count = 1
        api_key.latency_bias = 1
        api_key.success_rate_bias = 1
        api_key.cost_bias = 0
        api_key.qps_limit = None
        api_key.rpm_limit = None
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
        delete_functional_rows_in_batches(
            db,
            ApiClientBillingRecord,
            ApiClientBillingRecord.api_client_key_id == api_key.id,
        )
        delete_functional_rows_in_batches(
            db,
            UserAccountBillingRecord,
            UserAccountBillingRecord.api_client_key_id == api_key.id,
        )
        delete_functional_rows_in_batches(db, RequestLog, RequestLog.api_client_key_id == api_key.id)
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


def print_call_result(result: CallResult) -> None:
    print(
        "call label={label} ok={ok} status={status} latency_ms={latency:.0f} events={events} error={error}".format(
            label=result.label,
            ok=result.ok,
            status=result.status_code,
            latency=result.latency_ms,
            events=result.stream_events,
            error=result.error or "",
        ),
        flush=True,
    )


async def _call_stream(client: httpx.AsyncClient, url: str, headers: dict[str, str], payload: dict[str, Any], label: str) -> CallResult:
    started = time.perf_counter()
    events = 0
    bytes_read = 0
    try:
        async with client.stream("POST", url, headers=headers, json=payload) as response:
            async for line in response.aiter_lines():
                if line:
                    bytes_read += len(line.encode("utf-8"))
                    if bytes_read > MAX_FUNCTIONAL_STREAM_BYTES:
                        latency_ms = (time.perf_counter() - started) * 1000
                        return CallResult(
                            label=label,
                            ok=False,
                            status_code=response.status_code,
                            latency_ms=latency_ms,
                            stream_events=events,
                            error="stream_response_too_large",
                        )
                if line.startswith("data:"):
                    data = line[5:].strip()
                    if data and data != "[DONE]":
                        events += 1
                        if events > MAX_FUNCTIONAL_STREAM_EVENTS:
                            latency_ms = (time.perf_counter() - started) * 1000
                            return CallResult(
                                label=label,
                                ok=False,
                                status_code=response.status_code,
                                latency_ms=latency_ms,
                                stream_events=events,
                                error="stream_events_too_many",
                            )
            latency_ms = (time.perf_counter() - started) * 1000
            return CallResult(
                label=label,
                ok=response.status_code < 400,
                status_code=response.status_code,
                latency_ms=latency_ms,
                stream_events=events,
                error=None if response.status_code < 400 else f"http_{response.status_code}",
            )
    except Exception as exc:
        return CallResult(label=label, ok=False, status_code=None, latency_ms=(time.perf_counter() - started) * 1000, stream_events=events, error=str(exc))


async def _call_json(client: httpx.AsyncClient, url: str, headers: dict[str, str], payload: dict[str, Any], label: str) -> CallResult:
    started = time.perf_counter()
    try:
        response = await client.post(url, headers=headers, json=payload)
        latency_ms = (time.perf_counter() - started) * 1000
        return CallResult(
            label=label,
            ok=response.status_code < 400,
            status_code=response.status_code,
            latency_ms=latency_ms,
            stream_events=0,
            error=None if response.status_code < 400 else f"http_{response.status_code}",
        )
    except Exception as exc:
        return CallResult(label=label, ok=False, status_code=None, latency_ms=(time.perf_counter() - started) * 1000, stream_events=0, error=str(exc))


async def _call_get(client: httpx.AsyncClient, url: str, headers: dict[str, str], label: str) -> CallResult:
    started = time.perf_counter()
    try:
        response = await client.get(url, headers=headers)
        latency_ms = (time.perf_counter() - started) * 1000
        return CallResult(
            label=label,
            ok=response.status_code < 400,
            status_code=response.status_code,
            latency_ms=latency_ms,
            stream_events=0,
            error=None if response.status_code < 400 else f"http_{response.status_code}",
        )
    except Exception as exc:
        return CallResult(label=label, ok=False, status_code=None, latency_ms=(time.perf_counter() - started) * 1000, error=str(exc))


async def _with_wall_timeout(label: str, awaitable: Any, timeout_s: float) -> CallResult:
    started = time.perf_counter()
    try:
        return await asyncio.wait_for(awaitable, timeout=timeout_s)
    except asyncio.TimeoutError:
        return CallResult(
            label=label,
            ok=False,
            status_code=None,
            latency_ms=(time.perf_counter() - started) * 1000,
            error=f"client_wall_timeout_{timeout_s:.0f}s",
        )


async def run_suite(*, proxy_base_url: str, raw_api_key: str, model_name: str, max_concurrency: int, total_requests: int, client_timeout_s: float) -> list[CallResult]:
    chat_url = proxy_base_url.rstrip("/") + "/v1/chat/completions"
    responses_url = proxy_base_url.rstrip("/") + "/v1/responses"
    models_url = proxy_base_url.rstrip("/") + "/v1/models"
    headers = {"Authorization": f"Bearer {raw_api_key}"}
    timeout = httpx.Timeout(client_timeout_s)
    limits = httpx.Limits(max_connections=max_concurrency, max_keepalive_connections=max_concurrency)
    async with httpx.AsyncClient(timeout=timeout, limits=limits) as client:
        results: list[CallResult] = []
        for label, coro in (
            ("models", _call_get(client, models_url, headers, "models")),
            ("chat-json", _call_json(client, chat_url, headers, {"model": model_name, "messages": [{"role": "user", "content": "hello from proxy"}], "max_tokens": 64, "temperature": 0}, "chat-json")),
            ("responses-json", _call_json(client, responses_url, headers, {"model": model_name, "input": "hello from proxy", "max_output_tokens": 64, "temperature": 0}, "responses-json")),
            ("chat-stream", _call_stream(client, chat_url, headers, {"model": model_name, "messages": [{"role": "user", "content": "stream hello from proxy"}], "stream": True, "max_tokens": 64, "temperature": 0}, "chat-stream")),
            ("responses-stream", _call_stream(client, responses_url, headers, {"model": model_name, "input": "stream hello from proxy", "stream": True, "max_output_tokens": 64, "temperature": 0}, "responses-stream")),
        ):
            result = await _with_wall_timeout(label, coro, client_timeout_s)
            print_call_result(result)
            results.append(result)

        payloads: list[tuple[str, dict[str, Any], bool, str]] = []
        for index in range(total_requests):
            if index % 4 == 0:
                payloads.append((chat_url, {"model": model_name, "messages": [{"role": "user", "content": f"batch chat {index}"}], "max_tokens": 32, "temperature": 0}, False, f"batch-chat-json-{index}"))
            elif index % 4 == 1:
                payloads.append((responses_url, {"model": model_name, "input": f"batch responses {index}", "max_output_tokens": 32, "temperature": 0}, False, f"batch-responses-json-{index}"))
            elif index % 4 == 2:
                payloads.append((chat_url, {"model": model_name, "messages": [{"role": "user", "content": f"batch stream chat {index}"}], "stream": True, "max_tokens": 32, "temperature": 0}, True, f"batch-chat-stream-{index}"))
            else:
                payloads.append((responses_url, {"model": model_name, "input": f"batch stream responses {index}", "stream": True, "max_output_tokens": 32, "temperature": 0}, True, f"batch-responses-stream-{index}"))

        semaphore = asyncio.Semaphore(max_concurrency)

        async def run_item(item: tuple[str, dict[str, Any], bool, str]) -> CallResult:
            url, payload, stream, label = item
            async with semaphore:
                if stream:
                    return await _with_wall_timeout(label, _call_stream(client, url, headers, payload, label), client_timeout_s)
                return await _with_wall_timeout(label, _call_json(client, url, headers, payload, label), client_timeout_s)

        tasks = [asyncio.create_task(run_item(item)) for item in payloads]
        for task in asyncio.as_completed(tasks):
            result = await task
            print_call_result(result)
            results.append(result)
        return results


def wait_for_logs(*, api_key_id: int, expected_count: int, timeout_s: int = 90) -> list[RequestLog]:
    deadline = time.monotonic() + timeout_s
    db = SessionLocal()
    summary_options = (
        load_only(
            RequestLog.id,
            RequestLog.api_client_key_id,
            RequestLog.request_path,
            RequestLog.is_stream,
            RequestLog.success,
            RequestLog.status_code,
            RequestLog.trace_id,
            RequestLog.latency_ms,
            RequestLog.ttfb_ms,
            RequestLog.duration_ms,
            RequestLog.prompt_tokens,
            RequestLog.completion_tokens,
            RequestLog.total_tokens,
            RequestLog.billing_status,
            RequestLog.created_at,
        ),
    )
    try:
        while time.monotonic() < deadline:
            logs = list(
                db.scalars(
                    select(RequestLog)
                    .options(*summary_options)
                    .where(RequestLog.api_client_key_id == api_key_id)
                    .order_by(RequestLog.created_at.desc(), RequestLog.id.desc())
                    .limit(expected_count + 10)
                )
            )
            if len(logs) >= expected_count:
                return logs
            time.sleep(1)
        return list(
            db.scalars(
                select(RequestLog)
                .options(*summary_options)
                .where(RequestLog.api_client_key_id == api_key_id)
                .order_by(RequestLog.created_at.desc(), RequestLog.id.desc())
                .limit(expected_count + 10)
            )
        )
    finally:
        db.close()


def print_log_summary(logs: list[RequestLog], *, limit: int = 12) -> None:
    print(f"log_count={len(logs)}")
    for item in logs[:limit]:
        print(
            "log id={id} path={path} stream={stream} success={success} status={status} trace_id={trace} latency_ms={latency} ttfb_ms={ttfb} duration_ms={duration} prompt={prompt} completion={completion} total={total} billing={billing}".format(
                id=item.id,
                path=item.request_path,
                stream=bool(item.is_stream),
                success=bool(item.success),
                status=item.status_code,
                trace=item.trace_id or "",
                latency=item.latency_ms,
                ttfb=item.ttfb_ms,
                duration=item.duration_ms,
                prompt=item.prompt_tokens,
                completion=item.completion_tokens,
                total=item.total_tokens,
                billing=item.billing_status,
            )
        )


def main() -> int:
    args = parse_args()
    if not args.upstream_api_key.strip():
        print("missing upstream api key", file=sys.stderr)
        return 2

    upstream_models = query_upstream_models(args.upstream_base_url, args.upstream_api_key)
    candidate_models = tuple(item.strip() for item in args.candidate_models.split(",") if item.strip())
    model_name = args.model_name.strip() or choose_chat_model(upstream_models, candidate_models=candidate_models)
    if not model_name:
        print("no upstream model found", file=sys.stderr)
        return 3

    fixture = ensure_fixture(
        upstream_base_url=args.upstream_base_url,
        upstream_api_key=args.upstream_api_key,
        provider_name=args.provider_name,
        api_key_name=args.api_key_name,
        user_name=args.user_name,
        user_email=args.user_email,
        model_name=model_name,
    )
    results = asyncio.run(
        run_suite(
            proxy_base_url=args.proxy_base_url,
            raw_api_key=fixture["raw_api_key"],
            model_name=model_name,
            max_concurrency=max(1, min(args.max_concurrency, 30)),
            total_requests=max(1, min(args.total_requests, 30)),
            client_timeout_s=max(1.0, args.client_timeout_s),
        )
    )
    logs = wait_for_logs(api_key_id=int(fixture["api_key_id"]), expected_count=len(results), timeout_s=90)

    print(f"provider_id={fixture['provider_id']}")
    print(f"api_key_id={fixture['api_key_id']}")
    print(f"model_name={model_name}")
    print(f"upstream_models={len(upstream_models)}")
    print(f"total_calls={len(results)}")
    print(
        "results ok={ok} fail={fail} stream_ok={stream_ok} stream_fail={stream_fail}".format(
            ok=sum(1 for item in results if item.ok and (item.status_code or 0) < 400),
            fail=sum(1 for item in results if not item.ok or (item.status_code or 0) >= 400),
            stream_ok=sum(1 for item in results if item.stream_events > 0 and item.ok),
            stream_fail=sum(1 for item in results if item.stream_events == 0 and item.error),
        )
    )
    print_log_summary(logs)
    return 0 if all(item.ok and (item.status_code or 0) < 400 for item in results) else 1


def query_upstream_models(upstream_base_url: str, upstream_api_key: str) -> list[str]:
    url = upstream_base_url.rstrip("/") + "/models"
    with httpx.Client(timeout=30.0) as client:
        response = client.get(url, headers={"Authorization": f"Bearer {upstream_api_key}"})
        response.raise_for_status()
        payload = response.json()
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        return []
    result: list[str] = []
    for item in data:
        if isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"].strip():
            result.append(item["id"].strip())
            if len(result) >= MAX_UPSTREAM_MODEL_OPTIONS:
                break
    return result


def choose_chat_model(models: list[str], *, candidate_models: tuple[str, ...] = DEFAULT_CANDIDATE_MODELS) -> str:
    by_lower = {item.lower(): item for item in models}
    for item in candidate_models:
        matched = by_lower.get(item.lower())
        if matched:
            return matched
    return candidate_models[0] if candidate_models else ""


if __name__ == "__main__":
    raise SystemExit(main())
