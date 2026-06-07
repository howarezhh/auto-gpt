from __future__ import annotations

from sqlalchemy.orm import Session

from app.logging.envelope import LogEvent
from app.logging.registry import filter_payload_for_model, model_for_event


class DatabaseLogSink:
    @staticmethod
    def write(db: Session, event: LogEvent, *, auto_commit: bool = True):
        model = model_for_event(event.envelope.event_name)
        if model is None:
            return None
        payload = filter_payload_for_model(model, event.payload)
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
