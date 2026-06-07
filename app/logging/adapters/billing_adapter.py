from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.logging.dispatcher import LoggingDispatcher
from app.logging.sanitizers import dumps_sanitized


class BillingLogRecorder:
    @staticmethod
    def record_token_finalize(db: Session, *, request_log_id: int | None, queue_source: str, attempt_count: int = 0, usage_before: Any = None, usage_after: Any = None, token_source: str | None = None, enable_usage_fill: bool | None = None, result: str, error: str | None = None, auto_commit: bool = True):
        event = LoggingDispatcher.build_event(
            event_type="billing",
            event_name="token_finalize",
            correlation_id=str(request_log_id) if request_log_id is not None else None,
            module="billing",
            result="failed" if result == "failed" else "success",
            payload={
                "request_log_id": request_log_id,
                "queue_source": queue_source,
                "attempt_count": attempt_count,
                "usage_before_json": dumps_sanitized(usage_before),
                "usage_after_json": dumps_sanitized(usage_after),
                "token_source": token_source,
                "enable_usage_fill": enable_usage_fill,
                "result": result,
                "error": error,
            },
        )
        return LoggingDispatcher.record(event, db=db, auto_commit=auto_commit)

    @staticmethod
    def record_billing_process(db: Session, *, request_log_id: int | None, api_client_key_id: int | None, user_account_id: int | None, pricing_source: str | None = None, pricing_snapshot: Any = None, cost_snapshot: Any = None, balance_delta: float | None = None, balance_after: float | None = None, billing_status: str, billing_record_id: int | None = None, error: str | None = None, auto_commit: bool = True):
        event = LoggingDispatcher.build_event(
            event_type="billing",
            event_name="billing_process",
            correlation_id=str(request_log_id) if request_log_id is not None else None,
            module="billing",
            result="failed" if billing_status == "failed" else "success",
            payload={
                "request_log_id": request_log_id,
                "api_client_key_id": api_client_key_id,
                "user_account_id": user_account_id,
                "pricing_source": pricing_source,
                "pricing_snapshot_json": dumps_sanitized(pricing_snapshot),
                "cost_snapshot_json": dumps_sanitized(cost_snapshot),
                "balance_delta": balance_delta,
                "balance_after": balance_after,
                "billing_status": billing_status,
                "billing_record_id": billing_record_id,
                "error": error,
            },
        )
        return LoggingDispatcher.record(event, db=db, auto_commit=auto_commit)
