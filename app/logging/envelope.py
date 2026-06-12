from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import uuid4

from app.utils.timezone import now_beijing


@dataclass(slots=True)
class LogEnvelope:
    event_type: str
    event_name: str
    trace_id: str | None = None
    correlation_id: str | None = None
    module: str | None = None
    severity: str = "info"
    actor_type: str | None = None
    actor_id: str | None = None
    source_ip: str | None = None
    result: str = "success"
    occurred_at: datetime = field(default_factory=now_beijing)
    event_id: str = field(default_factory=lambda: uuid4().hex)

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "event_name": self.event_name,
            "trace_id": self.trace_id,
            "correlation_id": self.correlation_id,
            "module": self.module,
            "severity": self.severity,
            "actor_type": self.actor_type,
            "actor_id": self.actor_id,
            "source_ip": self.source_ip,
            "result": self.result,
            "occurred_at": self.occurred_at,
        }


@dataclass(slots=True)
class LogEvent:
    envelope: LogEnvelope
    payload: dict[str, Any]

    def to_queue_payload(self) -> dict[str, Any]:
        return {
            "envelope": self.envelope.to_dict(),
            "payload": self.payload,
        }
