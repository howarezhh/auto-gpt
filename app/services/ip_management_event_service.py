from __future__ import annotations

from datetime import datetime, timedelta
import hashlib

from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session

from app.models.ip_management import IpManagementEvent, IpManagementSetting
from app.services.ip_management_resolver_service import ClientIpResolution
from app.services.proxy_request_context import set_current_ip_management_event_id
from app.utils.json_utils import dumps_json
from app.utils.timezone import now_beijing


class IpManagementEventService:
    CLEANUP_BATCH_SIZE = 5000
    CLEANUP_MAX_BATCHES = 100

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
        sample_rate = max(0, min(100, int(setting.event_sample_rate or 0)))
        if sample_rate <= 0:
            return None
        if sample_rate < 100:
            sample_seed = "|".join(
                [
                    trace_id or "",
                    request_path or "",
                    scope or "",
                    resolution.resolved_client_ip or "",
                    decision,
                    decision_reason or "",
                ]
            )
            sample_bucket = int(hashlib.sha256(sample_seed.encode("utf-8")).hexdigest()[:8], 16) % 100
            if sample_bucket >= sample_rate:
                return None
        if trace_id:
            exists = db.scalar(
                select(IpManagementEvent.id)
                .where(
                    IpManagementEvent.trace_id == trace_id,
                    IpManagementEvent.request_path == request_path,
                    IpManagementEvent.http_method == http_method,
                    IpManagementEvent.scope == scope,
                    IpManagementEvent.resolved_client_ip == resolution.resolved_client_ip,
                    IpManagementEvent.direct_client_ip == resolution.direct_client_ip,
                    IpManagementEvent.resolution_source == resolution.resolution_source,
                    IpManagementEvent.resolution_status == resolution.resolution_status,
                    IpManagementEvent.decision == decision,
                    IpManagementEvent.decision_reason == decision_reason,
                    IpManagementEvent.matched_rule_id == matched_rule_id,
                    IpManagementEvent.enforced.is_(enforced),
                    IpManagementEvent.status_code == status_code,
                )
                .limit(1)
            )
            if exists:
                return None
        display_ip = IpManagementEventService.mask_ip(resolution.resolved_client_ip) if setting.ip_masking_enabled else resolution.resolved_client_ip
        forwarded_chain_payload = {
            "chain": resolution.forwarded_chain if setting.store_raw_headers_enabled else [],
            "chain_count": len(resolution.forwarded_chain),
            "ignored_headers": resolution.ignored_headers,
            "warnings": resolution.warnings,
            "raw_header_summary_stored": bool(setting.store_raw_headers_enabled),
        }
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
            forwarded_chain_json=dumps_json(forwarded_chain_payload),
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
        set_current_ip_management_event_id(event.id)
        return event

    @staticmethod
    def attach_request_context(
        db: Session,
        *,
        event_id: int | None,
        request_log_id: int | None,
        api_client_key_id: int | None = None,
        api_client_key_prefix: str | None = None,
        user_account_id: int | None = None,
    ) -> bool:
        if not event_id or not request_log_id:
            return False
        event = db.get(IpManagementEvent, int(event_id))
        if event is None:
            return False
        if event.request_log_id is not None and event.request_log_id != request_log_id:
            return False
        event.request_log_id = int(request_log_id)
        event.api_client_key_id = api_client_key_id
        event.api_client_key_prefix = api_client_key_prefix
        event.user_account_id = user_account_id
        db.flush()
        return True

    @staticmethod
    def list_events(
        db: Session,
        *,
        keyword: str | None = None,
        ip: str | None = None,
        decision: str | None = None,
        scope: str | None = None,
        status_code: int | None = None,
        api_key: str | None = None,
        request_log_id: int | None = None,
        user_account_id: int | None = None,
        started_at: datetime | None = None,
        ended_at: datetime | None = None,
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
        if status_code is not None:
            filters.append(IpManagementEvent.status_code == status_code)
        if api_key:
            like_value = f"%{api_key.strip().lower()}%"
            filters.append(func.lower(IpManagementEvent.api_client_key_prefix).like(like_value))
        if request_log_id is not None:
            filters.append(IpManagementEvent.request_log_id == request_log_id)
        if user_account_id is not None:
            filters.append(IpManagementEvent.user_account_id == user_account_id)
        if started_at is not None:
            filters.append(IpManagementEvent.created_at >= started_at)
        if ended_at is not None:
            filters.append(IpManagementEvent.created_at <= ended_at)
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
        cutoff = now_beijing() - timedelta(days=max(1, int(retention_days or 30)))
        total_deleted = 0
        for _ in range(IpManagementEventService.CLEANUP_MAX_BATCHES):
            ids = list(
                db.scalars(
                    select(IpManagementEvent.id)
                    .where(IpManagementEvent.created_at < cutoff)
                    .order_by(IpManagementEvent.id.asc())
                    .limit(IpManagementEventService.CLEANUP_BATCH_SIZE)
                )
            )
            if not ids:
                break
            result = db.execute(delete(IpManagementEvent).where(IpManagementEvent.id.in_(ids)))
            db.commit()
            total_deleted += int(result.rowcount or 0)
            if len(ids) < IpManagementEventService.CLEANUP_BATCH_SIZE:
                break
        return total_deleted
