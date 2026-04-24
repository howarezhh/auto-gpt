from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from app.database import get_db
from app.schemas.log import (
    LogFilterOptionsResponse,
    LogListResponse,
    LogSummaryOut,
    MetricItem,
    MetricListResponse,
    RequestLogOut,
)
from app.services.log_service import LogService


router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("/filter-options", response_model=LogFilterOptionsResponse)
def log_filter_options(
    exclude_health_checks: bool = Query(default=True),
    db: Session = Depends(get_db),
) -> LogFilterOptionsResponse:
    return LogFilterOptionsResponse.model_validate(
        LogService.get_filter_options(db, exclude_health_checks=exclude_health_checks)
    )


@router.get("", response_model=LogListResponse)
def list_logs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    log_type: str | None = None,
    provider_id: int | None = None,
    model_name: str | None = None,
    conversation_key: str | None = None,
    api_client_key_id: int | None = None,
    api_client_key_query: str | None = None,
    success: bool | None = None,
    exclude_health_checks: bool = Query(default=True),
    db: Session = Depends(get_db),
) -> LogListResponse:
    total, items, summary = LogService.list_logs(
        db,
        page=page,
        page_size=page_size,
        log_type=log_type,
        provider_id=provider_id,
        model_name=model_name,
        conversation_key=conversation_key,
        api_client_key_id=api_client_key_id,
        api_client_key_query=api_client_key_query,
        success=success,
        exclude_health_checks=exclude_health_checks,
    )
    return LogListResponse(
        total=total,
        items=[RequestLogOut.model_validate(item) for item in items],
        summary=LogSummaryOut.model_validate(summary),
    )


@router.delete("")
def clear_logs(db: Session = Depends(get_db)) -> dict:
    return {"deleted": LogService.clear_logs(db)}


@router.get("/metrics", response_model=MetricListResponse)
def log_metrics(
    window_minutes: int = Query(default=60, ge=1, le=1440),
    db: Session = Depends(get_db),
) -> MetricListResponse:
    items = [MetricItem.model_validate(item) for item in LogService.metric_summary(db, window_minutes=window_minutes)]
    return MetricListResponse(window_minutes=window_minutes, items=items)
