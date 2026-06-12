from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import Response
from sqlalchemy import case, desc, func, literal, or_, select, union_all
from sqlalchemy.orm import Session

from app.database import get_db
from app.logging.queue import LoggingQueue
from app.models.admin_audit_log import AdminAuditLog
from app.models.alert_event import AlertEvent
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
from app.models.provider import Provider
from app.models.request_log import RequestLog
from app.schemas.log import RequestLogOut
from app.services.admin_audit_service import AdminAuditService
from app.services.cache_service import CacheService
from app.services.log_service import LogService
from app.services.setting_service import SettingService
from app.services.redis_service import RedisService
from app.services.user_auth_service import UserAuthService
from app.utils.json_utils import dumps_json, to_jsonable


async def wait_for_typed_logging_queue(
    wait_for_latest: bool = Query(default=False),
    wait_timeout_ms: int = Query(default=2000, ge=0, le=10000),
) -> None:
    if not wait_for_latest:
        return
    try:
        await LoggingQueue.wait_until_idle(
            timeout_seconds=wait_timeout_ms / 1000,
            poll_interval_seconds=0.05,
        )
        CacheService.invalidate("typed-logging:queue-status")
    except Exception:
        return


router = APIRouter(
    prefix="/api/logging",
    tags=["logging"],
    dependencies=[Depends(wait_for_typed_logging_queue)],
)

BACKGROUND_JOB_STALE_AFTER = timedelta(hours=2)
REQUEST_TIMELINE_PER_MODEL_LIMIT = 50
REQUEST_TIMELINE_TOTAL_LIMIT = 200
HEALTH_PROBE_DETAIL_LIMIT = 500
TYPED_LOG_FILTER_VALUE_LIMIT = 50

BILLING_RESULT_STATUS_ALIASES: dict[str, tuple[str, ...]] = {
    "success": ("billed", "no_charge", "internal_request"),
    "filled": ("billed", "no_charge"),
    "failed": ("failed", "price_unresolved", "price_unset"),
    "error": ("failed", "price_unresolved", "price_unset"),
    "pending": ("pending_tokens", "retry"),
    "pending_tokens": ("pending_tokens",),
    "retry": ("retry",),
}

TYPED_LOG_TIME_FILTERS: dict[str, dict[str, str]] = {
    "exceptions": {"field": "occurred_at", "label": "发生时间", "start_label": "发生开始", "end_label": "发生结束"},
    "health-runs": {"field": "started_at", "label": "检查开始时间", "start_label": "检查开始", "end_label": "检查结束"},
    "billing-events": {"field": "created_at", "label": "计费事件时间", "start_label": "事件开始", "end_label": "事件结束"},
    "content-guard-events": {"field": "created_at", "label": "检测事件时间", "start_label": "检测开始", "end_label": "检测结束"},
    "background-jobs": {"field": "started_at", "label": "任务开始时间", "start_label": "任务开始", "end_label": "任务结束"},
    "asset-events": {"field": "created_at", "label": "素材事件时间", "start_label": "事件开始", "end_label": "事件结束"},
    "admin-audits": {"field": "created_at", "label": "审计时间", "start_label": "审计开始", "end_label": "审计结束"},
    "user-operations": {"field": "created_at", "label": "操作时间", "start_label": "操作开始", "end_label": "操作结束"},
    "alert-events": {"field": "last_seen_at", "label": "最近出现时间", "start_label": "出现开始", "end_label": "出现结束"},
}

TYPED_LOG_FILTER_ALIASES: dict[str, dict[str, tuple[str, ...]]] = {
    "severity": {
        "严重": ("critical",),
        "危险": ("danger",),
        "警告": ("warning",),
        "信息": ("info",),
    },
    "overall_result": {
        "成功": ("healthy",),
        "健康": ("healthy",),
        "异常": ("unhealthy", "failed"),
        "失败": ("failed", "unhealthy"),
        "跳过": ("skipped",),
        "运行": ("running",),
    },
    "result": {
        "成功": ("success", "filled"),
        "失败": ("failed",),
        "异常": ("failed", "error"),
        "等待": ("pending", "pending_tokens"),
        "重试": ("retry",),
    },
    "billing_status": {
        "已计费": ("billed",),
        "不扣费": ("no_charge",),
        "失败": ("failed",),
        "重试": ("retry",),
    },
    "status": {
        "成功": ("success",),
        "失败": ("failed",),
        "跳过": ("skipped", "skipped_locked", "skipped_lock_unavailable"),
        "运行": ("running", "stale_running"),
    },
    "lock_status": {
        "已获取": ("acquired",),
        "不可用": ("unavailable", "unavailable_skipped", "unavailable_fallback"),
        "锁定": ("skipped_locked",),
    },
    "guard_result": {
        "通过": ("pass",),
        "复核": ("review",),
        "拦截": ("block",),
        "异常": ("error",),
    },
    "risk_level": {
        "低": ("low",),
        "中": ("medium",),
        "高": ("high",),
    },
}

TYPED_LOG_FUZZY_FILTER_FIELDS = {
    "action",
    "actor_type",
    "asset_event_type",
    "billing_status",
    "event_family",
    "guard_result",
    "guard_stage",
    "lock_status",
    "overall_result",
    "result",
    "risk_level",
    "severity",
    "status",
    "storage_scope",
    "trigger_type",
}

TYPED_LOG_EXPORT_FIELDS: dict[str, list[str]] = {
    "exceptions": [
        "id", "event_id", "event_type", "event_name", "trace_id", "correlation_id", "module",
        "severity", "actor_type", "actor_id", "source_ip", "result", "exception_type",
        "status_code", "error_code", "message", "handler_name", "request_path", "method",
        "is_external_v1", "stack_hash", "stack_excerpt", "detail_json", "occurred_at",
    ],
    "health-runs": [
        "id", "run_id", "trigger_type", "scope_type", "scope_id",
        "health_probe_provider_names", "health_probe_model_ids", "overall_result",
        "total_probes", "success_probes", "failed_probes", "duration_ms", "started_at", "finished_at",
    ],
    "billing-events": [
        "event_family", "id", "request_log_id", "queue_source", "attempt_count",
        "usage_before_json", "usage_after_json", "token_source", "enable_usage_fill", "result",
        "pricing_source", "pricing_snapshot_json", "cost_snapshot_json", "billing_status",
        "balance_delta", "balance_after", "billing_record_id", "error", "created_at",
    ],
    "content-guard-events": [
        "id", "request_log_id", "trace_id", "guard_stage", "guard_result", "risk_level",
        "provider_id", "provider_name", "provider_model_id", "model_name", "requested_model",
        "request_path", "is_stream", "matched_categories_json", "matched_rules_json", "reason",
        "action", "excerpt", "provider_status_after", "confidence", "score_delta",
        "diagnostics_json", "created_at",
    ],
    "background-jobs": [
        "id", "job_run_id", "job_name", "trigger_type", "lock_key", "lock_status", "status",
        "started_at", "finished_at", "duration_ms", "processed_count", "success_count",
        "failed_count", "result_summary_json", "error", "created_at", "stale",
    ],
    "asset-events": [
        "id", "asset_id", "asset_event_type", "actor_type", "actor_id", "filename",
        "content_type", "file_size_bytes", "sha256_prefix", "sha256_hex", "storage_scope", "result",
        "error", "trace_id", "request_log_id", "created_at",
    ],
    "admin-audits": [
        "id", "actor_user_id", "actor_username", "action", "entity_type", "entity_id",
        "entity_name", "target_user_id", "request_trace_id", "source_ip", "summary",
        "risk_level", "detail_json", "created_at",
    ],
    "user-operations": [
        "id", "user_account_id", "username", "action", "entity_type", "entity_id",
        "entity_name", "summary", "detail_json", "source_ip", "trace_id", "result", "created_at",
    ],
    "alert-events": [
        "id", "alert_key", "alert_type", "severity", "title", "message", "payload_json",
        "status", "first_seen_at", "last_seen_at", "last_notified_at", "acknowledged_at",
        "resolved_at", "created_at", "updated_at",
    ],
}

TYPED_LOG_EXPORT_FIELD_LABELS: dict[str, str] = {
    "id": "ID",
    "event_id": "事件 ID",
    "event_type": "事件类型",
    "event_name": "事件名称",
    "trace_id": "追踪 ID",
    "correlation_id": "关联 ID",
    "module": "模块",
    "severity": "严重级别",
    "actor_type": "操作者类型",
    "actor_id": "操作者 ID",
    "source_ip": "来源 IP",
    "result": "结果",
    "status": "状态",
    "exception_type": "异常类型",
    "status_code": "状态码",
    "error_code": "错误码",
    "message": "消息",
    "request_path": "请求路径",
    "method": "方法",
    "request_log_id": "请求日志 ID",
    "provider_id": "提供商 ID",
    "provider_name": "提供商",
    "health_probe_provider_names": "检测提供商名称",
    "health_probe_model_ids": "检测模型 ID",
    "provider_model_id": "提供商模型 ID",
    "model_name": "实际模型",
    "requested_model": "请求模型",
    "request_path": "请求路径",
    "is_stream": "流式",
    "guard_stage": "防护阶段",
    "guard_result": "防护结果",
    "risk_level": "风险等级",
    "matched_categories_json": "命中分类",
    "matched_rules_json": "命中规则",
    "reason": "原因",
    "action": "动作",
    "excerpt": "摘录",
    "provider_status_after": "处置后提供商状态",
    "confidence": "可信度",
    "score_delta": "评分变化",
    "diagnostics_json": "诊断信息",
    "created_at": "创建时间",
    "started_at": "开始时间",
    "finished_at": "结束时间",
    "duration_ms": "耗时 ms",
    "job_name": "任务名称",
    "trigger_type": "触发方式",
    "lock_status": "锁状态",
    "filename": "文件名",
    "content_type": "内容类型",
    "file_size_bytes": "文件大小 B",
    "sha256_prefix": "SHA256 前缀",
    "sha256_hex": "SHA256",
    "storage_scope": "存储范围",
    "alert_key": "告警键",
    "alert_type": "告警类型",
    "title": "标题",
    "payload_json": "载荷",
    "first_seen_at": "首次出现时间",
    "last_seen_at": "最近出现时间",
    "acknowledged_at": "确认时间",
    "resolved_at": "解决时间",
    "updated_at": "更新时间",
    "actor_username": "管理员",
    "actor_user_id": "管理员 ID",
    "request_trace_id": "请求追踪 ID",
    "target_user_id": "目标用户 ID",
    "username": "用户名",
    "summary": "摘要",
    "detail_json": "详情",
}


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


def build_request_log_timeline_payload(
    db: Session,
    log: RequestLog,
    *,
    per_model_limit: int = REQUEST_TIMELINE_PER_MODEL_LIMIT,
    total_limit: int = REQUEST_TIMELINE_TOTAL_LIMIT,
) -> dict[str, Any]:
    per_model_limit = max(1, min(int(per_model_limit or REQUEST_TIMELINE_PER_MODEL_LIMIT), 200))
    total_limit = max(1, min(int(total_limit or REQUEST_TIMELINE_TOTAL_LIMIT), 1000))
    events: list[dict[str, Any]] = []
    truncated = False
    per_model_counts: dict[str, dict[str, int | bool]] = {}
    for label, model in REQUEST_TIMELINE_MODELS:
        rows = db.scalars(
            select(model)
            .where(model.request_log_id == log.id)
            .order_by(model.created_at.asc(), model.id.asc())
            .limit(per_model_limit + 1)
        ).all()
        if len(rows) > per_model_limit:
            truncated = True
        per_model_counts[model.__tablename__] = {
            "loaded": min(len(rows), per_model_limit),
            "truncated": len(rows) > per_model_limit,
        }
        for row in rows[:per_model_limit]:
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
    if len(events) > total_limit:
        truncated = True
        events = events[:total_limit]
    return {
        "request_log": RequestLogOut.model_validate(
            LogService.serialize_log(
                log,
                include_payload_fields=False,
                derive_image_observability=False,
                raw_api_key_by_id=LogService.load_raw_api_keys_for_logs(db, [log]),
            )
        ).model_dump(mode="json"),
        "events": events,
        "truncated": truncated,
        "limits": {"per_model": per_model_limit, "total": total_limit},
        "event_counts": per_model_counts,
    }


@router.get("/request-logs")
async def list_request_logs(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    keyword: str | None = None,
    success: bool | None = None,
    status_code: int | None = None,
    trace_id: str | None = None,
    content_guard_result: str | None = None,
    content_guard_risk_level: str | None = None,
    content_guard_action: str | None = None,
    content_guard_final_strategy: str | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    query = select(RequestLog).options(*LogService._lightweight_log_load_options())
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
    content_guard_filters = {
        "content_guard_result": content_guard_result,
        "content_guard_risk_level": content_guard_risk_level,
        "content_guard_action": content_guard_action,
        "content_guard_final_strategy": content_guard_final_strategy,
    }
    for field_name, value in content_guard_filters.items():
        if value is None or value == "":
            continue
        conditions.append(getattr(RequestLog, field_name) == value)
    for condition in conditions:
        query = query.where(condition)
        count_query = count_query.where(condition)
    total = int(db.scalar(count_query) or 0)
    items = db.scalars(
        query.order_by(desc(RequestLog.created_at), desc(RequestLog.id))
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    raw_api_key_by_id = LogService.load_raw_api_keys_for_logs(db, items)
    return _page_response(
        page=page,
        page_size=page_size,
        total=total,
        items=[
            RequestLogOut.model_validate(item).model_dump(mode="json")
            for item in LogService.serialize_logs(
                items,
                include_payload_fields=False,
                derive_image_observability=False,
                raw_api_key_by_id=raw_api_key_by_id,
            )
        ],
    )


@router.get("/request-logs/{request_log_id}")
def get_request_log(request_log_id: int, db: Session = Depends(get_db)) -> dict[str, Any]:
    log = db.scalar(
        select(RequestLog)
        .options(*LogService._lightweight_log_load_options())
        .where(RequestLog.id == request_log_id)
    )
    if log is None:
        raise HTTPException(status_code=404, detail="request log not found")
    raw_api_key_by_id = LogService.load_raw_api_keys_for_logs(db, [log])
    return RequestLogOut.model_validate(
        LogService.serialize_logs([log], raw_api_key_by_id=raw_api_key_by_id)[0]
    ).model_dump(mode="json")


@router.get("/request-logs/{request_log_id}/timeline")
def get_request_log_timeline(
    request_log_id: int,
    per_model_limit: int = Query(default=REQUEST_TIMELINE_PER_MODEL_LIMIT, ge=1, le=200),
    total_limit: int = Query(default=REQUEST_TIMELINE_TOTAL_LIMIT, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    log = db.get(RequestLog, request_log_id)
    if log is None:
        raise HTTPException(status_code=404, detail="request log not found")
    return build_request_log_timeline_payload(
        db,
        log,
        per_model_limit=per_model_limit,
        total_limit=total_limit,
    )


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
    data = _list_model(
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
    return _enrich_typed_log_response(db, "exceptions", data)


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
    data = _list_model(
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
    _enrich_health_run_probe_context(db, data.get("items") or [])
    return _enrich_typed_log_response(db, "health-runs", data)


@router.get("/health-runs/{run_id}")
def get_health_run(
    run_id: str,
    probe_limit: int = Query(default=HEALTH_PROBE_DETAIL_LIMIT, ge=1, le=1000),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    run = db.scalar(select(HealthCheckRun).where(HealthCheckRun.run_id == run_id))
    if run is None:
        raise HTTPException(status_code=404, detail="health run not found")
    normalized_limit = max(1, min(int(probe_limit or HEALTH_PROBE_DETAIL_LIMIT), 1000))
    probes = db.scalars(
        select(HealthProbeEvent)
        .where(HealthProbeEvent.run_id == run_id)
        .order_by(HealthProbeEvent.created_at.asc(), HealthProbeEvent.id.asc())
        .limit(normalized_limit + 1)
    ).all()
    probe_items = [_model_to_dict(item) for item in probes[:normalized_limit]]
    return {
        "run": _model_to_dict(run),
        "probes": probe_items,
        "probe_summary": _health_probe_detail_summary_from_db(db, run_id),
        "probe_limit": normalized_limit,
        "probes_truncated": len(probes) > normalized_limit,
    }


def _health_probe_detail_summary_from_db(db: Session, run_id: str) -> dict[str, Any]:
    summary = db.execute(
        select(
            func.count(HealthProbeEvent.id).label("probe_total"),
            func.coalesce(func.sum(case((HealthProbeEvent.success.is_(True), 1), else_=0)), 0).label("probe_success"),
            func.count(HealthProbeEvent.provider_id.distinct()).label("provider_count"),
        ).where(HealthProbeEvent.run_id == run_id)
    ).one()
    model_ids = db.scalars(
        select(HealthProbeEvent.provider_model_id)
        .where(HealthProbeEvent.run_id == run_id, HealthProbeEvent.provider_model_id.is_not(None))
        .distinct()
    ).all()
    model_names_without_id = db.scalars(
        select(HealthProbeEvent.model_name)
        .where(
            HealthProbeEvent.run_id == run_id,
            HealthProbeEvent.provider_model_id.is_(None),
            HealthProbeEvent.model_name.is_not(None),
        )
        .distinct()
    ).all()
    provider_success_count = int(
        db.scalar(
            select(func.count(HealthProbeEvent.provider_id.distinct())).where(
                HealthProbeEvent.run_id == run_id,
                HealthProbeEvent.provider_id.is_not(None),
                HealthProbeEvent.success.is_(True),
            )
        )
        or 0
    )
    model_success_ids = db.scalars(
        select(HealthProbeEvent.provider_model_id)
        .where(
            HealthProbeEvent.run_id == run_id,
            HealthProbeEvent.provider_model_id.is_not(None),
            HealthProbeEvent.success.is_(True),
        )
        .distinct()
    ).all()
    model_success_names_without_id = db.scalars(
        select(HealthProbeEvent.model_name)
        .where(
            HealthProbeEvent.run_id == run_id,
            HealthProbeEvent.provider_model_id.is_(None),
            HealthProbeEvent.model_name.is_not(None),
            HealthProbeEvent.success.is_(True),
        )
        .distinct()
    ).all()
    probe_total = int(summary.probe_total or 0)
    probe_success = int(summary.probe_success or 0)
    provider_count = int(summary.provider_count or 0)
    model_count = len(model_ids) + len(model_names_without_id)
    model_success_count = len(model_success_ids) + len(model_success_names_without_id)
    return {
        "probe_total": probe_total,
        "probe_success": probe_success,
        "probe_failed": max(0, probe_total - probe_success),
        "provider_count": provider_count,
        "provider_success_count": provider_success_count,
        "provider_failed_count": max(0, provider_count - provider_success_count),
        "model_count": model_count,
        "model_success_count": model_success_count,
        "model_failed_count": max(0, model_count - model_success_count),
    }


def _ordered_unique(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _enrich_health_run_probe_context(db: Session, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    run_ids = _ordered_unique([item.get("run_id") for item in items if isinstance(item, dict)])
    if not run_ids:
        return items
    probes = db.scalars(
        select(HealthProbeEvent)
        .where(HealthProbeEvent.run_id.in_(run_ids))
        .order_by(HealthProbeEvent.created_at.asc(), HealthProbeEvent.id.asc())
    ).all()
    provider_ids = _ordered_unique([
        probe.provider_id
        for probe in probes
        if probe.provider_id is not None
    ])
    provider_name_by_id: dict[str, str] = {}
    if provider_ids:
        provider_rows = db.execute(
            select(Provider.id, Provider.name).where(Provider.id.in_([int(item) for item in provider_ids]))
        ).all()
        provider_name_by_id = {
            str(provider_id): provider_name
            for provider_id, provider_name in provider_rows
            if provider_name
        }
    context_by_run_id: dict[str, dict[str, list[str]]] = {
        run_id: {"provider_names": [], "model_ids": []}
        for run_id in run_ids
    }
    for probe in probes:
        run_id = str(probe.run_id or "")
        if run_id not in context_by_run_id:
            continue
        if probe.provider_id is not None:
            provider_key = str(probe.provider_id)
            context_by_run_id[run_id]["provider_names"].append(
                provider_name_by_id.get(provider_key) or f"提供商 {provider_key}"
            )
        if probe.provider_model_id is not None:
            context_by_run_id[run_id]["model_ids"].append(str(probe.provider_model_id))
        elif probe.model_name:
            context_by_run_id[run_id]["model_ids"].append(f"未记录 ID：{probe.model_name}")
    for item in items:
        if not isinstance(item, dict):
            continue
        run_id = str(item.get("run_id") or "")
        context = context_by_run_id.get(run_id) or {"provider_names": [], "model_ids": []}
        provider_names = _ordered_unique(context["provider_names"])
        model_ids = _ordered_unique(context["model_ids"])
        item["health_probe_provider_names"] = provider_names
        item["health_probe_model_ids"] = model_ids
        item["health_probe_provider_count"] = len(provider_names)
        item["health_probe_model_count"] = len(model_ids)
    return items


def _health_probe_detail_summary(probes: list[dict[str, Any]]) -> dict[str, Any]:
    provider_ids = {item.get("provider_id") for item in probes if item.get("provider_id") is not None}
    provider_success_ids = {
        item.get("provider_id")
        for item in probes
        if item.get("provider_id") is not None and bool(item.get("success"))
    }
    model_keys = {
        item.get("provider_model_id") or item.get("model_name")
        for item in probes
        if item.get("provider_model_id") is not None or item.get("model_name")
    }
    model_success_keys = {
        item.get("provider_model_id") or item.get("model_name")
        for item in probes
        if (item.get("provider_model_id") is not None or item.get("model_name")) and bool(item.get("success"))
    }
    return {
        "probe_total": len(probes),
        "probe_success": sum(1 for item in probes if bool(item.get("success"))),
        "probe_failed": sum(1 for item in probes if not bool(item.get("success"))),
        "provider_count": len(provider_ids),
        "provider_success_count": len(provider_success_ids),
        "provider_failed_count": max(0, len(provider_ids) - len(provider_success_ids)),
        "model_count": len(model_keys),
        "model_success_count": len(model_success_keys),
        "model_failed_count": max(0, len(model_keys) - len(model_success_keys)),
    }


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
    if billing_status and normalized_family in ("", "token_finalize"):
        include_token = False

    offset = (page - 1) * page_size
    token_conditions = []
    if include_token:
        token_result = result if normalized_family in ("", "token_finalize") else None
        token_conditions = _model_conditions(
            TokenFinalizeEvent,
            keyword=keyword,
            keyword_columns=("queue_source", "token_source", "result", "error"),
            filters={"request_log_id": request_log_id, "result": token_result},
            order_column=TokenFinalizeEvent.created_at,
            start_at=start_at,
            end_at=end_at,
        )

    billing_conditions = []
    if include_billing:
        billing_status_values: tuple[str, ...] | None = None
        if billing_status:
            billing_status_values = tuple(_expand_filter_values("billing_status", billing_status))
        elif result:
            mapped_values: list[str] = []
            for value in _expand_filter_values("result", result):
                mapped_values.extend(BILLING_RESULT_STATUS_ALIASES.get(value, ()))
            billing_status_values = tuple(dict.fromkeys(mapped_values)) if mapped_values else None
        billing_conditions = _model_conditions(
            BillingProcessEvent,
            keyword=keyword,
            keyword_columns=("pricing_source", "billing_status", "error"),
            filters={
                "request_log_id": request_log_id,
            },
            order_column=BillingProcessEvent.created_at,
            start_at=start_at,
            end_at=end_at,
        )
        if billing_status_values:
            billing_conditions.append(BillingProcessEvent.billing_status.in_(billing_status_values))

    total = 0
    event_selects = []
    token_total = 0
    billing_total = 0
    if include_token:
        token_total = int(
            db.scalar(select(func.count()).select_from(TokenFinalizeEvent).where(*token_conditions)) or 0
        )
        total += token_total
        event_selects.append(
            select(
                literal("token_finalize").label("event_family"),
                TokenFinalizeEvent.id.label("id"),
                TokenFinalizeEvent.request_log_id.label("request_log_id"),
                TokenFinalizeEvent.created_at.label("created_at"),
                TokenFinalizeEvent.error.label("error"),
                TokenFinalizeEvent.queue_source.label("queue_source"),
                TokenFinalizeEvent.attempt_count.label("attempt_count"),
                TokenFinalizeEvent.usage_before_json.label("usage_before_json"),
                TokenFinalizeEvent.usage_after_json.label("usage_after_json"),
                TokenFinalizeEvent.token_source.label("token_source"),
                TokenFinalizeEvent.enable_usage_fill.label("enable_usage_fill"),
                TokenFinalizeEvent.result.label("result"),
                literal(None).label("api_client_key_id"),
                literal(None).label("user_account_id"),
                literal(None).label("pricing_source"),
                literal(None).label("pricing_snapshot_json"),
                literal(None).label("cost_snapshot_json"),
                literal(None).label("balance_delta"),
                literal(None).label("balance_after"),
                literal(None).label("billing_status"),
                literal(None).label("billing_record_id"),
            ).where(*token_conditions)
        )
    if include_billing:
        billing_total = int(
            db.scalar(select(func.count()).select_from(BillingProcessEvent).where(*billing_conditions)) or 0
        )
        total += billing_total
        event_selects.append(
            select(
                literal("billing_process").label("event_family"),
                BillingProcessEvent.id.label("id"),
                BillingProcessEvent.request_log_id.label("request_log_id"),
                BillingProcessEvent.created_at.label("created_at"),
                BillingProcessEvent.error.label("error"),
                literal(None).label("queue_source"),
                literal(None).label("attempt_count"),
                literal(None).label("usage_before_json"),
                literal(None).label("usage_after_json"),
                literal(None).label("token_source"),
                literal(None).label("enable_usage_fill"),
                literal(None).label("result"),
                BillingProcessEvent.api_client_key_id.label("api_client_key_id"),
                BillingProcessEvent.user_account_id.label("user_account_id"),
                BillingProcessEvent.pricing_source.label("pricing_source"),
                BillingProcessEvent.pricing_snapshot_json.label("pricing_snapshot_json"),
                BillingProcessEvent.cost_snapshot_json.label("cost_snapshot_json"),
                BillingProcessEvent.balance_delta.label("balance_delta"),
                BillingProcessEvent.balance_after.label("balance_after"),
                BillingProcessEvent.billing_status.label("billing_status"),
                BillingProcessEvent.billing_record_id.label("billing_record_id"),
            ).where(*billing_conditions)
        )
    if not event_selects:
        return _enrich_typed_log_response(
            db,
            "billing-events",
            _page_response(page=page, page_size=page_size, total=0, items=[]),
            summary=_billing_events_summary(
                db,
                token_conditions if include_token else [],
                billing_conditions if include_billing else [],
                include_token=include_token,
                include_billing=include_billing,
                token_total=token_total,
                billing_total=billing_total,
            ),
        )
    merged = union_all(*event_selects).subquery()
    rows = db.execute(
        select(merged)
        .order_by(desc(merged.c.created_at), desc(merged.c.event_family), desc(merged.c.id))
        .offset(offset)
        .limit(page_size)
    ).mappings()
    data = _page_response(page=page, page_size=page_size, total=total, items=[to_jsonable(dict(row)) for row in rows])
    return _enrich_typed_log_response(
        db,
        "billing-events",
        data,
        summary=_billing_events_summary(
            db,
            token_conditions if include_token else [],
            billing_conditions if include_billing else [],
            include_token=include_token,
            include_billing=include_billing,
            token_total=token_total,
            billing_total=billing_total,
        ),
    )


@router.get("/content-guard-events")
def list_content_guard_events(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    keyword: str | None = None,
    guard_stage: str | None = None,
    guard_result: str | None = None,
    risk_level: str | None = None,
    action: str | None = None,
    matched_rule: str | None = None,
    provider_id: int | None = None,
    model_name: str | None = None,
    request_path: str | None = None,
    is_stream: bool | None = None,
    request_log_id: int | None = None,
    trace_id: str | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    query = select(RequestContentGuardEvent, RequestLog).outerjoin(
        RequestLog,
        RequestLog.id == RequestContentGuardEvent.request_log_id,
    )
    count_query = select(func.count()).select_from(RequestContentGuardEvent).outerjoin(
        RequestLog,
        RequestLog.id == RequestContentGuardEvent.request_log_id,
    )
    conditions = []
    if keyword:
        conditions.append(
            or_(
                _contains(RequestContentGuardEvent.trace_id, keyword),
                _contains(RequestContentGuardEvent.guard_stage, keyword),
                _contains(RequestContentGuardEvent.guard_result, keyword),
                _contains(RequestContentGuardEvent.risk_level, keyword),
                _contains(RequestContentGuardEvent.provider_name, keyword),
                _contains(RequestContentGuardEvent.model_name, keyword),
                _contains(RequestContentGuardEvent.requested_model, keyword),
                _contains(RequestContentGuardEvent.request_path, keyword),
                _contains(RequestContentGuardEvent.reason, keyword),
                _contains(RequestContentGuardEvent.action, keyword),
                _contains(RequestContentGuardEvent.excerpt, keyword),
                _contains(RequestLog.trace_id, keyword),
                _contains(RequestLog.provider_name, keyword),
                _contains(RequestLog.model_name, keyword),
                _contains(RequestLog.requested_model, keyword),
                _contains(RequestLog.request_path, keyword),
            )
        )
    if matched_rule:
        conditions.append(
            or_(
                _contains(RequestContentGuardEvent.matched_rules_json, matched_rule),
                _contains(RequestContentGuardEvent.matched_categories_json, matched_rule),
                _contains(RequestContentGuardEvent.reason, matched_rule),
                _contains(RequestContentGuardEvent.excerpt, matched_rule),
            )
        )
    if model_name:
        conditions.append(
            or_(
                _contains(RequestContentGuardEvent.model_name, model_name),
                _contains(RequestContentGuardEvent.requested_model, model_name),
                _contains(RequestLog.model_name, model_name),
                _contains(RequestLog.requested_model, model_name),
            )
        )
    direct_filters = {
        "guard_stage": guard_stage,
        "guard_result": guard_result,
        "risk_level": risk_level,
        "action": action,
        "request_log_id": request_log_id,
    }
    for field_name, value in direct_filters.items():
        if value is None or value == "":
            continue
        conditions.append(getattr(RequestContentGuardEvent, field_name) == value)
    if provider_id is not None and provider_id != "":
        conditions.append(or_(RequestContentGuardEvent.provider_id == provider_id, RequestLog.provider_id == provider_id))
    if request_path:
        conditions.append(or_(RequestContentGuardEvent.request_path == request_path, RequestLog.request_path == request_path))
    if is_stream is not None and is_stream != "":
        conditions.append(or_(RequestContentGuardEvent.is_stream == is_stream, RequestLog.is_stream == is_stream))
    if trace_id:
        conditions.append(or_(RequestContentGuardEvent.trace_id == trace_id, RequestLog.trace_id == trace_id))
    if start_at is not None:
        conditions.append(RequestContentGuardEvent.created_at >= start_at)
    if end_at is not None:
        conditions.append(RequestContentGuardEvent.created_at <= end_at)
    for condition in conditions:
        query = query.where(condition)
        count_query = count_query.where(condition)
    total = int(db.scalar(count_query) or 0)
    rows = list(db.execute(
        query.order_by(desc(RequestContentGuardEvent.created_at), desc(RequestContentGuardEvent.id))
        .offset((page - 1) * page_size)
        .limit(page_size)
    ))
    data = _page_response(
        page=page,
        page_size=page_size,
        total=total,
        items=[
            _content_guard_event_to_dict(event, request_log)
            for event, request_log in rows
        ],
    )
    return _enrich_typed_log_response(db, "content-guard-events", data)


@router.get("/export")
def export_typed_logs(
    request: Request,
    typed_log_type: str | None = Query(default=None),
    log_type: str | None = Query(default=None),
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
    matched_rule: str | None = None,
    provider_id: int | None = None,
    model_name: str | None = None,
    is_stream: bool | None = None,
    actor_type: str | None = None,
    user_account_id: int | None = None,
    entity_type: str | None = None,
    alert_type: str | None = None,
    storage_scope: str | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    limit: int = Query(default=5000, ge=1, le=5000),
    db: Session = Depends(get_db),
) -> Response:
    resolved_log_type = typed_log_type or log_type
    if not resolved_log_type:
        raise HTTPException(status_code=400, detail="typed_log_type is required")
    log_type = resolved_log_type
    if resolved_log_type == "exceptions":
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
    elif resolved_log_type == "health-runs":
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
        _enrich_health_run_probe_context(db, data.get("items") or [])
    elif resolved_log_type == "billing-events":
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
    elif resolved_log_type == "content-guard-events":
        data = list_content_guard_events(
            page=1,
            page_size=limit,
            keyword=keyword,
            guard_stage=guard_stage,
            guard_result=guard_result,
            risk_level=risk_level,
            action=action,
            matched_rule=matched_rule,
            provider_id=provider_id,
            model_name=model_name,
            request_path=request_path or path,
            is_stream=is_stream,
            request_log_id=request_log_id,
            trace_id=trace_id,
            start_at=start_at,
            end_at=end_at,
            db=db,
        )
    elif resolved_log_type == "background-jobs":
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
    elif resolved_log_type == "asset-events":
        data = _list_model(
            db,
            AssetEvent,
            page=1,
            page_size=limit,
            keyword=keyword,
            keyword_columns=("filename", "content_type", "sha256_prefix", "sha256_hex", "trace_id", "error"),
            filters={"actor_type": actor_type, "storage_scope": storage_scope, "result": result, "trace_id": trace_id, "request_log_id": request_log_id},
            order_column=AssetEvent.created_at,
            start_at=start_at,
            end_at=end_at,
        )
    elif resolved_log_type == "admin-audits":
        data = _list_model(
            db,
            AdminAuditLog,
            page=1,
            page_size=limit,
            keyword=keyword,
            keyword_columns=("actor_username", "action", "entity_type", "entity_name", "summary", "request_trace_id"),
            filters={"action": action, "entity_type": entity_type, "risk_level": risk_level},
            order_column=AdminAuditLog.created_at,
            start_at=start_at,
            end_at=end_at,
        )
    elif resolved_log_type == "user-operations":
        data = _list_model(
            db,
            UserOperationAuditLog,
            page=1,
            page_size=limit,
            keyword=keyword,
            keyword_columns=("username", "action", "entity_type", "entity_name", "summary", "trace_id"),
            filters={"user_account_id": user_account_id, "action": action, "result": result},
            order_column=UserOperationAuditLog.created_at,
            start_at=start_at,
            end_at=end_at,
        )
    elif resolved_log_type == "alert-events":
        data = _list_model(
            db,
            AlertEvent,
            page=1,
            page_size=limit,
            keyword=keyword,
            keyword_columns=("alert_key", "alert_type", "title", "message", "payload_json"),
            filters={"alert_type": alert_type, "severity": severity, "status": status},
            order_column=AlertEvent.last_seen_at,
            start_at=start_at,
            end_at=end_at,
        )
    else:
        raise HTTPException(status_code=400, detail="unsupported typed log export")
    csv_text = _items_to_csv(data.get("items") or [], fieldnames=TYPED_LOG_EXPORT_FIELDS.get(log_type))
    filename = f"{resolved_log_type}-export-{now_beijing().strftime('%Y%m%d-%H%M%S')}.csv"
    _record_logging_admin_audit(
        db,
        request=request,
        action="export_typed_logs",
        summary=f"导出类型化日志：{resolved_log_type}",
        detail={
            "typed_log_type": resolved_log_type,
            "limit": limit,
            "keyword": keyword,
            "trace_id": trace_id,
            "request_log_id": request_log_id,
            "start_at": start_at.isoformat() if start_at else None,
            "end_at": end_at.isoformat() if end_at else None,
        },
        risk_level="medium",
    )
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/typed-events/backfill")
def backfill_typed_events(
    request: Request,
    limit: int = Query(default=500, ge=1, le=5000),
    scan_limit: int = Query(default=2500, ge=1, le=50000),
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    result = LogService.backfill_typed_events_from_request_logs(db, limit=limit, scan_limit=scan_limit)
    _record_logging_admin_audit(
        db,
        request=request,
        action="backfill_typed_events",
        summary=f"回填类型化日志 {result.get('created_events') or 0} 条",
        detail={"limit": limit, "scan_limit": scan_limit, "result": result},
        risk_level="high",
    )
    return {
        "success": True,
        "message": "类型化日志历史回填已执行",
        "result": result,
    }


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
    data = _list_background_jobs(
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
    return _enrich_typed_log_response(db, "background-jobs", data)


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
    data = _list_background_jobs_db_only(
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
    stale_before = now_beijing() - BACKGROUND_JOB_STALE_AFTER
    redis_candidates = [
        item
        for item in _redis_background_job_state_items(stale_before=stale_before)
        if _background_job_item_matches(
            item,
            keyword=keyword,
            status=status,
            job_name=job_name,
            trigger_type=trigger_type,
            lock_status=lock_status,
            start_at=start_at,
            end_at=end_at,
        )
    ]
    latest_status_by_job = _latest_background_job_statuses(
        db,
        [str(item.get("job_name") or "") for item in redis_candidates if item.get("job_name")],
    )
    redis_items = [
        item
        for item in redis_candidates
        if latest_status_by_job.get(str(item.get("job_name") or "")) not in {"running", "stale_running"}
    ]
    items = list(data.get("items") or [])
    if page == 1 and redis_items:
        items.extend(redis_items)
        items.sort(
            key=lambda item: (
                _datetime_sort_key(item.get("created_at") or item.get("started_at")),
                item.get("id") or 0,
            ),
            reverse=True,
        )
        items = items[:page_size]
    data["items"] = items
    data["total"] = int(data.get("total") or 0) + len(redis_items)
    data["redis_overlay_count"] = len(redis_items)
    return data


def _latest_background_job_statuses(db: Session, job_names: list[str]) -> dict[str, str]:
    unique_names = [item for item in dict.fromkeys(job_names) if item]
    if not unique_names:
        return {}
    latest_ids = (
        select(func.max(BackgroundJobEvent.id).label("id"))
        .where(BackgroundJobEvent.job_name.in_(unique_names))
        .group_by(BackgroundJobEvent.job_name)
        .subquery()
    )
    rows = db.execute(
        select(
            BackgroundJobEvent.job_name,
            BackgroundJobEvent.status,
            BackgroundJobEvent.started_at,
        ).where(BackgroundJobEvent.id.in_(select(latest_ids.c.id)))
    ).all()
    stale_before = now_beijing() - BACKGROUND_JOB_STALE_AFTER
    statuses: dict[str, str] = {}
    for row in rows:
        status = str(row.status or "")
        if status == "running" and row.started_at is not None and row.started_at <= stale_before:
            status = "stale_running"
        statuses[str(row.job_name or "")] = status
    return statuses


def _list_background_jobs_db_only(
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
                _contains(BackgroundJobEvent.result_summary_json, keyword),
            )
        )
    if status:
        if status == "skipped":
            conditions.append(BackgroundJobEvent.status.in_(("skipped", "skipped_locked", "skipped_lock_unavailable")))
        elif status == "stale_running":
            stale_before = now_beijing() - BACKGROUND_JOB_STALE_AFTER
            conditions.append(BackgroundJobEvent.status == "running")
            conditions.append(BackgroundJobEvent.started_at.is_not(None))
            conditions.append(BackgroundJobEvent.started_at <= stale_before)
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
    stale_before = now_beijing() - BACKGROUND_JOB_STALE_AFTER
    items = []
    for item in rows:
        payload = _model_to_dict(item)
        is_stale = (
            item.status == "running"
            and item.started_at is not None
            and item.started_at <= stale_before
        )
        if is_stale:
            payload["stale"] = True
            payload["status"] = "stale_running"
            payload["error"] = payload.get("error") or f"任务运行超过 {int(BACKGROUND_JOB_STALE_AFTER.total_seconds() // 60)} 分钟，可能已失去心跳或进程已退出"
        else:
            payload["stale"] = False
        items.append(payload)
    return _page_response(page=page, page_size=page_size, total=total, items=items)


def _background_job_payload(item: BackgroundJobEvent, *, stale_before: datetime) -> dict[str, Any]:
    payload = {
        column.name: getattr(item, column.name)
        for column in item.__table__.columns
    }
    is_stale = (
        item.status == "running"
        and item.started_at is not None
        and item.started_at <= stale_before
    )
    if is_stale:
        payload["stale"] = True
        payload["status"] = "stale_running"
        payload["error"] = payload.get("error") or f"任务运行超过 {int(BACKGROUND_JOB_STALE_AFTER.total_seconds() // 60)} 分钟，可能已失去心跳或进程已退出"
    else:
        payload["stale"] = False
    payload.setdefault("source", "database")
    return payload


def _redis_background_job_state_items(*, stale_before: datetime) -> list[dict[str, Any]]:
    try:
        client = RedisService.get_sync_client()
    except Exception:
        return []
    items: list[dict[str, Any]] = []
    scanned = 0
    for key in client.scan_iter(match="scheduler:job:*:state", count=50):
        scanned += 1
        if scanned > 100:
            break
        key_text = key.decode("utf-8") if isinstance(key, bytes) else str(key)
        job_name = key_text.removeprefix("scheduler:job:").removesuffix(":state")
        try:
            raw_state = client.hgetall(key)
        except Exception:
            continue
        state = {
            (item_key.decode("utf-8") if isinstance(item_key, bytes) else str(item_key)): (
                item_value.decode("utf-8") if isinstance(item_value, bytes) else str(item_value)
            )
            for item_key, item_value in raw_state.items()
        }
        status_value = state.get("status") or "running"
        started_at = _parse_datetime_value(state.get("started_at") or state.get("updated_at"))
        updated_at = _parse_datetime_value(state.get("updated_at"))
        finished_at = _parse_datetime_value(state.get("finished_at"))
        is_stale = status_value == "running" and started_at is not None and started_at <= stale_before
        items.append({
            "id": None,
            "job_run_id": state.get("job_run_id") or f"redis:{job_name}",
            "job_name": job_name,
            "trigger_type": state.get("trigger_type") or "scheduler",
            "lock_key": f"scheduler:lock:{job_name}",
            "lock_status": state.get("lock_status") or ("acquired" if status_value == "running" else None),
            "status": "stale_running" if is_stale else status_value,
            "started_at": started_at,
            "finished_at": finished_at,
            "duration_ms": None,
            "processed_count": None,
            "success_count": None,
            "failed_count": None,
            "result_summary_json": None,
            "error": state.get("error") or (f"任务运行超过 {int(BACKGROUND_JOB_STALE_AFTER.total_seconds() // 60)} 分钟，可能已失去心跳或进程已退出" if is_stale else None),
            "created_at": started_at or updated_at,
            "stale": is_stale,
            "source": "redis_state",
        })
    return items


def _background_job_item_matches(
    item: dict[str, Any],
    *,
    keyword: str | None,
    status: str | None,
    job_name: str | None,
    trigger_type: str | None,
    lock_status: str | None,
    start_at: datetime | None,
    end_at: datetime | None,
) -> bool:
    if keyword:
        keyword_lower = keyword.lower()
        searchable = " ".join(str(item.get(key) or "") for key in ("job_run_id", "job_name", "lock_key", "error", "result_summary_json")).lower()
        if keyword_lower not in searchable:
            return False
    item_status = str(item.get("status") or "")
    if status:
        if status == "skipped":
            if item_status not in {"skipped", "skipped_locked", "skipped_lock_unavailable"}:
                return False
        elif item_status != status:
            return False
    if job_name and job_name.lower() not in str(item.get("job_name") or "").lower():
        return False
    if trigger_type and item.get("trigger_type") != trigger_type:
        return False
    if lock_status and item.get("lock_status") != lock_status:
        return False
    started_at = item.get("started_at")
    if start_at is not None and isinstance(started_at, datetime) and started_at < start_at:
        return False
    if end_at is not None and isinstance(started_at, datetime) and started_at > end_at:
        return False
    return True


def _parse_datetime_value(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="ignore")
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _datetime_sort_key(value: Any) -> float:
    parsed = _parse_datetime_value(value)
    return parsed.timestamp() if parsed is not None else 0.0


@router.get("/admin-audits")
def list_admin_audits(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    keyword: str | None = None,
    action: str | None = None,
    entity_type: str | None = None,
    risk_level: str | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    data = _list_model(
        db,
        AdminAuditLog,
        page=page,
        page_size=page_size,
        keyword=keyword,
        keyword_columns=("actor_username", "action", "entity_type", "entity_name", "summary", "request_trace_id"),
        filters={"action": action, "entity_type": entity_type, "risk_level": risk_level},
        order_column=AdminAuditLog.created_at,
        start_at=start_at,
        end_at=end_at,
    )
    return _enrich_typed_log_response(db, "admin-audits", data)


@router.get("/user-operations")
def list_user_operations(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    keyword: str | None = None,
    user_account_id: int | None = None,
    action: str | None = None,
    result: str | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    data = _list_model(
        db,
        UserOperationAuditLog,
        page=page,
        page_size=page_size,
        keyword=keyword,
        keyword_columns=("username", "action", "entity_type", "entity_name", "summary", "trace_id"),
        filters={"user_account_id": user_account_id, "action": action, "result": result},
        order_column=UserOperationAuditLog.created_at,
        start_at=start_at,
        end_at=end_at,
    )
    return _enrich_typed_log_response(db, "user-operations", data)


@router.get("/alert-events")
def list_alert_events(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    keyword: str | None = None,
    alert_type: str | None = None,
    severity: str | None = None,
    status: str | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    db: Session = Depends(get_db),
) -> dict[str, Any]:
    data = _list_model(
        db,
        AlertEvent,
        page=page,
        page_size=page_size,
        keyword=keyword,
        keyword_columns=("alert_key", "alert_type", "title", "message", "payload_json"),
        filters={"alert_type": alert_type, "severity": severity, "status": status},
        order_column=AlertEvent.last_seen_at,
        start_at=start_at,
        end_at=end_at,
    )
    return _enrich_typed_log_response(db, "alert-events", data)


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
    data = _list_model(
        db,
        AssetEvent,
        page=page,
        page_size=page_size,
        keyword=keyword,
        keyword_columns=("filename", "content_type", "sha256_prefix", "sha256_hex", "trace_id", "error"),
        filters={"actor_type": actor_type, "storage_scope": storage_scope, "result": result, "trace_id": trace_id, "request_log_id": request_log_id},
        order_column=AssetEvent.created_at,
        start_at=start_at,
        end_at=end_at,
    )
    return _enrich_typed_log_response(db, "asset-events", data)


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
    conditions = _model_conditions(
        model,
        keyword=keyword,
        keyword_columns=keyword_columns,
        filters=filters,
        order_column=order_column,
        start_at=start_at,
        end_at=end_at,
    )
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


def _model_conditions(
    model: type,
    *,
    keyword: str | None,
    keyword_columns: tuple[str, ...],
    filters: dict[str, Any],
    order_column,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> list[Any]:
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
        condition = _filter_condition(getattr(model, field_name), field_name, value)
        if condition is not None:
            conditions.append(condition)
    if start_at is not None:
        conditions.append(order_column >= start_at)
    if end_at is not None:
        conditions.append(order_column <= end_at)
    return conditions


def _filter_condition(column, field_name: str, value: Any):
    values = _expand_filter_values(field_name, value)
    if not values:
        return None
    if len(values) > 1:
        return column.in_(values)
    single = values[0]
    if isinstance(single, str) and field_name in TYPED_LOG_FUZZY_FILTER_FIELDS:
        wildcard = single.replace("*", "%")
        if "%" in wildcard or "_" in wildcard:
            return column.ilike(wildcard, escape="\\")
    return column == single


def _expand_filter_values(field_name: str, value: Any) -> list[Any]:
    if isinstance(value, int):
        return [value]
    raw = str(value or "").strip()
    if not raw:
        return []
    parts = [item.strip() for item in raw.replace("；", ",").replace("，", ",").replace("|", ",").split(",")]
    aliases = TYPED_LOG_FILTER_ALIASES.get(field_name, {})
    values: list[Any] = []

    def append_value(item: Any) -> bool:
        if item in values:
            return True
        if len(values) >= TYPED_LOG_FILTER_VALUE_LIMIT:
            return False
        values.append(item)
        return True

    for part in parts:
        if not part:
            continue
        mapped = aliases.get(part) or aliases.get(part.lower())
        if mapped:
            for item in mapped:
                if not append_value(item):
                    return values
            continue
        if field_name.endswith("_id") or field_name == "request_log_id":
            try:
                if not append_value(int(part)):
                    return values
                continue
            except ValueError:
                continue
        if not append_value(part):
            return values
    return values


def _enrich_typed_log_response(
    db: Session,
    log_type: str,
    data: dict[str, Any],
    *,
    summary: dict[str, Any] | None = None,
) -> dict[str, Any]:
    enriched = dict(data)
    enriched["time_filter"] = TYPED_LOG_TIME_FILTERS.get(log_type, {"field": "created_at", "label": "创建时间"})
    enriched["queue_status"] = _typed_logging_queue_status()
    enriched["retention"] = _typed_log_retention(db, log_type)
    enriched["summary"] = summary if summary is not None else _summary_from_items(log_type, enriched.get("items") or [], enriched.get("total") or 0)
    return enriched


def _typed_logging_queue_status() -> dict[str, Any]:
    cache_key = "typed-logging:queue-status"
    cached = CacheService.get(cache_key)
    if isinstance(cached, dict):
        return cached
    try:
        client = RedisService.get_sync_client()
        queued = int(client.llen(LoggingQueue.QUEUE_KEY) or 0)
        processing = int(client.llen(LoggingQueue.PROCESSING_KEY) or 0)
        dead_letter = int(client.llen(LoggingQueue.DEAD_LETTER_KEY) or 0)
        failure_count = int(client.get(LoggingQueue.FAILURE_COUNT_KEY) or 0)
        return CacheService.set(cache_key, {
            "available": True,
            "queued": queued,
            "processing": processing,
            "dead_letter": dead_letter,
            "failure_count": failure_count,
            "total": queued + processing,
        }, ttl_seconds=2)
    except Exception as exc:
        return CacheService.set(cache_key, {
            "available": False,
            "queued": None,
            "processing": None,
            "total": None,
            "error": str(exc)[:200],
        }, ttl_seconds=2)


def _typed_log_retention(db: Session, log_type: str) -> dict[str, Any]:
    cache_key = f"typed-logging:retention:{log_type}"
    cached = CacheService.get(cache_key)
    if isinstance(cached, dict):
        return cached
    setting = SettingService.get_or_create(db)
    field_by_type = {
        "exceptions": "exception_log_retention_days",
        "health-runs": "health_log_retention_days",
        "billing-events": "billing_log_retention_days",
        "content-guard-events": "request_child_log_retention_days",
        "background-jobs": "background_job_log_retention_days",
        "admin-audits": "admin_audit_log_retention_days",
        "user-operations": "user_operation_log_retention_days",
        "asset-events": "asset_log_retention_days",
        "alert-events": "alert_event_retention_days",
    }
    field = field_by_type.get(log_type, "request_child_log_retention_days")
    days = int(getattr(setting, field, 0) or 0)
    return CacheService.set(cache_key, {
        "field": field,
        "days": days,
        "label": "不自动清理" if days <= 0 else f"保留 {days} d",
        "settings_url": "/settings",
    }, ttl_seconds=60)


def _summary_from_items(log_type: str, items: list[dict[str, Any]], total: int) -> dict[str, Any]:
    summary: dict[str, Any] = {"total": int(total or 0), "page_count": len(items)}
    if log_type == "exceptions":
        summary["danger_count"] = sum(1 for item in items if str(item.get("severity") or "") in {"critical", "danger"})
        summary["warning_count"] = sum(1 for item in items if str(item.get("severity") or "") == "warning")
        summary["unique_trace_count"] = len({item.get("trace_id") for item in items if item.get("trace_id")})
    elif log_type == "health-runs":
        summary["success_probes"] = sum(int(item.get("success_probes") or 0) for item in items)
        summary["failed_probes"] = sum(int(item.get("failed_probes") or 0) for item in items)
        summary["running_count"] = sum(1 for item in items if str(item.get("overall_result") or "") == "running")
    elif log_type == "content-guard-events":
        summary["block_count"] = sum(1 for item in items if str(item.get("guard_result") or "") == "block")
        summary["review_count"] = sum(1 for item in items if str(item.get("guard_result") or "") == "review")
        summary["high_risk_count"] = sum(1 for item in items if str(item.get("risk_level") or "") == "high")
    elif log_type == "background-jobs":
        summary["running_count"] = sum(1 for item in items if str(item.get("status") or "") in {"running", "stale_running"})
        summary["failed_count"] = sum(1 for item in items if str(item.get("status") or "") == "failed")
        summary["skipped_count"] = sum(1 for item in items if str(item.get("status") or "").startswith("skipped"))
    elif log_type == "asset-events":
        summary["failed_count"] = sum(1 for item in items if str(item.get("result") or "") == "failed")
        summary["total_bytes"] = sum(int(item.get("file_size_bytes") or 0) for item in items)
        summary["unique_hash_count"] = len({item.get("sha256_hex") or item.get("sha256_prefix") for item in items if item.get("sha256_hex") or item.get("sha256_prefix")})
    elif log_type == "admin-audits":
        summary["high_risk_count"] = sum(1 for item in items if str(item.get("risk_level") or "") == "high")
        summary["medium_risk_count"] = sum(1 for item in items if str(item.get("risk_level") or "") == "medium")
        summary["unique_actor_count"] = len({item.get("actor_username") or item.get("actor_user_id") for item in items if item.get("actor_username") or item.get("actor_user_id")})
    elif log_type == "user-operations":
        summary["failed_count"] = sum(1 for item in items if str(item.get("result") or "") == "failed")
        summary["unique_user_count"] = len({item.get("username") or item.get("user_account_id") for item in items if item.get("username") or item.get("user_account_id")})
    elif log_type == "alert-events":
        summary["active_count"] = sum(1 for item in items if str(item.get("status") or "") == "active")
        summary["resolved_count"] = sum(1 for item in items if str(item.get("status") or "") == "resolved")
        summary["danger_count"] = sum(1 for item in items if str(item.get("severity") or "") in {"critical", "danger"})
    return summary


def _billing_events_summary(
    db: Session,
    token_conditions: list[Any],
    billing_conditions: list[Any],
    *,
    include_token: bool,
    include_billing: bool,
    token_total: int | None = None,
    billing_total: int | None = None,
) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "total": 0,
        "token_finalize_count": 0,
        "billing_process_count": 0,
        "failed_count": 0,
        "pending_count": 0,
        "balance_delta": 0.0,
    }
    if include_token:
        token_row = db.execute(
            select(
                func.count(TokenFinalizeEvent.id).label("total"),
                func.coalesce(func.sum(case((TokenFinalizeEvent.result == "failed", 1), else_=0)), 0).label("failed"),
                func.coalesce(
                    func.sum(case((TokenFinalizeEvent.result.in_(("pending_tokens", "retry")), 1), else_=0)),
                    0,
                ).label("pending"),
            ).where(*token_conditions)
        ).one()
        summary["token_finalize_count"] = int(token_total if token_total is not None else token_row.total or 0)
        summary["failed_count"] += int(token_row.failed or 0)
        summary["pending_count"] += int(token_row.pending or 0)
    if include_billing:
        billing_row = db.execute(
            select(
                func.count(BillingProcessEvent.id).label("total"),
                func.coalesce(func.sum(case((BillingProcessEvent.billing_status == "failed", 1), else_=0)), 0).label("failed"),
                func.coalesce(
                    func.sum(case((BillingProcessEvent.billing_status.in_(("pending_tokens", "retry")), 1), else_=0)),
                    0,
                ).label("pending"),
                func.coalesce(func.sum(BillingProcessEvent.balance_delta), 0).label("balance_delta"),
            ).where(*billing_conditions)
        ).one()
        summary["billing_process_count"] = int(billing_total if billing_total is not None else billing_row.total or 0)
        summary["failed_count"] += int(billing_row.failed or 0)
        summary["pending_count"] += int(billing_row.pending or 0)
        summary["balance_delta"] = float(billing_row.balance_delta or 0)
    summary["total"] = int(summary["token_finalize_count"]) + int(summary["billing_process_count"])
    return summary


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


def _content_guard_event_to_dict(event: RequestContentGuardEvent, request_log: RequestLog | None) -> dict[str, Any]:
    payload = _model_to_dict(event)
    if request_log is None:
        return payload
    fallback_fields = {
        "trace_id": request_log.trace_id,
        "provider_id": request_log.provider_id,
        "provider_name": request_log.provider_name,
        "provider_model_id": request_log.resolved_provider_model_id,
        "model_name": request_log.model_name,
        "requested_model": request_log.requested_model,
        "request_path": request_log.request_path,
        "is_stream": request_log.is_stream,
    }
    for field_name, value in fallback_fields.items():
        if payload.get(field_name) in (None, "") and value is not None:
            payload[field_name] = value
    return to_jsonable(payload)


def _items_to_csv(items: list[dict[str, Any]], *, fieldnames: list[str] | None = None) -> str:
    buffer = io.StringIO()
    resolved_fieldnames: list[str] = list(fieldnames or [])
    if not resolved_fieldnames:
        for item in items:
            for key in item.keys():
                if key not in resolved_fieldnames:
                    resolved_fieldnames.append(key)
    header = [TYPED_LOG_EXPORT_FIELD_LABELS.get(key, key) for key in resolved_fieldnames]
    writer = csv.writer(buffer)
    if not items:
        writer.writerow(["导出状态", "说明"])
        writer.writerow(["无数据", "当前筛选条件没有匹配的类型化日志；请调整筛选条件、确认日志队列是否仍在落库，或稍后重试。"])
        if resolved_fieldnames:
            writer.writerow([])
            writer.writerow(header)
        return "\ufeff" + buffer.getvalue()
    writer.writerow(header)
    for item in items:
        writer.writerow([_format_csv_value(key, item.get(key)) for key in resolved_fieldnames])
    return "\ufeff" + buffer.getvalue()


def _format_csv_value(field_name: str, value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return LogService.format_bool_display(value)
    if isinstance(value, (dict, list)):
        return dumps_json(to_jsonable(value))
    if field_name in {
        "event_family",
        "result",
        "billing_status",
        "guard_stage",
        "guard_result",
        "risk_level",
        "action",
        "status",
        "severity",
        "overall_result",
        "trigger_type",
        "lock_status",
        "storage_scope",
        "actor_type",
        "asset_event_type",
        "alert_type",
    }:
        return LogService.format_csv_label(value)
    return str(value)


def _record_logging_admin_audit(
    db: Session,
    *,
    request: Request,
    action: str,
    summary: str,
    detail: dict[str, Any],
    risk_level: str,
) -> None:
    current_user = UserAuthService.get_current_user(request, db)
    AdminAuditService.create_log(
        db,
        actor_user_id=getattr(current_user, "id", None),
        actor_username=getattr(current_user, "username", None),
        action=action,
        entity_type="typed_logs",
        entity_id=None,
        entity_name="类型化日志中心",
        summary=summary,
        detail=detail,
        request_trace_id=getattr(request.state, "trace_id", None),
        source_ip=request.client.host if request.client else None,
        risk_level=risk_level,
    )

from app.utils.timezone import now_beijing
