from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.logging.dispatcher import LoggingDispatcher
from app.logging.sanitizers import dumps_sanitized
from app.models.request_log import RequestLog
from app.utils.json_utils import safeJsonParse


class RequestLogRecorder:
    @staticmethod
    def record_summary(db: Session, **kwargs: Any) -> RequestLog:
        from app.services.log_service import LogService

        return LogService.create_log(db, **kwargs)

    @staticmethod
    def record_auth(db: Session, *, request_log_id: int | None, trace_id: str | None, auto_commit: bool = True, **payload: Any):
        return RequestLogRecorder._record(db, "request_auth", request_log_id=request_log_id, trace_id=trace_id, auto_commit=auto_commit, **payload)

    @staticmethod
    def record_validation(db: Session, *, request_log_id: int | None, trace_id: str | None, auto_commit: bool = True, **payload: Any):
        return RequestLogRecorder._record(db, "request_validation", request_log_id=request_log_id, trace_id=trace_id, auto_commit=auto_commit, **payload)

    @staticmethod
    def record_model_permission(db: Session, *, request_log_id: int | None, trace_id: str | None, auto_commit: bool = True, **payload: Any):
        return RequestLogRecorder._record(db, "request_model_permission", request_log_id=request_log_id, trace_id=trace_id, auto_commit=auto_commit, **payload)

    @staticmethod
    def record_route_decision(db: Session, *, request_log_id: int | None, trace_id: str | None, auto_commit: bool = True, **payload: Any):
        return RequestLogRecorder._record(db, "request_route_decision", request_log_id=request_log_id, trace_id=trace_id, auto_commit=auto_commit, **payload)

    @staticmethod
    def record_provider_attempt(db: Session, *, request_log_id: int | None, trace_id: str | None, auto_commit: bool = True, **payload: Any):
        return RequestLogRecorder._record(db, "request_provider_attempt", request_log_id=request_log_id, trace_id=trace_id, auto_commit=auto_commit, **payload)

    @staticmethod
    def record_upstream_response(db: Session, *, request_log_id: int | None, trace_id: str | None, auto_commit: bool = True, **payload: Any):
        return RequestLogRecorder._record(db, "request_upstream_response", request_log_id=request_log_id, trace_id=trace_id, auto_commit=auto_commit, **payload)

    @staticmethod
    def record_stream(db: Session, *, request_log_id: int | None, trace_id: str | None, auto_commit: bool = True, **payload: Any):
        return RequestLogRecorder._record(db, "request_stream", request_log_id=request_log_id, trace_id=trace_id, auto_commit=auto_commit, **payload)

    @staticmethod
    def record_error_response(db: Session, *, request_log_id: int | None, trace_id: str | None, auto_commit: bool = True, **payload: Any):
        return RequestLogRecorder._record(db, "request_error_response", request_log_id=request_log_id, trace_id=trace_id, auto_commit=auto_commit, **payload)

    @staticmethod
    def record_content_guard(db: Session, *, request_log_id: int | None, trace_id: str | None, auto_commit: bool = True, **payload: Any):
        return RequestLogRecorder._record(db, "request_content_guard", request_log_id=request_log_id, trace_id=trace_id, auto_commit=auto_commit, **payload)

    @staticmethod
    def record_billing(db: Session, *, request_log_id: int | None, trace_id: str | None, auto_commit: bool = True, **payload: Any):
        return RequestLogRecorder._record(db, "request_billing", request_log_id=request_log_id, trace_id=trace_id, auto_commit=auto_commit, **payload)

    @staticmethod
    def record_events_from_summary(db: Session, log: RequestLog, *, auto_commit: bool = False) -> None:
        events = RequestLogRecorder.build_events_from_summary(log)
        if not events:
            return
        LoggingDispatcher.record_many(events, db=db, enqueue=False, auto_commit=auto_commit)

    @staticmethod
    def build_events_from_summary(log: RequestLog):
        events = []
        trace = safeJsonParse(log.trace_json) if log.trace_json else None
        trace_items = trace if isinstance(trace, list) else []
        native_events = RequestLogRecorder._build_native_events_from_trace(log, trace_items)
        native_event_names = {event.envelope.event_name for event in native_events}
        events.extend(native_events)
        request_log_id = log.id
        trace_id = log.trace_id
        if log.api_client_auth_result and "request_auth" not in native_event_names:
            events.append(LoggingDispatcher.build_event(
                event_type="external_request",
                event_name="request_auth",
                trace_id=trace_id,
                correlation_id=str(request_log_id),
                module="proxy",
                result="success" if log.api_client_auth_result == "authenticated" else "failed",
                payload={
                    "request_log_id": request_log_id,
                    "trace_id": trace_id,
                    "auth_result": log.api_client_auth_result,
                    "api_client_key_id": log.api_client_key_id,
                    "api_client_key_prefix": log.api_client_key_prefix,
                    "user_account_id": log.user_account_id,
                    "remaining_tokens": log.api_client_remaining_tokens,
                    "remaining_requests_daily": log.api_client_remaining_requests_daily,
                    "remaining_cost_daily": float(log.api_client_remaining_cost_daily) if log.api_client_remaining_cost_daily is not None else None,
                    "policy_snapshot_json": log.api_client_policy_snapshot_json,
                    "error_code": None if log.api_client_auth_result == "authenticated" else log.api_client_auth_result,
                },
            ))
        if log.request_body_json and "request_validation" not in native_event_names:
            events.append(LoggingDispatcher.build_event(
                event_type="external_request",
                event_name="request_validation",
                trace_id=trace_id,
                correlation_id=str(request_log_id),
                module="proxy",
                result="success" if log.success else "failed",
                payload={
                    "request_log_id": request_log_id,
                    "trace_id": trace_id,
                    "validation_stage": "request_summary",
                    "passed": log.success or not log.error_code,
                    "request_body_summary_json": log.request_body_json,
                    "error_code": log.error_code if not log.success else None,
                },
            ))
        if "request_model_permission" not in native_event_names:
            events.append(LoggingDispatcher.build_event(
                event_type="external_request",
                event_name="request_model_permission",
                trace_id=trace_id,
                correlation_id=str(request_log_id),
                module="proxy",
                result="success" if log.success else "failed",
                payload={
                    "request_log_id": request_log_id,
                    "trace_id": trace_id,
                    "requested_model": log.requested_model,
                    "resolved_model": log.model_name,
                    "endpoint_path": log.request_path,
                    "required_capabilities_json": dumps_sanitized({
                        "stream": log.is_stream,
                        "vision": log.has_image,
                        "endpoint_path": log.request_path,
                    }),
                    "permission_result": "allowed" if log.success else (log.error_code or "failed"),
                    "reason_details_json": dumps_sanitized({"message": log.message, "error_code": log.error_code}),
                },
            ))
        route_items = [item for item in trace_items if isinstance(item, dict) and item.get("result") in {"route_candidates_exhausted", "route_exhausted_wait_retry", "model_mapping", "stateful_responses_routing"}]
        if "request_route_decision" not in native_event_names and (route_items or log.provider_id):
            diagnostics = next((item.get("diagnostic") for item in route_items if isinstance(item.get("diagnostic"), dict)), None)
            events.append(LoggingDispatcher.build_event(
                event_type="external_request",
                event_name="request_route_decision",
                trace_id=trace_id,
                correlation_id=str(request_log_id),
                module="proxy",
                result="success" if log.provider_id else "failed",
                payload={
                    "request_log_id": request_log_id,
                    "trace_id": trace_id,
                    "route_round": 1,
                    "route_policy": "健康优先",
                    "candidate_count": diagnostics.get("final_candidate_count") if isinstance(diagnostics, dict) else None,
                    "selected_provider_id": log.provider_id,
                    "selected_provider_model_id": log.resolved_provider_model_id,
                    "sticky_hit": any(item.get("sticky_hit") for item in trace_items if isinstance(item, dict)),
                    "excluded_summary_json": dumps_sanitized(diagnostics.get("reason_counts") if isinstance(diagnostics, dict) else None),
                    "diagnostics_json": dumps_sanitized(route_items),
                },
            ))
        attempt_index = 0
        last_attempt_event_id = None
        for item in trace_items:
            if "request_provider_attempt" in native_event_names:
                break
            if not isinstance(item, dict) or "provider_id" not in item:
                continue
            attempt_index += 1
            event = LoggingDispatcher.build_event(
                event_type="external_request",
                event_name="request_provider_attempt",
                trace_id=trace_id,
                correlation_id=str(request_log_id),
                module="proxy",
                result="success" if item.get("result") in {"success", "stream_opened"} else "failed",
                payload={
                    "request_log_id": request_log_id,
                    "trace_id": trace_id,
                    "attempt_index": attempt_index,
                    "provider_id": item.get("provider_id"),
                    "provider_name": item.get("provider_name"),
                    "provider_model_id": item.get("provider_model_id"),
                    "actual_model": item.get("model_name"),
                    "endpoint_path": item.get("endpoint_path") or log.request_path,
                    "protocol_type": item.get("protocol_type"),
                    "capacity_lease_acquired": item.get("result") not in {"capacity_limited", "capacity_unavailable"},
                    "result": item.get("result") or "unknown",
                    "status_code": item.get("status_code"),
                    "latency_ms": item.get("latency_ms"),
                    "upstream_request_id": log.upstream_request_id if item.get("result") in {"success", "stream_opened"} else None,
                    "error_code": item.get("error_code"),
                    "retryable": item.get("result") not in {"success", "stream_opened", "model_not_found", "request_rejected"},
                },
            )
            events.append(event)
        if "request_upstream_response" not in native_event_names and (log.success or log.response_body_json or log.response_text):
            events.append(LoggingDispatcher.build_event(
                event_type="external_request",
                event_name="request_upstream_response",
                trace_id=trace_id,
                correlation_id=str(request_log_id),
                module="proxy",
                result="success" if log.success else "failed",
                payload={
                    "request_log_id": request_log_id,
                    "trace_id": trace_id,
                    "status_code": log.status_code,
                    "upstream_request_id": log.upstream_request_id,
                    "finish_reason": log.finish_reason,
                    "usage_json": dumps_sanitized({
                        "prompt_tokens": log.prompt_tokens,
                        "completion_tokens": log.completion_tokens,
                        "total_tokens": log.total_tokens,
                        "cache_read_tokens": log.cache_read_tokens,
                        "cache_write_tokens": log.cache_write_tokens,
                    }),
                    "response_summary_json": log.response_body_json,
                    "response_text_excerpt": (log.response_text or "")[:500] if log.response_text else None,
                    "response_body_truncated": "truncated" in (log.response_body_json or "").lower(),
                },
            ))
        if log.is_stream and "request_stream" not in native_event_names:
            stream_result = "completed" if log.success else ("client_disconnected" if log.status_code == 499 else "upstream_error")
            events.append(LoggingDispatcher.build_event(
                event_type="external_request",
                event_name="request_stream",
                trace_id=trace_id,
                correlation_id=str(request_log_id),
                module="proxy",
                result="success" if log.success else "failed",
                payload={
                    "request_log_id": request_log_id,
                    "trace_id": trace_id,
                    "stream_result": stream_result,
                    "first_token_latency_ms": log.first_token_latency_ms,
                    "ttfb_ms": log.ttfb_ms,
                    "duration_ms": log.duration_ms,
                    "captured_text_bytes": len((log.response_text or "").encode("utf-8")) if log.response_text else None,
                    "sse_error_sent": not log.success,
                    "done_sent": True,
                    "disconnect_status_code": 499 if log.status_code == 499 else None,
                },
            ))
        if not log.success and "request_error_response" not in native_event_names:
            events.append(LoggingDispatcher.build_event(
                event_type="external_request",
                event_name="request_error_response",
                trace_id=trace_id,
                correlation_id=str(request_log_id),
                module="proxy",
                severity="warning",
                result="failed",
                payload={
                    "request_log_id": request_log_id,
                    "trace_id": trace_id,
                    "status_code": log.status_code,
                    "error_type": log.error_type,
                    "error_code": log.error_code,
                    "public_message": log.message,
                    "category": RequestLogRecorder._error_category_from_body(log.response_body_json),
                    "retryable": log.retryable,
                    "recoverable": log.retryable,
                    "diagnostic_sample_json": log.response_body_json or dumps_sanitized({"trace": trace_items[-5:]}),
                },
            ))
        if log.content_guard_result and "request_content_guard" not in native_event_names:
            events.append(LoggingDispatcher.build_event(
                event_type="external_request",
                event_name="request_content_guard",
                trace_id=trace_id,
                correlation_id=str(request_log_id),
                module="content_guard",
                severity="danger" if log.content_guard_risk_level == "high" else "info",
                result="blocked" if log.content_guard_result == "block" else "success",
                payload={
                    "request_log_id": request_log_id,
                    "trace_id": trace_id,
                    "guard_stage": "stream_buffer" if log.is_stream else "non_stream_response",
                    "guard_result": log.content_guard_result,
                    "risk_level": log.content_guard_risk_level,
                    "matched_categories_json": log.content_guard_categories_json,
                    "reason": log.content_guard_reason,
                    "action": log.content_guard_action,
                    "excerpt": log.content_guard_excerpt,
                    "provider_status_after": log.content_guard_final_strategy,
                },
            ))
        if (log.billing_status or log.billing_event_id) and "request_billing" not in native_event_names:
            events.append(LoggingDispatcher.build_event(
                event_type="external_request",
                event_name="request_billing",
                trace_id=trace_id,
                correlation_id=str(request_log_id),
                module="billing",
                result="failed" if log.billing_status == "failed" else "success",
                payload={
                    "request_log_id": request_log_id,
                    "trace_id": trace_id,
                    "billing_event_id": log.billing_event_id,
                    "billing_stage": log.billing_status or "queued",
                    "token_source": "upstream_usage" if log.total_tokens is not None else "missing",
                    "prompt_tokens": log.prompt_tokens,
                    "completion_tokens": log.completion_tokens,
                    "cache_read_tokens": log.cache_read_tokens,
                    "cache_write_tokens": log.cache_write_tokens,
                    "prompt_cost": float(log.prompt_cost) if log.prompt_cost is not None else None,
                    "completion_cost": float(log.completion_cost) if log.completion_cost is not None else None,
                    "total_cost": float(log.total_cost) if log.total_cost is not None else None,
                    "balance_after": float(log.api_client_balance_after) if log.api_client_balance_after is not None else None,
                    "attempt_count": log.billing_attempt_count,
                    "error": log.billing_error,
                },
            ))
        return events

    @staticmethod
    def _build_native_events_from_trace(log: RequestLog, trace_items: list[dict]):
        events = []
        provider_attempt_index = 0
        for item in trace_items:
            if not isinstance(item, dict):
                continue
            event_name = item.get("typed_event")
            payload = item.get("payload")
            if not isinstance(event_name, str) or not event_name.startswith("request_") or not isinstance(payload, dict):
                continue
            native_payload = dict(payload)
            native_payload.setdefault("request_log_id", log.id)
            native_payload.setdefault("trace_id", log.trace_id)
            if event_name == "request_provider_attempt":
                provider_attempt_index += 1
                native_payload.setdefault("attempt_index", provider_attempt_index)
            events.append(LoggingDispatcher.build_event(
                event_type="external_request",
                event_name=event_name,
                trace_id=log.trace_id,
                correlation_id=str(log.id) if log.id is not None else None,
                module=str(item.get("module") or "proxy"),
                severity=str(item.get("severity") or "info"),
                result=str(item.get("event_result") or item.get("result") or "success"),
                payload=native_payload,
            ))
        return events

    @staticmethod
    def _record(db: Session, event_name: str, *, request_log_id: int | None, trace_id: str | None, auto_commit: bool = True, **payload: Any):
        payload = {"request_log_id": request_log_id, "trace_id": trace_id, **payload}
        event = LoggingDispatcher.build_event(
            event_type="external_request",
            event_name=event_name,
            trace_id=trace_id,
            correlation_id=str(request_log_id) if request_log_id is not None else None,
            module="proxy",
            result="failed" if payload.get("passed") is False or payload.get("result") in {"failed", "blocked"} else "success",
            payload=payload,
        )
        return LoggingDispatcher.record(event, db=db, auto_commit=auto_commit)

    @staticmethod
    def _error_category_from_body(response_body_json: str | None) -> str | None:
        parsed = safeJsonParse(response_body_json) if response_body_json else None
        if isinstance(parsed, dict):
            error_obj = parsed.get("error")
            if isinstance(error_obj, dict):
                category = error_obj.get("category")
                if isinstance(category, str):
                    return category
        return None
