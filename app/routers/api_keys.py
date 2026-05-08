import csv
import io

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.responses import Response
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.api_key import (
    ApiKeyAnalyticsOut,
    ApiKeyBalanceAdjustmentIn,
    ApiKeyBatchActionIn,
    ApiKeyBatchActionResultOut,
    ApiKeyBatchTemplateApplyIn,
    ApiKeyBatchRotateResultOut,
    ApiKeyBatchProviderUpdateIn,
    ApiKeyBillingSummaryOut,
    ApiKeyCreate,
    ApiKeyCreateResponse,
    ApiKeyCostInsightResponseOut,
    ApiKeyDetailOut,
    ApiKeyListResponse,
    ApiKeyOut,
    ApiKeyStatsOut,
    ApiKeySummaryOut,
    ApiKeyUpdate,
)
from app.schemas.log import LogListResponse, RequestLogOut
from app.services.admin_audit_service import AdminAuditService
from app.services.api_key_admin_service import ApiKeyAdminService
from app.services.log_service import LogService
from app.services.user_auth_service import require_admin_api_user


router = APIRouter(prefix="/api/api-keys", tags=["api-keys"])


@router.get("", response_model=list[ApiKeyOut])
def list_api_keys(db: Session = Depends(get_db)) -> list[ApiKeyOut]:
    return [ApiKeyOut(**ApiKeyAdminService.serialize_api_key(item)) for item in ApiKeyAdminService.list_api_keys(db)]


@router.get("/query", response_model=ApiKeyListResponse)
def query_api_keys(
    keyword: str | None = Query(default=None, max_length=200),
    status: str | None = Query(default=None),
    enabled: bool | None = Query(default=None),
    owner_user_id: int | None = Query(default=None, ge=1),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> ApiKeyListResponse:
    return ApiKeyAdminService.list_api_keys_paginated(
        db,
        keyword=keyword,
        status=status,
        enabled=enabled,
        owner_user_id=owner_user_id,
        page=page,
        page_size=page_size,
    )


@router.get("/summary", response_model=ApiKeySummaryOut)
def api_key_summary(db: Session = Depends(get_db)) -> ApiKeySummaryOut:
    return ApiKeyAdminService.get_summary(db)


@router.get("/export")
def export_api_keys(
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> Response:
    items = ApiKeyAdminService.list_api_keys(db)
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["id", "name", "owner_user_name", "key_prefix", "status", "enabled", "route_mode", "default_provider_id", "allowed_provider_count", "token_limit_total", "remaining_tokens", "cost_limit_total", "balance_amount", "total_cost_used", "created_at", "last_used_at"])
    for item in items:
        serialized = ApiKeyAdminService.serialize_api_key(item)
        writer.writerow([
            serialized["id"],
            serialized["name"],
            serialized["owner_user_name"] or "",
            serialized["key_prefix"],
            serialized["status"],
            "true" if serialized["enabled"] else "false",
            serialized["route_mode"],
            serialized["default_provider_id"] or "",
            len(serialized["allowed_provider_ids"]),
            serialized["token_limit_total"] or "",
            serialized["remaining_tokens"] or "",
            serialized["cost_limit_total"] or "",
            serialized["balance_amount"] or "",
            serialized["total_cost_used"] or 0,
            serialized["created_at"].isoformat() if serialized["created_at"] else "",
            serialized["last_used_at"].isoformat() if serialized["last_used_at"] else "",
        ])
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="export",
        entity_type="api_key",
        entity_id=None,
        entity_name="all_api_keys",
        summary="导出 API Key 列表 CSV",
    )
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="api-keys-export.csv"'},
    )


@router.post("", response_model=ApiKeyCreateResponse, status_code=status.HTTP_201_CREATED)
def create_api_key(
    payload: ApiKeyCreate,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> ApiKeyCreateResponse:
    try:
        api_key, raw_api_key = ApiKeyAdminService.create_api_key(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="create",
        entity_type="api_key",
        entity_id=api_key.id,
        entity_name=api_key.name,
        target_user_id=api_key.owner_user_id,
        summary=f"创建 API Key {api_key.name}",
        detail=payload.model_dump(mode="json"),
    )
    data = ApiKeyAdminService.serialize_api_key(api_key)
    data["raw_api_key"] = raw_api_key
    return ApiKeyCreateResponse(**data)


@router.get("/{api_key_id}", response_model=ApiKeyDetailOut)
def get_api_key(
    api_key_id: int,
    window_hours: int = Query(default=24, ge=1, le=720),
    db: Session = Depends(get_db),
) -> ApiKeyDetailOut:
    api_key = ApiKeyAdminService.get_api_key(db, api_key_id)
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    return ApiKeyAdminService.serialize_api_key_detail(db, api_key, window_hours=window_hours)


@router.put("/{api_key_id}", response_model=ApiKeyOut)
def update_api_key(
    api_key_id: int,
    payload: ApiKeyUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> ApiKeyOut:
    api_key = ApiKeyAdminService.get_api_key(db, api_key_id)
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    try:
        updated = ApiKeyAdminService.update_api_key(db, api_key, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="update",
        entity_type="api_key",
        entity_id=updated.id,
        entity_name=updated.name,
        target_user_id=updated.owner_user_id,
        summary=f"更新 API Key {updated.name}",
        detail=payload.model_dump(mode="json", exclude_unset=True),
    )
    return ApiKeyOut(**ApiKeyAdminService.serialize_api_key(updated))


@router.delete("/{api_key_id}")
def delete_api_key(
    api_key_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> dict:
    api_key = ApiKeyAdminService.get_api_key(db, api_key_id)
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    name = api_key.name
    owner_user_id = api_key.owner_user_id
    ApiKeyAdminService.delete_api_key(db, api_key)
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="delete",
        entity_type="api_key",
        entity_id=api_key_id,
        entity_name=name,
        target_user_id=owner_user_id,
        summary=f"删除 API Key {name}",
    )
    return {"message": "deleted"}


@router.post("/{api_key_id}/enable", response_model=ApiKeyOut)
def enable_api_key(
    api_key_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> ApiKeyOut:
    api_key = ApiKeyAdminService.get_api_key(db, api_key_id)
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    updated = ApiKeyAdminService.set_enabled(db, api_key, True)
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="enable",
        entity_type="api_key",
        entity_id=updated.id,
        entity_name=updated.name,
        target_user_id=updated.owner_user_id,
        summary=f"启用 API Key {updated.name}",
    )
    return ApiKeyOut(**ApiKeyAdminService.serialize_api_key(updated))


@router.post("/{api_key_id}/disable", response_model=ApiKeyOut)
def disable_api_key(
    api_key_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> ApiKeyOut:
    api_key = ApiKeyAdminService.get_api_key(db, api_key_id)
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    updated = ApiKeyAdminService.set_enabled(db, api_key, False)
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="disable",
        entity_type="api_key",
        entity_id=updated.id,
        entity_name=updated.name,
        target_user_id=updated.owner_user_id,
        summary=f"禁用 API Key {updated.name}",
    )
    return ApiKeyOut(**ApiKeyAdminService.serialize_api_key(updated))


@router.post("/batch/enable", response_model=ApiKeyBatchActionResultOut)
def batch_enable_api_keys(
    payload: ApiKeyBatchActionIn,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> ApiKeyBatchActionResultOut:
    result = ApiKeyAdminService.batch_enable(db, payload)
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="batch_enable",
        entity_type="api_key",
        entity_id=None,
        entity_name="batch",
        summary=f"批量启用 API Key {result.affected_count} 个",
        detail=payload.model_dump(),
    )
    return result


@router.post("/batch/disable", response_model=ApiKeyBatchActionResultOut)
def batch_disable_api_keys(
    payload: ApiKeyBatchActionIn,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> ApiKeyBatchActionResultOut:
    result = ApiKeyAdminService.batch_disable(db, payload)
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="batch_disable",
        entity_type="api_key",
        entity_id=None,
        entity_name="batch",
        summary=f"批量禁用 API Key {result.affected_count} 个",
        detail=payload.model_dump(),
    )
    return result


@router.post("/batch/delete", response_model=ApiKeyBatchActionResultOut)
def batch_delete_api_keys(
    payload: ApiKeyBatchActionIn,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> ApiKeyBatchActionResultOut:
    result = ApiKeyAdminService.batch_delete(db, payload)
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="batch_delete",
        entity_type="api_key",
        entity_id=None,
        entity_name="batch",
        summary=f"批量删除 API Key {result.affected_count} 个",
        detail=payload.model_dump(),
    )
    return result


@router.post("/batch/rotate", response_model=ApiKeyBatchRotateResultOut)
def batch_rotate_api_keys(
    payload: ApiKeyBatchActionIn,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> ApiKeyBatchRotateResultOut:
    result = ApiKeyAdminService.batch_rotate(db, payload)
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="batch_rotate",
        entity_type="api_key",
        entity_id=None,
        entity_name="batch",
        summary=f"批量轮换 API Key {result.affected_count} 个",
        detail={"api_key_ids": payload.api_key_ids},
    )
    return result


@router.post("/batch/expire", response_model=ApiKeyBatchActionResultOut)
def batch_expire_api_keys(
    payload: ApiKeyBatchActionIn,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> ApiKeyBatchActionResultOut:
    result = ApiKeyAdminService.batch_expire(db, payload)
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="batch_expire",
        entity_type="api_key",
        entity_id=None,
        entity_name="batch",
        summary=f"批量过期 API Key {result.affected_count} 个",
        detail={"api_key_ids": payload.api_key_ids},
    )
    return result


@router.post("/batch/providers", response_model=ApiKeyBatchActionResultOut)
def batch_update_api_key_providers(
    payload: ApiKeyBatchProviderUpdateIn,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> ApiKeyBatchActionResultOut:
    try:
        result = ApiKeyAdminService.batch_update_providers(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="batch_update_providers",
        entity_type="api_key",
        entity_id=None,
        entity_name="batch",
        summary=f"批量更新 API Key 渠道授权 {result.affected_count} 个",
        detail=payload.model_dump(),
    )
    return result


@router.post("/batch/template", response_model=ApiKeyBatchActionResultOut)
def batch_apply_api_key_template(
    payload: ApiKeyBatchTemplateApplyIn,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> ApiKeyBatchActionResultOut:
    try:
        result = ApiKeyAdminService.batch_apply_template(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="batch_apply_template",
        entity_type="api_key",
        entity_id=None,
        entity_name="batch",
        summary=f"批量应用 API Key 策略模板 {result.affected_count} 个",
        detail=payload.model_dump(),
    )
    return result


@router.get("/insights/cost", response_model=ApiKeyCostInsightResponseOut)
def api_key_cost_insights(
    group_by: str = Query(default="user"),
    window_days: int = Query(default=30, ge=1, le=365),
    limit: int = Query(default=20, ge=1, le=100),
    db: Session = Depends(get_db),
) -> ApiKeyCostInsightResponseOut:
    return ApiKeyAdminService.get_cost_insights(
        db,
        group_by=group_by,
        window_days=window_days,
        limit=limit,
    )


@router.get("/{api_key_id}/logs", response_model=LogListResponse)
def api_key_logs(
    api_key_id: int,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    log_type: str | None = None,
    success: bool | None = None,
    db: Session = Depends(get_db),
) -> LogListResponse:
    api_key = ApiKeyAdminService.get_api_key(db, api_key_id)
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    total, items = ApiKeyAdminService.get_logs(
        db,
        api_key_id=api_key_id,
        page=page,
        page_size=page_size,
        log_type=log_type,
        success=success,
    )
    return LogListResponse(total=total, items=[RequestLogOut.model_validate(item) for item in LogService.serialize_logs(items)], summary=None)


@router.get("/{api_key_id}/stats", response_model=ApiKeyStatsOut)
def api_key_stats(
    api_key_id: int,
    window_hours: int = Query(default=24, ge=1, le=720),
    db: Session = Depends(get_db),
) -> ApiKeyStatsOut:
    api_key = ApiKeyAdminService.get_api_key(db, api_key_id)
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    return ApiKeyAdminService.get_stats(db, api_key_id=api_key_id, window_hours=window_hours)


@router.get("/{api_key_id}/analytics", response_model=ApiKeyAnalyticsOut)
def api_key_analytics(
    api_key_id: int,
    recent_error_limit: int = Query(default=8, ge=1, le=50),
    model_limit: int = Query(default=12, ge=1, le=50),
    db: Session = Depends(get_db),
) -> ApiKeyAnalyticsOut:
    api_key = ApiKeyAdminService.get_api_key(db, api_key_id)
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    return ApiKeyAdminService.get_analytics(
        db,
        api_key_id=api_key_id,
        recent_error_limit=recent_error_limit,
        model_limit=model_limit,
    )


@router.get("/{api_key_id}/billing", response_model=ApiKeyBillingSummaryOut)
def api_key_billing(
    api_key_id: int,
    limit: int = Query(default=50, ge=1, le=200),
    db: Session = Depends(get_db),
) -> ApiKeyBillingSummaryOut:
    api_key = ApiKeyAdminService.get_api_key(db, api_key_id)
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    return ApiKeyAdminService.get_billing_summary(db, api_key_id=api_key_id, limit=limit)


@router.post("/{api_key_id}/billing/adjust", response_model=ApiKeyBillingSummaryOut)
def adjust_api_key_balance(
    api_key_id: int,
    payload: ApiKeyBalanceAdjustmentIn,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> ApiKeyBillingSummaryOut:
    api_key = ApiKeyAdminService.get_api_key(db, api_key_id)
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    summary = ApiKeyAdminService.adjust_balance(db, api_key=api_key, payload=payload)
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="adjust_balance",
        entity_type="api_key",
        entity_id=api_key.id,
        entity_name=api_key.name,
        target_user_id=api_key.owner_user_id,
        summary=f"调整 API Key {api_key.name} 余额",
        detail=payload.model_dump(),
    )
    return summary
