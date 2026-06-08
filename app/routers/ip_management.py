from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.ip_management import IpAccessRule
from app.schemas.ip_management import (
    IpAccessRuleCreate,
    IpAccessRuleUpdate,
    IpManagementSettingsUpdate,
    IpResolutionTestRequest,
    IpRuleTestRequest,
)
from app.services.admin_audit_service import AdminAuditService
from app.services.ip_management_event_service import IpManagementEventService
from app.services.ip_management_resolver_service import ClientIpResolver
from app.services.ip_management_rule_service import IpManagementRuleService
from app.services.ip_management_service import IpManagementService
from app.services.user_auth_service import require_admin_api_user

router = APIRouter(prefix="/api/ip-management", tags=["ip-management"])


@router.get("/overview")
def ip_management_overview(db: Session = Depends(get_db)) -> dict:
    return IpManagementService.build_overview(db)


@router.get("/settings")
def get_ip_management_settings(db: Session = Depends(get_db)) -> dict:
    return {"settings": IpManagementService.serialize_setting(IpManagementService.get_or_create_setting(db))}


@router.put("/settings")
def update_ip_management_settings(
    payload: IpManagementSettingsUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> dict:
    try:
        setting = IpManagementService.update_setting(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="update",
        entity_type="ip_management",
        entity_id=setting.id,
        entity_name="ip_management_settings",
        summary="更新 IP 管理设置",
        detail=payload.model_dump(),
    )
    return {"settings": IpManagementService.serialize_setting(setting)}


@router.get("/rules")
def list_ip_rules(
    keyword: str | None = None,
    action: str | None = None,
    scope: str | None = None,
    enabled: bool | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    total, rows = IpManagementService.list_rules(db, keyword=keyword, action=action, scope=scope, enabled=enabled, page=page, page_size=page_size)
    return {"total": total, "items": [IpManagementService.serialize_rule(item) for item in rows], "page": page, "page_size": page_size}


@router.post("/rules")
def create_ip_rule(
    payload: IpAccessRuleCreate,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> dict:
    try:
        rule = IpManagementService.create_rule(db, payload, username=current_user.username, user_id=current_user.id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="create",
        entity_type="ip_access_rule",
        entity_id=rule.id,
        entity_name=rule.name,
        summary=f"创建 IP 规则 {rule.name}",
        detail=IpManagementService.serialize_rule(rule),
    )
    return {"rule": IpManagementService.serialize_rule(rule)}


@router.put("/rules/{rule_id}")
def update_ip_rule(
    rule_id: int,
    payload: IpAccessRuleUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> dict:
    try:
        rule = IpManagementService.update_rule(db, rule_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="update",
        entity_type="ip_access_rule",
        entity_id=rule.id,
        entity_name=rule.name,
        summary=f"更新 IP 规则 {rule.name}",
        detail=IpManagementService.serialize_rule(rule),
    )
    return {"rule": IpManagementService.serialize_rule(rule)}


@router.delete("/rules/{rule_id}")
def delete_ip_rule(rule_id: int, db: Session = Depends(get_db), current_user=Depends(require_admin_api_user)) -> dict:
    rule = db.get(IpAccessRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="IP 规则不存在")
    name = rule.name
    db.delete(rule)
    db.commit()
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="delete",
        entity_type="ip_access_rule",
        entity_id=rule_id,
        entity_name=name,
        summary=f"删除 IP 规则 {name}",
    )
    return {"success": True}


@router.post("/rules/{rule_id}/enable")
def enable_ip_rule(rule_id: int, db: Session = Depends(get_db), current_user=Depends(require_admin_api_user)) -> dict:
    return _set_rule_enabled(db, current_user, rule_id, True)


@router.post("/rules/{rule_id}/disable")
def disable_ip_rule(rule_id: int, db: Session = Depends(get_db), current_user=Depends(require_admin_api_user)) -> dict:
    return _set_rule_enabled(db, current_user, rule_id, False)


def _set_rule_enabled(db: Session, current_user, rule_id: int, enabled: bool) -> dict:
    rule = db.get(IpAccessRule, rule_id)
    if rule is None:
        raise HTTPException(status_code=404, detail="IP 规则不存在")
    rule.enabled = enabled
    db.commit()
    db.refresh(rule)
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="enable" if enabled else "disable",
        entity_type="ip_access_rule",
        entity_id=rule.id,
        entity_name=rule.name,
        summary=("启用" if enabled else "停用") + f" IP 规则 {rule.name}",
    )
    return {"rule": IpManagementService.serialize_rule(rule)}


@router.get("/events")
def list_ip_events(
    keyword: str | None = None,
    ip: str | None = None,
    decision: str | None = None,
    scope: str | None = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    total, rows = IpManagementEventService.list_events(db, keyword=keyword, ip=ip, decision=decision, scope=scope, page=page, page_size=page_size)
    return {"total": total, "items": [IpManagementService.serialize_event(item) for item in rows], "page": page, "page_size": page_size}


@router.post("/events/cleanup")
def cleanup_ip_events(db: Session = Depends(get_db), current_user=Depends(require_admin_api_user)) -> dict:
    setting = IpManagementService.get_or_create_setting(db)
    count = IpManagementEventService.cleanup_old_events(db, retention_days=setting.event_retention_days)
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="cleanup",
        entity_type="ip_management_event",
        entity_id=None,
        entity_name="ip_management_events",
        summary=f"清理 IP 管理事件 {count} 条",
    )
    return {"deleted": count}


@router.post("/test-resolution")
def test_ip_resolution(payload: IpResolutionTestRequest) -> dict:
    resolution = ClientIpResolver.resolve(
        direct_client_ip=payload.direct_client_ip,
        headers={key.lower(): value for key, value in payload.headers.items()},
        trusted_proxy_resolution_enabled=payload.trusted_proxy_resolution_enabled,
        trusted_proxy_cidrs=payload.trusted_proxy_cidrs,
        trusted_header_order=list(payload.trusted_header_order),
    )
    return {"resolution": resolution.to_dict()}


@router.post("/test-rule")
def test_ip_rule(payload: IpRuleTestRequest, db: Session = Depends(get_db)) -> dict:
    rules = IpManagementService.enabled_rules(db)
    match = IpManagementRuleService.match(payload.ip, scope=payload.scope, rules=rules)
    return {
        "result": {
            "action": match.action,
            "reason": match.reason,
            "rule": IpManagementService.serialize_rule(match.rule) if match.rule is not None else None,
        }
    }
