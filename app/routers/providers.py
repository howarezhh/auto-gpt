from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.provider import (
    ProviderBatchConnectivityTestRequest,
    ProviderCreate,
    ProviderModelConfigOut,
    ProviderModelConfigUpdate,
    ProviderOut,
    ProviderUpdate,
)
from app.models.provider_model import ProviderModel
from app.services.health_service import HealthService
from app.services.provider_service import ProviderService
from app.services.setting_service import SettingService


router = APIRouter(prefix="/api/providers", tags=["providers"])


@router.get("", response_model=list[ProviderOut])
def list_providers(db: Session = Depends(get_db)) -> list[ProviderOut]:
    return [ProviderOut(**ProviderService.provider_to_dict(item)) for item in ProviderService.list_providers(db)]


@router.post("", response_model=ProviderOut, status_code=status.HTTP_201_CREATED)
def create_provider(payload: ProviderCreate, db: Session = Depends(get_db)) -> ProviderOut:
    provider = ProviderService.create_provider(db, payload)
    settings = SettingService.get_or_create(db)
    if settings.default_provider_id is None:
        settings.default_provider_id = provider.id
        db.commit()
    return ProviderOut(**ProviderService.provider_to_dict(provider))


@router.put("/{provider_id}", response_model=ProviderOut)
def update_provider(provider_id: int, payload: ProviderUpdate, db: Session = Depends(get_db)) -> ProviderOut:
    provider = ProviderService.get_provider(db, provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    return ProviderOut(**ProviderService.provider_to_dict(ProviderService.update_provider(db, provider, payload)))


@router.put("/{provider_id}/models/{provider_model_id}", response_model=ProviderModelConfigOut)
def update_provider_model(
    provider_id: int,
    provider_model_id: int,
    payload: ProviderModelConfigUpdate,
    db: Session = Depends(get_db),
) -> ProviderModelConfigOut:
    provider = ProviderService.get_provider(db, provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    try:
        provider_model = ProviderService.update_provider_model(db, provider, provider_model_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return ProviderModelConfigOut.model_validate(provider_model)


@router.delete("/{provider_id}")
def delete_provider(provider_id: int, db: Session = Depends(get_db)) -> dict:
    provider = ProviderService.get_provider(db, provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    ProviderService.delete_provider(db, provider)
    settings = SettingService.get_or_create(db)
    if settings.default_provider_id == provider_id:
        settings.default_provider_id = None
        db.commit()
    return {"message": "deleted"}


@router.post("/{provider_id}/test")
async def test_provider(provider_id: int, db: Session = Depends(get_db)) -> dict:
    provider = ProviderService.get_provider(db, provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    return await HealthService.check_provider(db, provider)


@router.post("/{provider_id}/models/{provider_model_id}/test")
async def test_provider_model(
    provider_id: int,
    provider_model_id: int,
    payload: dict | None = None,
    db: Session = Depends(get_db),
) -> dict:
    provider = ProviderService.get_provider(db, provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    provider_model = next((item for item in provider.provider_models if item.id == provider_model_id), None)
    if provider_model is None:
        raise HTTPException(status_code=404, detail="Provider model not found")
    body = payload or {}
    return await HealthService.check_provider_model(
        db,
        provider,
        provider_model,
        stream_probe=body.get("stream_probe") is True,
        vision_probe=body.get("vision_probe") is True,
    )


@router.post("/test-all")
async def test_all_providers(db: Session = Depends(get_db)) -> list[dict]:
    return await HealthService.check_all(db)


@router.post("/test-connectivity")
async def test_provider_connectivity(
    payload: ProviderBatchConnectivityTestRequest,
    db: Session = Depends(get_db),
) -> list[dict]:
    providers = ProviderService.list_providers(db)
    if payload.provider_ids:
        existing_ids = {provider.id for provider in providers}
        missing_ids = [provider_id for provider_id in payload.provider_ids if provider_id not in existing_ids]
        if missing_ids:
            raise HTTPException(status_code=404, detail=f"Provider not found: {', '.join(str(item) for item in missing_ids)}")
    return await HealthService.check_selected_providers(
        db,
        provider_ids=payload.provider_ids or None,
        include_disabled_models=True,
    )
