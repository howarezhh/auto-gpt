from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.provider_model import ProviderModel
from app.schemas.provider import ProviderModelConfigOut
from app.services.health_service import HealthService
from app.services.admin_audit_service import AdminAuditService
from app.services.provider_service import ProviderService
from app.services.user_auth_service import UserAuthService


router = APIRouter(prefix="/api/provider-models", tags=["provider-models"])


def _record_provider_model_audit(
    db: Session,
    *,
    request: Request,
    action: str,
    entity_id: int | str | None,
    entity_name: str | None,
    summary: str,
    detail: dict | None = None,
) -> None:
    current_user = UserAuthService.get_current_user(request, db)
    AdminAuditService.create_log(
        db,
        actor_user_id=getattr(current_user, "id", None),
        actor_username=getattr(current_user, "username", None),
        action=action,
        entity_type="provider_model",
        entity_id=entity_id,
        entity_name=entity_name,
        summary=summary,
        detail=detail,
        request_trace_id=getattr(request.state, "trace_id", None),
        source_ip=request.client.host if request.client else None,
        risk_level="medium",
    )


@router.get("", response_model=list[ProviderModelConfigOut])
def list_provider_models(db: Session = Depends(get_db)) -> list[ProviderModelConfigOut]:
    providers = ProviderService.list_providers(db)
    metrics = ProviderService._build_quality_metrics(db, providers)
    return [
        ProviderModelConfigOut(**ProviderService.provider_model_to_dict(provider_model, metrics=metrics["provider_models"].get(provider_model.id)))
        for provider in providers
        for provider_model in provider.provider_models
    ]


@router.post("/{provider_model_id}/test")
async def test_provider_model(
    provider_model_id: int,
    request: Request,
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
    _record_provider_model_audit(
        db,
        request=request,
        action="provider_model_test",
        entity_id=provider_model.id,
        entity_name=provider_model.model_name,
        summary=f"触发单模型测试：{provider.name} / {provider_model.model_name}",
        detail={
            "provider_id": provider.id,
            "stream_probe": body.get("stream_probe") is True,
            "vision_probe": body.get("vision_probe") is True,
        },
    )
    return await HealthService.check_provider_model(
        db,
        provider,
        provider_model,
        stream_probe=body.get("stream_probe") is True,
        vision_probe=body.get("vision_probe") is True,
    )


@router.post("/test-all")
async def test_all_provider_models(request: Request, db: Session = Depends(get_db)) -> list[dict]:
    _record_provider_model_audit(
        db,
        request=request,
        action="provider_model_batch_test",
        entity_id="all",
        entity_name="全部模型挂载",
        summary="触发全部模型挂载批量测试",
        detail=None,
    )
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
