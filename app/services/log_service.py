from datetime import datetime, timedelta

from sqlalchemy import case, delete, func, not_, or_, select
from sqlalchemy.orm import Session

from app.models.request_log import RequestLog
from app.utils.json_utils import dumps_json


class LogService:
    HEALTH_CHECK_LOG_TYPES = ("health_check", "health_check_provider", "health_check_model")
    ROUTE_TRAFFIC_LOG_TYPES = ("chat", "responses", "embeddings")

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
        resolved_provider_model_id: int | None = None,
        request_path: str | None = None,
        is_stream: bool = False,
        has_image: bool = False,
        success: bool,
        status_code: int | None = None,
        latency_ms: int | None = None,
        first_token_latency_ms: int | None = None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
        total_tokens: int | None = None,
        finish_reason: str | None = None,
        upstream_request_id: str | None = None,
        request_body_json: str | None = None,
        response_body_json: str | None = None,
        response_text: str | None = None,
        message: str | None = None,
        api_client_key_id: int | None = None,
        api_client_key_name: str | None = None,
        api_client_key_prefix: str | None = None,
        api_client_auth_result: str | None = None,
        api_client_remaining_tokens: int | None = None,
        api_client_policy_snapshot_json: str | None = None,
        trace: list[dict] | dict | None = None,
        token_request_payload: dict | None = None,
        token_response_payload: dict | None = None,
        token_response_text: str | None = None,
        schedule_token_fill: bool = True,
    ) -> RequestLog:
        log = RequestLog(
            log_type=log_type,
            provider_id=provider_id,
            provider_name=provider_name,
            model_name=model_name,
            requested_model=requested_model,
            request_id=request_id,
            conversation_key=conversation_key,
            resolved_provider_model_id=resolved_provider_model_id,
            request_path=request_path,
            is_stream=is_stream,
            has_image=has_image,
            success=success,
            status_code=status_code,
            latency_ms=latency_ms,
            first_token_latency_ms=first_token_latency_ms,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            finish_reason=finish_reason,
            upstream_request_id=upstream_request_id,
            request_body_json=request_body_json,
            response_body_json=response_body_json,
            response_text=response_text,
            message=message,
            api_client_key_id=api_client_key_id,
            api_client_key_name=api_client_key_name,
            api_client_key_prefix=api_client_key_prefix,
            api_client_auth_result=api_client_auth_result,
            api_client_remaining_tokens=api_client_remaining_tokens,
            api_client_policy_snapshot_json=api_client_policy_snapshot_json,
            trace_json=dumps_json(trace) if trace is not None else None,
        )
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
        provider_id: int | None,
        model_name: str | None,
        conversation_key: str | None,
        api_client_key_id: int | None,
        api_client_key_query: str | None,
        success: bool | None,
        exclude_health_checks: bool = False,
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
            provider_id=provider_id,
            model_name=model_name,
            conversation_key=conversation_key,
            api_client_key_id=api_client_key_id,
            api_client_key_query=api_client_key_query,
            success=success,
            exclude_health_checks=exclude_health_checks,
        )
        count_stmt = LogService._apply_log_filters(
            count_stmt,
            log_type=log_type,
            provider_id=provider_id,
            model_name=model_name,
            conversation_key=conversation_key,
            api_client_key_id=api_client_key_id,
            api_client_key_query=api_client_key_query,
            success=success,
            exclude_health_checks=exclude_health_checks,
        )
        summary_stmt = LogService._apply_log_filters(
            summary_stmt,
            log_type=log_type,
            provider_id=provider_id,
            model_name=model_name,
            conversation_key=conversation_key,
            api_client_key_id=api_client_key_id,
            api_client_key_query=api_client_key_query,
            success=success,
            exclude_health_checks=exclude_health_checks,
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
        provider_id: int | None,
        model_name: str | None,
        conversation_key: str | None,
        api_client_key_id: int | None,
        api_client_key_query: str | None,
        success: bool | None,
        exclude_health_checks: bool,
    ):
        if exclude_health_checks:
            stmt = stmt.where(LogService._non_health_check_expr())
        if log_type:
            stmt = stmt.where(RequestLog.log_type == log_type)
        if provider_id:
            stmt = stmt.where(RequestLog.provider_id == provider_id)
        if model_name:
            stmt = stmt.where(RequestLog.model_name == model_name)
        if conversation_key:
            stmt = stmt.where(RequestLog.conversation_key == conversation_key)
        if api_client_key_id:
            stmt = stmt.where(RequestLog.api_client_key_id == api_client_key_id)
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
        if exclude_health_checks:
            non_health_check_expr = LogService._non_health_check_expr()
            provider_stmt = provider_stmt.where(non_health_check_expr)
            model_stmt = model_stmt.where(non_health_check_expr)
            api_key_stmt = api_key_stmt.where(non_health_check_expr)

        provider_rows = db.execute(
            provider_stmt.distinct().order_by(RequestLog.provider_id.asc(), RequestLog.provider_name.asc())
        )
        model_rows = db.execute(model_stmt.distinct().order_by(RequestLog.model_name.asc()))
        api_key_rows = db.execute(
            api_key_stmt.distinct().order_by(RequestLog.api_client_key_id.asc(), RequestLog.api_client_key_name.asc())
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

        return {
            "providers": providers,
            "model_names": model_names,
            "api_client_key_ids": api_client_key_ids,
            "api_client_key_queries": api_client_key_queries,
        }

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
