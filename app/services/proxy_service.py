import asyncio
import base64
import hashlib
import logging
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import aiohttp
import httpx
import requests
from requests.adapters import HTTPAdapter
from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.concurrency import run_in_threadpool

from app.config import get_settings
from app.database import SessionLocal
from app.logging.adapters.asset_adapter import AssetLogRecorder
from app.models.model_catalog import ModelCatalog
from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.services.api_key_service import ApiKeyService
from app.services.api_key_service import ApiClientAuthContext
from app.services.billing_service import BillingService
from app.services.cache_service import CacheService
from app.services.content_guard_service import ContentGuardResult, ContentGuardService
from app.services.content_runtime_guard_service import ContentRuntimeGuardService
from app.services.log_service import LogService
from app.services.model_mapping_service import ModelMappingResolution, ModelMappingService
from app.services.model_pricing_service import ModelPricingService
from app.services.openai_error_service import OpenAIErrorService
from app.services.provider_capacity_service import (
    ProviderCapacityExceededError,
    ProviderCapacityService,
    ProviderCapacityUnavailableError,
)
from app.services.provider_health_state_service import ProviderHealthStateService
from app.services.provider_service import ProviderService
from app.services.request_log_queue_service import RequestLogQueueService
from app.services.router_service import RoutePolicyContext, RouterService
from app.services.setting_service import SettingService
from app.services.token_usage_service import TokenUsageService
from app.services.upstream_client import AiohttpJsonResponse, AiohttpStreamResponse, UpstreamClientService
from app.utils.json_utils import dumps_json, loads_json, safeJsonParse
from app.utils.request_body_structure import summarize_request_body_structure


logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PreparedUpstreamRequest:
    """描述准备发送到上游模型服务的请求。"""

    request_path: str
    request_payload: dict[str, Any]
    adapt_chat_response_to_responses: bool = False
    adapt_responses_response_to_chat: bool = False
    adapt_chat_response_to_completions: bool = False
    fallback_from_path: str | None = None
    response_model_override: str | None = None
    response_id_override: str | None = None


@dataclass(slots=True)
class RequestsUpstreamHTTPError(Exception):
    """表示上游 HTTP 错误及其响应详情。"""

    status_code: int
    detail: Any


@dataclass(slots=True)
class NonStreamResponseTooLarge(Exception):
    """表示非流式响应超过系统允许的大小限制。"""

    status_code: int
    detail: dict[str, Any]


@dataclass(slots=True)
class EndpointConversionSafety:
    """描述端点协议转换是否安全。"""

    safe: bool
    code: str | None = None
    message: str | None = None
    unsafe_fields: list[str] | None = None
    unsafe_reasons: list[str] | None = None


@dataclass(slots=True)
class StreamTimeoutPolicy:
    """定义流式请求的各阶段超时策略。"""

    first_token_timeout_seconds: int
    idle_timeout_seconds: int
    max_duration_seconds: int


class StreamTimeoutError(Exception):
    """表示流式请求超时，并携带业务错误码。"""

    def __init__(self, *, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ContentGuardBlockedError(Exception):
    """表示当前候选渠道的响应被内容完整性防护阻断。"""

    def __init__(
        self,
        *,
        guard_result: ContentGuardResult,
        status_code: int = status.HTTP_502_BAD_GATEWAY,
        detail: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(guard_result.reason or "content integrity violation")
        self.guard_result = guard_result
        self.status_code = status_code
        self.detail = detail or {}


UpstreamStreamResponse = httpx.Response | AiohttpStreamResponse
UpstreamJsonResponse = httpx.Response | AiohttpJsonResponse


class RouteExhaustedRetrySignal(Exception):
    """用于把候选耗尽等待重试从递归调用改为外层循环。"""

    def __init__(
        self,
        *,
        route_retry_started_at: float,
        route_retry_round: int,
        route_retry_trace: list[dict],
        route_retry_attempt_count: int,
    ) -> None:
        super().__init__("route exhausted retry requested")
        self.route_retry_started_at = route_retry_started_at
        self.route_retry_round = route_retry_round
        self.route_retry_trace = route_retry_trace
        self.route_retry_attempt_count = route_retry_attempt_count


class ProxyService:
    """处理鉴权、路由、转发、限流和响应适配的核心代理服务。"""

    LEGACY_IMAGE_OUTER_MODEL_CANDIDATES = ("gpt-5.4", "gpt-5", "gpt-5-mini", "gpt-4.1", "gpt-4o", "gpt-4o-mini")
    LEGACY_IMAGE_DEFAULT_TOOL_MODEL = "gpt-image-2"
    _provider_success_update_at: dict[tuple[int, int], float] = {}
    _requests_session_local = threading.local()
    _model_catalog_limit_locks: dict[str, threading.Lock] = {}
    _model_catalog_limit_locks_guard = threading.Lock()
    RESPONSES_CHAT_ADAPTER_SAFE_FIELDS = {
        "model",
        "instructions",
        "input",
        "tools",
        "tool_choice",
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
    RESPONSES_CHAT_ADAPTER_MAPPABLE_FIELDS = {
        "reasoning",
        "reasoning_effort",
        "store",
        "include",
        "parallel_tool_calls",
        "prompt_cache_key",
        "client_metadata",
    }
    CHAT_RESPONSES_ADAPTER_SAFE_FIELDS = {
        "model",
        "messages",
        "tools",
        "tool_choice",
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
        """在独立数据库会话中读取系统设置。"""
        return SettingService.get_cached()

    @staticmethod
    async def _get_setting_async(db: Session | None = None):
        """异步读取系统设置，兼容复用外部会话或自建会话。"""
        if db is not None:
            return await run_in_threadpool(SettingService.get_or_create, db)
        return ProxyService._get_setting_with_scoped_session()

    @staticmethod
    def _with_content_guard_route_policy(
        route_context: RoutePolicyContext | None,
        *,
        payload: dict[str, Any],
        endpoint_path: str | None,
        has_image: bool,
        require_tools: bool,
    ) -> RoutePolicyContext | None:
        if route_context is None:
            route_context = RoutePolicyContext()
        if not ContentGuardService.requires_trusted_provider(
            payload=payload,
            endpoint_path=endpoint_path,
            has_image=has_image,
            require_tools=require_tools,
        ):
            return route_context
        return RoutePolicyContext(
            allowed_provider_ids=list(route_context.allowed_provider_ids) if route_context.allowed_provider_ids is not None else None,
            forced_provider_id=route_context.forced_provider_id,
            preferred_provider_ids=list(route_context.preferred_provider_ids) if route_context.preferred_provider_ids is not None else None,
            preferred_region_tags=list(route_context.preferred_region_tags) if route_context.preferred_region_tags is not None else None,
            latency_bias=route_context.latency_bias,
            success_rate_bias=route_context.success_rate_bias,
            cost_bias=route_context.cost_bias,
            require_trusted_provider=True,
            content_guard_required=True,
        )

    @staticmethod
    def _run_db_write_sync(operation, *args, **kwargs):
        """在独立事务中执行同步数据库写操作。"""
        if ProxyService._try_enqueue_request_log(operation, args=args, kwargs=kwargs):
            return None
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
        """在独立事务里构造并抛出最终业务异常。"""
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
        """异步包装数据库写操作，避免阻塞事件循环。"""
        if db is not None:
            return await run_in_threadpool(operation, db, *args, **kwargs)
        return await run_in_threadpool(ProxyService._run_db_write_sync, operation, *args, **kwargs)

    @staticmethod
    def _try_enqueue_request_log(operation, *, args: tuple, kwargs: dict[str, Any]) -> bool:
        if args:
            return False
        if operation is LogService.create_log:
            if kwargs.get("success") is False:
                return False
            return RequestLogQueueService.enqueue(**kwargs)
        if operation is ProxyService._create_success_log_with_provider_status:
            if not RequestLogQueueService.enqueue(**kwargs):
                return False
            ProxyService._try_mark_success_for_enqueued_log(kwargs)
            return True
        return False

    @staticmethod
    def _try_mark_success_for_enqueued_log(kwargs: dict[str, Any]) -> None:
        resolved_provider_model_id = kwargs.get("resolved_provider_model_id")
        provider_id = kwargs.get("provider_id")
        latency_ms = kwargs.get("latency_ms")
        if provider_id is None or resolved_provider_model_id is None or latency_ms is None:
            return
        if not ProxyService._should_update_provider_success(int(provider_id), int(resolved_provider_model_id)):
            return
        db = SessionLocal()
        try:
            ProxyService._mark_success_by_id(db, int(provider_id), int(resolved_provider_model_id), int(latency_ms))
            db.commit()
        except Exception as exc:
            db.rollback()
            logger.warning("Failed to update provider success status for enqueued log: %s", exc)
        finally:
            db.close()

    @staticmethod
    def _queue_or_create_request_log(db: Session, **kwargs):
        if RequestLogQueueService.enqueue(**kwargs):
            return None
        kwargs["auto_commit"] = False
        return LogService.create_log(db, **kwargs)

    @staticmethod
    def _get_model_catalog_limits(model_name: str | None) -> dict[str, int | bool | None]:
        """查询模型目录中的上下文窗口与能力上限。"""
        if not model_name:
            return {}
        cache_key = f"model-catalog-limits:{model_name}"
        cached = CacheService.get(cache_key)
        if isinstance(cached, dict):
            return cached
        with ProxyService._model_catalog_limit_lock(cache_key):
            cached = CacheService.get(cache_key)
            if isinstance(cached, dict):
                return cached
            return ProxyService._load_model_catalog_limits_uncached(model_name, cache_key=cache_key)

    @staticmethod
    def _model_catalog_limit_lock(cache_key: str) -> threading.Lock:
        with ProxyService._model_catalog_limit_locks_guard:
            lock = ProxyService._model_catalog_limit_locks.get(cache_key)
            if lock is None:
                lock = threading.Lock()
                ProxyService._model_catalog_limit_locks[cache_key] = lock
            return lock

    @staticmethod
    def _load_model_catalog_limits_uncached(model_name: str, *, cache_key: str) -> dict[str, int | bool | None]:
        db = SessionLocal()
        try:
            catalog = db.query(ModelCatalog).filter(ModelCatalog.model_name == model_name).first()
            if catalog is None:
                return CacheService.set(cache_key, {}, ttl_seconds=300)
            limits = {
                "context_window_tokens": catalog.context_window_tokens,
                "max_input_tokens": catalog.max_input_tokens,
                "max_output_tokens": catalog.max_output_tokens,
                "supports_chat_completions": catalog.supports_chat_completions,
                "supports_responses": catalog.supports_responses,
                "supports_tools": catalog.supports_tools,
            }
            return CacheService.set(cache_key, limits, ttl_seconds=300)
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
        """在正式转发前估算请求 Token，用于限额预检。"""
        fast_estimate = TokenUsageService.fast_estimate_request_tokens(payload, request_path=request_path)
        if fast_estimate is None:
            return None, "fast_failed"
        if nearest_limit > 0 and fast_estimate > nearest_limit:
            return fast_estimate, "fast"
        if nearest_limit > 0 and fast_estimate < int(nearest_limit * 0.75):
            return fast_estimate, "fast"
        try:
            # 接近阈值时再走精算，兼顾性能与误判成本。
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
        """在请求超出 Token 限制时构造统一错误响应。"""
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
        request_body_json: str | None,
        requested_model: str | None = None,
        request_path_for_log: str | None = None,
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
        ProxyService._append_auth_trace_event(trace, api_client_auth)
        ProxyService._append_validation_trace_event(
            trace,
            stage="token_precheck",
            passed=False,
            request_body_json=request_body_json,
            limit_value=detail.get("global_max_request_tokens") or detail.get("model_max_input_tokens") or detail.get("model_context_window_tokens"),
            actual_value=detail.get("estimated_request_tokens"),
            error_code=detail.get("code"),
            safe_detail=detail,
        )
        classified = ProxyService._classify_retry_policy(status_code, detail)
        ProxyService._append_typed_request_event(
            trace,
            "request_error_response",
            {
                "status_code": status_code,
                "error_type": ProxyService._error_type_from_status(status_code, detail),
                "error_code": detail.get("code"),
                "public_message": detail.get("message"),
                "category": classified.get("category"),
                "retryable": False,
                "recoverable": bool(classified.get("recoverable")),
                "diagnostic_sample_json": dumps_json(detail),
            },
            result="failed",
            severity="warning",
        )
        await ProxyService._run_db_write(
            LogService.create_log,
            db=db,
            log_type=log_type,
            trace_id=trace_id,
            model_name=model_name,
            requested_model=requested_model or model_name,
            tenant_name=api_client_auth.api_client_key.tenant_name if api_client_auth else None,
            project_name=api_client_auth.api_client_key.project_name if api_client_auth else None,
            app_name=api_client_auth.api_client_key.app_name if api_client_auth else None,
            environment_name=api_client_auth.api_client_key.environment_name if api_client_auth else None,
            request_id=request_id,
            conversation_key=None,
            session_id=None,
            request_path=request_path_for_log or f"/v1{endpoint_path}",
            source_ip=source_ip,
            http_method="POST",
            is_stream=is_stream,
            has_image=has_image,
            success=False,
            status_code=status_code,
            reasoning_level=reasoning_level,
            model_reasoning_effort=model_reasoning_effort,
            request_body_json=request_body_json,
            message=detail.get("message"),
            error_type=ProxyService._error_type_from_status(status_code, detail),
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
    def _estimate_preflight_request_cost(
        db: Session,
        *,
        provider_model: ProviderModel,
        payload: dict[str, Any],
        request_path: str,
        model_name: str | None,
    ) -> tuple[Decimal | None, int | None, int]:
        input_tokens, _ = ProxyService._estimate_request_tokens_for_precheck(
            payload,
            model_name=model_name,
            request_path=request_path,
            nearest_limit=0,
        )
        if input_tokens is None:
            return None, None, 0
        output_tokens = int(ProxyService._requested_output_token_limit(payload) or provider_model.max_output_tokens or 0)
        catalog = db.scalar(select(ModelCatalog).where(ModelCatalog.model_name == provider_model.model_name))
        if catalog is not None:
            prices = ModelPricingService.resolve_catalog_prices_for_provider(
                pricing_mode=catalog.pricing_mode,
                pricing_json=catalog.pricing_json,
                input_price_per_1k=catalog.input_price_per_1k,
                output_price_per_1k=catalog.output_price_per_1k,
                cache_price_per_1k=catalog.cache_price_per_1k,
                price_multiplier=provider_model.price_multiplier,
                prompt_tokens=input_tokens,
                completion_tokens=output_tokens,
            )
            input_price = prices.get("input_price_per_1k")
            output_price = prices.get("output_price_per_1k")
        else:
            input_price = provider_model.input_price_per_1k
            output_price = provider_model.output_price_per_1k
        if input_tokens > 0 and input_price is None:
            return None, input_tokens, output_tokens
        if output_tokens > 0 and output_price is None:
            return None, input_tokens, output_tokens
        estimated_cost = (
            (Decimal(max(0, input_tokens)) / Decimal("1000")) * BillingService.to_decimal(input_price)
            + (Decimal(max(0, output_tokens)) / Decimal("1000")) * BillingService.to_decimal(output_price)
        )
        return BillingService.to_decimal(estimated_cost), input_tokens, output_tokens

    @staticmethod
    def _precheck_owner_balance_for_candidate(
        db: Session,
        *,
        api_client_auth: ApiClientAuthContext | None,
        provider_model: ProviderModel,
        payload: dict[str, Any],
        request_path: str,
        model_name: str | None,
    ) -> dict[str, Any] | None:
        if api_client_auth is None:
            return None
        owner_user = api_client_auth.api_client_key.owner_user
        if owner_user is None:
            return None
        estimated_cost, estimated_input_tokens, estimated_output_tokens = ProxyService._estimate_preflight_request_cost(
            db,
            provider_model=provider_model,
            payload=payload,
            request_path=request_path,
            model_name=model_name,
        )
        if estimated_cost is None or estimated_cost <= Decimal("0"):
            return None
        available_balance = BillingService.to_decimal(owner_user.balance_amount) - BillingService.to_decimal(owner_user.frozen_amount)
        if available_balance >= estimated_cost:
            return None
        return {
            "message": "账户可用余额不足以覆盖本次请求的预估最大费用",
            "code": "insufficient_balance_for_estimated_request",
            "estimated_cost": BillingService.to_float(estimated_cost),
            "available_balance": BillingService.to_float(available_balance),
            "estimated_input_tokens": estimated_input_tokens,
            "estimated_output_tokens": estimated_output_tokens,
        }

    @staticmethod
    def _payload_uses_tools(payload: dict[str, Any]) -> bool:
        if ProxyService._value_has_callable_tool_definition(payload.get("tools")):
            return True
        if ProxyService._value_has_callable_function_definition(payload.get("functions")):
            return True
        if ProxyService._tool_choice_requires_tool(payload.get("tool_choice")):
            return True
        if ProxyService._function_call_requires_tool(payload.get("function_call")):
            return True
        return ProxyService._value_has_tool_context(payload.get("messages")) or ProxyService._value_has_tool_context(payload.get("input"))

    @staticmethod
    def _value_has_callable_tool_definition(value: Any) -> bool:
        if isinstance(value, list):
            return any(ProxyService._value_has_callable_tool_definition(item) for item in value)
        if not isinstance(value, dict):
            return False
        item_type = value.get("type")
        if isinstance(item_type, str) and item_type in {"function", "image_generation"}:
            return True
        if "function" in value and isinstance(value.get("function"), dict):
            return True
        return False

    @staticmethod
    def _value_has_callable_function_definition(value: Any) -> bool:
        if isinstance(value, list):
            return any(isinstance(item, dict) and isinstance(item.get("name"), str) and bool(item.get("name", "").strip()) for item in value)
        return isinstance(value, dict) and isinstance(value.get("name"), str) and bool(value.get("name", "").strip())

    @staticmethod
    def _tool_choice_requires_tool(value: Any) -> bool:
        if isinstance(value, str):
            return value in {"required"}
        if not isinstance(value, dict):
            return False
        item_type = value.get("type")
        if isinstance(item_type, str) and item_type in {"function", "image_generation"}:
            return True
        function = value.get("function")
        return isinstance(function, dict) and isinstance(function.get("name"), str) and bool(function.get("name", "").strip())

    @staticmethod
    def _function_call_requires_tool(value: Any) -> bool:
        if isinstance(value, str):
            return value not in {"", "none", "auto"}
        return isinstance(value, dict) and isinstance(value.get("name"), str) and bool(value.get("name", "").strip())

    @staticmethod
    def _payload_has_stateful_responses_context(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        previous_response_id = payload.get("previous_response_id")
        if isinstance(previous_response_id, str) and previous_response_id.strip():
            return True
        return ProxyService._value_has_key(payload, "encrypted_content")

    @staticmethod
    def _value_has_key(value: Any, key_name: str) -> bool:
        if isinstance(value, list):
            return any(ProxyService._value_has_key(item, key_name) for item in value)
        if isinstance(value, dict):
            if key_name in value:
                return True
            return any(ProxyService._value_has_key(item, key_name) for item in value.values())
        return False

    @staticmethod
    def _payload_uses_image_generation(payload: dict[str, Any]) -> bool:
        return ProxyService._value_has_image_generation_tool(payload.get("tools"))

    @staticmethod
    def _value_has_image_generation_tool(value: Any) -> bool:
        if isinstance(value, list):
            return any(ProxyService._value_has_image_generation_tool(item) for item in value)
        if isinstance(value, dict):
            item_type = value.get("type")
            if isinstance(item_type, str) and item_type == "image_generation":
                return True
            return any(ProxyService._value_has_image_generation_tool(item) for item in value.values())
        return False

    @staticmethod
    def _payload_needs_image_transport(payload: dict[str, Any]) -> bool:
        return ProxyService._payload_has_image(payload) or ProxyService._payload_uses_image_generation(payload)

    @staticmethod
    def _resolve_legacy_image_outer_model(
        legacy_payload: dict[str, Any],
        *,
        api_client_auth: ApiClientAuthContext | None = None,
        route_context: RoutePolicyContext | None = None,
    ) -> str:
        for key in ("response_model", "outer_model"):
            candidate = legacy_payload.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        allowed_models: list[str] = []
        if api_client_auth is not None:
            loaded_allowed_models = loads_json(api_client_auth.api_client_key.allowed_model_names_json, [])
            if isinstance(loaded_allowed_models, list):
                allowed_models = [str(item).strip() for item in loaded_allowed_models if str(item).strip()]
        allowed_provider_ids = (
            set(route_context.allowed_provider_ids)
            if route_context is not None and route_context.allowed_provider_ids is not None
            else None
        )
        available_model_names: list[str] = []
        db = SessionLocal()
        try:
            for provider in ProviderService.list_providers(db):
                if not provider.enabled or provider.provider_type != "openai_compatible":
                    continue
                if allowed_provider_ids is not None and provider.id not in allowed_provider_ids:
                    continue
                for provider_model in provider.provider_models:
                    if not provider_model.enabled:
                        continue
                    if getattr(provider_model, "circuit_state", "closed") == "open":
                        continue
                    if not ProviderService.provider_model_supports_image_generation(provider_model):
                        continue
                    model_name = str(provider_model.model_name or "").strip()
                    if not model_name:
                        continue
                    if allowed_models and model_name not in allowed_models:
                        continue
                    if model_name not in available_model_names:
                        available_model_names.append(model_name)
        finally:
            db.close()
        for candidate in ProxyService.LEGACY_IMAGE_OUTER_MODEL_CANDIDATES:
            if candidate in available_model_names:
                return candidate
        if available_model_names:
            return available_model_names[0]
        if allowed_models:
            for candidate in ProxyService.LEGACY_IMAGE_OUTER_MODEL_CANDIDATES:
                if candidate in allowed_models:
                    return candidate
            return allowed_models[0]
        return ProxyService.LEGACY_IMAGE_OUTER_MODEL_CANDIDATES[0]

    @staticmethod
    def _normalize_legacy_image_tool_model(value: Any) -> str:
        if isinstance(value, str) and value.strip():
            return value.strip()
        return ProxyService.LEGACY_IMAGE_DEFAULT_TOOL_MODEL

    @staticmethod
    def _normalize_legacy_image_output_format(value: Any) -> str:
        normalized = str(value or "").strip().lower()
        if normalized in {"", "auto"}:
            return "png"
        if normalized == "jpg":
            return "jpeg"
        if normalized not in {"png", "jpeg", "webp"}:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "message": "response_format 不支持该 output_format，仅支持 png、jpeg、webp",
                    "code": "invalid_image_output_format",
                },
            )
        return normalized

    @staticmethod
    def _normalize_legacy_image_response_format(value: Any) -> str:
        normalized = str(value or "").strip().lower()
        if normalized in {"", "b64_json"}:
            return "b64_json"
        if normalized == "url":
            return "url"
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "legacy Images 兼容层当前仅支持 response_format=b64_json 或 url",
                "code": "invalid_image_response_format",
            },
        )

    @staticmethod
    def parse_legacy_image_count(value: Any) -> int:
        if value in (None, "", False):
            return 1
        try:
            count = int(str(value).strip())
        except (TypeError, ValueError) as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"message": "n 必须是 1 到 10 的整数", "code": "invalid_image_count"},
            ) from exc
        if count < 1 or count > 10:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"message": "n 必须是 1 到 10 的整数", "code": "invalid_image_count"},
            )
        return count

    @staticmethod
    def _assert_legacy_images_not_streaming(legacy_payload: dict[str, Any]) -> None:
        if legacy_payload.get("stream") is True:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "message": "legacy /v1/images/* 兼容入口不支持 stream=true；如需流式生图，请改用 /v1/responses",
                    "code": "legacy_images_stream_not_supported",
                },
            )

    @staticmethod
    def build_legacy_image_generation_responses_payload(
        legacy_payload: dict[str, Any],
        *,
        api_client_auth: ApiClientAuthContext | None = None,
        route_context: RoutePolicyContext | None = None,
    ) -> tuple[dict[str, Any], str]:
        ProxyService._assert_legacy_images_not_streaming(legacy_payload)
        prompt = str(legacy_payload.get("prompt") or "").strip()
        if not prompt:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"message": "prompt 不能为空", "code": "invalid_image_prompt"},
            )
        response_format = ProxyService._normalize_legacy_image_response_format(legacy_payload.get("response_format"))
        tool: dict[str, Any] = {
            "type": "image_generation",
            "model": ProxyService._normalize_legacy_image_tool_model(legacy_payload.get("model")),
            "action": "generate",
            "output_format": ProxyService._normalize_legacy_image_output_format(legacy_payload.get("output_format")),
        }
        ProxyService.parse_legacy_image_count(legacy_payload.get("n"))
        for key in ("size", "quality", "background", "moderation"):
            value = legacy_payload.get(key)
            if isinstance(value, str) and value.strip():
                tool[key] = value.strip()
        compression = legacy_payload.get("output_compression")
        if compression not in (None, ""):
            try:
                tool["output_compression"] = int(str(compression).strip())
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"message": "output_compression 必须是整数", "code": "invalid_image_output_compression"},
                ) from exc
        payload: dict[str, Any] = {
            "model": ProxyService._resolve_legacy_image_outer_model(
                legacy_payload,
                api_client_auth=api_client_auth,
                route_context=route_context,
            ),
            "input": prompt,
            "tools": [tool],
            "tool_choice": {"type": "image_generation"},
        }
        user_value = legacy_payload.get("user")
        if isinstance(user_value, str) and user_value.strip():
            payload["user"] = user_value.strip()
        metadata_value = legacy_payload.get("metadata")
        if isinstance(metadata_value, dict):
            payload["metadata"] = metadata_value
        return payload, response_format

    @staticmethod
    def build_legacy_image_edit_responses_payload(
        legacy_payload: dict[str, Any],
        *,
        image_urls: list[str],
        mask_urls: list[str] | None = None,
        api_client_auth: ApiClientAuthContext | None = None,
        route_context: RoutePolicyContext | None = None,
    ) -> tuple[dict[str, Any], str]:
        ProxyService._assert_legacy_images_not_streaming(legacy_payload)
        prompt = str(legacy_payload.get("prompt") or "").strip()
        if not prompt:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"message": "prompt 不能为空", "code": "invalid_image_prompt"},
            )
        if not image_urls:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"message": "image 不能为空", "code": "missing_image_input"},
            )
        response_format = ProxyService._normalize_legacy_image_response_format(legacy_payload.get("response_format"))
        tool: dict[str, Any] = {
            "type": "image_generation",
            "model": ProxyService._normalize_legacy_image_tool_model(legacy_payload.get("model")),
            "action": "edit",
            "output_format": ProxyService._normalize_legacy_image_output_format(legacy_payload.get("output_format")),
        }
        ProxyService.parse_legacy_image_count(legacy_payload.get("n"))
        for key in ("size", "quality", "background", "moderation"):
            value = legacy_payload.get(key)
            if isinstance(value, str) and value.strip():
                tool[key] = value.strip()
        compression = legacy_payload.get("output_compression")
        if compression not in (None, ""):
            try:
                tool["output_compression"] = int(str(compression).strip())
            except (TypeError, ValueError) as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail={"message": "output_compression 必须是整数", "code": "invalid_image_output_compression"},
                ) from exc
        content: list[dict[str, Any]] = [{"type": "input_text", "text": prompt}]
        for image_url in image_urls:
            content.append({"type": "input_image", "image_url": image_url})
        if mask_urls:
            content[0]["text"] = (
                f"{prompt}\n\n附加约束：最后附带的是遮罩参考图，请优先只修改遮罩覆盖区域，未遮罩区域尽量保持不变。"
            )
            for mask_url in mask_urls:
                content.append({"type": "input_image", "image_url": mask_url})
        payload: dict[str, Any] = {
            "model": ProxyService._resolve_legacy_image_outer_model(
                legacy_payload,
                api_client_auth=api_client_auth,
                route_context=route_context,
            ),
            "input": [{"role": "user", "content": content}],
            "tools": [tool],
            "tool_choice": {"type": "image_generation"},
        }
        user_value = legacy_payload.get("user")
        if isinstance(user_value, str) and user_value.strip():
            payload["user"] = user_value.strip()
        metadata_value = legacy_payload.get("metadata")
        if isinstance(metadata_value, dict):
            payload["metadata"] = metadata_value
        return payload, response_format

    @staticmethod
    def build_legacy_image_variation_responses_payload(
        legacy_payload: dict[str, Any],
        *,
        image_urls: list[str],
        api_client_auth: ApiClientAuthContext | None = None,
        route_context: RoutePolicyContext | None = None,
    ) -> tuple[dict[str, Any], str]:
        if not image_urls:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"message": "image 不能为空", "code": "missing_image_input"},
            )
        variation_prompt = str(legacy_payload.get("prompt") or "").strip() or (
            "基于输入图片生成一个高保真变体：保留主体语义、主要构图和整体风格，但在细节、纹理、光线或小元素上做自然变化；不要添加无关文字。"
        )
        payload_for_edit = {
            **legacy_payload,
            "prompt": variation_prompt,
        }
        return ProxyService.build_legacy_image_edit_responses_payload(
            payload_for_edit,
            image_urls=image_urls,
            mask_urls=None,
            api_client_auth=api_client_auth,
            route_context=route_context,
        )

    @staticmethod
    def _value_has_tool_context(value: Any) -> bool:
        if isinstance(value, list):
            return any(ProxyService._value_has_tool_context(item) for item in value)
        if isinstance(value, dict):
            if any(key in value for key in ("tool_calls", "tool_call_id", "function_call")):
                return True
            item_type = value.get("type")
            if isinstance(item_type, str) and item_type in {"tool_call", "function_call", "tool_result", "function_call_output"}:
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
        if (
            provider_id is not None
            and resolved_provider_model_id is not None
            and latency_ms is not None
            and ProxyService._should_update_provider_success(provider_id, resolved_provider_model_id)
        ):
            ProxyService._mark_success_by_id(db, provider_id, resolved_provider_model_id, latency_ms)
        return LogService.create_log(db, *args, auto_commit=False, **kwargs)

    @staticmethod
    async def _inspect_non_stream_content_guard(
        *,
        db: Session | None,
        setting: Any,
        provider: Provider,
        provider_model: ProviderModel,
        endpoint_path: str,
        request_payload: dict[str, Any],
        response_payload: dict[str, Any],
        route_context: RoutePolicyContext | None,
    ) -> ContentGuardResult:
        return await ContentRuntimeGuardService.inspect_non_stream_response(
            db=db,
            setting=setting,
            provider=provider,
            provider_model=provider_model,
            endpoint_path=endpoint_path,
            request_payload=request_payload,
            response_payload=response_payload,
            route_context=route_context,
        )

    @staticmethod
    def _content_guard_enabled_for_request(
        *,
        setting: Any,
        provider: Provider,
        route_context: RoutePolicyContext | None,
    ) -> bool:
        return ContentRuntimeGuardService.enabled_for_request(
            setting=setting,
            provider=provider,
            route_context=route_context,
        )

    @staticmethod
    def _content_guard_high_risk_strategy(setting: Any) -> str:
        return ContentRuntimeGuardService.high_risk_strategy(setting)

    @staticmethod
    def _content_guard_stream_mode(setting: Any) -> str:
        return ContentRuntimeGuardService.stream_mode(setting)

    @staticmethod
    def _content_guard_should_block_for_response(guard_result: ContentGuardResult, *, setting: Any) -> bool:
        return ContentRuntimeGuardService.should_block_for_response(guard_result, setting=setting)

    @staticmethod
    def _content_guard_should_switch_provider(setting: Any) -> bool:
        return ContentRuntimeGuardService.should_switch_provider(setting)

    @staticmethod
    def _content_guard_retry_provider_count(trace: list[dict] | None) -> int:
        if not isinstance(trace, list):
            return 0
        provider_ids = {
            item.get("provider_id")
            for item in trace
            if isinstance(item, dict)
            and item.get("result") == "content_integrity_violation"
            and item.get("provider_id") is not None
        }
        return len(provider_ids)

    @staticmethod
    def _stream_content_guard_should_buffer(
        *,
        setting: Any,
        provider: Provider,
        route_context: RoutePolicyContext | None,
    ) -> bool:
        return ContentRuntimeGuardService.stream_should_buffer(
            setting=setting,
            provider=provider,
            route_context=route_context,
        )

    @staticmethod
    def _append_limited_bytes(buffer: bytearray, chunk: bytes, *, limit_bytes: int) -> None:
        if limit_bytes <= 0 or len(buffer) >= limit_bytes:
            return
        remaining = limit_bytes - len(buffer)
        buffer.extend(chunk[:remaining])

    @staticmethod
    async def _inspect_stream_guard_buffer(
        *,
        db: Session | None,
        setting: Any,
        provider: Provider,
        provider_model: ProviderModel,
        endpoint_path: str,
        request_payload: dict[str, Any],
        buffered_bytes: bytes,
    ) -> ContentGuardResult:
        return await ContentRuntimeGuardService.inspect_stream_prefetch_buffer(
            db=db,
            setting=setting,
            provider=provider,
            provider_model=provider_model,
            endpoint_path=endpoint_path,
            request_payload=request_payload,
            buffered_bytes=buffered_bytes,
        )

    @staticmethod
    async def _prefetch_stream_guard_buffer(
        *,
        db: Session | None,
        setting: Any,
        provider: Provider,
        provider_model: ProviderModel,
        endpoint_path: str,
        request_payload: dict[str, Any],
        chunk_iterator: AsyncIterator[bytes],
        started: float,
        limit_bytes: int,
        max_delay_seconds: float = 0.3,
        stop_at_first_event: bool = True,
    ) -> tuple[bytes, ContentGuardResult, int | None, bool, asyncio.Task[bytes] | None]:
        buffer = bytearray()
        first_chunk_latency_ms: int | None = None
        stream_ended = False
        prefetch_started = time.perf_counter()
        deadline = time.perf_counter() + min(0.5, max(0.0, max_delay_seconds))
        while len(buffer) < limit_bytes:
            remaining: float | None = None
            remaining = deadline - time.perf_counter()
            if remaining <= 0:
                break
            read_task = asyncio.create_task(chunk_iterator.__anext__())
            done, _ = await asyncio.wait({read_task}, timeout=remaining)
            if not done:
                guard_result = (
                    await ProxyService._inspect_stream_guard_buffer(
                        db=db,
                        setting=setting,
                        provider=provider,
                        provider_model=provider_model,
                        endpoint_path=endpoint_path,
                        request_payload=request_payload,
                        buffered_bytes=bytes(buffer),
                    )
                    if buffer
                    else ContentGuardResult(
                        result=ContentGuardService.RESULT_PASS,
                        risk_level="low",
                        reason="流式预检等待窗口内未收到可检测内容，转为后续分块检测",
                        action="allow",
                    )
                )
                guard_result.buffer_wait_ms = max(0, int((time.perf_counter() - prefetch_started) * 1000))
                return bytes(buffer), guard_result, first_chunk_latency_ms, stream_ended, read_task
            try:
                chunk = read_task.result()
            except StopAsyncIteration:
                stream_ended = True
                break
            if not chunk:
                continue
            if first_chunk_latency_ms is None:
                first_chunk_latency_ms = int((time.perf_counter() - started) * 1000)
            ProxyService._append_limited_bytes(buffer, chunk, limit_bytes=limit_bytes)
            if stop_at_first_event and (b"\n\n" in buffer or b"\r\n\r\n" in buffer):
                break
        if not buffer:
            guard_result = ContentGuardResult(
                result=ContentGuardService.RESULT_PASS,
                risk_level="low",
                reason="流式预检等待窗口内未收到可检测内容，转为后续分块检测",
                action="allow",
            )
            guard_result.buffer_wait_ms = max(0, int((time.perf_counter() - prefetch_started) * 1000))
            return bytes(buffer), guard_result, first_chunk_latency_ms, stream_ended, None
        guard_result = await ProxyService._inspect_stream_guard_buffer(
            db=db,
            setting=setting,
            provider=provider,
            provider_model=provider_model,
            endpoint_path=endpoint_path,
            request_payload=request_payload,
            buffered_bytes=bytes(buffer),
        )
        guard_result.buffer_wait_ms = max(0, int((time.perf_counter() - prefetch_started) * 1000))
        return bytes(buffer), guard_result, first_chunk_latency_ms, stream_ended, None

    @staticmethod
    def _inspect_stream_guard_chunk(
        *,
        event_buffer: bytearray,
        chunk: bytes,
        setting: Any,
        endpoint_path: str,
        request_payload: dict[str, Any] | None,
    ) -> ContentGuardResult:
        return ContentRuntimeGuardService.inspect_stream_chunk(
            event_buffer=event_buffer,
            chunk=chunk,
            setting=setting,
            endpoint_path=endpoint_path,
            request_payload=request_payload,
        )

    @staticmethod
    def _record_content_guard_violation_by_id(
        db: Session,
        provider_id: int,
        provider_model_id: int,
        guard_result: Any,
    ) -> None:
        provider = db.get(Provider, provider_id)
        provider_model = db.get(ProviderModel, provider_model_id)
        ContentGuardService.record_violation(
            db,
            provider=provider,
            provider_model=provider_model,
            result=guard_result,
            auto_commit=False,
        )

    @staticmethod
    async def _log_content_guard_violation(
        *,
        db: Session | None,
        provider: Provider,
        provider_model: ProviderModel,
        guard_result: Any,
        log_type: str,
        trace_id: str | None,
        model_name: str | None,
        requested_model: str | None,
        api_client_auth: ApiClientAuthContext | None,
        request_id: str,
        conversation_key: str,
        session_id: str,
        request_path: str,
        source_ip: str | None,
        is_stream: bool,
        has_image: bool,
        latency_ms: int,
        duration_ms: int,
        reasoning_level: str | None,
        model_reasoning_effort: str | None,
        attempt_count: int,
        request_body_json: str | None,
        response_body_json: str | None,
        trace: list[dict],
        request_payload: dict[str, Any],
    ) -> None:
        await ProxyService._run_db_write(
            LogService.create_log,
            db=db,
            log_type=log_type,
            trace_id=trace_id,
            provider_id=provider.id,
            provider_name=provider.name,
            model_name=model_name,
            requested_model=requested_model,
            tenant_name=api_client_auth.api_client_key.tenant_name if api_client_auth else None,
            project_name=api_client_auth.api_client_key.project_name if api_client_auth else None,
            app_name=api_client_auth.api_client_key.app_name if api_client_auth else None,
            environment_name=api_client_auth.api_client_key.environment_name if api_client_auth else None,
            request_id=request_id,
            conversation_key=conversation_key,
            session_id=session_id,
            resolved_provider_model_id=provider_model.id,
            request_path=request_path,
            source_ip=source_ip,
            http_method="POST",
            is_stream=is_stream,
            has_image=has_image,
            success=False,
            status_code=status.HTTP_502_BAD_GATEWAY,
            latency_ms=latency_ms,
            duration_ms=duration_ms,
            reasoning_level=reasoning_level,
            model_reasoning_effort=model_reasoning_effort,
            attempt_count=attempt_count,
            request_body_json=request_body_json,
            response_body_json=response_body_json,
            message=guard_result.reason or "content integrity violation",
            error_type="server_error",
            error_code="content_integrity_violation",
            retryable=False,
            **guard_result.to_log_kwargs(),
            content_guard_retry_provider_count=ProxyService._content_guard_retry_provider_count(trace),
            **ProxyService._build_api_client_log_kwargs(api_client_auth, auth_result="authenticated"),
            **ProxyService._build_provider_log_kwargs(provider_model),
            trace=trace,
            token_request_payload=request_payload,
            schedule_token_fill=False,
        )

    @staticmethod
    def _should_update_provider_success(provider_id: int, provider_model_id: int) -> bool:
        interval_ms = int(getattr(get_settings(), "provider_success_update_interval_ms", 1000) or 0)
        if interval_ms <= 0:
            return True
        key = (int(provider_id), int(provider_model_id))
        now_ms = time.monotonic() * 1000
        last_ms = ProxyService._provider_success_update_at.get(key, 0)
        if now_ms - last_ms < interval_ms:
            return False
        ProxyService._provider_success_update_at[key] = now_ms
        return True

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
    async def forward_json_request(db: Session | None = None, **kwargs) -> tuple[dict[str, Any], Provider, list[dict], int]:
        route_retry_started_at = kwargs.pop("route_retry_started_at", None)
        route_retry_round = int(kwargs.pop("route_retry_round", 0) or 0)
        route_retry_trace = kwargs.pop("route_retry_trace", None)
        route_retry_attempt_count = int(kwargs.pop("route_retry_attempt_count", 0) or 0)
        while True:
            try:
                return await ProxyService._forward_json_request_once(
                    db,
                    **kwargs,
                    route_retry_started_at=route_retry_started_at,
                    route_retry_round=route_retry_round,
                    route_retry_trace=route_retry_trace,
                    route_retry_attempt_count=route_retry_attempt_count,
                )
            except RouteExhaustedRetrySignal as signal:
                route_retry_started_at = signal.route_retry_started_at
                route_retry_round = signal.route_retry_round
                route_retry_trace = signal.route_retry_trace
                route_retry_attempt_count = signal.route_retry_attempt_count
    @staticmethod
    async def _forward_json_request_once(
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
        request_path_for_log: str | None = None,
        public_endpoint_path: str | None = None,
        response_transform: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
        suppress_success_log: bool = False,
        route_retry_started_at: float | None = None,
        route_retry_round: int = 0,
        route_retry_trace: list[dict] | None = None,
        route_retry_attempt_count: int = 0,
        mapping_failover_excluded_target_model_names: tuple[str, ...] = (),
        request_id_override: str | None = None,
        conversation_key_override: str | None = None,
        session_id_override: str | None = None,
    ) -> tuple[dict[str, Any], Provider, list[dict], int]:
        payload = ProxyService._normalize_reasoning_request_payload(endpoint_path=endpoint_path, payload=payload)
        request_payload_for_log = payload
        model_name = payload.get("model")
        requested_model_name = model_name
        has_image_input = ProxyService._payload_has_image(payload)
        has_image = ProxyService._payload_needs_image_transport(payload)
        require_tools = ProxyService._payload_uses_tools(payload)
        require_image_generation = ProxyService._payload_uses_image_generation(payload)
        route_context = ProxyService._with_content_guard_route_policy(
            route_context,
            payload=payload,
            endpoint_path=endpoint_path,
            has_image=has_image_input or has_image,
            require_tools=require_tools,
        )
        effective_public_endpoint_path = public_endpoint_path or endpoint_path
        effective_log_request_path = request_path_for_log or f"/v1{effective_public_endpoint_path}"
        setting = await ProxyService._get_setting_async(db)
        payload = ProxyService._mark_stream_usage_required(
            endpoint_path=endpoint_path,
            payload=payload,
            enable_token_logging=setting.enable_token_logging,
        )
        request_id = request_id_override or uuid4().hex
        session_sticky_key = session_id_override or ProxyService._extract_session_sticky_key(payload)
        reasoning_level = LogService.extract_reasoning_level(payload)
        model_reasoning_effort = LogService.extract_model_reasoning_effort(payload)
        require_chat_completions, require_responses = ProxyService._route_endpoint_requirements(endpoint_path, payload)
        trace: list[dict] = list(route_retry_trace or [])
        ProxyService._append_auth_trace_event(trace, api_client_auth)
        required_capabilities_json = ProxyService._request_capabilities_payload(
            endpoint_path=effective_log_request_path,
            is_stream=False,
            has_image=has_image_input or has_image,
            require_tools=require_tools,
            require_image_generation=require_image_generation,
            require_chat_completions=require_chat_completions,
            require_responses=require_responses,
        )
        if api_client_auth is not None and not ApiKeyService.is_model_allowed(api_client_auth.api_client_key, requested_model_name):
            request_body_json_for_rejection = ProxyService._serialize_payload_for_logging(
                request_payload_for_log,
                setting=setting,
                preserve_request_content_when_disabled=True,
                structure_only=True,
            )
            ProxyService._append_validation_trace_event(
                trace,
                stage="schema_validation",
                passed=True,
                request_body_json=request_body_json_for_rejection,
            )
            ProxyService._append_model_permission_trace_event(
                trace,
                requested_model=requested_model_name,
                resolved_model=model_name,
                endpoint_path=effective_log_request_path,
                permission_result="model_not_allowed",
                required_capabilities_json=required_capabilities_json,
                reason_details={"message": "Requested model is not allowed for this api key", "error_code": "model_not_allowed"},
            )
            await ProxyService._run_db_write(
                LogService.create_log,
                db=db,
                log_type=log_type,
                trace_id=trace_id,
                model_name=model_name,
                requested_model=requested_model_name,
                tenant_name=api_client_auth.api_client_key.tenant_name,
                project_name=api_client_auth.api_client_key.project_name,
                app_name=api_client_auth.api_client_key.app_name,
                environment_name=api_client_auth.api_client_key.environment_name,
                request_id=request_id,
                request_path=effective_log_request_path,
                source_ip=source_ip,
                http_method="POST",
                is_stream=False,
                has_image=has_image,
                success=False,
                status_code=status.HTTP_403_FORBIDDEN,
                reasoning_level=reasoning_level,
                model_reasoning_effort=model_reasoning_effort,
                request_body_json=request_body_json_for_rejection,
                message="Requested model is not allowed for this api key",
                error_type="invalid_request_error",
                error_code="model_not_allowed",
                retryable=False,
                **ProxyService._build_api_client_log_kwargs(api_client_auth, auth_result="authenticated"),
                trace=trace,
                attempt_count=0,
                token_request_payload=payload,
                schedule_token_fill=False,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"message": "Requested model is not allowed for this api key", "code": "model_not_allowed"},
            )
        mapping_resolution = await ModelMappingService.resolve_for_request(
            source_model_name=requested_model_name if isinstance(requested_model_name, str) else None,
            api_client_auth=api_client_auth,
            sticky_key=session_sticky_key,
            excluded_target_model_names=mapping_failover_excluded_target_model_names,
        )
        model_mapping_unavailable = ProxyService._model_mapping_unavailable(mapping_resolution)
        if mapping_resolution is not None and not model_mapping_unavailable and mapping_resolution.selected_model_name != requested_model_name:
            payload = {**payload, "model": mapping_resolution.selected_model_name}
            model_name = mapping_resolution.selected_model_name
        token_limit_error = ProxyService._build_request_token_limit_error(
            setting=setting,
            payload=payload,
            model_name=model_name,
            endpoint_path=effective_public_endpoint_path,
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
                requested_model=requested_model_name,
                request_path_for_log=effective_log_request_path,
                request_body_json=ProxyService._serialize_payload_for_logging(
                    request_payload_for_log,
                    setting=setting,
                    preserve_request_content_when_disabled=True,
                    structure_only=True,
                ),
            )
        long_output_error = ProxyService._build_long_output_error(
            setting=setting,
            payload=payload,
            model_name=model_name,
            endpoint_path=effective_public_endpoint_path,
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
                requested_model=requested_model_name,
                request_path_for_log=effective_log_request_path,
                request_body_json=ProxyService._serialize_payload_for_logging(
                    payload,
                    setting=setting,
                    preserve_request_content_when_disabled=True,
                    structure_only=True,
                ),
            )
        conversation_key = conversation_key_override or ProxyService._extract_conversation_key(payload, request_id)
        session_id = session_id_override or LogService.extract_session_id(payload, conversation_key=conversation_key, fallback=request_id)
        request_body_json = ProxyService._serialize_payload_for_logging(
            request_payload_for_log,
            setting=setting,
            preserve_request_content_when_disabled=True,
            structure_only=True,
        )
        if payload.get("stream") is True:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={"message": f"Use stream endpoint handler for {effective_public_endpoint_path}", "code": "invalid_stream_mode"},
            )

        ProxyService._append_model_mapping_trace(trace, mapping_resolution)
        ProxyService._append_stateful_responses_route_trace(
            trace,
            endpoint_path=endpoint_path,
            payload=payload,
            requested_model_name=requested_model_name,
            selected_model_name=model_name,
            mapping_resolution=mapping_resolution,
        )
        ProxyService._append_validation_trace_event(
            trace,
            stage="schema_validation",
            passed=True,
            request_body_json=request_body_json,
        )
        ProxyService._append_model_permission_trace_event(
            trace,
            requested_model=requested_model_name,
            resolved_model=model_name,
            endpoint_path=effective_log_request_path,
            permission_result="allowed" if not model_mapping_unavailable else "model_not_available",
            required_capabilities_json=required_capabilities_json,
            reason_details=(mapping_resolution.trace if mapping_resolution is not None and isinstance(mapping_resolution.trace, dict) else {}),
        )
        last_upstream_error: dict[str, Any] | None = None
        attempt_count = max(0, int(route_retry_attempt_count or 0))
        route_retry_started_at = route_retry_started_at or time.perf_counter()

        try:
            candidates = (
                []
                if model_mapping_unavailable
                else await RouterService.async_order_candidates(
                    db,
                    model_name=model_name,
                    sticky_key=session_sticky_key,
                    forced_provider_id=forced_provider_id,
                    route_context=route_context,
                    require_vision=has_image_input,
                    require_stream=False,
                    require_tools=require_tools,
                    require_image_generation=require_image_generation,
                    require_chat_completions=require_chat_completions,
                    require_responses=require_responses,
                )
            )
        except ProviderCapacityUnavailableError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"message": str(exc), "code": exc.code},
            ) from exc
        if candidates:
            first_candidate = candidates[0]
            ProxyService._append_route_decision_trace_event(
                trace,
                route_round=route_retry_round + 1,
                candidate_count=len(candidates),
                selected_provider_id=first_candidate.provider.id,
                selected_provider_model_id=first_candidate.provider_model.id,
                sticky_hit=bool(session_sticky_key),
            )
        if not candidates:
            route_diagnostics = (
                ProxyService._model_mapping_unavailable_diagnostics(mapping_resolution)
                if model_mapping_unavailable
                else await RouterService.async_diagnose_candidate_unavailability(
                    model_name=model_name,
                    forced_provider_id=forced_provider_id,
                    route_context=route_context,
                    require_vision=has_image_input,
                    require_stream=False,
                    require_tools=require_tools,
                    require_image_generation=require_image_generation,
                    require_chat_completions=require_chat_completions,
                    require_responses=require_responses,
                    is_stream=False,
                )
            )
            ProxyService._append_route_decision_trace_event(
                trace,
                route_round=route_retry_round + 1,
                candidate_count=0,
                diagnostics=route_diagnostics,
            )
            if model_mapping_unavailable:
                route_message, error_code = ProxyService._build_model_mapping_unavailable_error(route_diagnostics)
            elif require_image_generation:
                route_message = "No native image-generation-capable provider for requested model"
                error_code = "model_image_generation_not_available"
            else:
                route_message = "No available provider for requested model"
                error_code = "model_not_available"
            route_error_detail = ProxyService._build_route_unavailable_error_detail(
                route_diagnostics,
                message=route_message,
                code=error_code,
                endpoint_path=effective_public_endpoint_path,
                requested_model=requested_model_name if isinstance(requested_model_name, str) else None,
                selected_model=model_name if isinstance(model_name, str) else None,
                requires_tools=require_tools,
                requires_image_generation=require_image_generation,
                model_mapping_unavailable=model_mapping_unavailable,
            )
            mapped_retry = await ProxyService._retry_with_next_mapped_model_json(
                db=db,
                endpoint_path=endpoint_path,
                payload=payload,
                log_type=log_type,
                forced_provider_id=forced_provider_id,
                route_context=route_context,
                api_client_auth=api_client_auth,
                trace_id=trace_id,
                source_ip=source_ip,
                request_path_for_log=effective_log_request_path,
                public_endpoint_path=public_endpoint_path,
                response_transform=response_transform,
                suppress_success_log=suppress_success_log,
                route_retry_started_at=route_retry_started_at,
                route_retry_round=route_retry_round,
                route_retry_trace=trace,
                route_retry_attempt_count=attempt_count,
                mapping_resolution=mapping_resolution,
                requested_model_name=requested_model_name if isinstance(requested_model_name, str) else None,
                current_model_name=model_name if isinstance(model_name, str) else None,
                excluded_target_model_names=mapping_failover_excluded_target_model_names,
                request_id=request_id,
                conversation_key=conversation_key,
                session_id=session_id,
                reason="no_route_candidate_for_mapped_target",
                route_diagnostics=route_diagnostics,
            )
            if mapped_retry is not None:
                return mapped_retry
            retryable_route_exhausted = ProxyService._should_retry_route_diagnostics(route_diagnostics)
            sleep_seconds = (
                ProxyService._route_exhausted_retry_sleep_seconds(
                    setting,
                    started_at=route_retry_started_at,
                    retry_round=route_retry_round,
                    route_context=route_context,
                )
                if retryable_route_exhausted
                else 0.0
            )
            if sleep_seconds > 0:
                ProxyService._append_route_exhausted_retry_trace(
                    trace,
                    reason="route_candidates_exhausted",
                    sleep_seconds=sleep_seconds,
                    started_at=route_retry_started_at,
                    retry_round=route_retry_round,
                    setting=setting,
                    route_context=route_context,
                    diagnostics=route_diagnostics,
                )
                await asyncio.sleep(sleep_seconds)
                raise RouteExhaustedRetrySignal(
                    route_retry_started_at=route_retry_started_at,
                    route_retry_round=route_retry_round + 1,
                    route_retry_trace=trace,
                    route_retry_attempt_count=attempt_count,
                )
            if retryable_route_exhausted and not ProxyService._route_exhausted_retry_infinite_enabled(setting, route_context) and ProxyService._route_exhausted_retry_max_wait_seconds(setting) > 0:
                await ProxyService._raise_final_error_async(
                    db,
                    model_name=model_name,
                    endpoint_path=endpoint_path,
                    log_type=log_type,
                    trace=trace + [{"result": "route_candidates_exhausted", "diagnostic": route_diagnostics}],
                    upstream_error=ProxyService._build_route_exhausted_retry_upstream_error(
                        setting,
                        started_at=route_retry_started_at,
                        attempt_count=attempt_count,
                        trace=trace,
                        trace_id=trace_id,
                        last_upstream_error=None,
                    ),
                    requested_model=requested_model_name,
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
                    request_path_for_log=effective_log_request_path,
                )
            await ProxyService._run_db_write(
                LogService.create_log,
                log_type=log_type,
                trace_id=trace_id,
                model_name=model_name,
                requested_model=requested_model_name,
                tenant_name=api_client_auth.api_client_key.tenant_name if api_client_auth else None,
                project_name=api_client_auth.api_client_key.project_name if api_client_auth else None,
                app_name=api_client_auth.api_client_key.app_name if api_client_auth else None,
                environment_name=api_client_auth.api_client_key.environment_name if api_client_auth else None,
                request_id=request_id,
                conversation_key=conversation_key,
                session_id=session_id,
                request_path=effective_log_request_path,
                source_ip=source_ip,
                http_method="POST",
                is_stream=False,
                has_image=has_image,
                success=False,
                status_code=status.HTTP_404_NOT_FOUND,
                reasoning_level=reasoning_level,
                model_reasoning_effort=model_reasoning_effort,
                request_body_json=request_body_json,
                message=route_message,
                error_type="invalid_request_error",
                error_code=error_code,
                retryable=False,
                **ProxyService._build_api_client_log_kwargs(api_client_auth, auth_result="authenticated"),
                trace=trace + [{"result": "route_candidates_exhausted", "diagnostic": route_diagnostics}],
                attempt_count=attempt_count,
                token_request_payload=payload,
                schedule_token_fill=setting.enable_token_logging,
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=route_error_detail,
            )

        for candidate in candidates:
            provider = candidate.provider
            provider_model = candidate.provider_model
            balance_rejection = ProxyService._precheck_owner_balance_for_candidate(
                db,
                api_client_auth=api_client_auth,
                provider_model=provider_model,
                payload=payload,
                request_path=effective_log_request_path,
                model_name=model_name,
            )
            if balance_rejection is not None:
                trace.append(
                    ProxyService._build_trace_item(
                        provider,
                        provider_model,
                        "insufficient_balance_precheck",
                        0,
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        error=balance_rejection.get("message"),
                    )
                )
                last_upstream_error = {
                    "status_code": status.HTTP_429_TOO_MANY_REQUESTS,
                    "detail": balance_rejection,
                }
                continue
            retries = ProxyService._same_provider_retry_budget(provider, setting)
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
                    client_response = response_transform(response) if response_transform is not None else response
                    client_response = ProxyService._restore_mapped_response_model(
                        client_response,
                        mapping_resolution=mapping_resolution,
                        requested_model=requested_model_name,
                    )
                    guard_result = await ProxyService._inspect_non_stream_content_guard(
                        db=db,
                        setting=setting,
                        provider=provider,
                        provider_model=provider_model,
                        endpoint_path=endpoint_path,
                        request_payload=request_payload_for_log,
                        response_payload=client_response,
                        route_context=route_context,
                    )
                    finish_reason = ProxyService._extract_finish_reason(response)
                    response_body_json = ProxyService._serialize_payload_for_logging(client_response, setting=setting)
                    response_text = (
                        ProxyService._extract_response_display_text(client_response, limit_bytes=setting.max_logged_body_bytes)
                        if setting.enable_payload_logging
                        else None
                    )
                    trace.extend(fallback_trace)
                    ProxyService._append_typed_request_event(
                        trace,
                        "request_upstream_response",
                        {
                            "status_code": 200,
                            "upstream_request_id": upstream_request_id,
                            "finish_reason": finish_reason,
                            "usage_json": dumps_json(usage_info),
                            "response_summary_json": response_body_json,
                            "response_text_excerpt": response_text[:500] if response_text else None,
                            "response_body_truncated": "truncated" in (response_body_json or "").lower(),
                        },
                    )
                    ProxyService._append_typed_request_event(
                        trace,
                        "request_content_guard",
                        {
                            "guard_stage": "non_stream_response",
                            "guard_result": guard_result.result,
                            "risk_level": guard_result.risk_level,
                            "matched_categories_json": dumps_json(guard_result.categories),
                            "matched_rules_json": guard_result.matched_rules_json(),
                            "reason": guard_result.reason,
                            "action": guard_result.action,
                            "excerpt": guard_result.excerpt,
                            "provider_status_after": getattr(provider, "content_integrity_status", None),
                        },
                        result="blocked" if ProxyService._content_guard_should_block_for_response(guard_result, setting=setting) else "success",
                        severity="danger" if guard_result.risk_level == "high" else "info",
                        module="content_guard",
                    )
                    if ProxyService._content_guard_should_block_for_response(guard_result, setting=setting):
                        trace.append(
                            ProxyService._build_trace_item(
                                provider,
                                provider_model,
                                "content_integrity_violation",
                                latency_ms,
                                status_code=status.HTTP_502_BAD_GATEWAY,
                                error=guard_result.reason,
                            )
                        )
                        await ProxyService._log_content_guard_violation(
                            db=db,
                            provider=provider,
                            provider_model=provider_model,
                            guard_result=guard_result,
                            log_type=log_type,
                            trace_id=trace_id,
                            model_name=model_name,
                            requested_model=requested_model_name,
                            api_client_auth=api_client_auth,
                            request_id=request_id,
                            conversation_key=conversation_key,
                            session_id=session_id,
                            request_path=effective_log_request_path,
                            source_ip=source_ip,
                            is_stream=False,
                            has_image=has_image,
                            latency_ms=latency_ms,
                            duration_ms=latency_ms,
                            reasoning_level=reasoning_level,
                            model_reasoning_effort=model_reasoning_effort,
                            attempt_count=attempt_count,
                            request_body_json=request_body_json,
                            response_body_json=response_body_json,
                            trace=trace,
                            request_payload=payload,
                        )
                        if ProxyService._content_guard_should_switch_provider(setting):
                            raise ContentGuardBlockedError(
                                guard_result=guard_result,
                                detail=ProxyService._build_content_guard_error_detail(
                                    guard_result=guard_result,
                                    trace_id=trace_id,
                                    retried=True,
                                ),
                            )
                        raise HTTPException(
                            status_code=status.HTTP_502_BAD_GATEWAY,
                            detail=ProxyService._build_content_guard_error_detail(
                                guard_result=guard_result,
                                trace_id=trace_id,
                                retried=False,
                            ),
                        )
                    trace.append(ProxyService._build_trace_item(provider, provider_model, "success", latency_ms, status_code=200))
                    ProxyService._append_typed_request_event(
                        trace,
                        "request_billing",
                        {
                            "billing_event_id": None,
                            "billing_stage": "queued" if setting.enable_token_logging else "skipped",
                            "token_source": "upstream_usage" if usage_info.get("total_tokens") is not None else "missing",
                            "prompt_tokens": usage_info["prompt_tokens"],
                            "completion_tokens": usage_info["completion_tokens"],
                            "cache_read_tokens": usage_info["cache_read_tokens"],
                            "cache_write_tokens": usage_info["cache_write_tokens"],
                            "attempt_count": 0,
                        },
                        module="billing",
                    )
                    if not suppress_success_log:
                        await ProxyService._run_db_write(
                            ProxyService._create_success_log_with_provider_status,
                            log_type=log_type,
                            trace_id=trace_id,
                            provider_id=provider.id,
                            provider_name=provider.name,
                            model_name=model_name,
                            requested_model=requested_model_name,
                            tenant_name=api_client_auth.api_client_key.tenant_name if api_client_auth else None,
                            project_name=api_client_auth.api_client_key.project_name if api_client_auth else None,
                            app_name=api_client_auth.api_client_key.app_name if api_client_auth else None,
                            environment_name=api_client_auth.api_client_key.environment_name if api_client_auth else None,
                            request_id=request_id,
                            conversation_key=conversation_key,
                            session_id=session_id,
                            resolved_provider_model_id=provider_model.id,
                            request_path=effective_log_request_path,
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
                            **guard_result.to_log_kwargs(),
                            content_guard_retry_provider_count=ProxyService._content_guard_retry_provider_count(trace),
                            **ProxyService._build_api_client_log_kwargs(api_client_auth, auth_result="authenticated"),
                            **ProxyService._build_provider_log_kwargs(provider_model),
                            trace=trace,
                            token_request_payload=payload,
                            token_response_payload=response,
                            token_response_text=response_text,
                            schedule_token_fill=setting.enable_token_logging,
                        )
                    return client_response, provider, trace, latency_ms
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
                except ContentGuardBlockedError as exc:
                    last_upstream_error = {
                        "status_code": exc.status_code,
                        "detail": exc.detail,
                    }
                    break
                except HTTPException:
                    raise
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
                    if not ProxyService._should_retry_same_provider_status(
                        exc.response.status_code,
                        detail=last_upstream_error["detail"],
                    ):
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
                    if not ProxyService._should_retry_same_provider_status(
                        exc.status_code,
                        detail=last_upstream_error["detail"],
                    ):
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
                    error_status_code, error_detail = ProxyService._build_upstream_exception_error(exc)
                    trace.append(
                        ProxyService._build_trace_item(
                            provider,
                            provider_model,
                            ProxyService._classify_exception(exc),
                            latency_ms,
                            error=ProxyService._error_message_for_log(error_detail),
                        )
                    )
                    last_upstream_error = {
                        "status_code": error_status_code,
                        "detail": error_detail,
                    }
                    await ProxyService._mark_failure_async(
                        provider,
                        provider_model,
                        latency_ms,
                        ProxyService._error_message_for_log(error_detail),
                        db=db,
                    )

        if ProxyService._should_retry_mapped_model(last_upstream_error):
            mapped_retry = await ProxyService._retry_with_next_mapped_model_json(
                db=db,
                endpoint_path=endpoint_path,
                payload=payload,
                log_type=log_type,
                forced_provider_id=forced_provider_id,
                route_context=route_context,
                api_client_auth=api_client_auth,
                trace_id=trace_id,
                source_ip=source_ip,
                request_path_for_log=effective_log_request_path,
                public_endpoint_path=public_endpoint_path,
                response_transform=response_transform,
                suppress_success_log=suppress_success_log,
                route_retry_started_at=route_retry_started_at,
                route_retry_round=route_retry_round,
                route_retry_trace=trace,
                route_retry_attempt_count=attempt_count,
                mapping_resolution=mapping_resolution,
                requested_model_name=requested_model_name if isinstance(requested_model_name, str) else None,
                current_model_name=model_name if isinstance(model_name, str) else None,
                excluded_target_model_names=mapping_failover_excluded_target_model_names,
                request_id=request_id,
                conversation_key=conversation_key,
                session_id=session_id,
                reason="mapped_target_upstream_failed",
                upstream_error=last_upstream_error,
            )
            if mapped_retry is not None:
                return mapped_retry

        if ProxyService._should_retry_route_upstream_error(last_upstream_error):
            sleep_seconds = ProxyService._route_exhausted_retry_sleep_seconds(
                setting,
                started_at=route_retry_started_at,
                retry_round=route_retry_round,
                    route_context=route_context,
                )
            if sleep_seconds > 0:
                ProxyService._append_route_exhausted_retry_trace(
                    trace,
                    reason="all_candidates_failed",
                    sleep_seconds=sleep_seconds,
                    started_at=route_retry_started_at,
                    retry_round=route_retry_round,
                    setting=setting,
                    route_context=route_context,
                    upstream_error=last_upstream_error,
                )
                await asyncio.sleep(sleep_seconds)
                raise RouteExhaustedRetrySignal(
                    route_retry_started_at=route_retry_started_at,
                    route_retry_round=route_retry_round + 1,
                    route_retry_trace=trace,
                    route_retry_attempt_count=attempt_count,
                )
            if not ProxyService._route_exhausted_retry_infinite_enabled(setting, route_context) and ProxyService._route_exhausted_retry_max_wait_seconds(setting) > 0:
                last_upstream_error = ProxyService._build_route_exhausted_retry_upstream_error(
                    setting,
                    started_at=route_retry_started_at,
                    attempt_count=attempt_count,
                    trace=trace,
                    trace_id=trace_id,
                    last_upstream_error=last_upstream_error,
                )

        await ProxyService._raise_final_error_async(
            db,
            model_name=model_name,
            endpoint_path=endpoint_path,
            log_type=log_type,
            trace=trace,
            upstream_error=last_upstream_error,
            requested_model=requested_model_name,
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
            request_path_for_log=effective_log_request_path,
        )

    @staticmethod
    async def forward_stream_request(db: Session | None = None, **kwargs) -> tuple[AsyncIterator[bytes], Provider, list[dict], int]:
        route_retry_started_at = kwargs.pop("route_retry_started_at", None)
        route_retry_round = int(kwargs.pop("route_retry_round", 0) or 0)
        route_retry_trace = kwargs.pop("route_retry_trace", None)
        route_retry_attempt_count = int(kwargs.pop("route_retry_attempt_count", 0) or 0)
        while True:
            try:
                return await ProxyService._forward_stream_request_once(
                    db,
                    **kwargs,
                    route_retry_started_at=route_retry_started_at,
                    route_retry_round=route_retry_round,
                    route_retry_trace=route_retry_trace,
                    route_retry_attempt_count=route_retry_attempt_count,
                )
            except RouteExhaustedRetrySignal as signal:
                route_retry_started_at = signal.route_retry_started_at
                route_retry_round = signal.route_retry_round
                route_retry_trace = signal.route_retry_trace
                route_retry_attempt_count = signal.route_retry_attempt_count
    @staticmethod
    async def _forward_stream_request_once(
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
        request_path_for_log: str | None = None,
        public_endpoint_path: str | None = None,
        route_retry_started_at: float | None = None,
        route_retry_round: int = 0,
        route_retry_trace: list[dict] | None = None,
        route_retry_attempt_count: int = 0,
        mapping_failover_excluded_target_model_names: tuple[str, ...] = (),
        request_id_override: str | None = None,
        conversation_key_override: str | None = None,
        session_id_override: str | None = None,
    ) -> tuple[AsyncIterator[bytes], Provider, list[dict], int]:
        payload = ProxyService._normalize_reasoning_request_payload(endpoint_path=endpoint_path, payload=payload)
        request_payload_for_log = payload
        model_name = payload.get("model")
        requested_model_name = model_name
        has_image_input = ProxyService._payload_has_image(payload)
        has_image = ProxyService._payload_needs_image_transport(payload)
        require_tools = ProxyService._payload_uses_tools(payload)
        require_image_generation = ProxyService._payload_uses_image_generation(payload)
        route_context = ProxyService._with_content_guard_route_policy(
            route_context,
            payload=payload,
            endpoint_path=endpoint_path,
            has_image=has_image_input or has_image,
            require_tools=require_tools,
        )
        effective_public_endpoint_path = public_endpoint_path or endpoint_path
        effective_log_request_path = request_path_for_log or f"/v1{effective_public_endpoint_path}"
        setting = await ProxyService._get_setting_async(db)
        request_id = request_id_override or uuid4().hex
        session_sticky_key = session_id_override or ProxyService._extract_session_sticky_key(payload)
        reasoning_level = LogService.extract_reasoning_level(payload)
        model_reasoning_effort = LogService.extract_model_reasoning_effort(payload)
        require_chat_completions, require_responses = ProxyService._route_endpoint_requirements(endpoint_path, payload)
        trace: list[dict] = list(route_retry_trace or [])
        ProxyService._append_auth_trace_event(trace, api_client_auth)
        required_capabilities_json = ProxyService._request_capabilities_payload(
            endpoint_path=effective_log_request_path,
            is_stream=True,
            has_image=has_image_input or has_image,
            require_tools=require_tools,
            require_image_generation=require_image_generation,
            require_chat_completions=require_chat_completions,
            require_responses=require_responses,
        )
        if api_client_auth is not None and not ApiKeyService.is_model_allowed(api_client_auth.api_client_key, requested_model_name):
            request_body_json_for_rejection = ProxyService._serialize_payload_for_logging(
                request_payload_for_log,
                setting=setting,
                preserve_request_content_when_disabled=True,
                structure_only=True,
            )
            ProxyService._append_validation_trace_event(
                trace,
                stage="schema_validation",
                passed=True,
                request_body_json=request_body_json_for_rejection,
            )
            ProxyService._append_model_permission_trace_event(
                trace,
                requested_model=requested_model_name,
                resolved_model=model_name,
                endpoint_path=effective_log_request_path,
                permission_result="model_not_allowed",
                required_capabilities_json=required_capabilities_json,
                reason_details={"message": "Requested model is not allowed for this api key", "error_code": "model_not_allowed"},
            )
            await ProxyService._run_db_write(
                LogService.create_log,
                db=db,
                log_type=log_type,
                trace_id=trace_id,
                model_name=model_name,
                requested_model=requested_model_name,
                tenant_name=api_client_auth.api_client_key.tenant_name,
                project_name=api_client_auth.api_client_key.project_name,
                app_name=api_client_auth.api_client_key.app_name,
                environment_name=api_client_auth.api_client_key.environment_name,
                request_id=request_id,
                request_path=effective_log_request_path,
                source_ip=source_ip,
                http_method="POST",
                is_stream=True,
                has_image=has_image,
                success=False,
                status_code=status.HTTP_403_FORBIDDEN,
                reasoning_level=reasoning_level,
                model_reasoning_effort=model_reasoning_effort,
                request_body_json=request_body_json_for_rejection,
                message="Requested model is not allowed for this api key",
                error_type="invalid_request_error",
                error_code="model_not_allowed",
                retryable=False,
                **ProxyService._build_api_client_log_kwargs(api_client_auth, auth_result="authenticated"),
                trace=trace,
                attempt_count=0,
                token_request_payload=payload,
                schedule_token_fill=False,
            )
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail={"message": "Requested model is not allowed for this api key", "code": "model_not_allowed"},
            )
        mapping_resolution = await ModelMappingService.resolve_for_request(
            source_model_name=requested_model_name if isinstance(requested_model_name, str) else None,
            api_client_auth=api_client_auth,
            sticky_key=session_sticky_key,
            excluded_target_model_names=mapping_failover_excluded_target_model_names,
        )
        model_mapping_unavailable = ProxyService._model_mapping_unavailable(mapping_resolution)
        if mapping_resolution is not None and not model_mapping_unavailable and mapping_resolution.selected_model_name != requested_model_name:
            payload = {**payload, "model": mapping_resolution.selected_model_name}
            model_name = mapping_resolution.selected_model_name
        token_limit_error = ProxyService._build_request_token_limit_error(
            setting=setting,
            payload=payload,
            model_name=model_name,
            endpoint_path=effective_public_endpoint_path,
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
                requested_model=requested_model_name,
                request_path_for_log=effective_log_request_path,
            )
        long_output_error = ProxyService._build_long_output_error(
            setting=setting,
            payload=payload,
            model_name=model_name,
            endpoint_path=effective_public_endpoint_path,
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
                requested_model=requested_model_name,
                request_path_for_log=effective_log_request_path,
                request_body_json=ProxyService._serialize_payload_for_logging(
                    request_payload_for_log,
                    setting=setting,
                    preserve_request_content_when_disabled=True,
                    structure_only=True,
                ),
            )
        conversation_key = conversation_key_override or ProxyService._extract_conversation_key(payload, request_id)
        session_id = session_id_override or LogService.extract_session_id(payload, conversation_key=conversation_key, fallback=request_id)
        request_body_json = ProxyService._serialize_payload_for_logging(
            request_payload_for_log,
            setting=setting,
            preserve_request_content_when_disabled=True,
            structure_only=True,
        )
        ProxyService._append_model_mapping_trace(trace, mapping_resolution)
        ProxyService._append_stateful_responses_route_trace(
            trace,
            endpoint_path=endpoint_path,
            payload=payload,
            requested_model_name=requested_model_name,
            selected_model_name=model_name,
            mapping_resolution=mapping_resolution,
        )
        ProxyService._append_validation_trace_event(
            trace,
            stage="schema_validation",
            passed=True,
            request_body_json=request_body_json,
        )
        ProxyService._append_model_permission_trace_event(
            trace,
            requested_model=requested_model_name,
            resolved_model=model_name,
            endpoint_path=effective_log_request_path,
            permission_result="allowed" if not model_mapping_unavailable else "model_not_available",
            required_capabilities_json=required_capabilities_json,
            reason_details=(mapping_resolution.trace if mapping_resolution is not None and isinstance(mapping_resolution.trace, dict) else {}),
        )
        last_upstream_error: dict[str, Any] | None = None
        attempt_count = max(0, int(route_retry_attempt_count or 0))
        route_retry_started_at = route_retry_started_at or time.perf_counter()
        try:
            candidates = (
                []
                if model_mapping_unavailable
                else await RouterService.async_order_candidates(
                    db,
                    model_name=model_name,
                    sticky_key=session_sticky_key,
                    forced_provider_id=forced_provider_id,
                    route_context=route_context,
                    require_vision=has_image_input,
                    require_stream=True,
                    require_tools=require_tools,
                    require_image_generation=require_image_generation,
                    require_chat_completions=require_chat_completions,
                    require_responses=require_responses,
                )
            )
        except ProviderCapacityUnavailableError as exc:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"message": str(exc), "code": exc.code},
            ) from exc
        if candidates:
            first_candidate = candidates[0]
            ProxyService._append_route_decision_trace_event(
                trace,
                route_round=route_retry_round + 1,
                candidate_count=len(candidates),
                selected_provider_id=first_candidate.provider.id,
                selected_provider_model_id=first_candidate.provider_model.id,
                sticky_hit=bool(session_sticky_key),
            )
        if not candidates:
            route_diagnostics = (
                ProxyService._model_mapping_unavailable_diagnostics(mapping_resolution)
                if model_mapping_unavailable
                else await RouterService.async_diagnose_candidate_unavailability(
                    model_name=model_name,
                    forced_provider_id=forced_provider_id,
                    route_context=route_context,
                    require_vision=has_image_input,
                    require_stream=True,
                    require_tools=require_tools,
                    require_image_generation=require_image_generation,
                    require_chat_completions=require_chat_completions,
                    require_responses=require_responses,
                    is_stream=True,
                )
            )
            ProxyService._append_route_decision_trace_event(
                trace,
                route_round=route_retry_round + 1,
                candidate_count=0,
                diagnostics=route_diagnostics,
            )
            if model_mapping_unavailable:
                route_message, error_code = ProxyService._build_model_mapping_unavailable_error(route_diagnostics)
            elif require_image_generation:
                route_message = "No native image-generation-capable provider for requested model"
                error_code = "model_image_generation_not_available"
            else:
                route_message = "No available provider for requested model"
                error_code = "model_not_available"
            route_error_detail = ProxyService._build_route_unavailable_error_detail(
                route_diagnostics,
                message=route_message,
                code=error_code,
                endpoint_path=effective_public_endpoint_path,
                requested_model=requested_model_name if isinstance(requested_model_name, str) else None,
                selected_model=model_name if isinstance(model_name, str) else None,
                requires_tools=require_tools,
                requires_image_generation=require_image_generation,
                model_mapping_unavailable=model_mapping_unavailable,
            )
            mapped_retry = await ProxyService._retry_with_next_mapped_model_stream(
                db=db,
                endpoint_path=endpoint_path,
                payload=payload,
                log_type=log_type,
                forced_provider_id=forced_provider_id,
                route_context=route_context,
                api_client_auth=api_client_auth,
                trace_id=trace_id,
                source_ip=source_ip,
                request_path_for_log=effective_log_request_path,
                public_endpoint_path=public_endpoint_path,
                route_retry_started_at=route_retry_started_at,
                route_retry_round=route_retry_round,
                route_retry_trace=trace,
                route_retry_attempt_count=attempt_count,
                mapping_resolution=mapping_resolution,
                requested_model_name=requested_model_name if isinstance(requested_model_name, str) else None,
                current_model_name=model_name if isinstance(model_name, str) else None,
                excluded_target_model_names=mapping_failover_excluded_target_model_names,
                request_id=request_id,
                conversation_key=conversation_key,
                session_id=session_id,
                reason="no_route_candidate_for_mapped_target",
                route_diagnostics=route_diagnostics,
            )
            if mapped_retry is not None:
                return mapped_retry
            retryable_route_exhausted = ProxyService._should_retry_route_diagnostics(route_diagnostics)
            sleep_seconds = (
                ProxyService._route_exhausted_retry_sleep_seconds(
                    setting,
                    started_at=route_retry_started_at,
                    retry_round=route_retry_round,
                    route_context=route_context,
                )
                if retryable_route_exhausted
                else 0.0
            )
            if sleep_seconds > 0:
                ProxyService._append_route_exhausted_retry_trace(
                    trace,
                    reason="route_candidates_exhausted",
                    sleep_seconds=sleep_seconds,
                    started_at=route_retry_started_at,
                    retry_round=route_retry_round,
                    setting=setting,
                    route_context=route_context,
                    diagnostics=route_diagnostics,
                )
                await asyncio.sleep(sleep_seconds)
                raise RouteExhaustedRetrySignal(
                    route_retry_started_at=route_retry_started_at,
                    route_retry_round=route_retry_round + 1,
                    route_retry_trace=trace,
                    route_retry_attempt_count=attempt_count,
                )
            if retryable_route_exhausted and not ProxyService._route_exhausted_retry_infinite_enabled(setting, route_context) and ProxyService._route_exhausted_retry_max_wait_seconds(setting) > 0:
                await ProxyService._raise_final_error_async(
                    db,
                    model_name=model_name,
                    endpoint_path=endpoint_path,
                    log_type=log_type,
                    trace=trace + [{"result": "route_candidates_exhausted", "diagnostic": route_diagnostics}],
                    upstream_error=ProxyService._build_route_exhausted_retry_upstream_error(
                        setting,
                        started_at=route_retry_started_at,
                        attempt_count=attempt_count,
                        trace=trace,
                        trace_id=trace_id,
                        last_upstream_error=None,
                    ),
                    requested_model=requested_model_name,
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
                    request_path_for_log=effective_log_request_path,
                )
            await ProxyService._run_db_write(
                LogService.create_log,
                log_type=log_type,
                trace_id=trace_id,
                model_name=model_name,
                requested_model=requested_model_name,
                tenant_name=api_client_auth.api_client_key.tenant_name if api_client_auth else None,
                project_name=api_client_auth.api_client_key.project_name if api_client_auth else None,
                app_name=api_client_auth.api_client_key.app_name if api_client_auth else None,
                environment_name=api_client_auth.api_client_key.environment_name if api_client_auth else None,
                request_id=request_id,
                conversation_key=conversation_key,
                session_id=session_id,
                request_path=effective_log_request_path,
                source_ip=source_ip,
                http_method="POST",
                is_stream=True,
                has_image=has_image,
                success=False,
                status_code=status.HTTP_404_NOT_FOUND,
                reasoning_level=reasoning_level,
                model_reasoning_effort=model_reasoning_effort,
                request_body_json=request_body_json,
                message=route_message,
                error_type="invalid_request_error",
                error_code=error_code,
                retryable=False,
                **ProxyService._build_api_client_log_kwargs(api_client_auth, auth_result="authenticated"),
                trace=trace + [{"result": "route_candidates_exhausted", "diagnostic": route_diagnostics}],
                attempt_count=attempt_count,
                token_request_payload=payload,
                schedule_token_fill=setting.enable_token_logging,
            )
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=route_error_detail,
            )

        for candidate in candidates:
            provider = candidate.provider
            provider_model = candidate.provider_model
            balance_rejection = ProxyService._precheck_owner_balance_for_candidate(
                db,
                api_client_auth=api_client_auth,
                provider_model=provider_model,
                payload=payload,
                request_path=effective_log_request_path,
                model_name=model_name,
            )
            if balance_rejection is not None:
                trace.append(
                    ProxyService._build_trace_item(
                        provider,
                        provider_model,
                        "insufficient_balance_precheck",
                        0,
                        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                        error=balance_rejection.get("message"),
                    )
                )
                last_upstream_error = {
                    "status_code": status.HTTP_429_TOO_MANY_REQUESTS,
                    "detail": balance_rejection,
                }
                continue
            retries = ProxyService._same_provider_retry_budget(provider, setting)
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
                    stream_log_fast_path = ProxyService._select_stream_log_fast_path(prepared)
                    stream_guard_enabled = ProxyService._content_guard_enabled_for_request(
                        setting=setting,
                        provider=provider,
                        route_context=route_context,
                    )
                    stream_guard_mode = ProxyService._content_guard_stream_mode(setting)
                    stream_guard_buffering = (
                        stream_guard_enabled
                        and stream_guard_mode != "pass_through_scan"
                        and ProxyService._stream_content_guard_should_buffer(
                            setting=setting,
                            provider=provider,
                            route_context=route_context,
                        )
                    )
                    guard_buffer_limit = int(getattr(setting, "content_guard_stream_buffer_max_bytes", 16384) or 16384)
                    guard_max_delay_ms = min(500, max(0, int(getattr(setting, "content_guard_max_detection_delay_ms", 300) or 0)))
                    guard_max_delay_seconds = guard_max_delay_ms / 1000
                    prefetched_stream_guard_bytes = b""
                    prefetch_first_chunk_latency_ms: int | None = None
                    prefetched_stream_ended = False
                    prefetch_pending_read_task: asyncio.Task[bytes] | None = None
                    prefetch_guard_result = ContentGuardResult(
                        result=ContentGuardService.RESULT_PASS,
                        risk_level="low",
                        reason=(
                            "流式响应未启用首段缓冲审核，后续仍会按分块滑动窗口执行内容完整性扫描"
                            if stream_guard_enabled and not stream_guard_buffering
                            else (
                                "流式响应内容防护未启用"
                                if not stream_guard_enabled
                                else "流式首段通过内容完整性审核"
                            )
                        ),
                        action="allow",
                    )
                    prefetched_chunk_iterator: AsyncIterator[bytes] | None = (
                        response.aiter_bytes().__aiter__() if stream_guard_buffering else None
                    )
                    if stream_guard_buffering and prefetched_chunk_iterator is not None:
                        (
                            prefetched_stream_guard_bytes,
                            prefetch_guard_result,
                            prefetch_first_chunk_latency_ms,
                            prefetched_stream_ended,
                            prefetch_pending_read_task,
                        ) = await ProxyService._prefetch_stream_guard_buffer(
                            db=db,
                            setting=setting,
                            provider=provider,
                            provider_model=provider_model,
                            endpoint_path=endpoint_path,
                            request_payload=request_payload_for_log,
                            chunk_iterator=prefetched_chunk_iterator,
                            started=started,
                            limit_bytes=guard_buffer_limit,
                            max_delay_seconds=guard_max_delay_seconds,
                            stop_at_first_event=stream_guard_mode != "full_buffer",
                        )
                        if ProxyService._content_guard_should_block_for_response(prefetch_guard_result, setting=setting):
                            if prefetch_pending_read_task is not None and not prefetch_pending_read_task.done():
                                prefetch_pending_read_task.cancel()
                                with suppress(asyncio.CancelledError):
                                    await prefetch_pending_read_task
                            duration_ms = int((time.perf_counter() - started) * 1000)
                            trace.append(
                                ProxyService._build_trace_item(
                                    provider,
                                    provider_model,
                                    "content_integrity_violation",
                                    duration_ms,
                                    status_code=status.HTTP_502_BAD_GATEWAY,
                                    error=prefetch_guard_result.reason,
                                )
                            )
                            await ProxyService._log_content_guard_violation(
                                db=db,
                                provider=provider,
                                provider_model=provider_model,
                                guard_result=prefetch_guard_result,
                                log_type=log_type,
                                trace_id=trace_id,
                                model_name=model_name,
                                requested_model=requested_model_name,
                                api_client_auth=api_client_auth,
                                request_id=request_id,
                                conversation_key=conversation_key,
                                session_id=session_id,
                                request_path=effective_log_request_path,
                                source_ip=source_ip,
                                is_stream=True,
                                has_image=has_image,
                                latency_ms=latency_ms,
                                duration_ms=duration_ms,
                                reasoning_level=reasoning_level,
                                model_reasoning_effort=model_reasoning_effort,
                                attempt_count=attempt_count,
                                request_body_json=request_body_json,
                                response_body_json=None,
                                trace=trace,
                                request_payload=payload,
                            )
                            await stream_context.__aexit__(None, None, None)
                            stream_context = None
                            if capacity_lease is not None and capacity_lease_entered:
                                await capacity_lease.__aexit__(None, None, None)
                                capacity_lease_entered = False
                            if not ProxyService._content_guard_should_switch_provider(setting):
                                raise HTTPException(
                                    status_code=status.HTTP_502_BAD_GATEWAY,
                                    detail=ProxyService._build_content_guard_error_detail(
                                        guard_result=prefetch_guard_result,
                                        trace_id=trace_id,
                                        retried=False,
                                    ),
                                )
                            last_upstream_error = ProxyService._content_guard_upstream_error(
                                guard_result=prefetch_guard_result,
                                trace_id=trace_id,
                                retried=True,
                            )
                            break

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
                        stream_usage_payload: dict[str, Any] | None = None
                        capture_stream_text = bool(setting.enable_stream_response_persist)
                        capture_token_text = bool(setting.enable_token_logging)
                        finish_reason: str | None = None
                        response_text_parts: list[str] = []
                        response_text_bytes = 0
                        token_response_parts: list[str] | None = [] if capture_token_text else None
                        token_response_bytes = 0
                        generated_image_summary: dict[str, Any] | None = {} if has_image else None
                        event_buffer = bytearray()
                        stream_guard_event_buffer = bytearray()
                        guard_result = prefetch_guard_result
                        guard_stage = "stream_buffer" if stream_guard_buffering else ("stream_chunk" if stream_guard_enabled else "stream_disabled")
                        downstream_started = False
                        downstream_transform_state = (
                            ProxyService._create_responses_stream_state(
                                payload=payload,
                                response_id=prepared.response_id_override,
                            )
                            if prepared.adapt_chat_response_to_responses
                            else None
                        )
                        completion_stream_transform_state = (
                            ProxyService._create_text_completion_stream_state(payload=payload)
                            if prepared.adapt_chat_response_to_completions
                            else None
                        )
                        chat_stream_transform_state = (
                            ProxyService._create_chat_stream_state(payload=payload)
                            if prepared.adapt_responses_response_to_chat
                            else None
                        )
                        timeout_policy = ProxyService._build_stream_timeout_policy(provider=provider, setting=setting)
                        stream_started = time.perf_counter()
                        try:
                            chunk_iterator = prefetched_chunk_iterator or response.aiter_bytes().__aiter__()
                            prefetched_chunks = [prefetched_stream_guard_bytes] if prefetched_stream_guard_bytes else []
                            pending_read_task = prefetch_pending_read_task
                            while True:
                                if prefetched_chunks:
                                    upstream_chunk = prefetched_chunks.pop(0)
                                elif prefetched_stream_ended:
                                    break
                                elif pending_read_task is not None:
                                    try:
                                        timeout_seconds, timeout_code, timeout_message = ProxyService._next_stream_read_timeout(
                                            first_chunk_latency_ms=first_chunk_latency_ms,
                                            stream_started=stream_started,
                                            timeout_policy=timeout_policy,
                                        )
                                        if timeout_seconds is None:
                                            upstream_chunk = await pending_read_task
                                        else:
                                            upstream_chunk = await asyncio.wait_for(pending_read_task, timeout=timeout_seconds)
                                    except asyncio.TimeoutError as exc:
                                        pending_read_task.cancel()
                                        raise StreamTimeoutError(code=timeout_code, message=timeout_message) from exc
                                    except StopAsyncIteration:
                                        break
                                    finally:
                                        pending_read_task = None
                                else:
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
                                    if stream_guard_enabled:
                                        current_guard_result = ProxyService._inspect_stream_guard_chunk(
                                            event_buffer=stream_guard_event_buffer,
                                            chunk=upstream_chunk,
                                            setting=setting,
                                            endpoint_path=endpoint_path,
                                            request_payload=request_payload_for_log,
                                        )
                                        if current_guard_result.result != ContentGuardService.RESULT_PASS:
                                            guard_result = current_guard_result
                                            guard_stage = "stream_chunk"
                                        if ProxyService._content_guard_should_block_for_response(current_guard_result, setting=setting):
                                            await ProxyService._run_db_write(
                                                ProxyService._record_content_guard_violation_by_id,
                                                provider.id,
                                                provider_model.id,
                                                current_guard_result,
                                                db=db,
                                            )
                                            interrupted = True
                                            error_message = current_guard_result.reason
                                            error_code = "content_integrity_violation"
                                            stream_error_message = (
                                                "上游流式响应在输出过程中命中内容完整性高风险规则。"
                                                "为避免混接不同渠道内容，已中止当前流式响应。"
                                                if downstream_started
                                                else "上游流式响应首段未通过内容完整性防护，已阻断返回。"
                                            )
                                            yield ProxyService._format_stream_error_event(
                                                message=stream_error_message,
                                                code=error_code,
                                                trace_id=trace_id,
                                            )
                                            yield b"data: [DONE]\n\n"
                                            return
                                    if capture_stream_text or capture_token_text or generated_image_summary is not None:
                                        (
                                            response_text_bytes,
                                            token_response_bytes,
                                            finish_reason,
                                            usage_info,
                                            stream_usage_payload,
                                        ) = ProxyService._collect_stream_log_data(
                                            chunk=upstream_chunk,
                                            event_buffer=event_buffer,
                                            response_text_parts=response_text_parts,
                                            response_text_bytes=response_text_bytes,
                                            token_response_parts=token_response_parts,
                                            token_response_bytes=token_response_bytes,
                                            finish_reason=finish_reason,
                                            usage_info=usage_info,
                                            usage_payload=stream_usage_payload,
                                            generated_image_summary=generated_image_summary,
                                            capture_text=capture_stream_text,
                                            capture_usage=capture_token_text,
                                            limit_bytes=setting.max_logged_body_bytes,
                                            token_limit_bytes=getattr(setting, "stream_token_capture_max_bytes", 1048576),
                                            stream_log_fast_path=stream_log_fast_path,
                                        )
                                    if first_chunk_latency_ms is None:
                                        first_chunk_latency_ms = (
                                            prefetch_first_chunk_latency_ms
                                            if prefetch_first_chunk_latency_ms is not None
                                            else int((time.perf_counter() - started) * 1000)
                                        )
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
                                            state=downstream_transform_state
                                            or ProxyService._create_responses_stream_state(
                                                payload=payload,
                                                response_id=prepared.response_id_override,
                                            ),
                                            requested_model=str(prepared.response_model_override or requested_model_name or payload.get("model") or ""),
                                        ):
                                            downstream_started = True
                                            yield downstream_chunk
                                    elif prepared.adapt_chat_response_to_completions:
                                        for downstream_chunk in ProxyService._adapt_chat_stream_chunk_to_text_completion_events(
                                            upstream_chunk,
                                            state=completion_stream_transform_state
                                            or ProxyService._create_text_completion_stream_state(payload=payload),
                                            requested_model=str(requested_model_name or payload.get("model") or ""),
                                        ):
                                            downstream_started = True
                                            yield downstream_chunk
                                    elif prepared.adapt_responses_response_to_chat:
                                        for downstream_chunk in ProxyService._adapt_responses_stream_chunk_to_chat_events(
                                            upstream_chunk,
                                            state=chat_stream_transform_state or ProxyService._create_chat_stream_state(payload=payload),
                                            requested_model=str(requested_model_name or payload.get("model") or ""),
                                        ):
                                            downstream_started = True
                                            yield downstream_chunk
                                    else:
                                        downstream_started = True
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
                                    or ProxyService._create_responses_stream_state(
                                        payload=payload,
                                        response_id=prepared.response_id_override,
                                    )
                                ):
                                    downstream_started = True
                                    yield downstream_chunk
                            elif prepared.adapt_chat_response_to_completions:
                                for downstream_chunk in ProxyService._build_text_completion_stream_done_events(
                                    completion_stream_transform_state
                                    or ProxyService._create_text_completion_stream_state(payload=payload)
                                ):
                                    downstream_started = True
                                    yield downstream_chunk
                            elif prepared.adapt_responses_response_to_chat:
                                for downstream_chunk in ProxyService._build_chat_stream_completion_events(
                                    chat_stream_transform_state or ProxyService._create_chat_stream_state(payload=payload)
                                ):
                                    downstream_started = True
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
                            base_log_kwargs = {
                                "log_type": log_type,
                                "trace_id": trace_id,
                                "provider_id": provider.id,
                                "provider_name": provider.name,
                                "model_name": model_name,
                                "requested_model": requested_model_name,
                                "tenant_name": api_client_auth.api_client_key.tenant_name if api_client_auth else None,
                                "project_name": api_client_auth.api_client_key.project_name if api_client_auth else None,
                                "app_name": api_client_auth.api_client_key.app_name if api_client_auth else None,
                                "environment_name": api_client_auth.api_client_key.environment_name if api_client_auth else None,
                                "request_id": request_id,
                                "conversation_key": conversation_key,
                                "session_id": session_id,
                                "resolved_provider_model_id": provider_model.id,
                                "request_path": effective_log_request_path,
                                "source_ip": source_ip,
                                "http_method": "POST",
                                "is_stream": True,
                                "has_image": has_image,
                                "latency_ms": latency_ms,
                                "first_token_latency_ms": first_chunk_latency_ms,
                                "ttfb_ms": first_chunk_latency_ms,
                                "duration_ms": total_duration_ms,
                                "reasoning_level": reasoning_level,
                                "model_reasoning_effort": model_reasoning_effort,
                                "attempt_count": attempt_count,
                                "prompt_tokens": usage_info["prompt_tokens"],
                                "completion_tokens": usage_info["completion_tokens"],
                                "total_tokens": usage_info["total_tokens"],
                                "cache_read_tokens": usage_info["cache_read_tokens"],
                                "cache_write_tokens": usage_info["cache_write_tokens"],
                                "finish_reason": finish_reason,
                                "upstream_request_id": upstream_request_id,
                                "request_body_json": request_body_json,
                                "response_body_json": ProxyService._serialize_stream_response_summary_for_logging(
                                    generated_image_summary,
                                    setting=setting,
                                ),
                                "response_text": ProxyService._finalize_stream_text_capture(
                                    response_text_parts,
                                    generated_image_summary=generated_image_summary,
                                ),
                                "token_request_payload": request_payload_for_log,
                                "token_response_payload": {"usage": stream_usage_payload} if stream_usage_payload is not None else None,
                                "token_response_text": ProxyService._finalize_text_capture(token_response_parts or []),
                                "schedule_token_fill": setting.enable_token_logging,
                                **guard_result.to_log_kwargs(),
                                "content_guard_retry_provider_count": ProxyService._content_guard_retry_provider_count(trace),
                                **ProxyService._build_api_client_log_kwargs(api_client_auth, auth_result="authenticated"),
                                **ProxyService._build_provider_log_kwargs(provider_model),
                            }
                            typed_terminal_events: list[dict] = []
                            response_summary_json = base_log_kwargs["response_body_json"]
                            response_text_for_log = base_log_kwargs["response_text"]
                            usage_payload = {
                                "prompt_tokens": usage_info["prompt_tokens"],
                                "completion_tokens": usage_info["completion_tokens"],
                                "total_tokens": usage_info["total_tokens"],
                                "cache_read_tokens": usage_info["cache_read_tokens"],
                                "cache_write_tokens": usage_info["cache_write_tokens"],
                            }
                            stream_result = (
                                "completed"
                                if success and not interrupted
                                else (
                                    "client_disconnected"
                                    if client_cancelled
                                    else ("empty_stream" if not success else ("timeout" if error_code and "timeout" in error_code else "upstream_error"))
                                )
                            )
                            ProxyService._append_typed_request_event(
                                typed_terminal_events,
                                "request_upstream_response",
                                {
                                    "status_code": 200 if success and not interrupted else (499 if client_cancelled else 502),
                                    "upstream_request_id": upstream_request_id,
                                    "finish_reason": finish_reason,
                                    "usage_json": dumps_json(usage_payload),
                                    "response_summary_json": response_summary_json,
                                    "response_text_excerpt": response_text_for_log[:500] if response_text_for_log else None,
                                    "response_body_truncated": "truncated" in (response_summary_json or "").lower(),
                                },
                                result="success" if success and not interrupted else "failed",
                            )
                            ProxyService._append_typed_request_event(
                                typed_terminal_events,
                                "request_stream",
                                {
                                    "stream_result": stream_result,
                                    "first_token_latency_ms": first_chunk_latency_ms,
                                    "ttfb_ms": first_chunk_latency_ms,
                                    "duration_ms": total_duration_ms,
                                    "chunk_count": None,
                                    "captured_text_bytes": len(response_text_for_log.encode("utf-8")) if response_text_for_log else None,
                                    "sse_error_sent": bool(interrupted and not client_cancelled),
                                    "done_sent": not client_cancelled,
                                    "disconnect_status_code": 499 if client_cancelled else None,
                                },
                                result="success" if stream_result == "completed" else "failed",
                            )
                            ProxyService._append_typed_request_event(
                                typed_terminal_events,
                                "request_content_guard",
                                {
                                    "guard_stage": guard_stage,
                                    "guard_result": guard_result.result,
                                    "risk_level": guard_result.risk_level,
                                    "matched_categories_json": dumps_json(guard_result.categories),
                                    "matched_rules_json": guard_result.matched_rules_json(),
                                    "reason": guard_result.reason,
                                    "action": guard_result.action,
                                    "excerpt": guard_result.excerpt,
                                    "provider_status_after": getattr(provider, "content_integrity_status", None),
                                },
                                result="blocked" if error_code == "content_integrity_violation" else "success",
                                severity="danger" if guard_result.risk_level == "high" else "info",
                                module="content_guard",
                            )
                            ProxyService._append_typed_request_event(
                                typed_terminal_events,
                                "request_billing",
                                {
                                    "billing_event_id": None,
                                    "billing_stage": "queued" if setting.enable_token_logging and success and not interrupted else "skipped",
                                    "token_source": "upstream_usage" if usage_info.get("total_tokens") is not None else "missing",
                                    "prompt_tokens": usage_info["prompt_tokens"],
                                    "completion_tokens": usage_info["completion_tokens"],
                                    "cache_read_tokens": usage_info["cache_read_tokens"],
                                    "cache_write_tokens": usage_info["cache_write_tokens"],
                                    "attempt_count": 0,
                                },
                                module="billing",
                            )
                            if interrupted or client_cancelled or not success:
                                terminal_status_for_error = 499 if client_cancelled else 502
                                classified_error = ProxyService._classify_retry_policy(
                                    terminal_status_for_error,
                                    {"message": error_message or f"stream {log_type} failed", "code": error_code},
                                )
                                ProxyService._append_typed_request_event(
                                    typed_terminal_events,
                                    "request_error_response",
                                    {
                                        "status_code": terminal_status_for_error,
                                        "error_type": "client_error" if client_cancelled else "server_error",
                                        "error_code": error_code,
                                        "public_message": error_message or f"stream {log_type} failed",
                                        "category": classified_error.get("category"),
                                        "retryable": not client_cancelled,
                                        "recoverable": bool(classified_error.get("recoverable")),
                                        "diagnostic_sample_json": dumps_json({"stream_result": stream_result}),
                                    },
                                    result="failed",
                                    severity="warning",
                                )
                            if success and not interrupted:
                                final_trace = trace + typed_terminal_events + [
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
                                await ProxyService._run_db_write(
                                    ProxyService._create_success_log_with_provider_status,
                                    **{
                                        **base_log_kwargs,
                                        "success": True,
                                        "status_code": 200,
                                        "message": f"stream {log_type} success",
                                        "retryable": False,
                                        "trace": final_trace,
                                    },
                                )
                            else:
                                terminal_result = "client_cancelled" if client_cancelled else ("interrupted" if interrupted else "finished")
                                terminal_status_code = 499 if client_cancelled else (502 if interrupted or not success else 200)
                                if not client_cancelled:
                                    await ProxyService._mark_failure_async(
                                        provider,
                                        provider_model,
                                        latency_ms,
                                        error_message or f"stream {log_type} failed",
                                        force_unhealthy=ProxyService._should_mark_provider_model_unhealthy(
                                            status_code=terminal_status_code,
                                            detail={"message": error_message or f"stream {log_type} failed"},
                                        ),
                                    )
                                interrupted_trace = trace + typed_terminal_events + [
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
                                await ProxyService._run_db_write(
                                    LogService.create_log,
                                    **{
                                        **base_log_kwargs,
                                        "success": False,
                                        "status_code": terminal_status_code,
                                        "message": error_message or f"stream {log_type} failed",
                                        "error_type": "server_error" if not client_cancelled else "client_error",
                                        "error_code": error_code,
                                        "retryable": not client_cancelled,
                                        "trace": interrupted_trace,
                                        **(
                                            guard_result.to_log_kwargs()
                                            if error_code == "content_integrity_violation"
                                            else {}
                                        ),
                                    },
                                )

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
                except HTTPException:
                    raise
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
                    if not ProxyService._should_retry_same_provider_status(
                        exc.response.status_code,
                        detail=last_upstream_error["detail"],
                    ):
                        break
                except Exception as exc:
                    if stream_context is not None:
                        await stream_context.__aexit__(type(exc), exc, exc.__traceback__)
                    if capacity_lease is not None and capacity_lease_entered:
                        await capacity_lease.__aexit__(type(exc), exc, exc.__traceback__)
                    latency_ms = int((time.perf_counter() - started) * 1000)
                    error_status_code, error_detail = ProxyService._build_upstream_exception_error(exc)
                    trace.append(
                        ProxyService._build_trace_item(
                            provider,
                            provider_model,
                            ProxyService._classify_exception(exc),
                            latency_ms,
                            error=ProxyService._error_message_for_log(error_detail),
                        )
                    )
                    last_upstream_error = {
                        "status_code": error_status_code,
                        "detail": error_detail,
                    }
                    await ProxyService._mark_failure_async(
                        provider,
                        provider_model,
                        latency_ms,
                        ProxyService._error_message_for_log(error_detail),
                        db=db,
                    )

        if ProxyService._should_retry_mapped_model(last_upstream_error):
            mapped_retry = await ProxyService._retry_with_next_mapped_model_stream(
                db=db,
                endpoint_path=endpoint_path,
                payload=payload,
                log_type=log_type,
                forced_provider_id=forced_provider_id,
                route_context=route_context,
                api_client_auth=api_client_auth,
                trace_id=trace_id,
                source_ip=source_ip,
                request_path_for_log=effective_log_request_path,
                public_endpoint_path=public_endpoint_path,
                route_retry_started_at=route_retry_started_at,
                route_retry_round=route_retry_round,
                route_retry_trace=trace,
                route_retry_attempt_count=attempt_count,
                mapping_resolution=mapping_resolution,
                requested_model_name=requested_model_name if isinstance(requested_model_name, str) else None,
                current_model_name=model_name if isinstance(model_name, str) else None,
                excluded_target_model_names=mapping_failover_excluded_target_model_names,
                request_id=request_id,
                conversation_key=conversation_key,
                session_id=session_id,
                reason="mapped_target_upstream_failed",
                upstream_error=last_upstream_error,
            )
            if mapped_retry is not None:
                return mapped_retry

        if ProxyService._should_retry_route_upstream_error(last_upstream_error):
            sleep_seconds = ProxyService._route_exhausted_retry_sleep_seconds(
                setting,
                started_at=route_retry_started_at,
                retry_round=route_retry_round,
                    route_context=route_context,
                )
            if sleep_seconds > 0:
                ProxyService._append_route_exhausted_retry_trace(
                    trace,
                    reason="all_candidates_failed",
                    sleep_seconds=sleep_seconds,
                    started_at=route_retry_started_at,
                    retry_round=route_retry_round,
                    setting=setting,
                    route_context=route_context,
                    upstream_error=last_upstream_error,
                )
                await asyncio.sleep(sleep_seconds)
                raise RouteExhaustedRetrySignal(
                    route_retry_started_at=route_retry_started_at,
                    route_retry_round=route_retry_round + 1,
                    route_retry_trace=trace,
                    route_retry_attempt_count=attempt_count,
                )
            if not ProxyService._route_exhausted_retry_infinite_enabled(setting, route_context) and ProxyService._route_exhausted_retry_max_wait_seconds(setting) > 0:
                last_upstream_error = ProxyService._build_route_exhausted_retry_upstream_error(
                    setting,
                    started_at=route_retry_started_at,
                    attempt_count=attempt_count,
                    trace=trace,
                    trace_id=trace_id,
                    last_upstream_error=last_upstream_error,
                )

        await ProxyService._raise_final_error_async(
            db,
            model_name=model_name,
            endpoint_path=endpoint_path,
            log_type=log_type,
            trace=trace,
            upstream_error=last_upstream_error,
            requested_model=requested_model_name,
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
            request_path_for_log=effective_log_request_path,
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
        request_timeout_seconds: float | None = None,
    ) -> tuple[dict[str, Any], str | None, list[dict]]:
        headers = {"Authorization": f"Bearer {provider.api_key}"}
        prepared = ProxyService._prepare_upstream_request(provider, endpoint_path=endpoint_path, payload=payload)
        response_json, upstream_request_id = await ProxyService._send_prepared_json(
            provider,
            prepared=prepared,
            headers=headers,
            requested_payload=payload,
            setting=setting,
            request_timeout_seconds=request_timeout_seconds,
        )
        return response_json, upstream_request_id, []

    @staticmethod
    async def _send_prepared_json(
        provider: Provider,
        *,
        prepared: PreparedUpstreamRequest,
        headers: dict[str, str],
        requested_payload: dict[str, Any],
        setting: Any,
        request_timeout_seconds: float | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        if ProxyService._payload_needs_image_transport(prepared.request_payload):
            return await ProxyService._forward_json_image_request(
                provider,
                prepared=prepared,
                headers=headers,
                setting=setting,
                request_timeout_seconds=request_timeout_seconds,
            )
        client = ProxyService._select_upstream_client(payload=prepared.request_payload)
        response = await ProxyService._post_json_with_response_size_limit(
            client,
            f"{provider.base_url}{prepared.request_path}",
            headers=headers,
            json=prepared.request_payload,
            timeout=ProxyService._build_httpx_timeout(
                provider,
                payload=prepared.request_payload,
                is_stream=False,
                request_timeout_seconds=request_timeout_seconds,
            ),
            max_response_bytes=int(getattr(setting, "max_non_stream_response_body_bytes", 20971520) or 0),
        )
        response.raise_for_status()
        response_json = response.json()
        if prepared.adapt_chat_response_to_responses:
            ProxyService._assert_chat_response_adapter_safe(response_json)
            response_json = ProxyService._convert_chat_completion_to_responses_payload(
                response_json,
                requested_model=str(prepared.response_model_override or requested_payload.get("model") or response_json.get("model") or ""),
            )
        if prepared.adapt_chat_response_to_completions:
            response_json = ProxyService._convert_chat_completion_to_text_completion(
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
        request_timeout_seconds: float | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        client = ProxyService._select_upstream_client(payload=prepared.request_payload)
        response = await ProxyService._post_json_with_response_size_limit(
            client,
            f"{provider.base_url}{prepared.request_path}",
            headers=headers,
            json=prepared.request_payload,
            timeout=ProxyService._build_httpx_timeout(
                provider,
                payload=prepared.request_payload,
                is_stream=False,
                request_timeout_seconds=request_timeout_seconds,
            ),
            max_response_bytes=int(getattr(setting, "max_non_stream_response_body_bytes", 20971520) or 0),
        )
        response.raise_for_status()
        response_json = response.json()
        if prepared.adapt_chat_response_to_responses:
            ProxyService._assert_chat_response_adapter_safe(response_json)
            response_json = ProxyService._convert_chat_completion_to_responses_payload(
                response_json,
                requested_model=str(prepared.response_model_override or prepared.request_payload.get("model") or response_json.get("model") or ""),
            )
        if prepared.adapt_chat_response_to_completions:
            response_json = ProxyService._convert_chat_completion_to_text_completion(
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
    ) -> UpstreamJsonResponse:
        client_name = get_settings().upstream_json_client.strip().lower()
        if client_name == "aiohttp":
            return await ProxyService._post_json_aiohttp_with_response_size_limit(
                url,
                headers=headers,
                json_payload=json,
                timeout=timeout,
                max_response_bytes=max_response_bytes,
            )
        if client_name == "requests":
            return await run_in_threadpool(
                ProxyService._post_json_requests_with_response_size_limit,
                url,
                headers=headers,
                json_payload=json,
                timeout=timeout,
                max_response_bytes=max_response_bytes,
            )
        async with client.stream("POST", url, headers=headers, json=json, timeout=timeout) as response:
            content = await ProxyService._read_limited_httpx_response(
                response,
                max_response_bytes=max_response_bytes,
            )
            response._content = content
            return response

    @staticmethod
    async def _post_json_aiohttp_with_response_size_limit(
        url: str,
        *,
        headers: dict[str, str],
        json_payload: dict[str, Any],
        timeout: httpx.Timeout,
        max_response_bytes: int,
    ) -> AiohttpJsonResponse:
        connect_timeout = float(timeout.connect or get_settings().request_timeout_ms / 1000)
        read_timeout = float(timeout.read or get_settings().request_timeout_ms / 1000)
        request_timeout = aiohttp.ClientTimeout(
            total=None,
            connect=connect_timeout,
            sock_connect=connect_timeout,
            sock_read=read_timeout,
        )
        session = UpstreamClientService.get_aiohttp_session()
        async with session.post(url, headers=headers, json=json_payload, timeout=request_timeout) as response:
            content_length = response.headers.get("content-length")
            try:
                response_bytes = int(content_length) if content_length is not None else None
            except ValueError:
                response_bytes = None
            if max_response_bytes > 0 and response_bytes is not None and response_bytes > max_response_bytes:
                raise NonStreamResponseTooLarge(
                    status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                    detail={
                        "message": f"非流式上游响应超过应用层上限 {max_response_bytes} 字节，请使用 stream=true",
                        "code": "non_stream_response_too_large",
                        "max_non_stream_response_body_bytes": max_response_bytes,
                    },
                )
            if response_bytes is not None and (max_response_bytes <= 0 or response_bytes <= max_response_bytes):
                body = await response.read()
            else:
                body_buffer = bytearray()
                async for chunk in response.content.iter_chunked(65536):
                    if not chunk:
                        continue
                    if max_response_bytes > 0 and len(body_buffer) + len(chunk) > max_response_bytes:
                        raise NonStreamResponseTooLarge(
                            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
                            detail={
                                "message": f"非流式上游响应超过应用层上限 {max_response_bytes} 字节，请使用 stream=true",
                                "code": "non_stream_response_too_large",
                                "max_non_stream_response_body_bytes": max_response_bytes,
                            },
                        )
                    body_buffer.extend(chunk)
                body = bytes(body_buffer)
            return AiohttpJsonResponse(
                status_code=response.status,
                headers=dict(response.headers),
                content=body,
                request_method="POST",
                request_url=url,
            )

    @staticmethod
    def _post_json_requests_with_response_size_limit(
        url: str,
        *,
        headers: dict[str, str],
        json_payload: dict[str, Any],
        timeout: httpx.Timeout,
        max_response_bytes: int,
    ) -> httpx.Response:
        connect_timeout = float(timeout.connect or get_settings().request_timeout_ms / 1000)
        read_timeout = float(timeout.read or get_settings().request_timeout_ms / 1000)
        session = ProxyService._get_thread_local_requests_session()
        with session.post(
            url,
            headers=headers,
            json=json_payload,
            timeout=(connect_timeout, read_timeout),
            stream=True,
        ) as response:
            body = bytearray()
            for chunk in response.iter_content(chunk_size=65536):
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
            return httpx.Response(
                response.status_code,
                headers=dict(response.headers),
                content=bytes(body),
                request=httpx.Request("POST", url),
            )

    @staticmethod
    def _get_thread_local_requests_session() -> requests.Session:
        session = getattr(ProxyService._requests_session_local, "session", None)
        if isinstance(session, requests.Session):
            return session
        settings = get_settings()
        pool_size = max(10, int(getattr(settings, "upstream_max_connections", 1200) or 1200))
        session = requests.Session()
        adapter = HTTPAdapter(pool_connections=pool_size, pool_maxsize=pool_size, max_retries=0, pool_block=False)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        ProxyService._requests_session_local.session = session
        return session

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
    ) -> AsyncIterator[tuple[UpstreamStreamResponse, PreparedUpstreamRequest]]:
        request_url = f"{provider.base_url}{prepared.request_path}"
        if ProxyService._select_upstream_stream_client_name(payload=prepared.request_payload) == "aiohttp":
            session = UpstreamClientService.get_aiohttp_session()
            async with session.post(
                request_url,
                headers=headers,
                json=prepared.request_payload,
                timeout=ProxyService._build_aiohttp_timeout(
                    provider,
                    payload=prepared.request_payload,
                    is_stream=True,
                    stream_connect_timeout_seconds=stream_connect_timeout_seconds,
                ),
            ) as response:
                yield AiohttpStreamResponse(response=response, request_method="POST", request_url=request_url), prepared
            return
        client = ProxyService._select_upstream_client(payload=prepared.request_payload)
        async with client.stream(
            "POST",
            request_url,
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
    ) -> tuple[UpstreamStreamResponse, PreparedUpstreamRequest, Any, list[dict]]:
        headers = {"Authorization": f"Bearer {provider.api_key}"}
        prepared = ProxyService._prepare_upstream_request(provider, endpoint_path=endpoint_path, payload=payload)
        stream_context = ProxyService._stream_prepared_request(
            provider,
            prepared=prepared,
            headers=headers,
            stream_connect_timeout_seconds=stream_connect_timeout_seconds,
        )
        stream_context_entered = False
        try:
            response, opened_prepared = await stream_context.__aenter__()
            stream_context_entered = True
            await ProxyService._raise_stream_response_for_status(response)
            return response, opened_prepared, stream_context, []
        except httpx.HTTPStatusError as exc:
            if stream_context_entered:
                await stream_context.__aexit__(type(exc), exc, exc.__traceback__)
            raise
        except Exception as exc:
            if stream_context_entered:
                await stream_context.__aexit__(type(exc), exc, exc.__traceback__)
            raise

    @staticmethod
    def _select_upstream_client(*, payload: dict[str, Any]) -> httpx.AsyncClient:
        if ProxyService._payload_needs_image_transport(payload):
            return UpstreamClientService.get_http1_client()
        return UpstreamClientService.get_client()

    @staticmethod
    def _select_upstream_stream_client_name(*, payload: dict[str, Any]) -> str:
        configured = getattr(get_settings(), "upstream_stream_client", "aiohttp").strip().lower()
        if configured == "aiohttp":
            return "aiohttp"
        return "httpx"

    @staticmethod
    def _build_httpx_timeout(
        provider: Provider,
        *,
        payload: dict[str, Any],
        is_stream: bool,
        stream_connect_timeout_seconds: int | None = None,
        request_timeout_seconds: float | None = None,
    ) -> httpx.Timeout:
        settings = get_settings()
        if request_timeout_seconds is not None:
            base_timeout_seconds = max(float(request_timeout_seconds), 1.0)
        else:
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
        elif request_timeout_seconds is None and ProxyService._payload_needs_image_transport(payload):
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
    def _build_aiohttp_timeout(
        provider: Provider,
        *,
        payload: dict[str, Any],
        is_stream: bool,
        stream_connect_timeout_seconds: int | None = None,
    ) -> aiohttp.ClientTimeout:
        timeout = ProxyService._build_httpx_timeout(
            provider,
            payload=payload,
            is_stream=is_stream,
            stream_connect_timeout_seconds=stream_connect_timeout_seconds,
        )
        connect_timeout = float(timeout.connect or get_settings().request_timeout_ms / 1000)
        read_timeout = None if is_stream else float(timeout.read or get_settings().request_timeout_ms / 1000)
        return aiohttp.ClientTimeout(
            total=None,
            connect=connect_timeout,
            sock_connect=connect_timeout,
            sock_read=read_timeout,
        )

    @staticmethod
    async def _raise_stream_response_for_status(response: UpstreamStreamResponse) -> None:
        if isinstance(response, httpx.Response):
            response.raise_for_status()
            return
        if response.status_code < 400:
            return
        body = await response.aread()
        error_response = response.to_httpx_response(content=body)
        error_response.raise_for_status()

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
            return normalized
        if not isinstance(normalized.get("reasoning_effort"), str):
            normalized["reasoning_effort"] = effort
        return normalized

    @staticmethod
    def _route_endpoint_requirements(endpoint_path: str, payload: dict[str, Any]) -> tuple[bool, bool]:
        if endpoint_path == "/responses":
            return False, True
        if endpoint_path in {"/chat/completions", "/completions"}:
            return True, False
        return endpoint_path == "/chat/completions", endpoint_path == "/responses"

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
        unsafe_fields: list[str] = []
        for key in payload.keys():
            if key in ProxyService.RESPONSES_CHAT_ADAPTER_MAPPABLE_FIELDS:
                continue
            if key not in ProxyService.RESPONSES_CHAT_ADAPTER_SAFE_FIELDS:
                unsafe_fields.append(key)
                continue
            if key == "tools":
                if not ProxyService._responses_tools_are_adapter_safe(payload.get("tools")):
                    unsafe_fields.append(key)
                continue
            if key == "tool_choice":
                if not ProxyService._responses_tool_choice_is_adapter_safe(payload.get("tool_choice")):
                    unsafe_fields.append(key)
                continue
            if key in ProxyService.ENDPOINT_ADAPTER_RISKY_FIELDS:
                unsafe_fields.append(key)
        unsafe_reasons: list[str] = []
        input_value = payload.get("input")
        if not ProxyService._responses_input_is_adapter_safe(input_value, unsafe_reasons=unsafe_reasons):
            pass
        if ProxyService._value_has_key(payload, "encrypted_content"):
            unsafe_reasons.append("responses payload contains encrypted_content state from a previous response")
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
        unsafe_fields: list[str] = []
        for key in payload.keys():
            if key not in ProxyService.CHAT_RESPONSES_ADAPTER_SAFE_FIELDS:
                unsafe_fields.append(key)
                continue
            if key == "tools":
                if not ProxyService._chat_tools_are_adapter_safe(payload.get("tools")):
                    unsafe_fields.append(key)
                continue
            if key == "tool_choice":
                if not ProxyService._chat_tool_choice_is_adapter_safe(payload.get("tool_choice")):
                    unsafe_fields.append(key)
                continue
            if key in ProxyService.ENDPOINT_ADAPTER_RISKY_FIELDS:
                unsafe_fields.append(key)
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
    def _chat_tools_are_adapter_safe(value: Any) -> bool:
        if not isinstance(value, list):
            return False
        for item in value:
            if not isinstance(item, dict) or item.get("type") != "function":
                return False
            function = item.get("function")
            if not isinstance(function, dict):
                return False
            name = function.get("name")
            if not isinstance(name, str) or not name.strip():
                return False
        return True

    @staticmethod
    def _responses_tools_are_adapter_safe(value: Any) -> bool:
        if not isinstance(value, list):
            return False
        for item in value:
            if not isinstance(item, dict) or item.get("type") != "function":
                return False
            name = item.get("name")
            if not isinstance(name, str) or not name.strip():
                return False
        return True

    @staticmethod
    def _chat_tool_choice_is_adapter_safe(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, str):
            return value in {"auto", "none", "required"}
        if not isinstance(value, dict) or value.get("type") != "function":
            return False
        function = value.get("function")
        return isinstance(function, dict) and isinstance(function.get("name"), str) and bool(function.get("name").strip())

    @staticmethod
    def _responses_tool_choice_is_adapter_safe(value: Any) -> bool:
        if value is None:
            return True
        if isinstance(value, str):
            return value in {"auto", "none", "required"}
        if not isinstance(value, dict) or value.get("type") != "function":
            return False
        name = value.get("name")
        return isinstance(name, str) and bool(name.strip())

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
            if (
                isinstance(part_type, str)
                and part_type in {"input_text", "text", "output_text"}
                and isinstance(part.get("text"), str)
            ):
                continue
            if (isinstance(part_type, str) and part_type in {"input_image", "image_url"}) or "image_url" in part:
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
            if isinstance(part_type, str) and part_type in {"text", "input_text"} and isinstance(part.get("text"), str):
                continue
            if (isinstance(part_type, str) and part_type in {"image_url", "input_image"}) or "image_url" in part:
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
                risky_keys = sorted(set(item.keys()) & {"function_call", "refusal", "audio"})
                if risky_keys:
                    unsafe_reasons.append(f"chat response contains risky keys: {', '.join(risky_keys)}")
                if "tool_calls" in item and not ProxyService._chat_response_tool_calls_are_adapter_safe(item.get("tool_calls")):
                    unsafe_reasons.append("chat response tool_calls are not convertible")
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
                if item_type in {"function_call", "tool_call"}:
                    if not ProxyService._responses_output_tool_call_is_adapter_safe(item):
                        unsafe_reasons.append("responses output tool call item is not convertible")
                    continue
                if item_type not in {None, "message"}:
                    unsafe_reasons.append(f"responses output item type {item_type!r} is not convertible")
                    continue
                risky_keys = sorted(set(item.keys()) & {"reasoning", "code_interpreter_call", "file_search_call"})
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
    def _chat_response_tool_calls_are_adapter_safe(value: Any) -> bool:
        if not isinstance(value, list) or not value:
            return False
        for item in value:
            if not isinstance(item, dict):
                return False
            if item.get("type") != "function":
                return False
            function = item.get("function")
            if not isinstance(function, dict):
                return False
            if not isinstance(function.get("name"), str) or not function.get("name", "").strip():
                return False
            if not isinstance(function.get("arguments"), str):
                return False
        return True

    @staticmethod
    def _responses_output_tool_call_is_adapter_safe(item: dict[str, Any]) -> bool:
        name = item.get("name")
        arguments = item.get("arguments")
        if not isinstance(name, str) or not name.strip():
            return False
        if not isinstance(arguments, str):
            return False
        return True

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
        return False

    @staticmethod
    def _classify_retry_policy(status_code: int, detail: Any | None = None) -> dict[str, Any]:
        return OpenAIErrorService.classify_error(status_code=status_code, detail=detail)

    @staticmethod
    def _should_retry_same_provider_status(status_code: int, detail: Any | None = None) -> bool:
        classified = ProxyService._classify_retry_policy(status_code, detail)
        if not bool(classified.get("recoverable")):
            return False
        # 429/425 can recover after time or another route, but immediate same-provider
        # retries usually amplify upstream throttling.
        if status_code in {425, status.HTTP_429_TOO_MANY_REQUESTS}:
            return False
        if 400 <= status_code < 500 and status_code not in {408, 409}:
            return False
        return True

    @staticmethod
    def _same_provider_retry_budget(provider: Provider, setting: Any) -> int:
        return max(1, min(int(provider.max_retries or 1), int(setting.global_max_retries or 1), 2))

    @staticmethod
    def _route_exhausted_retry_infinite_enabled(
        setting: Any,
        route_context: RoutePolicyContext | None = None,
    ) -> bool:
        return bool(getattr(setting, "route_exhausted_retry_infinite_enabled", False))

    @staticmethod
    def _route_exhausted_retry_max_wait_seconds(setting: Any) -> int:
        value = getattr(setting, "route_exhausted_retry_max_wait_seconds", 600)
        try:
            return max(0, min(int(value or 0), 600))
        except (TypeError, ValueError):
            return 600

    @staticmethod
    def _route_exhausted_retry_elapsed_seconds(*, started_at: float) -> float:
        return max(0.0, time.perf_counter() - started_at)

    @staticmethod
    def _route_exhausted_retry_sleep_seconds(
        setting: Any,
        *,
        started_at: float,
        retry_round: int,
        route_context: RoutePolicyContext | None = None,
    ) -> float:
        backoff_seconds = (1, 2, 5, 10, 15, 30)
        wait_seconds = float(backoff_seconds[min(max(0, retry_round), len(backoff_seconds) - 1)])
        if ProxyService._route_exhausted_retry_infinite_enabled(setting, route_context):
            return wait_seconds
        max_wait_seconds = ProxyService._route_exhausted_retry_max_wait_seconds(setting)
        if max_wait_seconds <= 0:
            return 0.0
        elapsed_seconds = ProxyService._route_exhausted_retry_elapsed_seconds(started_at=started_at)
        remaining_seconds = max_wait_seconds - elapsed_seconds
        if remaining_seconds <= 0:
            return 0.0
        return max(0.0, min(wait_seconds, remaining_seconds))

    @staticmethod
    def _append_route_exhausted_retry_trace(
        trace: list[dict],
        *,
        reason: str,
        sleep_seconds: float,
        started_at: float,
        retry_round: int,
        setting: Any,
        route_context: RoutePolicyContext | None = None,
        diagnostics: dict[str, Any] | None = None,
        upstream_error: dict[str, Any] | None = None,
    ) -> None:
        infinite_retry_enabled = ProxyService._route_exhausted_retry_infinite_enabled(setting, route_context)
        item: dict[str, Any] = {
            "result": "route_exhausted_wait_retry",
            "reason": reason,
            "retry_round": retry_round + 1,
            "sleep_seconds": round(sleep_seconds, 3),
            "elapsed_seconds": round(ProxyService._route_exhausted_retry_elapsed_seconds(started_at=started_at), 3),
            "max_wait_seconds": ProxyService._route_exhausted_retry_max_wait_seconds(setting),
            "infinite_retry_enabled": infinite_retry_enabled,
            "retry_mode": "infinite" if infinite_retry_enabled else "normal",
        }
        if diagnostics is not None:
            item["diagnostic_summary"] = diagnostics.get("summary")
            item["reason_counts"] = diagnostics.get("reason_counts")
        if upstream_error is not None:
            item["last_status_code"] = upstream_error.get("status_code")
            item["last_error_code"] = ProxyService._error_code_from_detail(upstream_error.get("detail"))
        trace.append(item)

    @staticmethod
    def _should_retry_route_diagnostics(diagnostics: dict[str, Any] | None) -> bool:
        if not diagnostics:
            return False
        matching_model_mount_count = int(diagnostics.get("matching_model_mount_count") or 0)
        pre_capacity_candidate_count = int(diagnostics.get("pre_capacity_candidate_count") or 0)
        final_candidate_count = int(diagnostics.get("final_candidate_count") or 0)
        reason_counts = diagnostics.get("reason_counts") or {}
        # 没有任何匹配模型挂载时，等待熔断恢复也无法凭空产生候选，直接判定为不可恢复。
        if matching_model_mount_count <= 0:
            return False
        if pre_capacity_candidate_count > 0 and final_candidate_count == 0:
            return True
        nonrecoverable_match_reasons = {
            "provider_not_authorized",
            "model_disabled",
            "model_globally_disabled",
            "vision_not_supported",
            "image_generation_not_supported",
            "vision_probe_unhealthy",
            "image_generation_probe_unhealthy",
            "chat_not_supported",
            "responses_not_supported",
        }
        nonrecoverable_match_count = sum(int(reason_counts.get(reason) or 0) for reason in nonrecoverable_match_reasons)
        if pre_capacity_candidate_count <= 0 and nonrecoverable_match_count >= matching_model_mount_count:
            return False
        recoverable_reasons = {
            "provider_capacity_exceeded",
            "provider_failure_rate_limited",
            "capacity_snapshot_unavailable",
            "provider_circuit_open",
            "model_circuit_open",
            "model_unhealthy",
        }
        if any(int(reason_counts.get(reason) or 0) > 0 for reason in recoverable_reasons):
            return True
        return False

    @staticmethod
    def _should_retry_route_upstream_error(upstream_error: dict[str, Any] | None) -> bool:
        if not upstream_error:
            return False
        try:
            status_code = int(upstream_error.get("status_code") or 0)
        except (TypeError, ValueError):
            return False
        classified = ProxyService._classify_retry_policy(status_code, upstream_error.get("detail"))
        return bool(classified.get("recoverable"))

    @staticmethod
    def _should_retry_mapped_model(upstream_error: dict[str, Any] | None) -> bool:
        if not upstream_error:
            return False
        try:
            status_code = int(upstream_error.get("status_code") or 0)
        except (TypeError, ValueError):
            return False
        classified = ProxyService._classify_retry_policy(status_code, upstream_error.get("detail"))
        category = str(classified.get("category") or "")
        if category in {"invalid_request", "authentication", "authorization", "client_cancelled"}:
            return False
        return category in {
            "model_unavailable",
            "capability_not_supported",
            "rate_limit",
            "timeout",
            "network",
            "upstream_transient",
            "server_error",
            "capacity_limited",
            "route_unavailable",
        } or bool(classified.get("recoverable"))

    @staticmethod
    def _build_route_exhausted_retry_upstream_error(
        setting: Any,
        *,
        started_at: float,
        attempt_count: int,
        trace: list[dict] | None,
        trace_id: str | None,
        last_upstream_error: dict[str, Any] | None,
    ) -> dict[str, Any]:
        elapsed_seconds = int(round(ProxyService._route_exhausted_retry_elapsed_seconds(started_at=started_at)))
        max_wait_seconds = ProxyService._route_exhausted_retry_max_wait_seconds(setting)
        effective_attempt_count = max(int(attempt_count or 0), LogService.derive_attempt_count(trace))
        detail: dict[str, Any] = {
            "message": f"所有可用提供商在 {max_wait_seconds} 秒等待重试窗口内均不可用或请求失败，已停止内部重试。",
            "code": "all_providers_unavailable_after_retry",
            "attempt_count": effective_attempt_count,
            "elapsed_seconds": elapsed_seconds,
            "max_wait_seconds": max_wait_seconds,
            "retryable": True,
        }
        if trace_id:
            detail["trace_id"] = trace_id
        if last_upstream_error is not None:
            detail["last_status_code"] = last_upstream_error.get("status_code")
            detail["last_error"] = last_upstream_error.get("detail")
        return {
            "status_code": status.HTTP_503_SERVICE_UNAVAILABLE,
            "detail": detail,
        }

    @staticmethod
    def _build_endpoint_fallback_request(
        *,
        requested_endpoint_path: str,
        failed_request_path: str,
        payload: dict[str, Any],
        primary_error: Any | None = None,
    ) -> PreparedUpstreamRequest:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "Endpoint conversion fallback is disabled; use the requested endpoint's native upstream support",
                "code": "endpoint_conversion_disabled",
                "requested_endpoint": requested_endpoint_path,
                "failed_endpoint": failed_request_path,
            },
        )

    @staticmethod
    def _prepare_upstream_request_for_provider_model(
        provider: Provider,
        provider_model: ProviderModel,
        *,
        endpoint_path: str,
        payload: dict[str, Any],
    ) -> PreparedUpstreamRequest:
        return ProxyService._prepare_upstream_request(provider, endpoint_path=endpoint_path, payload=payload)

    @staticmethod
    def _build_preselected_endpoint_fallback_trace(
        provider: Provider,
        provider_model: ProviderModel,
        *,
        prepared: PreparedUpstreamRequest,
        started: float,
    ) -> list[dict]:
        if prepared.fallback_from_path is None:
            return []
        return [
            ProxyService._build_trace_item(
                provider,
                provider_model,
                "endpoint_fallback_preselected",
                int((time.perf_counter() - started) * 1000),
                extra={
                    "from_endpoint": prepared.fallback_from_path,
                    "to_endpoint": prepared.request_path,
                    "reason": "provider_model_endpoint_not_supported",
                },
            )
        ]

    @staticmethod
    def _prepare_upstream_request(provider: Provider, *, endpoint_path: str, payload: dict[str, Any]) -> PreparedUpstreamRequest:
        internal_payload = dict(payload)
        adapt_chat_response_to_responses = bool(internal_payload.pop("__aotu_responses_chat_adapter", False))
        force_stream_usage = bool(internal_payload.pop("__aotu_include_usage", False))
        response_model_override = internal_payload.pop("__aotu_response_model_override", None)
        response_id_override = internal_payload.pop("__aotu_response_id_override", None)
        upstream_endpoint_path = endpoint_path
        adapt_chat_response_to_completions = False
        if endpoint_path == "/completions":
            internal_payload = ProxyService._convert_text_completion_request_to_chat(internal_payload)
            upstream_endpoint_path = "/chat/completions"
            adapt_chat_response_to_completions = True
        if force_stream_usage and upstream_endpoint_path == "/chat/completions":
            internal_payload = ProxyService._ensure_chat_stream_include_usage(internal_payload)
        normalized_payload = ProxyService._normalize_provider_request_payload(provider, endpoint_path=upstream_endpoint_path, payload=internal_payload)
        return PreparedUpstreamRequest(
            request_path=upstream_endpoint_path,
            request_payload=normalized_payload,
            adapt_chat_response_to_responses=adapt_chat_response_to_responses,
            adapt_chat_response_to_completions=adapt_chat_response_to_completions,
            response_model_override=response_model_override if isinstance(response_model_override, str) else None,
            response_id_override=response_id_override if isinstance(response_id_override, str) else None,
        )

    @staticmethod
    def _convert_text_completion_request_to_chat(payload: dict[str, Any]) -> dict[str, Any]:
        prompt = payload.get("prompt", "")
        content = prompt if isinstance(prompt, str) else dumps_json(prompt)
        chat_payload: dict[str, Any] = {
            "model": payload.get("model"),
            "messages": [{"role": "user", "content": content}],
        }
        for field in (
            "stream",
            "temperature",
            "top_p",
            "stop",
            "max_tokens",
            "presence_penalty",
            "frequency_penalty",
            "logit_bias",
            "user",
            "n",
            "seed",
            "metadata",
        ):
            if field in payload:
                chat_payload[field] = payload[field]
        return chat_payload

    @staticmethod
    def _mark_stream_usage_required(*, endpoint_path: str, payload: dict[str, Any], enable_token_logging: bool) -> dict[str, Any]:
        if not enable_token_logging or not isinstance(payload, dict) or payload.get("stream") is not True:
            return payload
        if endpoint_path not in {"/chat/completions", "/completions"}:
            return payload
        marked = dict(payload)
        marked["__aotu_include_usage"] = True
        return marked

    @staticmethod
    def _ensure_chat_stream_include_usage(payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict) or payload.get("stream") is not True:
            return payload
        normalized = dict(payload)
        stream_options = normalized.get("stream_options")
        if isinstance(stream_options, dict):
            stream_options = dict(stream_options)
        else:
            stream_options = {}
        stream_options["include_usage"] = True
        normalized["stream_options"] = stream_options
        return normalized

    @staticmethod
    def _normalize_provider_request_payload(provider: Provider, *, endpoint_path: str, payload: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(payload, dict):
            return payload
        normalized_payload = payload
        if endpoint_path == "/chat/completions" and ProxyService._provider_requires_max_completion_tokens(provider, payload):
            normalized_payload = ProxyService._convert_max_tokens_to_max_completion_tokens(normalized_payload)
        if ProxyService._payload_has_image(normalized_payload) and ProxyService._provider_requires_base64_image_urls(provider, normalized_payload):
            normalized_payload = ProxyService._convert_remote_image_urls_to_data_urls(normalized_payload)
        return normalized_payload

    @staticmethod
    def _provider_requires_max_completion_tokens(provider: Provider, payload: dict[str, Any]) -> bool:
        normalized_model = str(payload.get("model") or "").strip().lower()
        normalized_base_url = str(provider.base_url or "").strip().lower()
        if normalized_model.startswith("mimo-"):
            return True
        return "xiaomimimo.com" in normalized_base_url

    @staticmethod
    def _convert_max_tokens_to_max_completion_tokens(payload: dict[str, Any]) -> dict[str, Any]:
        if "max_tokens" not in payload:
            return payload
        normalized = dict(payload)
        if "max_completion_tokens" not in normalized:
            normalized["max_completion_tokens"] = normalized.get("max_tokens")
        normalized.pop("max_tokens", None)
        return normalized

    @staticmethod
    def _provider_requires_base64_image_urls(provider: Provider, payload: dict[str, Any]) -> bool:
        normalized_model = str(payload.get("model") or "").strip().lower()
        normalized_base_url = str(provider.base_url or "").strip().lower()
        if normalized_model.startswith("kimi-") or normalized_model.startswith("moonshot-v1"):
            return True
        return "moonshot.ai" in normalized_base_url or "moonshot.cn" in normalized_base_url

    @staticmethod
    def _convert_remote_image_urls_to_data_urls(value: Any) -> Any:
        if isinstance(value, list):
            return [ProxyService._convert_remote_image_urls_to_data_urls(item) for item in value]
        if not isinstance(value, dict):
            return value
        converted = {key: ProxyService._convert_remote_image_urls_to_data_urls(item) for key, item in value.items()}
        image_url_value = converted.get("image_url")
        if isinstance(image_url_value, str):
            normalized = ProxyService._convert_single_remote_image_url_to_data_url(image_url_value)
            if normalized != image_url_value:
                converted["image_url"] = normalized
            return converted
        if isinstance(image_url_value, dict):
            normalized_url = ProxyService._convert_single_remote_image_url_to_data_url(image_url_value.get("url"))
            if normalized_url != image_url_value.get("url"):
                image_payload = dict(image_url_value)
                image_payload["url"] = normalized_url
                converted["image_url"] = image_payload
        return converted

    @staticmethod
    def _convert_single_remote_image_url_to_data_url(value: Any) -> Any:
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if not normalized or normalized.startswith("data:") or not normalized.lower().startswith(("http://", "https://")):
            return value
        return ProxyService._fetch_remote_image_as_data_url(normalized)

    @staticmethod
    def _fetch_remote_image_as_data_url(url: str) -> str:
        max_image_bytes = 50 * 1024 * 1024
        timeout_seconds = 30
        session = ProxyService._get_thread_local_requests_session()
        try:
            response = session.get(url, timeout=timeout_seconds)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "message": "当前上游视觉模型不支持直接传入远程图片 URL，自动抓取该图片失败，请改为传入 base64 data URL 或可用文件 ID",
                    "code": "image_url_fetch_failed",
                    "image_url": url,
                },
            ) from exc
        content = response.content or b""
        if len(content) > max_image_bytes:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail={
                    "message": f"远程图片大小超过自动转换上限 {max_image_bytes} 字节，请改为传入 base64 data URL 或可用文件 ID",
                    "code": "image_url_too_large_for_inline_conversion",
                    "image_url": url,
                    "max_image_bytes": max_image_bytes,
                },
            )
        content_type = str(response.headers.get("content-type") or "").split(";", 1)[0].strip() or None
        encoded = base64.b64encode(content).decode("ascii")
        return ProxyService._build_data_url_from_base64(encoded, mime_type=content_type)

    @staticmethod
    def _assert_stateful_responses_mapping_safe(
        *,
        endpoint_path: str,
        payload: dict[str, Any],
        requested_model_name: Any,
        mapping_resolution: ModelMappingResolution | None,
    ) -> None:
        return

    @staticmethod
    def _model_mapping_unavailable(mapping_resolution: ModelMappingResolution | None) -> bool:
        return bool(
            mapping_resolution is not None
            and isinstance(mapping_resolution.trace, dict)
            and mapping_resolution.trace.get("result") == "model_mapping_no_available_target"
        )

    @staticmethod
    def _append_typed_request_event(
        trace: list[dict],
        event_name: str,
        payload: dict[str, Any],
        *,
        result: str = "success",
        severity: str = "info",
        module: str = "proxy",
    ) -> None:
        trace.append(
            {
                "typed_event": event_name,
                "event_result": result,
                "severity": severity,
                "module": module,
                "payload": payload,
            }
        )

    @staticmethod
    def _request_capabilities_payload(
        *,
        endpoint_path: str,
        is_stream: bool,
        has_image: bool,
        require_tools: bool,
        require_image_generation: bool,
        require_chat_completions: bool,
        require_responses: bool,
    ) -> str:
        return dumps_json(
            {
                "endpoint_path": endpoint_path,
                "stream": is_stream,
                "vision": has_image,
                "tools": require_tools,
                "image_generation": require_image_generation,
                "chat_completions": require_chat_completions,
                "responses": require_responses,
            }
        )

    @staticmethod
    def _append_auth_trace_event(trace: list[dict], api_client_auth: ApiClientAuthContext | None) -> None:
        if api_client_auth is None:
            return
        trace.append(
            {
                "typed_event": "request_auth",
                "event_result": "success",
                "severity": "info",
                "module": "proxy",
                "result": "authenticated",
                "payload": {
                    "auth_result": "authenticated",
                    "api_client_key_id": api_client_auth.api_client_key.id,
                    "api_client_key_prefix": api_client_auth.api_client_key.key_prefix,
                    "user_account_id": api_client_auth.api_client_key.owner_user_id,
                    "policy_snapshot_json": api_client_auth.policy_snapshot_json,
                },
            }
        )

    @staticmethod
    def _append_validation_trace_event(
        trace: list[dict],
        *,
        stage: str,
        passed: bool,
        request_body_json: str | None = None,
        limit_value: int | None = None,
        actual_value: int | None = None,
        error_code: str | None = None,
        safe_detail: dict[str, Any] | None = None,
    ) -> None:
        ProxyService._append_typed_request_event(
            trace,
            "request_validation",
            {
                "validation_stage": stage,
                "passed": passed,
                "limit_value": limit_value,
                "actual_value": actual_value,
                "request_body_summary_json": request_body_json,
                "error_code": error_code,
                "safe_detail_json": dumps_json(safe_detail) if safe_detail is not None else None,
            },
            result="success" if passed else "failed",
            severity="info" if passed else "warning",
        )

    @staticmethod
    def _append_model_permission_trace_event(
        trace: list[dict],
        *,
        requested_model: Any,
        resolved_model: Any,
        endpoint_path: str,
        permission_result: str,
        required_capabilities_json: str,
        reason_details: dict[str, Any] | None = None,
    ) -> None:
        ProxyService._append_typed_request_event(
            trace,
            "request_model_permission",
            {
                "requested_model": requested_model if isinstance(requested_model, str) else None,
                "resolved_model": resolved_model if isinstance(resolved_model, str) else None,
                "endpoint_path": f"/v1{endpoint_path}" if not str(endpoint_path).startswith("/v1") else endpoint_path,
                "required_capabilities_json": required_capabilities_json,
                "permission_result": permission_result,
                "reason_details_json": dumps_json(reason_details or {}),
            },
            result="success" if permission_result == "allowed" else "failed",
            severity="info" if permission_result == "allowed" else "warning",
        )

    @staticmethod
    def _append_route_decision_trace_event(
        trace: list[dict],
        *,
        route_round: int,
        candidate_count: int | None,
        selected_provider_id: int | None = None,
        selected_provider_model_id: int | None = None,
        sticky_hit: bool | None = None,
        diagnostics: dict[str, Any] | None = None,
        retry_wait_ms: int | None = None,
    ) -> None:
        ProxyService._append_typed_request_event(
            trace,
            "request_route_decision",
            {
                "route_round": route_round,
                "route_policy": "健康优先",
                "candidate_count": candidate_count,
                "selected_provider_id": selected_provider_id,
                "selected_provider_model_id": selected_provider_model_id,
                "sticky_hit": sticky_hit,
                "excluded_summary_json": dumps_json((diagnostics or {}).get("reason_counts")) if diagnostics else None,
                "diagnostics_json": dumps_json(diagnostics) if diagnostics is not None else None,
                "retry_wait_ms": retry_wait_ms,
            },
            result="success" if selected_provider_id or (candidate_count or 0) > 0 else "failed",
            severity="info" if selected_provider_id or (candidate_count or 0) > 0 else "warning",
        )

    @staticmethod
    def _append_model_mapping_trace(trace: list[dict], mapping_resolution: ModelMappingResolution | None) -> None:
        if mapping_resolution is None or not isinstance(mapping_resolution.trace, dict):
            return
        trace.append({"result": "model_mapping", **mapping_resolution.trace})

    @staticmethod
    def _append_stateful_responses_route_trace(
        trace: list[dict],
        *,
        endpoint_path: str,
        payload: dict[str, Any],
        requested_model_name: Any,
        selected_model_name: Any,
        mapping_resolution: ModelMappingResolution | None,
    ) -> None:
        if endpoint_path != "/responses" or not ProxyService._payload_has_stateful_responses_context(payload):
            return
        trace.append(
            {
                "result": "stateful_responses_routing",
                "policy": "prefer_recent_success_target_then_failover",
                "requested_model_name": requested_model_name,
                "selected_model_name": selected_model_name,
                "mapping_result": (
                    mapping_resolution.trace.get("result")
                    if mapping_resolution is not None and isinstance(mapping_resolution.trace, dict)
                    else None
                ),
                "selection_reason": (
                    mapping_resolution.trace.get("selection_reason")
                    if mapping_resolution is not None and isinstance(mapping_resolution.trace, dict)
                    else None
                ),
            }
        )

    @staticmethod
    def _remaining_model_mapping_targets(
        mapping_resolution: ModelMappingResolution | None,
        *,
        requested_model: str | None,
        current_model_name: str | None,
        excluded_target_model_names: set[str] | None = None,
    ) -> list[str]:
        if (
            mapping_resolution is None
            or not requested_model
            or mapping_resolution.selected_model_name == requested_model
        ):
            return []
        excluded = excluded_target_model_names or set()
        current = (current_model_name or "").strip()
        remaining: list[str] = []
        for item in mapping_resolution.candidate_model_names:
            model_name = str(item or "").strip()
            if not model_name or model_name == current or model_name in excluded:
                continue
            remaining.append(model_name)
        return remaining

    @staticmethod
    def _append_model_mapping_failover_trace(
        trace: list[dict],
        *,
        source_model_name: str | None,
        failed_model_name: str | None,
        remaining_model_names: list[str],
        reason: str,
        upstream_error: dict[str, Any] | None = None,
        route_diagnostics: dict[str, Any] | None = None,
        stateful_responses_context: bool = False,
    ) -> None:
        item: dict[str, Any] = {
            "result": "model_mapping_failover",
            "source_model_name": source_model_name,
            "failed_model_name": failed_model_name,
            "reason": reason,
            "remaining_model_names": remaining_model_names,
        }
        if stateful_responses_context:
            item["stateful_responses_context"] = True
            item["stateful_policy"] = "previous_target_unavailable_or_recoverable_failure_then_failover"
        if upstream_error is not None:
            item["status_code"] = upstream_error.get("status_code")
            item["error_code"] = ProxyService._error_code_from_detail(upstream_error.get("detail"))
            item["error_message"] = ProxyService._error_message_for_log(upstream_error.get("detail"))
        if route_diagnostics is not None:
            item["diagnostic_summary"] = route_diagnostics.get("summary")
            item["reason_counts"] = route_diagnostics.get("reason_counts")
        trace.append(item)

    @staticmethod
    async def _retry_with_next_mapped_model_json(
        *,
        db: Session | None,
        endpoint_path: str,
        payload: dict[str, Any],
        log_type: str,
        forced_provider_id: int | None,
        route_context: RoutePolicyContext | None,
        api_client_auth: ApiClientAuthContext | None,
        trace_id: str | None,
        source_ip: str | None,
        request_path_for_log: str | None,
        public_endpoint_path: str | None,
        response_transform: Callable[[dict[str, Any]], dict[str, Any]] | None,
        suppress_success_log: bool,
        route_retry_started_at: float | None,
        route_retry_round: int,
        route_retry_trace: list[dict],
        route_retry_attempt_count: int,
        mapping_resolution: ModelMappingResolution | None,
        requested_model_name: str | None,
        current_model_name: str | None,
        excluded_target_model_names: tuple[str, ...],
        request_id: str,
        conversation_key: str,
        session_id: str,
        reason: str,
        upstream_error: dict[str, Any] | None = None,
        route_diagnostics: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], Provider, list[dict], int] | None:
        remaining = ProxyService._remaining_model_mapping_targets(
            mapping_resolution,
            requested_model=requested_model_name,
            current_model_name=current_model_name,
            excluded_target_model_names=set(excluded_target_model_names),
        )
        if not remaining:
            return None
        next_excluded = tuple(
            dict.fromkeys(
                [
                    *(item for item in excluded_target_model_names if isinstance(item, str) and item.strip()),
                    *(item for item in ((current_model_name or "").strip(),) if item),
                ]
            )
        )
        next_trace = list(route_retry_trace)
        ProxyService._append_model_mapping_failover_trace(
            next_trace,
            source_model_name=requested_model_name,
            failed_model_name=current_model_name,
            remaining_model_names=remaining,
            reason=reason,
            upstream_error=upstream_error,
            route_diagnostics=route_diagnostics,
            stateful_responses_context=(
                endpoint_path == "/responses"
                and ProxyService._payload_has_stateful_responses_context(payload)
            ),
        )
        retry_payload = {**payload, "model": requested_model_name}
        return await ProxyService._forward_json_request_once(
            db,
            endpoint_path=endpoint_path,
            payload=retry_payload,
            log_type=log_type,
            forced_provider_id=forced_provider_id,
            route_context=route_context,
            api_client_auth=api_client_auth,
            trace_id=trace_id,
            source_ip=source_ip,
            request_path_for_log=request_path_for_log,
            public_endpoint_path=public_endpoint_path,
            response_transform=response_transform,
            suppress_success_log=suppress_success_log,
            route_retry_started_at=route_retry_started_at,
            route_retry_round=route_retry_round,
            route_retry_trace=next_trace,
            route_retry_attempt_count=route_retry_attempt_count,
            mapping_failover_excluded_target_model_names=next_excluded,
            request_id_override=request_id,
            conversation_key_override=conversation_key,
            session_id_override=session_id,
        )

    @staticmethod
    async def _retry_with_next_mapped_model_stream(
        *,
        db: Session | None,
        endpoint_path: str,
        payload: dict[str, Any],
        log_type: str,
        forced_provider_id: int | None,
        route_context: RoutePolicyContext | None,
        api_client_auth: ApiClientAuthContext | None,
        trace_id: str | None,
        source_ip: str | None,
        request_path_for_log: str | None,
        public_endpoint_path: str | None,
        route_retry_started_at: float | None,
        route_retry_round: int,
        route_retry_trace: list[dict],
        route_retry_attempt_count: int,
        mapping_resolution: ModelMappingResolution | None,
        requested_model_name: str | None,
        current_model_name: str | None,
        excluded_target_model_names: tuple[str, ...],
        request_id: str,
        conversation_key: str,
        session_id: str,
        reason: str,
        upstream_error: dict[str, Any] | None = None,
        route_diagnostics: dict[str, Any] | None = None,
    ) -> tuple[AsyncIterator[bytes], Provider, list[dict], int] | None:
        remaining = ProxyService._remaining_model_mapping_targets(
            mapping_resolution,
            requested_model=requested_model_name,
            current_model_name=current_model_name,
            excluded_target_model_names=set(excluded_target_model_names),
        )
        if not remaining:
            return None
        next_excluded = tuple(
            dict.fromkeys(
                [
                    *(item for item in excluded_target_model_names if isinstance(item, str) and item.strip()),
                    *(item for item in ((current_model_name or "").strip(),) if item),
                ]
            )
        )
        next_trace = list(route_retry_trace)
        ProxyService._append_model_mapping_failover_trace(
            next_trace,
            source_model_name=requested_model_name,
            failed_model_name=current_model_name,
            remaining_model_names=remaining,
            reason=reason,
            upstream_error=upstream_error,
            route_diagnostics=route_diagnostics,
            stateful_responses_context=(
                endpoint_path == "/responses"
                and ProxyService._payload_has_stateful_responses_context(payload)
            ),
        )
        retry_payload = {**payload, "model": requested_model_name}
        return await ProxyService._forward_stream_request_once(
            db,
            endpoint_path=endpoint_path,
            payload=retry_payload,
            log_type=log_type,
            forced_provider_id=forced_provider_id,
            route_context=route_context,
            api_client_auth=api_client_auth,
            trace_id=trace_id,
            source_ip=source_ip,
            request_path_for_log=request_path_for_log,
            public_endpoint_path=public_endpoint_path,
            route_retry_started_at=route_retry_started_at,
            route_retry_round=route_retry_round,
            route_retry_trace=next_trace,
            route_retry_attempt_count=route_retry_attempt_count,
            mapping_failover_excluded_target_model_names=next_excluded,
            request_id_override=request_id,
            conversation_key_override=conversation_key,
            session_id_override=session_id,
        )

    @staticmethod
    def _model_mapping_unavailable_diagnostics(mapping_resolution: ModelMappingResolution | None) -> dict[str, Any]:
        mapping_trace = mapping_resolution.trace if mapping_resolution is not None else {}
        target_diagnostics = [
            item.get("route_diagnostics")
            for item in (mapping_trace.get("targets") or [])
            if isinstance(item, dict) and isinstance(item.get("route_diagnostics"), dict)
        ]
        summary = "模型映射没有可用目标"
        aggregated_reason_counts: dict[str, int] = {}
        samples: list[dict[str, Any]] = []
        for diagnostic in target_diagnostics:
            for reason_code, count in (diagnostic.get("reason_counts") or {}).items():
                aggregated_reason_counts[reason_code] = aggregated_reason_counts.get(reason_code, 0) + int(count or 0)
            for sample in diagnostic.get("samples") or []:
                if len(samples) >= 8:
                    break
                if isinstance(sample, dict):
                    samples.append(sample)
        if target_diagnostics:
            if any((diagnostic.get("summary") or "").strip() for diagnostic in target_diagnostics):
                summary = "；".join(
                    str(diagnostic.get("summary")).strip()
                    for diagnostic in target_diagnostics
                    if str(diagnostic.get("summary") or "").strip()
                )
        return {
            "summary": summary,
            "reason_counts": aggregated_reason_counts or {"model_mapping_target_not_available": 1},
            "samples": samples,
            "model_mapping": mapping_trace,
        }

    @staticmethod
    def _build_model_mapping_unavailable_error(route_diagnostics: dict[str, Any]) -> tuple[str, str]:
        reason_counts = route_diagnostics.get("reason_counts") or {}
        if reason_counts.get("provider_not_authorized"):
            return (
                "No available mapped target model within current api key authorized providers",
                "model_mapping_target_provider_not_authorized",
            )
        if reason_counts.get("image_generation_not_supported") or reason_counts.get("image_generation_probe_unhealthy"):
            return (
                "No mapped target model supports image generation for this request",
                "model_mapping_target_image_generation_not_available",
            )
        if (
            reason_counts.get("provider_responses_protocol_not_supported")
            or reason_counts.get("responses_not_supported")
        ):
            return (
                "No mapped target model supports responses endpoint for this request",
                "model_mapping_target_responses_not_available",
            )
        if (
            reason_counts.get("provider_chat_protocol_not_supported")
            or reason_counts.get("chat_not_supported")
        ):
            return (
                "No mapped target model supports chat completions endpoint for this request",
                "model_mapping_target_chat_not_available",
            )
        return ("No available mapped target model", "model_mapping_target_not_available")

    @staticmethod
    def _build_route_unavailable_error_detail(
        route_diagnostics: dict[str, Any],
        *,
        message: str,
        code: str,
        endpoint_path: str,
        requested_model: str | None,
        selected_model: str | None,
        requires_tools: bool,
        requires_image_generation: bool,
        model_mapping_unavailable: bool,
    ) -> dict[str, Any]:
        requested_endpoint = ProxyService._external_v1_endpoint(endpoint_path)
        endpoint_detail = ProxyService._endpoint_unavailable_detail(route_diagnostics, requested_endpoint=requested_endpoint)
        if endpoint_detail is not None:
            message = str(endpoint_detail["message"])
            code = str(endpoint_detail["code"])
        detail: dict[str, Any] = {
            "message": message,
            "code": code,
            "requested_model": requested_model,
            "selected_model": selected_model,
            "requested_endpoint": requested_endpoint,
            "requires_tools": requires_tools,
            "requires_image_generation": requires_image_generation,
            "model_mapping_unavailable": model_mapping_unavailable,
            "retryable": False,
            "recoverable": False,
            "route_diagnostics": route_diagnostics,
        }
        if endpoint_detail is not None:
            detail.update(endpoint_detail)
        return detail

    @staticmethod
    def _external_v1_endpoint(endpoint_path: str) -> str:
        normalized = endpoint_path if endpoint_path.startswith("/") else f"/{endpoint_path}"
        return normalized if normalized.startswith("/v1/") else f"/v1{normalized}"

    @staticmethod
    def _endpoint_unavailable_detail(
        route_diagnostics: dict[str, Any],
        *,
        requested_endpoint: str,
    ) -> dict[str, Any] | None:
        reason_counts = route_diagnostics.get("reason_counts") or {}
        if requested_endpoint == "/v1/responses":
            reasons = {
                "provider_responses_protocol_not_supported",
                "responses_not_supported",
            }
            if not any(int(reason_counts.get(reason) or 0) > 0 for reason in reasons):
                return None
            return ProxyService._build_endpoint_capability_error_detail(
                route_diagnostics,
                code="responses_endpoint_not_supported",
                requested_endpoint=requested_endpoint,
                required_endpoint="/v1/responses",
                required_protocol="responses",
                missing_capability="native_responses_endpoint",
                primary_reason_code=ProxyService._first_present_reason(reason_counts, reasons),
            )
        if requested_endpoint == "/v1/chat/completions":
            reasons = {
                "provider_chat_protocol_not_supported",
                "chat_not_supported",
            }
            if not any(int(reason_counts.get(reason) or 0) > 0 for reason in reasons):
                return None
            return ProxyService._build_endpoint_capability_error_detail(
                route_diagnostics,
                code="chat_completions_endpoint_not_supported",
                requested_endpoint=requested_endpoint,
                required_endpoint="/v1/chat/completions",
                required_protocol="chat_completions",
                missing_capability="native_chat_completions_endpoint",
                primary_reason_code=ProxyService._first_present_reason(reason_counts, reasons),
            )
        return None

    @staticmethod
    def _build_endpoint_capability_error_detail(
        route_diagnostics: dict[str, Any],
        *,
        code: str,
        requested_endpoint: str,
        required_endpoint: str,
        required_protocol: str,
        missing_capability: str,
        primary_reason_code: str | None,
    ) -> dict[str, Any]:
        reason_details = ProxyService._route_reason_details(route_diagnostics)
        reason_summary = "；".join(
            f"{item['label']} {item['count']}"
            for item in reason_details[:5]
        )
        if not reason_summary:
            reason_summary = str(route_diagnostics.get("summary") or "未记录到候选筛除原因")
        return {
            "message": (
                f"请求入口 {requested_endpoint} 需要原生 {required_protocol} 端点，"
                f"但当前候选提供商或模型不支持该端点。具体原因：{reason_summary}。"
            ),
            "code": code,
            "requested_endpoint": requested_endpoint,
            "required_endpoint": required_endpoint,
            "required_protocol": required_protocol,
            "missing_capability": missing_capability,
            "primary_reason": {
                "code": primary_reason_code,
                "label": RouterService._diagnostic_reason_label(primary_reason_code) if primary_reason_code else None,
            },
            "reason_counts": route_diagnostics.get("reason_counts") or {},
            "reason_details": reason_details,
            "diagnostic_samples": route_diagnostics.get("samples") or [],
        }

    @staticmethod
    def _route_reason_details(route_diagnostics: dict[str, Any]) -> list[dict[str, Any]]:
        reason_counts = route_diagnostics.get("reason_counts") or {}
        ordered = sorted(reason_counts.items(), key=lambda item: (-int(item[1] or 0), item[0]))
        return [
            {
                "code": str(reason_code),
                "label": RouterService._diagnostic_reason_label(str(reason_code)),
                "count": int(count or 0),
            }
            for reason_code, count in ordered
        ]

    @staticmethod
    def _first_present_reason(reason_counts: dict[str, Any], reasons: set[str]) -> str | None:
        ordered = sorted(
            ((reason, int(reason_counts.get(reason) or 0)) for reason in reasons),
            key=lambda item: (-item[1], item[0]),
        )
        for reason, count in ordered:
            if count > 0:
                return reason
        return None

    @staticmethod
    def _restore_mapped_response_model(
        response: dict[str, Any],
        *,
        mapping_resolution: ModelMappingResolution | None,
        requested_model: str | None,
    ) -> dict[str, Any]:
        if (
            mapping_resolution is None
            or not requested_model
            or mapping_resolution.selected_model_name == requested_model
            or not isinstance(response, dict)
            or "model" not in response
        ):
            return response
        restored = dict(response)
        restored["model"] = requested_model
        return restored

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
        if "tools" in responses_payload:
            responses_payload["tools"] = ProxyService._normalize_chat_tools_for_responses(responses_payload.get("tools"))
        if "tool_choice" in responses_payload:
            responses_payload["tool_choice"] = ProxyService._normalize_chat_tool_choice_for_responses(
                responses_payload.get("tool_choice")
            )
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
        if isinstance(part_type, str) and part_type in {"text", "input_text"} and isinstance(part.get("text"), str):
            return {"type": "input_text", "text": part["text"]}
        if (isinstance(part_type, str) and part_type in {"image_url", "input_image"}) or "image_url" in part:
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

        passthrough_keys = {
            "temperature",
            "top_p",
            "presence_penalty",
            "frequency_penalty",
            "tools",
            "tool_choice",
            "stream",
            "user",
            "metadata",
            "seed",
        }
        for key in passthrough_keys:
            if key in payload:
                chat_payload[key] = payload[key]
        if "parallel_tool_calls" in payload:
            chat_payload["parallel_tool_calls"] = payload["parallel_tool_calls"]
        if "tools" in chat_payload:
            chat_payload["tools"] = ProxyService._normalize_responses_tools_for_chat(chat_payload.get("tools"))
        if "tool_choice" in chat_payload:
            chat_payload["tool_choice"] = ProxyService._normalize_responses_tool_choice_for_chat(
                chat_payload.get("tool_choice")
            )
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
        if (
            isinstance(part_type, str)
            and part_type in {"input_text", "text", "output_text"}
            and isinstance(part.get("text"), str)
        ):
            return {"type": "text", "text": part["text"]}
        if (isinstance(part_type, str) and part_type in {"input_image", "image_url"}) or "image_url" in part:
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
    def _normalize_chat_tools_for_responses(value: Any) -> Any:
        if not isinstance(value, list):
            return value
        normalized: list[Any] = []
        for item in value:
            if not isinstance(item, dict):
                normalized.append(item)
                continue
            if item.get("type") != "function":
                normalized.append(item)
                continue
            function = item.get("function")
            if not isinstance(function, dict):
                normalized.append(item)
                continue
            converted = {"type": "function"}
            for key in ("name", "description", "parameters", "strict"):
                if key in function:
                    converted[key] = function[key]
            normalized.append(converted)
        return normalized

    @staticmethod
    def _normalize_responses_tools_for_chat(value: Any) -> Any:
        if not isinstance(value, list):
            return value
        normalized: list[Any] = []
        for item in value:
            if not isinstance(item, dict):
                normalized.append(item)
                continue
            if item.get("type") != "function" or isinstance(item.get("function"), dict):
                normalized.append(item)
                continue
            converted = {"type": "function", "function": {}}
            for key in ("name", "description", "parameters", "strict"):
                if key in item:
                    converted["function"][key] = item[key]
            normalized.append(converted)
        return normalized

    @staticmethod
    def _normalize_chat_tool_choice_for_responses(value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        if value.get("type") != "function":
            return value
        function = value.get("function")
        if not isinstance(function, dict):
            return value
        name = function.get("name")
        if not isinstance(name, str) or not name.strip():
            return value
        return {"type": "function", "name": name.strip()}

    @staticmethod
    def _normalize_responses_tool_choice_for_chat(value: Any) -> Any:
        if not isinstance(value, dict):
            return value
        if value.get("type") != "function" or isinstance(value.get("function"), dict):
            return value
        name = value.get("name")
        if not isinstance(name, str) or not name.strip():
            return value
        return {"type": "function", "function": {"name": name.strip()}}

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
        tool_call_items: list[dict[str, Any]] = []
        if isinstance(choices, list) and choices:
            first_choice = choices[0] if isinstance(choices[0], dict) else {}
            message = first_choice.get("message") if isinstance(first_choice, dict) else {}
            if isinstance(message, dict) and isinstance(message.get("content"), str):
                assistant_text = message["content"]
            if isinstance(message, dict):
                tool_call_items = ProxyService._convert_chat_tool_calls_to_responses_output(message.get("tool_calls"))
            finish_reason = str(first_choice.get("finish_reason") or finish_reason)
        usage = chat_response.get("usage") if isinstance(chat_response.get("usage"), dict) else {}
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        total_tokens = usage.get("total_tokens")
        cache_read_tokens, cache_write_tokens = LogService.extract_cache_tokens({"usage": usage})
        normalized_prompt_tokens = LogService.normalize_prompt_tokens_for_cache_usage(
            usage,
            ProxyService._coerce_non_negative_int(prompt_tokens),
        )
        responses_usage = {
            "input_tokens": int(normalized_prompt_tokens or 0),
            "output_tokens": int(completion_tokens or 0),
            "total_tokens": int(total_tokens or ((normalized_prompt_tokens or 0) + (completion_tokens or 0))),
        }
        if cache_read_tokens is not None or cache_write_tokens is not None:
            responses_usage["input_tokens_details"] = {
                "cached_tokens": int(cache_read_tokens or 0),
                "cache_creation_tokens": int(cache_write_tokens or 0),
                "cache_creation_input_tokens": int(cache_write_tokens or 0),
            }
        output_items: list[dict[str, Any]] = []
        if assistant_text or not tool_call_items:
            output_items.append(
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
            )
        output_items.extend(tool_call_items)
        return {
            "id": response_id,
            "object": "response",
            "created_at": created_at,
            "status": "completed",
            "model": model_name,
            "output_text": assistant_text,
            "output": output_items,
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
        tool_calls = ProxyService._extract_tool_calls_from_responses_output(responses_payload)
        usage = responses_payload.get("usage") if isinstance(responses_payload.get("usage"), dict) else {}
        input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
        output_tokens = usage.get("output_tokens", usage.get("completion_tokens"))
        total_tokens = usage.get("total_tokens")
        cache_read_tokens, cache_write_tokens = LogService.extract_cache_tokens({"usage": usage})
        normalized_input_tokens = LogService.normalize_prompt_tokens_for_cache_usage(
            usage,
            ProxyService._coerce_non_negative_int(input_tokens),
        )
        chat_usage = {
            "prompt_tokens": int(normalized_input_tokens or 0),
            "completion_tokens": int(output_tokens or 0),
            "total_tokens": int(total_tokens or ((normalized_input_tokens or 0) + (output_tokens or 0))),
        }
        if cache_read_tokens is not None or cache_write_tokens is not None:
            chat_usage["prompt_tokens_details"] = {
                "cached_tokens": int(cache_read_tokens or 0),
                "cache_creation_tokens": int(cache_write_tokens or 0),
                "cache_creation_input_tokens": int(cache_write_tokens or 0),
            }
        finish_reason_value = "tool_calls" if tool_calls else (finish_reason if finish_reason != "completed" else "stop")
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
                        "tool_calls": tool_calls or None,
                    },
                    "finish_reason": finish_reason_value,
                }
            ],
            "usage": chat_usage,
        }

    @staticmethod
    def _convert_chat_completion_to_text_completion(
        chat_response: dict[str, Any],
        *,
        requested_model: str,
    ) -> dict[str, Any]:
        response_id = str(chat_response.get("id") or f"cmpl_{uuid4().hex}")
        created_at = chat_response.get("created") or int(datetime.utcnow().timestamp())
        model_name = str(chat_response.get("model") or requested_model or "")
        choices = chat_response.get("choices") if isinstance(chat_response.get("choices"), list) else []
        text_choices: list[dict[str, Any]] = []
        for index, choice in enumerate(choices):
            if not isinstance(choice, dict):
                continue
            message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
            text = message.get("content") if isinstance(message, dict) and isinstance(message.get("content"), str) else ""
            text_choices.append(
                {
                    "text": text,
                    "index": choice.get("index", index),
                    "logprobs": None,
                    "finish_reason": choice.get("finish_reason") or "stop",
                }
            )
        if not text_choices:
            text_choices.append({"text": "", "index": 0, "logprobs": None, "finish_reason": "stop"})
        payload: dict[str, Any] = {
            "id": response_id.replace("chatcmpl_", "cmpl_", 1),
            "object": "text_completion",
            "created": int(created_at or datetime.utcnow().timestamp()),
            "model": model_name,
            "choices": text_choices,
        }
        if isinstance(chat_response.get("usage"), dict):
            payload["usage"] = chat_response["usage"]
        return payload

    @staticmethod
    def _convert_chat_tool_calls_to_responses_output(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        items: list[dict[str, Any]] = []
        for tool_call in value:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function")
            if tool_call.get("type") != "function" or not isinstance(function, dict):
                continue
            name = function.get("name")
            arguments = function.get("arguments")
            if not isinstance(name, str) or not isinstance(arguments, str):
                continue
            tool_call_id = str(tool_call.get("id") or f"fc_{uuid4().hex}")
            items.append(
                {
                    "id": tool_call_id,
                    "type": "function_call",
                    "status": "completed",
                    "call_id": tool_call_id,
                    "name": name,
                    "arguments": arguments,
                }
            )
        return items

    @staticmethod
    def _extract_tool_calls_from_responses_output(responses_payload: dict[str, Any]) -> list[dict[str, Any]]:
        output = responses_payload.get("output")
        if not isinstance(output, list):
            return []
        tool_calls: list[dict[str, Any]] = []
        for item in output:
            if not isinstance(item, dict):
                continue
            item_type = item.get("type")
            if item_type not in {"function_call", "tool_call"}:
                continue
            name = item.get("name")
            arguments = item.get("arguments")
            if not isinstance(name, str) or not isinstance(arguments, str):
                continue
            tool_call_id = str(item.get("call_id") or item.get("id") or f"call_{uuid4().hex}")
            tool_calls.append(
                {
                    "id": tool_call_id,
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": arguments,
                    },
                }
            )
        return tool_calls

    @staticmethod
    def _create_responses_stream_state(*, payload: dict[str, Any], response_id: str | None = None) -> dict[str, Any]:
        return {
            "buffer": bytearray(),
            "response_id": response_id if isinstance(response_id, str) and response_id.strip() else f"resp_{uuid4().hex}",
            "message_id": f"msg_{uuid4().hex}",
            "created_at": int(datetime.utcnow().timestamp()),
            "model": str(payload.get("model") or ""),
            "output_text_parts": [],
            "tool_calls": {},
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
                    state["created_at"] = int(parsed.get("created") or state["created_at"])
                    state["model"] = str(requested_model or parsed.get("model") or state["model"])
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
                ProxyService._merge_chat_stream_delta_tool_calls(parsed, state=state)
                usage = parsed.get("usage")
                if isinstance(usage, dict):
                    input_tokens = usage.get("prompt_tokens", usage.get("input_tokens"))
                    output_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
                    total_tokens = usage.get("total_tokens")
                    cache_read_tokens, cache_write_tokens = LogService.extract_cache_tokens({"usage": usage})
                    normalized_input_tokens = LogService.normalize_prompt_tokens_for_cache_usage(
                        usage,
                        ProxyService._coerce_non_negative_int(input_tokens),
                    )
                    if normalized_input_tokens is not None:
                        state["usage"]["input_tokens"] = normalized_input_tokens
                    if isinstance(output_tokens, int):
                        state["usage"]["output_tokens"] = output_tokens
                    if isinstance(total_tokens, int):
                        state["usage"]["total_tokens"] = total_tokens
                    if cache_read_tokens is not None or cache_write_tokens is not None:
                        state["usage"]["input_tokens_details"] = {
                            "cached_tokens": int(cache_read_tokens or 0),
                            "cache_creation_tokens": int(cache_write_tokens or 0),
                            "cache_creation_input_tokens": int(cache_write_tokens or 0),
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
        tool_call_items = ProxyService._responses_output_from_chat_stream_tool_calls(state.get("tool_calls"))
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
            "output": (
                [
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
                ]
                if output_text or not tool_call_items
                else []
            ) + tool_call_items,
            "usage": usage,
        }
        return [
            *[
                ProxyService._format_sse_event(
                    {
                        "type": "response.function_call.completed",
                        "response_id": state["response_id"],
                        "item": item,
                    }
                )
                for item in tool_call_items
            ],
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
    def _merge_chat_stream_delta_tool_calls(event_json: dict[str, Any], *, state: dict[str, Any]) -> None:
        choices = event_json.get("choices")
        if not isinstance(choices, list):
            return
        tool_calls_state = state.get("tool_calls")
        if not isinstance(tool_calls_state, dict):
            tool_calls_state = {}
            state["tool_calls"] = tool_calls_state
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, dict):
                continue
            tool_calls = delta.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            for fallback_index, tool_call_delta in enumerate(tool_calls):
                if not isinstance(tool_call_delta, dict):
                    continue
                index_value = tool_call_delta.get("index", fallback_index)
                try:
                    index = int(index_value)
                except (TypeError, ValueError):
                    index = fallback_index
                current = tool_calls_state.setdefault(
                    index,
                    {"id": None, "type": "function", "function": {"name": "", "arguments": ""}},
                )
                if isinstance(tool_call_delta.get("id"), str):
                    current["id"] = tool_call_delta["id"]
                if isinstance(tool_call_delta.get("type"), str):
                    current["type"] = tool_call_delta["type"]
                function_delta = tool_call_delta.get("function")
                if isinstance(function_delta, dict):
                    current_function = current.setdefault("function", {"name": "", "arguments": ""})
                    if isinstance(function_delta.get("name"), str):
                        current_function["name"] = f"{current_function.get('name') or ''}{function_delta['name']}"
                    if isinstance(function_delta.get("arguments"), str):
                        current_function["arguments"] = f"{current_function.get('arguments') or ''}{function_delta['arguments']}"

    @staticmethod
    def _responses_output_from_chat_stream_tool_calls(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, dict):
            return []
        ordered_calls = [value[index] for index in sorted(value) if isinstance(value.get(index), dict)]
        return ProxyService._convert_chat_tool_calls_to_responses_output(ordered_calls)

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
    def _create_text_completion_stream_state(*, payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "buffer": bytearray(),
            "chunk_id": f"cmpl_{uuid4().hex}",
            "created": int(datetime.utcnow().timestamp()),
            "model": str(payload.get("model") or ""),
            "finish_reason": None,
            "completed_sent": False,
        }

    @staticmethod
    def _adapt_chat_stream_chunk_to_text_completion_events(
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
                    events.extend(ProxyService._build_text_completion_stream_done_events(state))
                    continue
                parsed = safeJsonParse(data)
                if not isinstance(parsed, dict):
                    continue
                state["chunk_id"] = str(parsed.get("id") or state["chunk_id"]).replace("chatcmpl_", "cmpl_", 1)
                state["created"] = int(parsed.get("created") or state["created"])
                state["model"] = str(parsed.get("model") or requested_model or state["model"])
                choices = parsed.get("choices") if isinstance(parsed.get("choices"), list) else []
                for index, choice in enumerate(choices):
                    if not isinstance(choice, dict):
                        continue
                    delta = choice.get("delta") if isinstance(choice.get("delta"), dict) else {}
                    text = delta.get("content") if isinstance(delta, dict) and isinstance(delta.get("content"), str) else ""
                    finish_reason = choice.get("finish_reason")
                    if isinstance(finish_reason, str) and finish_reason:
                        state["finish_reason"] = finish_reason
                    if text:
                        events.append(
                            ProxyService._format_sse_event(
                                {
                                    "id": state["chunk_id"],
                                    "object": "text_completion",
                                    "created": state["created"],
                                    "model": state["model"],
                                    "choices": [
                                        {
                                            "text": text,
                                            "index": choice.get("index", index),
                                            "logprobs": None,
                                            "finish_reason": None,
                                        }
                                    ],
                                }
                            )
                        )
        return events

    @staticmethod
    def _build_text_completion_stream_done_events(state: dict[str, Any]) -> list[bytes]:
        if state.get("completed_sent"):
            return []
        state["completed_sent"] = True
        return [
            ProxyService._format_sse_event(
                {
                    "id": state["chunk_id"],
                    "object": "text_completion",
                    "created": state["created"],
                    "model": state["model"],
                    "choices": [
                        {
                            "text": "",
                            "index": 0,
                            "logprobs": None,
                            "finish_reason": state.get("finish_reason") or "stop",
                        }
                    ],
                }
            ),
            b"data: [DONE]\n\n",
        ]

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
        detail = {
            "message": message,
            "code": code,
        }
        classified = OpenAIErrorService.classify_error(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail=detail)
        payload = {
            "error": OpenAIErrorService.build_error_payload(
                message=message,
                code=code,
                trace_id=trace_id,
                error_type=str(classified["error_type"]),
                retryable=bool(classified["retryable"]),
                recoverable=bool(classified["recoverable"]),
                category=str(classified["category"]),
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=detail,
            )["error"]
        }
        return f"event: error\ndata: {dumps_json(payload)}\n\n".encode("utf-8")

    @staticmethod
    def _build_content_guard_error_detail(
        *,
        guard_result: ContentGuardResult,
        trace_id: str | None,
        retried: bool,
        final: bool = False,
    ) -> dict[str, Any]:
        message = (
            "所有可用渠道的上游响应均未通过内容完整性防护，已阻断返回。"
            if final
            else "当前渠道的上游响应未通过内容完整性防护，已尝试切换其它可用渠道。"
            if retried
            else "上游响应未通过内容完整性防护，已阻断返回。"
        )
        detail = {
            "message": message,
            "code": "content_integrity_violation",
            "content_guard": {
                "result": guard_result.result,
                "risk_level": guard_result.risk_level,
                "categories": guard_result.categories,
                "reason": guard_result.reason,
                "action": guard_result.action,
            },
        }
        classified = OpenAIErrorService.classify_error(status_code=status.HTTP_502_BAD_GATEWAY, detail=detail)
        return OpenAIErrorService.build_error_payload(
            message=message,
            code="content_integrity_violation",
            trace_id=trace_id,
            error_type=str(classified["error_type"]),
            retryable=True if retried and not final else bool(classified["retryable"]),
            recoverable=True if retried and not final else bool(classified["recoverable"]),
            category=str(classified["category"]),
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=detail,
        )

    @staticmethod
    def _content_guard_upstream_error(
        *,
        guard_result: ContentGuardResult,
        trace_id: str | None,
        retried: bool,
        final: bool = False,
    ) -> dict[str, Any]:
        return {
            "status_code": status.HTTP_502_BAD_GATEWAY,
            "detail": ProxyService._build_content_guard_error_detail(
                guard_result=guard_result,
                trace_id=trace_id,
                retried=retried,
                final=final,
            ),
        }

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
        ProviderHealthStateService.record_route_failure(
            provider,
            provider_model,
            latency_ms=latency_ms,
            error_message=error_message,
            force_unhealthy=force_unhealthy,
        )

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
        ProviderHealthStateService.record_route_success(
            provider,
            provider_model,
            latency_ms=latency_ms,
        )

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
    def _trace_with_error_response_event(
        trace: list[dict],
        *,
        status_code: int,
        detail: Any,
        retryable: bool,
        required_endpoint: str | None = None,
        missing_capability: str | None = None,
    ) -> list[dict]:
        trace_for_log = list(trace)
        classified = ProxyService._classify_retry_policy(status_code, detail)
        ProxyService._append_typed_request_event(
            trace_for_log,
            "request_error_response",
            {
                "status_code": status_code,
                "error_type": ProxyService._error_type_from_status(status_code, detail),
                "error_code": ProxyService._error_code_from_detail(detail),
                "public_message": ProxyService._error_message_for_log(detail),
                "category": classified.get("category"),
                "retryable": retryable,
                "recoverable": bool(classified.get("recoverable")),
                "required_endpoint": required_endpoint,
                "missing_capability": missing_capability,
                "diagnostic_sample_json": dumps_json(detail),
            },
            result="failed",
            severity="warning" if status_code < 500 else "danger",
        )
        return trace_for_log

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
        request_path_for_log: str | None = None,
    ) -> None:
        effective_request_path = request_path_for_log or f"/v1{endpoint_path}"
        content_guard_exhausted = (
            upstream_error is not None
            and ProxyService._error_code_from_detail(upstream_error.get("detail")) == "content_integrity_violation"
        )
        if content_guard_exhausted:
            detail = upstream_error.get("detail")
            if isinstance(detail, dict) and isinstance(detail.get("error"), dict):
                detail["error"]["message"] = "所有可用渠道的上游响应均未通过内容完整性防护，已阻断返回。"
                detail["error"]["retryable"] = False
                detail["error"]["recoverable"] = False
            elif isinstance(detail, dict):
                detail["message"] = "所有可用渠道的上游响应均未通过内容完整性防护，已阻断返回。"
                detail["retryable"] = False
                detail["recoverable"] = False
            upstream_error["detail"] = detail
        if upstream_error is not None and (content_guard_exhausted or ProxyService._attempt_count(trace) <= 1):
            status_code = upstream_error["status_code"]
            detail = upstream_error["detail"]
            message = ProxyService._error_message_for_log(detail)
            trace_for_log = ProxyService._trace_with_error_response_event(
                trace,
                status_code=status_code,
                detail=detail,
                retryable=ProxyService._is_retryable_status(status_code, detail),
            )
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
                request_path=effective_request_path,
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
                error_type=ProxyService._error_type_from_status(status_code, detail),
                error_code=ProxyService._error_code_from_detail(detail),
                retryable=ProxyService._is_retryable_status(status_code, detail),
                content_guard_retry_provider_count=ProxyService._content_guard_retry_provider_count(trace) if content_guard_exhausted else None,
                content_guard_final_strategy="switch_provider_exhausted" if content_guard_exhausted else None,
                **ProxyService._build_api_client_log_kwargs(api_client_auth, auth_result="authenticated"),
                trace=trace_for_log,
                attempt_count=attempt_count,
                token_request_payload=request_payload,
                schedule_token_fill=schedule_token_fill,
            )
            raise HTTPException(status_code=status_code, detail=detail)

        if upstream_error is not None and upstream_error.get("status_code") in {status.HTTP_429_TOO_MANY_REQUESTS, status.HTTP_503_SERVICE_UNAVAILABLE}:
            status_code = upstream_error["status_code"]
            detail = upstream_error["detail"]
            trace_for_log = ProxyService._trace_with_error_response_event(
                trace,
                status_code=status_code,
                detail=detail,
                retryable=ProxyService._is_retryable_status(status_code, detail),
            )
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
                request_path=effective_request_path,
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
                error_type=ProxyService._error_type_from_status(status_code, detail),
                error_code=ProxyService._error_code_from_detail(detail),
                retryable=ProxyService._is_retryable_status(status_code, detail),
                **ProxyService._build_api_client_log_kwargs(api_client_auth, auth_result="authenticated"),
                trace=trace,
                attempt_count=attempt_count,
                token_request_payload=request_payload,
                schedule_token_fill=schedule_token_fill,
            )
            raise HTTPException(status_code=status_code, detail=detail)

        detail = {"message": "All providers failed", "trace": trace}
        if upstream_error is not None:
            detail["last_error"] = upstream_error.get("detail")
            detail["last_status_code"] = upstream_error.get("status_code")
        detail["code"] = "all_providers_failed"
        detail["attempt_count"] = attempt_count
        trace_for_log = ProxyService._trace_with_error_response_event(
            trace,
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=detail,
            retryable=True,
        )
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
            request_path=effective_request_path,
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
    def _exception_message(exc: BaseException) -> str:
        message = str(exc).strip()
        return message or exc.__class__.__name__

    @staticmethod
    def _build_upstream_exception_error(exc: BaseException) -> tuple[int, dict[str, Any]]:
        message = ProxyService._exception_message(exc)
        detail: dict[str, Any] = {
            "message": message,
            "exception_type": exc.__class__.__name__,
        }
        if isinstance(exc, httpx.ConnectTimeout):
            detail["code"] = "upstream_connect_timeout"
            return status.HTTP_504_GATEWAY_TIMEOUT, detail
        if isinstance(exc, httpx.ReadTimeout):
            detail["code"] = "upstream_read_timeout"
            return status.HTTP_504_GATEWAY_TIMEOUT, detail
        if isinstance(exc, httpx.WriteTimeout):
            detail["code"] = "upstream_write_timeout"
            return status.HTTP_504_GATEWAY_TIMEOUT, detail
        if isinstance(exc, httpx.PoolTimeout):
            detail["code"] = "upstream_pool_timeout"
            return status.HTTP_504_GATEWAY_TIMEOUT, detail
        if isinstance(exc, httpx.TimeoutException):
            detail["code"] = "upstream_timeout"
            return status.HTTP_504_GATEWAY_TIMEOUT, detail
        if isinstance(exc, httpx.ConnectError):
            detail["code"] = "upstream_connect_error"
            return status.HTTP_502_BAD_GATEWAY, detail
        if isinstance(exc, httpx.NetworkError):
            detail["code"] = "upstream_network_error"
            return status.HTTP_502_BAD_GATEWAY, detail
        detail["code"] = "upstream_request_failed"
        return status.HTTP_502_BAD_GATEWAY, detail

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
        retryable = result not in {
            "success",
            "finished",
            "stream_opened",
            "model_not_found",
            "request_rejected",
            "upstream_auth_error",
            "client_cancelled",
            "client_disconnected",
        }
        item["typed_event"] = "request_provider_attempt"
        item["event_result"] = "success" if result in {"success", "finished", "stream_opened"} else "failed"
        item["payload"] = {
            "provider_id": provider.id,
            "provider_name": provider.name,
            "provider_model_id": provider_model.id,
            "actual_model": provider_model.model_name,
            "protocol_type": getattr(provider_model, "protocol_type", None),
            "capacity_lease_acquired": result not in {"capacity_limited", "capacity_unavailable"},
            "result": result,
            "status_code": status_code,
            "latency_ms": latency_ms,
            "error_code": ProxyService._error_code_from_detail(error) if error is not None else None,
            "retryable": retryable,
        }
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
        client_metadata = payload.get("client_metadata")
        for container in (metadata, client_metadata):
            if not isinstance(container, dict):
                continue
            for key in ("session_id", "conversation_id", "thread_id"):
                value = container.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        prompt_cache_key = payload.get("prompt_cache_key")
        if isinstance(prompt_cache_key, str) and prompt_cache_key.strip():
            return prompt_cache_key.strip()
        return None

    @staticmethod
    def _extract_session_sticky_key(payload: dict[str, Any]) -> str | None:
        if not isinstance(payload, dict):
            return None
        metadata = payload.get("metadata")
        client_metadata = payload.get("client_metadata")
        containers = [
            item
            for item in (metadata, client_metadata, payload)
            if isinstance(item, dict)
        ]
        for container in containers:
            for key in ("session_id", "conversation_id", "thread_id", "session", "conversation_key"):
                value = container.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        prompt_cache_key = payload.get("prompt_cache_key")
        if isinstance(prompt_cache_key, str) and prompt_cache_key.strip():
            return prompt_cache_key.strip()
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
            if isinstance(item_type, str) and item_type in {"image_url", "input_image"}:
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
    def _extract_upstream_request_id(response: UpstreamStreamResponse | httpx.Response) -> str | None:
        for key in ("x-request-id", "request-id", "openai-request-id"):
            value = response.headers.get(key)
            if value:
                return value
        return None

    @staticmethod
    def _select_stream_log_fast_path(prepared: PreparedUpstreamRequest) -> str | None:
        if prepared.adapt_chat_response_to_responses or prepared.adapt_chat_response_to_completions or prepared.adapt_responses_response_to_chat:
            return None
        if prepared.request_path == "/chat/completions":
            return "chat"
        return None

    @staticmethod
    def _extract_usage_info(response_json: dict[str, Any]) -> dict[str, int | None]:
        usage = LogService.extract_usage_payload(response_json)
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
        normalized_prompt_tokens = LogService.normalize_prompt_tokens_for_cache_usage(
            usage,
            ProxyService._coerce_non_negative_int(prompt_tokens),
        )
        return {
            "prompt_tokens": normalized_prompt_tokens,
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
    def _normalize_image_mime_type(mime_type: str | None) -> str:
        normalized = str(mime_type or "").strip().lower().lstrip(".")
        if normalized in {"jpg", "jpeg"}:
            return "image/jpeg"
        if normalized in {"png", "gif", "webp"}:
            return f"image/{normalized}"
        if "/" in normalized:
            return normalized
        return "image/png"

    @staticmethod
    def _build_data_url_from_base64(base64_value: str, *, mime_type: str | None = None) -> str:
        return f"data:{ProxyService._normalize_image_mime_type(mime_type)};base64,{base64_value}"

    @staticmethod
    def _extract_base64_from_data_url(value: str | None) -> tuple[str | None, str | None]:
        if not isinstance(value, str):
            return None, None
        current = value.strip()
        if not current.startswith("data:"):
            return None, None
        header, separator, payload = current.partition(",")
        if separator != "," or ";base64" not in header.lower():
            return None, None
        mime_type = header[5:].split(";", 1)[0].strip() or None
        return payload.strip() or None, ProxyService._normalize_image_mime_type(mime_type)

    @staticmethod
    def _extract_revised_prompt(value: Any) -> str | None:
        if isinstance(value, dict):
            revised_prompt = value.get("revised_prompt")
            if isinstance(revised_prompt, str) and revised_prompt.strip():
                return revised_prompt.strip()
            for item in value.values():
                nested = ProxyService._extract_revised_prompt(item)
                if nested:
                    return nested
            return None
        if isinstance(value, list):
            for item in value:
                nested = ProxyService._extract_revised_prompt(item)
                if nested:
                    return nested
        return None

    @staticmethod
    def adapt_responses_to_legacy_image_response(
        response_json: dict[str, Any],
        *,
        response_format: str,
        created: int | None = None,
    ) -> dict[str, Any]:
        normalized_response_format = ProxyService._normalize_legacy_image_response_format(response_format)
        generated_images = ProxyService._extract_generated_images(response_json, limit_images=16)
        if not generated_images:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    "message": "上游图片响应未返回可解析的生成结果",
                    "code": "image_result_missing",
                },
            )
        revised_prompt = ProxyService._extract_revised_prompt(response_json)
        data: list[dict[str, Any]] = []
        for item in generated_images:
            image_url = item.get("url")
            if not isinstance(image_url, str) or not image_url.strip():
                continue
            entry: dict[str, Any]
            if normalized_response_format == "url":
                entry = {"url": image_url.strip()}
            else:
                base64_value, _mime_type = ProxyService._extract_base64_from_data_url(image_url)
                if not base64_value:
                    raise HTTPException(
                        status_code=status.HTTP_502_BAD_GATEWAY,
                        detail={
                            "message": "legacy Images 兼容层无法把上游图片结果转换成 b64_json",
                            "code": "image_result_b64_unavailable",
                        },
                    )
                entry = {"b64_json": base64_value}
            if revised_prompt:
                entry["revised_prompt"] = revised_prompt
            data.append(entry)
        if not data:
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail={
                    "message": "上游图片响应未返回可解析的生成结果",
                    "code": "image_result_missing",
                },
            )
        return {
            "created": int(created or time.time()),
            "data": data,
        }

    @staticmethod
    def _normalize_generated_image_candidate(value: Any, *, mime_type: str | None = None) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        if isinstance(value, list):
            for item in value:
                results.extend(ProxyService._normalize_generated_image_candidate(item, mime_type=mime_type))
            return results
        if isinstance(value, str):
            current = value.strip()
            if not current:
                return results
            if current.startswith(("http://", "https://", "data:")):
                results.append({"url": current, "mime_type": mime_type})
            else:
                results.append(
                    {
                        "url": ProxyService._build_data_url_from_base64(current, mime_type=mime_type),
                        "mime_type": ProxyService._normalize_image_mime_type(mime_type),
                    }
                )
            return results
        if not isinstance(value, dict):
            return results

        candidate_mime = value.get("mime_type") or value.get("output_format") or mime_type
        image_url_value = value.get("image_url") or value.get("url")
        if isinstance(image_url_value, dict):
            image_url_value = image_url_value.get("url")
        if isinstance(image_url_value, str) and image_url_value.strip():
            results.append({"url": image_url_value.strip(), "mime_type": candidate_mime})
            return results

        for key in ("b64_json", "image_base64", "base64", "partial_image_b64", "result"):
            candidate = value.get(key)
            if isinstance(candidate, str) and candidate.strip():
                results.append(
                    {
                        "url": ProxyService._build_data_url_from_base64(candidate.strip(), mime_type=candidate_mime),
                        "mime_type": ProxyService._normalize_image_mime_type(candidate_mime),
                    }
                )
                return results
        return results

    @staticmethod
    def _extract_generated_images(response_json: dict[str, Any], *, limit_images: int = 8) -> list[dict[str, Any]]:
        images: list[dict[str, Any]] = []
        seen: set[str] = set()

        def append_candidate(candidate: Any, *, mime_type: str | None = None) -> None:
            if len(images) >= limit_images:
                return
            for item in ProxyService._normalize_generated_image_candidate(candidate, mime_type=mime_type):
                url = item.get("url")
                if not isinstance(url, str) or not url:
                    continue
                if url in seen:
                    continue
                seen.add(url)
                images.append(item)
                if len(images) >= limit_images:
                    break

        def walk(value: Any) -> None:
            if len(images) >= limit_images:
                return
            if isinstance(value, list):
                for item in value:
                    walk(item)
                    if len(images) >= limit_images:
                        break
                return
            if not isinstance(value, dict):
                return

            item_type = value.get("type")
            if isinstance(item_type, str) and item_type == "image_generation_call":
                append_candidate(value.get("result"), mime_type=value.get("mime_type") or value.get("output_format"))
            if isinstance(item_type, str) and item_type == "response.image_generation_call.partial_image":
                append_candidate(value.get("partial_image_b64"), mime_type=value.get("mime_type") or value.get("output_format"))
            if isinstance(item_type, str) and item_type == "image_generation.partial_image":
                append_candidate(value.get("b64_json"), mime_type=value.get("mime_type") or value.get("output_format"))

            if any(key in value for key in ("b64_json", "image_base64", "base64", "partial_image_b64")):
                append_candidate(value, mime_type=value.get("mime_type") or value.get("output_format"))

            for nested in value.values():
                walk(nested)

        walk(response_json)
        return images

    @staticmethod
    def _extract_response_display_text(response_json: dict[str, Any], *, limit_bytes: int) -> str | None:
        text = ProxyService._extract_response_text(response_json, limit_bytes=limit_bytes)
        if text:
            return text
        generated_images = ProxyService._extract_generated_images(response_json)
        if not generated_images:
            return None
        return f"[生成了 {len(generated_images)} 张图片]"

    @staticmethod
    def _serialize_payload_for_logging(
        payload: dict[str, Any],
        *,
        setting: Any,
        preserve_request_content_when_disabled: bool = False,
        structure_only: bool = False,
    ) -> str | None:
        if structure_only:
            payload_to_log = summarize_request_body_structure(payload)
            return ProxyService._truncate_serialized_json(
                payload_to_log,
                getattr(setting, "max_logged_body_bytes", 16384),
            )
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
    def _estimate_base64_binary_bytes(value: str | None) -> int | None:
        if not isinstance(value, str):
            return None
        payload = value.strip()
        if not payload:
            return 0
        if payload.startswith("data:") and "," in payload:
            payload = payload.split(",", 1)[1].strip()
        payload = "".join(payload.split())
        if not payload:
            return 0
        padding = len(payload) - len(payload.rstrip("="))
        return max(0, (len(payload) * 3) // 4 - padding)

    @staticmethod
    def _collect_generated_image_payload_stats(value: Any, *, include_raw_string: bool = False) -> dict[str, Any]:
        approx_bytes = 0
        has_approx_bytes = False
        has_partial = False

        def add_base64(candidate: str | None) -> None:
            nonlocal approx_bytes, has_approx_bytes
            estimated = ProxyService._estimate_base64_binary_bytes(candidate)
            if estimated is None:
                return
            approx_bytes += estimated
            has_approx_bytes = True

        def walk(node: Any) -> None:
            nonlocal has_partial
            if isinstance(node, dict):
                for key, item in node.items():
                    lowered = str(key).lower()
                    if lowered == "partial_image_b64":
                        has_partial = True
                    if lowered in {"image", "image_base64", "b64_json", "partial_image_b64", "base64"} and isinstance(item, str):
                        add_base64(item)
                        continue
                    if lowered == "result" and isinstance(item, str):
                        add_base64(item)
                        continue
                    walk(item)
                return
            if isinstance(node, list):
                for item in node:
                    walk(item)
                return
            if include_raw_string and isinstance(node, str):
                add_base64(node)

        walk(value)
        return {
            "approx_bytes": approx_bytes if has_approx_bytes else None,
            "has_partial": has_partial,
        }

    @staticmethod
    def _build_generated_image_log_summary(
        candidate: Any,
        *,
        mime_type: str | None = None,
        summary_kind: str,
        has_partial: bool = False,
    ) -> dict[str, Any]:
        wrapper: dict[str, Any]
        if summary_kind == "generated_image_result":
            wrapper = {"type": "image_generation_call", "result": candidate, "mime_type": mime_type}
        else:
            wrapper = {"b64_json": candidate, "mime_type": mime_type}
            if has_partial:
                wrapper["type"] = "response.image_generation_call.partial_image"
        images = ProxyService._extract_generated_images(wrapper, limit_images=16)
        mime_types: list[str] = []
        seen_mime_types: set[str] = set()
        for item in images:
            current_mime = item.get("mime_type")
            if isinstance(current_mime, str) and current_mime not in seen_mime_types:
                seen_mime_types.add(current_mime)
                mime_types.append(current_mime)
        stats = ProxyService._collect_generated_image_payload_stats(candidate, include_raw_string=True)
        image_count = len(images)
        if image_count <= 0 and isinstance(candidate, str) and candidate.strip():
            image_count = 1
        return {
            "_summary": "generated image payload omitted from logs",
            "summary_kind": summary_kind,
            "image_count": image_count,
            "mime_types": mime_types,
            "approx_bytes": stats.get("approx_bytes"),
            "has_partial": bool(has_partial or stats.get("has_partial")),
            "result_truncated": True,
        }

    @staticmethod
    def _merge_generated_image_log_summaries(summaries: list[dict[str, Any]]) -> dict[str, Any] | None:
        if not summaries:
            return None
        image_count = 0
        approx_bytes = 0
        has_approx_bytes = False
        has_partial = False
        result_truncated = False
        mime_types: list[str] = []
        seen_mime_types: set[str] = set()
        summary_kind = "binary_image"
        for item in summaries:
            if item.get("summary_kind") == "generated_image_result":
                summary_kind = "generated_image_result"
            count_value = item.get("image_count")
            if isinstance(count_value, int):
                image_count += max(0, count_value)
            approx_value = item.get("approx_bytes")
            if isinstance(approx_value, int):
                approx_bytes += max(0, approx_value)
                has_approx_bytes = True
            if item.get("has_partial") is True:
                has_partial = True
            if item.get("result_truncated") is True:
                result_truncated = True
            for mime_type in item.get("mime_types") or []:
                if isinstance(mime_type, str) and mime_type not in seen_mime_types:
                    seen_mime_types.add(mime_type)
                    mime_types.append(mime_type)
        return {
            "_summary": "generated image payload omitted from logs",
            "summary_kind": summary_kind,
            "image_count": image_count,
            "mime_types": mime_types,
            "approx_bytes": approx_bytes if has_approx_bytes else None,
            "has_partial": has_partial,
            "result_truncated": result_truncated,
        }

    @staticmethod
    def _extract_stream_generated_image_log_summary(event_json: dict[str, Any]) -> dict[str, Any] | None:
        result_candidates: list[dict[str, Any]] = []
        partial_candidates: list[dict[str, Any]] = []

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                item_type = value.get("type")
                if isinstance(item_type, str) and item_type == "image_generation_call" and value.get("result") is not None:
                    result_candidates.append(
                        {
                            "candidate": value.get("result"),
                            "mime_type": value.get("mime_type") or value.get("output_format"),
                        }
                    )
                elif isinstance(item_type, str) and item_type in {"response.image_generation_call.partial_image", "image_generation.partial_image"}:
                    partial_candidates.append(
                        {
                            "candidate": value.get("partial_image_b64") or value.get("b64_json"),
                            "mime_type": value.get("mime_type") or value.get("output_format"),
                        }
                    )
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(event_json)
        summaries: list[dict[str, Any]] = []
        for item in result_candidates:
            summaries.append(
                ProxyService._build_generated_image_log_summary(
                    item.get("candidate"),
                    mime_type=item.get("mime_type"),
                    summary_kind="generated_image_result",
                )
            )
        if not summaries:
            for item in partial_candidates:
                summaries.append(
                    ProxyService._build_generated_image_log_summary(
                        item.get("candidate"),
                        mime_type=item.get("mime_type"),
                        summary_kind="binary_image",
                        has_partial=True,
                    )
                )
        merged = ProxyService._merge_generated_image_log_summaries(summaries)
        if merged and partial_candidates:
            merged["has_partial"] = True
        return merged

    @staticmethod
    def _merge_stream_generated_image_summary(target: dict[str, Any], incoming: dict[str, Any]) -> None:
        if not incoming:
            return
        if not target:
            target.update(incoming)
            return
        target_count = int(target.get("image_count") or 0)
        incoming_count = int(incoming.get("image_count") or 0)
        target_bytes = int(target.get("approx_bytes") or 0)
        incoming_bytes = int(incoming.get("approx_bytes") or 0)
        if incoming_count > target_count or (incoming_count == target_count and incoming_bytes > target_bytes):
            preserved_partial = bool(target.get("has_partial") or incoming.get("has_partial"))
            preserved_result_truncated = bool(target.get("result_truncated") or incoming.get("result_truncated"))
            target.clear()
            target.update(incoming)
            target["has_partial"] = preserved_partial
            target["result_truncated"] = preserved_result_truncated
            return
        target["has_partial"] = bool(target.get("has_partial") or incoming.get("has_partial"))
        target["result_truncated"] = bool(target.get("result_truncated") or incoming.get("result_truncated"))
        existing_mime_types = [item for item in target.get("mime_types") or [] if isinstance(item, str)]
        for mime_type in incoming.get("mime_types") or []:
            if isinstance(mime_type, str) and mime_type not in existing_mime_types:
                existing_mime_types.append(mime_type)
        if existing_mime_types:
            target["mime_types"] = existing_mime_types

    @staticmethod
    def _serialize_stream_response_summary_for_logging(summary: dict[str, Any] | None, *, setting: Any) -> str | None:
        if not isinstance(summary, dict) or not summary:
            return None
        payload = {"_summary": "stream response summary only", "generated_image": summary}
        return ProxyService._truncate_serialized_json(payload, getattr(setting, "max_logged_body_bytes", 16384))

    @staticmethod
    def _sanitize_for_logging(value: Any, *, mask_sensitive: bool) -> Any:
        if isinstance(value, dict):
            sanitized: dict[str, Any] = {}
            container_type = value.get("type") if isinstance(value.get("type"), str) else None
            for key, item in value.items():
                lowered = key.lower()
                if mask_sensitive and any(token in lowered for token in ("api_key", "authorization", "secret", "password", "token")):
                    sanitized[key] = "***"
                    continue
                if lowered in {"image", "image_base64", "b64_json", "partial_image_b64"} and isinstance(item, str):
                    sanitized[key] = ProxyService._build_generated_image_log_summary(
                        item,
                        mime_type=value.get("mime_type") or value.get("output_format"),
                        summary_kind="binary_image",
                        has_partial=lowered == "partial_image_b64",
                    )
                    continue
                if lowered in {"url", "image_url"} and isinstance(item, str) and item.strip().startswith("data:image/"):
                    sanitized[key] = ProxyService._build_generated_image_log_summary(
                        item,
                        mime_type=value.get("mime_type") or value.get("output_format"),
                        summary_kind="binary_image",
                    )
                    continue
                if lowered == "result" and container_type == "image_generation_call":
                    sanitized[key] = ProxyService._build_generated_image_log_summary(
                        item,
                        mime_type=value.get("mime_type") or value.get("output_format"),
                        summary_kind="generated_image_result",
                    )
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
        preview_budget = max(0, int(limit_bytes or 0) - 256)
        clipped = encoded[:preview_budget].decode("utf-8", errors="ignore")
        truncated_payload: dict[str, Any] = {
            "_summary": "payload truncated to keep log JSON parseable",
            "_truncated": True,
            "_original_bytes": len(encoded),
            "preview": clipped,
        }
        if isinstance(value, dict):
            keys = [str(item) for item in value.keys()]
            truncated_payload["key_count"] = len(keys)
            truncated_payload["keys"] = keys[:40]
            truncated_payload["truncated_keys"] = max(0, len(keys) - 40)
        return dumps_json(truncated_payload)

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
        usage_payload: dict[str, Any] | None,
        generated_image_summary: dict[str, Any] | None,
        capture_text: bool,
        capture_usage: bool,
        limit_bytes: int,
        token_limit_bytes: int,
        stream_log_fast_path: str | None,
        ) -> tuple[int, int, str | None, dict[str, int | None], dict[str, Any] | None]:
        if stream_log_fast_path == "chat":
            return ProxyService._collect_chat_stream_log_data(
                chunk=chunk,
                event_buffer=event_buffer,
                response_text_parts=response_text_parts,
                response_text_bytes=response_text_bytes,
                token_response_parts=token_response_parts,
                token_response_bytes=token_response_bytes,
                finish_reason=finish_reason,
                usage_info=usage_info,
                usage_payload=usage_payload,
                capture_text=capture_text,
                capture_usage=capture_usage,
                limit_bytes=limit_bytes,
                token_limit_bytes=token_limit_bytes,
            )
        event_buffer.extend(chunk)
        for data in ProxyService._consume_sse_data_payloads(event_buffer):
            if not data or data == "[DONE]":
                continue
            event_json = safeJsonParse(data)
            if not isinstance(event_json, dict):
                continue
            if capture_usage:
                event_usage = LogService.extract_usage_payload(event_json)
                if isinstance(event_usage, dict):
                    usage_payload = event_usage
                extracted_usage = ProxyService._extract_usage_info(event_json)
                for key, value in extracted_usage.items():
                    if value is not None:
                        usage_info[key] = value
            if finish_reason is None:
                finish_reason = ProxyService._extract_finish_reason(event_json)
            if generated_image_summary is not None:
                event_generated_image_summary = ProxyService._extract_stream_generated_image_log_summary(event_json)
                if event_generated_image_summary is not None:
                    ProxyService._merge_stream_generated_image_summary(generated_image_summary, event_generated_image_summary)
                    if not capture_text:
                        summary_text = f"[生成了 {int(generated_image_summary.get('image_count') or 0)} 张图片]"
                        if generated_image_summary.get("image_count") and (
                            not response_text_parts or response_text_parts[-1] != summary_text
                        ):
                            response_text_bytes = ProxyService._append_limited_text(
                                response_text_parts,
                                summary_text,
                                current_bytes=response_text_bytes,
                                limit_bytes=limit_bytes,
                            )
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
        return response_text_bytes, token_response_bytes, finish_reason, usage_info, usage_payload

    @staticmethod
    def _collect_chat_stream_log_data(
        *,
        chunk: bytes,
        event_buffer: bytearray,
        response_text_parts: list[str],
        response_text_bytes: int,
        token_response_parts: list[str] | None,
        token_response_bytes: int,
        finish_reason: str | None,
        usage_info: dict[str, int | None],
        usage_payload: dict[str, Any] | None,
        capture_text: bool,
        capture_usage: bool,
        limit_bytes: int,
        token_limit_bytes: int,
    ) -> tuple[int, int, str | None, dict[str, int | None], dict[str, Any] | None]:
        event_buffer.extend(chunk)
        for data in ProxyService._consume_sse_data_payloads(event_buffer):
            if not data or data == "[DONE]":
                continue
            event_json = safeJsonParse(data)
            if not isinstance(event_json, dict):
                continue
            if capture_usage and isinstance(event_json.get("usage"), dict):
                usage_payload = event_json["usage"]
                extracted_usage = ProxyService._extract_usage_info(event_json)
                for key, value in extracted_usage.items():
                    if value is not None:
                        usage_info[key] = value
            if finish_reason is None:
                choices = event_json.get("choices")
                if isinstance(choices, list) and choices:
                    first_choice = choices[0]
                    if isinstance(first_choice, dict):
                        choice_finish_reason = first_choice.get("finish_reason")
                        if isinstance(choice_finish_reason, str) and choice_finish_reason:
                            finish_reason = choice_finish_reason
            if capture_text or token_response_parts is not None:
                delta_text = ProxyService._extract_chat_stream_delta_text(event_json)
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
        return response_text_bytes, token_response_bytes, finish_reason, usage_info, usage_payload

    @staticmethod
    def _consume_sse_data_payloads(event_buffer: bytearray) -> list[str]:
        payloads: list[str] = []
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
            if not raw_event:
                continue
            stripped = raw_event.strip()
            if not stripped:
                continue
            if stripped.startswith(b"data:") and b"\n" not in stripped and b"\r" not in stripped:
                payload = stripped[5:].strip()
                if payload:
                    payloads.append(payload.decode("utf-8", errors="ignore"))
                continue
            event_payload_lines: list[str] = []
            for raw_line in raw_event.splitlines():
                line = raw_line.strip()
                if not line.startswith(b"data:"):
                    continue
                payload = line[5:].strip()
                if payload:
                    event_payload_lines.append(payload.decode("utf-8", errors="ignore"))
            if event_payload_lines:
                payloads.append("\n".join(event_payload_lines))
        return payloads

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
    def _finalize_stream_text_capture(parts: list[str], *, generated_image_summary: dict[str, Any] | None) -> str | None:
        text = ProxyService._finalize_text_capture(parts)
        if text:
            return text
        image_count = int((generated_image_summary or {}).get("image_count") or 0)
        if image_count > 0:
            return f"[生成了 {image_count} 张图片]"
        return None

    @staticmethod
    def _build_api_client_log_kwargs(
        api_client_auth: ApiClientAuthContext | None,
        *,
        auth_result: str | None,
    ) -> dict[str, Any]:
        if api_client_auth is None:
            return {}
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
            "channel_price_cache_write_per_1k": (
                provider_model.cache_write_price_per_1k
                if provider_model.cache_write_price_per_1k is not None
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
        cache_key = ProxyService._v1_models_cache_key(route_context=route_context, api_client_auth=api_client_auth)
        cached = CacheService.get(cache_key)
        if cached is not None:
            return cached
        model_set = {
            candidate.provider_model.model_name
            for candidate in RouterService.get_available_candidates(db, route_context=route_context)
            if api_client_auth is None or ApiKeyService.is_model_allowed(api_client_auth.api_client_key, candidate.provider_model.model_name)
        }
        for mapping in ModelMappingService.list_mappings(db):
            if not mapping.get("enabled"):
                continue
            source_model_name = mapping.get("source_model_name")
            if not source_model_name:
                continue
            if api_client_auth is not None and not ApiKeyService.is_model_allowed(api_client_auth.api_client_key, source_model_name):
                continue
            target_names = {
                item.get("model_name")
                for item in (mapping.get("targets") or [])
                if item.get("enabled", True)
            }
            if target_names & model_set:
                model_set.add(source_model_name)
        payload = {
            "object": "list",
            "data": [{"id": model_name, "object": "model", "owned_by": "aotu-gpt", "permission": []} for model_name in sorted(model_set)],
        }
        return CacheService.set(cache_key, payload, ttl_seconds=max(0, int(setting.model_list_cache_ttl_sec)))

    @staticmethod
    def _v1_models_cache_key(
        *,
        route_context: RoutePolicyContext | None,
        api_client_auth: ApiClientAuthContext | None,
    ) -> str:
        api_client_key = api_client_auth.api_client_key if api_client_auth is not None else None
        payload = {
            "version": 2,
            "route_context": (
                {
                    "allowed_provider_ids": sorted(route_context.allowed_provider_ids or []),
                    "forced_provider_id": route_context.forced_provider_id,
                    "preferred_provider_ids": list(route_context.preferred_provider_ids or []),
                    "preferred_region_tags": list(route_context.preferred_region_tags or []),
                    "latency_bias": route_context.latency_bias,
                    "success_rate_bias": route_context.success_rate_bias,
                    "cost_bias": route_context.cost_bias,
                    "require_trusted_provider": route_context.require_trusted_provider,
                    "content_guard_required": route_context.content_guard_required,
                }
                if route_context is not None
                else None
            ),
            "api_key": (
                {
                    "id": getattr(api_client_key, "id", None),
                    "allowed_model_names": ProxyService._normalized_cache_string_list(
                        loads_json(getattr(api_client_key, "allowed_model_names_json", None), [])
                    ),
                }
                if api_client_key is not None
                else None
            ),
        }
        digest = hashlib.sha256(dumps_json(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
        return f"v1-models|{digest}"

    @staticmethod
    def _normalized_cache_string_list(value: Any) -> list[str]:
        if not isinstance(value, (list, tuple, set)):
            return []
        return sorted(str(item).strip() for item in value if str(item).strip())

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
    async def list_response_input_items(
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
            action="input_items",
            query_items=query_items,
            route_context=route_context,
            api_client_auth=api_client_auth,
            trace_id=trace_id,
            source_ip=source_ip,
        )

    @staticmethod
    async def delete_response(
        *,
        response_id: str,
        route_context: RoutePolicyContext | None = None,
        api_client_auth: ApiClientAuthContext | None = None,
        trace_id: str | None = None,
        source_ip: str | None = None,
    ) -> tuple[dict[str, Any], Provider, list[dict], int]:
        return await ProxyService._forward_response_management_request(
            method="DELETE",
            response_id=response_id,
            action=None,
            query_items=None,
            route_context=route_context,
            api_client_auth=api_client_auth,
            trace_id=trace_id,
            source_ip=source_ip,
        )

    @staticmethod
    async def list_chat_completions(
        *,
        query_items: list[tuple[str, str]] | None = None,
        route_context: RoutePolicyContext | None = None,
        api_client_auth: ApiClientAuthContext | None = None,
        trace_id: str | None = None,
        source_ip: str | None = None,
    ) -> tuple[dict[str, Any], Provider, list[dict], int]:
        return await ProxyService._forward_response_management_request(
            method="GET",
            response_id="chat-completions",
            action=None,
            query_items=query_items,
            route_context=route_context,
            api_client_auth=api_client_auth,
            trace_id=trace_id,
            source_ip=source_ip,
            request_path_override="/chat/completions",
            log_type="chat",
            unavailable_message="No available provider for chat completions management request",
            unavailable_code="chat_completion_provider_not_available",
            not_found_message="Chat completions were not found in any authorized provider",
            not_found_code="chat_completion_not_found",
        )

    @staticmethod
    async def retrieve_chat_completion(
        *,
        completion_id: str,
        route_context: RoutePolicyContext | None = None,
        api_client_auth: ApiClientAuthContext | None = None,
        trace_id: str | None = None,
        source_ip: str | None = None,
    ) -> tuple[dict[str, Any], Provider, list[dict], int]:
        return await ProxyService._forward_response_management_request(
            method="GET",
            response_id=completion_id,
            action=None,
            query_items=None,
            route_context=route_context,
            api_client_auth=api_client_auth,
            trace_id=trace_id,
            source_ip=source_ip,
            request_path_override=f"/chat/completions/{completion_id}",
            log_type="chat",
            unavailable_message="No available provider for chat completion management request",
            unavailable_code="chat_completion_provider_not_available",
            not_found_message="Chat completion was not found in any authorized provider",
            not_found_code="chat_completion_not_found",
        )

    @staticmethod
    async def list_chat_completion_messages(
        *,
        completion_id: str,
        query_items: list[tuple[str, str]] | None = None,
        route_context: RoutePolicyContext | None = None,
        api_client_auth: ApiClientAuthContext | None = None,
        trace_id: str | None = None,
        source_ip: str | None = None,
    ) -> tuple[dict[str, Any], Provider, list[dict], int]:
        return await ProxyService._forward_response_management_request(
            method="GET",
            response_id=completion_id,
            action=None,
            query_items=query_items,
            route_context=route_context,
            api_client_auth=api_client_auth,
            trace_id=trace_id,
            source_ip=source_ip,
            request_path_override=f"/chat/completions/{completion_id}/messages",
            log_type="chat",
            unavailable_message="No available provider for chat completion messages request",
            unavailable_code="chat_completion_provider_not_available",
            not_found_message="Chat completion messages were not found in any authorized provider",
            not_found_code="chat_completion_not_found",
        )

    @staticmethod
    async def update_chat_completion(
        *,
        completion_id: str,
        payload: dict[str, Any],
        route_context: RoutePolicyContext | None = None,
        api_client_auth: ApiClientAuthContext | None = None,
        trace_id: str | None = None,
        source_ip: str | None = None,
    ) -> tuple[dict[str, Any], Provider, list[dict], int]:
        return await ProxyService._forward_response_management_request(
            method="POST",
            response_id=completion_id,
            action=None,
            query_items=None,
            route_context=route_context,
            api_client_auth=api_client_auth,
            trace_id=trace_id,
            source_ip=source_ip,
            request_path_override=f"/chat/completions/{completion_id}",
            payload=payload,
            log_type="chat",
            unavailable_message="No available provider for chat completion update request",
            unavailable_code="chat_completion_provider_not_available",
            not_found_message="Chat completion was not found in any authorized provider",
            not_found_code="chat_completion_not_found",
        )

    @staticmethod
    async def delete_chat_completion(
        *,
        completion_id: str,
        route_context: RoutePolicyContext | None = None,
        api_client_auth: ApiClientAuthContext | None = None,
        trace_id: str | None = None,
        source_ip: str | None = None,
    ) -> tuple[dict[str, Any], Provider, list[dict], int]:
        return await ProxyService._forward_response_management_request(
            method="DELETE",
            response_id=completion_id,
            action=None,
            query_items=None,
            route_context=route_context,
            api_client_auth=api_client_auth,
            trace_id=trace_id,
            source_ip=source_ip,
            request_path_override=f"/chat/completions/{completion_id}",
            log_type="chat",
            unavailable_message="No available provider for chat completion delete request",
            unavailable_code="chat_completion_provider_not_available",
            not_found_message="Chat completion was not found in any authorized provider",
            not_found_code="chat_completion_not_found",
        )

    @staticmethod
    async def create_moderation(
        *,
        payload: dict[str, Any],
        route_context: RoutePolicyContext | None = None,
        api_client_auth: ApiClientAuthContext | None = None,
        trace_id: str | None = None,
        source_ip: str | None = None,
    ) -> tuple[dict[str, Any], Provider, list[dict], int]:
        return await ProxyService._forward_response_management_request(
            method="POST",
            response_id="moderations",
            action=None,
            query_items=None,
            route_context=route_context,
            api_client_auth=api_client_auth,
            trace_id=trace_id,
            source_ip=source_ip,
            request_path_override="/moderations",
            payload=payload,
            log_type="moderations",
            unavailable_message="No available provider for moderation request",
            unavailable_code="moderation_provider_not_available",
            not_found_message="Moderation endpoint was not found in any authorized provider",
            not_found_code="moderation_not_found",
        )

    @staticmethod
    async def list_files(
        *,
        query_items: list[tuple[str, str]] | None = None,
        route_context: RoutePolicyContext | None = None,
        api_client_auth: ApiClientAuthContext | None = None,
        trace_id: str | None = None,
        source_ip: str | None = None,
    ) -> tuple[dict[str, Any], Provider, list[dict], int]:
        return await ProxyService._forward_response_management_request(
            method="GET",
            response_id="files",
            action=None,
            query_items=query_items,
            route_context=route_context,
            api_client_auth=api_client_auth,
            trace_id=trace_id,
            source_ip=source_ip,
            request_path_override="/files",
            log_type="files",
            unavailable_message="No available provider for files request",
            unavailable_code="files_provider_not_available",
            not_found_message="Files endpoint was not found in any authorized provider",
            not_found_code="files_not_found",
        )

    @staticmethod
    async def upload_file(
        *,
        form_fields: list[tuple[str, str]],
        form_files: list[tuple[str, tuple[str, bytes, str]]],
        route_context: RoutePolicyContext | None = None,
        api_client_auth: ApiClientAuthContext | None = None,
        trace_id: str | None = None,
        source_ip: str | None = None,
    ) -> tuple[dict[str, Any], Provider, list[dict], int]:
        result, provider, trace, latency_ms = await ProxyService._forward_response_management_request(
            method="POST",
            response_id="files",
            action=None,
            query_items=None,
            route_context=route_context,
            api_client_auth=api_client_auth,
            trace_id=trace_id,
            source_ip=source_ip,
            request_path_override="/files",
            form_fields=form_fields,
            form_files=form_files,
            log_type="files",
            unavailable_message="No available provider for file upload request",
            unavailable_code="files_provider_not_available",
            not_found_message="File upload endpoint was not found in any authorized provider",
            not_found_code="files_not_found",
        )
        for _field_name, file_data in form_files:
            filename, content, content_type = file_data
            await ProxyService._record_external_asset_event_async(
                asset_event_type="external_file_upload",
                api_client_auth=api_client_auth,
                filename=filename,
                content_type=content_type,
                file_size_bytes=len(content),
                sha256_hex=hashlib.sha256(content).hexdigest(),
                trace_id=trace_id,
                result="success",
            )
        return result, provider, trace, latency_ms

    @staticmethod
    async def retrieve_file(
        *,
        file_id: str,
        route_context: RoutePolicyContext | None = None,
        api_client_auth: ApiClientAuthContext | None = None,
        trace_id: str | None = None,
        source_ip: str | None = None,
    ) -> tuple[dict[str, Any], Provider, list[dict], int]:
        result, provider, trace, latency_ms = await ProxyService._forward_response_management_request(
            method="GET",
            response_id=file_id,
            action=None,
            query_items=None,
            route_context=route_context,
            api_client_auth=api_client_auth,
            trace_id=trace_id,
            source_ip=source_ip,
            request_path_override=f"/files/{file_id}",
            log_type="files",
            unavailable_message="No available provider for file retrieve request",
            unavailable_code="files_provider_not_available",
            not_found_message="File was not found in any authorized provider",
            not_found_code="file_not_found",
        )
        await ProxyService._record_external_asset_event_async(
            asset_event_type="external_file_read",
            api_client_auth=api_client_auth,
            filename=file_id,
            storage_scope="external_v1_file",
            trace_id=trace_id,
            result="success",
        )
        return result, provider, trace, latency_ms

    @staticmethod
    async def delete_file(
        *,
        file_id: str,
        route_context: RoutePolicyContext | None = None,
        api_client_auth: ApiClientAuthContext | None = None,
        trace_id: str | None = None,
        source_ip: str | None = None,
    ) -> tuple[dict[str, Any], Provider, list[dict], int]:
        result, provider, trace, latency_ms = await ProxyService._forward_response_management_request(
            method="DELETE",
            response_id=file_id,
            action=None,
            query_items=None,
            route_context=route_context,
            api_client_auth=api_client_auth,
            trace_id=trace_id,
            source_ip=source_ip,
            request_path_override=f"/files/{file_id}",
            log_type="files",
            unavailable_message="No available provider for file delete request",
            unavailable_code="files_provider_not_available",
            not_found_message="File was not found in any authorized provider",
            not_found_code="file_not_found",
        )
        await ProxyService._record_external_asset_event_async(
            asset_event_type="delete",
            api_client_auth=api_client_auth,
            filename=file_id,
            storage_scope="external_v1_file",
            trace_id=trace_id,
            result="success",
        )
        return result, provider, trace, latency_ms

    @staticmethod
    async def retrieve_file_content(
        *,
        file_id: str,
        route_context: RoutePolicyContext | None = None,
        api_client_auth: ApiClientAuthContext | None = None,
        trace_id: str | None = None,
        source_ip: str | None = None,
    ) -> tuple[bytes, str, Provider, list[dict], int]:
        content, content_type, provider, trace, latency_ms = await ProxyService._forward_raw_management_request(
            method="GET",
            response_id=file_id,
            request_path=f"/files/{file_id}/content",
            query_items=None,
            route_context=route_context,
            api_client_auth=api_client_auth,
            trace_id=trace_id,
            source_ip=source_ip,
            log_type="files",
            unavailable_message="No available provider for file content request",
            unavailable_code="files_provider_not_available",
            not_found_message="File content was not found in any authorized provider",
            not_found_code="file_content_not_found",
        )
        await ProxyService._record_external_asset_event_async(
            asset_event_type="external_file_read",
            api_client_auth=api_client_auth,
            filename=file_id,
            content_type=content_type,
            file_size_bytes=len(content),
            trace_id=trace_id,
            result="success",
        )
        return content, content_type, provider, trace, latency_ms

    @staticmethod
    async def _record_external_asset_event_async(
        *,
        asset_event_type: str,
        api_client_auth: ApiClientAuthContext | None,
        filename: str | None,
        storage_scope: str = "external_v1_file",
        content_type: str | None = None,
        file_size_bytes: int | None = None,
        sha256_hex: str | None = None,
        trace_id: str | None = None,
        result: str = "success",
        error: str | None = None,
    ) -> None:
        await run_in_threadpool(
            ProxyService._record_external_asset_event_sync,
            asset_event_type,
            api_client_auth,
            filename,
            storage_scope,
            content_type,
            file_size_bytes,
            sha256_hex,
            trace_id,
            result,
            error,
        )

    @staticmethod
    def _record_external_asset_event_sync(
        asset_event_type: str,
        api_client_auth: ApiClientAuthContext | None,
        filename: str | None,
        storage_scope: str,
        content_type: str | None,
        file_size_bytes: int | None,
        sha256_hex: str | None,
        trace_id: str | None,
        result: str,
        error: str | None,
    ) -> None:
        db = SessionLocal()
        try:
            api_key = api_client_auth.api_client_key if api_client_auth is not None else None
            AssetLogRecorder.record_asset_event(
                db,
                asset_id=None,
                asset_event_type=asset_event_type,
                actor_type="api_client",
                actor_id=api_key.id if api_key is not None else None,
                filename=filename,
                content_type=content_type,
                file_size_bytes=file_size_bytes,
                sha256_hex=sha256_hex,
                storage_scope=storage_scope,
                result=result,
                error=error,
                trace_id=trace_id,
            )
        except Exception as exc:
            logger.warning("Failed to record external asset event: %s", exc)
        finally:
            db.close()

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
        request_path_override: str | None = None,
        payload: dict[str, Any] | None = None,
        form_fields: list[tuple[str, str]] | None = None,
        form_files: list[tuple[str, tuple[str, bytes, str]]] | None = None,
        log_type: str = "responses",
        unavailable_message: str = "No available provider for responses management request",
        unavailable_code: str = "response_provider_not_available",
        not_found_message: str = "Response was not found in any authorized provider",
        not_found_code: str = "response_not_found",
    ) -> tuple[dict[str, Any], Provider, list[dict], int]:
        total_started = time.perf_counter()
        request_id = uuid4().hex
        request_path = request_path_override or f"/responses/{response_id}"
        if request_path_override is None and action:
            request_path = f"{request_path}/{action}"
        request_model_name = payload.get("model") if isinstance(payload, dict) and isinstance(payload.get("model"), str) else None
        db = SessionLocal()
        try:
            providers = ProxyService._ordered_response_management_providers(db, route_context=route_context)
            if not providers:
                detail = {
                    "message": unavailable_message,
                    "code": unavailable_code,
                }
                await ProxyService._write_response_management_log(
                    log_type=log_type,
                    method=method,
                    request_path=request_path,
                    response_id=response_id,
                    request_id=request_id,
                    provider=None,
                    trace=[],
                    trace_id=trace_id,
                    source_ip=source_ip,
                    api_client_auth=api_client_auth,
                    model_name=request_model_name,
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

        for provider in providers:
            started = time.perf_counter()
            try:
                async with ProviderCapacityService.async_lease(provider, is_stream=False):
                    send_kwargs: dict[str, Any] = {
                        "method": method,
                        "request_path": request_path,
                        "query_items": query_items,
                        "payload": payload,
                    }
                    if form_fields is not None or form_files is not None:
                        send_kwargs["form_fields"] = form_fields
                        send_kwargs["form_files"] = form_files
                    response_json, upstream_request_id = await ProxyService._send_response_management_request(
                        provider,
                        **send_kwargs,
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
                    log_type=log_type,
                    method=method,
                    request_path=request_path,
                    response_id=response_id,
                    request_id=request_id,
                    provider=provider,
                    trace=trace,
                    trace_id=trace_id,
                    source_ip=source_ip,
                    api_client_auth=api_client_auth,
                    model_name=request_model_name,
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
                    log_type=log_type,
                    method=method,
                    request_path=request_path,
                    response_id=response_id,
                    request_id=request_id,
                    provider=provider,
                    trace=trace,
                    trace_id=trace_id,
                    source_ip=source_ip,
                    api_client_auth=api_client_auth,
                    model_name=request_model_name,
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
                    log_type=log_type,
                    method=method,
                    request_path=request_path,
                    response_id=response_id,
                    request_id=request_id,
                    provider=provider,
                    trace=trace,
                    trace_id=trace_id,
                    source_ip=source_ip,
                    api_client_auth=api_client_auth,
                    model_name=request_model_name,
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
                    log_type=log_type,
                    method=method,
                    request_path=request_path,
                    response_id=response_id,
                    request_id=request_id,
                    provider=provider,
                    trace=trace,
                    trace_id=trace_id,
                    source_ip=source_ip,
                    api_client_auth=api_client_auth,
                    model_name=request_model_name,
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
                log_type=log_type,
                method=method,
                request_path=request_path,
                response_id=response_id,
                request_id=request_id,
                provider=None,
                trace=trace,
                trace_id=trace_id,
                source_ip=source_ip,
                api_client_auth=api_client_auth,
                model_name=request_model_name,
                success=False,
                status_code=status_code,
                latency_ms=int((time.perf_counter() - total_started) * 1000),
                upstream_request_id=None,
                detail=detail,
            )
            raise HTTPException(status_code=status_code, detail=detail)
        detail = {
            "message": not_found_message,
            "code": not_found_code,
            "object_id": response_id,
        }
        await ProxyService._write_response_management_log(
            log_type=log_type,
            method=method,
            request_path=request_path,
            response_id=response_id,
            request_id=request_id,
            provider=None,
            trace=trace,
            trace_id=trace_id,
            source_ip=source_ip,
            api_client_auth=api_client_auth,
            model_name=request_model_name,
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
        log_type: str = "responses",
        method: str,
        request_path: str,
        response_id: str,
        request_id: str,
        provider: Provider | None,
        trace: list[dict],
        trace_id: str | None,
        source_ip: str | None,
        api_client_auth: ApiClientAuthContext | None,
        model_name: str | None = None,
        success: bool,
        status_code: int,
        latency_ms: int,
        upstream_request_id: str | None,
        detail: Any | None,
    ) -> None:
        await ProxyService._run_db_write(
            LogService.create_log,
            log_type=log_type,
            provider_id=provider.id if provider is not None else None,
            provider_name=provider.name if provider is not None else None,
            trace_id=trace_id,
            model_name=model_name,
            requested_model=model_name,
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
            error_type=None if success else ProxyService._error_type_from_status(status_code, detail),
            error_code=None if success else ProxyService._error_code_from_detail(detail),
            retryable=None if success else ProxyService._is_retryable_status(status_code, detail),
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

        preferred_provider_ids = (
            list(route_context.preferred_provider_ids)
            if route_context is not None and route_context.preferred_provider_ids
            else []
        )
        ordered_ids: list[int] = []
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
        payload: dict[str, Any] | None = None,
        form_fields: list[tuple[str, str]] | None = None,
        form_files: list[tuple[str, tuple[str, bytes, str]]] | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        client = UpstreamClientService.get_client()
        response = await client.request(
            method.upper(),
            f"{provider.base_url}{request_path}",
            headers={"Authorization": f"Bearer {provider.api_key}"},
            params=query_items,
            json=payload if form_fields is None and form_files is None else None,
            data=form_fields,
            files=form_files,
            timeout=ProxyService._build_httpx_timeout(provider, payload={}, is_stream=False),
        )
        response.raise_for_status()
        if response.content:
            return response.json(), ProxyService._extract_upstream_request_id(response)
        return {"id": request_path.rsplit("/", 1)[-1], "object": "response"}, ProxyService._extract_upstream_request_id(response)

    @staticmethod
    async def _forward_raw_management_request(
        *,
        method: str,
        response_id: str,
        request_path: str,
        query_items: list[tuple[str, str]] | None,
        route_context: RoutePolicyContext | None,
        api_client_auth: ApiClientAuthContext | None,
        trace_id: str | None,
        source_ip: str | None,
        log_type: str,
        unavailable_message: str,
        unavailable_code: str,
        not_found_message: str,
        not_found_code: str,
    ) -> tuple[bytes, str, Provider, list[dict], int]:
        total_started = time.perf_counter()
        request_id = uuid4().hex
        db = SessionLocal()
        try:
            providers = ProxyService._ordered_response_management_providers(db, route_context=route_context)
            if not providers:
                detail = {"message": unavailable_message, "code": unavailable_code}
                await ProxyService._write_response_management_log(
                    log_type=log_type,
                    method=method,
                    request_path=request_path,
                    response_id=response_id,
                    request_id=request_id,
                    provider=None,
                    trace=[],
                    trace_id=trace_id,
                    source_ip=source_ip,
                    api_client_auth=api_client_auth,
                    model_name=None,
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
        for provider in providers:
            started = time.perf_counter()
            try:
                async with ProviderCapacityService.async_lease(provider, is_stream=False):
                    content, content_type, upstream_request_id = await ProxyService._send_raw_management_request(
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
                    log_type=log_type,
                    method=method,
                    request_path=request_path,
                    response_id=response_id,
                    request_id=request_id,
                    provider=provider,
                    trace=trace,
                    trace_id=trace_id,
                    source_ip=source_ip,
                    api_client_auth=api_client_auth,
                    model_name=None,
                    success=True,
                    status_code=200,
                    latency_ms=latency_ms,
                    upstream_request_id=upstream_request_id,
                    detail=None,
                )
                return content, content_type, provider, trace, latency_ms
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
                detail = {
                    "message": "Provider capacity state is unavailable",
                    "code": "provider_capacity_unavailable",
                    "provider_id": provider.id,
                    "provider_name": provider.name,
                }
                await ProxyService._write_response_management_log(
                    log_type=log_type,
                    method=method,
                    request_path=request_path,
                    response_id=response_id,
                    request_id=request_id,
                    provider=provider,
                    trace=trace,
                    trace_id=trace_id,
                    source_ip=source_ip,
                    api_client_auth=api_client_auth,
                    model_name=None,
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
                    log_type=log_type,
                    method=method,
                    request_path=request_path,
                    response_id=response_id,
                    request_id=request_id,
                    provider=provider,
                    trace=trace,
                    trace_id=trace_id,
                    source_ip=source_ip,
                    api_client_auth=api_client_auth,
                    model_name=None,
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
                    log_type=log_type,
                    method=method,
                    request_path=request_path,
                    response_id=response_id,
                    request_id=request_id,
                    provider=provider,
                    trace=trace,
                    trace_id=trace_id,
                    source_ip=source_ip,
                    api_client_auth=api_client_auth,
                    model_name=None,
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
                log_type=log_type,
                method=method,
                request_path=request_path,
                response_id=response_id,
                request_id=request_id,
                provider=None,
                trace=trace,
                trace_id=trace_id,
                source_ip=source_ip,
                api_client_auth=api_client_auth,
                model_name=None,
                success=False,
                status_code=status_code,
                latency_ms=int((time.perf_counter() - total_started) * 1000),
                upstream_request_id=None,
                detail=detail,
            )
            raise HTTPException(status_code=status_code, detail=detail)
        detail = {"message": not_found_message, "code": not_found_code, "object_id": response_id}
        await ProxyService._write_response_management_log(
            log_type=log_type,
            method=method,
            request_path=request_path,
            response_id=response_id,
            request_id=request_id,
            provider=None,
            trace=trace,
            trace_id=trace_id,
            source_ip=source_ip,
            api_client_auth=api_client_auth,
            model_name=None,
            success=False,
            status_code=status.HTTP_404_NOT_FOUND,
            latency_ms=int((time.perf_counter() - total_started) * 1000),
            upstream_request_id=None,
            detail=detail,
        )
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=detail)

    @staticmethod
    async def _send_raw_management_request(
        provider: Provider,
        *,
        method: str,
        request_path: str,
        query_items: list[tuple[str, str]] | None,
    ) -> tuple[bytes, str, str | None]:
        client = UpstreamClientService.get_client()
        response = await client.request(
            method.upper(),
            f"{provider.base_url}{request_path}",
            headers={"Authorization": f"Bearer {provider.api_key}"},
            params=query_items,
            timeout=ProxyService._build_httpx_timeout(provider, payload={}, is_stream=False),
        )
        response.raise_for_status()
        return (
            response.content,
            response.headers.get("content-type") or "application/octet-stream",
            ProxyService._extract_upstream_request_id(response),
        )

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
    def _error_type_from_status(status_code: int, detail: Any | None = None) -> str:
        return str(ProxyService._classify_retry_policy(status_code, detail).get("error_type") or "invalid_request_error")

    @staticmethod
    def _is_retryable_status(status_code: int, detail: Any | None = None) -> bool:
        return bool(ProxyService._classify_retry_policy(status_code, detail).get("retryable"))

    @staticmethod
    def _error_code_from_detail(detail: Any) -> str | None:
        if isinstance(detail, dict):
            if isinstance(detail.get("code"), str):
                return detail["code"]
            if isinstance(detail.get("error"), dict) and isinstance(detail["error"].get("code"), str):
                return detail["error"]["code"]
        return None
