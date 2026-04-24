from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.provider_model import ProviderModel
from app.schemas.provider import ProviderModelConfigOut
from app.services.health_service import HealthService
from app.services.provider_service import ProviderService


router = APIRouter(prefix="/api/provider-models", tags=["provider-models"])


@router.get("", response_model=list[ProviderModelConfigOut])
def list_provider_models(db: Session = Depends(get_db)) -> list[ProviderModelConfigOut]:
    providers = ProviderService.list_providers(db)
    return [
        ProviderModelConfigOut.model_validate(provider_model)
        for provider in providers
        for provider_model in provider.provider_models
    ]


@router.post("/{provider_model_id}/test")
async def test_provider_model(
    provider_model_id: int,
    payload: dict | None = None,
    db: Session = Depends(get_db),
) -> dict:
    provider_model = db.scalar(select(ProviderModel).where(ProviderModel.id == provider_model_id))
    if provider_model is None:
        raise HTTPException(status_code=404, detail="Provider model not found")
    provider = ProviderService.get_provider(db, provider_model.provider_id)
    if provider is None:
        raise HTTPException(status_code=404, detail="Provider not found")
    body = payload or {}
    return await HealthService.check_provider_model(
        db,
        provider,
        provider_model,
        stream_probe=body.get("stream_probe") is True,
        vision_probe=body.get("vision_probe") is True,
    )


@router.post("/test-all")
async def test_all_provider_models(db: Session = Depends(get_db)) -> list[dict]:
    results: list[dict] = []
    for provider in ProviderService.list_providers(db):
        if not provider.enabled:
            continue
        for provider_model in provider.provider_models:
            if not provider_model.enabled:
                continue
            results.append(
                {
                    "provider_id": provider.id,
                    "provider_name": provider.name,
                    "provider_model_id": provider_model.id,
                    "model_name": provider_model.model_name,
                    **(await HealthService.check_provider_model(db, provider, provider_model)),
                }
            )
    return results
