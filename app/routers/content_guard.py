from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.content_guard import ContentGuardRulesUpdate, ContentGuardRunRequest, ContentGuardSettingsUpdate, ContentGuardTextInspectRequest
from app.services.admin_audit_service import AdminAuditService
from app.services.content_guard_module_service import ContentGuardModuleService
from app.services.user_auth_service import require_admin_api_user
from app.tasks import configure_scheduler


router = APIRouter(prefix="/api/content-guard", tags=["content-guard"])


@router.get("/overview")
def content_guard_overview(db: Session = Depends(get_db)) -> dict:
    return ContentGuardModuleService.build_overview(db)


@router.put("/settings")
def update_content_guard_settings(
    payload: ContentGuardSettingsUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> dict:
    setting = ContentGuardModuleService.update_settings(db, payload)
    configure_scheduler()
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="update",
        entity_type="setting",
        entity_id=setting.id,
        entity_name="content_guard",
        summary="更新内容完整性防护设置",
        detail=payload.model_dump(),
    )
    return {"settings": ContentGuardModuleService.serialize_settings(setting)}


@router.put("/rules")
def update_content_guard_rules(
    payload: ContentGuardRulesUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> dict:
    setting = ContentGuardModuleService.update_rules(db, payload)
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="update",
        entity_type="setting",
        entity_id=setting.id,
        entity_name="content_guard_rules",
        summary="更新内容完整性防护规则",
        detail={"rule_count": len(payload.rules), "rule_ids": [item.id for item in payload.rules]},
    )
    return {"rules": ContentGuardModuleService.serialize_rules(setting)}


@router.post("/rules/reset")
def reset_content_guard_rules(
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> dict:
    setting = ContentGuardModuleService.reset_rules(db)
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="update",
        entity_type="setting",
        entity_id=setting.id,
        entity_name="content_guard_rules",
        summary="恢复内容完整性防护默认规则",
        detail={"reset_to_default": True},
    )
    return {"rules": ContentGuardModuleService.serialize_rules(setting)}


@router.post("/inspect-text")
def inspect_content_guard_text(
    payload: ContentGuardTextInspectRequest,
    db: Session = Depends(get_db),
) -> dict:
    return ContentGuardModuleService.inspect_text(db, payload)


@router.post("/probe")
async def run_content_guard_probe(
    payload: ContentGuardRunRequest,
    db: Session = Depends(get_db),
) -> dict:
    try:
        return await ContentGuardModuleService.run_probe(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
