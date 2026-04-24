import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any
from uuid import uuid4

import httpx
from fastapi import HTTPException, status
from sqlalchemy.orm import Session

from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.services.api_key_service import ApiClientAuthContext, ApiKeyService
from app.services.log_service import LogService
from app.services.provider_service import ProviderService
from app.services.router_service import RoutePolicyContext, RouterService
from app.services.setting_service import SettingService
from app.utils.json_utils import dumps_json, safeJsonParse


class ProxyService:
    @staticmethod
    async def chat_completions(db: Session, payload: dict[str, Any]) -> tuple[dict[str, Any], Provider, list[dict], int]:
        return await ProxyService.forward_json_request(db, endpoint_path="/chat/completions", payload=payload, log_type="chat")

    @staticmethod
    async def stream_chat_completions(
        db: Session, payload: dict[str, Any]
    ) -> tuple[AsyncIterator[bytes], Provider, list[dict], int]:
        return await ProxyService.forward_stream_request(db, endpoint_path="/chat/completions", payload=payload, log_type="chat")

    @staticmethod
    async def responses(db: Session, payload: dict[str, Any]) -> tuple[dict[str, Any], Provider, list[dict], int]:
        return await ProxyService.forward_json_request(db, endpoint_path="/responses", payload=payload, log_type="responses")

    @staticmethod
    async def stream_responses(db: Session, payload: dict[str, Any]) -> tuple[AsyncIterator[bytes], Provider, list[dict], int]:
        return await ProxyService.forward_stream_request(db, endpoint_path="/responses", payload=payload, log_type="responses")

    @staticmethod
    async def embeddings(db: Session, payload: dict[str, Any]) -> tuple[dict[str, Any], Provider, list[dict], int]:
        return await ProxyService.forward_json_request(db, endpoint_path="/embeddings", payload=payload, log_type="embeddings")

    @staticmethod
    async def forward_json_request(
        db: Session,
        *,
        endpoint_path: str,
        payload: dict[str, Any],
        log_type: str,
        forced_provider_id: int | None = None,
        route_context: RoutePolicyContext | None = None,
        api_client_auth: ApiClientAuthContext | None = None,
    ) -> tuple[dict[str, Any], Provider, list[dict], int]:
        model_name = payload.get("model")
        has_image = ProxyService._payload_has_image(payload)
        setting = SettingService.get_or_create(db)
        request_id = uuid4().hex
        conversation_key = ProxyService._extract_conversation_key(payload, request_id)
        request_body_json = ProxyService._serialize_payload_for_logging(payload, setting=setting)
        if payload.get("stream") is True:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Use stream endpoint handler for {endpoint_path}")

        candidates = RouterService.order_candidates(
            db,
            model_name=model_name,
            sticky_key=ProxyService._extract_sticky_key(payload),
            forced_provider_id=forced_provider_id,
            route_context=route_context,
            require_vision=has_image,
        )
        if not candidates:
            LogService.create_log(
                db,
                log_type=log_type,
                model_name=model_name,
                requested_model=model_name,
                request_id=request_id,
                conversation_key=conversation_key,
                request_path=f"/v1{endpoint_path}",
                is_stream=False,
                has_image=has_image,
                success=False,
                status_code=status.HTTP_404_NOT_FOUND,
                request_body_json=request_body_json,
                message="No available provider for requested model",
                **ProxyService._build_api_client_log_kwargs(api_client_auth, auth_result="authenticated"),
                trace=[],
                token_request_payload=payload,
                schedule_token_fill=setting.enable_token_logging,
            )
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No available provider for requested model")

        trace: list[dict] = []
        last_upstream_error: dict[str, Any] | None = None

        for candidate in candidates:
            provider = candidate.provider
            provider_model = candidate.provider_model
            retries = max(1, min(provider.max_retries, setting.global_max_retries))
            for _ in range(retries):
                started = time.perf_counter()
                try:
                    response, upstream_request_id = await ProxyService._forward_json(provider, endpoint_path, payload)
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    usage_info = ProxyService._extract_usage_info(response) if setting.enable_token_logging else {
                        "prompt_tokens": None,
                        "completion_tokens": None,
                        "total_tokens": None,
                    }
                    finish_reason = ProxyService._extract_finish_reason(response)
                    response_body_json = ProxyService._serialize_payload_for_logging(response, setting=setting)
                    response_text = (
                        ProxyService._extract_response_text(response, limit_bytes=setting.max_logged_body_bytes)
                        if setting.enable_payload_logging
                        else None
                    )
                    ProxyService._mark_success(db, provider, provider_model, latency_ms)
                    trace.append(ProxyService._build_trace_item(provider, provider_model, "success", latency_ms, status_code=200))
                    LogService.create_log(
                        db,
                        log_type=log_type,
                        provider_id=provider.id,
                        provider_name=provider.name,
                        model_name=model_name,
                        requested_model=model_name,
                        request_id=request_id,
                        conversation_key=conversation_key,
                        resolved_provider_model_id=provider_model.id,
                        request_path=f"/v1{endpoint_path}",
                        is_stream=False,
                        has_image=has_image,
                        success=True,
                        status_code=200,
                        latency_ms=latency_ms,
                        prompt_tokens=usage_info["prompt_tokens"],
                        completion_tokens=usage_info["completion_tokens"],
                        total_tokens=usage_info["total_tokens"],
                        finish_reason=finish_reason,
                        upstream_request_id=upstream_request_id,
                        request_body_json=request_body_json,
                        response_body_json=response_body_json,
                        response_text=response_text,
                        message=f"{log_type} success",
                        **ProxyService._build_api_client_log_kwargs(api_client_auth, auth_result="authenticated"),
                        trace=trace,
                        token_request_payload=payload,
                        token_response_payload=response,
                        token_response_text=response_text,
                        schedule_token_fill=setting.enable_token_logging,
                    )
                    ApiKeyService.apply_token_usage(
                        db,
                        api_client_key=api_client_auth.api_client_key if api_client_auth else None,
                        prompt_tokens=usage_info["prompt_tokens"],
                        completion_tokens=usage_info["completion_tokens"],
                        total_tokens=usage_info["total_tokens"],
                    )
                    return response, provider, trace, latency_ms
                except httpx.HTTPStatusError as exc:
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    error_body = await ProxyService._extract_response_error(exc.response)
                    trace.append(
                        ProxyService._build_trace_item(
                            provider,
                            provider_model,
                            ProxyService._classify_http_error(exc.response.status_code),
                            latency_ms,
                            status_code=exc.response.status_code,
                            error=error_body,
                        )
                    )
                    last_upstream_error = {
                        "status_code": exc.response.status_code,
                        "detail": ProxyService._normalize_error_detail(error_body),
                    }
                    ProxyService._mark_failure(db, provider, provider_model, latency_ms, error_body)
                    if exc.response.status_code not in {401, 403, 404, 429} and exc.response.status_code < 500:
                        break
                except Exception as exc:
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    trace.append(
                        ProxyService._build_trace_item(
                            provider,
                            provider_model,
                            ProxyService._classify_exception(exc),
                            latency_ms,
                            error=str(exc),
                        )
                    )
                    last_upstream_error = {
                        "status_code": status.HTTP_502_BAD_GATEWAY,
                        "detail": {"message": str(exc)},
                    }
                    ProxyService._mark_failure(db, provider, provider_model, latency_ms, str(exc))

        ProxyService._raise_final_error(
            db,
            model_name=model_name,
            endpoint_path=endpoint_path,
            log_type=log_type,
            trace=trace,
            upstream_error=last_upstream_error,
            requested_model=model_name,
            request_id=request_id,
            conversation_key=conversation_key,
            resolved_provider_model_id=None,
            is_stream=False,
            has_image=has_image,
            request_body_json=request_body_json,
            request_payload=payload,
            schedule_token_fill=setting.enable_token_logging,
            api_client_auth=api_client_auth,
        )

    @staticmethod
    async def forward_stream_request(
        db: Session,
        *,
        endpoint_path: str,
        payload: dict[str, Any],
        log_type: str,
        forced_provider_id: int | None = None,
        route_context: RoutePolicyContext | None = None,
        api_client_auth: ApiClientAuthContext | None = None,
    ) -> tuple[AsyncIterator[bytes], Provider, list[dict], int]:
        model_name = payload.get("model")
        has_image = ProxyService._payload_has_image(payload)
        setting = SettingService.get_or_create(db)
        request_id = uuid4().hex
        conversation_key = ProxyService._extract_conversation_key(payload, request_id)
        request_body_json = ProxyService._serialize_payload_for_logging(payload, setting=setting)
        candidates = RouterService.order_candidates(
            db,
            model_name=model_name,
            sticky_key=ProxyService._extract_sticky_key(payload),
            forced_provider_id=forced_provider_id,
            route_context=route_context,
            require_vision=has_image,
        )
        if not candidates:
            LogService.create_log(
                db,
                log_type=log_type,
                model_name=model_name,
                requested_model=model_name,
                request_id=request_id,
                conversation_key=conversation_key,
                request_path=f"/v1{endpoint_path}",
                is_stream=True,
                has_image=has_image,
                success=False,
                status_code=status.HTTP_404_NOT_FOUND,
                request_body_json=request_body_json,
                message="No available provider for requested model",
                **ProxyService._build_api_client_log_kwargs(api_client_auth, auth_result="authenticated"),
                trace=[],
                token_request_payload=payload,
                schedule_token_fill=setting.enable_token_logging,
            )
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No available provider for requested model")

        trace: list[dict] = []
        last_upstream_error: dict[str, Any] | None = None

        for candidate in candidates:
            provider = candidate.provider
            provider_model = candidate.provider_model
            retries = max(1, min(provider.max_retries, setting.global_max_retries))
            for _ in range(retries):
                started = time.perf_counter()
                stream_context = None
                trace.append(ProxyService._build_trace_item(provider, provider_model, "connecting", 0))
                try:
                    stream_context = ProxyService._stream_request(provider, endpoint_path, payload)
                    response = await stream_context.__aenter__()
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    response.raise_for_status()
                    upstream_request_id = ProxyService._extract_upstream_request_id(response)
                    trace.append(ProxyService._build_trace_item(provider, provider_model, "stream_opened", latency_ms, status_code=200))

                    async def stream_generator() -> AsyncIterator[bytes]:
                        success = False
                        interrupted = False
                        error_message: str | None = None
                        exc_type = None
                        exc_value = None
                        exc_traceback = None
                        first_chunk_latency_ms: int | None = None
                        usage_info = {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}
                        finish_reason: str | None = None
                        response_text_parts: list[str] = []
                        response_text_bytes = 0
                        token_response_parts: list[str] = []
                        event_buffer = bytearray()
                        try:
                            async for chunk in response.aiter_bytes():
                                if chunk:
                                    if setting.enable_stream_response_persist or setting.enable_token_logging:
                                        (
                                            response_text_bytes,
                                            finish_reason,
                                            usage_info,
                                        ) = ProxyService._collect_stream_log_data(
                                            chunk=chunk,
                                            event_buffer=event_buffer,
                                            response_text_parts=response_text_parts,
                                            response_text_bytes=response_text_bytes,
                                            token_response_parts=token_response_parts if setting.enable_token_logging else None,
                                            finish_reason=finish_reason,
                                            usage_info=usage_info,
                                            capture_text=setting.enable_stream_response_persist,
                                            capture_usage=setting.enable_token_logging,
                                            limit_bytes=setting.max_logged_body_bytes,
                                        )
                                    if first_chunk_latency_ms is None:
                                        first_chunk_latency_ms = int((time.perf_counter() - started) * 1000)
                                        trace.append(
                                            ProxyService._build_trace_item(
                                                provider,
                                                provider_model,
                                                "first_chunk_received",
                                                first_chunk_latency_ms,
                                            )
                                        )
                                    success = True
                                    yield chunk
                        except BaseException as exc:
                            interrupted = True
                            error_message = str(exc)
                            exc_type = type(exc)
                            exc_value = exc
                            exc_traceback = exc.__traceback__
                            raise
                        finally:
                            total_duration_ms = int((time.perf_counter() - started) * 1000)
                            await stream_context.__aexit__(exc_type, exc_value, exc_traceback)
                            if success and not interrupted:
                                ProxyService._mark_success(db, provider, provider_model, latency_ms)
                                final_trace = trace + [
                                    ProxyService._build_trace_item(
                                        provider,
                                        provider_model,
                                        "finished",
                                        total_duration_ms,
                                        status_code=200,
                                        first_token_latency_ms=first_chunk_latency_ms,
                                        total_duration_ms=total_duration_ms,
                                    ),
                                    ProxyService._build_trace_item(
                                        provider,
                                        provider_model,
                                        "success",
                                        latency_ms,
                                        status_code=200,
                                    ),
                                ]
                                LogService.create_log(
                                    db,
                                    log_type=log_type,
                                    provider_id=provider.id,
                                    provider_name=provider.name,
                                    model_name=model_name,
                                    requested_model=model_name,
                                    request_id=request_id,
                                    conversation_key=conversation_key,
                                    resolved_provider_model_id=provider_model.id,
                                    request_path=f"/v1{endpoint_path}",
                                    is_stream=True,
                                    has_image=has_image,
                                    success=True,
                                    status_code=200,
                                    latency_ms=latency_ms,
                                    first_token_latency_ms=first_chunk_latency_ms,
                                    prompt_tokens=usage_info["prompt_tokens"],
                                    completion_tokens=usage_info["completion_tokens"],
                                    total_tokens=usage_info["total_tokens"],
                                    finish_reason=finish_reason,
                                    upstream_request_id=upstream_request_id,
                                    request_body_json=request_body_json,
                                    response_text=ProxyService._finalize_text_capture(response_text_parts),
                                    message=f"stream {log_type} success",
                                    **ProxyService._build_api_client_log_kwargs(api_client_auth, auth_result="authenticated"),
                                    trace=final_trace,
                                    token_request_payload=payload,
                                    token_response_text=ProxyService._finalize_text_capture(token_response_parts),
                                    schedule_token_fill=setting.enable_token_logging,
                                )
                                ApiKeyService.apply_token_usage(
                                    db,
                                    api_client_key=api_client_auth.api_client_key if api_client_auth else None,
                                    prompt_tokens=usage_info["prompt_tokens"],
                                    completion_tokens=usage_info["completion_tokens"],
                                    total_tokens=usage_info["total_tokens"],
                                )
                            else:
                                ProxyService._mark_failure(
                                    db,
                                    provider,
                                    provider_model,
                                    latency_ms,
                                    error_message or f"stream {log_type} failed",
                                )
                                interrupted_trace = trace + [
                                    ProxyService._build_trace_item(
                                        provider,
                                        provider_model,
                                        "interrupted" if interrupted else "finished",
                                        total_duration_ms,
                                        status_code=502 if interrupted or not success else 200,
                                        first_token_latency_ms=first_chunk_latency_ms,
                                        total_duration_ms=total_duration_ms,
                                        error=error_message,
                                    )
                                ]
                                LogService.create_log(
                                    db,
                                    log_type=log_type,
                                    provider_id=provider.id,
                                    provider_name=provider.name,
                                    model_name=model_name,
                                    requested_model=model_name,
                                    request_id=request_id,
                                    conversation_key=conversation_key,
                                    resolved_provider_model_id=provider_model.id,
                                    request_path=f"/v1{endpoint_path}",
                                    is_stream=True,
                                    has_image=has_image,
                                    success=False,
                                    status_code=502,
                                    latency_ms=latency_ms,
                                    prompt_tokens=usage_info["prompt_tokens"],
                                    completion_tokens=usage_info["completion_tokens"],
                                    total_tokens=usage_info["total_tokens"],
                                    finish_reason=finish_reason,
                                    upstream_request_id=upstream_request_id,
                                    request_body_json=request_body_json,
                                    response_text=ProxyService._finalize_text_capture(response_text_parts),
                                    message=error_message or f"stream {log_type} failed",
                                    **ProxyService._build_api_client_log_kwargs(api_client_auth, auth_result="authenticated"),
                                    trace=interrupted_trace,
                                    token_request_payload=payload,
                                    token_response_text=ProxyService._finalize_text_capture(token_response_parts),
                                    schedule_token_fill=setting.enable_token_logging,
                                )

                    return stream_generator(), provider, trace, latency_ms
                except httpx.HTTPStatusError as exc:
                    if stream_context is not None:
                        await stream_context.__aexit__(type(exc), exc, exc.__traceback__)
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    error_body = await ProxyService._extract_response_error(exc.response)
                    trace.append(
                        ProxyService._build_trace_item(
                            provider,
                            provider_model,
                            ProxyService._classify_http_error(exc.response.status_code),
                            latency_ms,
                            status_code=exc.response.status_code,
                            error=error_body,
                        )
                    )
                    last_upstream_error = {
                        "status_code": exc.response.status_code,
                        "detail": ProxyService._normalize_error_detail(error_body),
                    }
                    ProxyService._mark_failure(db, provider, provider_model, latency_ms, error_body)
                    if exc.response.status_code not in {401, 403, 404, 429} and exc.response.status_code < 500:
                        break
                except Exception as exc:
                    if stream_context is not None:
                        await stream_context.__aexit__(type(exc), exc, exc.__traceback__)
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    trace.append(
                        ProxyService._build_trace_item(
                            provider,
                            provider_model,
                            ProxyService._classify_exception(exc),
                            latency_ms,
                            error=str(exc),
                        )
                    )
                    last_upstream_error = {
                        "status_code": status.HTTP_502_BAD_GATEWAY,
                        "detail": {"message": str(exc)},
                    }
                    ProxyService._mark_failure(db, provider, provider_model, latency_ms, str(exc))

        ProxyService._raise_final_error(
            db,
            model_name=model_name,
            endpoint_path=endpoint_path,
            log_type=log_type,
            trace=trace,
            upstream_error=last_upstream_error,
            requested_model=model_name,
            request_id=request_id,
            conversation_key=conversation_key,
            resolved_provider_model_id=None,
            is_stream=True,
            has_image=has_image,
            request_body_json=request_body_json,
            request_payload=payload,
            schedule_token_fill=setting.enable_token_logging,
            api_client_auth=api_client_auth,
        )

    @staticmethod
    async def _forward_json(provider: Provider, endpoint_path: str, payload: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        headers = {"Authorization": f"Bearer {provider.api_key}"}
        async with httpx.AsyncClient(timeout=provider.timeout_ms / 1000) as client:
            response = await client.post(f"{provider.base_url}{endpoint_path}", headers=headers, json=payload)
        response.raise_for_status()
        return response.json(), ProxyService._extract_upstream_request_id(response)

    @staticmethod
    @asynccontextmanager
    async def _stream_request(provider: Provider, endpoint_path: str, payload: dict[str, Any]) -> AsyncIterator[httpx.Response]:
        headers = {"Authorization": f"Bearer {provider.api_key}"}
        async with httpx.AsyncClient(timeout=provider.timeout_ms / 1000) as client:
            async with client.stream("POST", f"{provider.base_url}{endpoint_path}", headers=headers, json=payload) as response:
                yield response

    @staticmethod
    def _mark_failure(
        db: Session,
        provider: Provider,
        provider_model: ProviderModel,
        latency_ms: int,
        error_message: str | None,
    ) -> None:
        threshold = SettingService.get_or_create(db).circuit_breaker_threshold
        provider.failure_count += 1
        provider.last_latency_ms = latency_ms
        provider_model.failure_count += 1
        provider_model.last_latency_ms = latency_ms
        provider_model.last_error = error_message
        if provider_model.circuit_state == "half_open" or provider_model.failure_count >= threshold:
            provider_model.health_status = "unhealthy"
            provider_model.circuit_state = "open"
            provider_model.circuit_opened_at = datetime.utcnow()
        else:
            provider_model.health_status = "degraded"
            provider_model.circuit_state = "closed"
        ProviderService.refresh_provider_state(provider)
        db.commit()

    @staticmethod
    def _mark_success(db: Session, provider: Provider, provider_model: ProviderModel, latency_ms: int) -> None:
        provider.success_count += 1
        provider.failure_count = 0
        provider.last_latency_ms = latency_ms
        provider_model.success_count += 1
        provider_model.failure_count = 0
        provider_model.health_status = "healthy"
        provider_model.last_latency_ms = latency_ms
        provider_model.last_error = None
        provider_model.circuit_state = "closed"
        provider_model.circuit_opened_at = None
        ProviderService.refresh_provider_state(provider)
        db.commit()

    @staticmethod
    async def _extract_response_error(response: httpx.Response) -> str:
        try:
            await response.aread()
        except Exception:
            return f"upstream status {response.status_code}"

        try:
            return response.text[:500]
        except Exception:
            return f"upstream status {response.status_code}"

    @staticmethod
    def _normalize_error_detail(error_body: str) -> Any:
        if not error_body:
            return {"message": "Upstream request failed"}
        parsed = safeJsonParse(error_body)
        return parsed if parsed is not None else {"message": error_body}

    @staticmethod
    def _raise_final_error(
        db: Session,
        *,
        model_name: str | None,
        endpoint_path: str,
        log_type: str,
        trace: list[dict],
        upstream_error: dict[str, Any] | None,
        requested_model: str | None,
        request_id: str | None,
        conversation_key: str | None,
        resolved_provider_model_id: int | None,
        is_stream: bool,
        has_image: bool,
        request_body_json: str | None,
        request_payload: dict[str, Any] | None,
        schedule_token_fill: bool,
        api_client_auth: ApiClientAuthContext | None = None,
    ) -> None:
        if ProxyService._attempt_count(trace) <= 1 and upstream_error is not None:
            status_code = upstream_error["status_code"]
            detail = upstream_error["detail"]
            message = ProxyService._error_message_for_log(detail)
            LogService.create_log(
                db,
                log_type=log_type,
                model_name=model_name,
                requested_model=requested_model,
                request_id=request_id,
                conversation_key=conversation_key,
                resolved_provider_model_id=resolved_provider_model_id,
                request_path=f"/v1{endpoint_path}",
                is_stream=is_stream,
                has_image=has_image,
                success=False,
                status_code=status_code,
                request_body_json=request_body_json,
                message=message,
                **ProxyService._build_api_client_log_kwargs(api_client_auth, auth_result="authenticated"),
                trace=trace,
                token_request_payload=request_payload,
                schedule_token_fill=schedule_token_fill,
            )
            raise HTTPException(status_code=status_code, detail=detail)

        detail = {"message": "All providers failed", "trace": trace}
        LogService.create_log(
            db,
            log_type=log_type,
            model_name=model_name,
            requested_model=requested_model,
            request_id=request_id,
            conversation_key=conversation_key,
            resolved_provider_model_id=resolved_provider_model_id,
            request_path=f"/v1{endpoint_path}",
            is_stream=is_stream,
            has_image=has_image,
            success=False,
            status_code=502,
            request_body_json=request_body_json,
            message="All providers failed",
            **ProxyService._build_api_client_log_kwargs(api_client_auth, auth_result="authenticated"),
            trace=trace,
            token_request_payload=request_payload,
            schedule_token_fill=schedule_token_fill,
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)

    @staticmethod
    def _error_message_for_log(detail: Any) -> str:
        if isinstance(detail, dict):
            if isinstance(detail.get("error"), dict) and detail["error"].get("message"):
                return str(detail["error"]["message"])
            if detail.get("message"):
                return str(detail["message"])
        return str(detail)

    @staticmethod
    def _build_trace_item(
        provider: Provider,
        provider_model: ProviderModel,
        result: str,
        latency_ms: int,
        *,
        status_code: int | None = None,
        error: str | None = None,
        first_token_latency_ms: int | None = None,
        total_duration_ms: int | None = None,
    ) -> dict[str, Any]:
        item: dict[str, Any] = {
            "provider_id": provider.id,
            "provider_name": provider.name,
            "provider_model_id": provider_model.id,
            "model_name": provider_model.model_name,
            "result": result,
            "latency_ms": latency_ms,
        }
        if status_code is not None:
            item["status_code"] = status_code
        if error:
            item["error"] = error
        if first_token_latency_ms is not None:
            item["first_token_latency_ms"] = first_token_latency_ms
        if total_duration_ms is not None:
            item["total_duration_ms"] = total_duration_ms
        return item

    @staticmethod
    def _attempt_count(trace: list[dict]) -> int:
        return sum(1 for item in trace if item.get("result") in {"http_error", "exception", "success", "stream_opened"})

    @staticmethod
    def _extract_sticky_key(payload: dict[str, Any]) -> str | None:
        if isinstance(payload.get("user"), str) and payload["user"].strip():
            return payload["user"].strip()
        metadata = payload.get("metadata")
        if isinstance(metadata, dict):
            for key in ("session_id", "conversation_id", "thread_id"):
                value = metadata.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        return None

    @staticmethod
    def _payload_has_image(payload: dict[str, Any]) -> bool:
        return ProxyService._value_has_image(payload.get("messages")) or ProxyService._value_has_image(payload.get("input"))

    @staticmethod
    def _value_has_image(value: Any) -> bool:
        if isinstance(value, list):
            return any(ProxyService._value_has_image(item) for item in value)
        if isinstance(value, dict):
            item_type = value.get("type")
            if item_type in {"image_url", "input_image"}:
                return True
            if isinstance(value.get("image_url"), (dict, str)):
                return True
            return any(ProxyService._value_has_image(item) for item in value.values())
        return False

    @staticmethod
    def _classify_http_error(status_code: int) -> str:
        if status_code in {401, 403}:
            return "upstream_auth_error"
        if status_code == 404:
            return "model_not_found"
        if status_code == 429:
            return "rate_limited"
        if status_code >= 500:
            return "http_error"
        return "request_rejected"

    @staticmethod
    def _classify_exception(exc: BaseException) -> str:
        if exc.__class__.__name__ == "CancelledError":
            return "client_cancelled"
        return "exception"

    @staticmethod
    def _extract_conversation_key(payload: dict[str, Any], request_id: str) -> str:
        return ProxyService._extract_sticky_key(payload) or request_id

    @staticmethod
    def _extract_upstream_request_id(response: httpx.Response) -> str | None:
        for key in ("x-request-id", "request-id", "openai-request-id"):
            value = response.headers.get(key)
            if value:
                return value
        return None

    @staticmethod
    def _extract_usage_info(response_json: dict[str, Any]) -> dict[str, int | None]:
        usage = response_json.get("usage")
        if not isinstance(usage, dict):
            return {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}
        prompt_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
        completion_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
        total_tokens = usage.get("total_tokens")
        return {
            "prompt_tokens": int(prompt_tokens) if isinstance(prompt_tokens, int) else None,
            "completion_tokens": int(completion_tokens) if isinstance(completion_tokens, int) else None,
            "total_tokens": int(total_tokens) if isinstance(total_tokens, int) else None,
        }

    @staticmethod
    def _extract_finish_reason(response_json: dict[str, Any]) -> str | None:
        choices = response_json.get("choices")
        if isinstance(choices, list) and choices:
            finish_reason = choices[0].get("finish_reason")
            if isinstance(finish_reason, str):
                return finish_reason
        output = response_json.get("output")
        if isinstance(output, list):
            for item in output:
                if isinstance(item, dict):
                    finish_reason = item.get("finish_reason") or item.get("status")
                    if isinstance(finish_reason, str):
                        return finish_reason
        return None

    @staticmethod
    def _extract_response_text(response_json: dict[str, Any], *, limit_bytes: int) -> str | None:
        parts: list[str] = []
        current_bytes = 0

        def append_text(value: str) -> None:
            nonlocal current_bytes
            if not value or current_bytes >= limit_bytes:
                return
            encoded = value.encode("utf-8", errors="ignore")
            remaining = limit_bytes - current_bytes
            if len(encoded) > remaining:
                value = encoded[:remaining].decode("utf-8", errors="ignore")
                encoded = value.encode("utf-8", errors="ignore")
            if value:
                parts.append(value)
                current_bytes += len(encoded)

        choices = response_json.get("choices")
        if isinstance(choices, list):
            for choice in choices:
                if not isinstance(choice, dict):
                    continue
                message = choice.get("message")
                if isinstance(message, dict):
                    content = message.get("content")
                    if isinstance(content, str):
                        append_text(content)
                delta = choice.get("delta")
                if isinstance(delta, dict):
                    content = delta.get("content")
                    if isinstance(content, str):
                        append_text(content)

        output_text = response_json.get("output_text")
        if isinstance(output_text, str):
            append_text(output_text)

        output = response_json.get("output")
        if isinstance(output, list):
            for item in output:
                if not isinstance(item, dict):
                    continue
                content = item.get("content")
                if isinstance(content, list):
                    for block in content:
                        if not isinstance(block, dict):
                            continue
                        text_value = block.get("text")
                        if isinstance(text_value, str):
                            append_text(text_value)

        return "".join(parts) or None

    @staticmethod
    def _serialize_payload_for_logging(payload: dict[str, Any], *, setting: Any) -> str | None:
        if not getattr(setting, "enable_payload_logging", True):
            return None
        sanitized = ProxyService._sanitize_for_logging(payload, mask_sensitive=getattr(setting, "mask_sensitive_fields", True))
        return ProxyService._truncate_serialized_json(sanitized, getattr(setting, "max_logged_body_bytes", 16384))

    @staticmethod
    def _sanitize_for_logging(value: Any, *, mask_sensitive: bool) -> Any:
        if isinstance(value, dict):
            sanitized: dict[str, Any] = {}
            for key, item in value.items():
                lowered = key.lower()
                if mask_sensitive and any(token in lowered for token in ("api_key", "authorization", "secret", "password", "token")):
                    sanitized[key] = "***"
                    continue
                if lowered in {"image", "image_base64", "b64_json"} and isinstance(item, str):
                    sanitized[key] = f"[omitted binary payload: {len(item)} chars]"
                    continue
                sanitized[key] = ProxyService._sanitize_for_logging(item, mask_sensitive=mask_sensitive)
            return sanitized
        if isinstance(value, list):
            return [ProxyService._sanitize_for_logging(item, mask_sensitive=mask_sensitive) for item in value]
        return value

    @staticmethod
    def _truncate_serialized_json(value: Any, limit_bytes: int) -> str:
        serialized = dumps_json(value)
        encoded = serialized.encode("utf-8", errors="ignore")
        if len(encoded) <= limit_bytes:
            return serialized
        clipped = encoded[:limit_bytes].decode("utf-8", errors="ignore")
        return f"{clipped}...[truncated]"

    @staticmethod
    def _collect_stream_log_data(
        *,
        chunk: bytes,
        event_buffer: bytearray,
        response_text_parts: list[str],
        response_text_bytes: int,
        token_response_parts: list[str] | None,
        finish_reason: str | None,
        usage_info: dict[str, int | None],
        capture_text: bool,
        capture_usage: bool,
        limit_bytes: int,
    ) -> tuple[int, str | None, dict[str, int | None]]:
        event_buffer.extend(chunk)
        while True:
            separator_index = event_buffer.find(b"\n\n")
            if separator_index < 0:
                break
            raw_event = bytes(event_buffer[:separator_index])
            del event_buffer[: separator_index + 2]
            event_text = raw_event.decode("utf-8", errors="ignore")
            for line in event_text.splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data or data == "[DONE]":
                    continue
                event_json = safeJsonParse(data)
                if not isinstance(event_json, dict):
                    continue
                if capture_usage:
                    extracted_usage = ProxyService._extract_usage_info(event_json)
                    for key, value in extracted_usage.items():
                        if value is not None:
                            usage_info[key] = value
                if finish_reason is None:
                    finish_reason = ProxyService._extract_finish_reason(event_json)
                if capture_text or token_response_parts is not None:
                    delta_text = ProxyService._extract_response_text(event_json, limit_bytes=max(limit_bytes, 1_048_576))
                    if delta_text and capture_text:
                        response_text_bytes = ProxyService._append_limited_text(
                            response_text_parts,
                            delta_text,
                            current_bytes=response_text_bytes,
                            limit_bytes=limit_bytes,
                        )
                    if delta_text and token_response_parts is not None:
                        token_response_parts.append(delta_text)
        return response_text_bytes, finish_reason, usage_info

    @staticmethod
    def _append_limited_text(parts: list[str], value: str, *, current_bytes: int, limit_bytes: int) -> int:
        if current_bytes >= limit_bytes or not value:
            return current_bytes
        encoded = value.encode("utf-8", errors="ignore")
        remaining = limit_bytes - current_bytes
        if len(encoded) > remaining:
            value = encoded[:remaining].decode("utf-8", errors="ignore")
            encoded = value.encode("utf-8", errors="ignore")
        if value:
            parts.append(value)
            current_bytes += len(encoded)
        return current_bytes

    @staticmethod
    def _finalize_text_capture(parts: list[str]) -> str | None:
        if not parts:
            return None
        return "".join(parts)

    @staticmethod
    def _build_api_client_log_kwargs(
        api_client_auth: ApiClientAuthContext | None,
        *,
        auth_result: str | None,
    ) -> dict[str, Any]:
        if api_client_auth is None:
            return {}
        remaining_tokens = None
        if api_client_auth.api_client_key.token_limit_total is not None:
            remaining_tokens = max(
                0,
                api_client_auth.api_client_key.token_limit_total - api_client_auth.api_client_key.total_tokens_used,
            )
        return {
            "api_client_key_id": api_client_auth.api_client_key.id,
            "api_client_key_name": api_client_auth.api_client_key.name,
            "api_client_key_prefix": api_client_auth.api_client_key.key_prefix,
            "api_client_auth_result": auth_result,
            "api_client_remaining_tokens": remaining_tokens if remaining_tokens is not None else api_client_auth.remaining_tokens,
            "api_client_policy_snapshot_json": api_client_auth.policy_snapshot_json,
        }

    @staticmethod
    def list_models(
        db: Session,
        *,
        route_context: RoutePolicyContext | None = None,
    ) -> dict[str, Any]:
        model_set = {
            candidate.provider_model.model_name
            for candidate in RouterService.get_available_candidates(db, route_context=route_context)
        }
        return {
            "object": "list",
            "data": [{"id": model_name, "object": "model", "owned_by": "aotu-gpt", "permission": []} for model_name in sorted(model_set)],
        }
