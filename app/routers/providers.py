import asyncio
import contextlib
import json
from collections.abc import Awaitable, Callable

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from fastapi.responses import PlainTextResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import SessionLocal, get_db
from app.schemas.provider import (
    ProviderBatchConnectivityTestRequest,
    ProviderBatchGovernanceRequest,
    ProviderBatchGovernanceResponse,
    ProviderBatchImportRequest,
    ProviderBatchImportResponse,
    ProviderAvailabilityResponse,
    ProviderCredentialRotateIn,
    ProviderCreate,
    ProviderDiscoverModelsIn,
    ProviderDiscoverModelsResponse,
    ProviderEndpointProtocolDetectionRequest,
    ProviderListResponse,
    ProviderModelEndpointProtocolDetectionRequest,
    ProviderModelBatchImportRequest,
    ProviderModelBatchImportResponse,
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
from app.models.provider import Provider
from app.models.provider_model import ProviderModel
from app.services.health_service import HealthService
from app.services.admin_audit_service import AdminAuditService
from app.services.provider_service import ProviderService
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


async def _detect_created_provider_protocols_background(
    *,
    provider_id: int,
    provider_name: str,
    actor_user_id: int | None,
    actor_username: str | None,
    request_trace_id: str | None,
    source_ip: str | None,
) -> None:
    db = SessionLocal()
    try:
        result = await HealthService.detect_endpoint_protocols_for_provider_ids(
            db,
            provider_ids=[provider_id],
            trigger_type="auto_provider_created",
        )
        AdminAuditService.create_log(
            db,
            actor_user_id=actor_user_id,
            actor_username=actor_username,
            action="detect_endpoint_protocols_auto_provider_created",
            entity_type="provider",
            entity_id=provider_id,
            entity_name=provider_name,
            summary=f"新增提供商后检测端点协议：{provider_name}",
            detail=result,
            request_trace_id=request_trace_id,
            source_ip=source_ip,
            risk_level="medium",
        )
    except Exception as exc:
        db.rollback()
        AdminAuditService.create_log(
            db,
            actor_user_id=actor_user_id,
            actor_username=actor_username,
            action="detect_endpoint_protocols_auto_provider_created_failed",
            entity_type="provider",
            entity_id=provider_id,
            entity_name=provider_name,
            summary=f"新增提供商后端点协议检测失败：{provider_name}",
            detail={"error": str(exc)},
            request_trace_id=request_trace_id,
            source_ip=source_ip,
            risk_level="medium",
        )
    finally:
        db.close()


async def _stream_health_check_events(
    worker: Callable[[Callable[[dict], Awaitable[None]]], Awaitable[object]],
    on_finished: Callable[[], None] | None = None,
    request: Request | None = None,
):
    queue: asyncio.Queue[dict | None] = asyncio.Queue(maxsize=HealthService.HEALTH_STREAM_PROGRESS_QUEUE_SIZE)

    def offer_event(payload: dict | None) -> None:
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            try:
                queue.get_nowait()
                queue.task_done()
            except asyncio.QueueEmpty:
                pass
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(payload)

    async def reporter(payload: dict) -> None:
        offer_event(payload)

    async def run_worker() -> None:
        try:
            result = await worker(reporter)
            offer_event({"event": "completed", "result": result})
        except Exception as exc:
            offer_event({"event": "error", "message": str(exc)})
        finally:
            offer_event(None)

    task = asyncio.create_task(run_worker())
    try:
        while True:
            if request is not None and await request.is_disconnected():
                task.cancel()
                break
            try:
                item = await asyncio.wait_for(queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            if item is None:
                break
            yield _health_stream_line(item)
    finally:
        try:
            if not task.done():
                task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        finally:
            if on_finished is not None:
                on_finished()


@router.get("", response_model=list[ProviderOut])
def list_providers(db: Session = Depends(get_db)) -> list[ProviderOut]:
    return [ProviderOut(**item) for item in ProviderService.list_provider_dicts(db)]


@router.get("/directory", response_model=ProviderListResponse)
def list_provider_directory(
    keyword: str | None = Query(default=None),
    enabled: bool | None = Query(default=None),
    health_status: str | None = Query(default=None),
    trust_status: str | None = Query(default=None),
    circuit_state: str | None = Query(default=None),
    provider_type: str | None = Query(default=None),
    group_name: str | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> ProviderListResponse:
    return ProviderListResponse(
        **ProviderService.list_provider_directory(
            db,
            keyword=keyword,
            enabled=enabled,
            health_status=health_status,
            trust_status=trust_status,
            circuit_state=circuit_state,
            provider_type=provider_type,
            group_name=group_name,
            page=page,
            page_size=page_size,
        )
    )


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


@router.get("/export")
def export_providers(db: Session = Depends(get_db)) -> PlainTextResponse:
    text = ProviderService.export_providers_import_text(db)
    return PlainTextResponse(
        text,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="providers-import-template.txt"'},
    )


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


@router.get("/models/batch-import-template")
def get_provider_model_batch_import_template() -> dict:
    return {"template": ProviderService.MODEL_BATCH_IMPORT_TEMPLATE}


@router.get("/models/export")
def export_provider_models(db: Session = Depends(get_db)) -> PlainTextResponse:
    text = ProviderService.export_provider_models_import_text(db)
    return PlainTextResponse(
        text,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="provider-models-import-template.txt"'},
    )


@router.post("/models/batch-import", response_model=ProviderModelBatchImportResponse)
def batch_import_provider_models(
    payload: ProviderModelBatchImportRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> ProviderModelBatchImportResponse:
    try:
        result = ProviderService.batch_import_provider_models(db, payload)
        _record_provider_audit(
            db,
            request=request,
            action="batch_import_provider_models",
            entity_id=None,
            entity_name="批量导入模型挂载",
            summary=f"批量导入模型挂载，新增 {result.created_count} 条，更新 {result.updated_count} 条，失败 {result.failed_count} 条",
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
    model_group: str | None = Query(default=None),
    quality_window_hours: int = Query(default=1, ge=1),
    db: Session = Depends(get_db),
) -> ProviderModelMountListResponse:
    try:
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
                model_group=model_group,
                quality_window_hours=quality_window_hours,
            )
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/protocol-detection")
async def detect_selected_provider_endpoint_protocols(
    payload: ProviderEndpointProtocolDetectionRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    provider_ids = list(dict.fromkeys(int(item) for item in payload.provider_ids if int(item) > 0))
    if not provider_ids:
        raise HTTPException(status_code=400, detail="请先选择要检测的提供商")
    existing_ids = set(db.scalars(select(Provider.id).where(Provider.id.in_(provider_ids))))
    missing_ids = [provider_id for provider_id in provider_ids if provider_id not in existing_ids]
    if missing_ids:
        raise HTTPException(status_code=404, detail=f"Provider not found: {', '.join(str(item) for item in missing_ids)}")
    result = await HealthService.detect_endpoint_protocols_for_provider_ids(
        db,
        provider_ids=provider_ids,
        trigger_type="manual_provider_management",
    )
    _record_provider_audit(
        db,
        request=request,
        action="detect_endpoint_protocols_selected_providers",
        entity_id="selected",
        entity_name="选中提供商",
        summary=f"手动检测选中提供商端点协议：{len(provider_ids)} 个",
        detail=result,
        risk_level="medium",
    )
    return result


@router.post("/models/protocol-detection")
async def detect_selected_provider_model_endpoint_protocols(
    payload: ProviderModelEndpointProtocolDetectionRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> dict:
    targets = [
        item.model_dump()
        for item in payload.targets
    ]
    if not targets:
        raise HTTPException(status_code=400, detail="请先选择要检测的模型挂载")
    result = await HealthService.detect_endpoint_protocols_for_provider_model_ids(
        db,
        targets=targets,
        trigger_type="manual_mount_matrix",
    )
    _record_provider_audit(
        db,
        request=request,
        action="detect_endpoint_protocols_selected_provider_models",
        entity_id="selected",
        entity_name="选中模型挂载",
        summary=f"手动检测选中模型挂载端点协议：{len(targets)} 个",
        detail=result,
        risk_level="medium",
    )
    return result


@router.post("", response_model=ProviderOut, status_code=status.HTTP_201_CREATED)
async def create_provider(
    payload: ProviderCreate,
    request: Request,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
) -> ProviderOut:
    try:
        provider = ProviderService.create_provider(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except IntegrityError as exc:
        db.rollback()
        raise HTTPException(status_code=400, detail="提供商名称已存在，请使用其他名称") from exc
    current_user = UserAuthService.get_current_user(request, db)
    _record_provider_audit(
        db,
        request=request,
        action="create_provider",
        entity_id=provider.id,
        entity_name=provider.name,
        summary=f"创建提供商 {provider.name}",
        risk_level="medium",
    )
    background_tasks.add_task(
        _detect_created_provider_protocols_background,
        provider_id=provider.id,
        provider_name=provider.name,
        actor_user_id=getattr(current_user, "id", None),
        actor_username=getattr(current_user, "username", None),
        request_trace_id=getattr(request.state, "trace_id", None),
        source_ip=request.client.host if request.client else None,
    )
    provider_dict = ProviderService.provider_to_dict(provider, metrics=ProviderService._build_quality_metrics(db, [provider]))
    return ProviderOut(**provider_dict)


@router.put("/{provider_id}", response_model=ProviderOut)
async def update_provider(provider_id: int, payload: ProviderUpdate, request: Request, db: Session = Depends(get_db)) -> ProviderOut:
    provider = ProviderService.get_provider(db, provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    before_model_names = {item.model_name for item in provider.provider_models}
    before = ProviderService.provider_to_dict(provider, metrics=ProviderService._build_quality_metrics(db, [provider]))
    provider = ProviderService.update_provider(db, provider, payload)
    added_provider_models = [
        item
        for item in provider.provider_models
        if item.model_name not in before_model_names
    ]
    protocol_detection_result = None
    if added_provider_models and ("models" in payload.model_fields_set or "model_configs" in payload.model_fields_set):
        protocol_detection_result = await HealthService.detect_endpoint_protocols_for_provider_model_ids(
            db,
            targets=[
                {"provider_id": provider.id, "provider_model_id": item.id}
                for item in added_provider_models
            ],
            trigger_type="auto_provider_model_mounted",
        )
        _record_provider_audit(
            db,
            request=request,
            action="detect_endpoint_protocols_auto_provider_model_mounted",
            entity_id=provider.id,
            entity_name=provider.name,
            summary=f"新增模型挂载后检测端点协议：{provider.name}",
            detail=protocol_detection_result,
            risk_level="medium",
        )
        db.refresh(provider)
    after = ProviderService.provider_to_dict(provider, metrics=ProviderService._build_quality_metrics(db, [provider]))
    changed_fields = payload.model_dump(exclude_unset=True)
    if "api_key" in changed_fields and changed_fields["api_key"] is not None:
        changed_fields["api_key"] = ProviderService.mask_api_key(str(changed_fields["api_key"]))
    _record_provider_audit(
        db,
        request=request,
        action="update_provider",
        entity_id=provider.id,
        entity_name=provider.name,
        summary=f"更新提供商 {provider.name}",
        before=before,
        after=after,
        changed_fields={
            **changed_fields,
            **({"endpoint_protocol_detection": protocol_detection_result} if protocol_detection_result else {}),
        },
        risk_level="medium",
    )
    return ProviderOut(**after)


@router.post("/batch/governance", response_model=ProviderBatchGovernanceResponse)
def batch_governance_providers(
    payload: ProviderBatchGovernanceRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> ProviderBatchGovernanceResponse:
    try:
        result = ProviderService.batch_update_provider_governance(
            db,
            provider_ids=payload.provider_ids,
            action=payload.action,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    _record_provider_audit(
        db,
        request=request,
        action=f"batch_governance_{payload.action}",
        entity_id="selected",
        entity_name="批量提供商",
        summary=f"批量执行提供商治理动作：{payload.action}",
        detail={"provider_ids": payload.provider_ids, "result": result},
        risk_level="medium",
    )
    return ProviderBatchGovernanceResponse(**result)


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
        summary=f"触发提供商可用性检测：{provider.name}",
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
        summary=f"触发提供商流式可用性检测：{provider.name}",
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
        _stream_health_check_events(
            worker,
            on_finished=lambda: HealthService.release_manual_check_slot(slot_key),
            request=request,
        ),
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
    provider = db.get(Provider, provider_id)
    if not provider:
        raise HTTPException(status_code=404, detail="Provider not found")
    provider_model = db.scalar(
        select(ProviderModel).where(
            ProviderModel.id == provider_model_id,
            ProviderModel.provider_id == provider.id,
        )
    )
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
        summary=f"触发模型可用性检测：{provider.name} / {provider_model.model_name}",
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
        summary="触发全部提供商可用性检测",
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
        summary="触发全部提供商流式可用性检测",
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
        _stream_health_check_events(
            worker,
            on_finished=lambda: HealthService.release_manual_check_slot(slot_key),
            request=request,
        ),
        media_type="application/x-ndjson",
        headers=_health_stream_headers(),
    )


@router.post("/test-connectivity")
async def test_provider_connectivity(
    payload: ProviderBatchConnectivityTestRequest,
    request: Request,
    db: Session = Depends(get_db),
) -> list[dict]:
    if payload.provider_ids:
        existing_ids = set(
            db.scalars(select(Provider.id).where(Provider.id.in_(payload.provider_ids)))
        )
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
