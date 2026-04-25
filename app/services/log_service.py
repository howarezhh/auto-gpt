from datetime import datetime, timedelta

from sqlalchemy import case, delete, func, not_, or_, select
from sqlalchemy.orm import Session

from app.models.provider_model import ProviderModel
from app.models.request_log import RequestLog
from app.utils.json_utils import dumps_json, safeJsonParse


class LogService:
    HEALTH_CHECK_LOG_TYPES = ("health_check", "health_check_provider", "health_check_model")
    ROUTE_TRAFFIC_LOG_TYPES = ("chat", "responses")
    USER_VISIBLE_LOG_TYPES = ("chat", "responses")
    REASONING_LEVEL_NONE = "无"
    REASONING_LEVEL_VALUES = {REASONING_LEVEL_NONE, "low", "medium", "high", "xhigh"}

    @staticmethod
    def create_log(
        db: Session,
        *,
        log_type: str,
        provider_id: int | None = None,
        provider_name: str | None = None,
        model_name: str | None = None,
        requested_model: str | None = None,
        request_id: str | None = None,
        conversation_key: str | None = None,
        session_id: str | None = None,
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
        api_client_key_id: int | None = None,
        api_client_key_name: str | None = None,
        api_client_key_prefix: str | None = None,
        user_account_id: int | None = None,
        user_account_name: str | None = None,
        api_client_auth_result: str | None = None,
        api_client_remaining_tokens: int | None = None,
        api_client_policy_snapshot_json: str | None = None,
        billing_multiplier: float | None = None,
        channel_price_input_per_1k: float | None = None,
        channel_price_output_per_1k: float | None = None,
        trace: list[dict] | dict | None = None,
        token_request_payload: dict | None = None,
        token_response_payload: dict | None = None,
        token_response_text: str | None = None,
        schedule_token_fill: bool = True,
    ) -> RequestLog:
        provider_model = None
        if resolved_provider_model_id is not None and (
            billing_multiplier is None
            or channel_price_input_per_1k is None
            or channel_price_output_per_1k is None
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
            model_name=model_name,
            requested_model=requested_model,
            request_id=request_id,
            conversation_key=conversation_key,
            session_id=session_id or LogService.extract_session_id(token_request_payload, conversation_key=conversation_key, fallback=request_id),
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
            api_client_key_id=api_client_key_id,
            api_client_key_name=api_client_key_name,
            api_client_key_prefix=api_client_key_prefix,
            user_account_id=user_account_id,
            user_account_name=user_account_name,
            api_client_auth_result=api_client_auth_result,
            api_client_remaining_tokens=api_client_remaining_tokens,
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
            trace_json=dumps_json(trace) if trace is not None else None,
        )
        LogService.refresh_derived_fields(log, response_payload=token_response_payload, trace=trace)
        db.add(log)
        db.commit()
        db.refresh(log)
        if (
            schedule_token_fill
            and
            request_path
            and request_path != "/v1/models"
            and log_type not in LogService.HEALTH_CHECK_LOG_TYPES
            and (prompt_tokens is None or completion_tokens is None or total_tokens is None)
        ):
            from app.services.token_usage_service import TokenUsageService

            TokenUsageService.enqueue_log_usage_fill(
                log_id=log.id,
                model_name=requested_model or model_name,
                request_path=request_path,
                request_payload=token_request_payload,
                response_payload=token_response_payload,
                response_text=token_response_text,
            )
        return log

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
    ) -> dict[str, list[dict[str, str]]]:
        provider_stmt = select(RequestLog.provider_id, RequestLog.provider_name).where(RequestLog.provider_id.is_not(None))
        model_stmt = select(RequestLog.model_name).where(RequestLog.model_name.is_not(None))
        api_key_stmt = select(
            RequestLog.api_client_key_id,
            RequestLog.api_client_key_name,
            RequestLog.api_client_key_prefix,
        ).where(RequestLog.api_client_key_id.is_not(None))
        user_stmt = select(RequestLog.user_account_id, RequestLog.user_account_name).where(RequestLog.user_account_id.is_not(None))
        if exclude_health_checks:
            non_health_check_expr = LogService._non_health_check_expr()
            provider_stmt = provider_stmt.where(non_health_check_expr)
            model_stmt = model_stmt.where(non_health_check_expr)
            api_key_stmt = api_key_stmt.where(non_health_check_expr)
            user_stmt = user_stmt.where(non_health_check_expr)

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
            return None, None
        cache_read = LogService._extract_usage_int(
            usage,
            ("cache_read_tokens",),
            ("cached_tokens",),
            ("prompt_tokens_details", "cached_tokens"),
            ("input_tokens_details", "cached_tokens"),
            ("cache_read_input_tokens",),
        )
        cache_write = LogService._extract_usage_int(
            usage,
            ("cache_write_tokens",),
            ("cache_creation_tokens",),
            ("prompt_tokens_details", "cache_creation_tokens"),
            ("input_tokens_details", "cache_creation_tokens"),
            ("cache_creation_input_tokens",),
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
                return int(current)
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
    def metric_summary(db: Session, *, window_minutes: int) -> list[dict]:
        since = datetime.utcnow() - timedelta(minutes=window_minutes)
        stmt = (
            select(
                RequestLog.provider_id,
                RequestLog.provider_name,
                RequestLog.requested_model,
                func.count(RequestLog.id).label("total_requests"),
                func.sum(case((RequestLog.success.is_(False), 1), else_=0)).label("failed_requests"),
                func.avg(RequestLog.latency_ms).label("avg_latency_ms"),
                func.sum(RequestLog.prompt_tokens).label("prompt_tokens"),
                func.sum(RequestLog.completion_tokens).label("completion_tokens"),
                func.sum(RequestLog.total_tokens).label("total_tokens"),
            )
            .where(RequestLog.created_at >= since)
            .group_by(RequestLog.provider_id, RequestLog.provider_name, RequestLog.requested_model)
            .order_by(func.count(RequestLog.id).desc(), RequestLog.provider_id.asc())
        )
        results = []
        for row in db.execute(stmt):
            total_requests = int(row.total_requests or 0)
            failed_requests = int(row.failed_requests or 0)
            results.append(
                {
                    "provider_id": row.provider_id,
                    "provider_name": row.provider_name,
                    "requested_model": row.requested_model,
                    "total_requests": total_requests,
                    "failed_requests": failed_requests,
                    "failure_rate": round((failed_requests / total_requests) * 100, 2) if total_requests else 0.0,
                    "avg_latency_ms": round(float(row.avg_latency_ms), 2) if row.avg_latency_ms is not None else None,
                    "prompt_tokens": int(row.prompt_tokens or 0),
                    "completion_tokens": int(row.completion_tokens or 0),
                    "total_tokens": int(row.total_tokens or 0),
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
