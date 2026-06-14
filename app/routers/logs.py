from app.utils.timezone import now_beijing
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query, Request
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
from app.services.admin_audit_service import AdminAuditService
from app.services.request_log_queue_service import RequestLogQueueService
from app.services.user_auth_service import UserAuthService


router = APIRouter(prefix="/api/logs", tags=["logs"])


@router.get("/filter-options", response_model=LogFilterOptionsResponse)
def log_filter_options(
    exclude_health_checks: bool = Query(default=True),
    limit: int = Query(default=200, ge=1, le=500),
    db: Session = Depends(get_db),
) -> LogFilterOptionsResponse:
    return LogFilterOptionsResponse.model_validate(
        LogService.get_filter_options(db, exclude_health_checks=exclude_health_checks, limit=limit)
    )


@router.get("", response_model=LogListResponse)
async def list_logs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    log_type: str | None = None,
    provider_id: int | None = None,
    provider_trust_level: str | None = None,
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
    content_guard_result: str | None = None,
    content_guard_risk_level: str | None = None,
    content_guard_action: str | None = None,
    content_guard_final_strategy: str | None = None,
    content_guard_retry_count: int | None = Query(default=None, ge=0),
    content_guard_guard_stage: str | None = None,
    content_guard_category: str | None = None,
    content_guard_switched_provider: bool | None = None,
    content_guard_adaptation_skipped: bool | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    exclude_health_checks: bool = Query(default=True),
    wait_for_latest: bool = Query(default=False),
    wait_timeout_ms: int = Query(default=2000, ge=0, le=10000),
    db: Session = Depends(get_db),
) -> LogListResponse:
    queue_status: dict | None = None
    if wait_for_latest:
        queue_status = await RequestLogQueueService.wait_until_idle(
            timeout_seconds=wait_timeout_ms / 1000,
            poll_interval_seconds=0.05,
        )
    total, items, summary = LogService.list_logs(
        db,
        page=page,
        page_size=page_size,
        log_type=log_type,
        log_types=None,
        provider_id=provider_id,
        provider_trust_level=provider_trust_level,
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
        content_guard_result=content_guard_result,
        content_guard_risk_level=content_guard_risk_level,
        content_guard_action=content_guard_action,
        content_guard_final_strategy=content_guard_final_strategy,
        content_guard_retry_count=content_guard_retry_count,
        content_guard_guard_stage=content_guard_guard_stage,
        content_guard_category=content_guard_category,
        content_guard_switched_provider=content_guard_switched_provider,
        content_guard_adaptation_skipped=content_guard_adaptation_skipped,
        start_at=start_at,
        end_at=end_at,
    )
    return LogListResponse(
        total=total,
        items=[
            RequestLogOut.model_validate(item)
            for item in LogService.serialize_logs(
                items,
                include_payload_fields=False,
                derive_image_observability=False,
            )
        ],
        summary=LogSummaryOut.model_validate(summary),
        queue_idle=None if queue_status is None else bool(queue_status.get("idle")),
        queue_timed_out=None if queue_status is None else bool(queue_status.get("timed_out")),
        queued_request_logs=None if queue_status is None else int(queue_status.get("queued") or 0),
        processing_request_logs=None if queue_status is None else int(queue_status.get("processing") or 0),
        dead_letter_request_logs=None if queue_status is None else int(queue_status.get("dead_letter") or 0),
        failed_request_log_writes=None if queue_status is None else int(queue_status.get("failure_count") or 0),
    )


@router.delete("")
async def clear_logs(request: Request, db: Session = Depends(get_db)) -> dict:
    queue_status = await RequestLogQueueService.wait_until_idle(
        timeout_seconds=2,
        poll_interval_seconds=0.05,
    )
    discarded_queue = (
        RequestLogQueueService.discard_pending()
        if not bool(queue_status.get("idle"))
        else {"ingress": 0, "queued": 0, "processing": 0}
    )
    deleted = LogService.clear_logs(db)
    _record_log_admin_audit(
        db,
        request=request,
        action="clear_logs",
        summary=f"清空请求日志 {deleted} 条",
        detail={"deleted": deleted},
        risk_level="high",
    )
    return {
        "deleted": deleted,
        "queue_idle_before_clear": bool(queue_status.get("idle")),
        "discarded_pending_request_logs": discarded_queue,
    }


@router.delete("/filtered")
async def delete_filtered_logs(
    request: Request,
    log_type: str | None = None,
    provider_id: int | None = None,
    provider_trust_level: str | None = None,
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
    content_guard_result: str | None = None,
    content_guard_risk_level: str | None = None,
    content_guard_action: str | None = None,
    content_guard_final_strategy: str | None = None,
    content_guard_retry_count: int | None = Query(default=None, ge=0),
    content_guard_guard_stage: str | None = None,
    content_guard_category: str | None = None,
    content_guard_switched_provider: bool | None = None,
    content_guard_adaptation_skipped: bool | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    exclude_health_checks: bool = Query(default=True),
    db: Session = Depends(get_db),
) -> dict:
    filter_snapshot = {
        "log_type": log_type,
        "provider_id": provider_id,
        "provider_trust_level": provider_trust_level,
        "model_name": model_name,
        "model_query": model_query,
        "conversation_key": conversation_key,
        "api_client_key_id": api_client_key_id,
        "api_client_key_query": api_client_key_query,
        "user_account_id": user_account_id,
        "user_account_query": user_account_query,
        "tenant_name": tenant_name,
        "project_name": project_name,
        "app_name": app_name,
        "environment_name": environment_name,
        "success": success,
        "content_guard_result": content_guard_result,
        "content_guard_risk_level": content_guard_risk_level,
        "content_guard_action": content_guard_action,
        "content_guard_final_strategy": content_guard_final_strategy,
        "content_guard_retry_count": content_guard_retry_count,
        "content_guard_guard_stage": content_guard_guard_stage,
        "content_guard_category": content_guard_category,
        "content_guard_switched_provider": content_guard_switched_provider,
        "content_guard_adaptation_skipped": content_guard_adaptation_skipped,
        "start_at": start_at.isoformat() if start_at else None,
        "end_at": end_at.isoformat() if end_at else None,
    }
    if not _has_log_delete_scope(filter_snapshot):
        raise HTTPException(status_code=400, detail="删除日志必须至少指定一个筛选条件或时间范围")
    queue_status = await RequestLogQueueService.wait_until_idle(
        timeout_seconds=2,
        poll_interval_seconds=0.05,
    )
    deleted = LogService.delete_logs_by_filters(
        db,
        log_type=log_type,
        log_types=None,
        provider_id=provider_id,
        provider_trust_level=provider_trust_level,
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
        content_guard_result=content_guard_result,
        content_guard_risk_level=content_guard_risk_level,
        content_guard_action=content_guard_action,
        content_guard_final_strategy=content_guard_final_strategy,
        content_guard_retry_count=content_guard_retry_count,
        content_guard_guard_stage=content_guard_guard_stage,
        content_guard_category=content_guard_category,
        content_guard_switched_provider=content_guard_switched_provider,
        content_guard_adaptation_skipped=content_guard_adaptation_skipped,
        start_at=start_at,
        end_at=end_at,
    )
    _record_log_admin_audit(
        db,
        request=request,
        action="delete_filtered_logs",
        summary=f"按筛选删除请求日志 {deleted} 条",
        detail={
            "deleted": deleted,
            "filters": filter_snapshot,
            "exclude_health_checks": exclude_health_checks,
            "queue_idle_before_delete": bool(queue_status.get("idle")),
        },
        risk_level="high",
    )
    return {
        "deleted": deleted,
        "queue_idle_before_delete": bool(queue_status.get("idle")),
        "queue_timed_out": bool(queue_status.get("timed_out")),
    }


@router.get("/export")
async def export_logs(
    request: Request,
    log_type: str | None = None,
    provider_id: int | None = None,
    provider_trust_level: str | None = None,
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
    content_guard_result: str | None = None,
    content_guard_risk_level: str | None = None,
    content_guard_action: str | None = None,
    content_guard_final_strategy: str | None = None,
    content_guard_retry_count: int | None = Query(default=None, ge=0),
    content_guard_guard_stage: str | None = None,
    content_guard_category: str | None = None,
    content_guard_switched_provider: bool | None = None,
    content_guard_adaptation_skipped: bool | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    exclude_health_checks: bool = Query(default=True),
    wait_for_latest: bool = Query(default=False),
    wait_timeout_ms: int = Query(default=2000, ge=0, le=10000),
    limit: int = Query(default=5000, ge=1, le=5000),
    db: Session = Depends(get_db),
) -> Response:
    if wait_for_latest:
        await RequestLogQueueService.wait_until_idle(
            timeout_seconds=wait_timeout_ms / 1000,
            poll_interval_seconds=0.05,
        )
    csv_text = LogService.export_logs_csv(
        db,
        log_type=log_type,
        log_types=None,
        provider_id=provider_id,
        provider_trust_level=provider_trust_level,
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
        content_guard_result=content_guard_result,
        content_guard_risk_level=content_guard_risk_level,
        content_guard_action=content_guard_action,
        content_guard_final_strategy=content_guard_final_strategy,
        content_guard_retry_count=content_guard_retry_count,
        content_guard_guard_stage=content_guard_guard_stage,
        content_guard_category=content_guard_category,
        content_guard_switched_provider=content_guard_switched_provider,
        content_guard_adaptation_skipped=content_guard_adaptation_skipped,
        start_at=start_at,
        end_at=end_at,
        limit=limit,
    )
    filename = f"logs-export-{now_beijing().strftime('%Y%m%d-%H%M%S')}.csv"
    _record_log_admin_audit(
        db,
        request=request,
        action="export_logs",
        summary=f"导出请求日志 {limit} 条以内",
        detail={"limit": limit, "log_type": log_type, "success": success},
        risk_level="medium",
    )
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


def _record_log_admin_audit(
    db: Session,
    *,
    request: Request,
    action: str,
    summary: str,
    detail: dict,
    risk_level: str,
) -> None:
    current_user = UserAuthService.get_current_user(request, db)
    AdminAuditService.create_log(
        db,
        actor_user_id=getattr(current_user, "id", None),
        actor_username=getattr(current_user, "username", None),
        action=action,
        entity_type="logs",
        entity_id=None,
        entity_name="日志中心",
        summary=summary,
        detail=detail,
        request_trace_id=getattr(request.state, "trace_id", None),
        source_ip=request.client.host if request.client else None,
        risk_level=risk_level,
    )


def _has_log_delete_scope(filters: dict) -> bool:
    for value in filters.values():
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return True
    return False
