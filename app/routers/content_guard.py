from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.request_log import RequestLog
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


@router.post("/runtime/inspect-text")
def inspect_content_guard_text_runtime(
    payload: ContentGuardTextInspectRequest,
    db: Session = Depends(get_db),
) -> dict:
    return ContentGuardModuleService.inspect_text(db, payload)


@router.get("/runtime/events")
def content_guard_runtime_events(db: Session = Depends(get_db)) -> dict:
    rows = list(
        db.scalars(
            select(RequestLog)
            .where(RequestLog.content_guard_result.is_not(None))
            .order_by(RequestLog.created_at.desc(), RequestLog.id.desc())
            .limit(100)
        )
    )
    return {
        "events": [
            {
                "id": item.id,
                "created_at": item.created_at,
                "trace_id": item.trace_id,
                "provider_id": item.provider_id,
                "provider_name": item.provider_name,
                "model_name": item.model_name,
                "requested_model": item.requested_model,
                "request_path": item.request_path,
                "is_stream": item.is_stream,
                "content_guard_result": item.content_guard_result,
                "content_guard_risk_level": item.content_guard_risk_level,
                "content_guard_reason": item.content_guard_reason,
                "content_guard_action": item.content_guard_action,
                "content_guard_latency_ms": item.content_guard_latency_ms,
                "content_guard_buffer_wait_ms": item.content_guard_buffer_wait_ms,
                "content_guard_final_strategy": item.content_guard_final_strategy,
            }
            for item in rows
        ]
    }


@router.get("/runtime/settings")
def content_guard_runtime_settings(db: Session = Depends(get_db)) -> dict:
    return {"settings": ContentGuardModuleService.build_overview(db)["settings"]}


@router.post("/probe")
async def run_content_guard_probe(
    payload: ContentGuardRunRequest,
    db: Session = Depends(get_db),
) -> dict:
    try:
        return await ContentGuardModuleService.run_probe(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/precheck/probe")
async def run_content_guard_precheck_probe(
    payload: ContentGuardRunRequest,
    db: Session = Depends(get_db),
) -> dict:
    try:
        return await ContentGuardModuleService.run_probe(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/trust-probe")
async def run_content_guard_trust_probe(
    payload: ContentGuardRunRequest,
    db: Session = Depends(get_db),
) -> dict:
    try:
        return await ContentGuardModuleService.run_trust_probe(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/precheck/trust-probe")
async def run_content_guard_precheck_trust_probe(
    payload: ContentGuardRunRequest,
    db: Session = Depends(get_db),
) -> dict:
    try:
        return await ContentGuardModuleService.run_trust_probe(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/precheck/status")
def content_guard_precheck_status(db: Session = Depends(get_db)) -> dict:
    overview = ContentGuardModuleService.build_overview(db)
    return {
        "settings": overview.get("settings") or {},
        "summary": overview.get("summary") or {},
        "providers": overview.get("providers") or [],
        "probe_options": overview.get("probe_options") or [],
    }
