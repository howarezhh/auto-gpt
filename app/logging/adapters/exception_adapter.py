from __future__ import annotations

import traceback
from typing import Any

from sqlalchemy.orm import Session

from app.logging.dispatcher import LoggingDispatcher
from app.logging.sanitizers import dumps_sanitized, stack_hash


class ExceptionLogRecorder:
    @staticmethod
    def record_exception(
        db: Session,
        *,
        exc: BaseException | None,
        handler_name: str,
        request_path: str | None,
        method: str | None,
        trace_id: str | None = None,
        status_code: int | None = None,
        error_code: str | None = None,
        message: str | None = None,
        source_ip: str | None = None,
        is_external_v1: bool = False,
        severity: str = "danger",
        detail: Any = None,
        auto_commit: bool = True,
    ):
        stack_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)) if exc is not None else None
        exception_type = exc.__class__.__name__ if exc is not None else "HandledException"
        event = LoggingDispatcher.build_event(
            event_type="exception",
            event_name="exception",
            trace_id=trace_id,
            module="exception_handler",
            severity=severity,
            source_ip=source_ip,
            result="failed",
            payload={
                "exception_type": exception_type,
                "status_code": status_code,
                "error_code": error_code,
                "message": (message or str(exc) if exc is not None else message)[:1000] if (message or exc) else None,
                "handler_name": handler_name,
                "request_path": request_path,
                "method": method,
                "is_external_v1": is_external_v1,
                "stack_hash": stack_hash(stack_text),
                "stack_excerpt": stack_text[:4000] if stack_text else None,
                "detail_json": dumps_sanitized(detail),
            },
        )
        return LoggingDispatcher.record(event, db=db, auto_commit=auto_commit)
