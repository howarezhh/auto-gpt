from app.utils.timezone import now_beijing
import csv
import hashlib
import io
from typing import Any
from datetime import datetime, timedelta

from sqlalchemy import Text, and_, case, cast, delete, func, not_, or_, select
from sqlalchemy.orm import Session, load_only

from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.models.logging_events import RequestContentGuardEvent
from app.models.request_log import RequestLog
from app.services.cache_service import CacheService
from app.services.error_catalog_service import ErrorCatalogService
from app.services.openai_error_service import OpenAIErrorService
from app.services.proxy_request_context import get_current_provider_candidate
from app.services.redis_service import RedisService
from app.services.runtime_state_service import RuntimeStateService
from app.utils.json_utils import dumps_json, safeJsonParse


class LogService:
    """负责请求日志落库、派生指标计算和日志序列化。"""

    TOKEN_FINALIZE_MAX_ATTEMPTS = 3

    @staticmethod
    def format_money_display(value) -> str:
        if value is None:
            return ""
        return f"{float(value):.9f}".rstrip("0").rstrip(".") + " $"

    @staticmethod
    def format_price_display(value) -> str:
        if value is None:
            return ""
        return f"{float(value) * 1000:.9f}".rstrip("0").rstrip(".") + " $/1M"

    @staticmethod
    def format_token_display(value) -> str:
        if value is None:
            return ""
        numeric = max(0, int(value))
        if numeric < 1000:
            return f"{numeric} token"
        if numeric < 1000000:
            return f"{numeric / 1000:.2f}k"
        return f"{numeric / 1000000:.2f}m"

    @staticmethod
    def _escape_like(value: str) -> str:
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    @staticmethod
    def format_bool_display(value: bool | None) -> str:
        if value is None:
            return ""
        return "是" if value else "否"

    @staticmethod
    def format_csv_label(value: Any) -> str:
        if value is None:
            return ""
        text = str(value)
        return LogService.CSV_VALUE_LABELS.get(text, text)

    HEALTH_CHECK_LOG_TYPES = ("health_check", "health_check_provider", "health_check_model")
    ROUTE_TRAFFIC_LOG_TYPES = ("chat", "responses", "moderations", "files")
    TOKEN_BILLING_LOG_TYPES = ("chat", "responses", "embeddings")
    USER_VISIBLE_LOG_TYPES = ("chat", "responses", "moderations", "files")
    MODEL_LIST_PATH = "/v1/models"
    REASONING_LEVEL_NONE = "无"
    REASONING_LEVEL_VALUES = {REASONING_LEVEL_NONE, "low", "medium", "high", "xhigh"}
    METRIC_ROW_SAMPLE_LIMIT = 10000
    METRIC_GROUP_LIMIT = 500
    RECENT_RUNTIME_METRIC_MAX_ROWS = 5000
    RECENT_RUNTIME_CACHE_TTL_SECONDS = 60
    RECENT_RUNTIME_ACTIVE_CACHE_KEY = "route-runtime-metrics:active-provider-models"
    RECENT_RUNTIME_CACHE_PREFIX = "route-runtime-metrics"
    RECENT_RUNTIME_MIN_SAMPLE_FOR_ABNORMAL = 2
    RECENT_RUNTIME_HEALTH_FAILURE_CATEGORIES = {
        "timeout",
        "network",
        "upstream_transient",
        "server_error",
        "rate_limit",
        "route_unavailable",
        "invalid_response",
    }
    RECENT_RUNTIME_IGNORED_ERROR_CATEGORIES = {
        "invalid_request",
        "authentication",
        "authorization",
        "client_cancelled",
        "capability_not_supported",
        "model_unavailable",
        "content_integrity",
    }
    CLEAR_LOGS_BATCH_SIZE = 5000
    CLEAR_LOGS_MAX_BATCHES = 100
    EXPORT_LIMIT = 5000
    TOKEN_JOB_MAX_PAYLOAD_BYTES = 65536
    CSV_VALUE_LABELS = {
        "chat": "对话",
        "responses": "响应",
        "moderations": "审核",
        "files": "文件",
        "health_check": "健康检查",
        "health_check_provider": "提供商健康检查",
        "health_check_model": "模型健康检查",
        "pass": "通过",
        "review": "需复核",
        "block": "已拦截",
        "record": "记录",
        "switch_provider": "切换提供商",
        "safe_error": "安全错误",
        "record_only": "仅记录",
        "low": "低",
        "medium": "中",
        "high": "高",
        "success": "成功",
        "failed": "失败",
        "pending_tokens": "等待 Token",
        "retry": "重试",
        "billed": "已计费",
        "no_charge": "不扣费",
        "skipped": "跳过",
        "token_finalize": "Token 回填",
        "billing_process": "计费过程",
        "critical": "严重",
        "danger": "危险",
        "warning": "警告",
        "info": "信息",
        "healthy": "健康",
        "degraded": "降级",
        "unhealthy": "异常",
        "running": "运行中",
        "stale_running": "运行超时",
        "manual_single": "手动单项",
        "manual_batch": "手动批量",
        "scheduler": "调度器",
        "manual": "手动",
        "startup": "启动",
        "system": "系统",
        "acquired": "已获取",
        "unavailable": "不可用",
        "unavailable_fallback": "不可用已执行",
        "unavailable_skipped": "锁不可用跳过",
        "skipped_locked": "锁定跳过",
        "skipped_lock_unavailable": "锁不可用跳过",
        "non_stream_response": "非流式响应",
        "stream_buffer": "流式首段",
        "stream_chunk": "流式分块",
        "request_summary": "请求摘要",
        "admin_user": "管理员",
        "user": "用户",
        "api_client": "API Key",
        "user_asset": "用户素材",
        "playground_asset": "调试素材",
        "system_asset": "系统素材",
        "active": "活跃",
        "resolved": "已解决",
        "acknowledged": "已确认",
    }
    HEAVY_LOG_FIELD_NAMES = frozenset(
        (
            "request_body_json",
            "response_body_json",
            "response_text",
            "api_client_policy_snapshot_json",
            "trace_json",
            "usage_details_json",
        )
    )

    @staticmethod
    def create_log(
        db: Session,
        *,
        log_type: str,
        provider_id: int | None = None,
        provider_name: str | None = None,
        trace_id: str | None = None,
        model_name: str | None = None,
        requested_model: str | None = None,
        tenant_name: str | None = None,
        project_name: str | None = None,
        app_name: str | None = None,
        environment_name: str | None = None,
        request_id: str | None = None,
        conversation_key: str | None = None,
        session_id: str | None = None,
        source_ip: str | None = None,
        resolved_provider_model_id: int | None = None,
        request_path: str | None = None,
        http_method: str | None = None,
        is_stream: bool = False,
        has_image: bool = False,
        success: bool,
        status_code: int | None = None,
        latency_ms: int | None = None,
        first_token_latency_ms: int | None = None,
        ttfb_ms: int | None = None,
        duration_ms: int | None = None,
        tps: float | None = None,
        reasoning_level: str | None = None,
        model_reasoning_effort: str | None = None,
        attempt_count: int | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
        cache_read_tokens: int | None = None,
        cache_write_tokens: int | None = None,
        reasoning_tokens: int | None = None,
        prompt_audio_tokens: int | None = None,
        completion_audio_tokens: int | None = None,
        accepted_prediction_tokens: int | None = None,
        rejected_prediction_tokens: int | None = None,
        token_source: str | None = None,
        upstream_usage_missing: bool | None = None,
        usage_details_json: str | None = None,
        finish_reason: str | None = None,
        upstream_request_id: str | None = None,
        request_body_json: str | None = None,
        response_body_json: str | None = None,
        response_text: str | None = None,
        capability_result: dict | list | None = None,
        message: str | None = None,
        error_type: str | None = None,
        error_code: str | None = None,
        retryable: bool | None = None,
        content_guard_result: str | None = None,
        content_guard_risk_level: str | None = None,
        content_guard_categories_json: str | None = None,
        content_guard_reason: str | None = None,
        content_guard_action: str | None = None,
        content_guard_excerpt: str | None = None,
        content_guard_latency_ms: int | None = None,
        content_guard_buffer_wait_ms: int | None = None,
        content_guard_retry_provider_count: int | None = None,
        content_guard_final_strategy: str | None = None,
        content_guard_confidence: float | None = None,
        content_guard_score_delta: int | None = None,
        api_client_key_id: int | None = None,
        api_client_key_name: str | None = None,
        api_client_key_prefix: str | None = None,
        user_account_id: int | None = None,
        user_account_name: str | None = None,
        api_client_auth_result: str | None = None,
        api_client_policy_snapshot_json: str | None = None,
        billing_multiplier: float | None = None,
        channel_price_input_per_1k: float | None = None,
        channel_price_output_per_1k: float | None = None,
        channel_price_cache_per_1k: float | None = None,
        channel_price_cache_write_per_1k: float | None = None,
        trace: list[dict] | dict | None = None,
        token_request_payload: dict | None = None,
        token_response_payload: dict | None = None,
        token_response_text: str | None = None,
        schedule_token_fill: bool = True,
        auto_commit: bool = True,
        refresh_after_create: bool = True,
        flush_after_add: bool = True,
        enqueue_finalize: bool = True,
    ) -> RequestLog:
        """创建请求日志，并在必要时补充异步 token 统计任务。"""
        if not success:
            candidate_context = get_current_provider_candidate() or {}
            if provider_id is None and candidate_context.get("provider_id") is not None:
                provider_id = candidate_context.get("provider_id")
            if provider_name is None and candidate_context.get("provider_name") is not None:
                provider_name = candidate_context.get("provider_name")
            if resolved_provider_model_id is None and candidate_context.get("resolved_provider_model_id") is not None:
                resolved_provider_model_id = candidate_context.get("resolved_provider_model_id")
            if model_name is None and candidate_context.get("model_name") is not None:
                model_name = candidate_context.get("model_name")
        if provider_id is not None:
            provider_snapshot = db.get(Provider, provider_id)
            if provider_snapshot is not None:
                provider_name = provider_snapshot.name
        provider_model = None
        if resolved_provider_model_id is not None and (
            billing_multiplier is None
            or channel_price_input_per_1k is None
            or channel_price_output_per_1k is None
            or channel_price_cache_per_1k is None
        ):
            provider_model = db.get(ProviderModel, resolved_provider_model_id)
        usage_payload = LogService.extract_usage_payload(token_response_payload)
        read_tokens, write_tokens = LogService.extract_cache_tokens(token_response_payload)
        usage_token_counts = LogService.extract_usage_token_counts(usage_payload)
        usage_detail_tokens = LogService.extract_usage_detail_tokens(usage_payload)
        effective_prompt_tokens = prompt_tokens if prompt_tokens is not None else usage_token_counts["prompt_tokens"]
        effective_completion_tokens = (
            completion_tokens if completion_tokens is not None else usage_token_counts["completion_tokens"]
        )
        effective_total_tokens = total_tokens if total_tokens is not None else usage_token_counts["total_tokens"]
        effective_usage_details_json = usage_details_json
        if effective_usage_details_json is None and isinstance(usage_payload, dict):
            effective_usage_details_json = dumps_json(usage_payload)
        effective_token_source = token_source or LogService.resolve_token_source(
            has_upstream_usage=isinstance(usage_payload, dict),
            has_token_values=any(
                value is not None
                for value in (
                    effective_prompt_tokens,
                    effective_completion_tokens,
                    effective_total_tokens,
                    cache_read_tokens,
                    cache_write_tokens,
                )
            ),
            schedule_token_fill=schedule_token_fill,
            success=success,
        )
        effective_upstream_usage_missing = (
            upstream_usage_missing
            if upstream_usage_missing is not None
            else (bool(success and schedule_token_fill) and not isinstance(usage_payload, dict))
        )
        normalized_reasoning_level = LogService.normalize_reasoning_level(reasoning_level)
        normalized_model_reasoning_effort = LogService.normalize_reasoning_effort(
            model_reasoning_effort
            if model_reasoning_effort is not None
            else LogService.extract_model_reasoning_effort(token_request_payload)
        )
        effective_ttfb_ms = LogService.resolve_ttfb_ms(
            first_token_latency_ms=first_token_latency_ms,
            ttfb_ms=ttfb_ms,
            latency_ms=latency_ms,
            is_stream=is_stream,
            success=success,
        )
        effective_duration_ms = LogService.resolve_duration_ms(latency_ms=latency_ms, duration_ms=duration_ms)
        effective_attempt_count = LogService.resolve_attempt_count(attempt_count=attempt_count, trace=trace)
        effective_tps = tps if tps is not None else LogService.compute_tps(
            completion_tokens=effective_completion_tokens,
            duration_ms=effective_duration_ms,
            ttfb_ms=effective_ttfb_ms,
        )
        if not success and error_code:
            error_context = ErrorCatalogService.build_log_context(
                status_code=status_code,
                detail={"code": error_code, "message": message or ""},
                code=error_code,
                message=message,
                trace_id=trace_id,
            )
            message = str(error_context.get("message") or message or "")
            error_type = str(error_context.get("error_type") or error_type or "")
            error_code = str(error_context.get("code") or error_code)
            retryable = bool(error_context.get("retryable")) if retryable is None else retryable
            response_body_json = LogService._merge_error_context_into_response_body(
                response_body_json=response_body_json,
                error_context=error_context,
            )
            trace = LogService._merge_error_context_into_trace(trace=trace, error_context=error_context)
        if capability_result is not None and response_body_json is None:
            response_body_json = dumps_json({"capability_result": capability_result})
        log = RequestLog(
            log_type=log_type,
            provider_id=provider_id,
            provider_name=provider_name,
            trace_id=trace_id,
            model_name=model_name,
            requested_model=requested_model,
            tenant_name=tenant_name,
            project_name=project_name,
            app_name=app_name,
            environment_name=environment_name,
            request_id=request_id,
            conversation_key=conversation_key,
            session_id=session_id or LogService.extract_session_id(token_request_payload, conversation_key=conversation_key, fallback=request_id),
            source_ip=source_ip,
            resolved_provider_model_id=resolved_provider_model_id,
            request_path=request_path,
            http_method=http_method.upper() if isinstance(http_method, str) and http_method.strip() else None,
            is_stream=is_stream,
            has_image=has_image,
            success=success,
            status_code=status_code,
            latency_ms=latency_ms,
            first_token_latency_ms=first_token_latency_ms,
            ttfb_ms=effective_ttfb_ms,
            duration_ms=effective_duration_ms,
            tps=effective_tps,
            reasoning_level=normalized_reasoning_level,
            model_reasoning_effort=normalized_model_reasoning_effort,
            attempt_count=effective_attempt_count,
            prompt_tokens=effective_prompt_tokens,
            completion_tokens=effective_completion_tokens,
            total_tokens=effective_total_tokens,
            cache_read_tokens=cache_read_tokens if cache_read_tokens is not None else read_tokens,
            cache_write_tokens=cache_write_tokens if cache_write_tokens is not None else write_tokens,
            reasoning_tokens=reasoning_tokens if reasoning_tokens is not None else usage_detail_tokens.get("reasoning_tokens"),
            prompt_audio_tokens=(
                prompt_audio_tokens if prompt_audio_tokens is not None else usage_detail_tokens.get("prompt_audio_tokens")
            ),
            completion_audio_tokens=(
                completion_audio_tokens
                if completion_audio_tokens is not None
                else usage_detail_tokens.get("completion_audio_tokens")
            ),
            accepted_prediction_tokens=(
                accepted_prediction_tokens
                if accepted_prediction_tokens is not None
                else usage_detail_tokens.get("accepted_prediction_tokens")
            ),
            rejected_prediction_tokens=(
                rejected_prediction_tokens
                if rejected_prediction_tokens is not None
                else usage_detail_tokens.get("rejected_prediction_tokens")
            ),
            token_source=effective_token_source,
            upstream_usage_missing=effective_upstream_usage_missing,
            usage_details_json=effective_usage_details_json,
            finish_reason=finish_reason,
            upstream_request_id=upstream_request_id,
            request_body_json=request_body_json,
            response_body_json=response_body_json,
            response_text=response_text,
            message=message,
            error_type=error_type,
            error_code=error_code,
            retryable=retryable,
            content_guard_result=content_guard_result,
            content_guard_risk_level=content_guard_risk_level,
            content_guard_categories_json=content_guard_categories_json,
            content_guard_reason=content_guard_reason,
            content_guard_action=content_guard_action,
            content_guard_excerpt=content_guard_excerpt,
            content_guard_latency_ms=content_guard_latency_ms,
            content_guard_buffer_wait_ms=content_guard_buffer_wait_ms,
            content_guard_retry_provider_count=content_guard_retry_provider_count,
            content_guard_final_strategy=content_guard_final_strategy,
            content_guard_confidence=content_guard_confidence,
            content_guard_score_delta=content_guard_score_delta,
            api_client_key_id=api_client_key_id,
            api_client_key_name=api_client_key_name,
            api_client_key_prefix=api_client_key_prefix,
            user_account_id=user_account_id,
            user_account_name=user_account_name,
            api_client_auth_result=api_client_auth_result,
            api_client_policy_snapshot_json=api_client_policy_snapshot_json,
            billing_multiplier=billing_multiplier if billing_multiplier is not None else (provider_model.price_multiplier if provider_model else None),
            channel_price_input_per_1k=(
                channel_price_input_per_1k
                if channel_price_input_per_1k is not None
                else (provider_model.input_price_per_1k if provider_model else None)
            ),
            channel_price_output_per_1k=(
                channel_price_output_per_1k
                if channel_price_output_per_1k is not None
                else (provider_model.output_price_per_1k if provider_model else None)
            ),
            channel_price_cache_per_1k=(
                channel_price_cache_per_1k
                if channel_price_cache_per_1k is not None
                else (
                    (
                        provider_model.cache_price_per_1k
                        if provider_model.cache_price_per_1k is not None
                        else provider_model.input_price_per_1k
                    )
                    if provider_model
                    else None
                )
            ),
            channel_price_cache_write_per_1k=channel_price_cache_write_per_1k,
            trace_json=dumps_json(trace) if trace is not None else None,
        )
        if log_type in LogService.HEALTH_CHECK_LOG_TYPES:
            log.billing_status = "skipped"
            log.billing_finalized_at = now_beijing()
            log.token_finalize_error = None
            log.billing_error = None
        elif LogService._should_mark_no_charge_without_finalize(log):
            log.billing_status = "no_charge"
            log.billing_finalized_at = now_beijing()
            log.token_finalize_error = None
            log.billing_error = None
        LogService.refresh_derived_fields(log, response_payload=token_response_payload, trace=trace)
        db.add(log)
        if flush_after_add or auto_commit:
            db.flush()
        if log.id is not None:
            from app.logging.adapters.request_adapter import RequestLogRecorder

            RequestLogRecorder.record_events_from_summary(db, log, auto_commit=False)
            if auto_commit:
                db.commit()
        elif auto_commit:
            db.commit()
        if refresh_after_create:
            db.refresh(log)
        if enqueue_finalize:
            if log.id is None:
                db.flush()
            LogService.enqueue_finalize_for_log(
                log=log,
                model_name=model_name or requested_model,
                request_path=request_path,
                token_request_payload=token_request_payload,
                token_response_payload=token_response_payload,
                token_response_text=token_response_text,
                schedule_token_fill=schedule_token_fill,
        )
        LogService._cache_recent_runtime_log(log)
        return log

    @staticmethod
    def backfill_typed_events_from_request_logs(
        db: Session,
        *,
        limit: int = 500,
        scan_limit: int | None = None,
    ) -> dict[str, int | None]:
        from app.logging.adapters.request_adapter import RequestLogRecorder

        limit = max(1, min(5000, int(limit or 500)))
        normalized_scan_limit = max(limit, min(50000, int(scan_limit or limit * 5)))
        candidate_ids = (
            select(RequestLog.id)
            .order_by(RequestLog.id.desc())
            .limit(normalized_scan_limit)
            .subquery()
        )
        candidate_stmt = (
            select(RequestLog)
            .where(RequestLog.id.in_(select(candidate_ids.c.id)))
            .order_by(RequestLog.id.desc())
            .limit(limit)
        )
        missing_event_filters = [
            ~select(model.id).where(model.request_log_id == RequestLog.id).exists()
            for model in RequestLogRecorder.EVENT_MODELS.values()
        ]
        if missing_event_filters:
            candidate_stmt = candidate_stmt.where(or_(*missing_event_filters))
        logs = db.scalars(candidate_stmt).all()
        created_events = 0
        for log in logs:
            created_events += RequestLogRecorder.record_missing_events_from_summary(db, log, auto_commit=False)
        if created_events:
            db.commit()
        return {
            "scanned_logs": len(logs),
            "created_events": created_events,
            "last_request_log_id": logs[-1].id if logs else None,
            "scan_limit": normalized_scan_limit,
        }

    @staticmethod
    def _merge_error_context_into_response_body(*, response_body_json: str | None, error_context: dict[str, Any]) -> str:
        safe_context = {
            "code": error_context.get("code"),
            "message": error_context.get("message"),
            "error_type": error_context.get("error_type"),
            "public_code": error_context.get("public_code"),
            "public_message": error_context.get("public_message"),
            "trace_id": error_context.get("trace_id"),
            "category": error_context.get("category"),
            "retryable": error_context.get("retryable"),
            "recoverable": error_context.get("recoverable"),
            "handling_strategy": error_context.get("handling_strategy"),
            "alert_level": error_context.get("alert_level"),
        }
        safe_error = LogService._openai_error_from_context(safe_context)
        parsed = safeJsonParse(response_body_json) if response_body_json else None
        if isinstance(parsed, dict):
            parsed.setdefault("error", safe_error)
            parsed.setdefault("error_context", safe_context)
            return dumps_json(parsed)
        if isinstance(parsed, list):
            return dumps_json({"response_items": parsed, "error": safe_error, "error_context": safe_context})
        return dumps_json({"error": safe_error, "error_context": safe_context})

    @staticmethod
    def _openai_error_from_context(error_context: dict[str, Any]) -> dict[str, Any]:
        return {
            "message": error_context.get("public_message") or error_context.get("message") or "",
            "type": error_context.get("error_type") or "server_error",
            "code": error_context.get("public_code") or error_context.get("code"),
            "trace_id": error_context.get("trace_id"),
            "retryable": error_context.get("retryable"),
            "recoverable": error_context.get("recoverable"),
            "category": error_context.get("category"),
        }

    @staticmethod
    def _merge_error_context_into_trace(*, trace: list[dict] | dict | None, error_context: dict[str, Any]) -> list[dict] | dict:
        safe_context = {
            "code": error_context.get("code"),
            "public_code": error_context.get("public_code"),
            "trace_id": error_context.get("trace_id"),
            "category": error_context.get("category"),
            "handling_strategy": error_context.get("handling_strategy"),
            "alert_level": error_context.get("alert_level"),
        }
        event = {
            "result": "error_catalog_context",
            "error_context": safe_context,
            "latency_ms": 0,
        }
        if isinstance(trace, list):
            return [*trace, event]
        if isinstance(trace, dict):
            return {"trace": trace, "error_context": safe_context}
        return [event]

    @staticmethod
    def _token_job_payload_or_none(payload: dict | None) -> dict | None:
        """仅在 payload 足够小且可序列化时保留给异步补算任务。"""
        if not isinstance(payload, dict):
            return None
        try:
            payload_bytes = len(dumps_json(payload).encode("utf-8", errors="ignore"))
        except Exception:
            return None
        if payload_bytes > LogService.TOKEN_JOB_MAX_PAYLOAD_BYTES:
            return None
        return payload

    @staticmethod
    def enqueue_finalize_for_log(
        *,
        log: RequestLog,
        model_name: str | None,
        request_path: str | None,
        token_request_payload: dict | None = None,
        token_response_payload: dict | None = None,
        token_response_text: str | None = None,
        schedule_token_fill: bool = True,
    ) -> None:
        """在日志主记录已拿到 ID 后，按统一口径补充异步 token 统计任务。"""
        if (
            log.id is None
            or not request_path
            or not LogService.is_token_billing_finalize_candidate(log)
        ):
            return
        from app.services.token_usage_service import TokenUsageService

        safe_token_request_payload = LogService._token_job_payload_or_none(token_request_payload)

        # 大请求体不再进入异步补算任务，避免日志队列压力过高。
        TokenUsageService.enqueue_log_finalize(
            log_id=log.id,
            model_name=model_name,
            request_path=request_path,
            request_payload=safe_token_request_payload,
            response_payload=token_response_payload,
            response_text=token_response_text,
            enable_usage_fill=schedule_token_fill and safe_token_request_payload is not None,
        )

    @staticmethod
    def serialize_log(
        log: RequestLog,
        *,
        include_payload_fields: bool = True,
        derive_image_observability: bool = True,
    ) -> dict[str, Any]:
        """把单条日志对象转换为接口返回结构。"""
        data = {}
        for column in RequestLog.__table__.columns:
            if not include_payload_fields and column.name in LogService.HEAVY_LOG_FIELD_NAMES:
                data[column.name] = None
                continue
            data[column.name] = getattr(log, column.name)
        data["display_model"] = LogService.build_display_model(
            requested_model=log.requested_model,
            actual_model=log.model_name,
        )
        if derive_image_observability:
            data.update(LogService._derive_image_observability(log))
        else:
            data.update(LogService._basic_image_observability(log))
        return data

    @staticmethod
    def serialize_logs(
        logs: list[RequestLog],
        *,
        include_payload_fields: bool = True,
        derive_image_observability: bool = True,
    ) -> list[dict[str, Any]]:
        """批量序列化日志对象。"""
        return [
            LogService.serialize_log(
                item,
                include_payload_fields=include_payload_fields,
                derive_image_observability=derive_image_observability,
            )
            for item in logs
        ]

    @staticmethod
    def _lightweight_log_load_options():
        return (
            load_only(
                *[
                    getattr(RequestLog, column.name)
                    for column in RequestLog.__table__.columns
                    if column.name not in LogService.HEAVY_LOG_FIELD_NAMES
                ]
            ),
        )

    @staticmethod
    def build_display_model(*, requested_model: str | None, actual_model: str | None) -> str | None:
        requested = requested_model.strip() if isinstance(requested_model, str) and requested_model.strip() else None
        actual = actual_model.strip() if isinstance(actual_model, str) and actual_model.strip() else None
        if requested and actual and requested != actual:
            return f"{requested} -> {actual}"
        return requested or actual

    @staticmethod
    def _derive_image_observability(log: RequestLog) -> dict[str, Any]:
        """从请求和响应负载中提炼图像相关观测字段。"""
        request_payload = safeJsonParse(log.request_body_json) if log.request_body_json else None
        response_payload = safeJsonParse(log.response_body_json) if log.response_body_json else None
        has_image_input = LogService._payload_has_image_input(request_payload)
        uses_image_generation = LogService._payload_uses_image_generation(request_payload)
        generated_summary = LogService._extract_generated_image_summary(response_payload)
        generated_images_count = generated_summary.get("image_count")
        if generated_images_count is None:
            generated_images_count = LogService._extract_generated_image_count_from_text(log.response_text)
        if not uses_image_generation and generated_images_count and generated_images_count > 0:
            uses_image_generation = True
        if not has_image_input and log.has_image and not uses_image_generation:
            has_image_input = True
        request_modality = LogService._resolve_request_modality(
            has_image_input=has_image_input,
            uses_image_generation=uses_image_generation,
        )
        generated_image_mime_types = generated_summary.get("mime_types") or None
        generated_image_approx_bytes = generated_summary.get("approx_bytes")
        has_partial_generated_image = generated_summary.get("has_partial")
        generated_image_result_truncated = generated_summary.get("result_truncated")
        imagegen_related = bool(uses_image_generation or (generated_images_count and generated_images_count > 0))
        return {
            "has_image_input": has_image_input,
            "uses_image_generation": uses_image_generation,
            "request_modality": request_modality,
            "generated_images_count": generated_images_count,
            "generated_image_mime_types": generated_image_mime_types,
            "generated_image_approx_bytes": generated_image_approx_bytes,
            "has_partial_generated_image": has_partial_generated_image,
            "generated_image_result_truncated": generated_image_result_truncated,
            "image_response_mode": ("stream" if log.is_stream else "json") if imagegen_related else None,
        }

    @staticmethod
    def _basic_image_observability(log: RequestLog) -> dict[str, Any]:
        has_image_input = bool(log.has_image)
        return {
            "has_image_input": has_image_input,
            "uses_image_generation": None,
            "request_modality": "vision" if has_image_input else "text",
            "generated_images_count": None,
            "generated_image_mime_types": None,
            "generated_image_approx_bytes": None,
            "has_partial_generated_image": None,
            "generated_image_result_truncated": None,
            "image_response_mode": None,
        }

    @staticmethod
    def _resolve_request_modality(*, has_image_input: bool, uses_image_generation: bool) -> str:
        if uses_image_generation and has_image_input:
            return "image_edit"
        if uses_image_generation:
            return "image_generation"
        if has_image_input:
            return "vision"
        return "text"

    @staticmethod
    def _payload_has_image_input(payload: Any) -> bool:
        return LogService._payload_contains_type_value(payload, {"image_url", "input_image"}) or LogService._payload_contains_key(
            payload,
            {"image_url", "input_image"},
        )

    @staticmethod
    def _payload_uses_image_generation(payload: Any) -> bool:
        return LogService._payload_contains_type_value(payload, {"image_generation"})

    @staticmethod
    def _payload_contains_type_value(payload: Any, targets: set[str]) -> bool:
        if isinstance(payload, dict):
            direct_type = payload.get("type")
            if isinstance(direct_type, str) and direct_type in targets:
                return True
            if payload.get("type") == "string" and isinstance(payload.get("value"), str) and payload.get("value") in targets:
                return True
            return any(LogService._payload_contains_type_value(item, targets) for item in payload.values())
        if isinstance(payload, list):
            return any(LogService._payload_contains_type_value(item, targets) for item in payload)
        return False

    @staticmethod
    def _payload_contains_key(payload: Any, targets: set[str]) -> bool:
        if isinstance(payload, dict):
            if any(str(key) in targets for key in payload.keys()):
                return True
            return any(LogService._payload_contains_key(item, targets) for item in payload.values())
        if isinstance(payload, list):
            return any(LogService._payload_contains_key(item, targets) for item in payload)
        return False

    @staticmethod
    def _extract_generated_image_summary(payload: Any) -> dict[str, Any]:
        result = {
            "image_count": None,
            "mime_types": [],
            "approx_bytes": None,
            "has_partial": False,
            "result_truncated": False,
        }
        if not isinstance(payload, (dict, list)):
            return result

        summaries: list[dict[str, Any]] = []
        binary_summaries: list[dict[str, Any]] = []

        def walk(value: Any) -> None:
            if isinstance(value, dict):
                summary_kind = value.get("summary_kind")
                if summary_kind == "generated_image_result":
                    summaries.append(value)
                elif summary_kind == "binary_image":
                    binary_summaries.append(value)
                for item in value.values():
                    walk(item)
            elif isinstance(value, list):
                for item in value:
                    walk(item)

        walk(payload)
        source_items = summaries or binary_summaries
        if not source_items:
            return result

        image_count = 0
        approx_bytes = 0
        has_approx_bytes = False
        mime_types: list[str] = []
        seen_mime_types: set[str] = set()
        has_partial = False
        result_truncated = False
        for item in source_items:
            count_value = item.get("image_count")
            if isinstance(count_value, int):
                image_count += max(0, count_value)
            approx_value = item.get("approx_bytes")
            if isinstance(approx_value, int):
                approx_bytes += max(0, approx_value)
                has_approx_bytes = True
            for mime_type in item.get("mime_types") or []:
                if isinstance(mime_type, str) and mime_type not in seen_mime_types:
                    seen_mime_types.add(mime_type)
                    mime_types.append(mime_type)
            if item.get("has_partial") is True:
                has_partial = True
            if item.get("result_truncated") is True:
                result_truncated = True
        result["image_count"] = image_count
        result["mime_types"] = mime_types
        result["approx_bytes"] = approx_bytes if has_approx_bytes else None
        result["has_partial"] = has_partial
        result["result_truncated"] = result_truncated
        return result

    @staticmethod
    def _extract_generated_image_count_from_text(value: str | None) -> int | None:
        if not isinstance(value, str):
            return None
        prefix = "[生成了 "
        suffix = " 张图片]"
        if value.startswith(prefix) and value.endswith(suffix):
            try:
                return max(0, int(value[len(prefix):-len(suffix)]))
            except ValueError:
                return None
        return None

    @staticmethod
    def _response_has_usage(payload: Any) -> bool:
        if not isinstance(payload, dict):
            return False
        usage = payload.get("usage")
        if isinstance(usage, dict):
            return True
        nested = payload.get("response")
        return isinstance(nested, dict) and isinstance(nested.get("usage"), dict)

    @staticmethod
    def list_logs(
        db: Session,
        *,
        page: int,
        page_size: int,
        log_type: str | None,
        log_types: list[str] | None,
        provider_id: int | None,
        model_name: str | None,
        model_query: str | None,
        conversation_key: str | None,
        api_client_key_id: int | None,
        api_client_key_query: str | None,
        user_account_id: int | None,
        user_account_query: str | None,
        tenant_name: str | None,
        project_name: str | None,
        app_name: str | None,
        environment_name: str | None,
        success: bool | None,
        provider_trust_level: str | None = None,
        exclude_health_checks: bool = False,
        content_guard_result: str | None = None,
        content_guard_risk_level: str | None = None,
        content_guard_action: str | None = None,
        content_guard_final_strategy: str | None = None,
        content_guard_retry_count: int | None = None,
        content_guard_guard_stage: str | None = None,
        content_guard_category: str | None = None,
        content_guard_switched_provider: bool | None = None,
        content_guard_adaptation_skipped: bool | None = None,
        api_client_key_ids: list[int] | None = None,
    ) -> tuple[int, list[RequestLog], dict[str, int]]:
        stmt = select(RequestLog).options(*LogService._lightweight_log_load_options())
        count_stmt = select(func.count()).select_from(RequestLog)
        summary_stmt = select(
            func.count(RequestLog.id).label("total_requests"),
            func.sum(case((RequestLog.success.is_(True), 1), else_=0)).label("success_requests"),
            func.sum(case((RequestLog.success.is_(False), 1), else_=0)).label("failed_requests"),
            func.sum(RequestLog.prompt_tokens).label("prompt_tokens"),
            func.sum(RequestLog.completion_tokens).label("completion_tokens"),
            func.sum(RequestLog.total_tokens).label("total_tokens"),
            func.sum(RequestLog.total_cost).label("total_cost"),
            func.count(func.distinct(RequestLog.api_client_key_id)).label("matched_api_keys"),
        )
        stmt = LogService._apply_log_filters(
            stmt,
            log_type=log_type,
            log_types=log_types,
            provider_id=provider_id,
            provider_trust_level=provider_trust_level,
            model_name=model_name,
            model_query=model_query,
            conversation_key=conversation_key,
            api_client_key_id=api_client_key_id,
            api_client_key_query=api_client_key_query,
            user_account_id=user_account_id,
            user_account_query=user_account_query,
            tenant_name=tenant_name,
            project_name=project_name,
            app_name=app_name,
            environment_name=environment_name,
            success=success,
            exclude_health_checks=exclude_health_checks,
            content_guard_result=content_guard_result,
            content_guard_risk_level=content_guard_risk_level,
            content_guard_action=content_guard_action,
            content_guard_final_strategy=content_guard_final_strategy,
            content_guard_retry_count=content_guard_retry_count,
            content_guard_guard_stage=content_guard_guard_stage,
            content_guard_category=content_guard_category,
            content_guard_switched_provider=content_guard_switched_provider,
            content_guard_adaptation_skipped=content_guard_adaptation_skipped,
            api_client_key_ids=api_client_key_ids,
        )
        count_stmt = LogService._apply_log_filters(
            count_stmt,
            log_type=log_type,
            log_types=log_types,
            provider_id=provider_id,
            provider_trust_level=provider_trust_level,
            model_name=model_name,
            model_query=model_query,
            conversation_key=conversation_key,
            api_client_key_id=api_client_key_id,
            api_client_key_query=api_client_key_query,
            user_account_id=user_account_id,
            user_account_query=user_account_query,
            tenant_name=tenant_name,
            project_name=project_name,
            app_name=app_name,
            environment_name=environment_name,
            success=success,
            exclude_health_checks=exclude_health_checks,
            content_guard_result=content_guard_result,
            content_guard_risk_level=content_guard_risk_level,
            content_guard_action=content_guard_action,
            content_guard_final_strategy=content_guard_final_strategy,
            content_guard_retry_count=content_guard_retry_count,
            content_guard_guard_stage=content_guard_guard_stage,
            content_guard_category=content_guard_category,
            content_guard_switched_provider=content_guard_switched_provider,
            content_guard_adaptation_skipped=content_guard_adaptation_skipped,
            api_client_key_ids=api_client_key_ids,
        )
        summary_stmt = LogService._apply_log_filters(
            summary_stmt,
            log_type=log_type,
            log_types=log_types,
            provider_id=provider_id,
            provider_trust_level=provider_trust_level,
            model_name=model_name,
            model_query=model_query,
            conversation_key=conversation_key,
            api_client_key_id=api_client_key_id,
            api_client_key_query=api_client_key_query,
            user_account_id=user_account_id,
            user_account_query=user_account_query,
            tenant_name=tenant_name,
            project_name=project_name,
            app_name=app_name,
            environment_name=environment_name,
            success=success,
            exclude_health_checks=exclude_health_checks,
            content_guard_result=content_guard_result,
            content_guard_risk_level=content_guard_risk_level,
            content_guard_action=content_guard_action,
            content_guard_final_strategy=content_guard_final_strategy,
            content_guard_retry_count=content_guard_retry_count,
            content_guard_guard_stage=content_guard_guard_stage,
            content_guard_category=content_guard_category,
            content_guard_switched_provider=content_guard_switched_provider,
            content_guard_adaptation_skipped=content_guard_adaptation_skipped,
            api_client_key_ids=api_client_key_ids,
        )
        total = db.scalar(count_stmt) or 0
        summary_row = db.execute(summary_stmt).one()
        summary = {
            "total_requests": int(summary_row.total_requests or 0),
            "success_requests": int(summary_row.success_requests or 0),
            "failed_requests": int(summary_row.failed_requests or 0),
            "prompt_tokens": int(summary_row.prompt_tokens or 0),
            "completion_tokens": int(summary_row.completion_tokens or 0),
            "total_tokens": int(summary_row.total_tokens or 0),
            "total_cost": float(summary_row.total_cost or 0),
            "matched_api_keys": int(summary_row.matched_api_keys or 0),
        }
        items = list(
            db.scalars(
                stmt.order_by(RequestLog.created_at.desc(), RequestLog.id.desc()).offset((page - 1) * page_size).limit(page_size)
            )
        )
        return total, items, summary

    @staticmethod
    def _apply_log_filters(
        stmt,
        *,
        log_type: str | None,
        log_types: list[str] | None,
        provider_id: int | None,
        model_name: str | None,
        model_query: str | None,
        conversation_key: str | None,
        api_client_key_id: int | None,
        api_client_key_query: str | None,
        user_account_id: int | None,
        user_account_query: str | None,
        tenant_name: str | None,
        project_name: str | None,
        app_name: str | None,
        environment_name: str | None,
        success: bool | None,
        exclude_health_checks: bool,
        provider_trust_level: str | None = None,
        content_guard_result: str | None = None,
        content_guard_risk_level: str | None = None,
        content_guard_action: str | None = None,
        content_guard_final_strategy: str | None = None,
        content_guard_retry_count: int | None = None,
        content_guard_guard_stage: str | None = None,
        content_guard_category: str | None = None,
        content_guard_switched_provider: bool | None = None,
        content_guard_adaptation_skipped: bool | None = None,
        api_client_key_ids: list[int] | None = None,
    ):
        if exclude_health_checks:
            stmt = stmt.where(LogService._non_health_check_expr())
        needs_user_traffic_scope = (
            api_client_key_id is not None
            or api_client_key_query is not None
            or api_client_key_ids is not None
        )
        if needs_user_traffic_scope and not log_type and not log_types:
            stmt = stmt.where(LogService._route_traffic_expr())
        if log_type:
            stmt = stmt.where(RequestLog.log_type == log_type)
        elif log_types:
            stmt = stmt.where(RequestLog.log_type.in_(log_types))
        if provider_id:
            stmt = stmt.where(RequestLog.provider_id == provider_id)
        if provider_trust_level:
            provider_trust_level = provider_trust_level.strip()
        if provider_trust_level:
            stmt = stmt.where(
                RequestLog.provider_id.in_(
                    select(Provider.id).where(Provider.trust_level == provider_trust_level)
                )
            )
        if model_name:
            stmt = stmt.where(
                or_(
                    RequestLog.model_name == model_name,
                    RequestLog.requested_model == model_name,
                )
            )
        if model_query:
            keyword = f"%{model_query.strip()}%"
            stmt = stmt.where(
                or_(
                    RequestLog.model_name.ilike(keyword),
                    RequestLog.requested_model.ilike(keyword),
                )
            )
        if conversation_key:
            stmt = stmt.where(RequestLog.conversation_key == conversation_key)
        if api_client_key_id:
            if api_client_key_ids is not None and api_client_key_id not in api_client_key_ids:
                stmt = stmt.where(RequestLog.api_client_key_id == -1)
            else:
                stmt = stmt.where(RequestLog.api_client_key_id == api_client_key_id)
        elif api_client_key_ids is not None:
            if not api_client_key_ids:
                stmt = stmt.where(RequestLog.api_client_key_id == -1)
            else:
                stmt = stmt.where(RequestLog.api_client_key_id.in_(api_client_key_ids))
        if user_account_id is not None:
            stmt = stmt.where(RequestLog.user_account_id == user_account_id)
        if api_client_key_query:
            keyword = f"%{api_client_key_query.strip()}%"
            stmt = stmt.where(
                or_(
                    RequestLog.api_client_key_name.ilike(keyword),
                    RequestLog.api_client_key_prefix.ilike(keyword),
                    cast(RequestLog.api_client_key_id, Text).ilike(keyword),
                )
            )
        if user_account_query:
            keyword = f"%{user_account_query.strip()}%"
            stmt = stmt.where(
                or_(
                    RequestLog.user_account_name.ilike(keyword),
                    cast(RequestLog.user_account_id, Text).ilike(keyword),
                )
            )
        if tenant_name:
            stmt = stmt.where(RequestLog.tenant_name == tenant_name.strip())
        if project_name:
            stmt = stmt.where(RequestLog.project_name == project_name.strip())
        if app_name:
            stmt = stmt.where(RequestLog.app_name == app_name.strip())
        if environment_name:
            stmt = stmt.where(RequestLog.environment_name == environment_name.strip())
        if success is not None:
            stmt = stmt.where(RequestLog.success == success)
        if content_guard_result:
            stmt = stmt.where(RequestLog.content_guard_result == content_guard_result.strip())
        if content_guard_risk_level:
            stmt = stmt.where(RequestLog.content_guard_risk_level == content_guard_risk_level.strip())
        if content_guard_action:
            stmt = stmt.where(RequestLog.content_guard_action == content_guard_action.strip())
        if content_guard_final_strategy:
            stmt = stmt.where(RequestLog.content_guard_final_strategy == content_guard_final_strategy.strip())
        if content_guard_retry_count is not None:
            stmt = stmt.where(RequestLog.content_guard_retry_provider_count == content_guard_retry_count)
        if content_guard_guard_stage:
            stage = content_guard_guard_stage.strip()
            child_stage_logs = select(RequestContentGuardEvent.request_log_id).where(
                RequestContentGuardEvent.request_log_id.is_not(None),
                RequestContentGuardEvent.guard_stage == stage,
            )
            stmt = stmt.where(
                or_(
                    RequestLog.id.in_(child_stage_logs),
                    RequestLog.trace_json.ilike(
                        f'%"guard_stage": "{LogService._escape_like(stage)}"%',
                        escape="\\",
                    ),
                    RequestLog.trace_json.ilike(
                        f'%"guard_stage":"{LogService._escape_like(stage)}"%',
                        escape="\\",
                    ),
                )
            )
        if content_guard_category:
            category = content_guard_category.strip()
            category_pattern = f'%"{LogService._escape_like(category)}"%'
            child_category_logs = select(RequestContentGuardEvent.request_log_id).where(
                RequestContentGuardEvent.request_log_id.is_not(None),
                or_(
                    RequestContentGuardEvent.matched_categories_json.ilike(category_pattern, escape="\\"),
                    RequestContentGuardEvent.matched_rules_json.ilike(category_pattern, escape="\\"),
                ),
            )
            stmt = stmt.where(
                or_(
                    RequestLog.content_guard_categories_json.ilike(category_pattern, escape="\\"),
                    RequestLog.id.in_(child_category_logs),
                )
            )
        if content_guard_switched_provider is not None:
            if content_guard_switched_provider:
                stmt = stmt.where(RequestLog.content_guard_retry_provider_count > 0)
            else:
                stmt = stmt.where(
                    or_(
                        RequestLog.content_guard_retry_provider_count.is_(None),
                        RequestLog.content_guard_retry_provider_count <= 0,
                    )
                )
        if content_guard_adaptation_skipped is not None:
            skipped_expr = or_(
                RequestLog.error_code.in_(
                    [
                        "endpoint_fallback_conversion_unsafe",
                        "endpoint_response_conversion_unsafe",
                        "unsupported_endpoint_fallback",
                    ]
                ),
                RequestLog.trace_json.ilike('%"event": "endpoint_fallback_preselected"%'),
                RequestLog.trace_json.ilike('%"code": "endpoint_fallback_conversion_unsafe"%'),
                RequestLog.trace_json.ilike('%"code": "endpoint_response_conversion_unsafe"%'),
            )
            stmt = stmt.where(skipped_expr if content_guard_adaptation_skipped else not_(skipped_expr))
        return stmt

    @staticmethod
    def _non_health_check_expr():
        return not_(
            or_(
                RequestLog.log_type.in_(LogService.HEALTH_CHECK_LOG_TYPES),
                RequestLog.log_type.like("health_check_%"),
            )
        )

    @staticmethod
    def _route_traffic_expr():
        return RequestLog.log_type.in_(LogService.ROUTE_TRAFFIC_LOG_TYPES)

    @staticmethod
    def _token_billing_finalize_candidate_expr():
        return and_(
            RequestLog.request_path.is_not(None),
            LogService._non_model_list_request_expr(),
            LogService._non_health_check_expr(),
            RequestLog.log_type.in_(LogService.TOKEN_BILLING_LOG_TYPES),
            RequestLog.api_client_key_id.is_not(None),
            RequestLog.success.is_(True),
        )

    @staticmethod
    def _pending_token_billing_finalize_expr(*, max_attempts: int | None = TOKEN_FINALIZE_MAX_ATTEMPTS):
        conditions = [
            LogService._token_billing_finalize_candidate_expr(),
            or_(
                RequestLog.billing_finalized_at.is_(None),
                RequestLog.billing_status == "pending_tokens",
            ),
        ]
        if max_attempts is not None:
            conditions.append(
                or_(
                    RequestLog.token_finalize_attempt_count.is_(None),
                    RequestLog.token_finalize_attempt_count < max(0, int(max_attempts)),
                )
            )
        return and_(*conditions)

    @staticmethod
    def is_token_billing_finalize_candidate(log: RequestLog) -> bool:
        return bool(
            log.request_path
            and not LogService.is_model_list_request_path(log.request_path)
            and log.log_type not in LogService.HEALTH_CHECK_LOG_TYPES
            and log.log_type in LogService.TOKEN_BILLING_LOG_TYPES
            and log.api_client_key_id is not None
            and log.success is True
        )

    @staticmethod
    def _should_mark_no_charge_without_finalize(log: RequestLog) -> bool:
        if log.api_client_key_id is None or log.billing_finalized_at is not None:
            return False
        if LogService.is_token_billing_finalize_candidate(log):
            return False
        return True

    @staticmethod
    def is_model_list_request_path(request_path: str | None) -> bool:
        return bool(
            request_path == LogService.MODEL_LIST_PATH
            or (isinstance(request_path, str) and request_path.startswith(f"{LogService.MODEL_LIST_PATH}/"))
        )

    @staticmethod
    def _non_model_list_request_expr():
        return or_(
            RequestLog.request_path.is_(None),
            not_(
                or_(
                    RequestLog.request_path == LogService.MODEL_LIST_PATH,
                    RequestLog.request_path.like(f"{LogService.MODEL_LIST_PATH}/%"),
                )
            ),
        )

    @staticmethod
    def get_filter_options(
        db: Session,
        *,
        exclude_health_checks: bool = False,
        user_account_id: int | None = None,
        api_client_key_ids: list[int] | None = None,
        limit: int = 200,
    ) -> dict[str, list[dict[str, str]]]:
        normalized_limit = max(1, min(int(limit or 200), 500))
        api_key_scope = ",".join(str(item) for item in sorted(api_client_key_ids or []))
        api_key_scope_digest = hashlib.sha256(api_key_scope.encode("utf-8")).hexdigest()[:16] if api_key_scope else "*"
        cache_key = (
            "logs:filter-options:"
            f"exclude_health={int(bool(exclude_health_checks))}:"
            f"user={user_account_id or 0}:keys={api_key_scope_digest}:limit={normalized_limit}"
        )
        cached = CacheService.get(cache_key)
        if isinstance(cached, dict):
            return cached

        def build_business_stmt(column):
            stmt = select(column).where(column.is_not(None), column != "")
            return LogService._apply_log_filters(
                stmt,
                log_type=None,
                log_types=None,
                provider_id=None,
                provider_trust_level=None,
                model_name=None,
                model_query=None,
                conversation_key=None,
                api_client_key_id=None,
                api_client_key_query=None,
                user_account_id=user_account_id,
                user_account_query=None,
                tenant_name=None,
                project_name=None,
                app_name=None,
                environment_name=None,
                success=None,
                exclude_health_checks=exclude_health_checks,
                api_client_key_ids=api_client_key_ids,
            )

        provider_stmt = select(RequestLog.provider_id, RequestLog.provider_name).where(RequestLog.provider_id.is_not(None))
        model_stmt = select(RequestLog.model_name).where(RequestLog.model_name.is_not(None), RequestLog.model_name != "")
        requested_model_stmt = select(RequestLog.requested_model).where(
            RequestLog.requested_model.is_not(None),
            RequestLog.requested_model != "",
        )
        api_key_stmt = select(
            RequestLog.api_client_key_id,
            RequestLog.api_client_key_name,
            RequestLog.api_client_key_prefix,
        ).where(RequestLog.api_client_key_id.is_not(None))
        user_stmt = select(RequestLog.user_account_id, RequestLog.user_account_name).where(RequestLog.user_account_id.is_not(None))
        tenant_stmt = build_business_stmt(RequestLog.tenant_name)
        project_stmt = build_business_stmt(RequestLog.project_name)
        app_stmt = build_business_stmt(RequestLog.app_name)
        environment_stmt = build_business_stmt(RequestLog.environment_name)
        provider_stmt = LogService._apply_log_filters(
            provider_stmt,
            log_type=None,
            log_types=None,
            provider_id=None,
            provider_trust_level=None,
            model_name=None,
            model_query=None,
            conversation_key=None,
            api_client_key_id=None,
            api_client_key_query=None,
            user_account_id=user_account_id,
            user_account_query=None,
            tenant_name=None,
            project_name=None,
            app_name=None,
            environment_name=None,
            success=None,
            exclude_health_checks=exclude_health_checks,
            api_client_key_ids=api_client_key_ids,
        )
        model_stmt = LogService._apply_log_filters(
            model_stmt,
            log_type=None,
            log_types=None,
            provider_id=None,
            provider_trust_level=None,
            model_name=None,
            model_query=None,
            conversation_key=None,
            api_client_key_id=None,
            api_client_key_query=None,
            user_account_id=user_account_id,
            user_account_query=None,
            tenant_name=None,
            project_name=None,
            app_name=None,
            environment_name=None,
            success=None,
            exclude_health_checks=exclude_health_checks,
            api_client_key_ids=api_client_key_ids,
        )
        requested_model_stmt = LogService._apply_log_filters(
            requested_model_stmt,
            log_type=None,
            log_types=None,
            provider_id=None,
            provider_trust_level=None,
            model_name=None,
            model_query=None,
            conversation_key=None,
            api_client_key_id=None,
            api_client_key_query=None,
            user_account_id=user_account_id,
            user_account_query=None,
            tenant_name=None,
            project_name=None,
            app_name=None,
            environment_name=None,
            success=None,
            exclude_health_checks=exclude_health_checks,
            api_client_key_ids=api_client_key_ids,
        )
        api_key_stmt = LogService._apply_log_filters(
            api_key_stmt,
            log_type=None,
            log_types=None,
            provider_id=None,
            provider_trust_level=None,
            model_name=None,
            model_query=None,
            conversation_key=None,
            api_client_key_id=None,
            api_client_key_query=None,
            user_account_id=user_account_id,
            user_account_query=None,
            tenant_name=None,
            project_name=None,
            app_name=None,
            environment_name=None,
            success=None,
            exclude_health_checks=exclude_health_checks,
            api_client_key_ids=api_client_key_ids,
        )
        user_stmt = LogService._apply_log_filters(
            user_stmt,
            log_type=None,
            log_types=None,
            provider_id=None,
            model_name=None,
            model_query=None,
            conversation_key=None,
            api_client_key_id=None,
            api_client_key_query=None,
            user_account_id=user_account_id,
            user_account_query=None,
            tenant_name=None,
            project_name=None,
            app_name=None,
            environment_name=None,
            success=None,
            exclude_health_checks=exclude_health_checks,
            api_client_key_ids=api_client_key_ids,
        )

        provider_rows = db.execute(
            provider_stmt.distinct().order_by(RequestLog.provider_id.asc(), RequestLog.provider_name.asc()).limit(normalized_limit)
        )
        model_rows = list(db.execute(model_stmt.distinct().order_by(RequestLog.model_name.asc()).limit(normalized_limit)))
        requested_model_rows = list(
            db.execute(requested_model_stmt.distinct().order_by(RequestLog.requested_model.asc()).limit(normalized_limit))
        )
        api_key_rows = db.execute(
            api_key_stmt.distinct().order_by(RequestLog.api_client_key_id.asc(), RequestLog.api_client_key_name.asc()).limit(normalized_limit)
        )
        user_rows = db.execute(
            user_stmt.distinct().order_by(RequestLog.user_account_id.asc(), RequestLog.user_account_name.asc()).limit(normalized_limit)
        )
        tenant_rows = db.execute(tenant_stmt.distinct().order_by(RequestLog.tenant_name.asc()).limit(normalized_limit))
        project_rows = db.execute(project_stmt.distinct().order_by(RequestLog.project_name.asc()).limit(normalized_limit))
        app_rows = db.execute(app_stmt.distinct().order_by(RequestLog.app_name.asc()).limit(normalized_limit))
        environment_rows = db.execute(environment_stmt.distinct().order_by(RequestLog.environment_name.asc()).limit(normalized_limit))

        providers = [
            {
                "value": str(row.provider_id),
                "label": f"{row.provider_id} · {row.provider_name or '-'}",
            }
            for row in provider_rows
            if row.provider_id is not None
        ]
        model_values: set[str] = set()
        for row in model_rows:
            if row.model_name:
                model_values.add(str(row.model_name))
        for row in requested_model_rows:
            if row.requested_model:
                model_values.add(str(row.requested_model))
        model_names = [
            {
                "value": value,
                "label": value,
            }
            for value in sorted(model_values)
        ]

        api_client_key_ids: list[dict[str, str]] = []
        api_client_key_queries: list[dict[str, str]] = []
        seen_query_values: set[str] = set()
        for row in api_key_rows:
            if row.api_client_key_id is None:
                continue
            key_name = row.api_client_key_name or "-"
            key_prefix = row.api_client_key_prefix or "-"
            api_client_key_ids.append(
                {
                    "value": str(row.api_client_key_id),
                    "label": f"{row.api_client_key_id} · {key_name}",
                }
            )
            query_value = row.api_client_key_prefix or row.api_client_key_name or str(row.api_client_key_id)
            if query_value in seen_query_values:
                continue
            seen_query_values.add(query_value)
            api_client_key_queries.append(
                {
                    "value": str(query_value),
                    "label": f"{key_name} · {key_prefix}",
                }
            )
        users = [
            {
                "value": str(row.user_account_id),
                "label": f"{row.user_account_id} · {row.user_account_name or '-'}",
            }
            for row in user_rows
            if row.user_account_id is not None
        ]
        tenants = [
            {"value": str(row.tenant_name), "label": str(row.tenant_name)}
            for row in tenant_rows
            if row.tenant_name
        ]
        projects = [
            {"value": str(row.project_name), "label": str(row.project_name)}
            for row in project_rows
            if row.project_name
        ]
        apps = [
            {"value": str(row.app_name), "label": str(row.app_name)}
            for row in app_rows
            if row.app_name
        ]
        environments = [
            {"value": str(row.environment_name), "label": str(row.environment_name)}
            for row in environment_rows
            if row.environment_name
        ]

        result = {
            "providers": providers,
            "model_names": model_names[:normalized_limit],
            "api_client_key_ids": api_client_key_ids,
            "api_client_key_queries": api_client_key_queries,
            "users": users,
            "tenants": tenants,
            "projects": projects,
            "apps": apps,
            "environments": environments,
        }
        return CacheService.set(cache_key, result, ttl_seconds=10)

    @staticmethod
    def normalize_reasoning_level(value: str | None) -> str:
        if value is None:
            return LogService.REASONING_LEVEL_NONE
        normalized = str(value).strip()
        if not normalized:
            return LogService.REASONING_LEVEL_NONE
        lowered = normalized.lower()
        if lowered in {"none", "null", "unset"}:
            return LogService.REASONING_LEVEL_NONE
        if lowered in {"low", "medium", "high", "xhigh"}:
            return lowered
        if normalized == LogService.REASONING_LEVEL_NONE:
            return LogService.REASONING_LEVEL_NONE
        return LogService.REASONING_LEVEL_NONE

    @staticmethod
    def normalize_reasoning_effort(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        if not normalized:
            return None
        lowered = normalized.lower()
        if lowered in {"none", "null", "unset", LogService.REASONING_LEVEL_NONE}:
            return None
        if lowered in {"low", "medium", "high", "xhigh"}:
            return lowered
        return None

    @staticmethod
    def extract_model_reasoning_effort(payload: dict | None) -> str | None:
        if not isinstance(payload, dict):
            return None
        for key in ("model_reasoning_effort", "reasoning_effort"):
            value = payload.get(key)
            if isinstance(value, str):
                return LogService.normalize_reasoning_effort(value)
        reasoning = payload.get("reasoning")
        if isinstance(reasoning, dict):
            for key in ("effort", "reasoning_effort"):
                value = reasoning.get(key)
                if isinstance(value, str):
                    return LogService.normalize_reasoning_effort(value)
        return None

    @staticmethod
    def extract_reasoning_level(payload: dict | None) -> str:
        if not isinstance(payload, dict):
            return LogService.REASONING_LEVEL_NONE
        effort = LogService.extract_model_reasoning_effort(payload)
        if effort is not None:
            return LogService.normalize_reasoning_level(effort)
        direct_value = payload.get("reasoning_level")
        if isinstance(direct_value, str):
            return LogService.normalize_reasoning_level(direct_value)
        reasoning = payload.get("reasoning")
        if isinstance(reasoning, dict):
            for key in ("level",):
                value = reasoning.get(key)
                if isinstance(value, str):
                    return LogService.normalize_reasoning_level(value)
        return LogService.REASONING_LEVEL_NONE

    @staticmethod
    def extract_session_id(
        payload: dict | None,
        *,
        conversation_key: str | None = None,
        fallback: str | None = None,
    ) -> str | None:
        if isinstance(payload, dict):
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
        return conversation_key or fallback

    @staticmethod
    def resolve_attempt_count(*, attempt_count: int | None, trace: list[dict] | dict | None) -> int:
        derived = LogService.derive_attempt_count(trace)
        if isinstance(trace, list) and trace:
            has_route_wait = any(
                isinstance(item, dict) and item.get("result") == "route_exhausted_wait_retry"
                for item in trace
            )
            if derived > 0 or has_route_wait:
                return derived
        return int(attempt_count or 0)

    @staticmethod
    def derive_attempt_count(trace: list[dict] | dict | None) -> int:
        if not isinstance(trace, list) or not trace:
            return 0
        attempt_markers = {
            "success",
            "http_error",
            "exception",
            "model_not_found",
            "rate_limited",
            "request_rejected",
            "upstream_auth_error",
            "stream_opened",
            "capacity_limited",
            "capacity_unavailable",
            "auth_rejected",
            "interrupted",
            "client_cancelled",
        }
        direct_attempt_count = sum(
            1 for item in trace if isinstance(item, dict) and item.get("result") in attempt_markers
        )
        if direct_attempt_count > 0:
            return direct_attempt_count
        return 0

    @staticmethod
    def resolve_ttfb_ms(
        *,
        first_token_latency_ms: int | None,
        ttfb_ms: int | None,
        latency_ms: int | None,
        is_stream: bool,
        success: bool,
    ) -> int | None:
        if ttfb_ms is not None:
            return int(ttfb_ms)
        if first_token_latency_ms is not None:
            return int(first_token_latency_ms)
        if not is_stream and success and latency_ms is not None:
            return int(latency_ms)
        return None

    @staticmethod
    def resolve_duration_ms(*, latency_ms: int | None, duration_ms: int | None) -> int | None:
        if duration_ms is not None:
            return int(duration_ms)
        if latency_ms is not None:
            return int(latency_ms)
        return None

    @staticmethod
    def compute_tps(
        *,
        completion_tokens: int | None,
        duration_ms: int | None,
        ttfb_ms: int | None,
    ) -> float | None:
        if completion_tokens is None or duration_ms is None or duration_ms <= 0:
            return None
        active_duration_ms = duration_ms
        if ttfb_ms is not None and duration_ms > ttfb_ms:
            active_duration_ms = duration_ms - ttfb_ms
        if active_duration_ms <= 0:
            active_duration_ms = duration_ms
        if active_duration_ms <= 0:
            return None
        return round((completion_tokens * 1000) / active_duration_ms, 4)

    @staticmethod
    def extract_cache_tokens(response_payload: dict | None) -> tuple[int | None, int | None]:
        usage = LogService.extract_usage_payload(response_payload)
        if not isinstance(usage, dict):
            return None, None
        cache_read = LogService._extract_usage_int(
            usage,
            ("cache_read_tokens",),
            ("cache_read_input_tokens",),
            ("cache_read_input_token_count",),
            ("prompt_cache_hit_tokens",),
            ("cacheReadInputTokens",),
            ("cacheReadInputTokenCount",),
            ("cached_tokens",),
            ("cachedTokens",),
            ("cached_token_count",),
            ("cachedTokenCount",),
            ("cached_content_token_count",),
            ("cachedContentTokenCount",),
            ("prompt_tokens_details", "cached_tokens"),
            ("prompt_tokens_details", "cachedTokens"),
            ("input_tokens_details", "cached_tokens"),
            ("input_tokens_details", "cachedTokens"),
        )
        cache_write = LogService._extract_usage_int(
            usage,
            ("cache_write_tokens",),
            ("cache_write_input_tokens",),
            ("cache_write_input_token_count",),
            ("cacheWriteInputTokens",),
            ("cacheWriteInputTokenCount",),
            ("cache_creation_tokens",),
            ("cache_creation_input_tokens",),
            ("cache_creation_input_token_count",),
            ("cacheCreationTokens",),
            ("cacheCreationInputTokens",),
            ("cacheCreationInputTokenCount",),
            ("prompt_tokens_details", "cache_creation_tokens"),
            ("prompt_tokens_details", "cache_creation_input_tokens"),
            ("prompt_tokens_details", "cacheCreationTokens"),
            ("prompt_tokens_details", "cacheCreationInputTokens"),
            ("input_tokens_details", "cache_creation_tokens"),
            ("input_tokens_details", "cache_creation_input_tokens"),
            ("input_tokens_details", "cacheCreationTokens"),
            ("input_tokens_details", "cacheCreationInputTokens"),
        )
        return cache_read, cache_write

    @staticmethod
    def extract_usage_payload(response_payload: dict | None) -> dict | None:
        if not isinstance(response_payload, dict):
            return None
        usage = response_payload.get("usage")
        if not isinstance(usage, dict):
            usage_metadata = response_payload.get("usageMetadata")
            if isinstance(usage_metadata, dict):
                usage = {
                    "usage_schema": "gemini_usage_metadata",
                    "prompt_tokens": usage_metadata.get("promptTokenCount"),
                    "input_tokens": usage_metadata.get("promptTokenCount"),
                    "completion_tokens": usage_metadata.get("candidatesTokenCount"),
                    "output_tokens": usage_metadata.get("candidatesTokenCount"),
                    "total_tokens": usage_metadata.get("totalTokenCount"),
                    "cache_read_tokens": usage_metadata.get("cachedContentTokenCount"),
                    "prompt_tokens_details": {"cached_tokens": usage_metadata.get("cachedContentTokenCount")},
                    "native_usage": {"provider": "gemini", "usageMetadata": usage_metadata},
                }
                if usage_metadata.get("thoughtsTokenCount") is not None:
                    usage["reasoning_tokens"] = usage_metadata.get("thoughtsTokenCount")
                    usage["completion_tokens_details"] = {"reasoning_tokens": usage_metadata.get("thoughtsTokenCount")}
                return usage
        if not isinstance(usage, dict):
            nested_response = response_payload.get("response")
            if isinstance(nested_response, dict):
                usage = nested_response.get("usage")
        return usage if isinstance(usage, dict) else None

    @staticmethod
    def extract_usage_token_counts(usage: dict | None) -> dict[str, int | None]:
        if not isinstance(usage, dict):
            return {"prompt_tokens": None, "completion_tokens": None, "total_tokens": None}
        prompt_tokens = LogService._extract_usage_int(
            usage,
            ("prompt_tokens",),
            ("input_tokens",),
        )
        completion_tokens = LogService._extract_usage_int(
            usage,
            ("completion_tokens",),
            ("output_tokens",),
        )
        total_tokens = LogService._extract_usage_int(usage, ("total_tokens",))
        if total_tokens is None and (prompt_tokens is not None or completion_tokens is not None):
            total_tokens = int(prompt_tokens or 0) + int(completion_tokens or 0)
        return {
            "prompt_tokens": LogService.normalize_prompt_tokens_for_cache_usage(usage, prompt_tokens),
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
        }

    @staticmethod
    def extract_usage_detail_tokens(usage: dict | None) -> dict[str, int | None]:
        if not isinstance(usage, dict):
            return {
                "reasoning_tokens": None,
                "prompt_audio_tokens": None,
                "completion_audio_tokens": None,
                "accepted_prediction_tokens": None,
                "rejected_prediction_tokens": None,
            }
        return {
            "reasoning_tokens": LogService._extract_usage_int(
                usage,
                ("reasoning_tokens",),
                ("thoughtsTokenCount",),
                ("completion_tokens_details", "reasoning_tokens"),
                ("output_tokens_details", "reasoning_tokens"),
            ),
            "prompt_audio_tokens": LogService._extract_usage_int(
                usage,
                ("prompt_tokens_details", "audio_tokens"),
                ("input_tokens_details", "audio_tokens"),
            ),
            "completion_audio_tokens": LogService._extract_usage_int(
                usage,
                ("completion_tokens_details", "audio_tokens"),
                ("output_tokens_details", "audio_tokens"),
            ),
            "accepted_prediction_tokens": LogService._extract_usage_int(
                usage,
                ("completion_tokens_details", "accepted_prediction_tokens"),
                ("output_tokens_details", "accepted_prediction_tokens"),
            ),
            "rejected_prediction_tokens": LogService._extract_usage_int(
                usage,
                ("completion_tokens_details", "rejected_prediction_tokens"),
                ("output_tokens_details", "rejected_prediction_tokens"),
            ),
        }

    @staticmethod
    def resolve_token_source(
        *,
        has_upstream_usage: bool,
        has_token_values: bool,
        schedule_token_fill: bool,
        success: bool,
    ) -> str | None:
        if has_upstream_usage:
            return "upstream_usage"
        if has_token_values:
            return "precomputed"
        if not success:
            return "missing"
        if schedule_token_fill:
            return "pending"
        return "disabled"

    @staticmethod
    def normalize_prompt_tokens_for_cache_usage(usage: dict, prompt_tokens: int | None) -> int | None:
        """Anthropic-style usage separates cache read/write tokens from input_tokens."""
        if prompt_tokens is None or usage.get("prompt_tokens") is not None:
            return prompt_tokens
        has_anthropic_style_cache = any(
            LogService._extract_usage_int(usage, path) is not None
            for path in (
                ("cache_read_input_tokens",),
                ("cache_read_input_token_count",),
                ("cache_creation_input_tokens",),
                ("cache_creation_input_token_count",),
                ("cacheReadInputTokens",),
                ("cacheCreationInputTokens",),
            )
        )
        if not has_anthropic_style_cache:
            return prompt_tokens
        cache_read, cache_write = LogService.extract_cache_tokens({"usage": usage})
        return max(0, int(prompt_tokens) + int(cache_read or 0) + int(cache_write or 0))

    @staticmethod
    def _extract_usage_int(usage: dict, *paths: tuple[str, ...]) -> int | None:
        for path in paths:
            current = usage
            for key in path:
                if not isinstance(current, dict):
                    current = None
                    break
                current = current.get(key)
            if isinstance(current, bool):
                continue
            if isinstance(current, (int, float)):
                return max(0, int(current))
        return None

    @staticmethod
    def refresh_derived_fields(
        log: RequestLog,
        *,
        response_payload: dict | None = None,
        trace: list[dict] | dict | None = None,
    ) -> bool:
        changed = False
        parsed_trace = trace
        if parsed_trace is None:
            parsed_trace = safeJsonParse(log.trace_json) if log.trace_json else None
        if not log.reasoning_level:
            log.reasoning_level = LogService.REASONING_LEVEL_NONE
            changed = True
        derived_attempt_count = LogService.derive_attempt_count(parsed_trace)
        if derived_attempt_count and ((log.attempt_count or 0) <= 0 or derived_attempt_count > int(log.attempt_count or 0)):
            log.attempt_count = derived_attempt_count
            changed = True
        derived_ttfb = LogService.resolve_ttfb_ms(
            first_token_latency_ms=log.first_token_latency_ms,
            ttfb_ms=log.ttfb_ms,
            latency_ms=log.latency_ms,
            is_stream=log.is_stream,
            success=log.success,
        )
        if log.ttfb_ms != derived_ttfb:
            log.ttfb_ms = derived_ttfb
            changed = True
        derived_duration = LogService.resolve_duration_ms(latency_ms=log.latency_ms, duration_ms=log.duration_ms)
        if log.duration_ms != derived_duration:
            log.duration_ms = derived_duration
            changed = True
        derived_tps = LogService.compute_tps(
            completion_tokens=log.completion_tokens,
            duration_ms=log.duration_ms,
            ttfb_ms=log.ttfb_ms,
        )
        if log.tps != derived_tps:
            log.tps = derived_tps
            changed = True
        payload = response_payload
        if payload is None:
            parsed_response = safeJsonParse(log.response_body_json) if log.response_body_json else None
            payload = parsed_response if isinstance(parsed_response, dict) else None
        usage_payload = LogService.extract_usage_payload(payload)
        usage_token_counts = LogService.extract_usage_token_counts(usage_payload)
        for field_name, value in usage_token_counts.items():
            if value is not None and getattr(log, field_name, None) != value:
                setattr(log, field_name, value)
                changed = True
        cache_read, cache_write = LogService.extract_cache_tokens(payload)
        if cache_read is not None and log.cache_read_tokens != cache_read:
            log.cache_read_tokens = cache_read
            changed = True
        if cache_write is not None and log.cache_write_tokens != cache_write:
            log.cache_write_tokens = cache_write
            changed = True
        if isinstance(usage_payload, dict):
            usage_details_json = dumps_json(usage_payload)
            if log.usage_details_json != usage_details_json:
                log.usage_details_json = usage_details_json
                changed = True
            if log.token_source != "upstream_usage":
                log.token_source = "upstream_usage"
                changed = True
            if log.upstream_usage_missing is not False:
                log.upstream_usage_missing = False
                changed = True
            detail_tokens = LogService.extract_usage_detail_tokens(usage_payload)
            for field_name, value in detail_tokens.items():
                if value is not None and getattr(log, field_name, None) != value:
                    setattr(log, field_name, value)
                    changed = True
        return changed

    @staticmethod
    def clear_logs(db: Session) -> int:
        total_deleted = 0
        for _ in range(LogService.CLEAR_LOGS_MAX_BATCHES):
            ids = list(
                db.scalars(
                    select(RequestLog.id)
                    .order_by(RequestLog.id.asc())
                    .limit(LogService.CLEAR_LOGS_BATCH_SIZE)
                )
            )
            if not ids:
                break
            result = db.execute(delete(RequestLog).where(RequestLog.id.in_(ids)))
            db.commit()
            total_deleted += int(result.rowcount or 0)
            if len(ids) < LogService.CLEAR_LOGS_BATCH_SIZE:
                break
        return total_deleted

    @staticmethod
    def metric_summary(
        db: Session,
        *,
        window_minutes: int,
        user_account_id: int | None = None,
        api_client_key_ids: list[int] | None = None,
    ) -> list[dict]:
        cache_key = LogService._metric_cache_key(
            "summary",
            window_minutes=window_minutes,
            user_account_id=user_account_id,
            api_client_key_ids=api_client_key_ids,
        )
        cached = CacheService.get(cache_key)
        if isinstance(cached, list):
            return cached
        since = now_beijing() - timedelta(minutes=window_minutes)
        window_seconds = max(1, window_minutes * 60)
        results: list[dict] = []
        stmt = (
            select(
                RequestLog.provider_id,
                RequestLog.provider_name,
                RequestLog.requested_model,
                func.count(RequestLog.id).label("total_requests"),
                func.sum(case((RequestLog.success.is_(True), 1), else_=0)).label("success_requests"),
                func.avg(RequestLog.latency_ms).label("avg_latency_ms"),
                func.avg(RequestLog.ttfb_ms).label("avg_ttfb_ms"),
                func.avg(RequestLog.duration_ms).label("avg_duration_ms"),
                func.sum(case((RequestLog.is_stream.is_(True), 1), else_=0)).label("stream_requests"),
                func.sum(case((RequestLog.has_image.is_(True), 1), else_=0)).label("image_requests"),
                func.count(func.distinct(RequestLog.user_account_id)).label("unique_users"),
                func.sum(RequestLog.prompt_tokens).label("prompt_tokens"),
                func.sum(RequestLog.completion_tokens).label("completion_tokens"),
                func.sum(RequestLog.total_tokens).label("total_tokens"),
                func.sum(RequestLog.total_cost).label("total_cost"),
            )
            .where(
                RequestLog.created_at >= since,
                LogService._route_traffic_expr(),
            )
            .group_by(RequestLog.provider_id, RequestLog.provider_name, RequestLog.requested_model)
            .order_by(func.count(RequestLog.id).desc())
            .limit(LogService.METRIC_GROUP_LIMIT)
        )
        stmt = LogService._apply_metric_scope(
            stmt,
            user_account_id=user_account_id,
            api_client_key_ids=api_client_key_ids,
        )
        rows = db.execute(stmt)
        metric_samples = LogService._load_route_metric_samples(
            db,
            since=since,
            user_account_id=user_account_id,
            api_client_key_ids=api_client_key_ids,
        )
        sample_groups: dict[tuple[int | None, str | None, str | None], list] = {}
        for sample in metric_samples:
            key = (sample.provider_id, sample.provider_name, sample.requested_model)
            sample_groups.setdefault(key, []).append(sample)
        for row in rows:
            total_requests = int(row.total_requests or 0)
            success_requests = int(row.success_requests or 0)
            failed_requests = total_requests - success_requests
            sample_logs = sample_groups.get((row.provider_id, row.provider_name, row.requested_model), [])
            latency_values = LogService._metric_values(sample_logs, "latency_ms")
            ttfb_values = LogService._metric_values(sample_logs, "ttfb_ms")
            content_guard_latency_values = LogService._metric_values(sample_logs, "content_guard_latency_ms")
            results.append(
                {
                    "provider_id": row.provider_id,
                    "provider_name": row.provider_name,
                    "requested_model": row.requested_model,
                    "total_requests": total_requests,
                    "success_requests": success_requests,
                    "failed_requests": failed_requests,
                    "failure_rate": round((failed_requests / total_requests) * 100, 2) if total_requests else 0.0,
                    "avg_latency_ms": LogService._round_float(row.avg_latency_ms),
                    "avg_ttfb_ms": LogService._round_float(row.avg_ttfb_ms),
                    "avg_duration_ms": LogService._round_float(row.avg_duration_ms),
                    "p50_latency_ms": LogService._percentile(latency_values, 50),
                    "p95_latency_ms": LogService._percentile(latency_values, 95),
                    "p99_latency_ms": LogService._percentile(latency_values, 99),
                    "p50_ttfb_ms": LogService._percentile(ttfb_values, 50),
                    "p95_ttfb_ms": LogService._percentile(ttfb_values, 95),
                    "p99_ttfb_ms": LogService._percentile(ttfb_values, 99),
                    "content_guard_p50_latency_ms": LogService._percentile(content_guard_latency_values, 50),
                    "content_guard_p95_latency_ms": LogService._percentile(content_guard_latency_values, 95),
                    "content_guard_p99_latency_ms": LogService._percentile(content_guard_latency_values, 99),
                    "qps": round(total_requests / window_seconds, 4),
                    "peak_active_requests": LogService._compute_peak_active_requests(sample_logs),
                    "stream_requests": int(row.stream_requests or 0),
                    "image_requests": int(row.image_requests or 0),
                    "unique_users": int(row.unique_users or 0),
                    "prompt_tokens": int(row.prompt_tokens or 0),
                    "completion_tokens": int(row.completion_tokens or 0),
                    "total_tokens": int(row.total_tokens or 0),
                    "total_cost": round(float(row.total_cost or 0), 6),
                }
            )
        return CacheService.set(cache_key, results, ttl_seconds=5)

    @staticmethod
    def route_metric_summary(db: Session, *, window_minutes: int, requested_model: str | None = None) -> dict[tuple[int | None, str | None], dict]:
        metric_model_expr = func.coalesce(RequestLog.model_name, RequestLog.requested_model)
        cache_key = f"route-metrics:{int(window_minutes)}:{requested_model or '*'}:actual-model"
        cached = CacheService.get(cache_key)
        if isinstance(cached, list):
            return {
                (item.get("provider_id"), item.get("model_name")): {
                    "total_requests": int(item.get("total_requests") or 0),
                    "failed_requests": int(item.get("failed_requests") or 0),
                    "failure_rate": float(item.get("failure_rate") or 0.0),
                    "success_rate": float(item.get("success_rate") if item.get("success_rate") is not None else 1.0),
                    "avg_latency_ms": item.get("avg_latency_ms"),
                }
                for item in cached
                if isinstance(item, dict)
            }
        since = now_beijing() - timedelta(minutes=window_minutes)
        stmt = (
            select(
                RequestLog.provider_id,
                metric_model_expr.label("metric_model_name"),
                func.count(RequestLog.id).label("total_requests"),
                func.sum(case((RequestLog.success.is_(False), 1), else_=0)).label("failed_requests"),
                func.avg(RequestLog.latency_ms).label("avg_latency_ms"),
            )
            .where(
                RequestLog.created_at >= since,
                LogService._route_traffic_expr(),
            )
        )
        if requested_model:
            stmt = stmt.where(metric_model_expr == requested_model)
        stmt = (
            stmt.group_by(RequestLog.provider_id, metric_model_expr)
            .order_by(func.count(RequestLog.id).desc())
            .limit(LogService.METRIC_GROUP_LIMIT)
        )

        summary: dict[tuple[int | None, str | None], dict] = {}
        for row in db.execute(stmt):
            total_requests = int(row.total_requests or 0)
            failed_requests = int(row.failed_requests or 0)
            summary[(row.provider_id, row.metric_model_name)] = {
                "total_requests": total_requests,
                "failed_requests": failed_requests,
                "failure_rate": (failed_requests / total_requests) if total_requests else 0.0,
                "success_rate": ((total_requests - failed_requests) / total_requests) if total_requests else 1.0,
                "avg_latency_ms": float(row.avg_latency_ms) if row.avg_latency_ms is not None else None,
            }
        CacheService.set(
            cache_key,
            [
                {
                    "provider_id": provider_id,
                    "model_name": model_name,
                    **payload,
                }
                for (provider_id, model_name), payload in summary.items()
            ],
            ttl_seconds=2,
        )
        return summary

    @staticmethod
    def _recent_runtime_second_bucket(value: datetime | None = None) -> int:
        return int((value or now_beijing()).timestamp())

    @staticmethod
    def _recent_runtime_cache_key(second_bucket: int, provider_id: int, provider_model_id: int) -> str:
        return f"{LogService.RECENT_RUNTIME_CACHE_PREFIX}:{second_bucket}:{provider_id}:{provider_model_id}"

    @staticmethod
    def _recent_runtime_member(provider_id: int, provider_model_id: int) -> str:
        return f"{provider_id}:{provider_model_id}"

    @staticmethod
    def _parse_recent_runtime_member(value: Any) -> tuple[int, int] | None:
        try:
            provider_text, provider_model_text = str(value).split(":", 1)
            return int(provider_text), int(provider_model_text)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _cache_recent_runtime_log(log: RequestLog) -> None:
        if (
            log.provider_id is None
            or log.resolved_provider_model_id is None
            or log.log_type not in LogService.ROUTE_TRAFFIC_LOG_TYPES
        ):
            return
        try:
            provider_id = int(log.provider_id)
            provider_model_id = int(log.resolved_provider_model_id)
            second_bucket = LogService._recent_runtime_second_bucket(log.created_at)
            key = LogService._recent_runtime_cache_key(second_bucket, provider_id, provider_model_id)
            member = LogService._recent_runtime_member(provider_id, provider_model_id)
            classification = LogService._classify_runtime_log_failure(
                success=bool(log.success),
                status_code=log.status_code,
                error_code=log.error_code,
                message=log.message,
                content_guard_risk_level=log.content_guard_risk_level,
            )
            client = RedisService.get_sync_client()
            pipe = client.pipeline()
            pipe.zadd(LogService.RECENT_RUNTIME_ACTIVE_CACHE_KEY, {member: second_bucket})
            pipe.expire(LogService.RECENT_RUNTIME_ACTIVE_CACHE_KEY, LogService.RECENT_RUNTIME_CACHE_TTL_SECONDS)
            pipe.hincrby(key, "total_requests", 1)
            if log.success:
                pipe.hincrby(key, "success_requests", 1)
            else:
                pipe.hincrby(key, "failed_requests", 1)
                pipe.hincrby(key, classification["counter_field"], 1)
                if classification.get("category"):
                    pipe.hset(key, "latest_error_category", classification["category"])
                if classification.get("code"):
                    pipe.hset(key, "latest_error_code", classification["code"])
                if log.status_code is not None:
                    pipe.hset(key, "latest_status_code", int(log.status_code))
                if log.trace_id:
                    pipe.hset(key, "latest_trace_id", log.trace_id)
                if log.message:
                    pipe.hset(key, "latest_error_message", str(log.message)[:500])
                pipe.hset(key, "latest_error_at", log.created_at.isoformat() if log.created_at else now_beijing().isoformat())
            if log.model_name:
                pipe.hset(key, "model_name", log.model_name)
            if log.requested_model:
                pipe.hset(key, "requested_model", log.requested_model)
            pipe.hset(key, "latest_log_at", log.created_at.isoformat() if log.created_at else now_beijing().isoformat())
            if log.latency_ms is not None:
                pipe.hincrbyfloat(key, "latency_sum", float(max(0, int(log.latency_ms))))
                pipe.hincrby(key, "latency_count", 1)
            if log.ttfb_ms is not None:
                pipe.hincrbyfloat(key, "ttfb_sum", float(max(0, int(log.ttfb_ms))))
                pipe.hincrby(key, "ttfb_count", 1)
            pipe.expire(key, LogService.RECENT_RUNTIME_CACHE_TTL_SECONDS)
            pipe.execute()
        except Exception:
            return

    @staticmethod
    def _classify_runtime_log_failure(
        *,
        success: bool,
        status_code: int | None,
        error_code: str | None,
        message: str | None,
        content_guard_risk_level: str | None,
    ) -> dict[str, str]:
        if success:
            return {"counter_field": "success_requests", "category": "", "code": ""}
        normalized_code = str(error_code or "")
        status = int(status_code or 0) or 502
        classified = OpenAIErrorService.classify_error(
            status_code=status,
            detail={"code": normalized_code, "message": message or ""},
        )
        category = str(classified.get("category") or "")
        code = str(classified.get("code") or normalized_code or "")
        if code == "content_integrity_violation" or category == "content_integrity":
            if str(content_guard_risk_level or "").lower() == "high" or code == "content_integrity_violation":
                return {"counter_field": "content_integrity_high_risk_count", "category": category, "code": code}
            return {"counter_field": "ignored_failure_requests", "category": category, "code": code}
        if category in LogService.RECENT_RUNTIME_IGNORED_ERROR_CATEGORIES:
            return {"counter_field": "ignored_failure_requests", "category": category, "code": code}
        if category in LogService.RECENT_RUNTIME_HEALTH_FAILURE_CATEGORIES or bool(classified.get("recoverable")):
            field = {
                "timeout": "timeout_count",
                "network": "network_count",
                "rate_limit": "rate_limit_count",
                "server_error": "server_error_count",
                "invalid_response": "invalid_response_count",
            }.get(category, "upstream_transient_count")
            return {"counter_field": field, "category": category, "code": code}
        return {"counter_field": "ignored_failure_requests", "category": category, "code": code}

    @staticmethod
    def _recent_runtime_metrics_from_cache(
        *,
        window_seconds: int,
        provider_id: int | None = None,
        provider_model_id: int | None = None,
    ) -> dict[tuple[int, int], dict[str, Any]] | None:
        try:
            client = RedisService.get_sync_client()
            now_bucket = LogService._recent_runtime_second_bucket()
            window = max(1, min(int(window_seconds or 5), 30))
            if provider_id is not None and provider_model_id is not None:
                members = [LogService._recent_runtime_member(int(provider_id), int(provider_model_id))]
            else:
                raw_members = client.zrangebyscore(
                    LogService.RECENT_RUNTIME_ACTIVE_CACHE_KEY,
                    now_bucket - window - 1,
                    now_bucket,
                )
                members = [str(item) for item in raw_members]
            parsed_members = [LogService._parse_recent_runtime_member(member) for member in members]
            parsed_keys = [item for item in parsed_members if item is not None]
            if not parsed_keys:
                return {}
            pipe = client.pipeline()
            bucket_range = range(now_bucket - window + 1, now_bucket + 1)
            lookup_order: list[tuple[int, int, int]] = []
            for item_provider_id, item_provider_model_id in parsed_keys:
                for second_bucket in bucket_range:
                    lookup_order.append((second_bucket, item_provider_id, item_provider_model_id))
                    pipe.hgetall(LogService._recent_runtime_cache_key(second_bucket, item_provider_id, item_provider_model_id))
            values = pipe.execute()
        except Exception:
            return None
        results: dict[tuple[int, int], dict[str, Any]] = {}
        for (second_bucket, item_provider_id, item_provider_model_id), payload in zip(lookup_order, values, strict=False):
            if not isinstance(payload, dict) or not payload:
                continue
            key = (item_provider_id, item_provider_model_id)
            item = results.setdefault(
                key,
                LogService._empty_recent_runtime_metrics(
                    provider_id=item_provider_id,
                    provider_model_id=item_provider_model_id,
                    window_seconds=window,
                ),
            )
            LogService._merge_recent_runtime_cache_bucket(item, payload)
        for item in results.values():
            LogService._finalize_recent_runtime_metric(item)
        return results

    @staticmethod
    def _merge_recent_runtime_cache_bucket(item: dict[str, Any], payload: dict[str, Any]) -> None:
        int_fields = (
            "total_requests",
            "success_requests",
            "failed_requests",
            "ignored_failure_requests",
            "timeout_count",
            "network_count",
            "rate_limit_count",
            "server_error_count",
            "upstream_transient_count",
            "invalid_response_count",
            "content_integrity_high_risk_count",
        )
        for field in int_fields:
            item[field] = int(item.get(field) or 0) + int(payload.get(field) or 0)
        item["upstream_failure_requests"] = (
            int(item.get("timeout_count") or 0)
            + int(item.get("network_count") or 0)
            + int(item.get("rate_limit_count") or 0)
            + int(item.get("server_error_count") or 0)
            + int(item.get("upstream_transient_count") or 0)
            + int(item.get("invalid_response_count") or 0)
        )
        item["model_name"] = payload.get("model_name") or item.get("model_name")
        item["requested_model"] = payload.get("requested_model") or item.get("requested_model")
        if payload.get("latest_log_at"):
            item["latest_log_at"] = payload.get("latest_log_at")
        if payload.get("latest_error_code"):
            item["latest_error_code"] = payload.get("latest_error_code")
            item["latest_error_category"] = payload.get("latest_error_category")
            item["latest_status_code"] = int(payload.get("latest_status_code") or 0) or None
            item["latest_trace_id"] = payload.get("latest_trace_id")
            item["latest_error_message"] = payload.get("latest_error_message")
            item["latest_error_at"] = payload.get("latest_error_at") or item.get("latest_error_at")
        latency_count = int(payload.get("latency_count") or 0)
        if latency_count > 0:
            item.setdefault("_latency_sum", 0.0)
            item.setdefault("_latency_count", 0)
            item["_latency_sum"] += float(payload.get("latency_sum") or 0.0)
            item["_latency_count"] += latency_count
        ttfb_count = int(payload.get("ttfb_count") or 0)
        if ttfb_count > 0:
            item.setdefault("_ttfb_sum", 0.0)
            item.setdefault("_ttfb_count", 0)
            item["_ttfb_sum"] += float(payload.get("ttfb_sum") or 0.0)
            item["_ttfb_count"] += ttfb_count

    @staticmethod
    def provider_model_recent_runtime_metrics(
        db: Session,
        *,
        provider_id: int,
        provider_model_id: int,
        window_seconds: int = 5,
        max_rows: int | None = None,
    ) -> dict[str, Any]:
        """返回指定提供商模型 ID 最近短窗口的正式路由运行指标。

        注意：provider_model_id 是唯一模型挂载 ID；model_name 只是自定义展示/请求名，不用于唯一定位。
        """
        metrics = LogService.provider_model_recent_runtime_metrics_batch(
            db,
            window_seconds=window_seconds,
            max_rows=max_rows,
            provider_id=provider_id,
            provider_model_id=provider_model_id,
        )
        return metrics.get(
            (provider_id, provider_model_id),
            LogService._empty_recent_runtime_metrics(
                provider_id=provider_id,
                provider_model_id=provider_model_id,
                window_seconds=window_seconds,
            ),
        )

    @staticmethod
    def provider_model_recent_runtime_metrics_batch(
        db: Session,
        *,
        window_seconds: int = 5,
        max_rows: int | None = None,
        provider_id: int | None = None,
        provider_model_id: int | None = None,
    ) -> dict[tuple[int, int], dict[str, Any]]:
        """批量返回最近短窗口内活跃 provider_model 的运行指标。

        为避免定时任务消耗过多资源，本函数优先读取 Redis 秒级聚合缓存；缓存不可用时只读取轻量列。
        max_rows 为正数时限制数据库兜底扫描行数；max_rows <= 0 表示不截断最近短窗口。
        """
        parsed_window_seconds = max(1, min(int(window_seconds or 5), 30))
        if max_rows is None:
            parsed_max_rows: int | None = LogService.RECENT_RUNTIME_METRIC_MAX_ROWS
        else:
            raw_max_rows = int(max_rows)
            parsed_max_rows = None if raw_max_rows <= 0 else max(1, min(raw_max_rows, 20000))
        cached = LogService._recent_runtime_metrics_from_cache(
            window_seconds=parsed_window_seconds,
            provider_id=provider_id,
            provider_model_id=provider_model_id,
        )
        if cached is not None:
            if provider_id is None and provider_model_id is None:
                return cached
            requested_key = (
                int(provider_id) if provider_id is not None else None,
                int(provider_model_id) if provider_model_id is not None else None,
            )
            if None not in requested_key and requested_key in cached:
                return cached
            if None in requested_key:
                return cached
        since = now_beijing() - timedelta(seconds=parsed_window_seconds)
        stmt = (
            select(
                RequestLog.provider_id,
                RequestLog.resolved_provider_model_id,
                RequestLog.model_name,
                RequestLog.requested_model,
                RequestLog.success,
                RequestLog.status_code,
                RequestLog.latency_ms,
                RequestLog.ttfb_ms,
                RequestLog.duration_ms,
                RequestLog.error_code,
                RequestLog.message,
                RequestLog.trace_id,
                RequestLog.created_at,
                RequestLog.content_guard_result,
                RequestLog.content_guard_risk_level,
            )
            .where(
                RequestLog.created_at >= since,
                RequestLog.provider_id.is_not(None),
                RequestLog.resolved_provider_model_id.is_not(None),
                LogService._route_traffic_expr(),
            )
            .order_by(RequestLog.created_at.desc(), RequestLog.id.desc())
        )
        if parsed_max_rows is not None:
            stmt = stmt.limit(parsed_max_rows)
        if provider_id is not None:
            stmt = stmt.where(RequestLog.provider_id == provider_id)
        if provider_model_id is not None:
            stmt = stmt.where(RequestLog.resolved_provider_model_id == provider_model_id)

        grouped: dict[tuple[int, int], dict[str, Any]] = {}
        for row in db.execute(stmt):
            if row.provider_id is None or row.resolved_provider_model_id is None:
                continue
            key = (int(row.provider_id), int(row.resolved_provider_model_id))
            item = grouped.setdefault(
                key,
                LogService._empty_recent_runtime_metrics(
                    provider_id=key[0],
                    provider_model_id=key[1],
                    window_seconds=parsed_window_seconds,
                ),
            )
            LogService._accumulate_recent_runtime_metric(item, row)
        for item in grouped.values():
            LogService._finalize_recent_runtime_metric(item)
        return grouped

    @staticmethod
    def _empty_recent_runtime_metrics(*, provider_id: int, provider_model_id: int, window_seconds: int) -> dict[str, Any]:
        return {
            "provider_id": provider_id,
            "provider_model_id": provider_model_id,
            "model_name": None,
            "requested_model": None,
            "window_seconds": max(1, min(int(window_seconds or 5), 30)),
            "total_requests": 0,
            "success_requests": 0,
            "failed_requests": 0,
            "upstream_failure_requests": 0,
            "ignored_failure_requests": 0,
            "success_rate": 1.0,
            "upstream_failure_rate": 0.0,
            "avg_latency_ms": None,
            "p95_latency_ms": None,
            "avg_ttfb_ms": None,
            "p95_ttfb_ms": None,
            "timeout_count": 0,
            "network_count": 0,
            "rate_limit_count": 0,
            "server_error_count": 0,
            "upstream_transient_count": 0,
            "invalid_response_count": 0,
            "content_integrity_high_risk_count": 0,
            "latest_error_code": None,
            "latest_error_category": None,
            "latest_status_code": None,
            "latest_trace_id": None,
            "latest_error_message": None,
            "latest_error_at": None,
            "latest_log_at": None,
            "decision": "no_data",
            "confidence": "none",
            "_latency_values": [],
            "_ttfb_values": [],
            "_latency_sum": 0.0,
            "_latency_count": 0,
            "_ttfb_sum": 0.0,
            "_ttfb_count": 0,
        }

    @staticmethod
    def _accumulate_recent_runtime_metric(item: dict[str, Any], row: Any) -> None:
        item["total_requests"] += 1
        item["model_name"] = item.get("model_name") or row.model_name
        item["requested_model"] = item.get("requested_model") or row.requested_model
        item["latest_log_at"] = row.created_at.isoformat() if row.created_at else item.get("latest_log_at")
        if row.latency_ms is not None:
            item["_latency_values"].append(max(0, int(row.latency_ms)))
        if row.ttfb_ms is not None:
            item["_ttfb_values"].append(max(0, int(row.ttfb_ms)))
        if bool(row.success):
            item["success_requests"] += 1
            return
        item["failed_requests"] += 1
        status_code = int(row.status_code or 0) or 502
        detail = {"code": row.error_code, "message": row.message or ""}
        classified = OpenAIErrorService.classify_error(status_code=status_code, detail=detail)
        category = str(classified.get("category") or "")
        code = str(classified.get("code") or row.error_code or "")
        if not item.get("latest_error_code"):
            item["latest_error_code"] = code or None
            item["latest_error_category"] = category or None
            item["latest_status_code"] = status_code
            item["latest_trace_id"] = row.trace_id
            item["latest_error_message"] = str(row.message or "")[:500] or None
            item["latest_error_at"] = row.created_at.isoformat() if row.created_at else None
        if code == "content_integrity_violation" or category == "content_integrity":
            if str(row.content_guard_risk_level or "").lower() == "high" or code == "content_integrity_violation":
                item["content_integrity_high_risk_count"] += 1
            item["ignored_failure_requests"] += 1
            return
        if category in LogService.RECENT_RUNTIME_IGNORED_ERROR_CATEGORIES:
            item["ignored_failure_requests"] += 1
            return
        if category in LogService.RECENT_RUNTIME_HEALTH_FAILURE_CATEGORIES or bool(classified.get("recoverable")):
            item["upstream_failure_requests"] += 1
            if category == "timeout":
                item["timeout_count"] += 1
            elif category == "network":
                item["network_count"] += 1
            elif category == "rate_limit":
                item["rate_limit_count"] += 1
            elif category == "server_error":
                item["server_error_count"] += 1
            elif category == "invalid_response":
                item["invalid_response_count"] += 1
            else:
                item["upstream_transient_count"] += 1
            return
        item["ignored_failure_requests"] += 1

    @staticmethod
    def _finalize_recent_runtime_metric(item: dict[str, Any]) -> None:
        total_requests = int(item.get("total_requests") or 0)
        success_requests = int(item.get("success_requests") or 0)
        upstream_failure_requests = int(item.get("upstream_failure_requests") or 0)
        latency_values = item.pop("_latency_values", [])
        ttfb_values = item.pop("_ttfb_values", [])
        latency_sum = float(item.pop("_latency_sum", 0.0) or 0.0)
        latency_count = int(item.pop("_latency_count", 0) or 0)
        ttfb_sum = float(item.pop("_ttfb_sum", 0.0) or 0.0)
        ttfb_count = int(item.pop("_ttfb_count", 0) or 0)
        if latency_values:
            latency_sum += float(sum(latency_values))
            latency_count += len(latency_values)
        if ttfb_values:
            ttfb_sum += float(sum(ttfb_values))
            ttfb_count += len(ttfb_values)
        item["success_rate"] = round(success_requests / total_requests, 6) if total_requests else 1.0
        item["upstream_failure_rate"] = round(upstream_failure_requests / total_requests, 6) if total_requests else 0.0
        item["avg_latency_ms"] = round(latency_sum / latency_count, 2) if latency_count else None
        item["p95_latency_ms"] = LogService._percentile(latency_values, 95) if latency_values else None
        item["avg_ttfb_ms"] = round(ttfb_sum / ttfb_count, 2) if ttfb_count else None
        item["p95_ttfb_ms"] = LogService._percentile(ttfb_values, 95) if ttfb_values else None
        if total_requests <= 0:
            item["decision"] = "no_data"
            item["confidence"] = "none"
        elif upstream_failure_requests <= 0:
            item["decision"] = "healthy_signal"
            item["confidence"] = "medium" if total_requests >= LogService.RECENT_RUNTIME_MIN_SAMPLE_FOR_ABNORMAL else "low"
        elif total_requests < LogService.RECENT_RUNTIME_MIN_SAMPLE_FOR_ABNORMAL:
            item["decision"] = "observe_only"
            item["confidence"] = "low"
        else:
            item["decision"] = "probe_required"
            item["confidence"] = "high" if upstream_failure_requests >= 2 else "medium"

    @staticmethod
    def export_logs_csv(
        db: Session,
        *,
        log_type: str | None,
        log_types: list[str] | None,
        provider_id: int | None,
        model_name: str | None,
        model_query: str | None,
        conversation_key: str | None,
        api_client_key_id: int | None,
        api_client_key_query: str | None,
        user_account_id: int | None,
        user_account_query: str | None,
        tenant_name: str | None,
        project_name: str | None,
        app_name: str | None,
        environment_name: str | None,
        success: bool | None,
        exclude_health_checks: bool,
        provider_trust_level: str | None = None,
        content_guard_result: str | None = None,
        content_guard_risk_level: str | None = None,
        content_guard_action: str | None = None,
        content_guard_final_strategy: str | None = None,
        content_guard_retry_count: int | None = None,
        content_guard_guard_stage: str | None = None,
        content_guard_category: str | None = None,
        content_guard_switched_provider: bool | None = None,
        content_guard_adaptation_skipped: bool | None = None,
        api_client_key_ids: list[int] | None = None,
        limit: int = 5000,
    ) -> str:
        stmt = select(RequestLog).options(*LogService._lightweight_log_load_options())
        stmt = LogService._apply_log_filters(
            stmt,
            log_type=log_type,
            log_types=log_types,
            provider_id=provider_id,
            provider_trust_level=provider_trust_level,
            model_name=model_name,
            model_query=model_query,
            conversation_key=conversation_key,
            api_client_key_id=api_client_key_id,
            api_client_key_query=api_client_key_query,
            user_account_id=user_account_id,
            user_account_query=user_account_query,
            tenant_name=tenant_name,
            project_name=project_name,
            app_name=app_name,
            environment_name=environment_name,
            success=success,
            exclude_health_checks=exclude_health_checks,
            content_guard_result=content_guard_result,
            content_guard_risk_level=content_guard_risk_level,
            content_guard_action=content_guard_action,
            content_guard_final_strategy=content_guard_final_strategy,
            content_guard_retry_count=content_guard_retry_count,
            content_guard_guard_stage=content_guard_guard_stage,
            content_guard_category=content_guard_category,
            content_guard_switched_provider=content_guard_switched_provider,
            content_guard_adaptation_skipped=content_guard_adaptation_skipped,
            api_client_key_ids=api_client_key_ids,
        )
        rows = list(
            db.scalars(
                stmt.order_by(RequestLog.created_at.desc(), RequestLog.id.desc()).limit(max(1, min(limit, LogService.EXPORT_LIMIT)))
            )
        )
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([
            "创建时间",
            "日志类型",
            "链路 ID",
            "请求 ID",
            "会话 ID",
            "会话键",
            "展示模型",
            "请求模型",
            "实际模型",
            "提供商",
            "租户",
            "项目",
            "应用",
            "环境",
            "来源 IP",
            "HTTP 方法",
            "是否成功",
            "状态码",
            "错误类型",
            "错误码",
            "是否可重试",
            "是否流式",
            "是否含图片",
            "请求模态",
            "是否生图",
            "生成图片数",
            "生成图片 MIME",
            "生成图片近似字节",
            "是否有局部生图",
            "生图结果是否截断",
            "图片响应模式",
            "上游用量是否缺失",
            "内容检测结果",
            "内容风险等级",
            "内容处置动作",
            "内容命中分类",
            "内容最终策略",
            "内容重试提供商数",
            "内容检测耗时 ms",
            "内容缓冲等待 ms",
            "内容置信度",
            "内容评分变化",
            "内容原因",
            "内容摘录",
            "延迟 ms",
            "首包 ms",
            "总耗时 ms",
            "输出速率",
            "输入 Token",
            "输出 Token",
            "总 Token",
            "缓存读取 Token",
            "缓存写入 Token",
            "推理 Token",
            "输入音频 Token",
            "输出音频 Token",
            "接受预测 Token",
            "拒绝预测 Token",
            "Token 来源",
            "计费倍率",
            "缓存写入价格",
            "价格档位",
            "总费用",
            "计费公式",
            "思维等级",
            "模型思维参数",
            "API Key 名称",
            "用户账号",
            "消息",
        ])
        for item in rows:
            serialized = LogService.serialize_log(
                item,
                include_payload_fields=False,
                derive_image_observability=False,
            )
            writer.writerow([
                item.created_at.isoformat() if item.created_at else "",
                LogService.format_csv_label(item.log_type),
                item.trace_id or "",
                item.request_id or "",
                item.session_id or "",
                item.conversation_key or "",
                serialized.get("display_model") or "",
                item.requested_model or "",
                item.model_name or "",
                item.provider_name or "",
                item.tenant_name or "",
                item.project_name or "",
                item.app_name or "",
                item.environment_name or "",
                item.source_ip or "",
                item.http_method or "",
                LogService.format_bool_display(item.success),
                item.status_code if item.status_code is not None else "",
                item.error_type or "",
                item.error_code or "",
                LogService.format_bool_display(item.retryable),
                LogService.format_bool_display(item.is_stream),
                LogService.format_bool_display(item.has_image),
                serialized.get("request_modality") or "",
                LogService.format_bool_display(serialized.get("uses_image_generation")),
                serialized.get("generated_images_count") if serialized.get("generated_images_count") is not None else "",
                ",".join(serialized.get("generated_image_mime_types") or []),
                serialized.get("generated_image_approx_bytes") if serialized.get("generated_image_approx_bytes") is not None else "",
                LogService.format_bool_display(serialized.get("has_partial_generated_image")),
                LogService.format_bool_display(serialized.get("generated_image_result_truncated")),
                serialized.get("image_response_mode") or "",
                LogService.format_bool_display(serialized.get("upstream_usage_missing")),
                LogService.format_csv_label(item.content_guard_result),
                LogService.format_csv_label(item.content_guard_risk_level),
                LogService.format_csv_label(item.content_guard_action),
                item.content_guard_categories_json or "",
                LogService.format_csv_label(item.content_guard_final_strategy),
                item.content_guard_retry_provider_count if item.content_guard_retry_provider_count is not None else "",
                item.content_guard_latency_ms if item.content_guard_latency_ms is not None else "",
                item.content_guard_buffer_wait_ms if item.content_guard_buffer_wait_ms is not None else "",
                item.content_guard_confidence if item.content_guard_confidence is not None else "",
                item.content_guard_score_delta if item.content_guard_score_delta is not None else "",
                item.content_guard_reason or "",
                item.content_guard_excerpt or "",
                item.latency_ms if item.latency_ms is not None else "",
                item.ttfb_ms if item.ttfb_ms is not None else "",
                item.duration_ms if item.duration_ms is not None else "",
                item.tps if item.tps is not None else "",
                LogService.format_token_display(item.prompt_tokens),
                LogService.format_token_display(item.completion_tokens),
                LogService.format_token_display(item.total_tokens),
                LogService.format_token_display(item.cache_read_tokens),
                LogService.format_token_display(item.cache_write_tokens),
                LogService.format_token_display(item.reasoning_tokens),
                LogService.format_token_display(item.prompt_audio_tokens),
                LogService.format_token_display(item.completion_audio_tokens),
                LogService.format_token_display(item.accepted_prediction_tokens),
                LogService.format_token_display(item.rejected_prediction_tokens),
                item.token_source or "",
                item.billing_multiplier if item.billing_multiplier is not None else "",
                LogService.format_price_display(item.channel_price_cache_write_per_1k),
                item.pricing_tier_name or "",
                LogService.format_money_display(item.total_cost),
                LogService.format_billing_calculation(item),
                item.reasoning_level or "",
                item.model_reasoning_effort or "",
                item.api_client_key_name or "",
                item.user_account_name or "",
                item.message or "",
            ])
        return "\ufeff" + buffer.getvalue()

    @staticmethod
    def format_billing_calculation(item: RequestLog) -> str:
        multiplier = item.billing_multiplier if item.billing_multiplier is not None else 1
        input_price = item.channel_price_input_per_1k
        output_price = item.channel_price_output_per_1k
        cache_price = item.channel_price_cache_per_1k if item.channel_price_cache_per_1k is not None else input_price
        cache_write_price = item.channel_price_cache_write_per_1k if item.channel_price_cache_write_per_1k is not None else input_price
        cache_read_tokens = int(item.cache_read_tokens or 0)
        cache_write_tokens = int(item.cache_write_tokens or 0)
        regular_input_tokens = max(0, int(item.prompt_tokens or 0) - cache_read_tokens - cache_write_tokens)
        input_part = (
            "输入单价未设置"
            if input_price is None
            else f"输入 {LogService.format_token_display(regular_input_tokens)} × {LogService.format_price_display(input_price)}"
        )
        cache_part = (
            None
            if cache_read_tokens <= 0
            else (
                "缓存单价未设置"
                if cache_price is None
                else f"缓存 {LogService.format_token_display(cache_read_tokens)} × {LogService.format_price_display(cache_price)}"
            )
        )
        cache_write_part = (
            None
            if cache_write_tokens <= 0
            else (
                "缓存写入单价未设置"
                if cache_write_price is None
                else f"缓存写 {LogService.format_token_display(cache_write_tokens)} × {LogService.format_price_display(cache_write_price)}"
            )
        )
        output_part = (
            "输出单价未设置"
            if output_price is None
            else f"输出 {LogService.format_token_display(item.completion_tokens or 0)} × {LogService.format_price_display(output_price)}"
        )
        parts = [input_part]
        if cache_part:
            parts.append(cache_part)
        if cache_write_part:
            parts.append(cache_write_part)
        parts.append(output_part)
        tier_text = f"档位 {item.pricing_tier_name}；" if item.pricing_tier_name else ""
        return f"{tier_text}倍率 {float(multiplier):.2f}x；" + " + ".join(parts)

    @staticmethod
    def metric_timeseries(
        db: Session,
        *,
        window_minutes: int,
        bucket_minutes: int,
        user_account_id: int | None = None,
        api_client_key_ids: list[int] | None = None,
    ) -> list[dict]:
        bucket_minutes = max(1, min(bucket_minutes, window_minutes))
        cache_key = LogService._metric_cache_key(
            "timeseries",
            window_minutes=window_minutes,
            bucket_minutes=bucket_minutes,
            user_account_id=user_account_id,
            api_client_key_ids=api_client_key_ids,
        )
        cached = CacheService.get(cache_key)
        if isinstance(cached, list):
            return cached
        since = now_beijing() - timedelta(minutes=window_minutes)
        stmt = (
            select(
                RequestLog.created_at,
                RequestLog.success,
                RequestLog.is_stream,
                RequestLog.has_image,
                RequestLog.latency_ms,
                RequestLog.ttfb_ms,
                RequestLog.total_tokens,
                RequestLog.total_cost,
                RequestLog.duration_ms,
            )
            .where(
                RequestLog.created_at >= since,
                LogService._route_traffic_expr(),
            )
            .order_by(RequestLog.created_at.desc(), RequestLog.id.desc())
            .limit(LogService.METRIC_ROW_SAMPLE_LIMIT)
        )
        stmt = LogService._apply_metric_scope(
            stmt,
            user_account_id=user_account_id,
            api_client_key_ids=api_client_key_ids,
        )
        rows = db.execute(stmt)
        buckets: dict[datetime, list] = {}
        for row in rows:
            if row.created_at is None:
                continue
            minute_floor = row.created_at.replace(second=0, microsecond=0)
            bucket_minute = minute_floor.minute - (minute_floor.minute % bucket_minutes)
            bucket_start = minute_floor.replace(minute=bucket_minute)
            buckets.setdefault(bucket_start, []).append(row)

        results = []
        bucket_window_seconds = max(1, bucket_minutes * 60)
        for bucket_start in sorted(buckets.keys()):
            bucket_logs = buckets[bucket_start]
            latency_values = LogService._metric_values(bucket_logs, "latency_ms")
            ttfb_values = LogService._metric_values(bucket_logs, "ttfb_ms")
            total_requests = len(bucket_logs)
            success_requests = sum(1 for item in bucket_logs if item.success)
            failed_requests = total_requests - success_requests
            results.append(
                {
                    "bucket_start": bucket_start,
                    "total_requests": total_requests,
                    "success_requests": success_requests,
                    "failed_requests": failed_requests,
                    "stream_requests": sum(1 for item in bucket_logs if item.is_stream),
                    "image_requests": sum(1 for item in bucket_logs if item.has_image),
                    "avg_latency_ms": LogService._average(latency_values),
                    "avg_ttfb_ms": LogService._average(ttfb_values),
                    "p50_latency_ms": LogService._percentile(latency_values, 50),
                    "p95_latency_ms": LogService._percentile(latency_values, 95),
                    "p99_latency_ms": LogService._percentile(latency_values, 99),
                    "qps": round(total_requests / bucket_window_seconds, 4),
                    "peak_active_requests": LogService._compute_peak_active_requests(bucket_logs),
                    "total_tokens": sum(int(item.total_tokens or 0) for item in bucket_logs),
                    "total_cost": round(sum(float(item.total_cost or 0) for item in bucket_logs), 6),
                }
            )
        return CacheService.set(cache_key, results, ttl_seconds=5)

    @staticmethod
    def _metric_cache_key(
        metric_name: str,
        *,
        window_minutes: int,
        bucket_minutes: int | None = None,
        user_account_id: int | None = None,
        api_client_key_ids: list[int] | None = None,
    ) -> str:
        sorted_key_ids = sorted(api_client_key_ids or [])
        if sorted_key_ids:
            key_material = ",".join(str(item) for item in sorted_key_ids)
            key_digest = hashlib.sha256(key_material.encode("utf-8")).hexdigest()[:16]
            api_key_scope = f"{len(sorted_key_ids)}:{key_digest}"
        else:
            api_key_scope = "*"
        return (
            f"metrics:{metric_name}:window={int(window_minutes)}:"
            f"bucket={int(bucket_minutes or 0)}:user={user_account_id or 0}:keys={api_key_scope}"
        )

    @staticmethod
    def metric_period_report(
        db: Session,
        *,
        window_days: int,
        period_type: str,
    ) -> list[dict]:
        normalized_period = (period_type or "").strip().lower()
        if normalized_period not in {"day", "week", "month"}:
            raise ValueError("period_type must be one of: day, week, month")
        since = now_beijing() - timedelta(days=window_days)
        bucket_expr = LogService._period_bucket_expr(db, normalized_period)
        rows = db.execute(
            select(
                bucket_expr.label("period_bucket"),
                func.min(RequestLog.created_at).label("period_start"),
                func.count(RequestLog.id).label("total_requests"),
                func.sum(case((RequestLog.success.is_(True), 1), else_=0)).label("success_requests"),
                func.sum(RequestLog.total_tokens).label("total_tokens"),
                func.sum(RequestLog.total_cost).label("total_cost"),
            )
            .where(
                RequestLog.created_at >= since,
                LogService._route_traffic_expr(),
            )
            .group_by(bucket_expr)
            .order_by(bucket_expr.asc())
        )

        items: list[dict] = []
        for row in rows:
            total_requests = int(row.total_requests or 0)
            success_requests = int(row.success_requests or 0)
            failed_requests = total_requests - success_requests
            period_start = LogService._normalize_period_row_start(row.period_start, normalized_period)
            items.append(
                {
                    "period_start": period_start,
                    "period_type": normalized_period,
                    "total_requests": total_requests,
                    "success_requests": success_requests,
                    "failed_requests": failed_requests,
                    "total_tokens": int(row.total_tokens or 0),
                    "total_cost": round(float(row.total_cost or 0), 6),
                }
            )
        return items

    @staticmethod
    def _load_route_metric_logs(db: Session, *, since: datetime) -> list[RequestLog]:
        return list(
            db.scalars(
                select(RequestLog)
                .where(
                    RequestLog.created_at >= since,
                    LogService._route_traffic_expr(),
                )
                .order_by(RequestLog.created_at.asc(), RequestLog.id.asc())
            )
        )

    @staticmethod
    def _load_route_metric_samples(
        db: Session,
        *,
        since: datetime,
        user_account_id: int | None = None,
        api_client_key_ids: list[int] | None = None,
    ) -> list:
        stmt = (
            select(
                RequestLog.provider_id,
                RequestLog.provider_name,
                RequestLog.requested_model,
                RequestLog.created_at,
                RequestLog.latency_ms,
                RequestLog.ttfb_ms,
                RequestLog.duration_ms,
                RequestLog.content_guard_latency_ms,
            )
            .where(
                RequestLog.created_at >= since,
                LogService._route_traffic_expr(),
            )
            .order_by(RequestLog.created_at.desc(), RequestLog.id.desc())
            .limit(LogService.METRIC_ROW_SAMPLE_LIMIT)
        )
        stmt = LogService._apply_metric_scope(
            stmt,
            user_account_id=user_account_id,
            api_client_key_ids=api_client_key_ids,
        )
        return list(db.execute(stmt))

    @staticmethod
    def _apply_metric_scope(
        stmt,
        *,
        user_account_id: int | None = None,
        api_client_key_ids: list[int] | None = None,
    ):
        if user_account_id is not None:
            stmt = stmt.where(RequestLog.user_account_id == user_account_id)
        if api_client_key_ids is not None:
            if not api_client_key_ids:
                stmt = stmt.where(RequestLog.api_client_key_id == -1)
            else:
                stmt = stmt.where(RequestLog.api_client_key_id.in_(api_client_key_ids))
        return stmt

    @staticmethod
    def _metric_values(logs: list, field_name: str) -> list[float]:
        values: list[float] = []
        for item in logs:
            value = getattr(item, field_name, None)
            if value is not None:
                values.append(float(value))
        return values

    @staticmethod
    def _round_float(value) -> float | None:
        if value is None:
            return None
        return round(float(value), 2)

    @staticmethod
    def _average(values: list[float]) -> float | None:
        if not values:
            return None
        return round(sum(values) / len(values), 2)

    @staticmethod
    def _percentile(values: list[float], percentile: int) -> float | None:
        if not values:
            return None
        ordered = sorted(values)
        if len(ordered) == 1:
            return round(ordered[0], 2)
        rank = max(0.0, min(1.0, percentile / 100)) * (len(ordered) - 1)
        lower = int(rank)
        upper = min(len(ordered) - 1, lower + 1)
        fraction = rank - lower
        value = ordered[lower] + (ordered[upper] - ordered[lower]) * fraction
        return round(value, 2)

    @staticmethod
    def _compute_peak_active_requests(logs: list[RequestLog]) -> int:
        if not logs:
            return 0
        events: list[tuple[datetime, int]] = []
        for item in logs:
            if item.created_at is None:
                continue
            duration_ms = max(1, int(item.duration_ms or item.latency_ms or 1))
            start_at = item.created_at
            end_at = start_at + timedelta(milliseconds=duration_ms)
            events.append((start_at, 1))
            events.append((end_at, -1))
        if not events:
            return 0
        current = 0
        peak = 0
        for _, delta in sorted(events, key=lambda item: (item[0], item[1])):
            current += delta
            if current > peak:
                peak = current
        if peak <= 0:
            return min(len(logs), RuntimeStateService.peak_active_requests())
        return peak

    @staticmethod
    def _period_start(value: datetime, period_type: str) -> datetime:
        if period_type == "day":
            return value.replace(hour=0, minute=0, second=0, microsecond=0)
        if period_type == "week":
            day_start = value.replace(hour=0, minute=0, second=0, microsecond=0)
            return day_start - timedelta(days=day_start.weekday())
        return value.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    @staticmethod
    def _period_bucket_expr(db: Session, period_type: str):
        return func.date_trunc(period_type, RequestLog.created_at)

    @staticmethod
    def _normalize_period_row_start(value, period_type: str) -> datetime:
        if isinstance(value, datetime):
            return LogService._period_start(value, period_type)
        if isinstance(value, str):
            try:
                if period_type == "day":
                    return datetime.strptime(value[:10], "%Y-%m-%d")
                if period_type == "month":
                    return datetime.strptime(value[:7], "%Y-%m")
            except ValueError:
                pass
        return LogService._period_start(now_beijing(), period_type)
