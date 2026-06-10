from __future__ import annotations

import logging

from sqlalchemy.orm import Session

from app.logging.envelope import LogEvent
from app.logging.registry import columns_for_model, filter_payload_for_model, model_for_event
from app.logging.sanitizers import dumps_sanitized
from app.models.logging_events import ExceptionEvent


logger = logging.getLogger(__name__)


class DatabaseLogSink:
    @staticmethod
    def write(db: Session, event: LogEvent, *, auto_commit: bool = True):
        model = model_for_event(event.envelope.event_name)
        if model is None:
            logger.warning("Unregistered typed log event ignored: %s", event.envelope.event_name)
            item = ExceptionEvent(
                event_id=event.envelope.event_id,
                event_type="logging",
                event_name="typed_log_unregistered_event",
                trace_id=event.envelope.trace_id,
                correlation_id=event.envelope.correlation_id,
                module=event.envelope.module or "logging",
                severity="warning",
                actor_type=event.envelope.actor_type,
                actor_id=event.envelope.actor_id,
                source_ip=event.envelope.source_ip,
                result="failed",
                exception_type="UnregisteredTypedLogEvent",
                error_code="typed_log_event_unregistered",
                message=f"未注册的类型化日志事件：{event.envelope.event_name}",
                detail_json=dumps_sanitized({
                    "event_name": event.envelope.event_name,
                    "event_type": event.envelope.event_type,
                    "payload_keys": sorted(event.payload.keys()),
                }),
                occurred_at=event.envelope.occurred_at,
            )
            db.add(item)
            if auto_commit:
                db.commit()
                db.refresh(item)
            return item
        allowed_columns = columns_for_model(model)
        payload = filter_payload_for_model(model, event.payload)
        extra_payload = {
            key: value
            for key, value in event.payload.items()
            if key not in allowed_columns and value is not None
        }
        if extra_payload and "diagnostics_json" in allowed_columns and not payload.get("diagnostics_json"):
            payload["diagnostics_json"] = dumps_sanitized(extra_payload)
        if hasattr(model, "event_id"):
            payload.setdefault("event_id", event.envelope.event_id)
        if hasattr(model, "event_type"):
            payload.setdefault("event_type", event.envelope.event_type)
        if hasattr(model, "event_name"):
            payload.setdefault("event_name", event.envelope.event_name)
        if hasattr(model, "trace_id") and event.envelope.trace_id:
            payload.setdefault("trace_id", event.envelope.trace_id)
        if hasattr(model, "correlation_id") and event.envelope.correlation_id:
            payload.setdefault("correlation_id", event.envelope.correlation_id)
        if hasattr(model, "module") and event.envelope.module:
            payload.setdefault("module", event.envelope.module)
        if hasattr(model, "severity"):
            payload.setdefault("severity", event.envelope.severity)
        if hasattr(model, "actor_type") and event.envelope.actor_type:
            payload.setdefault("actor_type", event.envelope.actor_type)
        if hasattr(model, "actor_id") and event.envelope.actor_id:
            payload.setdefault("actor_id", event.envelope.actor_id)
        if hasattr(model, "source_ip") and event.envelope.source_ip:
            payload.setdefault("source_ip", event.envelope.source_ip)
        if hasattr(model, "result"):
            payload.setdefault("result", event.envelope.result)
        if hasattr(model, "occurred_at"):
            payload.setdefault("occurred_at", event.envelope.occurred_at)
        item = model(**payload)
        db.add(item)
        if auto_commit:
            db.commit()
            db.refresh(item)
        return item
