from __future__ import annotations

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.orm import Session

from app.models.admin_audit_log import AdminAuditLog
from app.services.cache_service import CacheService
from app.utils.json_utils import dumps_json, safeJsonParse


class AdminAuditService:
    AUDIT_JSON_MAX_BYTES = 65536

    @staticmethod
    def create_log(
        db: Session,
        *,
        actor_user_id: int | None,
        actor_username: str | None,
        action: str,
        entity_type: str,
        entity_id: int | str | None,
        entity_name: str | None,
        summary: str,
        detail: dict | list | str | None = None,
        target_user_id: int | None = None,
        request_trace_id: str | None = None,
        source_ip: str | None = None,
        before: dict | list | str | None = None,
        after: dict | list | str | None = None,
        changed_fields: dict | list | str | None = None,
        risk_level: str = "low",
        auto_commit: bool = True,
    ) -> AdminAuditLog:
        payload = AdminAuditService._bounded_json_text(detail)
        before_json = AdminAuditService._bounded_json_text(before)
        after_json = AdminAuditService._bounded_json_text(after)
        changed_fields_json = AdminAuditService._bounded_json_text(changed_fields)
        item = AdminAuditLog(
            actor_user_id=actor_user_id,
            actor_username=actor_username,
            action=action.strip(),
            entity_type=entity_type.strip(),
            entity_id=str(entity_id) if entity_id is not None else None,
            entity_name=(entity_name or "").strip() or None,
            target_user_id=target_user_id,
            request_trace_id=request_trace_id,
            source_ip=source_ip,
            summary=summary.strip(),
            before_json=before_json,
            after_json=after_json,
            changed_fields_json=changed_fields_json,
            risk_level=(risk_level or "low").strip() or "low",
            detail_json=payload,
        )
        db.add(item)
        if auto_commit:
            db.commit()
            db.refresh(item)
        return item

    @staticmethod
    def _bounded_json_text(value: dict | list | str | None) -> str | None:
        if value is None:
            return None
        text = value if isinstance(value, str) else dumps_json(value)
        encoded = text.encode("utf-8", errors="ignore")
        if len(encoded) <= AdminAuditService.AUDIT_JSON_MAX_BYTES:
            return text
        clipped = encoded[: AdminAuditService.AUDIT_JSON_MAX_BYTES].decode("utf-8", errors="ignore")
        return f"{clipped}...[truncated]"

    @staticmethod
    def list_logs(
        db: Session,
        *,
        keyword: str | None,
        action: str | None,
        entity_type: str | None,
        page: int,
        page_size: int,
    ) -> tuple[int, list[AdminAuditLog]]:
        stmt = select(AdminAuditLog)
        count_stmt = select(func.count()).select_from(AdminAuditLog)

        if keyword:
            like_value = f"%{keyword.strip().lower()}%"
            keyword_filter = or_(
                func.lower(AdminAuditLog.actor_username).like(like_value),
                func.lower(AdminAuditLog.entity_name).like(like_value),
                func.lower(AdminAuditLog.summary).like(like_value),
                cast(AdminAuditLog.entity_id, String).like(like_value),
            )
            stmt = stmt.where(keyword_filter)
            count_stmt = count_stmt.where(keyword_filter)
        if action:
            stmt = stmt.where(AdminAuditLog.action == action)
            count_stmt = count_stmt.where(AdminAuditLog.action == action)
        if entity_type:
            stmt = stmt.where(AdminAuditLog.entity_type == entity_type)
            count_stmt = count_stmt.where(AdminAuditLog.entity_type == entity_type)

        total = int(db.scalar(count_stmt) or 0)
        items = list(
            db.scalars(
                stmt.order_by(AdminAuditLog.created_at.desc(), AdminAuditLog.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        return total, items

    @staticmethod
    def get_filter_options(db: Session, *, limit: int = 200) -> dict[str, list[str]]:
        normalized_limit = max(1, min(int(limit or 200), 500))
        cache_key = f"admin-audit:filter-options:limit={normalized_limit}"
        cached = CacheService.get(cache_key)
        if isinstance(cached, dict):
            return cached
        actions = list(
            db.scalars(
                select(AdminAuditLog.action).distinct().order_by(AdminAuditLog.action.asc())
                .limit(normalized_limit)
            )
        )
        entity_types = list(
            db.scalars(
                select(AdminAuditLog.entity_type).distinct().order_by(AdminAuditLog.entity_type.asc())
                .limit(normalized_limit)
            )
        )
        result = {
            "actions": [item for item in actions if item],
            "entity_types": [item for item in entity_types if item],
        }
        return CacheService.set(cache_key, result, ttl_seconds=30)

    @staticmethod
    def serialize_detail(detail_json: str | None) -> str:
        if not detail_json:
            return "-"
        parsed = safeJsonParse(detail_json)
        if parsed is None:
            return detail_json
        return dumps_json(parsed, indent=2)
