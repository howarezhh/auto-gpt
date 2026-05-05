import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from uuid import uuid4

import httpx
from fastapi import HTTPException, status
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.database import SessionLocal
from app.models.model_catalog import ModelCatalog
from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.services.api_key_service import ApiKeyService
from app.services.api_key_service import ApiClientAuthContext
from app.services.cache_service import CacheService
from app.services.log_service import LogService
from app.services.provider_capacity_service import (
    ProviderCapacityExceededError,
    ProviderCapacityService,
    ProviderCapacityUnavailableError,
)
from app.services.provider_service import ProviderService
from app.services.router_service import RoutePolicyContext, RouterService
from app.services.setting_service import SettingService
from app.services.token_usage_service import TokenUsageService
from app.services.upstream_client import UpstreamClientService
from app.utils.json_utils import dumps_json, safeJsonParse


@dataclass(slots=True)
class PreparedUpstreamRequest:
    request_path: str
    request_payload: dict[str, Any]
    adapt_chat_response_to_responses: bool = False
    adapt_responses_response_to_chat: bool = False
    fallback_from_path: str | None = None


@dataclass(slots=True)
class RequestsUpstreamHTTPError(Exception):
    status_code: int
    detail: Any


@dataclass(slots=True)
class NonStreamResponseTooLarge(Exception):
    status_code: int
    detail: dict[str, Any]


@dataclass(slots=True)
class EndpointConversionSafety:
    safe: bool
    code: str | None = None
    message: str | None = None
    unsafe_fields: list[str] | None = None
    unsafe_reasons: list[str] | None = None


@dataclass(slots=True)
class StreamTimeoutPolicy:
    first_token_timeout_seconds: int
    idle_timeout_seconds: int
    max_duration_seconds: int


class StreamTimeoutError(Exception):
    def __init__(self, *, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ProxyService:
    RESPONSES_CHAT_ADAPTER_SAFE_FIELDS = {
        "model",
        "instructions",
        "input",
        "temperature",
        "top_p",
        "presence_penalty",
        "frequency_penalty",
        "stream",
        "user",
        "metadata",
        "seed",
        "max_output_tokens",
        "max_tokens",
    }
    CHAT_RESPONSES_ADAPTER_SAFE_FIELDS = {
        "model",
        "messages",
        "temperature",
        "top_p",
        "presence_penalty",
        "frequency_penalty",
        "stream",
        "user",
        "metadata",
        "seed",
        "max_tokens",
        "max_completion_tokens",
    }
    ENDPOINT_ADAPTER_RISKY_FIELDS = {
        "tools",
        "tool_choice",
        "functions",
        "function_call",
        "response_format",
        "previous_response_id",
        "parallel_tool_calls",
        "reasoning",
        "reasoning_effort",
        "store",
        "text",
        "include",
        "max_tool_calls",
        "truncation",
        "background",
        "modalities",
        "audio",
        "prediction",
    }

    @staticmethod
    def _get_setting_with_scoped_session():
        db = SessionLocal()
        try:
            return SettingService.get_or_create(db)
        finally:
            db.close()

    @staticmethod
    async def _get_setting_async(db: Session | None = None):
        if db is not None:
            return await run_in_threadpool(SettingService.get_or_create, db)
        return await run_in_threadpool(ProxyService._get_setting_with_scoped_session)

    @staticmethod
    def _run_db_write_sync(operation, *args, **kwargs):
        db = SessionLocal()
        try:
            result = operation(db, *args, **kwargs)
            db.commit()
            return result
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _raise_final_error_with_scoped_session(**kwargs) -> None:
        db = SessionLocal()
        try:
            ProxyService._raise_final_error(db, **kwargs)
        except HTTPException:
            raise
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    async def _run_db_write(operation, *args, db: Session | None = None, **kwargs):
        if db is not None:
            return await run_in_threadpool(operation, db, *args, **kwargs)
        return await run_in_threadpool(ProxyService._run_db_write_sync, operation, *args, **kwargs)

    @staticmethod
    def _get_model_catalog_limits(model_name: str | None) -> dict[str, int | bool | None]:
        if not model_name:
            return {}
        db = SessionLocal()
        try:
            catalog = db.query(ModelCatalog).filter(ModelCatalog.model_name == model_name).first()
            if catalog is None:
                return {}
            return {
                "context_window_tokens": catalog.context_window_tokens,
                "max_input_tokens": catalog.max_input_tokens,
                "max_output_tokens": catalog.max_output_tokens,
                "supports_chat_completions": catalog.supports_chat_completions,
                "supports_responses": catalog.supports_responses,
                "supports_tools": catalog.supports_tools,
            }
        finally:
            db.close()

    @staticmethod
    def _estimate_request_tokens_for_precheck(
        payload: dict[str, Any],
        *,
        model_name: str | None,
        request_path: str,
        nearest_limit: int,
    ) -> tuple[int | None, str]:
        fast_estimate = TokenUsageService.fast_estimate_request_tokens(payload, request_path=request_path)
        if fast_estimate is None:
            return None, "fast_failed"
        if nearest_limit > 0 and fast_estimate > nearest_limit:
            return fast_estimate, "fast"
        if nearest_limit > 0 and fast_estimate < int(nearest_limit * 0.75):
            return fast_estimate, "fast"
        try:
            exact = TokenUsageService.estimate_request_tokens(
                payload,
                model_name=model_name,
                request_path=request_path,
            )
        except Exception:
            return fast_estimate, "fast_after_exact_failed"
        return exact, "exact"

    @staticmethod
    def _build_request_token_limit_error(
        *,
        setting: Any,
        payload: dict[str, Any],
        model_name: str | None,
        endpoint_path: str,
    ) -> tuple[int, dict[str, Any]] | None:
        limit = int(getattr(setting, "global_max_request_tokens", 0) or 0)
        model_limits = ProxyService._get_model_catalog_limits(model_name)
        max_input_tokens = int(model_limits.get("max_input_tokens") or 0)
        context_window_tokens = int(model_limits.get("context_window_tokens") or 0)
        effective_input_limit = max_input_tokens or context_window_tokens
        nearest_limits = [item for item in (limit, effective_input_limit) if item > 0]
        if not nearest_limits:
            return None
        request_path = f"/v1{endpoint_path}"
        nearest_limit = min(nearest_limits)
        estimated_tokens, estimation_mode = ProxyService._estimate_request_tokens_for_precheck(
            payload,
            model_name=model_name,
            request_path=request_path,
            nearest_limit=nearest_limit,
        )
        if estimated_tokens is None:
            return status.HTTP_400_BAD_REQUEST, {
                "message": "无法估算请求 Token，已按全局最大请求 Token 策略拒绝",
                "code": "request_token_estimation_failed",
                "model": model_name,
                "request_path": request_path,
                "global_max_request_tokens": limit,
                "model_max_input_tokens": max_input_tokens or None,
                "model_context_window_tokens": context_window_tokens or None,
                "token_estimation_mode": estimation_mode,
            }
        if limit > 0 and estimated_tokens > limit:
            return status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, {
                "message": f"请求 Token 估算值 {estimated_tokens} 超过全局最大请求 Token {limit}",
                "code": "request_tokens_exceeded",
                "model": model_name,
                "request_path": request_path,
                "estimated_request_tokens": estimated_tokens,
                "global_max_request_tokens": limit,
                "token_estimation_mode": estimation_mode,
            }
        if effective_input_limit > 0 and estimated_tokens > effective_input_limit:
            return status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, {
                "message": f"请求输入 Token 估算值 {estimated_tokens} 超过模型输入窗口 {effective_input_limit}",
                "code": "model_input_tokens_exceeded",
                "model": model_name,
                "request_path": request_path,
                "estimated_request_tokens": estimated_tokens,
                "model_max_input_tokens": max_input_tokens or None,
                "model_context_window_tokens": context_window_tokens or None,
                "token_estimation_mode": estimation_mode,
            }
        return None

    @staticmethod
    async def _reject_by_request_token_limit_async(
        db: Session | None,
        *,
        status_code: int,
        detail: dict[str, Any],
        model_name: str | None,
        endpoint_path: str,
        log_type: str,
        request_id: str,
        is_stream: bool,
        has_image: bool,
        reasoning_level: str,
        model_reasoning_effort: str | None,
        api_client_auth: ApiClientAuthContext | None,
        trace_id: str | None,
        source_ip: str | None,
    ) -> None:
        trace = [{
            "result": "request_token_limit_rejected",
            "latency_ms": 0,
            "status_code": status_code,
            "error": detail.get("code"),
            "estimated_request_tokens": detail.get("estimated_request_tokens"),
            "global_max_request_tokens": detail.get("global_max_request_tokens"),
            "token_estimation_mode": detail.get("token_estimation_mode"),
        }]
        await ProxyService._run_db_write(
            LogService.create_log,
            db=db,
            log_type=log_type,
            trace_id=trace_id,
            model_name=model_name,
            requested_model=model_name,
            tenant_name=api_client_auth.api_client_key.tenant_name if api_client_auth else None,
            project_name=api_client_auth.api_client_key.project_name if api_client_auth else None,
            app_name=api_client_auth.api_client_key.app_name if api_client_auth else None,
            environment_name=api_client_auth.api_client_key.environment_name if api_client_auth else None,
            request_id=request_id,
            conversation_key=None,
            session_id=None,
            request_path=f"/v1{endpoint_path}",
            source_ip=source_ip,
            http_method="POST",
            is_stream=is_stream,
            has_image=has_image,
            success=False,
            status_code=status_code,
            reasoning_level=reasoning_level,
            model_reasoning_effort=model_reasoning_effort,
            request_body_json=None,
            message=detail.get("message"),
            error_type=ProxyService._error_type_from_status(status_code),
            error_code=detail.get("code"),
            retryable=False,
            **ProxyService._build_api_client_log_kwargs(api_client_auth, auth_result="authenticated"),
            trace=trace,
            attempt_count=0,
            token_request_payload=None,
            schedule_token_fill=False,
        )
        raise HTTPException(status_code=status_code, detail=detail)

    @staticmethod
    def _requested_output_token_limit(payload: dict[str, Any]) -> int | None:
        for key in ("max_tokens", "max_completion_tokens", "max_output_tokens"):
            value = payload.get(key)
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)) and int(value) > 0:
                return int(value)
        return None

    @staticmethod
    def _payload_uses_tools(payload: dict[str, Any]) -> bool:
        if any(key in payload for key in ("tools", "tool_choice", "functions", "function_call", "parallel_tool_calls")):
            return True
        return ProxyService._value_has_tool_context(payload.get("messages")) or ProxyService._value_has_tool_context(payload.get("input"))

    @staticmethod
    def _value_has_tool_context(value: Any) -> bool:
        if isinstance(value, list):
            return any(ProxyService._value_has_tool_context(item) for item in value)
        if isinstance(value, dict):
            if any(key in value for key in ("tool_calls", "tool_call_id", "function_call")):
                return True
            if value.get("type") in {"tool_call", "function_call", "tool_result", "function_call_output"}:
                return True
            return any(ProxyService._value_has_tool_context(item) for item in value.values())
        return False

    @staticmethod
    def _build_long_output_error(
        *,
        setting: Any,
        payload: dict[str, Any],
        model_name: str | None,
        endpoint_path: str,
        is_stream: bool,
    ) -> tuple[int, dict[str, Any]] | None:
        requested_output_tokens = ProxyService._requested_output_token_limit(payload)
        if requested_output_tokens is None:
            return None
        model_limits = ProxyService._get_model_catalog_limits(model_name)
        max_output_tokens = int(model_limits.get("max_output_tokens") or 0)
        if max_output_tokens > 0 and requested_output_tokens > max_output_tokens:
            return status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, {
                "message": f"请求输出 Token 上限 {requested_output_tokens} 超过模型最大输出 Token {max_output_tokens}",
                "code": "model_output_tokens_exceeded",
                "model": model_name,
                "request_path": f"/v1{endpoint_path}",
                "requested_output_tokens": requested_output_tokens,
                "model_max_output_tokens": max_output_tokens,
            }
        threshold = int(getattr(setting, "long_output_stream_threshold_tokens", 0) or 0)
        if not is_stream and threshold > 0 and requested_output_tokens > threshold:
            return status.HTTP_400_BAD_REQUEST, {
                "message": f"请求输出 Token 上限 {requested_output_tokens} 超过非流式阈值 {threshold}，请使用 stream=true",
                "code": "long_output_requires_stream",
                "model": model_name,
                "request_path": f"/v1{endpoint_path}",
                "requested_output_tokens": requested_output_tokens,
                "long_output_stream_threshold_tokens": threshold,
            }
        return None

    @staticmethod
    def _mark_success_by_id(db: Session, provider_id: int, provider_model_id: int, latency_ms: int) -> None:
        provider = db.get(Provider, provider_id)
        provider_model = db.get(ProviderModel, provider_model_id)
        if provider is None or provider_model is None:
            return
        ProxyService._mark_success(db, provider, provider_model, latency_ms)

    @staticmethod
    async def _mark_success_async(
        provider: Provider,
        provider_model: ProviderModel,
        latency_ms: int,
        *,
        db: Session | None = None,
    ) -> None:
        if db is not None:
            await run_in_threadpool(ProxyService._mark_success, db, provider, provider_model, latency_ms)
            return
        await ProxyService._run_db_write(
            ProxyService._mark_success_by_id,
            provider.id,
            provider_model.id,
            latency_ms,
        )

    @staticmethod
    def _mark_failure_by_id(
        db: Session,
        provider_id: int,
        provider_model_id: int,
        latency_ms: int,
        error_message: str | None,
        force_unhealthy: bool = False,
    ) -> None:
        provider = db.get(Provider, provider_id)
        provider_model = db.get(ProviderModel, provider_model_id)
        if provider is None or provider_model is None:
            return
        ProxyService._mark_failure(
            db,
            provider,
            provider_model,
            latency_ms,
            error_message,
            force_unhealthy=force_unhealthy,
        )

    @staticmethod
    async def _mark_failure_async(
        provider: Provider,
        provider_model: ProviderModel,
        latency_ms: int,
        error_message: str | None,
        *,
        db: Session | None = None,
        force_unhealthy: bool = False,
    ) -> None:
        if db is not None:
            await run_in_threadpool(
                ProxyService._mark_failure,
                db,
                provider,
                provider_model,
                latency_ms,
                error_message,
                force_unhealthy,
            )
            return
        await ProxyService._run_db_write(
            ProxyService._mark_failure_by_id,
            provider.id,
            provider_model.id,
            latency_ms,
            error_message,
            force_unhealthy,
        )

    @staticmethod
    def _create_success_log_with_provider_status(db: Session, *args, **kwargs):
        resolved_provider_model_id = kwargs.get("resolved_provider_model_id")
        provider_id = kwargs.get("provider_id")
        latency_ms = kwargs.get("latency_ms")
        if provider_id is not None and resolved_provider_model_id is not None and latency_ms is not None:
            ProxyService._mark_success_by_id(db, provider_id, resolved_provider_model_id, latency_ms)
        return LogService.create_log(db, *args, auto_commit=False, **kwargs)

    @staticmethod
    async def chat_completions(db: Session, payload: dict[str, Any]) -> tuple[dict[str, Any], Provider, list[dict], int]:
        return await ProxyService.forward_json_request(endpoint_path="/chat/completions", payload=payload, log_type="chat")

    @staticmethod
    async def stream_chat_completions(
        db: Session, payload: dict[str, Any]
    ) -> tuple[AsyncIterator[bytes], Provider, list[dict], int]:
        return await ProxyService.forward_stream_request(endpoint_path="/chat/completions", payload=payload, log_type="chat")

    @staticmethod
    async def responses(db: Session, payload: dict[str, Any]) -> tuple[dict[str, Any], Provider, list[dict], int]:
        return await ProxyService.forward_json_request(endpoint_path="/responses", payload=payload, log_type="responses")

    @staticmethod
    async def stream_responses(db: Session, payload: dict[str, Any]) -> tuple[AsyncIterator[bytes], Provider, list[dict], int]:
        return await ProxyService.forward_stream_request(endpoint_path="/responses", payload=payload, log_type="responses")

    @staticmethod
    async def forward_json_request(
        db: Session | None = None,
        *,
        endpoint_path: str,
        payload: dict[str, Any],
        log_type: str,
        forced_provider_id: int | None = None,
        route_context: RoutePolicyContext | None = None,
        api_client_auth: ApiClientAuthContext | None = None,
        trace_id: str | None = None,
        source_ip: str | None = None,
    ) -> tuple[dict[str, Any], Provider, list[dict], int]:
        payload = ProxyService._normalize_reasoning_request_payload(endpoint_path=endpoint_path, payload=payload)
        model_name = payload.get("model")
        has_image = ProxyService._payload_has_image(payload)
        require_tools = ProxyService._payload_uses_tools(payload)
        setting = await ProxyService._get_setting_async(db)
        request_id = uuid4().hex
        reasoning_level = LogService.extract_reasoning_level(payload)
        model_reasoning_effort = LogService.extract_model_reasoning_effort(payload)
        token_limit_error = ProxyService._build_request_token_limit_error(
            setting=setting,
            payload=payload,
            model_name=model_name,
            endpoint_path=endpoint_path,
        )
        if token_limit_error is not None:
            reject_status_code, reject_detail = token_limit_error
            await ProxyService._reject_by_request_token_limit_async(
                db,
                status_code=reject_status_code,
                detail=reject_detail,
                model_name=model_name,
                endpoint_path=endpoint_path,
                log_type=log_type,
                request_id=request_id,
                is_stream=False,
                has_image=has_image,
                reasoning_level=reasoning_level,
                model_reasoning_effort=model_reasoning_effort,
                api_client_auth=api_client_auth,
                trace_id=trace_id,
                source_ip=source_ip,
            )
        long_output_error = ProxyService._build_long_output_error(
            setting=setting,
            payload=payload,
            model_name=model_name,
            endpoint_path=endpoint_path,
            is_stream=False,
        )
        if long_output_error is not None:
            reject_status_code, reject_detail = long_output_error
            await ProxyService._reject_by_request_token_limit_async(
                db,
                status_code=reject_status_code,
                detail=reject_detail,
                model_name=model_name,
                endpoint_path=endpoint_path,
                log_type=log_type,
                request_id=request_id,
                is_stream=False,
                has_image=has_image,
                reasoning_level=reasoning_level,
                model_reasoning_effort=model_reasoning_effort,
                api_client_auth=api_client_auth,
                trace_id=trace_id,
                source_ip=source_ip,
            )
        conversation_key = ProxyService._extract_conversation_key(payload, request_id)
        session_id = LogService.extract_session_id(payload, conversation_key=conversation_key, fallback=request_id)
        request_body_json = ProxyService._serialize_payload_for_logging(
            payload,
            setting=setting,
            preserve_request_content_when_disabled=True,
        )
        if api_client_auth is not None and not ApiKeyService.is_model_allowed(api_client_auth.api_client_key, model_name):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"message": "Requested model is not allowed for this api key", "code": "model_not_allowed"},
            )
        if payload.get("stream") is True:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"message": f"Use stream endpoint handler for {endpoint_path}", "code": "invalid_stream_mode"},
            )

        try:
            candidates = await RouterService.async_order_candidates(
                db,
                model_name=model_name,
                sticky_key=ProxyService._extract_sticky_key(payload),
                forced_provider_id=forced_provider_id,
                route_context=route_context,
                require_vision=has_image,
                require_stream=False,
                require_tools=require_tools,
                require_chat_completions=endpoint_path == "/chat/completions",
                require_responses=endpoint_path == "/responses",
            )
        except ProviderCapacityUnavailableError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"message": str(exc), "code": exc.code},
            ) from exc
        if not candidates:
            await ProxyService._run_db_write(
                LogService.create_log,
                log_type=log_type,
                trace_id=trace_id,
                model_name=model_name,
                requested_model=model_name,
                tenant_name=api_client_auth.api_client_key.tenant_name if api_client_auth else None,
                project_name=api_client_auth.api_client_key.project_name if api_client_auth else None,
                app_name=api_client_auth.api_client_key.app_name if api_client_auth else None,
                environment_name=api_client_auth.api_client_key.environment_name if api_client_auth else None,
                request_id=request_id,
                conversation_key=conversation_key,
                session_id=session_id,
                request_path=f"/v1{endpoint_path}",
                source_ip=source_ip,
                http_method="POST",
                is_stream=False,
                has_image=has_image,
                success=False,
                status_code=status.HTTP_404_NOT_FOUND,
                reasoning_level=reasoning_level,
                model_reasoning_effort=model_reasoning_effort,
                request_body_json=request_body_json,
                message="No native tool-capable provider for requested model" if require_tools else "No available provider for requested model",
                error_type="invalid_request_error",
                error_code="model_tools_not_available" if require_tools else "model_not_available",
                retryable=False,
                **ProxyService._build_api_client_log_kwargs(api_client_auth, auth_result="authenticated"),
                trace=[],
                attempt_count=0,
                token_request_payload=payload,
                schedule_token_fill=setting.enable_token_logging,
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "message": "No native tool-capable provider for requested model" if require_tools else "No available provider for requested model",
                    "code": "model_tools_not_available" if require_tools else "model_not_available",
                    "requires_tools": require_tools,
                },
            )

        trace: list[dict] = []
        last_upstream_error: dict[str, Any] | None = None
        attempt_count = 0

        for candidate in candidates:
            provider = candidate.provider
            provider_model = candidate.provider_model
            retries = max(1, min(provider.max_retries, setting.global_max_retries))
            for _ in range(retries):
                attempt_count += 1
                started = time.perf_counter()
                try:
                    async with ProviderCapacityService.async_lease(provider, is_stream=False):
                        response, upstream_request_id, fallback_trace = await ProxyService._forward_json_with_endpoint_fallback(
                            provider,
                            provider_model,
                            endpoint_path,
                            payload,
                            started=started,
                            setting=setting,
                        )
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    usage_info = ProxyService._extract_usage_info(response) if setting.enable_token_logging else {
                        "prompt_tokens": None,
                        "completion_tokens": None,
                        "total_tokens": None,
                        "cache_read_tokens": None,
                        "cache_write_tokens": None,
                    }
                    finish_reason = ProxyService._extract_finish_reason(response)
                    response_body_json = ProxyService._serialize_payload_for_logging(response, setting=setting)
                    response_text = (
                        ProxyService._extract_response_text(response, limit_bytes=setting.max_logged_body_bytes)
                        if setting.enable_payload_logging
                        else None
                    )
                    trace.extend(fallback_trace)
                    trace.append(ProxyService._build_trace_item(provider, provider_model, "success", latency_ms, status_code=200))
                    await ProxyService._run_db_write(
                        ProxyService._create_success_log_with_provider_status,
                        log_type=log_type,
                        trace_id=trace_id,
                        provider_id=provider.id,
                        provider_name=provider.name,
                        model_name=model_name,
                        requested_model=model_name,
                        tenant_name=api_client_auth.api_client_key.tenant_name if api_client_auth else None,
                        project_name=api_client_auth.api_client_key.project_name if api_client_auth else None,
                        app_name=api_client_auth.api_client_key.app_name if api_client_auth else None,
                        environment_name=api_client_auth.api_client_key.environment_name if api_client_auth else None,
                        request_id=request_id,
                        conversation_key=conversation_key,
                        session_id=session_id,
                        resolved_provider_model_id=provider_model.id,
                        request_path=f"/v1{endpoint_path}",
                        source_ip=source_ip,
                        http_method="POST",
                        is_stream=False,
                        has_image=has_image,
                        success=True,
                        status_code=200,
                        latency_ms=latency_ms,
                        duration_ms=latency_ms,
                        reasoning_level=reasoning_level,
                        model_reasoning_effort=model_reasoning_effort,
                        attempt_count=attempt_count,
                        prompt_tokens=usage_info["prompt_tokens"],
                        completion_tokens=usage_info["completion_tokens"],
                        total_tokens=usage_info["total_tokens"],
                        cache_read_tokens=usage_info["cache_read_tokens"],
                        cache_write_tokens=usage_info["cache_write_tokens"],
                        finish_reason=finish_reason,
                        upstream_request_id=upstream_request_id,
                        request_body_json=request_body_json,
                        response_body_json=response_body_json,
                        response_text=response_text,
                        message=f"{log_type} success",
                        retryable=False,
                        **ProxyService._build_api_client_log_kwargs(api_client_auth, auth_result="authenticated"),
                        **ProxyService._build_provider_log_kwargs(provider_model),
                        trace=trace,
                        token_request_payload=payload,
                        token_response_payload=response,
                        token_response_text=response_text,
                        schedule_token_fill=setting.enable_token_logging,
                    )
                    return response, provider, trace, latency_ms
                except ProviderCapacityExceededError as exc:
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    trace.append(
                        ProxyService._build_trace_item(
                            provider,
                            provider_model,
                            "rate_limited",
                            latency_ms,
                            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            error=str(exc),
                        )
                    )
                    last_upstream_error = {
                        "status_code": status.HTTP_429_TOO_MANY_REQUESTS,
                        "detail": {"message": str(exc), "code": exc.code},
                    }
                    break
                except ProviderCapacityUnavailableError as exc:
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail={"message": str(exc), "code": exc.code},
                    ) from exc
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
                    await ProxyService._mark_failure_async(
                        provider,
                        provider_model,
                        latency_ms,
                        error_body,
                        db=db,
                        force_unhealthy=ProxyService._should_mark_provider_model_unhealthy(
                            status_code=exc.response.status_code,
                            detail=ProxyService._normalize_error_detail(error_body),
                        ),
                    )
                    if exc.response.status_code not in {401, 403, 404, 429} and exc.response.status_code < 500:
                        break
                except RequestsUpstreamHTTPError as exc:
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    error_body = ProxyService._error_message_for_log(exc.detail)
                    trace.append(
                        ProxyService._build_trace_item(
                            provider,
                            provider_model,
                            ProxyService._classify_http_error(exc.status_code),
                            latency_ms,
                            status_code=exc.status_code,
                            error=error_body,
                        )
                    )
                    last_upstream_error = {
                        "status_code": exc.status_code,
                        "detail": exc.detail,
                    }
                    await ProxyService._mark_failure_async(
                        provider,
                        provider_model,
                        latency_ms,
                        error_body,
                        db=db,
                        force_unhealthy=ProxyService._should_mark_provider_model_unhealthy(
                            status_code=exc.status_code,
                            detail=exc.detail,
                        ),
                    )
                    if exc.status_code not in {401, 403, 404, 429} and exc.status_code < 500:
                        break
                except NonStreamResponseTooLarge as exc:
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    trace.append(
                        ProxyService._build_trace_item(
                            provider,
                            provider_model,
                            "non_stream_response_too_large",
                            latency_ms,
                            status_code=exc.status_code,
                            error=exc.detail.get("message"),
                        )
                    )
                    last_upstream_error = {
                        "status_code": exc.status_code,
                        "detail": exc.detail,
                    }
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
                    await ProxyService._mark_failure_async(provider, provider_model, latency_ms, str(exc), db=db)

        await ProxyService._raise_final_error_async(
            db,
            model_name=model_name,
            endpoint_path=endpoint_path,
            log_type=log_type,
            trace=trace,
            upstream_error=last_upstream_error,
            requested_model=model_name,
            request_id=request_id,
            conversation_key=conversation_key,
            session_id=session_id,
            resolved_provider_model_id=None,
            is_stream=False,
            has_image=has_image,
            request_body_json=request_body_json,
            request_payload=payload,
            schedule_token_fill=setting.enable_token_logging,
            reasoning_level=reasoning_level,
            attempt_count=attempt_count,
            model_reasoning_effort=model_reasoning_effort,
            api_client_auth=api_client_auth,
            trace_id=trace_id,
            source_ip=source_ip,
        )

    @staticmethod
    async def forward_stream_request(
        db: Session | None = None,
        *,
        endpoint_path: str,
        payload: dict[str, Any],
        log_type: str,
        forced_provider_id: int | None = None,
        route_context: RoutePolicyContext | None = None,
        api_client_auth: ApiClientAuthContext | None = None,
        trace_id: str | None = None,
        source_ip: str | None = None,
    ) -> tuple[AsyncIterator[bytes], Provider, list[dict], int]:
        payload = ProxyService._normalize_reasoning_request_payload(endpoint_path=endpoint_path, payload=payload)
        model_name = payload.get("model")
        has_image = ProxyService._payload_has_image(payload)
        require_tools = ProxyService._payload_uses_tools(payload)
        setting = await ProxyService._get_setting_async(db)
        request_id = uuid4().hex
        reasoning_level = LogService.extract_reasoning_level(payload)
        model_reasoning_effort = LogService.extract_model_reasoning_effort(payload)
        token_limit_error = ProxyService._build_request_token_limit_error(
            setting=setting,
            payload=payload,
            model_name=model_name,
            endpoint_path=endpoint_path,
        )
        if token_limit_error is not None:
            reject_status_code, reject_detail = token_limit_error
            await ProxyService._reject_by_request_token_limit_async(
                db,
                status_code=reject_status_code,
                detail=reject_detail,
                model_name=model_name,
                endpoint_path=endpoint_path,
                log_type=log_type,
                request_id=request_id,
                is_stream=True,
                has_image=has_image,
                reasoning_level=reasoning_level,
                model_reasoning_effort=model_reasoning_effort,
                api_client_auth=api_client_auth,
                trace_id=trace_id,
                source_ip=source_ip,
            )
        long_output_error = ProxyService._build_long_output_error(
            setting=setting,
            payload=payload,
            model_name=model_name,
            endpoint_path=endpoint_path,
            is_stream=True,
        )
        if long_output_error is not None:
            reject_status_code, reject_detail = long_output_error
            await ProxyService._reject_by_request_token_limit_async(
                db,
                status_code=reject_status_code,
                detail=reject_detail,
                model_name=model_name,
                endpoint_path=endpoint_path,
                log_type=log_type,
                request_id=request_id,
                is_stream=True,
                has_image=has_image,
                reasoning_level=reasoning_level,
                model_reasoning_effort=model_reasoning_effort,
                api_client_auth=api_client_auth,
                trace_id=trace_id,
                source_ip=source_ip,
            )
        conversation_key = ProxyService._extract_conversation_key(payload, request_id)
        session_id = LogService.extract_session_id(payload, conversation_key=conversation_key, fallback=request_id)
        request_body_json = ProxyService._serialize_payload_for_logging(
            payload,
            setting=setting,
            preserve_request_content_when_disabled=True,
        )
        if api_client_auth is not None and not ApiKeyService.is_model_allowed(api_client_auth.api_client_key, model_name):
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"message": "Requested model is not allowed for this api key", "code": "model_not_allowed"},
            )
        try:
            candidates = await RouterService.async_order_candidates(
                db,
                model_name=model_name,
                sticky_key=ProxyService._extract_sticky_key(payload),
                forced_provider_id=forced_provider_id,
                route_context=route_context,
                require_vision=has_image,
                require_stream=True,
                require_tools=require_tools,
                require_chat_completions=endpoint_path == "/chat/completions",
                require_responses=endpoint_path == "/responses",
            )
        except ProviderCapacityUnavailableError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"message": str(exc), "code": exc.code},
            ) from exc
        if not candidates:
            await ProxyService._run_db_write(
                LogService.create_log,
                log_type=log_type,
                trace_id=trace_id,
                model_name=model_name,
                requested_model=model_name,
                tenant_name=api_client_auth.api_client_key.tenant_name if api_client_auth else None,
                project_name=api_client_auth.api_client_key.project_name if api_client_auth else None,
                app_name=api_client_auth.api_client_key.app_name if api_client_auth else None,
                environment_name=api_client_auth.api_client_key.environment_name if api_client_auth else None,
                request_id=request_id,
                conversation_key=conversation_key,
                session_id=session_id,
                request_path=f"/v1{endpoint_path}",
                source_ip=source_ip,
                http_method="POST",
                is_stream=True,
                has_image=has_image,
                success=False,
                status_code=status.HTTP_404_NOT_FOUND,
                reasoning_level=reasoning_level,
                model_reasoning_effort=model_reasoning_effort,
                request_body_json=request_body_json,
                message="No native tool-capable provider for requested model" if require_tools else "No available provider for requested model",
                error_type="invalid_request_error",
                error_code="model_tools_not_available" if require_tools else "model_not_available",
                retryable=False,
                **ProxyService._build_api_client_log_kwargs(api_client_auth, auth_result="authenticated"),
                trace=[],
                attempt_count=0,
                token_request_payload=payload,
                schedule_token_fill=setting.enable_token_logging,
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail={
                    "message": "No native tool-capable provider for requested model" if require_tools else "No available provider for requested model",
                    "code": "model_tools_not_available" if require_tools else "model_not_available",
                    "requires_tools": require_tools,
                },
            )

        trace: list[dict] = []
        last_upstream_error: dict[str, Any] | None = None
        attempt_count = 0

        for candidate in candidates:
            provider = candidate.provider
            provider_model = candidate.provider_model
            retries = max(1, min(provider.max_retries, setting.global_max_retries))
            for _ in range(retries):
                attempt_count += 1
                started = time.perf_counter()
                stream_context = None
                capacity_lease = None
                capacity_lease_entered = False
                trace.append(ProxyService._build_trace_item(provider, provider_model, "connecting", 0))
                try:
                    capacity_lease = ProviderCapacityService.async_lease(provider, is_stream=True)
                    await capacity_lease.__aenter__()
                    capacity_lease_entered = True
                    response, prepared, stream_context, fallback_trace = await ProxyService._open_stream_with_endpoint_fallback(
                        provider,
                        provider_model,
                        endpoint_path,
                        payload,
                        started=started,
                        stream_connect_timeout_seconds=setting.stream_connect_timeout_seconds,
                    )
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    upstream_request_id = ProxyService._extract_upstream_request_id(response)
                    trace.extend(fallback_trace)
                    trace.append(ProxyService._build_trace_item(provider, provider_model, "stream_opened", latency_ms, status_code=200))

                    async def stream_generator() -> AsyncIterator[bytes]:
                        success = False
                        interrupted = False
                        client_cancelled = False
                        error_message: str | None = None
                        exc_type = None
                        exc_value = None
                        exc_traceback = None
                        error_code = "stream_interrupted"
                        first_chunk_latency_ms: int | None = None
                        usage_info = {
                            "prompt_tokens": None,
                            "completion_tokens": None,
                            "total_tokens": None,
                            "cache_read_tokens": None,
                            "cache_write_tokens": None,
                        }
                        finish_reason: str | None = None
                        response_text_parts: list[str] = []
                        response_text_bytes = 0
                        token_response_parts: list[str] = []
                        token_response_bytes = 0
                        event_buffer = bytearray()
                        downstream_transform_state = ProxyService._create_responses_stream_state(payload=payload)
                        chat_stream_transform_state = ProxyService._create_chat_stream_state(payload=payload)
                        timeout_policy = ProxyService._build_stream_timeout_policy(provider=provider, setting=setting)
                        stream_started = time.perf_counter()
                        try:
                            chunk_iterator = response.aiter_bytes().__aiter__()
                            while True:
                                try:
                                    upstream_chunk = await ProxyService._read_next_stream_chunk(
                                        chunk_iterator,
                                        first_chunk_latency_ms=first_chunk_latency_ms,
                                        stream_started=stream_started,
                                        timeout_policy=timeout_policy,
                                    )
                                except StopAsyncIteration:
                                    break
                                if upstream_chunk:
                                    if setting.enable_stream_response_persist or setting.enable_token_logging:
                                        (
                                            response_text_bytes,
                                            token_response_bytes,
                                            finish_reason,
                                            usage_info,
                                        ) = ProxyService._collect_stream_log_data(
                                            chunk=upstream_chunk,
                                            event_buffer=event_buffer,
                                            response_text_parts=response_text_parts,
                                            response_text_bytes=response_text_bytes,
                                            token_response_parts=token_response_parts if setting.enable_token_logging else None,
                                            token_response_bytes=token_response_bytes,
                                            finish_reason=finish_reason,
                                            usage_info=usage_info,
                                            capture_text=setting.enable_stream_response_persist,
                                            capture_usage=setting.enable_token_logging,
                                            limit_bytes=setting.max_logged_body_bytes,
                                            token_limit_bytes=getattr(setting, "stream_token_capture_max_bytes", 1048576),
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
                                    if prepared.adapt_chat_response_to_responses:
                                        for downstream_chunk in ProxyService._adapt_chat_stream_chunk_to_responses_events(
                                            upstream_chunk,
                                            state=downstream_transform_state,
                                            requested_model=str(payload.get("model") or ""),
                                        ):
                                            yield downstream_chunk
                                    elif prepared.adapt_responses_response_to_chat:
                                        for downstream_chunk in ProxyService._adapt_responses_stream_chunk_to_chat_events(
                                            upstream_chunk,
                                            state=chat_stream_transform_state,
                                            requested_model=str(payload.get("model") or ""),
                                        ):
                                            yield downstream_chunk
                                    else:
                                        yield upstream_chunk
                            if not success:
                                interrupted = True
                                error_message = "upstream stream ended without any response data"
                                error_code = "upstream_stream_empty"
                                yield ProxyService._format_stream_error_event(
                                    message=error_message,
                                    code=error_code,
                                    trace_id=trace_id,
                                )
                                yield b"data: [DONE]\n\n"
                                return
                            if prepared.adapt_chat_response_to_responses:
                                for downstream_chunk in ProxyService._build_responses_stream_completion_events(
                                    downstream_transform_state
                                ):
                                    yield downstream_chunk
                            elif prepared.adapt_responses_response_to_chat:
                                for downstream_chunk in ProxyService._build_chat_stream_completion_events(
                                    chat_stream_transform_state
                                ):
                                    yield downstream_chunk
                        except asyncio.CancelledError as exc:
                            interrupted = True
                            client_cancelled = True
                            error_message = "client cancelled stream"
                            error_code = "client_cancelled"
                            exc_type = type(exc)
                            exc_value = exc
                            exc_traceback = exc.__traceback__
                            raise
                        except StreamTimeoutError as exc:
                            interrupted = True
                            error_message = str(exc)
                            error_code = exc.code
                            exc_type = type(exc)
                            exc_value = exc
                            exc_traceback = exc.__traceback__
                            raise
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
                            await capacity_lease.__aexit__(exc_type, exc_value, exc_traceback)
                            log_db = SessionLocal()
                            try:
                                log_provider = log_db.get(Provider, provider.id)
                                log_provider_model = log_db.get(ProviderModel, provider_model.id)
                                if success and not interrupted:
                                    if log_provider is not None and log_provider_model is not None:
                                        ProxyService._mark_success(log_db, log_provider, log_provider_model, latency_ms)
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
                                        log_db,
                                        log_type=log_type,
                                        trace_id=trace_id,
                                        provider_id=provider.id,
                                        provider_name=provider.name,
                                        model_name=model_name,
                                        requested_model=model_name,
                                        tenant_name=api_client_auth.api_client_key.tenant_name if api_client_auth else None,
                                        project_name=api_client_auth.api_client_key.project_name if api_client_auth else None,
                                        app_name=api_client_auth.api_client_key.app_name if api_client_auth else None,
                                        environment_name=api_client_auth.api_client_key.environment_name if api_client_auth else None,
                                        request_id=request_id,
                                        conversation_key=conversation_key,
                                        session_id=session_id,
                                        resolved_provider_model_id=provider_model.id,
                                        request_path=f"/v1{endpoint_path}",
                                        source_ip=source_ip,
                                        http_method="POST",
                                        is_stream=True,
                                        has_image=has_image,
                                        success=True,
                                        status_code=200,
                                        latency_ms=latency_ms,
                                        first_token_latency_ms=first_chunk_latency_ms,
                                        ttfb_ms=first_chunk_latency_ms,
                                        duration_ms=total_duration_ms,
                                        reasoning_level=reasoning_level,
                                        model_reasoning_effort=model_reasoning_effort,
                                        attempt_count=attempt_count,
                                        prompt_tokens=usage_info["prompt_tokens"],
                                        completion_tokens=usage_info["completion_tokens"],
                                        total_tokens=usage_info["total_tokens"],
                                        cache_read_tokens=usage_info["cache_read_tokens"],
                                        cache_write_tokens=usage_info["cache_write_tokens"],
                                        finish_reason=finish_reason,
                                        upstream_request_id=upstream_request_id,
                                        request_body_json=request_body_json,
                                        response_text=ProxyService._finalize_text_capture(response_text_parts),
                                        message=f"stream {log_type} success",
                                        retryable=False,
                                        **ProxyService._build_api_client_log_kwargs(api_client_auth, auth_result="authenticated"),
                                        **ProxyService._build_provider_log_kwargs(provider_model),
                                        trace=final_trace,
                                        token_request_payload=payload,
                                        token_response_text=ProxyService._finalize_text_capture(token_response_parts),
                                        schedule_token_fill=setting.enable_token_logging,
                                        auto_commit=False,
                                    )
                                    log_db.commit()
                                else:
                                    terminal_result = "client_cancelled" if client_cancelled else ("interrupted" if interrupted else "finished")
                                    terminal_status_code = 499 if client_cancelled else (502 if interrupted or not success else 200)
                                    if not client_cancelled and log_provider is not None and log_provider_model is not None:
                                        ProxyService._mark_failure(
                                            log_db,
                                            log_provider,
                                            log_provider_model,
                                            latency_ms,
                                            error_message or f"stream {log_type} failed",
                                            force_unhealthy=ProxyService._should_mark_provider_model_unhealthy(
                                                status_code=terminal_status_code,
                                                detail={"message": error_message or f"stream {log_type} failed"},
                                            ),
                                        )
                                    interrupted_trace = trace + [
                                        ProxyService._build_trace_item(
                                            provider,
                                            provider_model,
                                            terminal_result,
                                            total_duration_ms,
                                            status_code=terminal_status_code,
                                            first_token_latency_ms=first_chunk_latency_ms,
                                            total_duration_ms=total_duration_ms,
                                            error=error_message,
                                        )
                                    ]
                                    LogService.create_log(
                                        log_db,
                                        log_type=log_type,
                                        trace_id=trace_id,
                                        provider_id=provider.id,
                                        provider_name=provider.name,
                                        model_name=model_name,
                                        requested_model=model_name,
                                        tenant_name=api_client_auth.api_client_key.tenant_name if api_client_auth else None,
                                        project_name=api_client_auth.api_client_key.project_name if api_client_auth else None,
                                        app_name=api_client_auth.api_client_key.app_name if api_client_auth else None,
                                        environment_name=api_client_auth.api_client_key.environment_name if api_client_auth else None,
                                        request_id=request_id,
                                        conversation_key=conversation_key,
                                        session_id=session_id,
                                        resolved_provider_model_id=provider_model.id,
                                        request_path=f"/v1{endpoint_path}",
                                        source_ip=source_ip,
                                        http_method="POST",
                                        is_stream=True,
                                        has_image=has_image,
                                        success=False,
                                        status_code=terminal_status_code,
                                        latency_ms=latency_ms,
                                        first_token_latency_ms=first_chunk_latency_ms,
                                        ttfb_ms=first_chunk_latency_ms,
                                        duration_ms=total_duration_ms,
                                        reasoning_level=reasoning_level,
                                        model_reasoning_effort=model_reasoning_effort,
                                        attempt_count=attempt_count,
                                        prompt_tokens=usage_info["prompt_tokens"],
                                        completion_tokens=usage_info["completion_tokens"],
                                        total_tokens=usage_info["total_tokens"],
                                        cache_read_tokens=usage_info["cache_read_tokens"],
                                        cache_write_tokens=usage_info["cache_write_tokens"],
                                        finish_reason=finish_reason,
                                        upstream_request_id=upstream_request_id,
                                        request_body_json=request_body_json,
                                        response_text=ProxyService._finalize_text_capture(response_text_parts),
                                        message=error_message or f"stream {log_type} failed",
                                        error_type="server_error" if not client_cancelled else "client_error",
                                        error_code=error_code,
                                        retryable=not client_cancelled,
                                        **ProxyService._build_api_client_log_kwargs(api_client_auth, auth_result="authenticated"),
                                        **ProxyService._build_provider_log_kwargs(provider_model),
                                        trace=interrupted_trace,
                                        token_request_payload=payload,
                                        token_response_text=ProxyService._finalize_text_capture(token_response_parts),
                                        schedule_token_fill=setting.enable_token_logging,
                                        auto_commit=False,
                                    )
                                    log_db.commit()
                            finally:
                                log_db.close()

                    return stream_generator(), provider, trace, latency_ms
                except ProviderCapacityExceededError as exc:
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    trace.append(
                        ProxyService._build_trace_item(
                            provider,
                            provider_model,
                            "rate_limited",
                            latency_ms,
                            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            error=str(exc),
                        )
                    )
                    last_upstream_error = {
                        "status_code": status.HTTP_429_TOO_MANY_REQUESTS,
                        "detail": {"message": str(exc), "code": exc.code},
                    }
                    break
                except ProviderCapacityUnavailableError as exc:
                    if stream_context is not None:
                        await stream_context.__aexit__(type(exc), exc, exc.__traceback__)
                    if capacity_lease is not None and capacity_lease_entered:
                        await capacity_lease.__aexit__(type(exc), exc, exc.__traceback__)
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail={"message": str(exc), "code": exc.code},
                    ) from exc
                except httpx.HTTPStatusError as exc:
                    if stream_context is not None:
                        await stream_context.__aexit__(type(exc), exc, exc.__traceback__)
                    if capacity_lease is not None and capacity_lease_entered:
                        await capacity_lease.__aexit__(type(exc), exc, exc.__traceback__)
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
                    await ProxyService._mark_failure_async(
                        provider,
                        provider_model,
                        latency_ms,
                        error_body,
                        db=db,
                        force_unhealthy=ProxyService._should_mark_provider_model_unhealthy(
                            status_code=exc.response.status_code,
                            detail=ProxyService._normalize_error_detail(error_body),
                        ),
                    )
                    if exc.response.status_code not in {401, 403, 404, 429} and exc.response.status_code < 500:
                        break
                except Exception as exc:
                    if stream_context is not None:
                        await stream_context.__aexit__(type(exc), exc, exc.__traceback__)
                    if capacity_lease is not None and capacity_lease_entered:
                        await capacity_lease.__aexit__(type(exc), exc, exc.__traceback__)
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
                    await ProxyService._mark_failure_async(provider, provider_model, latency_ms, str(exc), db=db)

        await ProxyService._raise_final_error_async(
            db,
            model_name=model_name,
            endpoint_path=endpoint_path,
            log_type=log_type,
            trace=trace,
            upstream_error=last_upstream_error,
            requested_model=model_name,
            request_id=request_id,
            conversation_key=conversation_key,
            session_id=session_id,
            resolved_provider_model_id=None,
            is_stream=True,
            has_image=has_image,
            request_body_json=request_body_json,
            request_payload=payload,
            schedule_token_fill=setting.enable_token_logging,
            reasoning_level=reasoning_level,
            attempt_count=attempt_count,
            model_reasoning_effort=model_reasoning_effort,
            api_client_auth=api_client_auth,
            trace_id=trace_id,
            source_ip=source_ip,
        )

    @staticmethod
    async def _forward_json(provider: Provider, endpoint_path: str, payload: dict[str, Any]) -> tuple[dict[str, Any], str | None]:
        headers = {"Authorization": f"Bearer {provider.api_key}"}
        prepared = ProxyService._prepare_upstream_request(provider, endpoint_path=endpoint_path, payload=payload)
        setting = await ProxyService._get_setting_async()
        return await ProxyService._send_prepared_json(provider, prepared=prepared, headers=headers, requested_payload=payload, setting=setting)

    @staticmethod
    async def _forward_json_with_endpoint_fallback(
        provider: Provider,
        provider_model: ProviderModel,
        endpoint_path: str,
        payload: dict[str, Any],
        *,
        started: float,
        setting: Any,
    ) -> tuple[dict[str, Any], str | None, list[dict]]:
        headers = {"Authorization": f"Bearer {provider.api_key}"}
        prepared = ProxyService._prepare_upstream_request(provider, endpoint_path=endpoint_path, payload=payload)
        try:
            response_json, upstream_request_id = await ProxyService._send_prepared_json(
                provider,
                prepared=prepared,
                headers=headers,
                requested_payload=payload,
                setting=setting,
            )
            return response_json, upstream_request_id, []
        except httpx.HTTPStatusError as exc:
            primary_error_body = await ProxyService._extract_response_error(exc.response)
            if not ProxyService._should_try_endpoint_fallback(
                provider,
                endpoint_path=prepared.request_path,
                status_code=exc.response.status_code,
                error_detail=primary_error_body,
            ):
                raise
            fallback_prepared = ProxyService._build_endpoint_fallback_request(
                requested_endpoint_path=endpoint_path,
                failed_request_path=prepared.request_path,
                payload=payload,
                primary_error=primary_error_body,
            )
            fallback_trace = [
                ProxyService._build_trace_item(
                    provider,
                    provider_model,
                    "endpoint_fallback",
                    int((time.perf_counter() - started) * 1000),
                    status_code=exc.response.status_code,
                    error=primary_error_body,
                    extra={
                        "from_endpoint": prepared.request_path,
                        "to_endpoint": fallback_prepared.request_path,
                    },
                )
            ]
            response_json, upstream_request_id = await ProxyService._send_prepared_json(
                provider,
                prepared=fallback_prepared,
                headers=headers,
                requested_payload=payload,
                setting=setting,
            )
            fallback_trace.append(
                ProxyService._build_trace_item(
                    provider,
                    provider_model,
                    "endpoint_fallback_success",
                    int((time.perf_counter() - started) * 1000),
                    status_code=200,
                    extra={
                        "from_endpoint": prepared.request_path,
                        "to_endpoint": fallback_prepared.request_path,
                    },
                )
            )
            return response_json, upstream_request_id, fallback_trace
        except RequestsUpstreamHTTPError as exc:
            if not ProxyService._should_try_endpoint_fallback(
                provider,
                endpoint_path=prepared.request_path,
                status_code=exc.status_code,
                error_detail=exc.detail,
            ):
                raise
            fallback_prepared = ProxyService._build_endpoint_fallback_request(
                requested_endpoint_path=endpoint_path,
                failed_request_path=prepared.request_path,
                payload=payload,
                primary_error=exc.detail,
            )
            fallback_trace = [
                ProxyService._build_trace_item(
                    provider,
                    provider_model,
                    "endpoint_fallback",
                    int((time.perf_counter() - started) * 1000),
                    status_code=exc.status_code,
                    error=ProxyService._error_message_for_log(exc.detail),
                    extra={
                        "from_endpoint": prepared.request_path,
                        "to_endpoint": fallback_prepared.request_path,
                    },
                )
            ]
            response_json, upstream_request_id = await ProxyService._send_prepared_json(
                provider,
                prepared=fallback_prepared,
                headers=headers,
                requested_payload=payload,
                setting=setting,
            )
            fallback_trace.append(
                ProxyService._build_trace_item(
                    provider,
                    provider_model,
                    "endpoint_fallback_success",
                    int((time.perf_counter() - started) * 1000),
                    status_code=200,
                    extra={
                        "from_endpoint": prepared.request_path,
                        "to_endpoint": fallback_prepared.request_path,
                    },
                )
            )
            return response_json, upstream_request_id, fallback_trace

    @staticmethod
    async def _send_prepared_json(
        provider: Provider,
        *,
        prepared: PreparedUpstreamRequest,
        headers: dict[str, str],
        requested_payload: dict[str, Any],
        setting: Any,
    ) -> tuple[dict[str, Any], str | None]:
        if ProxyService._payload_has_image(prepared.request_payload):
            return await ProxyService._forward_json_image_request(provider, prepared=prepared, headers=headers, setting=setting)
        client = ProxyService._select_upstream_client(payload=prepared.request_payload)
        response = await ProxyService._post_json_with_response_size_limit(
            client,
            f"{provider.base_url}{prepared.request_path}",
            headers=headers,
            json=prepared.request_payload,
            timeout=ProxyService._build_httpx_timeout(provider, payload=prepared.request_payload, is_stream=False),
            max_response_bytes=int(getattr(setting, "max_non_stream_response_body_bytes", 20971520) or 0),
        )
        response.raise_for_status()
        response_json = response.json()
        if prepared.adapt_chat_response_to_responses:
            ProxyService._assert_chat_response_adapter_safe(response_json)
            response_json = ProxyService._convert_chat_completion_to_responses_payload(
                response_json,
                requested_model=str(requested_payload.get("model") or response_json.get("model") or ""),
            )
        if prepared.adapt_responses_response_to_chat:
            ProxyService._assert_responses_response_adapter_safe(response_json)
            response_json = ProxyService._convert_responses_payload_to_chat_completion(
                response_json,
                requested_model=str(requested_payload.get("model") or response_json.get("model") or ""),
            )
        return response_json, ProxyService._extract_upstream_request_id(response)

    @staticmethod
    async def _forward_json_image_request(
        provider: Provider,
        *,
        prepared: PreparedUpstreamRequest,
        headers: dict[str, str],
        setting: Any,
    ) -> tuple[dict[str, Any], str | None]:
        client = ProxyService._select_upstream_client(payload=prepared.request_payload)
        response = await ProxyService._post_json_with_response_size_limit(
            client,
            f"{provider.base_url}{prepared.request_path}",
            headers=headers,
            json=prepared.request_payload,
            timeout=ProxyService._build_httpx_timeout(provider, payload=prepared.request_payload, is_stream=False),
            max_response_bytes=int(getattr(setting, "max_non_stream_response_body_bytes", 20971520) or 0),
        )
        response.raise_for_status()
        response_json = response.json()
        if prepared.adapt_chat_response_to_responses:
            ProxyService._assert_chat_response_adapter_safe(response_json)
            response_json = ProxyService._convert_chat_completion_to_responses_payload(
                response_json,
                requested_model=str(prepared.request_payload.get("model") or response_json.get("model") or ""),
            )
        if prepared.adapt_responses_response_to_chat:
            ProxyService._assert_responses_response_adapter_safe(response_json)
            response_json = ProxyService._convert_responses_payload_to_chat_completion(
                response_json,
                requested_model=str(prepared.request_payload.get("model") or response_json.get("model") or ""),
            )
        return response_json, ProxyService._extract_upstream_request_id(response)

    @staticmethod
    async def _post_json_with_response_size_limit(
        client: httpx.AsyncClient,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, Any],
        timeout: httpx.Timeout,
        max_response_bytes: int,
    ) -> httpx.Response:
        async with client.stream("POST", url, headers=headers, json=json, timeout=timeout) as response:
            content = await ProxyService._read_limited_httpx_response(
                response,
                max_response_bytes=max_response_bytes,
            )
            response._content = content
            return response

    @staticmethod
    async def _read_limited_httpx_response(response: httpx.Response, *, max_response_bytes: int) -> bytes:
        body = bytearray()
        async for chunk in response.aiter_bytes():
            if not chunk:
                continue
            if max_response_bytes > 0 and len(body) + len(chunk) > max_response_bytes:
                raise NonStreamResponseTooLarge(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail={
                        "message": f"非流式上游响应超过应用层上限 {max_response_bytes} 字节，请使用 stream=true",
                        "code": "non_stream_response_too_large",
                        "max_non_stream_response_body_bytes": max_response_bytes,
                    },
                )
            body.extend(chunk)
        return bytes(body)

    @staticmethod
    @asynccontextmanager
    async def _stream_request(
        provider: Provider,
        endpoint_path: str,
        payload: dict[str, Any],
        *,
        stream_connect_timeout_seconds: int | None = None,
    ) -> AsyncIterator[tuple[httpx.Response, PreparedUpstreamRequest]]:
        headers = {"Authorization": f"Bearer {provider.api_key}"}
        client = ProxyService._select_upstream_client(payload=payload)
        prepared = ProxyService._prepare_upstream_request(provider, endpoint_path=endpoint_path, payload=payload)
        async with ProxyService._stream_prepared_request(
            provider,
            prepared=prepared,
            headers=headers,
            stream_connect_timeout_seconds=stream_connect_timeout_seconds,
        ) as item:
            yield item

    @staticmethod
    @asynccontextmanager
    async def _stream_prepared_request(
        provider: Provider,
        *,
        prepared: PreparedUpstreamRequest,
        headers: dict[str, str],
        stream_connect_timeout_seconds: int | None = None,
    ) -> AsyncIterator[tuple[httpx.Response, PreparedUpstreamRequest]]:
        client = ProxyService._select_upstream_client(payload=prepared.request_payload)
        async with client.stream(
            "POST",
            f"{provider.base_url}{prepared.request_path}",
            headers=headers,
            json=prepared.request_payload,
            timeout=ProxyService._build_httpx_timeout(
                provider,
                payload=prepared.request_payload,
                is_stream=True,
                stream_connect_timeout_seconds=stream_connect_timeout_seconds,
            ),
        ) as response:
            yield response, prepared

    @staticmethod
    async def _open_stream_with_endpoint_fallback(
        provider: Provider,
        provider_model: ProviderModel,
        endpoint_path: str,
        payload: dict[str, Any],
        *,
        started: float,
        stream_connect_timeout_seconds: int | None = None,
    ) -> tuple[httpx.Response, PreparedUpstreamRequest, Any, list[dict]]:
        headers = {"Authorization": f"Bearer {provider.api_key}"}
        prepared = ProxyService._prepare_upstream_request(provider, endpoint_path=endpoint_path, payload=payload)
        stream_context = ProxyService._stream_prepared_request(
            provider,
            prepared=prepared,
            headers=headers,
            stream_connect_timeout_seconds=stream_connect_timeout_seconds,
        )
        try:
            response, opened_prepared = await stream_context.__aenter__()
            response.raise_for_status()
            return response, opened_prepared, stream_context, []
        except httpx.HTTPStatusError as exc:
            error_body = await ProxyService._extract_response_error(exc.response)
            await stream_context.__aexit__(type(exc), exc, exc.__traceback__)
            if not ProxyService._should_try_endpoint_fallback(
                provider,
                endpoint_path=prepared.request_path,
                status_code=exc.response.status_code,
                error_detail=error_body,
            ):
                raise
            fallback_prepared = ProxyService._build_endpoint_fallback_request(
                requested_endpoint_path=endpoint_path,
                failed_request_path=prepared.request_path,
                payload=payload,
                primary_error=error_body,
            )
            fallback_trace = [
                ProxyService._build_trace_item(
                    provider,
                    provider_model,
                    "endpoint_fallback",
                    int((time.perf_counter() - started) * 1000),
                    status_code=exc.response.status_code,
                    error=error_body,
                    extra={
                        "from_endpoint": prepared.request_path,
                        "to_endpoint": fallback_prepared.request_path,
                    },
                )
            ]
            fallback_context = ProxyService._stream_prepared_request(
                provider,
                prepared=fallback_prepared,
                headers=headers,
                stream_connect_timeout_seconds=stream_connect_timeout_seconds,
            )
            try:
                fallback_response, opened_fallback_prepared = await fallback_context.__aenter__()
                fallback_response.raise_for_status()
            except Exception as fallback_exc:
                await fallback_context.__aexit__(type(fallback_exc), fallback_exc, fallback_exc.__traceback__)
                raise
            fallback_trace.append(
                ProxyService._build_trace_item(
                    provider,
                    provider_model,
                    "endpoint_fallback_success",
                    int((time.perf_counter() - started) * 1000),
                    status_code=200,
                    extra={
                        "from_endpoint": prepared.request_path,
                        "to_endpoint": fallback_prepared.request_path,
                    },
                )
            )
            return fallback_response, opened_fallback_prepared, fallback_context, fallback_trace
        except Exception as exc:
            await stream_context.__aexit__(type(exc), exc, exc.__traceback__)
            raise

    @staticmethod
    def _select_upstream_client(*, payload: dict[str, Any]) -> httpx.AsyncClient:
        if ProxyService._payload_has_image(payload):
            return UpstreamClientService.get_http1_client()
        return UpstreamClientService.get_client()

    @staticmethod
    def _build_httpx_timeout(
        provider: Provider,
        *,
        payload: dict[str, Any],
        is_stream: bool,
        stream_connect_timeout_seconds: int | None = None,
    ) -> httpx.Timeout:
        settings = get_settings()
        base_timeout_seconds = max(provider.timeout_ms / 1000, 1.0)
        connect_timeout: float | None = base_timeout_seconds
        read_timeout: float | None
        if is_stream:
            configured_connect_timeout = (
                stream_connect_timeout_seconds
                if stream_connect_timeout_seconds is not None
                else settings.stream_connect_timeout_seconds
            )
            connect_timeout = (
                max(float(configured_connect_timeout), 1.0)
                if configured_connect_timeout > 0
                else None
            )
            read_timeout = None
        elif ProxyService._payload_has_image(payload):
            read_timeout = max(base_timeout_seconds, 180.0)
        else:
            read_timeout = base_timeout_seconds
        return httpx.Timeout(
            connect=connect_timeout,
            write=base_timeout_seconds,
            read=read_timeout,
            pool=settings.upstream_pool_timeout_s,
        )

    @staticmethod
    def _build_stream_timeout_policy(*, provider: Provider, setting: Any) -> StreamTimeoutPolicy:
        provider_first_token_timeout = int(provider.first_token_timeout_sec or 0)
        setting_first_token_timeout = int(getattr(setting, "stream_first_token_timeout_seconds", 0) or 0)
        first_token_timeout = (
            provider_first_token_timeout
            if provider_first_token_timeout > 0
            else setting_first_token_timeout
        )
        return StreamTimeoutPolicy(
            first_token_timeout_seconds=max(0, first_token_timeout),
            idle_timeout_seconds=max(0, int(getattr(setting, "stream_idle_timeout_seconds", 0) or 0)),
            max_duration_seconds=max(0, int(getattr(setting, "stream_max_duration_seconds", 0) or 0)),
        )

    @staticmethod
    async def _read_next_stream_chunk(
        chunk_iterator: AsyncIterator[bytes],
        *,
        first_chunk_latency_ms: int | None,
        stream_started: float,
        timeout_policy: StreamTimeoutPolicy,
    ) -> bytes:
        timeout_seconds, timeout_code, timeout_message = ProxyService._next_stream_read_timeout(
            first_chunk_latency_ms=first_chunk_latency_ms,
            stream_started=stream_started,
            timeout_policy=timeout_policy,
        )
        try:
            if timeout_seconds is None:
                return await chunk_iterator.__anext__()
            return await asyncio.wait_for(chunk_iterator.__anext__(), timeout=timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise StreamTimeoutError(code=timeout_code, message=timeout_message) from exc

    @staticmethod
    def _next_stream_read_timeout(
        *,
        first_chunk_latency_ms: int | None,
        stream_started: float,
        timeout_policy: StreamTimeoutPolicy,
    ) -> tuple[float | None, str, str]:
        max_duration_remaining: float | None = None
        if timeout_policy.max_duration_seconds > 0:
            elapsed = time.perf_counter() - stream_started
            max_duration_remaining = timeout_policy.max_duration_seconds - elapsed
            if max_duration_remaining <= 0:
                raise StreamTimeoutError(
                    code="stream_max_duration_exceeded",
                    message="stream exceeded maximum duration",
                )

        if first_chunk_latency_ms is None:
            chunk_timeout = timeout_policy.first_token_timeout_seconds
            timeout_code = "stream_first_token_timeout"
            timeout_message = "stream first token timeout"
        else:
            chunk_timeout = timeout_policy.idle_timeout_seconds
            timeout_code = "stream_idle_timeout"
            timeout_message = "stream idle timeout"

        timeout_seconds = float(chunk_timeout) if chunk_timeout > 0 else None
        if max_duration_remaining is not None and (
            timeout_seconds is None or max_duration_remaining < timeout_seconds
        ):
            timeout_seconds = max_duration_remaining
            timeout_code = "stream_max_duration_exceeded"
            timeout_message = "stream exceeded maximum duration"

        return timeout_seconds, timeout_code, timeout_message

    @staticmethod
    def _normalize_reasoning_request_payload(*, endpoint_path: str, payload: dict[str, Any]) -> dict[str, Any]:
        normalized = dict(payload)
        normalized.pop("model_reasoning_effort", None)
        effort = LogService.extract_model_reasoning_effort(payload)
        if effort is None:
            return normalized
        if endpoint_path == "/responses":
            reasoning = normalized.get("reasoning")
            if isinstance(reasoning, dict):
                merged_reasoning = dict(reasoning)
                if not isinstance(merged_reasoning.get("effort"), str):
                    merged_reasoning["effort"] = effort
                normalized["reasoning"] = merged_reasoning
            else:
                normalized["reasoning"] = {"effort": effort}
            normalized.pop("reasoning_effort", None)
            return normalized
        if not isinstance(normalized.get("reasoning_effort"), str):
            normalized["reasoning_effort"] = effort
        return normalized

    @staticmethod
    def _assess_endpoint_conversion_safety(
        *,
        from_endpoint_path: str,
        to_endpoint_path: str,
        payload: dict[str, Any],
    ) -> EndpointConversionSafety:
        if from_endpoint_path == "/responses" and to_endpoint_path == "/chat/completions":
            return ProxyService._assess_responses_to_chat_conversion_safety(payload)
        if from_endpoint_path == "/chat/completions" and to_endpoint_path == "/responses":
            return ProxyService._assess_chat_to_responses_conversion_safety(payload)
        return EndpointConversionSafety(
            safe=False,
            code="unsupported_endpoint_fallback",
            message="Only /v1/chat/completions and /v1/responses endpoint fallback conversion is supported",
            unsafe_reasons=[f"unsupported conversion path: {from_endpoint_path} -> {to_endpoint_path}"],
        )

    @staticmethod
    def _assess_responses_to_chat_conversion_safety(payload: dict[str, Any]) -> EndpointConversionSafety:
        unsafe_fields = sorted(
            key
            for key in payload.keys()
            if key not in ProxyService.RESPONSES_CHAT_ADAPTER_SAFE_FIELDS
            or key in ProxyService.ENDPOINT_ADAPTER_RISKY_FIELDS
        )
        unsafe_reasons: list[str] = []
        input_value = payload.get("input")
        if not ProxyService._responses_input_is_adapter_safe(input_value, unsafe_reasons=unsafe_reasons):
            pass
        if unsafe_fields or unsafe_reasons:
            return EndpointConversionSafety(
                safe=False,
                code="endpoint_fallback_conversion_unsafe",
                message=(
                    "This /v1/responses request contains tools, reasoning, stateful context, "
                    "structured-output options, or complex multimodal content. It will not be converted to "
                    "/v1/chat/completions because the conversion may be lossy."
                ),
                unsafe_fields=unsafe_fields,
                unsafe_reasons=unsafe_reasons,
            )
        return EndpointConversionSafety(safe=True)

    @staticmethod
    def _assess_chat_to_responses_conversion_safety(payload: dict[str, Any]) -> EndpointConversionSafety:
        unsafe_fields = sorted(
            key
            for key in payload.keys()
            if key not in ProxyService.CHAT_RESPONSES_ADAPTER_SAFE_FIELDS
            or key in ProxyService.ENDPOINT_ADAPTER_RISKY_FIELDS
        )
        unsafe_reasons: list[str] = []
        messages = payload.get("messages")
        if not ProxyService._chat_messages_are_adapter_safe(messages, unsafe_reasons=unsafe_reasons):
            pass
        if unsafe_fields or unsafe_reasons:
            return EndpointConversionSafety(
                safe=False,
                code="endpoint_fallback_conversion_unsafe",
                message=(
                    "This /v1/chat/completions request contains tools, reasoning, structured-output options, "
                    "or complex multimodal content. It will not be converted to /v1/responses because the "
                    "conversion may be lossy."
                ),
                unsafe_fields=unsafe_fields,
                unsafe_reasons=unsafe_reasons,
            )
        return EndpointConversionSafety(safe=True)

    @staticmethod
    def _responses_input_is_adapter_safe(value: Any, *, unsafe_reasons: list[str]) -> bool:
        if isinstance(value, str):
            return True
        if isinstance(value, dict):
            return ProxyService._responses_input_item_is_adapter_safe(value, unsafe_reasons=unsafe_reasons)
        if isinstance(value, list):
            return all(
                ProxyService._responses_input_item_is_adapter_safe(item, unsafe_reasons=unsafe_reasons)
                for item in value
            )
        unsafe_reasons.append("responses.input must be a string, object, or list of simple message objects")
        return False

    @staticmethod
    def _responses_input_item_is_adapter_safe(item: Any, *, unsafe_reasons: list[str]) -> bool:
        if isinstance(item, str):
            return True
        if not isinstance(item, dict):
            unsafe_reasons.append("responses input item is not a string or object")
            return False
        item_type = item.get("type")
        if item_type not in {None, "message"}:
            unsafe_reasons.append(f"responses input item type {item_type!r} is not convertible")
            return False
        role = item.get("role")
        if role is not None and str(role) not in {"system", "developer", "user", "assistant"}:
            unsafe_reasons.append(f"responses input role {role!r} is not convertible")
            return False
        risky_keys = sorted(set(item.keys()) & ProxyService.ENDPOINT_ADAPTER_RISKY_FIELDS)
        if risky_keys:
            unsafe_reasons.append(f"responses input item contains risky keys: {', '.join(risky_keys)}")
            return False
        content = item.get("content")
        if content is None and isinstance(item.get("text"), str):
            return True
        return ProxyService._responses_content_is_adapter_safe(content, unsafe_reasons=unsafe_reasons)

    @staticmethod
    def _responses_content_is_adapter_safe(content: Any, *, unsafe_reasons: list[str]) -> bool:
        if isinstance(content, str):
            return True
        if not isinstance(content, list):
            unsafe_reasons.append("responses content must be a string or list of simple text/image parts")
            return False
        safe = True
        for part in content:
            if isinstance(part, str):
                continue
            if not isinstance(part, dict):
                unsafe_reasons.append("responses content part is not a string or object")
                safe = False
                continue
            part_type = part.get("type")
            if part_type in {"input_text", "text", "output_text"} and isinstance(part.get("text"), str):
                continue
            if part_type in {"input_image", "image_url"} or "image_url" in part:
                image_url = part.get("image_url")
                if isinstance(image_url, (str, dict)):
                    continue
                unsafe_reasons.append("responses image part must use a string or object image_url")
                safe = False
                continue
            unsafe_reasons.append(f"responses content part type {part_type!r} is not convertible")
            safe = False
        return safe

    @staticmethod
    def _chat_messages_are_adapter_safe(messages: Any, *, unsafe_reasons: list[str]) -> bool:
        if not isinstance(messages, list) or not messages:
            unsafe_reasons.append("chat messages must be a non-empty list")
            return False
        safe = True
        for message in messages:
            if not isinstance(message, dict):
                unsafe_reasons.append("chat message is not an object")
                safe = False
                continue
            role = str(message.get("role") or "user")
            if role not in {"system", "developer", "user", "assistant"}:
                unsafe_reasons.append(f"chat role {role!r} is not convertible")
                safe = False
            risky_keys = sorted(set(message.keys()) & (ProxyService.ENDPOINT_ADAPTER_RISKY_FIELDS | {"tool_calls", "function_call", "tool_call_id"}))
            if risky_keys:
                unsafe_reasons.append(f"chat message contains risky keys: {', '.join(risky_keys)}")
                safe = False
            content = message.get("content")
            if isinstance(content, str) or content is None:
                continue
            if not ProxyService._chat_content_is_adapter_safe(content, unsafe_reasons=unsafe_reasons):
                safe = False
        return safe

    @staticmethod
    def _chat_content_is_adapter_safe(content: Any, *, unsafe_reasons: list[str]) -> bool:
        if not isinstance(content, list):
            unsafe_reasons.append("chat content must be a string or list of simple text/image parts")
            return False
        safe = True
        for part in content:
            if isinstance(part, str):
                continue
            if not isinstance(part, dict):
                unsafe_reasons.append("chat content part is not a string or object")
                safe = False
                continue
            part_type = part.get("type")
            if part_type in {"text", "input_text"} and isinstance(part.get("text"), str):
                continue
            if part_type in {"image_url", "input_image"} or "image_url" in part:
                image_url = part.get("image_url")
                if isinstance(image_url, (str, dict)):
                    continue
                unsafe_reasons.append("chat image part must use a string or object image_url")
                safe = False
                continue
            unsafe_reasons.append(f"chat content part type {part_type!r} is not convertible")
            safe = False
        return safe

    @staticmethod
    def _unsafe_endpoint_conversion_detail(
        *,
        from_endpoint_path: str,
        to_endpoint_path: str,
        safety: EndpointConversionSafety,
        primary_error: Any | None = None,
    ) -> dict[str, Any]:
        detail: dict[str, Any] = {
            "message": safety.message or "Endpoint fallback conversion is not safe for this request",
            "code": safety.code or "endpoint_fallback_conversion_unsafe",
            "from_endpoint": from_endpoint_path,
            "to_endpoint": to_endpoint_path,
            "unsafe_fields": safety.unsafe_fields or [],
            "unsafe_reasons": safety.unsafe_reasons or [],
        }
        if primary_error is not None:
            detail["primary_error"] = ProxyService._normalize_error_detail(
                primary_error if isinstance(primary_error, str) else ProxyService._error_message_for_log(primary_error)
            )
        return detail

    @staticmethod
    def _raise_unsafe_endpoint_conversion(
        *,
        from_endpoint_path: str,
        to_endpoint_path: str,
        safety: EndpointConversionSafety,
        primary_error: Any | None = None,
    ) -> None:
        raise RequestsUpstreamHTTPError(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=ProxyService._unsafe_endpoint_conversion_detail(
                from_endpoint_path=from_endpoint_path,
                to_endpoint_path=to_endpoint_path,
                safety=safety,
                primary_error=primary_error,
            ),
        )

    @staticmethod
    def _assert_chat_response_adapter_safe(chat_response: dict[str, Any]) -> None:
        choices = chat_response.get("choices")
        unsafe_reasons: list[str] = []
        if not isinstance(choices, list) or not choices:
            unsafe_reasons.append("chat response choices must be a non-empty list")
        elif len(choices) > 1:
            unsafe_reasons.append("chat response contains multiple choices and cannot be losslessly converted")
        else:
            choice = choices[0] if isinstance(choices[0], dict) else {}
            message = choice.get("message") if isinstance(choice, dict) else None
            delta = choice.get("delta") if isinstance(choice, dict) else None
            item = message if isinstance(message, dict) else delta
            if not isinstance(item, dict):
                unsafe_reasons.append("chat response choice does not contain a simple message or delta object")
            else:
                risky_keys = sorted(set(item.keys()) & {"tool_calls", "function_call", "refusal", "audio"})
                if risky_keys:
                    unsafe_reasons.append(f"chat response contains risky keys: {', '.join(risky_keys)}")
                content = item.get("content")
                if content is not None and not isinstance(content, str):
                    unsafe_reasons.append("chat response content is not simple text")
        if unsafe_reasons:
            ProxyService._raise_unsafe_response_conversion(
                from_endpoint_path="/chat/completions",
                to_endpoint_path="/responses",
                unsafe_reasons=unsafe_reasons,
            )

    @staticmethod
    def _assert_responses_response_adapter_safe(responses_payload: dict[str, Any]) -> None:
        unsafe_reasons: list[str] = []
        output = responses_payload.get("output")
        if output is None:
            if not isinstance(responses_payload.get("output_text"), str):
                unsafe_reasons.append("responses payload does not contain simple output_text or output message content")
        elif not isinstance(output, list):
            unsafe_reasons.append("responses output must be a list")
        else:
            for item in output:
                if not isinstance(item, dict):
                    unsafe_reasons.append("responses output item is not an object")
                    continue
                item_type = item.get("type")
                if item_type not in {None, "message"}:
                    unsafe_reasons.append(f"responses output item type {item_type!r} is not convertible")
                    continue
                risky_keys = sorted(set(item.keys()) & {"tool_call", "function_call", "reasoning", "code_interpreter_call", "file_search_call"})
                if risky_keys:
                    unsafe_reasons.append(f"responses output item contains risky keys: {', '.join(risky_keys)}")
                content = item.get("content")
                if not isinstance(content, list):
                    unsafe_reasons.append("responses output message content must be a list")
                    continue
                for part in content:
                    if not isinstance(part, dict):
                        unsafe_reasons.append("responses output content part is not an object")
                        continue
                    part_type = part.get("type")
                    if part_type not in {"output_text", "text"} or not isinstance(part.get("text"), str):
                        unsafe_reasons.append(f"responses output content part type {part_type!r} is not simple text")
        if unsafe_reasons:
            ProxyService._raise_unsafe_response_conversion(
                from_endpoint_path="/responses",
                to_endpoint_path="/chat/completions",
                unsafe_reasons=unsafe_reasons,
            )

    @staticmethod
    def _raise_unsafe_response_conversion(
        *,
        from_endpoint_path: str,
        to_endpoint_path: str,
        unsafe_reasons: list[str],
    ) -> None:
        raise RequestsUpstreamHTTPError(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail={
                "message": (
                    "Upstream returned a complex response that cannot be safely converted while preserving "
                    "the client's requested endpoint format."
                ),
                "code": "endpoint_response_conversion_unsafe",
                "from_endpoint": from_endpoint_path,
                "to_endpoint": to_endpoint_path,
                "unsafe_reasons": unsafe_reasons,
            },
        )

    @staticmethod
    def _should_try_endpoint_fallback(
        provider: Provider,
        *,
        endpoint_path: str,
        status_code: int,
        error_detail: Any,
    ) -> bool:
        if provider.provider_type != "openai_compatible":
            return False
        if endpoint_path not in {"/responses", "/chat/completions"}:
            return False
        if status_code not in {400, 404}:
            return False
        message = ProxyService._error_message_for_log(error_detail).lower()
        endpoint_tokens = (
            "unsupported",
            "not support",
            "not_supported",
            "invalidparameter",
            "invalid parameter",
            "not found",
            "unknown",
            "responses",
            "chat/completions",
            "model",
        )
        return any(token in message for token in endpoint_tokens)

    @staticmethod
    def _build_endpoint_fallback_request(
        *,
        requested_endpoint_path: str,
        failed_request_path: str,
        payload: dict[str, Any],
        primary_error: Any | None = None,
    ) -> PreparedUpstreamRequest:
        if failed_request_path == "/responses":
            safety = ProxyService._assess_endpoint_conversion_safety(
                from_endpoint_path="/responses",
                to_endpoint_path="/chat/completions",
                payload=payload,
            )
            if not safety.safe:
                ProxyService._raise_unsafe_endpoint_conversion(
                    from_endpoint_path="/responses",
                    to_endpoint_path="/chat/completions",
                    safety=safety,
                    primary_error=primary_error,
                )
            return PreparedUpstreamRequest(
                request_path="/chat/completions",
                request_payload=ProxyService._build_chat_payload_from_responses_payload(payload),
                adapt_chat_response_to_responses=requested_endpoint_path == "/responses",
                fallback_from_path=failed_request_path,
            )
        if failed_request_path == "/chat/completions":
            safety = ProxyService._assess_endpoint_conversion_safety(
                from_endpoint_path="/chat/completions",
                to_endpoint_path="/responses",
                payload=payload,
            )
            if not safety.safe:
                ProxyService._raise_unsafe_endpoint_conversion(
                    from_endpoint_path="/chat/completions",
                    to_endpoint_path="/responses",
                    safety=safety,
                    primary_error=primary_error,
                )
            return PreparedUpstreamRequest(
                request_path="/responses",
                request_payload=ProxyService._build_responses_payload_from_chat_payload(payload),
                adapt_responses_response_to_chat=requested_endpoint_path == "/chat/completions",
                fallback_from_path=failed_request_path,
            )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": "Unsupported endpoint fallback path", "code": "unsupported_endpoint_fallback"},
        )

    @staticmethod
    def _prepare_upstream_request(provider: Provider, *, endpoint_path: str, payload: dict[str, Any]) -> PreparedUpstreamRequest:
        return PreparedUpstreamRequest(
            request_path=endpoint_path,
            request_payload=payload,
            adapt_chat_response_to_responses=False,
        )

    @staticmethod
    def _build_responses_payload_from_chat_payload(payload: dict[str, Any]) -> dict[str, Any]:
        responses_payload: dict[str, Any] = {
            "model": payload.get("model"),
            "input": ProxyService._build_responses_input_from_chat_messages(payload.get("messages")),
        }
        passthrough_map = {
            "temperature": "temperature",
            "top_p": "top_p",
            "presence_penalty": "presence_penalty",
            "frequency_penalty": "frequency_penalty",
            "tools": "tools",
            "tool_choice": "tool_choice",
            "response_format": "response_format",
            "stream": "stream",
            "user": "user",
            "metadata": "metadata",
            "seed": "seed",
        }
        for source_key, target_key in passthrough_map.items():
            if source_key in payload:
                responses_payload[target_key] = payload[source_key]
        if "max_completion_tokens" in payload:
            responses_payload["max_output_tokens"] = payload["max_completion_tokens"]
        elif "max_tokens" in payload:
            responses_payload["max_output_tokens"] = payload["max_tokens"]
        reasoning_effort = LogService.extract_model_reasoning_effort(payload)
        if reasoning_effort is not None:
            responses_payload["reasoning"] = {"effort": reasoning_effort}
        return responses_payload

    @staticmethod
    def _build_responses_input_from_chat_messages(messages: Any) -> list[dict[str, Any]]:
        if not isinstance(messages, list) or not messages:
            return [{"role": "user", "content": [{"type": "input_text", "text": ""}]}]
        items: list[dict[str, Any]] = []
        for message in messages:
            if not isinstance(message, dict):
                continue
            role = str(message.get("role") or "user")
            content = message.get("content")
            if role == "system":
                role = "developer"
            if isinstance(content, str):
                items.append({"role": role, "content": [{"type": "input_text", "text": content}]})
                continue
            if isinstance(content, list):
                parts = []
                for part in content:
                    converted = ProxyService._convert_chat_content_part_to_responses_content(part)
                    if converted is not None:
                        parts.append(converted)
                items.append({"role": role, "content": parts or [{"type": "input_text", "text": ""}]})
                continue
            if content is None:
                items.append({"role": role, "content": [{"type": "input_text", "text": ""}]})
        return items or [{"role": "user", "content": [{"type": "input_text", "text": ""}]}]

    @staticmethod
    def _convert_chat_content_part_to_responses_content(part: Any) -> dict[str, Any] | None:
        if isinstance(part, str):
            return {"type": "input_text", "text": part}
        if not isinstance(part, dict):
            return None
        part_type = part.get("type")
        if part_type in {"text", "input_text"} and isinstance(part.get("text"), str):
            return {"type": "input_text", "text": part["text"]}
        if part_type in {"image_url", "input_image"} or "image_url" in part:
            image_url_value = part.get("image_url")
            if isinstance(image_url_value, dict):
                image_url = image_url_value.get("url")
                detail = image_url_value.get("detail") or part.get("detail")
            else:
                image_url = image_url_value
                detail = part.get("detail")
            result = {"type": "input_image", "image_url": image_url}
            if detail:
                result["detail"] = detail
            return result
        return None

    @staticmethod
    def _build_chat_payload_from_responses_payload(payload: dict[str, Any]) -> dict[str, Any]:
        chat_payload: dict[str, Any] = {
            "model": payload.get("model"),
        }
        instructions = payload.get("instructions")
        messages: list[dict[str, Any]] = []
        if isinstance(instructions, str) and instructions.strip():
            messages.append({"role": "system", "content": instructions})
        input_value = payload.get("input")
        if isinstance(input_value, str):
            messages.append({"role": "user", "content": input_value})
        elif isinstance(input_value, list):
            for item in input_value:
                converted = ProxyService._convert_responses_input_item_to_chat_message(item)
                if converted is not None:
                    messages.append(converted)
        elif isinstance(input_value, dict):
            converted = ProxyService._convert_responses_input_item_to_chat_message(input_value)
            if converted is not None:
                messages.append(converted)
        chat_payload["messages"] = messages or [{"role": "user", "content": ""}]

        passthrough_excluded_keys = {"input", "instructions", "max_output_tokens"}
        for key, value in payload.items():
            if key in passthrough_excluded_keys or key == "model":
                continue
            chat_payload[key] = value
        if "max_output_tokens" in payload and "max_completion_tokens" not in chat_payload and "max_tokens" not in chat_payload:
            chat_payload["max_completion_tokens"] = payload["max_output_tokens"]
        if "max_tokens" in payload and "max_completion_tokens" not in chat_payload:
            chat_payload["max_tokens"] = payload["max_tokens"]
        reasoning_effort = LogService.extract_model_reasoning_effort(payload)
        if reasoning_effort is not None and "reasoning_effort" not in chat_payload:
            chat_payload["reasoning_effort"] = reasoning_effort
        return chat_payload

    @staticmethod
    def _convert_responses_input_item_to_chat_message(item: Any) -> dict[str, Any] | None:
        if isinstance(item, str):
            return {"role": "user", "content": item}
        if not isinstance(item, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"message": "Responses adapter input items must be strings or objects"},
            )
        role = str(item.get("role") or "user")
        content = item.get("content")
        if isinstance(content, list):
            converted_parts: list[dict[str, Any]] = []
            for part in content:
                converted_part = ProxyService._convert_responses_content_part_to_chat_content(part)
                if converted_part is not None:
                    converted_parts.append(converted_part)
            return {"role": role, "content": converted_parts}
        if isinstance(content, str):
            return {"role": role, "content": content}
        if content is None and "text" in item and isinstance(item.get("text"), str):
            return {"role": role, "content": item["text"]}
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={"message": "Responses adapter received an unsupported input item content format"},
        )

    @staticmethod
    def _convert_responses_content_part_to_chat_content(part: Any) -> dict[str, Any] | None:
        if isinstance(part, str):
            return {"type": "text", "text": part}
        if not isinstance(part, dict):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"message": "Responses adapter content parts must be strings or objects"},
            )
        part_type = part.get("type")
        if part_type in {"input_text", "text", "output_text"} and isinstance(part.get("text"), str):
            return {"type": "text", "text": part["text"]}
        if part_type in {"input_image", "image_url"} or "image_url" in part:
            image_url_value = part.get("image_url")
            detail = part.get("detail")
            if isinstance(image_url_value, dict):
                image_url_payload = dict(image_url_value)
            else:
                image_url_payload = {"url": image_url_value}
            if detail and "detail" not in image_url_payload:
                image_url_payload["detail"] = detail
            return {"type": "image_url", "image_url": image_url_payload}
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "Responses adapter received an unsupported content part type",
                "unsupported_part_type": part_type,
            },
        )

    @staticmethod
    def _convert_chat_completion_to_responses_payload(
        chat_response: dict[str, Any],
        *,
        requested_model: str,
    ) -> dict[str, Any]:
        response_id = str(chat_response.get("id") or f"resp_{uuid4().hex}")
        created_at = chat_response.get("created")
        model_name = str(chat_response.get("model") or requested_model or "")
        choices = chat_response.get("choices")
        assistant_text = ""
        finish_reason = "completed"
        if isinstance(choices, list) and choices:
            first_choice = choices[0] if isinstance(choices[0], dict) else {}
            message = first_choice.get("message") if isinstance(first_choice, dict) else {}
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                assistant_text = message["content"]
            finish_reason = str(first_choice.get("finish_reason") or finish_reason)
        usage = chat_response.get("usage") if isinstance(chat_response.get("usage"), dict) else {}
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        total_tokens = usage.get("total_tokens")
        cache_read_tokens, cache_write_tokens = LogService.extract_cache_tokens({"usage": usage})
        responses_usage = {
            "input_tokens": int(prompt_tokens or 0),
            "output_tokens": int(completion_tokens or 0),
            "total_tokens": int(total_tokens or ((prompt_tokens or 0) + (completion_tokens or 0))),
        }
        if cache_read_tokens is not None or cache_write_tokens is not None:
            responses_usage["input_tokens_details"] = {
                "cached_tokens": int(cache_read_tokens or 0),
                "cache_creation_tokens": int(cache_write_tokens or 0),
            }
        return {
            "id": response_id,
            "object": "response",
            "created_at": created_at,
            "status": "completed",
            "model": model_name,
            "output_text": assistant_text,
            "output": [
                {
                    "id": f"msg_{uuid4().hex}",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "finish_reason": finish_reason,
                    "content": [
                        {
                            "type": "output_text",
                            "text": assistant_text,
                            "annotations": [],
                        }
                    ],
                }
            ],
            "usage": responses_usage,
        }

    @staticmethod
    def _convert_responses_payload_to_chat_completion(
        responses_payload: dict[str, Any],
        *,
        requested_model: str,
    ) -> dict[str, Any]:
        response_id = str(responses_payload.get("id") or f"chatcmpl_{uuid4().hex}")
        created_at = responses_payload.get("created_at") or int(datetime.utcnow().timestamp())
        model_name = str(responses_payload.get("model") or requested_model or "")
        assistant_text = ProxyService._extract_response_text(responses_payload, limit_bytes=1_048_576) or ""
        finish_reason = ProxyService._extract_finish_reason(responses_payload) or "stop"
        usage = responses_payload.get("usage") if isinstance(responses_payload.get("usage"), dict) else {}
        input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
        output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
        total_tokens = usage.get("total_tokens")
        cache_read_tokens, cache_write_tokens = LogService.extract_cache_tokens({"usage": usage})
        chat_usage = {
            "prompt_tokens": int(input_tokens or 0),
            "completion_tokens": int(output_tokens or 0),
            "total_tokens": int(total_tokens or ((input_tokens or 0) + (output_tokens or 0))),
        }
        if cache_read_tokens is not None or cache_write_tokens is not None:
            chat_usage["prompt_tokens_details"] = {
                "cached_tokens": int(cache_read_tokens or 0),
                "cache_creation_tokens": int(cache_write_tokens or 0),
            }
        return {
            "id": response_id,
            "object": "chat.completion",
            "created": int(created_at or datetime.utcnow().timestamp()),
            "model": model_name,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": assistant_text,
                    },
                    "finish_reason": finish_reason if finish_reason != "completed" else "stop",
                }
            ],
            "usage": chat_usage,
        }

    @staticmethod
    def _create_responses_stream_state(*, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "buffer": bytearray(),
            "response_id": f"resp_{uuid4().hex}",
            "message_id": f"msg_{uuid4().hex}",
            "created_at": int(datetime.utcnow().timestamp()),
            "model": str(payload.get("model") or ""),
            "output_text_parts": [],
            "finish_reason": None,
            "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
            "created_sent": False,
            "completed_sent": False,
        }

    @staticmethod
    def _adapt_chat_stream_chunk_to_responses_events(
        chunk: bytes,
        *,
        state: dict[str, Any],
        requested_model: str,
    ) -> list[bytes]:
        events: list[bytes] = []
        state_buffer = state["buffer"]
        state_buffer.extend(chunk)
        for event_text in ProxyService._consume_sse_event_texts(state_buffer):
            for line in event_text.splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data:
                    continue
                if data == "[DONE]":
                    events.extend(ProxyService._build_responses_stream_completion_events(state))
                    continue
                parsed = safeJsonParse(data)
                if not isinstance(parsed, dict):
                    continue
                if not state["created_sent"]:
                    state["response_id"] = str(parsed.get("id") or state["response_id"])
                    state["created_at"] = int(parsed.get("created") or state["created_at"])
                    state["model"] = str(parsed.get("model") or requested_model or state["model"])
                    state["created_sent"] = True
                    events.append(
                        ProxyService._format_sse_event(
                            {
                                "type": "response.created",
                                "response": {
                                    "id": state["response_id"],
                                    "object": "response",
                                    "created_at": state["created_at"],
                                    "status": "in_progress",
                                    "model": state["model"],
                                },
                            }
                        )
                    )
                delta_text = ProxyService._extract_chat_stream_delta_text(parsed)
                if delta_text:
                    state["output_text_parts"].append(delta_text)
                    events.append(
                        ProxyService._format_sse_event(
                            {
                                "type": "response.output_text.delta",
                                "response_id": state["response_id"],
                                "item_id": state["message_id"],
                                "output_index": 0,
                                "content_index": 0,
                                "delta": delta_text,
                            }
                        )
                    )
                usage = parsed.get("usage")
                if isinstance(usage, dict):
                    input_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
                    output_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
                    total_tokens = usage.get("total_tokens")
                    cache_read_tokens, cache_write_tokens = LogService.extract_cache_tokens({"usage": usage})
                    if isinstance(input_tokens, int):
                        state["usage"]["input_tokens"] = input_tokens
                    if isinstance(output_tokens, int):
                        state["usage"]["output_tokens"] = output_tokens
                    if isinstance(total_tokens, int):
                        state["usage"]["total_tokens"] = total_tokens
                    if cache_read_tokens is not None or cache_write_tokens is not None:
                        state["usage"]["input_tokens_details"] = {
                            "cached_tokens": int(cache_read_tokens or 0),
                            "cache_creation_tokens": int(cache_write_tokens or 0),
                        }
                finish_reason = ProxyService._extract_finish_reason(parsed)
                if isinstance(finish_reason, str) and finish_reason:
                    state["finish_reason"] = finish_reason
                    events.extend(ProxyService._build_responses_stream_completion_events(state))
        return events

    @staticmethod
    def _build_responses_stream_completion_events(state: dict[str, Any]) -> list[bytes]:
        if state.get("completed_sent"):
            return []
        state["completed_sent"] = True
        output_text = "".join(state["output_text_parts"])
        finish_reason = state.get("finish_reason") or "completed"
        usage = dict(state["usage"])
        if not usage.get("total_tokens"):
            usage["total_tokens"] = int(usage.get("input_tokens", 0) or 0) + int(usage.get("output_tokens", 0) or 0)
        response_payload = {
            "id": state["response_id"],
            "object": "response",
            "created_at": state["created_at"],
            "status": "completed",
            "model": state["model"],
            "output_text": output_text,
            "output": [
                {
                    "id": state["message_id"],
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "finish_reason": finish_reason,
                    "content": [
                        {
                            "type": "output_text",
                            "text": output_text,
                            "annotations": [],
                        }
                    ],
                }
            ],
            "usage": usage,
        }
        return [
            ProxyService._format_sse_event(
                {
                    "type": "response.output_text.done",
                    "response_id": state["response_id"],
                    "item_id": state["message_id"],
                    "output_index": 0,
                    "content_index": 0,
                    "text": output_text,
                }
            ),
            ProxyService._format_sse_event({"type": "response.completed", "response": response_payload}),
            b"data: [DONE]\n\n",
        ]

    @staticmethod
    def _create_chat_stream_state(*, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "buffer": bytearray(),
            "chunk_id": f"chatcmpl_{uuid4().hex}",
            "created": int(datetime.utcnow().timestamp()),
            "model": str(payload.get("model") or ""),
            "finish_reason": None,
            "created_sent": False,
            "completed_sent": False,
        }

    @staticmethod
    def _adapt_responses_stream_chunk_to_chat_events(
        chunk: bytes,
        *,
        state: dict[str, Any],
        requested_model: str,
    ) -> list[bytes]:
        events: list[bytes] = []
        state_buffer = state["buffer"]
        state_buffer.extend(chunk)
        for event_text in ProxyService._consume_sse_event_texts(state_buffer):
            for line in event_text.splitlines():
                line = line.strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if not data:
                    continue
                if data == "[DONE]":
                    events.extend(ProxyService._build_chat_stream_completion_events(state))
                    continue
                parsed = safeJsonParse(data)
                if not isinstance(parsed, dict):
                    continue
                response = parsed.get("response")
                if isinstance(response, dict):
                    state["chunk_id"] = str(response.get("id") or state["chunk_id"])
                    state["created"] = int(response.get("created_at") or state["created"])
                    state["model"] = str(response.get("model") or requested_model or state["model"])
                if not state["created_sent"]:
                    state["created_sent"] = True
                    events.append(
                        ProxyService._format_sse_event(
                            {
                                "id": state["chunk_id"],
                                "object": "chat.completion.chunk",
                                "created": state["created"],
                                "model": state["model"],
                                "choices": [{"index": 0, "delta": {"role": "assistant"}, "finish_reason": None}],
                            }
                        )
                    )
                delta = parsed.get("delta")
                if isinstance(delta, str) and delta:
                    events.append(
                        ProxyService._format_sse_event(
                            {
                                "id": state["chunk_id"],
                                "object": "chat.completion.chunk",
                                "created": state["created"],
                                "model": state["model"],
                                "choices": [{"index": 0, "delta": {"content": delta}, "finish_reason": None}],
                            }
                        )
                    )
                finish_reason = ProxyService._extract_finish_reason(parsed)
                if isinstance(finish_reason, str) and finish_reason:
                    state["finish_reason"] = finish_reason
                event_type = parsed.get("type")
                if event_type == "response.completed":
                    events.extend(ProxyService._build_chat_stream_completion_events(state))
        return events

    @staticmethod
    def _build_chat_stream_completion_events(state: dict[str, Any]) -> list[bytes]:
        if state.get("completed_sent"):
            return []
        state["completed_sent"] = True
        finish_reason = state.get("finish_reason") or "stop"
        if finish_reason == "completed":
            finish_reason = "stop"
        return [
            ProxyService._format_sse_event(
                {
                    "id": state["chunk_id"],
                    "object": "chat.completion.chunk",
                    "created": state["created"],
                    "model": state["model"],
                    "choices": [{"index": 0, "delta": {}, "finish_reason": finish_reason}],
                }
            ),
            b"data: [DONE]\n\n",
        ]

    @staticmethod
    def _format_sse_event(payload: dict[str, Any]) -> bytes:
        return f"data: {dumps_json(payload)}\n\n".encode("utf-8")

    @staticmethod
    def _format_stream_error_event(*, message: str, code: str, trace_id: str | None) -> bytes:
        payload: dict[str, Any] = {
            "error": {
                "message": message,
                "type": "server_error",
                "code": code,
                "retryable": True,
            }
        }
        if trace_id:
            payload["error"]["trace_id"] = trace_id
        return f"event: error\ndata: {dumps_json(payload)}\n\n".encode("utf-8")

    @staticmethod
    def _extract_chat_stream_delta_text(event_json: dict[str, Any]) -> str | None:
        choices = event_json.get("choices")
        if not isinstance(choices, list):
            return None
        parts: list[str] = []
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if isinstance(delta, dict):
                content = delta.get("content")
                if isinstance(content, str):
                    parts.append(content)
        return "".join(parts) or None

    @staticmethod
    def _mark_failure(
        db: Session,
        provider: Provider,
        provider_model: ProviderModel,
        latency_ms: int,
        error_message: str | None,
        force_unhealthy: bool = False,
    ) -> None:
        provider.failure_count += 1
        provider.last_latency_ms = latency_ms
        provider_model.failure_count += 1
        provider_model.last_latency_ms = latency_ms
        provider_model.last_error = error_message
        if force_unhealthy:
            provider_model.health_status = "unhealthy"
            provider_model.circuit_state = "open"
            provider_model.circuit_opened_at = datetime.utcnow()
        elif provider.auto_circuit_break_enabled:
            threshold = ProviderService.get_effective_circuit_breaker_threshold(db, provider)
            if provider_model.circuit_state == "half_open" or provider_model.failure_count >= threshold:
                provider_model.health_status = "unhealthy"
                provider_model.circuit_state = "open"
                provider_model.circuit_opened_at = datetime.utcnow()
            else:
                provider_model.health_status = "degraded"
                provider_model.circuit_state = "closed"
                provider_model.circuit_opened_at = None
        else:
            provider_model.health_status = "degraded"
            provider_model.circuit_state = "closed"
            provider_model.circuit_opened_at = None
        ProviderService.refresh_provider_state(provider)

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
            return {"message": "Upstream request failed", "code": "upstream_request_failed"}
        parsed = safeJsonParse(error_body)
        return parsed if parsed is not None else {"message": error_body, "code": "upstream_request_failed"}

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
        session_id: str | None,
        resolved_provider_model_id: int | None,
        is_stream: bool,
        has_image: bool,
        request_body_json: str | None,
        request_payload: dict[str, Any] | None,
        schedule_token_fill: bool,
        reasoning_level: str,
        attempt_count: int,
        model_reasoning_effort: str | None = None,
        api_client_auth: ApiClientAuthContext | None = None,
        trace_id: str | None = None,
        source_ip: str | None = None,
    ) -> None:
        if ProxyService._attempt_count(trace) <= 1 and upstream_error is not None:
            status_code = upstream_error["status_code"]
            detail = upstream_error["detail"]
            message = ProxyService._error_message_for_log(detail)
            LogService.create_log(
                db,
                log_type=log_type,
                trace_id=trace_id,
                model_name=model_name,
                requested_model=requested_model,
                tenant_name=api_client_auth.api_client_key.tenant_name if api_client_auth else None,
                project_name=api_client_auth.api_client_key.project_name if api_client_auth else None,
                app_name=api_client_auth.api_client_key.app_name if api_client_auth else None,
                environment_name=api_client_auth.api_client_key.environment_name if api_client_auth else None,
                request_id=request_id,
                conversation_key=conversation_key,
                session_id=session_id,
                resolved_provider_model_id=resolved_provider_model_id,
                request_path=f"/v1{endpoint_path}",
                source_ip=source_ip,
                http_method="POST",
                is_stream=is_stream,
                has_image=has_image,
                success=False,
                status_code=status_code,
                reasoning_level=reasoning_level,
                model_reasoning_effort=model_reasoning_effort,
                request_body_json=request_body_json,
                message=message,
                error_type=ProxyService._error_type_from_status(status_code),
                error_code=ProxyService._error_code_from_detail(detail),
                retryable=ProxyService._is_retryable_status(status_code),
                **ProxyService._build_api_client_log_kwargs(api_client_auth, auth_result="authenticated"),
                trace=trace,
                attempt_count=attempt_count,
                token_request_payload=request_payload,
                schedule_token_fill=schedule_token_fill,
            )
            raise HTTPException(status_code=status_code, detail=detail)

        if upstream_error is not None and upstream_error.get("status_code") in {status.HTTP_429_TOO_MANY_REQUESTS, status.HTTP_503_SERVICE_UNAVAILABLE}:
            status_code = upstream_error["status_code"]
            detail = upstream_error["detail"]
            LogService.create_log(
                db,
                log_type=log_type,
                trace_id=trace_id,
                model_name=model_name,
                requested_model=requested_model,
                tenant_name=api_client_auth.api_client_key.tenant_name if api_client_auth else None,
                project_name=api_client_auth.api_client_key.project_name if api_client_auth else None,
                app_name=api_client_auth.api_client_key.app_name if api_client_auth else None,
                environment_name=api_client_auth.api_client_key.environment_name if api_client_auth else None,
                request_id=request_id,
                conversation_key=conversation_key,
                session_id=session_id,
                resolved_provider_model_id=resolved_provider_model_id,
                request_path=f"/v1{endpoint_path}",
                source_ip=source_ip,
                http_method="POST",
                is_stream=is_stream,
                has_image=has_image,
                success=False,
                status_code=status_code,
                reasoning_level=reasoning_level,
                model_reasoning_effort=model_reasoning_effort,
                request_body_json=request_body_json,
                message=ProxyService._error_message_for_log(detail),
                error_type=ProxyService._error_type_from_status(status_code),
                error_code=ProxyService._error_code_from_detail(detail),
                retryable=True,
                **ProxyService._build_api_client_log_kwargs(api_client_auth, auth_result="authenticated"),
                trace=trace,
                attempt_count=attempt_count,
                token_request_payload=request_payload,
                schedule_token_fill=schedule_token_fill,
            )
            raise HTTPException(status_code=status_code, detail=detail)

        detail = {"message": "All providers failed", "trace": trace}
        LogService.create_log(
            db,
            log_type=log_type,
            trace_id=trace_id,
            model_name=model_name,
            requested_model=requested_model,
            tenant_name=api_client_auth.api_client_key.tenant_name if api_client_auth else None,
            project_name=api_client_auth.api_client_key.project_name if api_client_auth else None,
            app_name=api_client_auth.api_client_key.app_name if api_client_auth else None,
            environment_name=api_client_auth.api_client_key.environment_name if api_client_auth else None,
            request_id=request_id,
            conversation_key=conversation_key,
            session_id=session_id,
            resolved_provider_model_id=resolved_provider_model_id,
            request_path=f"/v1{endpoint_path}",
            source_ip=source_ip,
            http_method="POST",
            is_stream=is_stream,
            has_image=has_image,
            success=False,
            status_code=502,
            reasoning_level=reasoning_level,
            model_reasoning_effort=model_reasoning_effort,
            request_body_json=request_body_json,
            message="All providers failed",
            error_type="server_error",
            error_code="all_providers_failed",
            retryable=True,
            **ProxyService._build_api_client_log_kwargs(api_client_auth, auth_result="authenticated"),
            trace=trace,
            attempt_count=attempt_count,
            token_request_payload=request_payload,
            schedule_token_fill=schedule_token_fill,
        )
        raise HTTPException(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)

    @staticmethod
    async def _raise_final_error_async(
        db: Session | None,
        **kwargs,
    ) -> None:
        if db is not None:
            await run_in_threadpool(ProxyService._raise_final_error, db, **kwargs)
            return
        await run_in_threadpool(ProxyService._raise_final_error_with_scoped_session, **kwargs)

    @staticmethod
    def _error_message_for_log(detail: Any) -> str:
        if isinstance(detail, dict):
            if isinstance(detail.get("error"), dict) and detail["error"].get("message"):
                return str(detail["error"]["message"])
            if detail.get("message"):
                return str(detail["message"])
        return str(detail)

    @staticmethod
    def _should_mark_provider_model_unhealthy(*, status_code: int | None, detail: Any) -> bool:
        error_code = (ProxyService._error_code_from_detail(detail) or "").strip().lower()
        message = ProxyService._error_message_for_log(detail).strip().lower()
        if status_code in {401, 403, 404}:
            return True
        fatal_code_tokens = (
            "subscription",
            "model_not_found",
            "resource_not_found",
            "unsupported",
            "not_supported",
            "permission_denied",
            "access_denied",
            "forbidden",
            "invalid_api_key",
            "insufficient_permissions",
        )
        if error_code and any(token in error_code for token in fatal_code_tokens):
            return True
        fatal_message_tokens = (
            "no active subscription",
            "subscription not found",
            "model not found",
            "unknown model",
            "no such model",
            "does not support",
            "do not support",
            "not support",
            "unsupported",
            "not available for this group",
            "resource not found",
            "access denied",
            "permission denied",
            "forbidden",
            "invalid api key",
        )
        return any(token in message for token in fatal_message_tokens)

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
        extra: dict[str, Any] | None = None,
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
        if extra:
            item.update(extra)
        return item

    @staticmethod
    def _attempt_count(trace: list[dict]) -> int:
        attempt_results = {
            "success",
            "stream_opened",
            "http_error",
            "exception",
            "rate_limited",
            "model_not_found",
            "request_rejected",
            "upstream_auth_error",
            "capacity_limited",
            "capacity_unavailable",
        }
        return sum(1 for item in trace if item.get("result") in attempt_results)

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
            nested_response = response_json.get("response")
            if isinstance(nested_response, dict):
                usage = nested_response.get("usage")
        if not isinstance(usage, dict):
            return {
                "prompt_tokens": None,
                "completion_tokens": None,
                "total_tokens": None,
                "cache_read_tokens": None,
                "cache_write_tokens": None,
            }
        prompt_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
        completion_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
        total_tokens = usage.get("total_tokens")
        cache_read_tokens, cache_write_tokens = LogService.extract_cache_tokens({"usage": usage})
        return {
            "prompt_tokens": ProxyService._coerce_non_negative_int(prompt_tokens),
            "completion_tokens": ProxyService._coerce_non_negative_int(completion_tokens),
            "total_tokens": ProxyService._coerce_non_negative_int(total_tokens),
            "cache_read_tokens": cache_read_tokens,
            "cache_write_tokens": cache_write_tokens,
        }

    @staticmethod
    def _coerce_non_negative_int(value: Any) -> int | None:
        if isinstance(value, bool):
            return None
        if isinstance(value, (int, float)):
            return max(0, int(value))
        return None

    @staticmethod
    def _extract_finish_reason(response_json: dict[str, Any]) -> str | None:
        nested_response = response_json.get("response")
        if isinstance(nested_response, dict):
            finish_reason = ProxyService._extract_finish_reason(nested_response)
            if finish_reason is not None:
                return finish_reason
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
            if parts and parts[-1] == value:
                return
            encoded = value.encode("utf-8", errors="ignore")
            remaining = limit_bytes - current_bytes
            if len(encoded) > remaining:
                value = encoded[:remaining].decode("utf-8", errors="ignore")
                encoded = value.encode("utf-8", errors="ignore")
            if value:
                parts.append(value)
                current_bytes += len(encoded)

        nested_response = response_json.get("response")
        if isinstance(nested_response, dict):
            nested_text = ProxyService._extract_response_text(nested_response, limit_bytes=limit_bytes)
            if nested_text:
                append_text(nested_text)

        part = response_json.get("part")
        if isinstance(part, dict):
            part_text = part.get("text")
            if isinstance(part_text, str):
                append_text(part_text)

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
    def _serialize_payload_for_logging(
        payload: dict[str, Any],
        *,
        setting: Any,
        preserve_request_content_when_disabled: bool = False,
    ) -> str | None:
        should_log_full_payload = getattr(setting, "enable_payload_logging", True)
        payload_to_log: Any = payload
        if not should_log_full_payload:
            if not preserve_request_content_when_disabled:
                return None
            payload_to_log = ProxyService._extract_request_logging_payload(payload, setting=setting)
            if payload_to_log is None:
                return None
        sanitized = ProxyService._sanitize_for_logging(payload_to_log, mask_sensitive=getattr(setting, "mask_sensitive_fields", True))
        return ProxyService._truncate_serialized_json(sanitized, getattr(setting, "max_logged_body_bytes", 16384))

    @staticmethod
    def _extract_request_logging_payload(payload: dict[str, Any], *, setting: Any) -> dict[str, Any] | None:
        if not isinstance(payload, dict):
            return None
        compact_payload: dict[str, Any] = {}
        for key in (
            "model",
            "stream",
            "user",
            "max_tokens",
            "max_completion_tokens",
            "max_output_tokens",
            "reasoning",
            "reasoning_effort",
        ):
            if key in payload:
                compact_payload[key] = payload[key]
        if "metadata" in payload:
            compact_payload["metadata"] = ProxyService._compact_metadata_for_logging(
                payload.get("metadata"),
                max_bytes=int(getattr(setting, "max_logged_metadata_bytes", 1024) or 0),
            )
        return compact_payload or None

    @staticmethod
    def _compact_metadata_for_logging(value: Any, *, max_bytes: int) -> Any:
        if value is None:
            return None
        serialized = dumps_json(value)
        encoded = serialized.encode("utf-8", errors="ignore")
        if max_bytes > 0 and len(encoded) <= max_bytes:
            return value

        summary: dict[str, Any] = {
            "_summary": "metadata omitted from compact request log",
            "value_type": type(value).__name__,
            "original_bytes": len(encoded),
        }
        if isinstance(value, dict):
            keys = [str(key) for key in value.keys()]
            summary["key_count"] = len(keys)
            summary["keys"] = keys[:50]
        elif isinstance(value, list):
            summary["item_count"] = len(value)
        return summary

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
        token_response_bytes: int,
        finish_reason: str | None,
        usage_info: dict[str, int | None],
        capture_text: bool,
        capture_usage: bool,
        limit_bytes: int,
        token_limit_bytes: int,
    ) -> tuple[int, int, str | None, dict[str, int | None]]:
        event_buffer.extend(chunk)
        for event_text in ProxyService._consume_sse_event_texts(event_buffer):
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
                        token_response_bytes = ProxyService._append_limited_text(
                            token_response_parts,
                            delta_text,
                            current_bytes=token_response_bytes,
                            limit_bytes=token_limit_bytes,
                        )
        return response_text_bytes, token_response_bytes, finish_reason, usage_info

    @staticmethod
    def _consume_sse_event_texts(event_buffer: bytearray) -> list[str]:
        events: list[str] = []
        while True:
            separator_length = 0
            separator_index = event_buffer.find(b"\r\n\r\n")
            if separator_index >= 0:
                separator_length = 4
            else:
                separator_index = event_buffer.find(b"\n\n")
                if separator_index >= 0:
                    separator_length = 2
            if separator_index < 0:
                break
            raw_event = bytes(event_buffer[:separator_index])
            del event_buffer[: separator_index + separator_length]
            events.append(raw_event.decode("utf-8", errors="ignore"))
        return events

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
            "user_account_id": api_client_auth.api_client_key.owner_user_id,
            "user_account_name": (
                api_client_auth.api_client_key.owner_user.username
                if api_client_auth.api_client_key.owner_user is not None
                else None
            ),
            "api_client_auth_result": auth_result,
            "api_client_remaining_tokens": remaining_tokens if remaining_tokens is not None else api_client_auth.remaining_tokens,
            "api_client_remaining_requests_daily": api_client_auth.remaining_requests_daily,
            "api_client_remaining_cost_daily": api_client_auth.remaining_cost_daily,
            "api_client_policy_snapshot_json": api_client_auth.policy_snapshot_json,
        }

    @staticmethod
    def _build_provider_log_kwargs(provider_model: ProviderModel | None) -> dict[str, Any]:
        if provider_model is None:
            return {}
        return {
            "billing_multiplier": provider_model.price_multiplier,
            "channel_price_input_per_1k": provider_model.input_price_per_1k,
            "channel_price_output_per_1k": provider_model.output_price_per_1k,
            "channel_price_cache_per_1k": (
                provider_model.cache_price_per_1k
                if provider_model.cache_price_per_1k is not None
                else provider_model.input_price_per_1k
            ),
        }

    @staticmethod
    def list_models(
        db: Session,
        *,
        route_context: RoutePolicyContext | None = None,
        api_client_auth: ApiClientAuthContext | None = None,
    ) -> dict[str, Any]:
        setting = SettingService.get_or_create(db)
        cache_key = "v1-models|" + "|".join(
            [
                ",".join(str(item) for item in (route_context.allowed_provider_ids or [])) if route_context else "",
                ",".join(str(item) for item in (route_context.preferred_provider_ids or [])) if route_context else "",
                ",".join(route_context.preferred_region_tags or []) if route_context else "",
                ",".join(
                    str(item)
                    for item in (
                        safeJsonParse(api_client_auth.api_client_key.allowed_model_names_json)
                        if api_client_auth is not None
                        else []
                    )
                    or []
                ),
            ]
        )
        cached = CacheService.get(cache_key)
        if cached is not None:
            return cached
        model_set = {
            candidate.provider_model.model_name
            for candidate in RouterService.get_available_candidates(db, route_context=route_context)
            if api_client_auth is None or ApiKeyService.is_model_allowed(api_client_auth.api_client_key, candidate.provider_model.model_name)
        }
        payload = {
            "object": "list",
            "data": [{"id": model_name, "object": "model", "owned_by": "aotu-gpt", "permission": []} for model_name in sorted(model_set)],
        }
        return CacheService.set(cache_key, payload, ttl_seconds=max(0, int(setting.model_list_cache_ttl_sec)))

    @staticmethod
    def _list_models_with_scoped_session(
        *,
        route_context: RoutePolicyContext | None = None,
        api_client_auth: ApiClientAuthContext | None = None,
    ) -> dict[str, Any]:
        db = SessionLocal()
        try:
            return ProxyService.list_models(db, route_context=route_context, api_client_auth=api_client_auth)
        finally:
            db.close()

    @staticmethod
    async def async_list_models(
        *,
        route_context: RoutePolicyContext | None = None,
        api_client_auth: ApiClientAuthContext | None = None,
    ) -> dict[str, Any]:
        return await run_in_threadpool(
            ProxyService._list_models_with_scoped_session,
            route_context=route_context,
            api_client_auth=api_client_auth,
        )

    @staticmethod
    async def retrieve_response(
        *,
        response_id: str,
        query_items: list[tuple[str, str]] | None = None,
        route_context: RoutePolicyContext | None = None,
        api_client_auth: ApiClientAuthContext | None = None,
        trace_id: str | None = None,
        source_ip: str | None = None,
    ) -> tuple[dict[str, Any], Provider, list[dict], int]:
        return await ProxyService._forward_response_management_request(
            method="GET",
            response_id=response_id,
            action=None,
            query_items=query_items,
            route_context=route_context,
            api_client_auth=api_client_auth,
            trace_id=trace_id,
            source_ip=source_ip,
        )

    @staticmethod
    async def cancel_response(
        *,
        response_id: str,
        route_context: RoutePolicyContext | None = None,
        api_client_auth: ApiClientAuthContext | None = None,
        trace_id: str | None = None,
        source_ip: str | None = None,
    ) -> tuple[dict[str, Any], Provider, list[dict], int]:
        return await ProxyService._forward_response_management_request(
            method="POST",
            response_id=response_id,
            action="cancel",
            query_items=None,
            route_context=route_context,
            api_client_auth=api_client_auth,
            trace_id=trace_id,
            source_ip=source_ip,
        )

    @staticmethod
    async def _forward_response_management_request(
        *,
        method: str,
        response_id: str,
        action: str | None,
        query_items: list[tuple[str, str]] | None,
        route_context: RoutePolicyContext | None,
        api_client_auth: ApiClientAuthContext | None,
        trace_id: str | None,
        source_ip: str | None,
    ) -> tuple[dict[str, Any], Provider, list[dict], int]:
        total_started = time.perf_counter()
        request_id = uuid4().hex
        db = SessionLocal()
        try:
            providers = ProxyService._ordered_response_management_providers(db, route_context=route_context)
            if not providers:
                detail = {
                    "message": "No available provider for responses management request",
                    "code": "response_provider_not_available",
                }
                await ProxyService._write_response_management_log(
                    method=method,
                    request_path=f"/responses/{response_id}" + (f"/{action}" if action else ""),
                    response_id=response_id,
                    request_id=request_id,
                    provider=None,
                    trace=[],
                    trace_id=trace_id,
                    source_ip=source_ip,
                    api_client_auth=api_client_auth,
                    success=False,
                    status_code=status.HTTP_404_NOT_FOUND,
                    latency_ms=int((time.perf_counter() - total_started) * 1000),
                    upstream_request_id=None,
                    detail=detail,
                )
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)
        finally:
            db.close()

        last_error: tuple[int, Any] | None = None
        trace: list[dict] = []
        request_path = f"/responses/{response_id}"
        if action:
            request_path = f"{request_path}/{action}"

        for provider in providers:
            started = time.perf_counter()
            try:
                async with ProviderCapacityService.async_lease(provider, is_stream=False):
                    response_json, upstream_request_id = await ProxyService._send_response_management_request(
                        provider,
                        method=method,
                        request_path=request_path,
                        query_items=query_items,
                    )
                latency_ms = int((time.perf_counter() - started) * 1000)
                trace.append(
                    {
                        "provider_id": provider.id,
                        "provider_name": provider.name,
                        "request_path": f"/v1{request_path}",
                        "result": "success",
                        "latency_ms": latency_ms,
                        "status_code": 200,
                        "upstream_request_id": upstream_request_id,
                    }
                )
                await ProxyService._write_response_management_log(
                    method=method,
                    request_path=request_path,
                    response_id=response_id,
                    request_id=request_id,
                    provider=provider,
                    trace=trace,
                    trace_id=trace_id,
                    source_ip=source_ip,
                    api_client_auth=api_client_auth,
                    success=True,
                    status_code=200,
                    latency_ms=latency_ms,
                    upstream_request_id=upstream_request_id,
                    detail=None,
                )
                return response_json, provider, trace, latency_ms
            except ProviderCapacityExceededError as exc:
                latency_ms = int((time.perf_counter() - started) * 1000)
                detail = {
                    "message": str(exc),
                    "code": exc.code,
                    "provider_id": provider.id,
                    "provider_name": provider.name,
                }
                trace.append(
                    {
                        "provider_id": provider.id,
                        "provider_name": provider.name,
                        "request_path": f"/v1{request_path}",
                        "result": "capacity_limited",
                        "latency_ms": latency_ms,
                        "status_code": status.HTTP_429_TOO_MANY_REQUESTS,
                        "error": ProxyService._error_message_for_log(detail),
                    }
                )
                last_error = (status.HTTP_429_TOO_MANY_REQUESTS, detail)
                continue
            except ProviderCapacityUnavailableError as exc:
                latency_ms = int((time.perf_counter() - started) * 1000)
                detail = {
                    "message": "Provider capacity state is unavailable",
                    "code": "provider_capacity_unavailable",
                    "provider_id": provider.id,
                    "provider_name": provider.name,
                }
                trace.append(
                    {
                        "provider_id": provider.id,
                        "provider_name": provider.name,
                        "request_path": f"/v1{request_path}",
                        "result": "capacity_unavailable",
                        "latency_ms": latency_ms,
                        "status_code": status.HTTP_503_SERVICE_UNAVAILABLE,
                        "error": str(exc),
                    }
                )
                await ProxyService._write_response_management_log(
                    method=method,
                    request_path=request_path,
                    response_id=response_id,
                    request_id=request_id,
                    provider=provider,
                    trace=trace,
                    trace_id=trace_id,
                    source_ip=source_ip,
                    api_client_auth=api_client_auth,
                    success=False,
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    latency_ms=int((time.perf_counter() - total_started) * 1000),
                    upstream_request_id=None,
                    detail=detail,
                )
                raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail) from exc
            except httpx.HTTPStatusError as exc:
                latency_ms = int((time.perf_counter() - started) * 1000)
                error_body = await ProxyService._extract_response_error(exc.response)
                detail = ProxyService._normalize_error_detail(error_body)
                trace.append(
                    {
                        "provider_id": provider.id,
                        "provider_name": provider.name,
                        "request_path": f"/v1{request_path}",
                        "result": ProxyService._classify_http_error(exc.response.status_code),
                        "latency_ms": latency_ms,
                        "status_code": exc.response.status_code,
                        "error": ProxyService._error_message_for_log(detail),
                    }
                )
                last_error = (exc.response.status_code, detail)
                if ProxyService._should_continue_response_management_lookup(exc.response.status_code, detail):
                    continue
                await ProxyService._write_response_management_log(
                    method=method,
                    request_path=request_path,
                    response_id=response_id,
                    request_id=request_id,
                    provider=provider,
                    trace=trace,
                    trace_id=trace_id,
                    source_ip=source_ip,
                    api_client_auth=api_client_auth,
                    success=False,
                    status_code=exc.response.status_code,
                    latency_ms=int((time.perf_counter() - total_started) * 1000),
                    upstream_request_id=None,
                    detail=detail,
                )
                raise HTTPException(status_code=exc.response.status_code, detail=detail) from exc
            except RequestsUpstreamHTTPError as exc:
                latency_ms = int((time.perf_counter() - started) * 1000)
                trace.append(
                    {
                        "provider_id": provider.id,
                        "provider_name": provider.name,
                        "request_path": f"/v1{request_path}",
                        "result": ProxyService._classify_http_error(exc.status_code),
                        "latency_ms": latency_ms,
                        "status_code": exc.status_code,
                        "error": ProxyService._error_message_for_log(exc.detail),
                    }
                )
                last_error = (exc.status_code, exc.detail)
                if ProxyService._should_continue_response_management_lookup(exc.status_code, exc.detail):
                    continue
                await ProxyService._write_response_management_log(
                    method=method,
                    request_path=request_path,
                    response_id=response_id,
                    request_id=request_id,
                    provider=provider,
                    trace=trace,
                    trace_id=trace_id,
                    source_ip=source_ip,
                    api_client_auth=api_client_auth,
                    success=False,
                    status_code=exc.status_code,
                    latency_ms=int((time.perf_counter() - total_started) * 1000),
                    upstream_request_id=None,
                    detail=exc.detail,
                )
                raise HTTPException(status_code=exc.status_code, detail=exc.detail)

        if last_error is not None:
            status_code, detail = last_error
            await ProxyService._write_response_management_log(
                method=method,
                request_path=request_path,
                response_id=response_id,
                request_id=request_id,
                provider=None,
                trace=trace,
                trace_id=trace_id,
                source_ip=source_ip,
                api_client_auth=api_client_auth,
                success=False,
                status_code=status_code,
                latency_ms=int((time.perf_counter() - total_started) * 1000),
                upstream_request_id=None,
                detail=detail,
            )
            raise HTTPException(status_code=status_code, detail=detail)
        detail = {
            "message": "Response was not found in any authorized provider",
            "code": "response_not_found",
            "response_id": response_id,
        }
        await ProxyService._write_response_management_log(
            method=method,
            request_path=request_path,
            response_id=response_id,
            request_id=request_id,
            provider=None,
            trace=trace,
            trace_id=trace_id,
            source_ip=source_ip,
            api_client_auth=api_client_auth,
            success=False,
            status_code=status.HTTP_404_NOT_FOUND,
            latency_ms=int((time.perf_counter() - total_started) * 1000),
            upstream_request_id=None,
            detail=detail,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=detail,
        )

    @staticmethod
    async def _write_response_management_log(
        *,
        method: str,
        request_path: str,
        response_id: str,
        request_id: str,
        provider: Provider | None,
        trace: list[dict],
        trace_id: str | None,
        source_ip: str | None,
        api_client_auth: ApiClientAuthContext | None,
        success: bool,
        status_code: int,
        latency_ms: int,
        upstream_request_id: str | None,
        detail: Any | None,
    ) -> None:
        await ProxyService._run_db_write(
            LogService.create_log,
            log_type="responses",
            provider_id=provider.id if provider is not None else None,
            provider_name=provider.name if provider is not None else None,
            trace_id=trace_id,
            requested_model=None,
            tenant_name=api_client_auth.api_client_key.tenant_name if api_client_auth else None,
            project_name=api_client_auth.api_client_key.project_name if api_client_auth else None,
            app_name=api_client_auth.api_client_key.app_name if api_client_auth else None,
            environment_name=api_client_auth.api_client_key.environment_name if api_client_auth else None,
            request_id=request_id,
            conversation_key=response_id,
            session_id=response_id,
            source_ip=source_ip,
            request_path=f"/v1{request_path}",
            http_method=method.upper(),
            is_stream=False,
            has_image=False,
            success=success,
            status_code=status_code,
            latency_ms=latency_ms,
            duration_ms=latency_ms,
            upstream_request_id=upstream_request_id,
            message=None if success else ProxyService._error_message_for_log(detail),
            error_type=None if success else ProxyService._error_type_from_status(status_code),
            error_code=None if success else ProxyService._error_code_from_detail(detail),
            retryable=None if success else ProxyService._is_retryable_status(status_code),
            **ProxyService._build_api_client_log_kwargs(api_client_auth, auth_result="authenticated"),
            trace=trace,
            attempt_count=ProxyService._attempt_count(trace),
            schedule_token_fill=False,
            token_request_payload=None,
        )

    @staticmethod
    def _ordered_response_management_providers(
        db: Session,
        *,
        route_context: RoutePolicyContext | None,
    ) -> list[Provider]:
        providers = [
            item
            for item in ProviderService.list_providers(db)
            if item.enabled and item.provider_type == "openai_compatible"
        ]
        allowed_provider_ids = (
            set(route_context.allowed_provider_ids)
            if route_context is not None and route_context.allowed_provider_ids is not None
            else None
        )
        if allowed_provider_ids is not None:
            providers = [item for item in providers if item.id in allowed_provider_ids]

        default_provider_id = route_context.default_provider_id if route_context is not None else None
        preferred_provider_ids = (
            list(route_context.preferred_provider_ids)
            if route_context is not None and route_context.preferred_provider_ids
            else []
        )
        ordered_ids: list[int] = []
        if default_provider_id is not None:
            ordered_ids.append(default_provider_id)
        ordered_ids.extend(item for item in preferred_provider_ids if item not in ordered_ids)

        ordered: list[Provider] = []
        seen: set[int] = set()
        for provider_id in ordered_ids:
            provider = next((item for item in providers if item.id == provider_id), None)
            if provider is None or provider.id in seen:
                continue
            ordered.append(provider)
            seen.add(provider.id)
        for provider in providers:
            if provider.id in seen:
                continue
            ordered.append(provider)
            seen.add(provider.id)
        return ordered

    @staticmethod
    async def _send_response_management_request(
        provider: Provider,
        *,
        method: str,
        request_path: str,
        query_items: list[tuple[str, str]] | None,
    ) -> tuple[dict[str, Any], str | None]:
        client = UpstreamClientService.get_client()
        response = await client.request(
            method.upper(),
            f"{provider.base_url}{request_path}",
            headers={"Authorization": f"Bearer {provider.api_key}"},
            params=query_items,
            timeout=ProxyService._build_httpx_timeout(provider, payload={}, is_stream=False),
        )
        response.raise_for_status()
        if response.content:
            return response.json(), ProxyService._extract_upstream_request_id(response)
        return {"id": request_path.rsplit("/", 1)[-1], "object": "response"}, ProxyService._extract_upstream_request_id(response)

    @staticmethod
    def _should_continue_response_management_lookup(status_code: int, detail: Any) -> bool:
        if status_code == status.HTTP_404_NOT_FOUND:
            return True
        if status_code not in {status.HTTP_400_BAD_REQUEST, status.HTTP_409_CONFLICT}:
            return False
        message = ProxyService._error_message_for_log(detail).strip().lower()
        continue_tokens = (
            "not found",
            "no such",
            "unknown response",
            "response not found",
            "invalid response",
            "unsupported",
            "not support",
            "not_supported",
        )
        return any(token in message for token in continue_tokens)

    @staticmethod
    def _error_type_from_status(status_code: int) -> str:
        if status_code in {401, 403}:
            return "authentication_error"
        if status_code == 429:
            return "rate_limit_error"
        if status_code >= 500:
            return "server_error"
        return "invalid_request_error"

    @staticmethod
    def _is_retryable_status(status_code: int) -> bool:
        return status_code in {408, 409, 429} or status_code >= 500

    @staticmethod
    def _error_code_from_detail(detail: Any) -> str | None:
        if isinstance(detail, dict):
            if isinstance(detail.get("code"), str):
                return detail["code"]
            if isinstance(detail.get("error"), dict) and isinstance(detail["error"].get("code"), str):
                return detail["error"]["code"]
        return None
