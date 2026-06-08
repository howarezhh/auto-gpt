from __future__ import annotations

import csv
import io
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from sqlalchemy import desc, func, or_, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.admin_audit_log import AdminAuditLog
from app.models.logging_events import (
    AssetEvent,
    BackgroundJobEvent,
    BillingProcessEvent,
    ExceptionEvent,
    HealthCheckRun,
    HealthProbeEvent,
    RequestAuthEvent,
    RequestBillingEvent,
    RequestContentGuardEvent,
    RequestErrorResponseEvent,
    RequestModelPermissionEvent,
    RequestProviderAttemptEvent,
    RequestRouteDecisionEvent,
    RequestStreamEvent,
    RequestUpstreamResponseEvent,
    RequestValidationEvent,
    TokenFinalizeEvent,
    UserOperationAuditLog,
)
from app.models.request_log import RequestLog
from app.schemas.log import RequestLogOut
from app.services.log_service import LogService
from app.utils.json_utils import to_jsonable


router = APIRouter(prefix="/api/logging", tags=["logging"])


REQUEST_TIMELINE_MODELS: tuple[tuple[str, type], ...] = (
    ("鉴权", RequestAuthEvent),
    ("请求校验", RequestValidationEvent),
    ("模型能力", RequestModelPermissionEvent),
    ("路由决策", RequestRouteDecisionEvent),
    ("Provider 尝试", RequestProviderAttemptEvent),
    ("上游响应", RequestUpstreamResponseEvent),
    ("流式响应", RequestStreamEvent),
    ("错误响应", RequestErrorResponseEvent),
    ("内容防护", RequestContentGuardEvent),
    ("计费", RequestBillingEvent),
)


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _contains(column, value: str):
    return column.ilike(f"%{_escape_like(value.strip())}%", escape="\\")


def build_request_log_timeline_payload(db: Session, log: RequestLog) -> dict[str, Any]:
    events: list[dict[str, Any]] = []
    for label, model in REQUEST_TIMELINE_MODELS:
        rows = db.scalars(
            select(model)
            .where(model.request_log_id == log.id)
            .order_by(model.created_at.asc(), model.id.asc())
        ).all()
        for row in rows:
            payload = _model_to_dict(row)
            events.append(
                {
                    "id": f"{model.__tablename__}:{row.id}",
                    "event_type": model.__tablename__,
                    "label": label,
                    "event_name": model.__tablename__,
                    "created_at": payload.get("created_at"),
                    "payload": payload,
                }
            )
    events.sort(key=lambda item: str(item.get("created_at") or ""))
    return {
        "request_log": RequestLogOut.model_validate(LogService.serialize_logs([log])[0]).model_dump(mode="json"),
        "events": events,
    }


@router.get("/request-logs")
async def list_request_logs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    keyword: str | None = None,
    success: bool | None = None,
    status_code: int | None = None,
    trace_id: str | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    query = select(RequestLog)
    count_query = select(func.count()).select_from(RequestLog)
    conditions = []
    if keyword:
        conditions.append(
            or_(
                _contains(RequestLog.trace_id, keyword),
                _contains(RequestLog.request_path, keyword),
                _contains(RequestLog.requested_model, keyword),
                _contains(RequestLog.model_name, keyword),
                _contains(RequestLog.api_client_key_name, keyword),
                _contains(RequestLog.user_account_name, keyword),
                _contains(RequestLog.error_code, keyword),
                _contains(RequestLog.message, keyword),
            )
        )
    if success is not None:
        conditions.append(RequestLog.success.is_(success))
    if status_code is not None:
        conditions.append(RequestLog.status_code == status_code)
    if trace_id:
        conditions.append(RequestLog.trace_id == trace_id)
    for condition in conditions:
        query = query.where(condition)
        count_query = count_query.where(condition)
    total = int(db.scalar(count_query) or 0)
    items = db.scalars(
        query.order_by(desc(RequestLog.created_at), desc(RequestLog.id))
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return _page_response(
        page=page,
        page_size=page_size,
        total=total,
        items=[RequestLogOut.model_validate(item).model_dump(mode="json") for item in LogService.serialize_logs(items)],
    )


@router.get("/request-logs/{request_log_id}")
def get_request_log(request_log_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    log = db.get(RequestLog, request_log_id)
    if log is None:
        raise HTTPException(status_code=404, detail="request log not found")
    return RequestLogOut.model_validate(LogService.serialize_logs([log])[0]).model_dump(mode="json")


@router.get("/request-logs/{request_log_id}/timeline")
def get_request_log_timeline(request_log_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    log = db.get(RequestLog, request_log_id)
    if log is None:
        raise HTTPException(status_code=404, detail="request log not found")
    return build_request_log_timeline_payload(db, log)


@router.get("/exceptions")
def list_exception_events(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    keyword: str | None = None,
    severity: str | None = None,
    error_code: str | None = None,
    path: str | None = None,
    trace_id: str | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return _list_model(
        db,
        ExceptionEvent,
        page=page,
        page_size=page_size,
        keyword=keyword,
        keyword_columns=("trace_id", "exception_type", "message", "request_path", "error_code"),
        filters={"severity": severity, "error_code": error_code, "request_path": path, "trace_id": trace_id},
        order_column=ExceptionEvent.occurred_at,
        start_at=start_at,
        end_at=end_at,
    )


@router.get("/health-runs")
def list_health_runs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    keyword: str | None = None,
    trigger_type: str | None = None,
    overall_result: str | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return _list_model(
        db,
        HealthCheckRun,
        page=page,
        page_size=page_size,
        keyword=keyword,
        keyword_columns=("run_id", "scope_type", "scope_id"),
        filters={"trigger_type": trigger_type, "overall_result": overall_result},
        order_column=HealthCheckRun.started_at,
        start_at=start_at,
        end_at=end_at,
    )


@router.get("/health-runs/{run_id}")
def get_health_run(run_id: str, db: Session = Depends(get_db)) -> dict[str, Any]:
    run = db.scalar(select(HealthCheckRun).where(HealthCheckRun.run_id == run_id))
    if run is None:
        raise HTTPException(status_code=404, detail="health run not found")
    probes = db.scalars(
        select(HealthProbeEvent)
        .where(HealthProbeEvent.run_id == run_id)
        .order_by(HealthProbeEvent.created_at.asc(), HealthProbeEvent.id.asc())
    ).all()
    return {"run": _model_to_dict(run), "probes": [_model_to_dict(item) for item in probes]}


@router.get("/billing-events")
def list_billing_events(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    keyword: str | None = None,
    event_family: str | None = None,
    result: str | None = None,
    billing_status: str | None = None,
    request_log_id: int | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    normalized_family = (event_family or "").strip()
    include_token = normalized_family in ("", "token_finalize")
    include_billing = normalized_family in ("", "billing_process")

    merge_window_size = page * page_size
    token_page = {"total": 0, "items": []}
    if include_token:
        token_page = _list_model(
            db,
            TokenFinalizeEvent,
            page=1,
            page_size=merge_window_size,
            keyword=keyword,
            keyword_columns=("queue_source", "token_source", "result", "error"),
            filters={"request_log_id": request_log_id, "result": result},
            order_column=TokenFinalizeEvent.created_at,
            start_at=start_at,
            end_at=end_at,
        )

    billing_page = {"total": 0, "items": []}
    if include_billing:
        billing_page = _list_model(
            db,
            BillingProcessEvent,
            page=1,
            page_size=merge_window_size,
            keyword=keyword,
            keyword_columns=("pricing_source", "billing_status", "error"),
            filters={
                "billing_status": billing_status or result,
                "request_log_id": request_log_id,
            },
            order_column=BillingProcessEvent.created_at,
            start_at=start_at,
            end_at=end_at,
        )

    items = [
        {"event_family": "token_finalize", **item}
        for item in token_page["items"]
    ] + [
        {"event_family": "billing_process", **item}
        for item in billing_page["items"]
    ]
    items.sort(key=lambda item: str(item.get("created_at") or ""), reverse=True)
    total = int(token_page["total"] or 0) + int(billing_page["total"] or 0)
    return _page_response(page=page, page_size=page_size, total=total, items=items[(page - 1) * page_size : page * page_size])


@router.get("/content-guard-events")
def list_content_guard_events(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    keyword: str | None = None,
    guard_stage: str | None = None,
    guard_result: str | None = None,
    risk_level: str | None = None,
    action: str | None = None,
    request_log_id: int | None = None,
    trace_id: str | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return _list_model(
        db,
        RequestContentGuardEvent,
        page=page,
        page_size=page_size,
        keyword=keyword,
        keyword_columns=("trace_id", "guard_stage", "guard_result", "risk_level", "reason", "action", "excerpt"),
        filters={
            "guard_stage": guard_stage,
            "guard_result": guard_result,
            "risk_level": risk_level,
            "action": action,
            "request_log_id": request_log_id,
            "trace_id": trace_id,
        },
        order_column=RequestContentGuardEvent.created_at,
        start_at=start_at,
        end_at=end_at,
    )


@router.get("/export")
def export_typed_logs(
    log_type: str = Query(...),
    keyword: str | None = None,
    severity: str | None = None,
    error_code: str | None = None,
    path: str | None = None,
    trace_id: str | None = None,
    request_log_id: int | None = None,
    trigger_type: str | None = None,
    overall_result: str | None = None,
    event_family: str | None = None,
    result: str | None = None,
    billing_status: str | None = None,
    status: str | None = None,
    job_name: str | None = None,
    lock_status: str | None = None,
    guard_stage: str | None = None,
    guard_result: str | None = None,
    risk_level: str | None = None,
    action: str | None = None,
    actor_type: str | None = None,
    storage_scope: str | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    limit: int = Query(default=5000, ge=1, le=10000),
    db: Session = Depends(get_db),
) -> Response:
    if log_type == "exceptions":
        data = _list_model(
            db,
            ExceptionEvent,
            page=1,
            page_size=limit,
            keyword=keyword,
            keyword_columns=("trace_id", "exception_type", "message", "request_path", "error_code"),
            filters={"severity": severity, "error_code": error_code, "request_path": path, "trace_id": trace_id},
            order_column=ExceptionEvent.occurred_at,
            start_at=start_at,
            end_at=end_at,
        )
    elif log_type == "health-runs":
        data = _list_model(
            db,
            HealthCheckRun,
            page=1,
            page_size=limit,
            keyword=keyword,
            keyword_columns=("run_id", "scope_type", "scope_id"),
            filters={"trigger_type": trigger_type, "overall_result": overall_result},
            order_column=HealthCheckRun.started_at,
            start_at=start_at,
            end_at=end_at,
        )
    elif log_type == "billing-events":
        data = list_billing_events(
            page=1,
            page_size=limit,
            keyword=keyword,
            event_family=event_family,
            result=result,
            billing_status=billing_status,
            request_log_id=request_log_id,
            start_at=start_at,
            end_at=end_at,
            db=db,
        )
    elif log_type == "content-guard-events":
        data = list_content_guard_events(
            page=1,
            page_size=limit,
            keyword=keyword,
            guard_stage=guard_stage,
            guard_result=guard_result,
            risk_level=risk_level,
            action=action,
            request_log_id=request_log_id,
            trace_id=trace_id,
            start_at=start_at,
            end_at=end_at,
            db=db,
        )
    elif log_type == "background-jobs":
        data = _list_background_jobs(
            db,
            page=1,
            page_size=limit,
            keyword=keyword,
            status=status,
            job_name=job_name,
            trigger_type=trigger_type,
            lock_status=lock_status,
            start_at=start_at,
            end_at=end_at,
        )
    elif log_type == "asset-events":
        data = _list_model(
            db,
            AssetEvent,
            page=1,
            page_size=limit,
            keyword=keyword,
            keyword_columns=("filename", "content_type", "sha256_prefix", "trace_id", "error"),
            filters={"actor_type": actor_type, "storage_scope": storage_scope, "result": result, "request_log_id": request_log_id},
            order_column=AssetEvent.created_at,
            start_at=start_at,
            end_at=end_at,
        )
    else:
        raise HTTPException(status_code=400, detail="unsupported typed log export")
    csv_text = _items_to_csv(data.get("items") or [])
    filename = f"{log_type}-export-{datetime.utcnow().strftime('%Y%m%d-%H%M%S')}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/background-jobs")
def list_background_jobs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    keyword: str | None = None,
    status: str | None = None,
    job_name: str | None = None,
    trigger_type: str | None = None,
    lock_status: str | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return _list_background_jobs(
        db,
        page=page,
        page_size=page_size,
        keyword=keyword,
        status=status,
        job_name=job_name,
        trigger_type=trigger_type,
        lock_status=lock_status,
        start_at=start_at,
        end_at=end_at,
    )


def _list_background_jobs(
    db: Session,
    *,
    page: int,
    page_size: int,
    keyword: str | None,
    status: str | None,
    job_name: str | None,
    trigger_type: str | None = None,
    lock_status: str | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> dict[str, Any]:
    query = select(BackgroundJobEvent)
    count_query = select(func.count()).select_from(BackgroundJobEvent)
    conditions = []
    if keyword:
        conditions.append(
            or_(
                _contains(BackgroundJobEvent.job_run_id, keyword),
                _contains(BackgroundJobEvent.job_name, keyword),
                _contains(BackgroundJobEvent.lock_key, keyword),
                _contains(BackgroundJobEvent.error, keyword),
            )
        )
    if status:
        if status == "skipped":
            conditions.append(BackgroundJobEvent.status.in_(("skipped", "skipped_locked", "skipped_lock_unavailable")))
        else:
            conditions.append(BackgroundJobEvent.status == status)
    if job_name:
        conditions.append(_contains(BackgroundJobEvent.job_name, job_name))
    if trigger_type:
        conditions.append(BackgroundJobEvent.trigger_type == trigger_type)
    if lock_status:
        conditions.append(BackgroundJobEvent.lock_status == lock_status)
    if start_at is not None:
        conditions.append(BackgroundJobEvent.started_at >= start_at)
    if end_at is not None:
        conditions.append(BackgroundJobEvent.started_at <= end_at)
    for condition in conditions:
        query = query.where(condition)
        count_query = count_query.where(condition)
    total = int(db.scalar(count_query) or 0)
    rows = db.scalars(
        query.order_by(desc(BackgroundJobEvent.created_at), desc(BackgroundJobEvent.id))
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return _page_response(page=page, page_size=page_size, total=total, items=[_model_to_dict(item) for item in rows])


@router.get("/admin-audits")
def list_admin_audits(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    keyword: str | None = None,
    action: str | None = None,
    entity_type: str | None = None,
    risk_level: str | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return _list_model(
        db,
        AdminAuditLog,
        page=page,
        page_size=page_size,
        keyword=keyword,
        keyword_columns=("actor_username", "action", "entity_type", "entity_name", "summary", "request_trace_id"),
        filters={"action": action, "entity_type": entity_type, "risk_level": risk_level},
        order_column=AdminAuditLog.created_at,
    )


@router.get("/user-operations")
def list_user_operations(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    keyword: str | None = None,
    user_account_id: int | None = None,
    action: str | None = None,
    result: str | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return _list_model(
        db,
        UserOperationAuditLog,
        page=page,
        page_size=page_size,
        keyword=keyword,
        keyword_columns=("username", "action", "entity_type", "entity_name", "summary", "trace_id"),
        filters={"user_account_id": user_account_id, "action": action, "result": result},
        order_column=UserOperationAuditLog.created_at,
    )


@router.get("/asset-events")
def list_asset_events(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    keyword: str | None = None,
    actor_type: str | None = None,
    storage_scope: str | None = None,
    result: str | None = None,
    trace_id: str | None = None,
    request_log_id: int | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return _list_model(
        db,
        AssetEvent,
        page=page,
        page_size=page_size,
        keyword=keyword,
        keyword_columns=("filename", "content_type", "sha256_prefix", "trace_id", "error"),
        filters={"actor_type": actor_type, "storage_scope": storage_scope, "result": result, "trace_id": trace_id, "request_log_id": request_log_id},
        order_column=AssetEvent.created_at,
        start_at=start_at,
        end_at=end_at,
    )


def _list_model(
    db: Session,
    model: type,
    *,
    page: int,
    page_size: int,
    keyword: str | None,
    keyword_columns: tuple[str, ...],
    filters: dict[str, Any],
    order_column,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> dict[str, Any]:
    query = select(model)
    count_query = select(func.count()).select_from(model)
    conditions = []
    if keyword:
        keyword_conditions = [
            _contains(getattr(model, column), keyword)
            for column in keyword_columns
            if hasattr(model, column)
        ]
        if keyword_conditions:
            conditions.append(or_(*keyword_conditions))
    for field_name, value in filters.items():
        if value is None or value == "" or not hasattr(model, field_name):
            continue
        conditions.append(getattr(model, field_name) == value)
    if start_at is not None:
        conditions.append(order_column >= start_at)
    if end_at is not None:
        conditions.append(order_column <= end_at)
    for condition in conditions:
        query = query.where(condition)
        count_query = count_query.where(condition)
    total = int(db.scalar(count_query) or 0)
    rows = db.scalars(
        query.order_by(desc(order_column), desc(model.id))
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return _page_response(page=page, page_size=page_size, total=total, items=[_model_to_dict(item) for item in rows])


def _page_response(*, page: int, page_size: int, total: int, items: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "items": items,
    }


def _model_to_dict(item) -> dict[str, Any]:
    payload = {
        column.name: getattr(item, column.name)
        for column in item.__table__.columns
    }
    return to_jsonable(payload)


def _items_to_csv(items: list[dict[str, Any]]) -> str:
    buffer = io.StringIO()
    fieldnames: list[str] = []
    for item in items:
        for key in item.keys():
            if key not in fieldnames:
                fieldnames.append(key)
    writer = csv.writer(buffer)
    writer.writerow(fieldnames)
    for item in items:
        writer.writerow([_format_csv_value(item.get(key)) for key in fieldnames])
    return buffer.getvalue()


def _format_csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return str(to_jsonable(value))
    return str(value)
