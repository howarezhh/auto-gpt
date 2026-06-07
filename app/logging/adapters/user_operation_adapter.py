from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.logging.dispatcher import LoggingDispatcher
from app.logging.sanitizers import dumps_sanitized


class UserOperationLogRecorder:
    @staticmethod
    def record_user_action(db: Session, *, user_account_id: int | None, username: str | None, action: str, entity_type: str, entity_id: int | str | None = None, entity_name: str | None = None, summary: str, detail: Any = None, source_ip: str | None = None, trace_id: str | None = None, result: str = "success", auto_commit: bool = True):
        event = LoggingDispatcher.build_event(
            event_type="user_operation",
            event_name="user_operation",
            trace_id=trace_id,
            actor_type="user",
            actor_id=str(user_account_id) if user_account_id is not None else None,
            source_ip=source_ip,
            module="user_portal",
            result=result,
            payload={
                "user_account_id": user_account_id,
                "username": username,
                "action": action,
                "entity_type": entity_type,
                "entity_id": str(entity_id) if entity_id is not None else None,
                "entity_name": entity_name,
                "summary": summary,
                "detail_json": dumps_sanitized(detail),
                "source_ip": source_ip,
                "trace_id": trace_id,
                "result": result,
            },
        )
        return LoggingDispatcher.record(event, db=db, auto_commit=auto_commit)
