from collections.abc import AsyncIterator
from json import JSONDecodeError
import json
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.database import SessionLocal
from app.services.api_key_service import ApiClientAuthContext, ApiClientAuthError, ApiKeyService, require_api_client_auth
from app.services.concurrency_service import (
    ConcurrencyLease,
    ConcurrencyLimitExceededError,
    ConcurrencyLimits,
    ConcurrencyService,
)
from app.services.log_service import LogService
from app.services.proxy_service import ProxyService
from app.services.openai_error_service import OpenAIErrorService
from app.services.setting_service import SettingService
from app.utils.json_utils import dumps_json
from app.utils.http_headers import build_proxy_response_headers
from app.utils.request_body_structure import summarize_request_body_structure


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
    max_logged_body_bytes = int(getattr(setting, "max_logged_body_bytes", 16384) or 16384)
    if limit > 0 and content_length_bytes is not None and content_length_bytes > limit:
        request.state.v1_request_body_structure_json = _truncate_json_for_log(
            {
                "_summary": "request body structure omitted because Content-Length exceeds application limit",
                "structure": {
                    "type": "bytes",
                    "content_length": content_length_bytes,
                    "max_v1_request_body_bytes": limit,
                },
            },
            max_logged_body_bytes,
        )
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
            request.state.v1_request_body_structure_json = _truncate_json_for_log(
                {
                    "_summary": "request body structure omitted because streamed body exceeds application limit",
                    "structure": {
                        "type": "bytes",
                        "bytes_read": len(body),
                        "max_v1_request_body_bytes": limit,
                    },
                },
                max_logged_body_bytes,
            )
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
        request.state.v1_request_body_structure_json = _truncate_json_for_log(
            {
                "_summary": "request body structure only; request body is empty",
                "structure": {"type": "empty"},
            },
            max_logged_body_bytes,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": "请求体不能为空", "code": "invalid_json_body"},
        )
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, JSONDecodeError) as exc:
        request.state.v1_request_body_structure_json = _truncate_json_for_log(
            {
                "_summary": "request body structure only; body is not valid JSON",
                "structure": {
                    "type": "bytes",
                    "bytes": len(body),
                    "decode_or_json_error": exc.__class__.__name__,
                },
            },
            max_logged_body_bytes,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": "请求体必须是合法 JSON", "code": "invalid_json_body"},
        ) from exc
    if not isinstance(payload, dict):
        request.state.v1_request_body_structure_json = _truncate_json_for_log(
            summarize_request_body_structure(payload),
            max_logged_body_bytes,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": "请求体 JSON 顶层必须是对象", "code": "invalid_json_body"},
        )
    request.state.v1_request_body_structure_json = _truncate_json_for_log(
        summarize_request_body_structure(payload),
        max_logged_body_bytes,
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
    lease = await _acquire_request_concurrency(request=request, api_client_auth=api_client_auth, is_stream=False)
    try:
        result, provider, trace, latency_ms = await ProxyService.retrieve_response(
            response_id=response_id,
            query_items=list(request.query_params.multi_items()),
            route_context=api_client_auth.route_context,
            api_client_auth=api_client_auth,
            trace_id=getattr(request.state, "trace_id", None),
            source_ip=ApiKeyService.extract_source_ip(request),
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


@router.post("/v1/responses/{response_id}/cancel", response_model=None)
async def cancel_response(
    response_id: str,
    request: Request,
    response: Response,
    api_client_auth: ApiClientAuthContext = Depends(require_api_client_auth),
):
    lease = await _acquire_request_concurrency(request=request, api_client_auth=api_client_auth, is_stream=False)
    try:
        result, provider, trace, latency_ms = await ProxyService.cancel_response(
            response_id=response_id,
            route_context=api_client_auth.route_context,
            api_client_auth=api_client_auth,
            trace_id=getattr(request.state, "trace_id", None),
            source_ip=ApiKeyService.extract_source_ip(request),
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


@router.get("/v1/models")
async def list_models(
    request: Request,
    api_client_auth: ApiClientAuthContext = Depends(require_api_client_auth),
) -> dict:
    lease = await _acquire_request_concurrency(request=request, api_client_auth=api_client_auth, is_stream=False)
    try:
        result = await ProxyService.async_list_models(route_context=api_client_auth.route_context, api_client_auth=api_client_auth)
        await run_in_threadpool(
            _log_v1_models_request,
            trace_id=getattr(request.state, "trace_id", None),
            source_ip=ApiKeyService.extract_source_ip(request),
            api_client_auth=api_client_auth,
            model_count=len(result.get("data", [])) if isinstance(result.get("data"), list) else None,
        )
        return result
    finally:
        await _release_request_concurrency(lease)


@router.options("/v1", response_model=None)
@router.options("/v1/{path:path}", response_model=None)
async def v1_options(request: Request) -> Response:
    await run_in_threadpool(
        _log_v1_preflight,
        request_path=request.url.path,
        trace_id=getattr(request.state, "trace_id", None),
        source_ip=ApiKeyService.extract_source_ip(request),
    )
    return Response(status_code=status.HTTP_204_NO_CONTENT, headers=_build_v1_cors_headers(request))


@router.api_route("/v1", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"], response_model=None)
@router.api_route("/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"], response_model=None)
async def unsupported_v1_endpoint(
    request: Request,
    path: str = "",
    api_client_auth: ApiClientAuthContext = Depends(require_api_client_auth),
) -> JSONResponse:
    request_path = request.url.path
    trace_id = getattr(request.state, "trace_id", None)
    supported_endpoints = [
        "GET /v1/models",
        "POST /v1/chat/completions",
        "POST /v1/responses",
        "GET /v1/responses/{response_id}",
        "POST /v1/responses/{response_id}/cancel",
    ]
    message = (
        f"Unsupported OpenAI-compatible endpoint: {request.method.upper()} {request_path}. "
        f"Supported endpoints: {', '.join(supported_endpoints)}."
    )
    detail = {
        "request_path": request_path,
        "method": request.method.upper(),
        "supported_endpoints": supported_endpoints,
        "hint": "Use the base URL ending in /v1 only when the client appends a supported endpoint path.",
    }
    await run_in_threadpool(
        _log_unsupported_v1_endpoint,
        request_path=request_path,
        http_method=request.method.upper(),
        trace_id=trace_id,
        source_ip=ApiKeyService.extract_source_ip(request),
        api_client_auth=api_client_auth,
        message=message,
        detail=detail,
        request_body_json=await _read_request_body_structure_for_log(request),
    )
    payload = OpenAIErrorService.build_error_payload(
        message=message,
        code="unsupported_endpoint",
        trace_id=trace_id,
        error_type="invalid_request_error",
        retryable=False,
        detail=detail,
    )
    return JSONResponse(
        status_code=status.HTTP_404_NOT_FOUND,
        content=payload,
        headers=_build_v1_cors_headers(request),
    )


async def _read_request_body_structure_for_log(request: Request) -> str | None:
    if request.method.upper() not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    setting = await run_in_threadpool(_get_setting_with_scoped_session)
    max_logged_body_bytes = int(getattr(setting, "max_logged_body_bytes", 16384) or 16384)
    max_read_bytes = max(1, min(max_logged_body_bytes * 4, 262144))
    body = bytearray()
    truncated = False
    async for chunk in request.stream():
        if not chunk:
            continue
        if len(body) + len(chunk) > max_read_bytes:
            remaining = max_read_bytes - len(body)
            if remaining > 0:
                body.extend(chunk[:remaining])
            truncated = True
            break
        body.extend(chunk)
    if not body:
        return None
    try:
        parsed = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, JSONDecodeError):
        return _truncate_json_for_log(
            {
                "_summary": "request body structure only; body is not valid JSON or was truncated before parsing",
                "structure": {
                    "type": "bytes",
                    "bytes_read": len(body),
                    "truncated": truncated,
                },
            },
            max_logged_body_bytes,
        )
    summary = summarize_request_body_structure(parsed)
    if truncated:
        summary["structure"]["truncated_body_before_parse"] = True
    return _truncate_json_for_log(summary, max_logged_body_bytes)


def _build_v1_cors_headers(request: Request) -> dict[str, str]:
    origin = request.headers.get("origin")
    return {
        "Access-Control-Allow-Origin": origin or "*",
        "Access-Control-Allow-Methods": "GET,POST,OPTIONS",
        "Access-Control-Allow-Headers": (
            request.headers.get("access-control-request-headers")
            or "authorization,content-type,x-request-id,x-trace-id"
        ),
        "Access-Control-Expose-Headers": (
            "x-request-id,x-trace-id,x-proxy-provider-id,x-proxy-provider-name,x-proxy-latency-ms"
        ),
        "Vary": "Origin",
    }


def _truncate_json_for_log(value, limit_bytes: int) -> str:
    serialized = dumps_json(value)
    encoded = serialized.encode("utf-8", errors="ignore")
    if len(encoded) <= limit_bytes:
        return serialized
    clipped = encoded[:limit_bytes].decode("utf-8", errors="ignore")
    return f"{clipped}...[truncated]"


def _log_unsupported_v1_endpoint(
    *,
    request_path: str,
    http_method: str,
    trace_id: str | None,
    source_ip: str | None,
    api_client_auth: ApiClientAuthContext,
    message: str,
    detail: dict,
    request_body_json: str | None,
) -> None:
    api_key = api_client_auth.api_client_key
    db = SessionLocal()
    try:
        LogService.create_log(
            db,
            log_type="api_client_auth",
            trace_id=trace_id,
            request_path=request_path,
            source_ip=source_ip,
            http_method=http_method,
            success=False,
            status_code=status.HTTP_404_NOT_FOUND,
            message=message,
            error_type="invalid_request_error",
            error_code="unsupported_endpoint",
            retryable=False,
            api_client_key_id=api_key.id,
            api_client_key_name=api_key.name,
            api_client_key_prefix=api_key.key_prefix,
            user_account_id=api_key.owner_user_id,
            user_account_name=api_key.owner_user.username if api_key.owner_user else None,
            api_client_auth_result="authenticated",
            api_client_remaining_tokens=api_client_auth.remaining_tokens,
            api_client_remaining_requests_daily=api_client_auth.remaining_requests_daily,
            api_client_remaining_cost_daily=api_client_auth.remaining_cost_daily,
            api_client_policy_snapshot_json=api_client_auth.policy_snapshot_json,
            request_body_json=request_body_json,
            response_body_json=dumps_json({"error": detail}),
            trace=[{"result": "unsupported_endpoint", "error": "unsupported_endpoint", "latency_ms": 0}],
            attempt_count=1,
            schedule_token_fill=False,
        )
    finally:
        db.close()


def _log_v1_preflight(
    *,
    request_path: str,
    trace_id: str | None,
    source_ip: str | None,
) -> None:
    db = SessionLocal()
    try:
        LogService.create_log(
            db,
            log_type="api_client_auth",
            trace_id=trace_id,
            request_path=request_path,
            source_ip=source_ip,
            http_method="OPTIONS",
            success=True,
            status_code=status.HTTP_204_NO_CONTENT,
            message="CORS preflight accepted",
            api_client_auth_result="preflight",
            trace=[{"result": "preflight", "latency_ms": 0}],
            attempt_count=1,
            schedule_token_fill=False,
        )
    finally:
        db.close()


def _log_v1_models_request(
    *,
    trace_id: str | None,
    source_ip: str | None,
    api_client_auth: ApiClientAuthContext,
    model_count: int | None,
) -> None:
    api_key = api_client_auth.api_client_key
    db = SessionLocal()
    try:
        LogService.create_log(
            db,
            log_type="models",
            trace_id=trace_id,
            request_path="/v1/models",
            source_ip=source_ip,
            http_method="GET",
            success=True,
            status_code=status.HTTP_200_OK,
            message="models list success",
            retryable=False,
            api_client_key_id=api_key.id,
            api_client_key_name=api_key.name,
            api_client_key_prefix=api_key.key_prefix,
            user_account_id=api_key.owner_user_id,
            user_account_name=api_key.owner_user.username if api_key.owner_user else None,
            api_client_auth_result="authenticated",
            api_client_remaining_tokens=api_client_auth.remaining_tokens,
            api_client_remaining_requests_daily=api_client_auth.remaining_requests_daily,
            api_client_remaining_cost_daily=api_client_auth.remaining_cost_daily,
            api_client_policy_snapshot_json=api_client_auth.policy_snapshot_json,
            response_body_json=dumps_json({"object": "list", "model_count": model_count}),
            trace=[{"result": "models_list_success", "model_count": model_count, "latency_ms": 0}],
            attempt_count=1,
            schedule_token_fill=False,
        )
    finally:
        db.close()
