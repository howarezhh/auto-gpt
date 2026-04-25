from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.api_key import (
    ApiKeyAnalyticsOut,
    ApiKeyBalanceAdjustmentIn,
    ApiKeyBillingSummaryOut,
    ApiKeyCreate,
    ApiKeyCreateResponse,
    ApiKeyDetailOut,
    ApiKeyOut,
    ApiKeyStatsOut,
    ApiKeySummaryOut,
    ApiKeyUpdate,
)
from app.schemas.log import LogListResponse, RequestLogOut
from app.services.api_key_admin_service import ApiKeyAdminService


router = APIRouter(prefix="/api/api-keys", tags=["api-keys"])


@router.get("", response_model=list[ApiKeyOut])
def list_api_keys(db: Session = Depends(get_db)) -> list[ApiKeyOut]:
    return [ApiKeyOut(**ApiKeyAdminService.serialize_api_key(item)) for item in ApiKeyAdminService.list_api_keys(db)]


@router.get("/summary", response_model=ApiKeySummaryOut)
def api_key_summary(db: Session = Depends(get_db)) -> ApiKeySummaryOut:
    return ApiKeyAdminService.get_summary(db)


@router.post("", response_model=ApiKeyCreateResponse, status_code=status.HTTP_201_CREATED)
def create_api_key(payload: ApiKeyCreate, db: Session = Depends(get_db)) -> ApiKeyCreateResponse:
    try:
        api_key, raw_api_key = ApiKeyAdminService.create_api_key(db, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
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
def update_api_key(api_key_id: int, payload: ApiKeyUpdate, db: Session = Depends(get_db)) -> ApiKeyOut:
    api_key = ApiKeyAdminService.get_api_key(db, api_key_id)
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    try:
        updated = ApiKeyAdminService.update_api_key(db, api_key, payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return ApiKeyOut(**ApiKeyAdminService.serialize_api_key(updated))


@router.delete("/{api_key_id}")
def delete_api_key(api_key_id: int, db: Session = Depends(get_db)) -> dict:
    api_key = ApiKeyAdminService.get_api_key(db, api_key_id)
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    ApiKeyAdminService.delete_api_key(db, api_key)
    return {"message": "deleted"}


@router.post("/{api_key_id}/enable", response_model=ApiKeyOut)
def enable_api_key(api_key_id: int, db: Session = Depends(get_db)) -> ApiKeyOut:
    api_key = ApiKeyAdminService.get_api_key(db, api_key_id)
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    updated = ApiKeyAdminService.set_enabled(db, api_key, True)
    return ApiKeyOut(**ApiKeyAdminService.serialize_api_key(updated))


@router.post("/{api_key_id}/disable", response_model=ApiKeyOut)
def disable_api_key(api_key_id: int, db: Session = Depends(get_db)) -> ApiKeyOut:
    api_key = ApiKeyAdminService.get_api_key(db, api_key_id)
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    updated = ApiKeyAdminService.set_enabled(db, api_key, False)
    return ApiKeyOut(**ApiKeyAdminService.serialize_api_key(updated))


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
    return LogListResponse(total=total, items=[RequestLogOut.model_validate(item) for item in items], summary=None)


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
) -> ApiKeyBillingSummaryOut:
    api_key = ApiKeyAdminService.get_api_key(db, api_key_id)
    if api_key is None:
        raise HTTPException(status_code=404, detail="API key not found")
    return ApiKeyAdminService.adjust_balance(db, api_key=api_key, payload=payload)
