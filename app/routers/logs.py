from datetime import datetime

from fastapi import APIRouter, Depends, Query
from fastapi.responses import Response
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
    model_query: str | None = None,
    conversation_key: str | None = None,
    api_client_key_id: int | None = None,
    api_client_key_query: str | None = None,
    user_account_id: int | None = None,
    user_account_query: str | None = None,
    tenant_name: str | None = None,
    project_name: str | None = None,
    app_name: str | None = None,
    environment_name: str | None = None,
    success: bool | None = None,
    exclude_health_checks: bool = Query(default=True),
    db: Session = Depends(get_db),
) -> LogListResponse:
    total, items, summary = LogService.list_logs(
        db,
        page=page,
        page_size=page_size,
        log_type=log_type,
        log_types=None,
        provider_id=provider_id,
        model_name=model_name,
        model_query=model_query,
        conversation_key=conversation_key,
        api_client_key_id=api_client_key_id,
        api_client_key_query=api_client_key_query,
        user_account_id=user_account_id,
        user_account_query=user_account_query,
        tenant_name=tenant_name,
        project_name=project_name,
        app_name=app_name,
        environment_name=environment_name,
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


@router.get("/export")
def export_logs(
    log_type: str | None = None,
    provider_id: int | None = None,
    model_name: str | None = None,
    model_query: str | None = None,
    conversation_key: str | None = None,
    api_client_key_id: int | None = None,
    api_client_key_query: str | None = None,
    user_account_id: int | None = None,
    user_account_query: str | None = None,
    tenant_name: str | None = None,
    project_name: str | None = None,
    app_name: str | None = None,
    environment_name: str | None = None,
    success: bool | None = None,
    exclude_health_checks: bool = Query(default=True),
    limit: int = Query(default=5000, ge=1, le=10000),
    db: Session = Depends(get_db),
) -> Response:
    csv_text = LogService.export_logs_csv(
        db,
        log_type=log_type,
        log_types=None,
        provider_id=provider_id,
        model_name=model_name,
        model_query=model_query,
        conversation_key=conversation_key,
        api_client_key_id=api_client_key_id,
        api_client_key_query=api_client_key_query,
        user_account_id=user_account_id,
        user_account_query=user_account_query,
        tenant_name=tenant_name,
        project_name=project_name,
        app_name=app_name,
        environment_name=environment_name,
        success=success,
        exclude_health_checks=exclude_health_checks,
        limit=limit,
    )
    filename = f"logs-export-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/metrics", response_model=MetricListResponse)
def log_metrics(
    window_minutes: int = Query(default=60, ge=1, le=1440),
    db: Session = Depends(get_db),
) -> MetricListResponse:
    items = [MetricItem.model_validate(item) for item in LogService.metric_summary(db, window_minutes=window_minutes)]
    return MetricListResponse(window_minutes=window_minutes, items=items)
