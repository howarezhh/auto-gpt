import asyncio
import json
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.schemas.provider import (
    ProviderBatchConnectivityTestRequest,
    ProviderBatchImportRequest,
    ProviderBatchImportResponse,
    ProviderAvailabilityResponse,
    ProviderCredentialRotateIn,
    ProviderCreate,
    ProviderDiscoverModelsIn,
    ProviderDiscoverModelsResponse,
    ProviderModelMountListResponse,
    ProviderModelConfigOut,
    ProviderPageContentOut,
    ProviderModelConfigUpdate,
    ProviderOptionOut,
    ProviderOut,
    ProviderPlaygroundOut,
    ProviderSummaryOut,
    ProviderUpdate,
)
from app.models.provider_model import ProviderModel
from app.services.health_service import HealthService
from app.services.admin_audit_service import AdminAuditService
from app.services.provider_service import ProviderService
from app.services.setting_service import SettingService
from app.services.user_auth_service import UserAuthService
from app.utils.test_features import normalize_test_features, phase_keys_from_test_features


router = APIRouter(prefix="/api/providers", tags=["providers"])


def _health_stream_line(payload: dict) -> bytes:
    return (json.dumps(payload, ensure_ascii=False) + "\n").encode("utf-8")


def _health_stream_headers() -> dict[str, str]:
    return {
        "Cache-Control": "no-cache, no-transform",
        "X-Accel-Buffering": "no",
    }


def _record_provider_audit(
    db: Session,
    *,
    request: Request,
    action: str,
    entity_id: int | str | None,
    entity_name: str | None,
    summary: str,
    detail: dict | list | str | None = None,
    before: dict | list | str | None = None,
    after: dict | list | str | None = None,
    changed_fields: dict | list | str | None = None,
    risk_level: str = "low",
) -> None:
    current_user = UserAuthService.get_current_user(request, db)
    AdminAuditService.create_log(
        db,
        actor_user_id=getattr(current_user, "id", None),
        actor_username=getattr(current_user, "username", None),
        action=action,
        entity_type="provider",
        entity_id=entity_id,
        entity_name=entity_name,
        summary=summary,
        detail=detail,
        before=before,
        after=after,
        changed_fields=changed_fields,
        request_trace_id=getattr(request.state, "trace_id", None),
        source_ip=request.client.host if request.client else None,
        risk_level=risk_level,
    )


async def _stream_health_check_events(
    worker: Callable[[Callable[[dict], Awaitable[None]]], Awaitable[object]],
    on_finished: Callable[[], None] | None = None,
):
    queue: asyncio.Queue[dict | None] = asyncio.Queue()

    async def reporter(payload: dict) -> None:
        await queue.put(payload)

    async def run_worker() -> None:
        try:
            result = await worker(reporter)
            await queue.put({"event": "completed", "result": result})
        except Exception as exc:
            await queue.put({"event": "error", "message": str(exc)})
        finally:
            await queue.put(None)

    task = asyncio.create_task(run_worker())
    try:
        while True:
            item = await queue.get()
            if item is None:
                break
            yield _health_stream_line(item)
    finally:
        try:
            await task
        finally:
            if on_finished is not None:
                on_finished()


@router.get("", response_model=list[ProviderOut])
def list_providers(db: Session = Depends(get_db)) -> list[ProviderOut]:
    return [ProviderOut(**item) for item in ProviderService.list_provider_dicts(db)]


@router.get("/overview", response_model=ProviderPageContentOut)
def provider_page_overview(db: Session = Depends(get_db)) -> ProviderPageContentOut:
    return ProviderPageContentOut(**ProviderService.build_provider_page_content(db))


@router.get("/options", response_model=list[ProviderOptionOut])
def list_provider_options(db: Session = Depends(get_db)) -> list[ProviderOptionOut]:
    return [ProviderOptionOut(**item) for item in ProviderService.list_provider_option_dicts(db)]


@router.get("/playground", response_model=list[ProviderPlaygroundOut])
def list_provider_playground_items(db: Session = Depends(get_db)) -> list[ProviderPlaygroundOut]:
    return [ProviderPlaygroundOut(**item) for item in ProviderService.list_provider_playground_dicts(db)]


@router.get("/summary", response_model=list[ProviderSummaryOut])
def list_provider_summaries(db: Session = Depends(get_db)) -> list[ProviderSummaryOut]:
    return [ProviderSummaryOut(**item) for item in ProviderService.list_provider_summary_dicts(db)]


@router.get("/batch-import-template")
def get_provider_batch_import_template() -> dict:
    return {"template": ProviderService.BATCH_IMPORT_TEMPLATE}


@router.post("/batch-import", response_model=ProviderBatchImportResponse)
def batch_import_providers(
    payload: ProviderBatchImportRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> ProviderBatchImportResponse:
    try:
        result = ProviderService.batch_import_providers(db, payload)
        _record_provider_audit(
            db,
            request=request,
            action="batch_import_providers",
            entity_id=None,
            entity_name="批量导入提供商",
            summary=f"批量导入提供商，创建 {result.created_count} 条，失败 {result.failed_count} 条",
            detail=result.model_dump(),
            risk_level="medium",
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/models", response_model=ProviderModelMountListResponse)
def list_provider_model_mounts(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    keyword: str | None = Query(default=None),
    provider_id: int | None = Query(default=None, ge=1),
    enabled: bool | None = Query(default=None),
    health_status: str | None = Query(default=None),
    trust_status: str | None = Query(default=None),
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
            trust_status=trust_status,
        )
    )


@router.post("", response_model=ProviderOut, status_code=status.HTTP_201_CREATED)
def create_provider(payload: ProviderCreate, request: Request, db: Session = Depends(get_db)) -> ProviderOut:
    provider = ProviderService.create_provider(db, payload)
    settings = SettingService.get_or_create(db)
    if settings.default_provider_id is None:
        settings.default_provider_id = provider.id
        db.commit()
    provider_dict = ProviderService.provider_to_dict(provider, metrics=ProviderService._build_quality_metrics(db, [provider]))
    _record_provider_audit(
        db,
        request=request,
        action="create_provider",
        entity_id=provider.id,
        entity_name=provider.name,
        summary=f"创建提供商 {provider.name}",
        after=provider_dict,
        risk_level="medium",
    )
    return ProviderOut(**provider_dict)


@router.put("/{provider_id}", response_model=ProviderOut)
def update_provider(provider_id: int, payload: ProviderUpdate, request: Request, db: Session = Depends(get_db)) -> ProviderOut:
    provider = ProviderService.get_provider(db, provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    before = ProviderService.provider_to_dict(provider, metrics=ProviderService._build_quality_metrics(db, [provider]))
    provider = ProviderService.update_provider(db, provider, payload)
    after = ProviderService.provider_to_dict(provider, metrics=ProviderService._build_quality_metrics(db, [provider]))
    _record_provider_audit(
        db,
        request=request,
        action="update_provider",
        entity_id=provider.id,
        entity_name=provider.name,
        summary=f"更新提供商 {provider.name}",
        before=before,
        after=after,
        changed_fields=payload.model_dump(exclude_unset=True),
        risk_level="medium",
    )
    return ProviderOut(**after)


@router.put("/{provider_id}/models/{provider_model_id}", response_model=ProviderModelConfigOut)
def update_provider_model(
    provider_id: int,
    provider_model_id: int,
    payload: ProviderModelConfigUpdate,
    request: Request,
    db: Session = Depends(get_db),
) -> ProviderModelConfigOut:
    provider = ProviderService.get_provider(db, provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    try:
        before = ProviderService.provider_model_to_dict(provider_model, metrics=None) if (provider_model := next((item for item in provider.provider_models if item.id == provider_model_id), None)) else None
        provider_model = ProviderService.update_provider_model(db, provider, provider_model_id, payload)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    metrics = ProviderService._build_quality_metrics(db, [provider])
    after = ProviderService.provider_model_to_dict(provider_model, metrics=metrics["provider_models"].get(provider_model.id))
    _record_provider_audit(
        db,
        request=request,
        action="update_provider_model",
        entity_id=provider_model.id,
        entity_name=provider_model.model_name,
        summary=f"更新提供商模型挂载 {provider.name} / {provider_model.model_name}",
        before=before,
        after=after,
        changed_fields=payload.model_dump(exclude_unset=True),
        risk_level="medium",
    )
    return ProviderModelConfigOut(**after)


@router.delete("/{provider_id}")
def delete_provider(provider_id: int, request: Request, db: Session = Depends(get_db)) -> dict:
    provider = ProviderService.get_provider(db, provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    before = ProviderService.provider_to_dict(provider, metrics=ProviderService._build_quality_metrics(db, [provider]))
    provider_name = provider.name
    ProviderService.delete_provider(db, provider)
    settings = SettingService.get_or_create(db)
    if settings.default_provider_id == provider_id:
        settings.default_provider_id = None
        db.commit()
    _record_provider_audit(
        db,
        request=request,
        action="delete_provider",
        entity_id=provider_id,
        entity_name=provider_name,
        summary=f"删除提供商 {provider_name}",
        before=before,
        risk_level="high",
    )
    return {"message": "deleted"}


@router.post("/{provider_id}/rotate-credential", response_model=ProviderOut)
def rotate_provider_credential(
    provider_id: int,
    payload: ProviderCredentialRotateIn,
    request: Request,
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
    provider_dict = ProviderService.provider_to_dict(provider, metrics=ProviderService._build_quality_metrics(db, [provider]))
    _record_provider_audit(
        db,
        request=request,
        action="rotate_provider_credential",
        entity_id=provider.id,
        entity_name=provider.name,
        summary=f"轮换提供商密钥 {provider.name}",
        detail={"credential_hint": payload.credential_hint},
        after={"credential_hint": provider.credential_hint, "credential_rotated_at": provider.credential_rotated_at.isoformat() if provider.credential_rotated_at else None},
        risk_level="high",
    )
    return ProviderOut(**provider_dict)


@router.post("/discover-models", response_model=ProviderDiscoverModelsResponse)
async def discover_provider_models(
    payload: ProviderDiscoverModelsIn,
    request: Request,
    db: Session = Depends(get_db),
) -> ProviderDiscoverModelsResponse:
    try:
        result = await ProviderService.discover_models(db, payload)
        _record_provider_audit(
            db,
            request=request,
            action="discover_provider_models",
            entity_id=payload.provider_id,
            entity_name="模型发现",
            summary=f"执行提供商模型发现 {payload.provider_id}",
            detail=result.model_dump(),
            risk_level="low",
        )
        return result
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
async def test_provider(provider_id: int, request: Request, payload: dict | None = None, db: Session = Depends(get_db)) -> dict:
    provider = ProviderService.get_provider(db, provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    slot_key = f"provider:{provider.id}"
    try:
        HealthService.claim_manual_check_slot(slot_key, f"提供商 {provider.name}")
    except ValueError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    features = normalize_test_features(payload)
    _record_provider_audit(
        db,
        request=request,
        action="health_check_provider",
        entity_id=provider.id,
        entity_name=provider.name,
        summary=f"触发提供商健康检查：{provider.name}",
        detail={"features": features},
        risk_level="medium",
    )
    try:
        return await HealthService.check_provider(
            db,
            provider,
            phase_keys=phase_keys_from_test_features(features),
            text_probe_max_tokens=HealthService.INTERACTIVE_TEXT_PROBE_MAX_TOKENS,
            capability_probe_max_tokens=HealthService.SCHEDULED_CAPABILITY_PROBE_MAX_TOKENS,
            interactive_mode=True,
            single_endpoint_mode=True,
        )
    finally:
        HealthService.release_manual_check_slot(slot_key)


@router.post("/{provider_id}/test-stream")
async def test_provider_stream(provider_id: int, request: Request, payload: dict | None = None, db: Session = Depends(get_db)) -> StreamingResponse:
    provider = ProviderService.get_provider(db, provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    slot_key = f"provider:{provider.id}"
    try:
        HealthService.claim_manual_check_slot(slot_key, f"提供商 {provider.name}")
    except ValueError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc

    features = normalize_test_features(payload)
    _record_provider_audit(
        db,
        request=request,
        action="health_check_provider_stream",
        entity_id=provider.id,
        entity_name=provider.name,
        summary=f"触发提供商流式健康检查：{provider.name}",
        detail={"features": features},
        risk_level="medium",
    )

    async def worker(progress_reporter: Callable[[dict], Awaitable[None]]) -> dict:
        stream_db = SessionLocal()
        try:
            stream_provider = ProviderService.get_provider(stream_db, provider_id)
            if not stream_provider:
                raise RuntimeError("Provider not found")
            return await HealthService.check_provider(
                stream_db,
                stream_provider,
                phase_keys=phase_keys_from_test_features(features),
                text_probe_max_tokens=HealthService.INTERACTIVE_TEXT_PROBE_MAX_TOKENS,
                capability_probe_max_tokens=HealthService.SCHEDULED_CAPABILITY_PROBE_MAX_TOKENS,
                progress_callback=progress_reporter,
                interactive_mode=True,
                single_endpoint_mode=True,
            )
        finally:
            stream_db.close()

    return StreamingResponse(
        _stream_health_check_events(worker, on_finished=lambda: HealthService.release_manual_check_slot(slot_key)),
        media_type="application/x-ndjson",
        headers=_health_stream_headers(),
    )


@router.post("/{provider_id}/models/{provider_model_id}/test")
async def test_provider_model(
    provider_id: int,
    provider_model_id: int,
    request: Request,
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
    features = normalize_test_features(body, single_model=True)
    _record_provider_audit(
        db,
        request=request,
        action="health_check_provider_model",
        entity_id=provider_model.id,
        entity_name=provider_model.model_name,
        summary=f"触发模型健康检查：{provider.name} / {provider_model.model_name}",
        detail={"provider_id": provider.id, "features": features},
        risk_level="medium",
    )
    return await HealthService.check_provider_model(
        db,
        provider,
        provider_model,
        stream_probe=body.get("stream_probe") is True,
        vision_probe=body.get("vision_probe") is True,
        phase_keys=phase_keys_from_test_features(features),
        text_probe_max_tokens=HealthService.INTERACTIVE_TEXT_PROBE_MAX_TOKENS,
        capability_probe_max_tokens=HealthService.INTERACTIVE_CAPABILITY_PROBE_MAX_TOKENS,
        interactive_mode=True,
        parallel_phases=True,
        single_endpoint_mode=True,
    )


@router.post("/test-all")
async def test_all_providers(request: Request, payload: dict | None = None, db: Session = Depends(get_db)) -> list[dict]:
    slot_key = "all"
    try:
        HealthService.claim_manual_check_slot(slot_key, "全部提供商")
    except ValueError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    features = normalize_test_features(payload)
    _record_provider_audit(
        db,
        request=request,
        action="health_check_all",
        entity_id="all",
        entity_name="全部提供商",
        summary="触发全部提供商健康检查",
        detail={"features": features},
        risk_level="medium",
    )
    try:
        return await HealthService.check_all(
            db,
            selective=False,
            phase_keys=phase_keys_from_test_features(features),
            text_probe_max_tokens=HealthService.INTERACTIVE_TEXT_PROBE_MAX_TOKENS,
            capability_probe_max_tokens=HealthService.SCHEDULED_CAPABILITY_PROBE_MAX_TOKENS,
            interactive_mode=True,
            single_endpoint_mode=True,
        )
    finally:
        HealthService.release_manual_check_slot(slot_key)


@router.post("/test-all-stream")
async def test_all_providers_stream(request: Request, payload: dict | None = None, db: Session = Depends(get_db)) -> StreamingResponse:
    slot_key = "all"
    try:
        HealthService.claim_manual_check_slot(slot_key, "全部提供商")
    except ValueError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc

    features = normalize_test_features(payload)
    _record_provider_audit(
        db,
        request=request,
        action="health_check_all_stream",
        entity_id="all",
        entity_name="全部提供商",
        summary="触发全部提供商流式健康检查",
        detail={"features": features},
        risk_level="medium",
    )

    async def worker(progress_reporter: Callable[[dict], Awaitable[None]]) -> list[dict]:
        stream_db = SessionLocal()
        try:
            return await HealthService.check_all(
                stream_db,
                selective=False,
                phase_keys=phase_keys_from_test_features(features),
                text_probe_max_tokens=HealthService.INTERACTIVE_TEXT_PROBE_MAX_TOKENS,
                capability_probe_max_tokens=HealthService.SCHEDULED_CAPABILITY_PROBE_MAX_TOKENS,
                progress_callback=progress_reporter,
                interactive_mode=True,
                single_endpoint_mode=True,
            )
        finally:
            stream_db.close()

    return StreamingResponse(
        _stream_health_check_events(worker, on_finished=lambda: HealthService.release_manual_check_slot(slot_key)),
        media_type="application/x-ndjson",
        headers=_health_stream_headers(),
    )


@router.post("/test-connectivity")
async def test_provider_connectivity(
    payload: ProviderBatchConnectivityTestRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> list[dict]:
    providers = ProviderService.list_providers(db)
    if payload.provider_ids:
        existing_ids = {provider.id for provider in providers}
        missing_ids = [provider_id for provider_id in payload.provider_ids if provider_id not in existing_ids]
        if missing_ids:
            raise HTTPException(status_code=404, detail=f"Provider not found: {', '.join(str(item) for item in missing_ids)}")
    _record_provider_audit(
        db,
        request=request,
        action="health_check_connectivity",
        entity_id="selected" if payload.provider_ids else "all",
        entity_name="指定提供商" if payload.provider_ids else "全部提供商",
        summary="触发提供商连通性检查",
        detail={"provider_ids": payload.provider_ids},
        risk_level="medium",
    )
    return await HealthService.check_selected_providers(
        db,
        provider_ids=payload.provider_ids or None,
        include_disabled_models=True,
        phase_keys=HealthService.INTERACTIVE_TEXT_PROBE_PHASE_KEYS,
        text_probe_max_tokens=HealthService.INTERACTIVE_TEXT_PROBE_MAX_TOKENS,
        interactive_mode=True,
        single_endpoint_mode=True,
    )
