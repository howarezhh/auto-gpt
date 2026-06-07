from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
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
        like = f"%{keyword.strip()}%"
        conditions.append(
            or_(
                RequestLog.trace_id.like(like),
                RequestLog.request_path.like(like),
                RequestLog.requested_model.like(like),
                RequestLog.model_name.like(like),
                RequestLog.api_client_key_name.like(like),
                RequestLog.user_account_name.like(like),
                RequestLog.error_code.like(like),
                RequestLog.message.like(like),
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
        query.order_by(desc(RequestLog.created_at))
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
    events: list[dict[str, Any]] = []
    for label, model in REQUEST_TIMELINE_MODELS:
        rows = db.scalars(
            select(model)
            .where(model.request_log_id == request_log_id)
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


@router.get("/exceptions")
def list_exception_events(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    keyword: str | None = None,
    severity: str | None = None,
    error_code: str | None = None,
    path: str | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return _list_model(
        db,
        ExceptionEvent,
        page=page,
        page_size=page_size,
        keyword=keyword,
        keyword_columns=("trace_id", "exception_type", "message", "request_path", "error_code"),
        filters={"severity": severity, "error_code": error_code, "request_path": path},
        order_column=ExceptionEvent.occurred_at,
    )


@router.get("/health-runs")
def list_health_runs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    keyword: str | None = None,
    trigger_type: str | None = None,
    overall_result: str | None = None,
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
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    normalized_family = (event_family or "").strip()
    include_token = normalized_family in ("", "token_finalize")
    include_billing = normalized_family in ("", "billing_process")

    token_page = {"total": 0, "items": []}
    if include_token:
        token_page = _list_model(
            db,
            TokenFinalizeEvent,
            page=1,
            page_size=page_size,
            keyword=keyword,
            keyword_columns=("queue_source", "token_source", "result", "error"),
            filters={"request_log_id": request_log_id, "result": result},
            order_column=TokenFinalizeEvent.created_at,
        )

    billing_page = {"total": 0, "items": []}
    if include_billing:
        billing_page = _list_model(
            db,
            BillingProcessEvent,
            page=1,
            page_size=page_size,
            keyword=keyword,
            keyword_columns=("pricing_source", "billing_status", "error"),
            filters={
                "billing_status": billing_status or result,
                "request_log_id": request_log_id,
            },
            order_column=BillingProcessEvent.created_at,
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


@router.get("/background-jobs")
def list_background_jobs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    keyword: str | None = None,
    status: str | None = None,
    job_name: str | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return _list_model(
        db,
        BackgroundJobEvent,
        page=page,
        page_size=page_size,
        keyword=keyword,
        keyword_columns=("job_run_id", "job_name", "lock_key", "error"),
        filters={"status": status, "job_name": job_name},
        order_column=BackgroundJobEvent.created_at,
    )


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
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    return _list_model(
        db,
        AssetEvent,
        page=page,
        page_size=page_size,
        keyword=keyword,
        keyword_columns=("filename", "content_type", "sha256_prefix", "trace_id", "error"),
        filters={"actor_type": actor_type, "storage_scope": storage_scope, "result": result},
        order_column=AssetEvent.created_at,
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
) -> dict[str, Any]:
    query = select(model)
    count_query = select(func.count()).select_from(model)
    conditions = []
    if keyword:
        like = f"%{keyword.strip()}%"
        keyword_conditions = [
            getattr(model, column).like(like)
            for column in keyword_columns
            if hasattr(model, column)
        ]
        if keyword_conditions:
            conditions.append(or_(*keyword_conditions))
    for field_name, value in filters.items():
        if value is None or value == "" or not hasattr(model, field_name):
            continue
        conditions.append(getattr(model, field_name) == value)
    for condition in conditions:
        query = query.where(condition)
        count_query = count_query.where(condition)
    total = int(db.scalar(count_query) or 0)
    rows = db.scalars(
        query.order_by(desc(order_column))
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
