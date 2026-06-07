from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session


class AuditLogRecorder:
    @staticmethod
    def record_admin_action(db: Session, **kwargs: Any):
        from app.services.admin_audit_service import AdminAuditService

        return AdminAuditService.create_log(db, **kwargs)
