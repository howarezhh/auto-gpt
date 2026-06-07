from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.logging.envelope import LogEnvelope, LogEvent
from app.logging.queue import LoggingQueue
from app.logging.sinks import DatabaseLogSink


class LoggingDispatcher:
    @staticmethod
    def record(event: LogEvent, *, db: Session | None = None, enqueue: bool = False, auto_commit: bool = True):
        if enqueue and LoggingQueue.enqueue(event.to_queue_payload()):
            return None
        if db is not None:
            return DatabaseLogSink.write(db, event, auto_commit=auto_commit)
        session = SessionLocal()
        try:
            return DatabaseLogSink.write(session, event, auto_commit=True)
        finally:
            session.close()

    @staticmethod
    def record_many(events: list[LogEvent], *, db: Session | None = None, enqueue: bool = False, auto_commit: bool = True) -> None:
        if enqueue:
            not_queued = [event for event in events if not LoggingQueue.enqueue(event.to_queue_payload())]
            events = not_queued
        if not events:
            return
        if db is not None:
            for event in events:
                DatabaseLogSink.write(db, event, auto_commit=False)
            if auto_commit:
                db.commit()
            return
        session = SessionLocal()
        try:
            for event in events:
                DatabaseLogSink.write(session, event, auto_commit=False)
            session.commit()
        finally:
            session.close()

    @staticmethod
    def build_event(
        *,
        event_type: str,
        event_name: str,
        payload: dict[str, Any],
        trace_id: str | None = None,
        correlation_id: str | None = None,
        module: str | None = None,
        severity: str = "info",
        actor_type: str | None = None,
        actor_id: str | None = None,
        source_ip: str | None = None,
        result: str = "success",
    ) -> LogEvent:
        return LogEvent(
            envelope=LogEnvelope(
                event_type=event_type,
                event_name=event_name,
                trace_id=trace_id,
                correlation_id=correlation_id,
                module=module,
                severity=severity,
                actor_type=actor_type,
                actor_id=actor_id,
                source_ip=source_ip,
                result=result,
            ),
            payload=payload,
        )
