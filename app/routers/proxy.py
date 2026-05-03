from collections.abc import AsyncIterator
from json import JSONDecodeError
import json
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.database import SessionLocal
from app.services.api_key_service import ApiClientAuthContext, ApiClientAuthError, ApiKeyService, require_api_client_auth
from app.services.concurrency_service import (
    ConcurrencyLease,
    ConcurrencyLimitExceededError,
    ConcurrencyLimits,
    ConcurrencyService,
)
from app.services.proxy_service import ProxyService
from app.services.openai_error_service import OpenAIErrorService
from app.services.setting_service import SettingService
from app.utils.json_utils import dumps_json
from app.utils.http_headers import build_proxy_response_headers


router = APIRouter(tags=["proxy"])


async def _acquire_request_concurrency(
    *,
    request: Request,
    api_client_auth: ApiClientAuthContext,
    is_stream: bool,
) -> ConcurrencyLease:
    setting = await run_in_threadpool(_get_setting_with_scoped_session)
    try:
        return await ConcurrencyService.acquire(
            request_id=uuid4().hex,
            ttl_seconds=setting.concurrency_lease_ttl_seconds,
            is_stream=is_stream,
            api_key_id=api_client_auth.api_client_key.id,
            account_id=api_client_auth.api_client_key.owner_user_id,
            limits=ConcurrencyLimits(
                global_max_active_requests=setting.global_max_active_requests,
                global_max_active_streams=setting.global_max_active_streams,
                api_key_max_active_requests=setting.api_key_max_active_requests,
                api_key_max_active_streams=setting.api_key_max_active_streams,
                account_max_active_requests=setting.account_max_active_requests,
                account_max_active_streams=setting.account_max_active_streams,
            ),
        )
    except ConcurrencyLimitExceededError as exc:
        api_key = api_client_auth.api_client_key
        raise ApiClientAuthError(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            code=exc.code,
            message=exc.message,
            api_client_key_id=api_key.id,
            api_client_key_name=api_key.name,
            api_client_key_prefix=api_key.key_prefix,
            user_account_id=api_key.owner_user_id,
            user_account_name=api_key.owner_user.username if api_key.owner_user else None,
            remaining_tokens=api_client_auth.remaining_tokens,
            remaining_balance=api_client_auth.remaining_balance,
            remaining_requests_daily=api_client_auth.remaining_requests_daily,
            remaining_cost_daily=api_client_auth.remaining_cost_daily,
            policy_snapshot_json=api_client_auth.policy_snapshot_json,
        ) from exc
    except Exception as exc:
        api_key = api_client_auth.api_client_key
        raise ApiClientAuthError(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            code="redis_unavailable",
            message="Redis concurrency service is unavailable",
            api_client_key_id=api_key.id,
            api_client_key_name=api_key.name,
            api_client_key_prefix=api_key.key_prefix,
            user_account_id=api_key.owner_user_id,
            user_account_name=api_key.owner_user.username if api_key.owner_user else None,
            remaining_tokens=api_client_auth.remaining_tokens,
            remaining_balance=api_client_auth.remaining_balance,
            remaining_requests_daily=api_client_auth.remaining_requests_daily,
            remaining_cost_daily=api_client_auth.remaining_cost_daily,
            policy_snapshot_json=api_client_auth.policy_snapshot_json,
        ) from exc


async def _release_request_concurrency(lease: ConcurrencyLease | None) -> None:
    await ConcurrencyService.release(lease)


def _get_setting_with_scoped_session():
    db = SessionLocal()
    try:
        return SettingService.get_or_create(db)
    finally:
        db.close()


def _effective_request_body_limit(setting, endpoint_path: str) -> int:
    global_limit = int(getattr(setting, "max_v1_request_body_bytes", 0) or 0)
    endpoint_limit = 0
    if endpoint_path == "/chat/completions":
        endpoint_limit = int(getattr(setting, "max_v1_chat_request_body_bytes", 0) or 0)
    elif endpoint_path == "/responses":
        endpoint_limit = int(getattr(setting, "max_v1_responses_request_body_bytes", 0) or 0)
    positive_limits = [item for item in (global_limit, endpoint_limit) if item > 0]
    return min(positive_limits) if positive_limits else 0


async def _read_limited_v1_json_payload(request: Request, *, endpoint_path: str) -> dict:
    setting = await run_in_threadpool(_get_setting_with_scoped_session)
    limit = _effective_request_body_limit(setting, endpoint_path)
    content_length = request.headers.get("content-length")
    try:
        content_length_bytes = int(content_length) if content_length is not None else None
    except ValueError:
        content_length_bytes = None
    if limit > 0 and content_length_bytes is not None and content_length_bytes > limit:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail={
                "message": f"请求体大小 {content_length_bytes} 字节超过应用层上限 {limit} 字节",
                "code": "request_body_too_large",
                "request_body_bytes": content_length_bytes,
                "max_v1_request_body_bytes": limit,
                "endpoint_path": f"/v1{endpoint_path}",
            },
        )

    body = bytearray()
    async for chunk in request.stream():
        if not chunk:
            continue
        if limit > 0 and len(body) + len(chunk) > limit:
            raise HTTPException(
                status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                detail={
                    "message": f"请求体大小超过应用层上限 {limit} 字节",
                    "code": "request_body_too_large",
                    "max_v1_request_body_bytes": limit,
                    "endpoint_path": f"/v1{endpoint_path}",
                },
            )
        body.extend(chunk)

    if not body:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": "请求体不能为空", "code": "invalid_json_body"},
        )
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, JSONDecodeError) as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": "请求体必须是合法 JSON", "code": "invalid_json_body"},
        ) from exc
    if not isinstance(payload, dict):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": "请求体 JSON 顶层必须是对象", "code": "invalid_json_body"},
        )
    return payload


async def _release_after_stream(
    stream: AsyncIterator[bytes],
    lease: ConcurrencyLease,
    *,
    trace_id: str | None,
) -> AsyncIterator[bytes]:
    try:
        async for chunk in stream:
            yield chunk
    except Exception as exc:
        yield _format_sse_error_event(exc, trace_id=trace_id)
        yield b"data: [DONE]\n\n"
    finally:
        await _release_request_concurrency(lease)


def _format_sse_error_event(exc: Exception, *, trace_id: str | None) -> bytes:
    status_code = getattr(exc, "status_code", 500)
    detail = getattr(exc, "detail", None)
    error_type, default_code, retryable = OpenAIErrorService.classify_status_code(
        status_code if isinstance(status_code, int) else 500
    )
    message = OpenAIErrorService.extract_message(detail, fallback=str(exc) or exc.__class__.__name__)
    code = default_code
    if isinstance(detail, dict):
        if isinstance(detail.get("code"), str):
            code = detail["code"]
        elif isinstance(detail.get("error"), dict) and isinstance(detail["error"].get("code"), str):
            code = detail["error"]["code"]
    payload = OpenAIErrorService.build_error_payload(
        message=message,
        code=code,
        trace_id=trace_id,
        error_type=error_type,
        retryable=retryable,
        detail=detail if isinstance(detail, dict) else {"exception_type": exc.__class__.__name__},
    )
    return f"event: error\ndata: {dumps_json(payload)}\n\n".encode("utf-8")


@router.post("/v1/chat/completions", response_model=None)
async def chat_completions(
    request: Request,
    response: Response,
    api_client_auth: ApiClientAuthContext = Depends(require_api_client_auth),
):
    source_ip = ApiKeyService.extract_source_ip(request)
    payload = await _read_limited_v1_json_payload(request, endpoint_path="/chat/completions")
    if payload.get("stream") is True:
        lease = await _acquire_request_concurrency(request=request, api_client_auth=api_client_auth, is_stream=True)
        try:
            stream, provider, trace, latency_ms = await ProxyService.forward_stream_request(
                endpoint_path="/chat/completions",
                payload=payload,
                log_type="chat",
                route_context=api_client_auth.route_context,
                api_client_auth=api_client_auth,
                trace_id=getattr(request.state, "trace_id", None),
                source_ip=source_ip,
            )
            headers = build_proxy_response_headers(
                provider_id=provider.id,
                provider_name=provider.name,
                latency_ms=latency_ms,
                trace_length=len(trace),
                trace_id=getattr(request.state, "trace_id", None),
            )
            return StreamingResponse(
                _release_after_stream(stream, lease, trace_id=getattr(request.state, "trace_id", None)),
                media_type="text/event-stream",
                headers=headers,
            )
        except Exception:
            await _release_request_concurrency(lease)
            raise

    lease = await _acquire_request_concurrency(request=request, api_client_auth=api_client_auth, is_stream=False)
    try:
        result, provider, trace, latency_ms = await ProxyService.forward_json_request(
            endpoint_path="/chat/completions",
            payload=payload,
            log_type="chat",
            route_context=api_client_auth.route_context,
            api_client_auth=api_client_auth,
            trace_id=getattr(request.state, "trace_id", None),
            source_ip=source_ip,
        )
    finally:
        await _release_request_concurrency(lease)
    for key, value in build_proxy_response_headers(
        provider_id=provider.id,
        provider_name=provider.name,
        latency_ms=latency_ms,
        trace_length=len(trace),
        trace_id=getattr(request.state, "trace_id", None),
    ).items():
        response.headers[key] = value
    return result


@router.post("/v1/responses", response_model=None)
async def responses(
    request: Request,
    response: Response,
    api_client_auth: ApiClientAuthContext = Depends(require_api_client_auth),
):
    source_ip = ApiKeyService.extract_source_ip(request)
    payload = await _read_limited_v1_json_payload(request, endpoint_path="/responses")
    if payload.get("stream") is True:
        lease = await _acquire_request_concurrency(request=request, api_client_auth=api_client_auth, is_stream=True)
        try:
            stream, provider, trace, latency_ms = await ProxyService.forward_stream_request(
                endpoint_path="/responses",
                payload=payload,
                log_type="responses",
                route_context=api_client_auth.route_context,
                api_client_auth=api_client_auth,
                trace_id=getattr(request.state, "trace_id", None),
                source_ip=source_ip,
            )
            headers = build_proxy_response_headers(
                provider_id=provider.id,
                provider_name=provider.name,
                latency_ms=latency_ms,
                trace_length=len(trace),
                trace_id=getattr(request.state, "trace_id", None),
            )
            return StreamingResponse(
                _release_after_stream(stream, lease, trace_id=getattr(request.state, "trace_id", None)),
                media_type="text/event-stream",
                headers=headers,
            )
        except Exception:
            await _release_request_concurrency(lease)
            raise

    lease = await _acquire_request_concurrency(request=request, api_client_auth=api_client_auth, is_stream=False)
    try:
        result, provider, trace, latency_ms = await ProxyService.forward_json_request(
            endpoint_path="/responses",
            payload=payload,
            log_type="responses",
            route_context=api_client_auth.route_context,
            api_client_auth=api_client_auth,
            trace_id=getattr(request.state, "trace_id", None),
            source_ip=source_ip,
        )
    finally:
        await _release_request_concurrency(lease)
    for key, value in build_proxy_response_headers(
        provider_id=provider.id,
        provider_name=provider.name,
        latency_ms=latency_ms,
        trace_length=len(trace),
        trace_id=getattr(request.state, "trace_id", None),
    ).items():
        response.headers[key] = value
    return result


@router.get("/v1/responses/{response_id}", response_model=None)
async def retrieve_response(
    response_id: str,
    request: Request,
    response: Response,
    api_client_auth: ApiClientAuthContext = Depends(require_api_client_auth),
):
    result, provider, trace, latency_ms = await ProxyService.retrieve_response(
        response_id=response_id,
        query_items=list(request.query_params.multi_items()),
        route_context=api_client_auth.route_context,
    )
    for key, value in build_proxy_response_headers(
        provider_id=provider.id,
        provider_name=provider.name,
        latency_ms=latency_ms,
        trace_length=len(trace),
        trace_id=getattr(request.state, "trace_id", None),
    ).items():
        response.headers[key] = value
    return result


@router.post("/v1/responses/{response_id}/cancel", response_model=None)
async def cancel_response(
    response_id: str,
    request: Request,
    response: Response,
    api_client_auth: ApiClientAuthContext = Depends(require_api_client_auth),
):
    result, provider, trace, latency_ms = await ProxyService.cancel_response(
        response_id=response_id,
        route_context=api_client_auth.route_context,
    )
    for key, value in build_proxy_response_headers(
        provider_id=provider.id,
        provider_name=provider.name,
        latency_ms=latency_ms,
        trace_length=len(trace),
        trace_id=getattr(request.state, "trace_id", None),
    ).items():
        response.headers[key] = value
    return result


@router.get("/v1/models")
async def list_models(
    request: Request,
    api_client_auth: ApiClientAuthContext = Depends(require_api_client_auth),
) -> dict:
    lease = await _acquire_request_concurrency(request=request, api_client_auth=api_client_auth, is_stream=False)
    try:
        return await ProxyService.async_list_models(route_context=api_client_auth.route_context, api_client_auth=api_client_auth)
    finally:
        await _release_request_concurrency(lease)
