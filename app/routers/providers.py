from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.provider import (
    ProviderBatchConnectivityTestRequest,
    ProviderAvailabilityResponse,
    ProviderCredentialRotateIn,
    ProviderCreate,
    ProviderDiscoverModelsIn,
    ProviderDiscoverModelsResponse,
    ProviderModelMountListResponse,
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
    return [ProviderOut(**item) for item in ProviderService.list_provider_dicts(db)]


@router.get("/models", response_model=ProviderModelMountListResponse)
def list_provider_model_mounts(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    keyword: str | None = Query(default=None),
    provider_id: int | None = Query(default=None, ge=1),
    enabled: bool | None = Query(default=None),
    health_status: str | None = Query(default=None),
    db: Session = Depends(get_db),
) -> ProviderModelMountListResponse:
    return ProviderModelMountListResponse(
        **ProviderService.list_provider_model_mounts(
            db,
            page=page,
            page_size=page_size,
            keyword=keyword,
            provider_id=provider_id,
            enabled=enabled,
            health_status=health_status,
        )
    )


@router.post("", response_model=ProviderOut, status_code=status.HTTP_201_CREATED)
def create_provider(payload: ProviderCreate, db: Session = Depends(get_db)) -> ProviderOut:
    provider = ProviderService.create_provider(db, payload)
    settings = SettingService.get_or_create(db)
    if settings.default_provider_id is None:
        settings.default_provider_id = provider.id
        db.commit()
    return ProviderOut(**ProviderService.provider_to_dict(provider, metrics=ProviderService._build_quality_metrics(db, [provider])))


@router.put("/{provider_id}", response_model=ProviderOut)
def update_provider(provider_id: int, payload: ProviderUpdate, db: Session = Depends(get_db)) -> ProviderOut:
    provider = ProviderService.get_provider(db, provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    provider = ProviderService.update_provider(db, provider, payload)
    return ProviderOut(**ProviderService.provider_to_dict(provider, metrics=ProviderService._build_quality_metrics(db, [provider])))


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
    metrics = ProviderService._build_quality_metrics(db, [provider])
    return ProviderModelConfigOut(**ProviderService.provider_model_to_dict(provider_model, metrics=metrics["provider_models"].get(provider_model.id)))


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


@router.post("/{provider_id}/rotate-credential", response_model=ProviderOut)
def rotate_provider_credential(
    provider_id: int,
    payload: ProviderCredentialRotateIn,
    db: Session = Depends(get_db),
) -> ProviderOut:
    provider = ProviderService.get_provider(db, provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    provider = ProviderService.rotate_provider_credential(
        db,
        provider,
        api_key=payload.api_key,
        credential_hint=payload.credential_hint,
    )
    return ProviderOut(**ProviderService.provider_to_dict(provider, metrics=ProviderService._build_quality_metrics(db, [provider])))


@router.post("/discover-models", response_model=ProviderDiscoverModelsResponse)
async def discover_provider_models(
    payload: ProviderDiscoverModelsIn,
    db: Session = Depends(get_db),
) -> ProviderDiscoverModelsResponse:
    try:
        return await ProviderService.discover_models(db, payload)
    except ValueError as exc:
        detail = str(exc)
        status_code = 404 if detail == "Provider not found" else 400
        raise HTTPException(status_code=status_code, detail=detail) from exc


@router.get("/{provider_id}/availability", response_model=ProviderAvailabilityResponse)
def provider_availability(
    provider_id: int,
    window_hours: int = 24,
    bucket_minutes: int = 60,
    db: Session = Depends(get_db),
) -> ProviderAvailabilityResponse:
    provider = ProviderService.get_provider(db, provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    items = ProviderService.availability_timeseries(
        db,
        provider=provider,
        window_hours=window_hours,
        bucket_minutes=bucket_minutes,
    )
    return ProviderAvailabilityResponse(
        provider_id=provider.id,
        provider_name=provider.name,
        window_hours=max(1, min(window_hours, 24 * 30)),
        bucket_minutes=max(5, min(bucket_minutes, 24 * 60)),
        items=items,
    )


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
