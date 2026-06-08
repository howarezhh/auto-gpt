from __future__ import annotations

from datetime import datetime, timedelta

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.models.ip_management import IpManagementEvent, IpManagementSetting
from app.services.ip_management_resolver_service import ClientIpResolution
from app.utils.json_utils import dumps_json


class IpManagementEventService:
    @staticmethod
    def mask_ip(ip_value: str | None) -> str | None:
        if not ip_value:
            return ip_value
        if "." in ip_value:
            parts = ip_value.split(".")
            if len(parts) == 4:
                return ".".join(parts[:3] + ["0"])
        if ":" in ip_value:
            parts = ip_value.split(":")
            return ":".join(parts[:4] + ["0000"] * max(0, 8 - len(parts[:4])))
        return ip_value

    @staticmethod
    def create_event(
        db: Session,
        *,
        setting: IpManagementSetting,
        resolution: ClientIpResolution,
        trace_id: str | None,
        request_path: str | None,
        http_method: str | None,
        scope: str | None,
        matched_rule_id: int | None,
        matched_rule_name: str | None,
        decision: str,
        decision_reason: str,
        enforced: bool,
        status_code: int | None,
    ) -> IpManagementEvent | None:
        if not setting.event_logging_enabled:
            return None
        display_ip = IpManagementEventService.mask_ip(resolution.resolved_client_ip) if setting.ip_masking_enabled else resolution.resolved_client_ip
        event = IpManagementEvent(
            trace_id=trace_id,
            request_path=request_path,
            http_method=http_method,
            scope=scope,
            direct_client_ip=resolution.direct_client_ip,
            resolved_client_ip=resolution.resolved_client_ip,
            display_client_ip=display_ip,
            resolution_source=resolution.resolution_source,
            resolution_status=resolution.resolution_status,
            trusted_proxy_matched=resolution.trusted_proxy_matched,
            forwarded_chain_json=dumps_json(
                {
                    "chain": resolution.forwarded_chain,
                    "ignored_headers": resolution.ignored_headers,
                    "warnings": resolution.warnings,
                }
            ),
            matched_rule_id=matched_rule_id,
            matched_rule_name=matched_rule_name,
            decision=decision,
            decision_reason=decision_reason,
            enforced=enforced,
            status_code=status_code,
        )
        db.add(event)
        db.commit()
        db.refresh(event)
        return event

    @staticmethod
    def list_events(
        db: Session,
        *,
        keyword: str | None = None,
        ip: str | None = None,
        decision: str | None = None,
        scope: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[int, list[IpManagementEvent]]:
        stmt = select(IpManagementEvent)
        count_stmt = select(func.count()).select_from(IpManagementEvent)
        filters = []
        if keyword:
            like_value = f"%{keyword.strip().lower()}%"
            filters.append(
                or_(
                    func.lower(IpManagementEvent.request_path).like(like_value),
                    func.lower(IpManagementEvent.trace_id).like(like_value),
                    func.lower(IpManagementEvent.matched_rule_name).like(like_value),
                )
            )
        if ip:
            filters.append(IpManagementEvent.resolved_client_ip == ip.strip())
        if decision:
            filters.append(IpManagementEvent.decision == decision.strip())
        if scope:
            filters.append(IpManagementEvent.scope == scope.strip())
        for item in filters:
            stmt = stmt.where(item)
            count_stmt = count_stmt.where(item)
        total = int(db.scalar(count_stmt) or 0)
        rows = list(
            db.scalars(
                stmt.order_by(IpManagementEvent.created_at.desc(), IpManagementEvent.id.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            )
        )
        return total, rows

    @staticmethod
    def cleanup_old_events(db: Session, *, retention_days: int) -> int:
        cutoff = datetime.utcnow() - timedelta(days=max(1, int(retention_days or 30)))
        rows = list(db.scalars(select(IpManagementEvent).where(IpManagementEvent.created_at < cutoff).limit(5000)))
        for row in rows:
            db.delete(row)
        db.commit()
        return len(rows)
