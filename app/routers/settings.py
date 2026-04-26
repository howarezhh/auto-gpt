from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.setting import SettingOut, SettingUpdate
from app.tasks import configure_scheduler
from app.services.admin_audit_service import AdminAuditService
from app.services.setting_service import SettingService
from app.services.user_auth_service import require_admin_api_user


router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("", response_model=SettingOut)
def get_settings_api(db: Session = Depends(get_db)) -> SettingOut:
    return SettingOut.model_validate(SettingService.get_or_create(db))


@router.put("", response_model=SettingOut)
def update_settings(
    payload: SettingUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> SettingOut:
    try:
        setting = SettingService.update(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    configure_scheduler()
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="update",
        entity_type="setting",
        entity_id=setting.id,
        entity_name="app_settings",
        summary="更新系统设置",
        detail=payload.model_dump(exclude_unset=True),
    )
    return SettingOut.model_validate(setting)
