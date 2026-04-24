from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.setting import SettingOut, SettingUpdate
from app.tasks import configure_scheduler
from app.services.setting_service import SettingService


router = APIRouter(prefix="/api/settings", tags=["settings"])


@router.get("", response_model=SettingOut)
def get_settings_api(db: Session = Depends(get_db)) -> SettingOut:
    return SettingOut.model_validate(SettingService.get_or_create(db))


@router.put("", response_model=SettingOut)
def update_settings(payload: SettingUpdate, db: Session = Depends(get_db)) -> SettingOut:
    try:
        setting = SettingService.update(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    configure_scheduler()
    return SettingOut.model_validate(setting)
