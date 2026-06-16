from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.logging.dispatcher import LoggingDispatcher
from app.logging.sanitizers import dumps_sanitized
from app.models.logging_events import (
    RequestAuthEvent,
    RequestBillingEvent,
    RequestContentGuardEvent,
    RequestErrorResponseEvent,
    RequestModelPermissionEvent,
    RequestProviderAttemptEvent,
    RequestRouteDecisionEvent,
    RequestStreamEvent,
    RequestUpstreamResponseEvent,
    RequestValidationEvent,
)
from app.models.request_log import RequestLog
from app.utils.json_utils import safeJsonParse


class RequestLogRecorder:
    EVENT_MODELS = {
        "request_auth": RequestAuthEvent,
        "request_validation": RequestValidationEvent,
        "request_model_permission": RequestModelPermissionEvent,
        "request_route_decision": RequestRouteDecisionEvent,
        "request_provider_attempt": RequestProviderAttemptEvent,
        "request_upstream_response": RequestUpstreamResponseEvent,
        "request_stream": RequestStreamEvent,
        "request_error_response": RequestErrorResponseEvent,
        "request_content_guard": RequestContentGuardEvent,
        "request_billing": RequestBillingEvent,
    }
    PROVIDER_ATTEMPT_RESULTS = {
        "success",
        "stream_opened",
        "http_error",
        "exception",
        "model_not_found",
        "rate_limited",
        "request_rejected",
        "upstream_auth_error",
        "capacity_limited",
        "capacity_unavailable",
        "interrupted",
        "client_cancelled",
        "empty_stream",
        "stream_error",
        "timeout",
        "content_integrity_violation",
        "insufficient_balance_precheck",
    }

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
    def record_missing_events_from_summary(db: Session, log: RequestLog, *, auto_commit: bool = False) -> int:
        events = []
        for event in RequestLogRecorder.build_events_from_summary(log):
            model = RequestLogRecorder.EVENT_MODELS.get(event.envelope.event_name)
            if model is None or log.id is None:
                continue
            exists = db.scalar(
                select(func.count()).select_from(model).where(model.request_log_id == log.id)
            )
            if not exists:
                events.append(event)
        if not events:
            return 0
        LoggingDispatcher.record_many(events, db=db, enqueue=False, auto_commit=auto_commit)
        return len(events)

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
            exhausted_item = next(
                (
                    item for item in reversed(route_items)
                    if isinstance(item, dict) and item.get("result") == "route_candidates_exhausted"
                ),
                None,
            )
            candidate_count = None
            if isinstance(exhausted_item, dict) and "candidate_count_after_failed_exclusion" in exhausted_item:
                candidate_count = exhausted_item.get("candidate_count_after_failed_exclusion")
            elif isinstance(diagnostics, dict):
                candidate_count = diagnostics.get("final_candidate_count")
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
                    "route_policy": "可用性优先",
                    "candidate_count": candidate_count,
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
            if item.get("result") not in RequestLogRecorder.PROVIDER_ATTEMPT_RESULTS:
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
                    "response_body_truncated": RequestLogRecorder._is_response_body_truncated(log.response_body_json),
                },
            ))
        if log.is_stream and "request_stream" not in native_event_names:
            stream_payload = RequestLogRecorder._stream_payload_from_trace(log, trace_items)
            stream_result = stream_payload.get("stream_result") or (
                "completed" if log.success else ("client_disconnected" if log.status_code == 499 else "upstream_error")
            )
            sse_error_sent = stream_payload.get("sse_error_sent")
            done_sent = stream_payload.get("done_sent")
            if sse_error_sent is None and log.error_code == "content_integrity_violation" and log.status_code != 499:
                sse_error_sent = True
            if done_sent is None and log.error_code == "content_integrity_violation" and log.status_code != 499:
                done_sent = True
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
                    "chunk_count": stream_payload.get("chunk_count"),
                    "captured_text_bytes": (
                        stream_payload.get("captured_text_bytes")
                        if stream_payload.get("captured_text_bytes") is not None
                        else (len((log.response_text or "").encode("utf-8")) if log.response_text else None)
                    ),
                    "sse_error_sent": sse_error_sent,
                    "done_sent": done_sent,
                    "disconnect_status_code": 499 if log.status_code == 499 else None,
                },
            ))
        if not log.success and "request_error_response" not in native_event_names:
            error_body = safeJsonParse(log.response_body_json or "")
            error_context = error_body.get("error_context") if isinstance(error_body, dict) else None
            recoverable = (
                bool(error_context.get("recoverable"))
                if isinstance(error_context, dict) and error_context.get("recoverable") is not None
                else log.retryable
            )
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
                    "recoverable": recoverable,
                    "diagnostic_sample_json": log.response_body_json or dumps_sanitized({"trace": trace_items[-5:]}),
                },
            ))
        if log.content_guard_result and "request_content_guard" not in native_event_names:
            content_guard_payload = RequestLogRecorder._latest_content_guard_payload(
                trace_items,
                response_body_json=log.response_body_json,
            )
            guard_result = str(log.content_guard_result or "")
            event_result = "blocked" if RequestLogRecorder._content_guard_event_is_blocked(guard_result) else ("review" if guard_result == "review" else "success")
            matched_rules_json = (
                content_guard_payload.get("matched_rules_json")
                or RequestLogRecorder._content_guard_matched_rules_from_summary(log)
            )
            events.append(LoggingDispatcher.build_event(
                event_type="external_request",
                event_name="request_content_guard",
                trace_id=trace_id,
                correlation_id=str(request_log_id),
                module="content_guard",
                severity="danger" if log.content_guard_risk_level == "high" else "info",
                result=event_result,
                payload={
                    "request_log_id": request_log_id,
                    "trace_id": trace_id,
                    "provider_id": log.provider_id,
                    "provider_name": log.provider_name,
                    "provider_model_id": log.resolved_provider_model_id,
                    "model_name": log.model_name,
                    "requested_model": log.requested_model,
                    "request_path": log.request_path,
                    "is_stream": log.is_stream,
                    "guard_stage": RequestLogRecorder._content_guard_stage_from_payload(log, content_guard_payload),
                    "guard_result": log.content_guard_result,
                    "risk_level": log.content_guard_risk_level,
                    "matched_categories_json": content_guard_payload.get("matched_categories_json") or log.content_guard_categories_json,
                    "matched_rules_json": matched_rules_json,
                    "reason": log.content_guard_reason,
                    "action": log.content_guard_action,
                    "excerpt": log.content_guard_excerpt,
                    "provider_status_after": content_guard_payload.get("provider_status_after"),
                    "confidence": content_guard_payload.get("confidence"),
                    "score_delta": content_guard_payload.get("score_delta"),
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
    def _content_guard_event_is_blocked(value: str | None) -> bool:
        normalized = str(value or "").strip().lower()
        return normalized in {"block", "blocked", "deny", "denied", "reject", "rejected"}

    @staticmethod
    def _content_guard_matched_rules_from_summary(log: RequestLog) -> str | None:
        categories = safeJsonParse(log.content_guard_categories_json or "")
        if not isinstance(categories, list):
            categories = []
        normalized_categories = [str(item).strip() for item in categories if str(item).strip()]
        if not normalized_categories and not log.content_guard_reason:
            return None
        category = normalized_categories[0] if normalized_categories else "content_guard_summary"
        return dumps_sanitized([
            {
                "id": f"legacy_summary_{category}",
                "name": "历史请求摘要回填",
                "category": category,
                "match_type": "request_summary",
                "risk_level": log.content_guard_risk_level,
                "action": log.content_guard_action,
                "reason": log.content_guard_reason,
                "source": "request_log_summary_backfill",
            }
        ])

    @staticmethod
    def _latest_trace_item(trace_items: list[Any], event_name: str) -> dict[str, Any]:
        for item in reversed(trace_items or []):
            if not isinstance(item, dict):
                continue
            if item.get("typed_event") == event_name:
                return item
        return {}

    @staticmethod
    def _latest_content_guard_payload(
        trace_items: list[Any],
        *,
        response_body_json: str | None = None,
    ) -> dict[str, Any]:
        for item in reversed(trace_items or []):
            payload = RequestLogRecorder._content_guard_payload_from_trace_item(item)
            if payload:
                return payload
        error_body = safeJsonParse(response_body_json or "")
        if isinstance(error_body, dict):
            payload = RequestLogRecorder._content_guard_payload_from_error_body(error_body)
            if payload:
                return payload
        return {}

    @staticmethod
    def _content_guard_payload_from_trace_item(item: Any) -> dict[str, Any]:
        if not isinstance(item, dict):
            return {}
        payload = item.get("payload")
        if item.get("typed_event") == "request_content_guard" and isinstance(payload, dict):
            return dict(payload)
        if isinstance(payload, dict):
            if payload.get("guard_result") or payload.get("risk_level") or payload.get("guard_stage"):
                return dict(payload)
            nested = payload.get("content_guard")
            if isinstance(nested, dict):
                return RequestLogRecorder._normalize_content_guard_detail(nested, payload=payload)
            detail_payload = RequestLogRecorder._content_guard_payload_from_error_body(payload)
            if detail_payload:
                return detail_payload
        nested = item.get("content_guard")
        if isinstance(nested, dict):
            return RequestLogRecorder._normalize_content_guard_detail(nested, payload=item)
        return RequestLogRecorder._content_guard_payload_from_error_body(item)

    @staticmethod
    def _stream_payload_from_trace(log: RequestLog, trace_items: list[Any]) -> dict[str, Any]:
        for item in reversed(trace_items or []):
            if not isinstance(item, dict):
                continue
            if item.get("typed_event") == "request_stream" and isinstance(item.get("payload"), dict):
                return dict(item["payload"])
            payload = item.get("payload")
            if isinstance(payload, dict) and any(
                key in payload
                for key in ("stream_result", "sse_error_sent", "done_sent", "chunk_count", "captured_text_bytes")
            ):
                return dict(payload)
        results = [str(item.get("result") or "") for item in trace_items if isinstance(item, dict)]
        if log.status_code == 499 or any("disconnect" in item for item in results):
            return {"stream_result": "client_disconnected", "done_sent": False, "sse_error_sent": False}
        if any(item in {"empty_stream", "stream_empty"} for item in results):
            return {"stream_result": "empty_stream"}
        if any("timeout" in item for item in results):
            return {"stream_result": "timeout"}
        if any(item in {"stream_error", "upstream_error"} for item in results):
            return {"stream_result": "upstream_error"}
        return {}

    @staticmethod
    def _is_response_body_truncated(response_body_json: str | None) -> bool | None:
        if not response_body_json:
            return None
        parsed = safeJsonParse(response_body_json)
        if isinstance(parsed, dict):
            for key in ("truncated", "response_body_truncated", "is_truncated"):
                value = parsed.get(key)
                if isinstance(value, bool):
                    return value
            meta = parsed.get("metadata") or parsed.get("meta")
            if isinstance(meta, dict):
                for key in ("truncated", "response_body_truncated", "is_truncated"):
                    value = meta.get(key)
                    if isinstance(value, bool):
                        return value
        return "truncated" in response_body_json.lower()

    @staticmethod
    def _content_guard_payload_from_error_body(value: dict[str, Any]) -> dict[str, Any]:
        error = value.get("error") if isinstance(value, dict) else None
        detail = error.get("detail") if isinstance(error, dict) else None
        guard = detail.get("content_guard") if isinstance(detail, dict) else None
        if isinstance(guard, dict):
            return RequestLogRecorder._normalize_content_guard_detail(guard, payload=detail)
        guard = value.get("content_guard") if isinstance(value, dict) else None
        if isinstance(guard, dict):
            return RequestLogRecorder._normalize_content_guard_detail(guard, payload=value)
        return {}

    @staticmethod
    def _normalize_content_guard_detail(guard: dict[str, Any], *, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        payload = payload or {}
        categories = guard.get("categories")
        matched_rules = guard.get("matched_rules")
        return {
            "guard_stage": payload.get("guard_stage") or guard.get("guard_stage"),
            "guard_result": guard.get("result") or guard.get("guard_result"),
            "risk_level": guard.get("risk_level"),
            "matched_categories_json": dumps_sanitized(categories) if categories is not None else guard.get("matched_categories_json"),
            "matched_rules_json": dumps_sanitized(matched_rules) if matched_rules is not None else guard.get("matched_rules_json"),
            "reason": guard.get("reason"),
            "action": guard.get("action"),
            "excerpt": guard.get("excerpt"),
            "provider_status_after": payload.get("provider_status_after") or guard.get("provider_status_after"),
            "confidence": guard.get("confidence"),
            "score_delta": guard.get("score_delta"),
        }

    @staticmethod
    def _content_guard_stage_from_payload(log: RequestLog, payload: dict[str, Any]) -> str:
        stage = str(payload.get("guard_stage") or "").strip()
        if stage:
            return stage
        if not log.is_stream:
            return "non_stream_response"
        if log.content_guard_buffer_wait_ms is not None and int(log.content_guard_buffer_wait_ms or 0) > 0:
            return "stream_buffer"
        return "stream_chunk"

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
            if event_name == "request_content_guard":
                native_payload.setdefault("provider_id", log.provider_id)
                native_payload.setdefault("provider_name", log.provider_name)
                native_payload.setdefault("provider_model_id", log.resolved_provider_model_id)
                native_payload.setdefault("model_name", log.model_name)
                native_payload.setdefault("requested_model", log.requested_model)
                native_payload.setdefault("request_path", log.request_path)
                native_payload.setdefault("is_stream", log.is_stream)
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
