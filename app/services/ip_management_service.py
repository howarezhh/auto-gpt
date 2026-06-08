from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from fastapi import Request
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.ip_management import IpAccessRule, IpManagementEvent, IpManagementSetting
from app.schemas.ip_management import IpAccessRuleCreate, IpAccessRuleUpdate, IpManagementSettingsUpdate
from app.services.ip_management_event_service import IpManagementEventService
from app.services.ip_management_rate_limit_service import IpManagementRateLimitService
from app.services.ip_management_resolver_service import ClientIpResolution, ClientIpResolver
from app.services.ip_management_rule_service import IpManagementRuleService
from app.services.rate_limit_service import RateLimitExceededError
from app.utils.json_utils import dumps_json, loads_json, safeJsonParse


@dataclass(frozen=True)
class IpManagementDecision:
    resolution: ClientIpResolution
    scope: str | None
    decision: str
    status_code: int | None
    error_code: str | None
    message: str | None
    matched_rule_id: int | None
    matched_rule_name: str | None
    enforced: bool
    reason: str

    @property
    def should_block(self) -> bool:
        return self.enforced and self.status_code is not None and self.error_code is not None


class IpManagementService:
    _cached_setting: IpManagementSetting | None = None

    @staticmethod
    def invalidate_cache() -> None:
        IpManagementService._cached_setting = None

    @staticmethod
    def get_or_create_setting(db: Session) -> IpManagementSetting:
        setting = db.get(IpManagementSetting, 1)
        if setting is None:
            setting = IpManagementSetting(id=1)
            db.add(setting)
            db.commit()
            db.refresh(setting)
        return setting

    @staticmethod
    def get_cached_setting(db: Session) -> IpManagementSetting:
        if IpManagementService._cached_setting is None:
            IpManagementService._cached_setting = IpManagementService.get_or_create_setting(db)
        return IpManagementService._cached_setting

    @staticmethod
    def serialize_setting(setting: IpManagementSetting) -> dict[str, Any]:
        return {
            "enabled": bool(setting.enabled),
            "observe_only_enabled": bool(setting.observe_only_enabled),
            "trusted_proxy_resolution_enabled": bool(setting.trusted_proxy_resolution_enabled),
            "trusted_proxy_cidrs": loads_json(setting.trusted_proxy_cidrs_json, []),
            "trusted_header_order": loads_json(setting.trusted_header_order_json, []),
            "apply_external_v1_enabled": bool(setting.apply_external_v1_enabled),
            "apply_internal_api_enabled": bool(setting.apply_internal_api_enabled),
            "apply_user_pages_enabled": bool(setting.apply_user_pages_enabled),
            "rule_engine_enabled": bool(setting.rule_engine_enabled),
            "block_action_enabled": bool(setting.block_action_enabled),
            "rate_limit_enabled": bool(setting.rate_limit_enabled),
            "event_logging_enabled": bool(setting.event_logging_enabled),
            "event_sample_rate": int(setting.event_sample_rate or 0),
            "event_retention_days": int(setting.event_retention_days or 30),
            "store_raw_headers_enabled": bool(setting.store_raw_headers_enabled),
            "ip_masking_enabled": bool(setting.ip_masking_enabled),
            "fail_open_enabled": bool(setting.fail_open_enabled),
            "updated_at": setting.updated_at,
        }

    @staticmethod
    def update_setting(db: Session, payload: IpManagementSettingsUpdate) -> IpManagementSetting:
        for cidr in payload.trusted_proxy_cidrs:
            ClientIpResolver.parse_networks([cidr])
            networks, warnings = ClientIpResolver.parse_networks([cidr])
            if not networks or warnings:
                raise ValueError(f"可信代理 CIDR 无效: {cidr}")
        setting = IpManagementService.get_or_create_setting(db)
        data = payload.model_dump()
        setting.trusted_proxy_cidrs_json = dumps_json(data.pop("trusted_proxy_cidrs"))
        setting.trusted_header_order_json = dumps_json(data.pop("trusted_header_order"))
        for field, value in data.items():
            setattr(setting, field, value)
        db.commit()
        db.refresh(setting)
        IpManagementService.invalidate_cache()
        return setting

    @staticmethod
    def resolve_scope(path: str) -> str | None:
        if path == "/v1" or path.startswith("/v1/"):
            return "external_v1"
        if path == "/api" or path.startswith("/api/"):
            return "internal_api"
        if path == "/user" or path.startswith("/user/"):
            return "user_pages"
        return None

    @staticmethod
    def scope_enabled(setting: IpManagementSetting, scope: str | None) -> bool:
        if scope == "external_v1":
            return bool(setting.apply_external_v1_enabled)
        if scope == "internal_api":
            return bool(setting.apply_internal_api_enabled)
        if scope == "user_pages":
            return bool(setting.apply_user_pages_enabled)
        return False

    @staticmethod
    async def evaluate_request(db: Session, request: Request) -> IpManagementDecision | None:
        setting = IpManagementService.get_cached_setting(db)
        if not setting.enabled:
            return None
        scope = IpManagementService.resolve_scope(request.url.path)
        if not IpManagementService.scope_enabled(setting, scope):
            return None
        resolution = ClientIpResolver.resolve_request(
            request,
            trusted_proxy_resolution_enabled=bool(setting.trusted_proxy_resolution_enabled),
            trusted_proxy_cidrs=loads_json(setting.trusted_proxy_cidrs_json, []),
            trusted_header_order=loads_json(setting.trusted_header_order_json, []),
        )
        rules = IpManagementService.enabled_rules(db) if setting.rule_engine_enabled else []
        match = IpManagementRuleService.match(resolution.resolved_client_ip, scope=scope or "", rules=rules)
        decision = match.action
        status_code = None
        error_code = None
        message = None
        enforced = False
        reason = match.reason
        if decision == "block":
            enforced = bool(setting.block_action_enabled and not setting.observe_only_enabled)
            if enforced:
                status_code = 403
                error_code = "source_ip_blocked"
                message = "当前来源 IP 已被 IP 管理规则拦截。"
        elif decision == "rate_limit":
            qps = int(match.rule.rate_qps_limit if match.rule is not None else 0)
            rpm = int(match.rule.rate_rpm_limit if match.rule is not None else 0)
            if setting.rate_limit_enabled and not setting.observe_only_enabled:
                try:
                    await IpManagementRateLimitService.check_ip_limits(
                        resolved_ip=resolution.resolved_client_ip,
                        scope=scope or "unknown",
                        qps_limit=qps,
                        rpm_limit=rpm,
                    )
                except RateLimitExceededError as exc:
                    enforced = True
                    status_code = 429
                    error_code = exc.code
                    message = exc.message
                    reason = f"rate_limit_exceeded:{exc.key}"
        result = IpManagementDecision(
            resolution=resolution,
            scope=scope,
            decision=decision,
            status_code=status_code,
            error_code=error_code,
            message=message,
            matched_rule_id=match.rule.id if match.rule is not None else None,
            matched_rule_name=match.rule.name if match.rule is not None else None,
            enforced=enforced,
            reason=reason,
        )
        IpManagementEventService.create_event(
            db,
            setting=setting,
            resolution=resolution,
            trace_id=getattr(request.state, "trace_id", None),
            request_path=request.url.path,
            http_method=request.method.upper(),
            scope=scope,
            matched_rule_id=result.matched_rule_id,
            matched_rule_name=result.matched_rule_name,
            decision=result.decision,
            decision_reason=result.reason,
            enforced=result.enforced,
            status_code=result.status_code,
        )
        return result

    @staticmethod
    def enabled_rules(db: Session) -> list[IpAccessRule]:
        return list(db.scalars(select(IpAccessRule).where(IpAccessRule.enabled.is_(True)).order_by(IpAccessRule.priority.asc(), IpAccessRule.id.asc())))

    @staticmethod
    def create_rule(db: Session, payload: IpAccessRuleCreate, *, username: str | None, user_id: int | None) -> IpAccessRule:
        IpManagementRuleService.validate_rule_fields(scope=payload.scope, match_type=payload.match_type, action=payload.action)
        normalized = IpManagementRuleService.normalize_rule_value(payload.match_type, payload.match_value)
        rule = IpAccessRule(
            name=payload.name,
            enabled=payload.enabled,
            priority=payload.priority,
            scope=payload.scope,
            match_type=payload.match_type,
            match_value=payload.match_value,
            normalized_value=normalized,
            action=payload.action,
            rate_qps_limit=payload.rate_qps_limit,
            rate_rpm_limit=payload.rate_rpm_limit,
            expires_at=payload.expires_at,
            reason=payload.reason,
            created_by_user_id=user_id,
            created_by_username=username,
        )
        db.add(rule)
        db.commit()
        db.refresh(rule)
        return rule

    @staticmethod
    def update_rule(db: Session, rule_id: int, payload: IpAccessRuleUpdate) -> IpAccessRule:
        rule = db.get(IpAccessRule, rule_id)
        if rule is None:
            raise ValueError("IP 规则不存在")
        IpManagementRuleService.validate_rule_fields(scope=payload.scope, match_type=payload.match_type, action=payload.action)
        normalized = IpManagementRuleService.normalize_rule_value(payload.match_type, payload.match_value)
        for field, value in payload.model_dump().items():
            setattr(rule, field, value)
        rule.normalized_value = normalized
        db.commit()
        db.refresh(rule)
        return rule

    @staticmethod
    def list_rules(db: Session, *, keyword: str | None, action: str | None, scope: str | None, enabled: bool | None, page: int, page_size: int) -> tuple[int, list[IpAccessRule]]:
        stmt = select(IpAccessRule)
        count_stmt = select(func.count()).select_from(IpAccessRule)
        filters = []
        if keyword:
            like_value = f"%{keyword.strip().lower()}%"
            filters.append(func.lower(IpAccessRule.name).like(like_value))
        if action:
            filters.append(IpAccessRule.action == action)
        if scope:
            filters.append(IpAccessRule.scope == scope)
        if enabled is not None:
            filters.append(IpAccessRule.enabled.is_(enabled))
        for item in filters:
            stmt = stmt.where(item)
            count_stmt = count_stmt.where(item)
        total = int(db.scalar(count_stmt) or 0)
        rows = list(db.scalars(stmt.order_by(IpAccessRule.priority.asc(), IpAccessRule.id.asc()).offset((page - 1) * page_size).limit(page_size)))
        return total, rows

    @staticmethod
    def serialize_rule(rule: IpAccessRule) -> dict[str, Any]:
        return {
            "id": rule.id,
            "name": rule.name,
            "enabled": rule.enabled,
            "priority": rule.priority,
            "scope": rule.scope,
            "match_type": rule.match_type,
            "match_value": rule.match_value,
            "normalized_value": rule.normalized_value,
            "action": rule.action,
            "rate_qps_limit": rule.rate_qps_limit,
            "rate_rpm_limit": rule.rate_rpm_limit,
            "expires_at": rule.expires_at,
            "reason": rule.reason,
            "created_by_username": rule.created_by_username,
            "created_at": rule.created_at,
            "updated_at": rule.updated_at,
        }

    @staticmethod
    def serialize_event(event: IpManagementEvent) -> dict[str, Any]:
        return {
            "id": event.id,
            "trace_id": event.trace_id,
            "request_path": event.request_path,
            "http_method": event.http_method,
            "scope": event.scope,
            "direct_client_ip": event.direct_client_ip,
            "resolved_client_ip": event.resolved_client_ip,
            "display_client_ip": event.display_client_ip,
            "resolution_source": event.resolution_source,
            "resolution_status": event.resolution_status,
            "trusted_proxy_matched": event.trusted_proxy_matched,
            "forwarded_chain": safeJsonParse(event.forwarded_chain_json or "{}"),
            "matched_rule_id": event.matched_rule_id,
            "matched_rule_name": event.matched_rule_name,
            "decision": event.decision,
            "decision_reason": event.decision_reason,
            "enforced": event.enforced,
            "status_code": event.status_code,
            "created_at": event.created_at,
        }

    @staticmethod
    def build_overview(db: Session) -> dict[str, Any]:
        setting = IpManagementService.get_or_create_setting(db)
        since = datetime.utcnow() - timedelta(hours=1)
        total_events = int(db.scalar(select(func.count()).select_from(IpManagementEvent)) or 0)
        recent_blocked = int(db.scalar(select(func.count()).select_from(IpManagementEvent).where(IpManagementEvent.created_at >= since, IpManagementEvent.decision == "block")) or 0)
        recent_limited = int(db.scalar(select(func.count()).select_from(IpManagementEvent).where(IpManagementEvent.created_at >= since, IpManagementEvent.decision == "rate_limit")) or 0)
        trusted_hits = int(db.scalar(select(func.count()).select_from(IpManagementEvent).where(IpManagementEvent.created_at >= since, IpManagementEvent.trusted_proxy_matched.is_(True))) or 0)
        invalid_ignored = int(db.scalar(select(func.count()).select_from(IpManagementEvent).where(IpManagementEvent.created_at >= since, IpManagementEvent.resolution_status == "invalid_header_ignored")) or 0)
        rule_count = int(db.scalar(select(func.count()).select_from(IpAccessRule)) or 0)
        enabled_rule_count = int(db.scalar(select(func.count()).select_from(IpAccessRule).where(IpAccessRule.enabled.is_(True))) or 0)
        return {
            "settings": IpManagementService.serialize_setting(setting),
            "summary": {
                "event_count": total_events,
                "rule_count": rule_count,
                "enabled_rule_count": enabled_rule_count,
                "recent_block_count": recent_blocked,
                "recent_rate_limit_count": recent_limited,
                "recent_trusted_proxy_hit_count": trusted_hits,
                "recent_invalid_header_ignored_count": invalid_ignored,
            },
        }
