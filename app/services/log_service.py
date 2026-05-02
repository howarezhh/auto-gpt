import csv
import io
from datetime import datetime, timedelta

from sqlalchemy import case, delete, func, not_, or_, select
from sqlalchemy.orm import Session

from app.models.provider_model import ProviderModel
from app.models.request_log import RequestLog
from app.services.runtime_state_service import RuntimeStateService
from app.utils.json_utils import dumps_json, safeJsonParse


class LogService:
    HEALTH_CHECK_LOG_TYPES = ("health_check", "health_check_provider", "health_check_model")
    ROUTE_TRAFFIC_LOG_TYPES = ("chat", "responses")
    USER_VISIBLE_LOG_TYPES = ("chat", "responses")
    REASONING_LEVEL_NONE = "无"
    REASONING_LEVEL_VALUES = {REASONING_LEVEL_NONE, "low", "medium", "high", "xhigh"}
    METRIC_ROW_SAMPLE_LIMIT = 10000
    TOKEN_JOB_MAX_PAYLOAD_BYTES = 65536

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
        attempt_count: int | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
        cache_read_tokens: int | None = None,
        cache_write_tokens: int | None = None,
        finish_reason: str | None = None,
        upstream_request_id: str | None = None,
        request_body_json: str | None = None,
        response_body_json: str | None = None,
        response_text: str | None = None,
        message: str | None = None,
        error_type: str | None = None,
        error_code: str | None = None,
        retryable: bool | None = None,
        api_client_key_id: int | None = None,
        api_client_key_name: str | None = None,
        api_client_key_prefix: str | None = None,
        user_account_id: int | None = None,
        user_account_name: str | None = None,
        api_client_auth_result: str | None = None,
        api_client_remaining_tokens: int | None = None,
        api_client_remaining_requests_daily: int | None = None,
        api_client_remaining_cost_daily: float | None = None,
        api_client_policy_snapshot_json: str | None = None,
        billing_multiplier: float | None = None,
        channel_price_input_per_1k: float | None = None,
        channel_price_output_per_1k: float | None = None,
        channel_price_cache_per_1k: float | None = None,
        trace: list[dict] | dict | None = None,
        token_request_payload: dict | None = None,
        token_response_payload: dict | None = None,
        token_response_text: str | None = None,
        schedule_token_fill: bool = True,
        auto_commit: bool = True,
    ) -> RequestLog:
        provider_model = None
        if resolved_provider_model_id is not None and (
            billing_multiplier is None
            or channel_price_input_per_1k is None
            or channel_price_output_per_1k is None
            or channel_price_cache_per_1k is None
        ):
            provider_model = db.get(ProviderModel, resolved_provider_model_id)
        read_tokens, write_tokens = LogService.extract_cache_tokens(token_response_payload)
        normalized_reasoning_level = LogService.normalize_reasoning_level(reasoning_level)
        effective_ttfb_ms = LogService.resolve_ttfb_ms(
            first_token_latency_ms=first_token_latency_ms,
            ttfb_ms=ttfb_ms,
            latency_ms=latency_ms,
            is_stream=is_stream,
            success=success,
        )
        effective_duration_ms = LogService.resolve_duration_ms(latency_ms=latency_ms, duration_ms=duration_ms)
        effective_attempt_count = attempt_count if attempt_count is not None else LogService.derive_attempt_count(trace)
        effective_tps = tps if tps is not None else LogService.compute_tps(
            completion_tokens=completion_tokens,
            duration_ms=effective_duration_ms,
            ttfb_ms=effective_ttfb_ms,
        )
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
            attempt_count=effective_attempt_count,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cache_read_tokens=cache_read_tokens if cache_read_tokens is not None else read_tokens,
            cache_write_tokens=cache_write_tokens if cache_write_tokens is not None else write_tokens,
            finish_reason=finish_reason,
            upstream_request_id=upstream_request_id,
            request_body_json=request_body_json,
            response_body_json=response_body_json,
            response_text=response_text,
            message=message,
            error_type=error_type,
            error_code=error_code,
            retryable=retryable,
            api_client_key_id=api_client_key_id,
            api_client_key_name=api_client_key_name,
            api_client_key_prefix=api_client_key_prefix,
            user_account_id=user_account_id,
            user_account_name=user_account_name,
            api_client_auth_result=api_client_auth_result,
            api_client_remaining_tokens=api_client_remaining_tokens,
            api_client_remaining_requests_daily=api_client_remaining_requests_daily,
            api_client_remaining_cost_daily=api_client_remaining_cost_daily,
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
            trace_json=dumps_json(trace) if trace is not None else None,
        )
        if log_type in LogService.HEALTH_CHECK_LOG_TYPES:
            log.billing_status = "skipped"
            log.billing_finalized_at = datetime.utcnow()
            log.token_finalize_error = None
            log.billing_error = None
        LogService.refresh_derived_fields(log, response_payload=token_response_payload, trace=trace)
        db.add(log)
        db.flush()
        if auto_commit:
            db.commit()
        db.refresh(log)
        if (
            request_path
            and request_path != "/v1/models"
            and log_type not in LogService.HEALTH_CHECK_LOG_TYPES
        ):
            from app.services.token_usage_service import TokenUsageService
            safe_token_request_payload = LogService._token_job_payload_or_none(token_request_payload)

            TokenUsageService.enqueue_log_finalize(
                log_id=log.id,
                model_name=requested_model or model_name,
                request_path=request_path,
                request_payload=safe_token_request_payload,
                response_payload=token_response_payload,
                response_text=token_response_text,
                enable_usage_fill=schedule_token_fill and safe_token_request_payload is not None,
            )
        return log

    @staticmethod
    def _token_job_payload_or_none(payload: dict | None) -> dict | None:
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
    def list_logs(
        db: Session,
        *,
        page: int,
        page_size: int,
        log_type: str | None,
        log_types: list[str] | None,
        provider_id: int | None,
        model_name: str | None,
        conversation_key: str | None,
        api_client_key_id: int | None,
        api_client_key_query: str | None,
        user_account_id: int | None,
        success: bool | None,
        exclude_health_checks: bool = False,
        api_client_key_ids: list[int] | None = None,
    ) -> tuple[int, list[RequestLog], dict[str, int]]:
        stmt = select(RequestLog)
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
            model_name=model_name,
            conversation_key=conversation_key,
            api_client_key_id=api_client_key_id,
            api_client_key_query=api_client_key_query,
            user_account_id=user_account_id,
            success=success,
            exclude_health_checks=exclude_health_checks,
            api_client_key_ids=api_client_key_ids,
        )
        count_stmt = LogService._apply_log_filters(
            count_stmt,
            log_type=log_type,
            log_types=log_types,
            provider_id=provider_id,
            model_name=model_name,
            conversation_key=conversation_key,
            api_client_key_id=api_client_key_id,
            api_client_key_query=api_client_key_query,
            user_account_id=user_account_id,
            success=success,
            exclude_health_checks=exclude_health_checks,
            api_client_key_ids=api_client_key_ids,
        )
        summary_stmt = LogService._apply_log_filters(
            summary_stmt,
            log_type=log_type,
            log_types=log_types,
            provider_id=provider_id,
            model_name=model_name,
            conversation_key=conversation_key,
            api_client_key_id=api_client_key_id,
            api_client_key_query=api_client_key_query,
            user_account_id=user_account_id,
            success=success,
            exclude_health_checks=exclude_health_checks,
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
                stmt.order_by(RequestLog.created_at.desc()).offset((page - 1) * page_size).limit(page_size)
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
        conversation_key: str | None,
        api_client_key_id: int | None,
        api_client_key_query: str | None,
        user_account_id: int | None,
        success: bool | None,
        exclude_health_checks: bool,
        api_client_key_ids: list[int] | None = None,
    ):
        if exclude_health_checks:
            stmt = stmt.where(LogService._non_health_check_expr())
        if log_type:
            stmt = stmt.where(RequestLog.log_type == log_type)
        elif log_types:
            stmt = stmt.where(RequestLog.log_type.in_(log_types))
        if provider_id:
            stmt = stmt.where(RequestLog.provider_id == provider_id)
        if model_name:
            stmt = stmt.where(RequestLog.model_name == model_name)
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
                )
            )
        if success is not None:
            stmt = stmt.where(RequestLog.success == success)
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
    def get_filter_options(
        db: Session,
        *,
        exclude_health_checks: bool = False,
        user_account_id: int | None = None,
        api_client_key_ids: list[int] | None = None,
    ) -> dict[str, list[dict[str, str]]]:
        provider_stmt = select(RequestLog.provider_id, RequestLog.provider_name).where(RequestLog.provider_id.is_not(None))
        model_stmt = select(RequestLog.model_name).where(RequestLog.model_name.is_not(None))
        api_key_stmt = select(
            RequestLog.api_client_key_id,
            RequestLog.api_client_key_name,
            RequestLog.api_client_key_prefix,
        ).where(RequestLog.api_client_key_id.is_not(None))
        user_stmt = select(RequestLog.user_account_id, RequestLog.user_account_name).where(RequestLog.user_account_id.is_not(None))
        provider_stmt = LogService._apply_log_filters(
            provider_stmt,
            log_type=None,
            log_types=None,
            provider_id=None,
            model_name=None,
            conversation_key=None,
            api_client_key_id=None,
            api_client_key_query=None,
            user_account_id=user_account_id,
            success=None,
            exclude_health_checks=exclude_health_checks,
            api_client_key_ids=api_client_key_ids,
        )
        model_stmt = LogService._apply_log_filters(
            model_stmt,
            log_type=None,
            log_types=None,
            provider_id=None,
            model_name=None,
            conversation_key=None,
            api_client_key_id=None,
            api_client_key_query=None,
            user_account_id=user_account_id,
            success=None,
            exclude_health_checks=exclude_health_checks,
            api_client_key_ids=api_client_key_ids,
        )
        api_key_stmt = LogService._apply_log_filters(
            api_key_stmt,
            log_type=None,
            log_types=None,
            provider_id=None,
            model_name=None,
            conversation_key=None,
            api_client_key_id=None,
            api_client_key_query=None,
            user_account_id=user_account_id,
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
            conversation_key=None,
            api_client_key_id=None,
            api_client_key_query=None,
            user_account_id=user_account_id,
            success=None,
            exclude_health_checks=exclude_health_checks,
            api_client_key_ids=api_client_key_ids,
        )

        provider_rows = db.execute(
            provider_stmt.distinct().order_by(RequestLog.provider_id.asc(), RequestLog.provider_name.asc())
        )
        model_rows = db.execute(model_stmt.distinct().order_by(RequestLog.model_name.asc()))
        api_key_rows = db.execute(
            api_key_stmt.distinct().order_by(RequestLog.api_client_key_id.asc(), RequestLog.api_client_key_name.asc())
        )
        user_rows = db.execute(
            user_stmt.distinct().order_by(RequestLog.user_account_id.asc(), RequestLog.user_account_name.asc())
        )

        providers = [
            {
                "value": str(row.provider_id),
                "label": f"{row.provider_id} · {row.provider_name or '-'}",
            }
            for row in provider_rows
            if row.provider_id is not None
        ]
        model_names = [
            {
                "value": str(row.model_name),
                "label": str(row.model_name),
            }
            for row in model_rows
            if row.model_name
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

        return {
            "providers": providers,
            "model_names": model_names,
            "api_client_key_ids": api_client_key_ids,
            "api_client_key_queries": api_client_key_queries,
            "users": users,
        }

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
    def extract_reasoning_level(payload: dict | None) -> str:
        if not isinstance(payload, dict):
            return LogService.REASONING_LEVEL_NONE
        direct_value = payload.get("reasoning_level") or payload.get("reasoning_effort")
        if isinstance(direct_value, str):
            return LogService.normalize_reasoning_level(direct_value)
        reasoning = payload.get("reasoning")
        if isinstance(reasoning, dict):
            for key in ("effort", "reasoning_effort", "level"):
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
            containers = [metadata, payload] if isinstance(metadata, dict) else [payload]
            for container in containers:
                if not isinstance(container, dict):
                    continue
                for key in ("session_id", "conversation_id", "thread_id", "session", "conversation_key"):
                    value = container.get(key)
                    if isinstance(value, str) and value.strip():
                        return value.strip()
        return conversation_key or fallback

    @staticmethod
    def derive_attempt_count(trace: list[dict] | dict | None) -> int:
        if not isinstance(trace, list) or not trace:
            return 0
        primary_markers = {
            "http_error",
            "exception",
            "model_not_found",
            "rate_limited",
            "request_rejected",
            "upstream_auth_error",
            "stream_opened",
        }
        fallback_markers = {"success", "auth_rejected", "interrupted", "client_cancelled"}
        primary_count = sum(1 for item in trace if isinstance(item, dict) and item.get("result") in primary_markers)
        if primary_count:
            return primary_count
        return sum(1 for item in trace if isinstance(item, dict) and item.get("result") in fallback_markers)

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
        if not isinstance(response_payload, dict):
            return None, None
        usage = response_payload.get("usage")
        if not isinstance(usage, dict):
            nested_response = response_payload.get("response")
            if isinstance(nested_response, dict):
                usage = nested_response.get("usage")
        if not isinstance(usage, dict):
            return None, None
        cache_read = LogService._extract_usage_int(
            usage,
            ("cache_read_tokens",),
            ("cache_read_input_tokens",),
            ("cache_read_input_token_count",),
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
            ("prompt_tokens_details", "cacheCreationTokens"),
            ("input_tokens_details", "cache_creation_tokens"),
            ("input_tokens_details", "cacheCreationTokens"),
        )
        return cache_read, cache_write

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
        if derived_attempt_count and log.attempt_count != derived_attempt_count:
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
        cache_read, cache_write = LogService.extract_cache_tokens(payload)
        if cache_read is not None and log.cache_read_tokens != cache_read:
            log.cache_read_tokens = cache_read
            changed = True
        if cache_write is not None and log.cache_write_tokens != cache_write:
            log.cache_write_tokens = cache_write
            changed = True
        return changed

    @staticmethod
    def clear_logs(db: Session) -> int:
        result = db.execute(delete(RequestLog))
        db.commit()
        return result.rowcount or 0

    @staticmethod
    def metric_summary(
        db: Session,
        *,
        window_minutes: int,
        user_account_id: int | None = None,
        api_client_key_ids: list[int] | None = None,
    ) -> list[dict]:
        since = datetime.utcnow() - timedelta(minutes=window_minutes)
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
                    "p95_latency_ms": LogService._percentile(latency_values, 95),
                    "p99_latency_ms": LogService._percentile(latency_values, 99),
                    "p95_ttfb_ms": LogService._percentile(ttfb_values, 95),
                    "p99_ttfb_ms": LogService._percentile(ttfb_values, 99),
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
        return results

    @staticmethod
    def route_metric_summary(db: Session, *, window_minutes: int, requested_model: str | None = None) -> dict[tuple[int | None, str | None], dict]:
        since = datetime.utcnow() - timedelta(minutes=window_minutes)
        stmt = (
            select(
                RequestLog.provider_id,
                RequestLog.requested_model,
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
            stmt = stmt.where(RequestLog.requested_model == requested_model)
        stmt = stmt.group_by(RequestLog.provider_id, RequestLog.requested_model)

        summary: dict[tuple[int | None, str | None], dict] = {}
        for row in db.execute(stmt):
            total_requests = int(row.total_requests or 0)
            failed_requests = int(row.failed_requests or 0)
            summary[(row.provider_id, row.requested_model)] = {
                "total_requests": total_requests,
                "failed_requests": failed_requests,
                "failure_rate": (failed_requests / total_requests) if total_requests else 0.0,
                "success_rate": ((total_requests - failed_requests) / total_requests) if total_requests else 1.0,
                "avg_latency_ms": float(row.avg_latency_ms) if row.avg_latency_ms is not None else None,
            }
        return summary

    @staticmethod
    def export_logs_csv(
        db: Session,
        *,
        log_type: str | None,
        log_types: list[str] | None,
        provider_id: int | None,
        model_name: str | None,
        conversation_key: str | None,
        api_client_key_id: int | None,
        api_client_key_query: str | None,
        user_account_id: int | None,
        success: bool | None,
        exclude_health_checks: bool,
        api_client_key_ids: list[int] | None = None,
        limit: int = 5000,
    ) -> str:
        stmt = select(RequestLog)
        stmt = LogService._apply_log_filters(
            stmt,
            log_type=log_type,
            log_types=log_types,
            provider_id=provider_id,
            model_name=model_name,
            conversation_key=conversation_key,
            api_client_key_id=api_client_key_id,
            api_client_key_query=api_client_key_query,
            user_account_id=user_account_id,
            success=success,
            exclude_health_checks=exclude_health_checks,
            api_client_key_ids=api_client_key_ids,
        )
        rows = list(
            db.scalars(
                stmt.order_by(RequestLog.created_at.desc(), RequestLog.id.desc()).limit(max(1, min(limit, 10000)))
            )
        )
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow([
            "created_at",
            "log_type",
            "trace_id",
            "request_id",
            "session_id",
            "conversation_key",
            "requested_model",
            "provider_name",
            "tenant_name",
            "project_name",
            "app_name",
            "environment_name",
            "source_ip",
            "http_method",
            "success",
            "status_code",
            "error_type",
            "error_code",
            "retryable",
            "is_stream",
            "has_image",
            "latency_ms",
            "ttfb_ms",
            "duration_ms",
            "tps",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "cache_read_tokens",
            "cache_write_tokens",
            "billing_multiplier",
            "total_cost",
            "billing_calculation",
            "reasoning_level",
            "api_client_key_name",
            "user_account_name",
            "api_client_remaining_requests_daily",
            "api_client_remaining_cost_daily",
            "message",
        ])
        for item in rows:
            writer.writerow([
                item.created_at.isoformat() if item.created_at else "",
                item.log_type,
                item.trace_id or "",
                item.request_id or "",
                item.session_id or "",
                item.conversation_key or "",
                item.requested_model or item.model_name or "",
                item.provider_name or "",
                item.tenant_name or "",
                item.project_name or "",
                item.app_name or "",
                item.environment_name or "",
                item.source_ip or "",
                item.http_method or "",
                "true" if item.success else "false",
                item.status_code if item.status_code is not None else "",
                item.error_type or "",
                item.error_code or "",
                "" if item.retryable is None else ("true" if item.retryable else "false"),
                "true" if item.is_stream else "false",
                "true" if item.has_image else "false",
                item.latency_ms if item.latency_ms is not None else "",
                item.ttfb_ms if item.ttfb_ms is not None else "",
                item.duration_ms if item.duration_ms is not None else "",
                item.tps if item.tps is not None else "",
                item.prompt_tokens if item.prompt_tokens is not None else "",
                item.completion_tokens if item.completion_tokens is not None else "",
                item.total_tokens if item.total_tokens is not None else "",
                item.cache_read_tokens if item.cache_read_tokens is not None else "",
                item.cache_write_tokens if item.cache_write_tokens is not None else "",
                item.billing_multiplier if item.billing_multiplier is not None else "",
                item.total_cost if item.total_cost is not None else "",
                LogService.format_billing_calculation(item),
                item.reasoning_level or "",
                item.api_client_key_name or "",
                item.user_account_name or "",
                item.api_client_remaining_requests_daily if item.api_client_remaining_requests_daily is not None else "",
                item.api_client_remaining_cost_daily if item.api_client_remaining_cost_daily is not None else "",
                item.message or "",
            ])
        return buffer.getvalue()

    @staticmethod
    def format_billing_calculation(item: RequestLog) -> str:
        multiplier = item.billing_multiplier if item.billing_multiplier is not None else 1
        input_price = item.channel_price_input_per_1k
        output_price = item.channel_price_output_per_1k
        cache_price = item.channel_price_cache_per_1k if item.channel_price_cache_per_1k is not None else input_price
        cache_read_tokens = int(item.cache_read_tokens or 0)
        regular_input_tokens = max(0, int(item.prompt_tokens or 0) - cache_read_tokens)
        input_part = (
            "输入单价未设置"
            if input_price is None
            else f"输入 {regular_input_tokens}/1000 × {float(input_price):.6f}"
        )
        cache_part = (
            None
            if cache_read_tokens <= 0
            else (
                "缓存单价未设置"
                if cache_price is None
                else f"缓存 {cache_read_tokens}/1000 × {float(cache_price):.6f}"
            )
        )
        output_part = (
            "输出单价未设置"
            if output_price is None
            else f"输出 {int(item.completion_tokens or 0)}/1000 × {float(output_price):.6f}"
        )
        parts = [input_part]
        if cache_part:
            parts.append(cache_part)
        parts.append(output_part)
        return f"倍率 {float(multiplier):.2f}x；" + " + ".join(parts)

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
        since = datetime.utcnow() - timedelta(minutes=window_minutes)
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
                    "p95_latency_ms": LogService._percentile(latency_values, 95),
                    "p99_latency_ms": LogService._percentile(latency_values, 99),
                    "qps": round(total_requests / bucket_window_seconds, 4),
                    "peak_active_requests": LogService._compute_peak_active_requests(bucket_logs),
                    "total_tokens": sum(int(item.total_tokens or 0) for item in bucket_logs),
                    "total_cost": round(sum(float(item.total_cost or 0) for item in bucket_logs), 6),
                }
            )
        return results

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
        since = datetime.utcnow() - timedelta(days=window_days)
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
        dialect = db.get_bind().dialect.name
        if dialect == "sqlite":
            if period_type == "day":
                return func.strftime("%Y-%m-%d", RequestLog.created_at)
            if period_type == "week":
                return func.strftime("%Y-%W", RequestLog.created_at)
            return func.strftime("%Y-%m", RequestLog.created_at)
        if dialect == "postgresql":
            return func.date_trunc(period_type, RequestLog.created_at)
        if period_type == "day":
            return func.date(RequestLog.created_at)
        if period_type == "week":
            return func.extract("week", RequestLog.created_at)
        return func.extract("month", RequestLog.created_at)

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
        return LogService._period_start(datetime.utcnow(), period_type)
