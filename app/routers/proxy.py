import base64
from collections.abc import AsyncIterator
from json import JSONDecodeError
import json
import time
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from app.database import SessionLocal
from app.services.asset_service import AssetService
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
    elif endpoint_path in {"/responses", "/images/generations", "/images/edits", "/images/variations"}:
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


async def _prepare_v1_body_limit_context(request: Request, *, endpoint_path: str) -> tuple[object, int, int]:
    setting = await run_in_threadpool(_get_setting_with_scoped_session)
    limit = _effective_request_body_limit(setting, endpoint_path)
    max_logged_body_bytes = int(getattr(setting, "max_logged_body_bytes", 16384) or 16384)
    content_length = request.headers.get("content-length")
    try:
        content_length_bytes = int(content_length) if content_length is not None else None
    except ValueError:
        content_length_bytes = None
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
    return setting, limit, max_logged_body_bytes


async def _upload_to_data_url(upload_file, *, field_name: str) -> dict[str, object]:
    content_type = (getattr(upload_file, "content_type", None) or "").strip().lower()
    if content_type not in AssetService.IMAGE_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": f"{field_name} 仅支持 PNG/JPEG/WEBP/GIF 图片", "code": "invalid_image_file"},
        )
    content = await upload_file.read(AssetService.MAX_IMAGE_BYTES + 1)
    if not content:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": f"{field_name} 上传文件不能为空", "code": "empty_image_file"},
        )
    if len(content) > AssetService.MAX_IMAGE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": f"{field_name} 图片大小不能超过 10 MB", "code": "image_file_too_large"},
        )
    encoded = base64.b64encode(content).decode("ascii")
    return {
        "url": f"data:{content_type};base64,{encoded}",
        "filename": getattr(upload_file, "filename", None) or f"{field_name}.png",
        "content_type": content_type,
        "file_size_bytes": len(content),
    }


def _normalize_legacy_image_values(value, *, field_name: str) -> list[dict[str, object]]:
    normalized: list[dict[str, object]] = []
    candidates = value if isinstance(value, list) else [value]
    for item in candidates:
        if item in (None, ""):
            continue
        if isinstance(item, str):
            current = item.strip()
            if current:
                normalized.append({"url": current, "source": "text"})
            continue
        image_candidates = ProxyService._normalize_generated_image_candidate(item)
        for candidate in image_candidates:
            url = candidate.get("url")
            if isinstance(url, str) and url.strip():
                normalized.append({"url": url.strip(), "source": "json"})
    if not normalized:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": f"{field_name} 不能为空", "code": f"missing_{field_name}_input"},
        )
    return normalized


async def _read_legacy_image_edit_form_payload(request: Request) -> tuple[dict, list[str], list[str], str]:
    _setting, _limit, max_logged_body_bytes = await _prepare_v1_body_limit_context(request, endpoint_path="/images/edits")
    form = await request.form()
    image_items = list(form.getlist("image")) or list(form.getlist("image[]"))
    mask_items = list(form.getlist("mask")) or list(form.getlist("mask[]"))
    image_entries: list[dict[str, object]] = []
    mask_entries: list[dict[str, object]] = []
    for item in image_items:
        if isinstance(item, str):
            image_entries.extend(_normalize_legacy_image_values(item, field_name="image"))
        else:
            upload_entry = await _upload_to_data_url(item, field_name="image")
            upload_entry["source"] = "upload"
            image_entries.append(upload_entry)
    for item in mask_items:
        if isinstance(item, str):
            mask_entries.extend(_normalize_legacy_image_values(item, field_name="mask"))
        else:
            upload_entry = await _upload_to_data_url(item, field_name="mask")
            upload_entry["source"] = "upload"
            mask_entries.append(upload_entry)
    payload = {
        "prompt": form.get("prompt"),
        "model": form.get("model"),
        "n": form.get("n"),
        "size": form.get("size"),
        "quality": form.get("quality"),
        "background": form.get("background"),
        "output_format": form.get("output_format"),
        "output_compression": form.get("output_compression"),
        "moderation": form.get("moderation"),
        "response_format": form.get("response_format"),
        "user": form.get("user"),
    }
    summary_payload = {
        **{key: value for key, value in payload.items() if value not in (None, "")},
        "image": [
            {
                "type": "input_image",
                "source": entry.get("source"),
                "filename": entry.get("filename"),
                "content_type": entry.get("content_type"),
                "file_size_bytes": entry.get("file_size_bytes"),
            }
            for entry in image_entries
        ],
        "mask": [
            {
                "type": "input_image",
                "source": entry.get("source"),
                "filename": entry.get("filename"),
                "content_type": entry.get("content_type"),
                "file_size_bytes": entry.get("file_size_bytes"),
            }
            for entry in mask_entries
        ],
    }
    request.state.v1_request_body_structure_json = _truncate_json_for_log(
        summarize_request_body_structure(summary_payload),
        max_logged_body_bytes,
    )
    return (
        payload,
        [str(entry["url"]) for entry in image_entries if isinstance(entry.get("url"), str)],
        [str(entry["url"]) for entry in mask_entries if isinstance(entry.get("url"), str)],
        "multipart",
    )


def _merge_legacy_image_api_results(results: list[dict], *, response_format: str) -> dict:
    merged = {"created": int(time.time()), "data": []}
    for item in results:
        data_items = item.get("data")
        if isinstance(data_items, list):
            merged["data"].extend(data_items)
    if not merged["data"]:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "message": f"legacy Images 兼容层未返回可解析的 {response_format} 图片结果",
                "code": "legacy_image_result_missing",
            },
        )
    return merged


async def _forward_legacy_image_batch(
    *,
    request: Request,
    api_client_auth: ApiClientAuthContext,
    source_ip: str | None,
    responses_payload: dict,
    requested_count: int,
    request_path_for_log: str,
    public_endpoint_path: str,
    response_format: str,
) -> tuple[dict, object, list[dict], int]:
    merged_results: list[dict] = []
    provider = None
    trace: list[dict] = []
    latency_ms_total = 0
    for index in range(requested_count):
        result, current_provider, current_trace, current_latency_ms = await ProxyService.forward_json_request(
            endpoint_path="/responses",
            payload=responses_payload,
            log_type="responses",
            route_context=api_client_auth.route_context,
            api_client_auth=api_client_auth,
            trace_id=getattr(request.state, "trace_id", None),
            source_ip=source_ip,
            request_path_for_log=request_path_for_log,
            public_endpoint_path=public_endpoint_path,
            response_transform=lambda upstream: ProxyService.adapt_responses_to_legacy_image_response(
                upstream,
                response_format=response_format,
            ),
        )
        merged_results.append(result)
        provider = current_provider
        latency_ms_total += current_latency_ms
        if requested_count == 1:
            trace = current_trace
        else:
            trace.extend(
                {
                    **item,
                    "legacy_batch_index": index,
                }
                for item in current_trace
            )
    return _merge_legacy_image_api_results(merged_results, response_format=response_format), provider, trace, latency_ms_total


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


@router.post("/v1/images/generations", response_model=None)
async def image_generations(
    request: Request,
    response: Response,
    api_client_auth: ApiClientAuthContext = Depends(require_api_client_auth),
):
    source_ip = ApiKeyService.extract_source_ip(request)
    legacy_payload = await _read_limited_v1_json_payload(request, endpoint_path="/images/generations")
    requested_count = ProxyService.parse_legacy_image_count(legacy_payload.get("n"))
    responses_payload, response_format = ProxyService.build_legacy_image_generation_responses_payload(
        legacy_payload,
        api_client_auth=api_client_auth,
        route_context=api_client_auth.route_context,
    )
    lease = await _acquire_request_concurrency(request=request, api_client_auth=api_client_auth, is_stream=False)
    try:
        result, provider, trace, latency_ms = await _forward_legacy_image_batch(
            request=request,
            api_client_auth=api_client_auth,
            source_ip=source_ip,
            responses_payload=responses_payload,
            requested_count=requested_count,
            request_path_for_log="/v1/images/generations",
            public_endpoint_path="/images/generations",
            response_format=response_format,
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


@router.post("/v1/images/edits", response_model=None)
async def image_edits(
    request: Request,
    response: Response,
    api_client_auth: ApiClientAuthContext = Depends(require_api_client_auth),
):
    source_ip = ApiKeyService.extract_source_ip(request)
    content_type = (request.headers.get("content-type") or "").strip().lower()
    if "application/json" in content_type:
        legacy_payload = await _read_limited_v1_json_payload(request, endpoint_path="/images/edits")
        image_source_value = legacy_payload.get("images") if legacy_payload.get("images") not in (None, "") else legacy_payload.get("image")
        image_entries = _normalize_legacy_image_values(image_source_value, field_name="image")
        mask_entries = _normalize_legacy_image_values(legacy_payload.get("mask"), field_name="mask") if legacy_payload.get("mask") not in (None, "") else []
        image_urls = [str(entry["url"]) for entry in image_entries if isinstance(entry.get("url"), str)]
        mask_urls = [str(entry["url"]) for entry in mask_entries if isinstance(entry.get("url"), str)]
    else:
        legacy_payload, image_urls, mask_urls, _payload_mode = await _read_legacy_image_edit_form_payload(request)
    requested_count = ProxyService.parse_legacy_image_count(legacy_payload.get("n"))
    responses_payload, response_format = ProxyService.build_legacy_image_edit_responses_payload(
        legacy_payload,
        image_urls=image_urls,
        mask_urls=mask_urls,
        api_client_auth=api_client_auth,
        route_context=api_client_auth.route_context,
    )
    lease = await _acquire_request_concurrency(request=request, api_client_auth=api_client_auth, is_stream=False)
    try:
        result, provider, trace, latency_ms = await _forward_legacy_image_batch(
            request=request,
            api_client_auth=api_client_auth,
            source_ip=source_ip,
            responses_payload=responses_payload,
            requested_count=requested_count,
            request_path_for_log="/v1/images/edits",
            public_endpoint_path="/images/edits",
            response_format=response_format,
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


@router.post("/v1/images/variations", response_model=None)
async def image_variations(
    request: Request,
    response: Response,
    api_client_auth: ApiClientAuthContext = Depends(require_api_client_auth),
):
    source_ip = ApiKeyService.extract_source_ip(request)
    content_type = (request.headers.get("content-type") or "").strip().lower()
    if "application/json" in content_type:
        legacy_payload = await _read_limited_v1_json_payload(request, endpoint_path="/images/variations")
        image_source_value = legacy_payload.get("images") if legacy_payload.get("images") not in (None, "") else legacy_payload.get("image")
        image_entries = _normalize_legacy_image_values(image_source_value, field_name="image")
        image_urls = [str(entry["url"]) for entry in image_entries if isinstance(entry.get("url"), str)]
    else:
        legacy_payload, image_urls, _mask_urls, _payload_mode = await _read_legacy_image_edit_form_payload(request)
    requested_count = ProxyService.parse_legacy_image_count(legacy_payload.get("n"))
    responses_payload, response_format = ProxyService.build_legacy_image_variation_responses_payload(
        legacy_payload,
        image_urls=image_urls,
        api_client_auth=api_client_auth,
        route_context=api_client_auth.route_context,
    )
    lease = await _acquire_request_concurrency(request=request, api_client_auth=api_client_auth, is_stream=False)
    try:
        result, provider, trace, latency_ms = await _forward_legacy_image_batch(
            request=request,
            api_client_auth=api_client_auth,
            source_ip=source_ip,
            responses_payload=responses_payload,
            requested_count=requested_count,
            request_path_for_log="/v1/images/variations",
            public_endpoint_path="/images/variations",
            response_format=response_format,
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
        "POST /v1/images/generations",
        "POST /v1/images/edits",
        "POST /v1/images/variations",
        "GET /v1/responses/{response_id}",
        "POST /v1/responses/{response_id}/cancel",
    ]
    legacy_images_hint = None
    if request_path.startswith("/v1/images/"):
        legacy_images_hint = {
            "message": "本平台正式推荐的生图入口仍是 POST /v1/responses + tools=[{\"type\":\"image_generation\"}]；如客户端仍使用旧版 Images API，目前兼容 /v1/images/generations、/v1/images/edits 和 /v1/images/variations。",
            "recommended_endpoint": "POST /v1/responses",
            "recommended_tool": {"type": "image_generation", "model": "gpt-image-2"},
            "edit_hint": "图片编辑也应继续走 /v1/responses，并把源图放进 input 内容，工具 action 设为 edit。",
            "variation_hint": "图片变体当前通过兼容适配层映射到同一条 Responses 图片编辑内核，建议优先迁移到 /v1/responses 自行控制提示词。",
        }
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
    if legacy_images_hint is not None:
        detail["legacy_images_hint"] = legacy_images_hint
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
