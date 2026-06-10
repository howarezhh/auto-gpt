from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy.orm import Session

from app.logging.dispatcher import LoggingDispatcher
from app.logging.sanitizers import dumps_sanitized


class BackgroundJobLogRecorder:
    RESULT_SUMMARY_MAX_BYTES = 8192
    RESULT_BY_STATUS = {
        "failed": "failed",
        "cancelled": "failed",
        "running": "running",
        "stale_running": "failed",
        "skipped": "skipped",
        "skipped_locked": "skipped",
        "skipped_lock_unavailable": "warning",
    }

    @staticmethod
    def new_run_id() -> str:
        return uuid4().hex

    @staticmethod
    def record_job_event(db: Session, *, job_run_id: str, job_name: str, trigger_type: str = "scheduler", lock_key: str | None = None, lock_status: str | None = None, status: str, started_at: datetime | None = None, finished_at: datetime | None = None, duration_ms: int | None = None, processed_count: int | None = None, success_count: int | None = None, failed_count: int | None = None, result_summary: Any = None, error: str | None = None, auto_commit: bool = True):
        result = BackgroundJobLogRecorder.RESULT_BY_STATUS.get(status, "success")
        if lock_status in {"unavailable", "unavailable_fallback", "unavailable_skipped"} and result == "success":
            result = "warning"
        event = LoggingDispatcher.build_event(
            event_type="background_job",
            event_name="background_job",
            correlation_id=job_run_id,
            module="scheduler",
            result=result,
            payload={
                "job_run_id": job_run_id,
                "job_name": job_name,
                "trigger_type": trigger_type,
                "lock_key": lock_key,
                "lock_status": lock_status,
                "status": status,
                "started_at": started_at,
                "finished_at": finished_at,
                "duration_ms": duration_ms,
                "processed_count": processed_count,
                "success_count": success_count,
                "failed_count": failed_count,
                "result_summary_json": dumps_sanitized(
                    result_summary,
                    max_string_length=500,
                    max_bytes=BackgroundJobLogRecorder.RESULT_SUMMARY_MAX_BYTES,
                    max_list_items=50,
                ),
                "error": error,
            },
        )
        return LoggingDispatcher.record(event, db=db, auto_commit=auto_commit)
