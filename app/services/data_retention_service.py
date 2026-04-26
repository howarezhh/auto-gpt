from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models.admin_audit_log import AdminAuditLog
from app.models.request_log import RequestLog


class DataRetentionService:
    @staticmethod
    def cleanup(db: Session, *, request_log_retention_days: int, admin_audit_log_retention_days: int) -> dict[str, int]:
        result = {
            "request_logs_deleted": 0,
            "admin_audit_logs_deleted": 0,
        }
        changed = False

        if request_log_retention_days > 0:
            request_cutoff = datetime.utcnow() - timedelta(days=request_log_retention_days)
            request_delete = db.execute(
                delete(RequestLog).where(RequestLog.created_at < request_cutoff)
            )
            result["request_logs_deleted"] = int(request_delete.rowcount or 0)
            changed = changed or result["request_logs_deleted"] > 0

        if admin_audit_log_retention_days > 0:
            audit_cutoff = datetime.utcnow() - timedelta(days=admin_audit_log_retention_days)
            audit_delete = db.execute(
                delete(AdminAuditLog).where(AdminAuditLog.created_at < audit_cutoff)
            )
            result["admin_audit_logs_deleted"] = int(audit_delete.rowcount or 0)
            changed = changed or result["admin_audit_logs_deleted"] > 0

        if changed:
            db.commit()
        else:
            db.rollback()
        return result
