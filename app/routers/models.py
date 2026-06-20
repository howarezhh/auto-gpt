from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import PlainTextResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.provider_model import ProviderModel
from app.schemas.model_catalog import (
    ModelCatalogBatchImportRequest,
    ModelCatalogBatchImportResponse,
    ModelCatalogBatchContextWindowUpdate,
    ModelCatalogCreate,
    ModelCatalogDetailOut,
    ModelCatalogOptionOut,
    ModelCatalogOut,
)
from app.schemas.model_catalog import ModelCatalogPageOut, ModelCatalogUpdate, UserModelOut
from app.schemas.model_mapping import (
    ModelMappingCreate,
    ModelMappingOut,
    ModelMappingSelectionOut,
    ModelMappingSelectionProbe,
    ModelMappingUpdate,
)
from app.services.asset_service import AssetService
from app.services.admin_audit_service import AdminAuditService
from app.services.health_service import HealthService
from app.services.model_catalog_service import ModelCatalogService
from app.services.model_mapping_service import ModelMappingService
from app.services.user_auth_service import require_admin_api_user, require_session_api_user
from app.utils.test_features import normalize_test_features, phase_keys_from_test_features


router = APIRouter(tags=["models"])


@router.get("/api/models", dependencies=[Depends(require_admin_api_user)])
def list_models(
    paginated: bool = Query(default=True),
    keyword: str | None = Query(default=None),
    enabled: bool | None = Query(default=None),
    health_status: str | None = Query(default=None),
    model_group: str | None = Query(default=None),
    provider_id: int | None = Query(default=None, ge=1),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=10, le=100),
    db: Session = Depends(get_db),
) -> list[ModelCatalogOut] | ModelCatalogPageOut:
    if paginated:
        try:
            payload = ModelCatalogService.list_model_page(
                db,
                keyword=keyword,
                enabled=enabled,
                health_status=health_status,
                model_group=model_group,
                provider_id=provider_id,
                page=page,
                page_size=page_size,
            )
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        return ModelCatalogPageOut(**payload)
    return [ModelCatalogOut(**item) for item in ModelCatalogService.list_model_dicts(db)[:500]]


@router.get("/api/models/options", response_model=list[ModelCatalogOptionOut], dependencies=[Depends(require_admin_api_user)])
def list_model_options(db: Session = Depends(get_db)) -> list[ModelCatalogOptionOut]:
    return [ModelCatalogOptionOut(**item) for item in ModelCatalogService.list_model_option_dicts(db)]


@router.post("/api/models", response_model=ModelCatalogDetailOut, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_admin_api_user)])
async def create_model(
    payload: ModelCatalogCreate,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> ModelCatalogDetailOut:
    try:
        catalog = ModelCatalogService.create_model(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="create",
        entity_type="model",
        entity_id=catalog.id,
        entity_name=catalog.model_name,
        summary=f"创建模型 {catalog.model_name}",
        detail=payload.model_dump(),
    )
    detail = ModelCatalogService.get_model_detail(db, catalog.model_name)
    return ModelCatalogDetailOut(**detail)


@router.post(
    "/api/models/batch/context-window",
    response_model=list[ModelCatalogOut],
    dependencies=[Depends(require_admin_api_user)],
)
def batch_update_model_context_window(
    payload: ModelCatalogBatchContextWindowUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> list[ModelCatalogOut]:
    try:
        catalogs = ModelCatalogService.batch_update_context_window(
            db,
            model_names=payload.model_names,
            context_window_tokens=payload.context_window_tokens,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="batch_update",
        entity_type="model",
        entity_name="模型上下文窗口",
        summary=f"批量更新 {len(catalogs)} 个模型的最大上下文窗口",
        detail=payload.model_dump(),
    )
    providers = ModelCatalogService._load_catalogs_and_providers(db)[1]
    return [
        ModelCatalogOut(**ModelCatalogService._serialize_catalog(catalog, providers))
        for catalog in catalogs
    ]


@router.post("/api/models/test-all", dependencies=[Depends(require_admin_api_user)])
async def test_all_model_health(payload: dict | None = None, db: Session = Depends(get_db)) -> list[dict]:
    return await ModelCatalogService.test_all_model_health(
        db,
        phase_keys=phase_keys_from_test_features(normalize_test_features(payload)),
    )


@router.get("/api/models/batch-import-template", dependencies=[Depends(require_admin_api_user)])
def get_model_batch_import_template() -> dict:
    return {"template": ModelCatalogService.MODEL_CATALOG_BATCH_IMPORT_TEMPLATE}


@router.get("/api/models/export", dependencies=[Depends(require_admin_api_user)])
def export_models(db: Session = Depends(get_db)) -> PlainTextResponse:
    text = ModelCatalogService.export_models_import_text(db)
    return PlainTextResponse(
        text,
        media_type="text/plain; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="models-import-template.txt"'},
    )


@router.post(
    "/api/models/batch-import",
    response_model=ModelCatalogBatchImportResponse,
    dependencies=[Depends(require_admin_api_user)],
)
def batch_import_models(
    payload: ModelCatalogBatchImportRequest,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> ModelCatalogBatchImportResponse:
    try:
        result = ModelCatalogService.batch_import_models(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not payload.dry_run:
        AdminAuditService.create_log(
            db,
            actor_user_id=current_user.id,
            actor_username=current_user.username,
            action="batch_import",
            entity_type="model",
            entity_name="批量导入模型",
            summary=f"批量导入模型，创建 {result.created_count} 条，失败 {result.failed_count} 条",
            detail=result.model_dump(),
        )
    return result


@router.post("/api/models/{model_name:path}/test", dependencies=[Depends(require_admin_api_user)])
async def test_model_health(model_name: str, payload: dict | None = None, db: Session = Depends(get_db)) -> dict:
    try:
        return await ModelCatalogService.test_model_health(
            db,
            model_name,
            phase_keys=phase_keys_from_test_features(normalize_test_features(payload, single_model=True)),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/api/models/{model_name:path}", response_model=ModelCatalogDetailOut, dependencies=[Depends(require_admin_api_user)])
def get_model_detail(model_name: str, db: Session = Depends(get_db)) -> ModelCatalogDetailOut:
    detail = ModelCatalogService.get_model_detail(db, model_name)
    if detail is None:
        raise HTTPException(status_code=404, detail="模型不存在")
    return ModelCatalogDetailOut(**detail)


@router.put("/api/models/{model_name:path}", response_model=ModelCatalogDetailOut, dependencies=[Depends(require_admin_api_user)])
async def update_model(
    model_name: str,
    payload: ModelCatalogUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> ModelCatalogDetailOut:
    catalog = ModelCatalogService.get_catalog(db, model_name)
    if catalog is None:
        raise HTTPException(status_code=404, detail="模型不存在")
    before_provider_ids = set(
        db.scalars(select(ProviderModel.provider_id).where(ProviderModel.model_name == model_name))
    )
    try:
        ModelCatalogService.update_model(db, catalog, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    protocol_detection_result = None
    if "provider_bindings" in payload.model_fields_set:
        new_provider_models = [
            item
            for item in db.scalars(select(ProviderModel).where(ProviderModel.model_name == model_name))
            if item.provider_id not in before_provider_ids
        ]
        if new_provider_models:
            protocol_detection_result = await HealthService.detect_endpoint_protocols_for_provider_model_ids(
                db,
                targets=[
                    {"provider_id": item.provider_id, "provider_model_id": item.id}
                    for item in new_provider_models
                ],
                trigger_type="auto_provider_model_mounted",
            )
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="update",
        entity_type="model",
        entity_id=catalog.id,
        entity_name=catalog.model_name,
        summary=f"更新模型 {catalog.model_name}",
        detail={
            **payload.model_dump(exclude_unset=True),
            **({"endpoint_protocol_detection": protocol_detection_result} if protocol_detection_result else {}),
        },
    )
    detail = ModelCatalogService.get_model_detail(db, model_name)
    return ModelCatalogDetailOut(**detail)


@router.delete("/api/models/{model_name:path}", dependencies=[Depends(require_admin_api_user)])
def delete_model(
    model_name: str,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> dict:
    catalog = ModelCatalogService.get_catalog(db, model_name)
    if catalog is None:
        raise HTTPException(status_code=404, detail="模型不存在")
    entity_id = catalog.id
    entity_name = catalog.model_name
    ModelCatalogService.delete_model(db, catalog)
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="delete",
        entity_type="model",
        entity_id=entity_id,
        entity_name=entity_name,
        summary=f"删除模型 {entity_name}",
    )
    return {"message": "deleted"}


@router.get("/api/model-mappings", response_model=list[ModelMappingOut], dependencies=[Depends(require_admin_api_user)])
def list_model_mappings(db: Session = Depends(get_db)) -> list[ModelMappingOut]:
    return [ModelMappingOut(**item) for item in ModelMappingService.list_mappings(db)]


@router.post("/api/model-mappings", response_model=ModelMappingOut, status_code=status.HTTP_201_CREATED, dependencies=[Depends(require_admin_api_user)])
def create_model_mapping(
    payload: ModelMappingCreate,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> ModelMappingOut:
    try:
        mapping = ModelMappingService.create_mapping(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="create",
        entity_type="model_mapping",
        entity_id=mapping.id,
        entity_name=mapping.source_model_name,
        summary=f"创建模型映射 {mapping.source_model_name}",
        detail=payload.model_dump(),
    )
    return ModelMappingOut(**ModelMappingService.serialize_mapping(mapping))


@router.put("/api/model-mappings/{source_model_name:path}", response_model=ModelMappingOut, dependencies=[Depends(require_admin_api_user)])
def update_model_mapping(
    source_model_name: str,
    payload: ModelMappingUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> ModelMappingOut:
    mapping = ModelMappingService.get_mapping(db, source_model_name)
    if mapping is None:
        raise HTTPException(status_code=404, detail="模型映射不存在")
    try:
        mapping = ModelMappingService.update_mapping(db, mapping, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="update",
        entity_type="model_mapping",
        entity_id=mapping.id,
        entity_name=mapping.source_model_name,
        summary=f"更新模型映射 {mapping.source_model_name}",
        detail=payload.model_dump(exclude_unset=True),
    )
    return ModelMappingOut(**ModelMappingService.serialize_mapping(mapping))


@router.delete("/api/model-mappings/{source_model_name:path}", dependencies=[Depends(require_admin_api_user)])
def delete_model_mapping(
    source_model_name: str,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> dict:
    mapping = ModelMappingService.get_mapping(db, source_model_name)
    if mapping is None:
        raise HTTPException(status_code=404, detail="模型映射不存在")
    entity_id = mapping.id
    entity_name = mapping.source_model_name
    ModelMappingService.delete_mapping(db, mapping)
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="delete",
        entity_type="model_mapping",
        entity_id=entity_id,
        entity_name=entity_name,
        summary=f"删除模型映射 {entity_name}",
    )
    return {"message": "deleted"}


@router.post("/api/model-mappings/select", response_model=ModelMappingSelectionOut, dependencies=[Depends(require_admin_api_user)])
async def select_model_mapping_target(payload: ModelMappingSelectionProbe, db: Session = Depends(get_db)) -> ModelMappingSelectionOut:
    resolution = await ModelMappingService.resolve_for_request(
        source_model_name=payload.source_model_name,
        api_client_auth=None,
        sticky_key=payload.source_model_name,
    )
    if resolution is None:
        mapping = ModelMappingService.get_mapping(db, payload.source_model_name)
        reason = "model_mapping_not_found" if mapping is None else "model_mapping_disabled_or_no_available_target"
        return ModelMappingSelectionOut(
            mapped=False,
            source_model_name=payload.source_model_name,
            selected_model_name=payload.source_model_name,
            trace={
                "result": "model_mapping_not_applied",
                "reason": reason,
                "source_model_name": payload.source_model_name,
                "mapping_id": getattr(mapping, "id", None),
                "mapping_enabled": getattr(mapping, "enabled", None),
            },
        )
    return ModelMappingSelectionOut(
        mapped=resolution.selected_model_name != resolution.source_model_name,
        source_model_name=resolution.source_model_name,
        selected_model_name=resolution.selected_model_name,
        trace=resolution.trace,
    )


@router.get("/api/user/models", response_model=list[UserModelOut])
def list_user_models(current_user=Depends(require_session_api_user), db: Session = Depends(get_db)) -> list[UserModelOut]:
    return [UserModelOut(**item) for item in ModelCatalogService.list_user_models(db, user=current_user)]


@router.post("/api/user/assets/upload")
def upload_user_asset(
    request: Request,
    current_user=Depends(require_session_api_user),
    file: UploadFile = File(...),
    db: Session = Depends(get_db),
) -> dict:
    asset = AssetService.create_uploaded_image(
        db,
        upload_file=file,
        actor_type="user",
        actor_id=getattr(current_user, "id", None),
        storage_scope="user_asset",
        trace_id=getattr(request.state, "trace_id", None),
    )
    return {
        "id": asset.id,
        "filename": asset.filename,
        "content_type": asset.content_type,
        "file_size_bytes": asset.file_size_bytes,
        "public_path": asset.public_path,
        "asset_url": str(request.base_url).rstrip("/") + asset.public_path,
    }
