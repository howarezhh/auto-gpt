import csv
from datetime import datetime, timedelta
from io import StringIO
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.logging_events import RequestContentGuardEvent
from app.models.request_log import RequestLog
from app.schemas.content_guard import ContentGuardRulesUpdate, ContentGuardRunRequest, ContentGuardSettingsUpdate, ContentGuardTextInspectRequest
from app.services.admin_audit_service import AdminAuditService
from app.services.content_guard_module_service import ContentGuardModuleService
from app.services.setting_service import SettingService
from app.services.user_auth_service import require_admin_api_user
from app.tasks import configure_scheduler


router = APIRouter(
    prefix="/api/content-guard",
    tags=["content-guard"],
    dependencies=[Depends(require_admin_api_user)],
)
DEFAULT_RUNTIME_EVENT_WINDOW_DAYS = 7


def _content_guard_settings_audit_detail(payload: ContentGuardSettingsUpdate) -> dict[str, Any]:
    data = payload.model_dump()
    allowlist = str(data.pop("content_guard_url_allowlist_json", "") or "")
    return {
        **data,
        "url_allowlist_redacted": True,
        "url_allowlist_length": len(allowlist),
    }


def _content_guard_rules_audit_detail(before_rules: dict[str, dict[str, Any]], after_rules: dict[str, dict[str, Any]]) -> dict[str, Any]:
    before_ids = set(before_rules)
    after_ids = set(after_rules)
    common_ids = before_ids & after_ids
    diff_fields = ("name", "category", "enabled", "match_type", "risk_level", "action", "score_delta", "confidence", "reason")
    changed_rule_diffs: list[dict[str, Any]] = []
    changed_ids = [
        rule_id
        for rule_id in sorted(common_ids)
        if {
            key: before_rules[rule_id].get(key)
            for key in diff_fields
        }
        != {
            key: after_rules[rule_id].get(key)
            for key in diff_fields
        }
        or len(before_rules[rule_id].get("patterns") or []) != len(after_rules[rule_id].get("patterns") or [])
    ]
    for rule_id in changed_ids[:50]:
        before = before_rules[rule_id]
        after = after_rules[rule_id]
        fields: dict[str, dict[str, Any]] = {}
        for key in diff_fields:
            before_value = before.get(key)
            after_value = after.get(key)
            if before_value != after_value:
                fields[key] = {"before": before_value, "after": after_value}
        before_patterns = [str(item) for item in (before.get("patterns") or [])]
        after_patterns = [str(item) for item in (after.get("patterns") or [])]
        if before_patterns != after_patterns:
            fields["patterns"] = {
                "before_count": len(before_patterns),
                "after_count": len(after_patterns),
                "before_sample": before_patterns[:10],
                "after_sample": after_patterns[:10],
            }
        changed_rule_diffs.append({"rule_id": rule_id, "fields": fields})
    return {
        "rule_count_before": len(before_rules),
        "rule_count_after": len(after_rules),
        "added_rule_ids": sorted(after_ids - before_ids),
        "removed_rule_ids": sorted(before_ids - after_ids),
        "changed_rule_ids": changed_ids,
        "changed_rule_diffs": changed_rule_diffs,
    }


@router.get("/overview")
def content_guard_overview(
    provider_keyword: str = Query(default="", max_length=120),
    provider_page: int = Query(default=1, ge=1),
    provider_page_size: int = Query(default=50, ge=1, le=100),
    db: Session = Depends(get_db),
) -> dict:
    return ContentGuardModuleService.build_overview(
        db,
        provider_keyword=provider_keyword,
        provider_page=provider_page,
        provider_page_size=provider_page_size,
    )


@router.put("/settings")
def update_content_guard_settings(
    payload: ContentGuardSettingsUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> dict:
    audit_detail = _content_guard_settings_audit_detail(payload)
    setting = ContentGuardModuleService.update_settings(db, payload)
    configure_scheduler()
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="update",
        entity_type="setting",
        entity_id=setting.id,
        entity_name="content_guard",
        summary="更新内容完整性防护设置",
        detail=audit_detail,
    )
    return {"settings": ContentGuardModuleService.serialize_settings(setting)}


@router.put("/rules")
def update_content_guard_rules(
    payload: ContentGuardRulesUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> dict:
    before_rules = {item.get("id"): item for item in ContentGuardModuleService.serialize_rules(SettingService.get_or_create(db))}
    after_rules = {item.id: item.model_dump() for item in payload.rules}
    setting = ContentGuardModuleService.update_rules(db, payload)
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="update",
        entity_type="setting",
        entity_id=setting.id,
        entity_name="content_guard_rules",
        summary="更新内容完整性防护规则",
        detail=_content_guard_rules_audit_detail(before_rules, after_rules),
    )
    return {"rules": ContentGuardModuleService.serialize_rules(setting)}


@router.get("/rules")
def list_content_guard_rules(
    keyword: str = Query(default="", max_length=120),
    category: str = Query(default="", max_length=64),
    enabled: bool | None = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=50),
    db: Session = Depends(get_db),
) -> dict:
    return ContentGuardModuleService.list_rules(
        db,
        keyword=keyword,
        category=category,
        enabled=enabled,
        page=page,
        page_size=page_size,
    )


@router.post("/rules/reset")
def reset_content_guard_rules(
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> dict:
    setting = ContentGuardModuleService.reset_rules(db)
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="update",
        entity_type="setting",
        entity_id=setting.id,
        entity_name="content_guard_rules",
        summary="恢复内容完整性防护默认规则",
        detail={"reset_to_default": True},
    )
    return {"rules": ContentGuardModuleService.serialize_rules(setting)}


@router.post("/inspect-text")
def inspect_content_guard_text(
    payload: ContentGuardTextInspectRequest,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> dict:
    result = ContentGuardModuleService.inspect_text(db, payload)
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="inspect",
        entity_type="content_guard",
        entity_id="inspect_text",
        entity_name="inspect_text",
        summary="执行内容防护本地文本检测",
        detail={
            "text_length": len(payload.text or ""),
            "endpoint_path": payload.endpoint_path,
            "max_scan_bytes": payload.max_scan_bytes,
            "result": result.get("result", {}).get("content_guard_result"),
            "risk_level": result.get("risk_level"),
        },
    )
    return result


@router.post("/runtime/inspect-text")
def inspect_content_guard_text_runtime(
    payload: ContentGuardTextInspectRequest,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> dict:
    result = ContentGuardModuleService.inspect_text(db, payload)
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="inspect",
        entity_type="content_guard",
        entity_id="runtime_inspect_text",
        entity_name="runtime_inspect_text",
        summary="执行内容防护运行时文本检测",
        detail={
            "text_length": len(payload.text or ""),
            "endpoint_path": payload.endpoint_path,
            "max_scan_bytes": payload.max_scan_bytes,
            "result": result.get("result", {}).get("content_guard_result"),
            "risk_level": result.get("risk_level"),
        },
    )
    return result


def _apply_runtime_event_filters(
    query: Select[Any],
    *,
    keyword: str | None,
    guard_stage: str | None,
    guard_result: str | None,
    risk_level: str | None,
    action: str | None,
    provider_id: int | None,
    model_name: str | None,
    trace_id: str | None,
    request_log_id: int | None,
    start_at: datetime | None,
    end_at: datetime | None,
) -> Select[Any]:
    query = query.outerjoin(RequestLog, RequestLog.id == RequestContentGuardEvent.request_log_id)
    if keyword:
        like = f"%{keyword.strip()}%"
        query = query.where(
            or_(
                RequestContentGuardEvent.trace_id.ilike(like),
                RequestContentGuardEvent.guard_stage.ilike(like),
                RequestContentGuardEvent.reason.ilike(like),
                RequestContentGuardEvent.action.ilike(like),
                RequestContentGuardEvent.excerpt.ilike(like),
                RequestContentGuardEvent.provider_name.ilike(like),
                RequestContentGuardEvent.model_name.ilike(like),
                RequestContentGuardEvent.requested_model.ilike(like),
                RequestLog.provider_name.ilike(like),
                RequestLog.model_name.ilike(like),
                RequestLog.requested_model.ilike(like),
            )
        )
    if provider_id is not None:
        query = query.where(or_(RequestContentGuardEvent.provider_id == provider_id, RequestLog.provider_id == provider_id))
    if model_name:
        like = f"%{model_name.strip()}%"
        query = query.where(
            or_(
                RequestContentGuardEvent.model_name.ilike(like),
                RequestContentGuardEvent.requested_model.ilike(like),
                RequestLog.model_name.ilike(like),
                RequestLog.requested_model.ilike(like),
            )
        )
    filters = {
        RequestContentGuardEvent.guard_stage: guard_stage,
        RequestContentGuardEvent.guard_result: guard_result,
        RequestContentGuardEvent.risk_level: risk_level,
        RequestContentGuardEvent.action: action,
        RequestContentGuardEvent.trace_id: trace_id,
        RequestContentGuardEvent.request_log_id: request_log_id,
    }
    for column, value in filters.items():
        if value in (None, ""):
            continue
        query = query.where(column == value)
    if start_at is not None:
        query = query.where(RequestContentGuardEvent.created_at >= start_at)
    if end_at is not None:
        query = query.where(RequestContentGuardEvent.created_at <= end_at)
    return query


def _runtime_event_export_query() -> Select[Any]:
    return select(
        RequestContentGuardEvent.id,
        RequestContentGuardEvent.created_at,
        RequestContentGuardEvent.trace_id,
        RequestContentGuardEvent.request_log_id,
        func.coalesce(RequestContentGuardEvent.provider_id, RequestLog.provider_id).label("provider_id"),
        func.coalesce(RequestContentGuardEvent.provider_name, RequestLog.provider_name).label("provider_name"),
        func.coalesce(RequestContentGuardEvent.model_name, RequestLog.model_name).label("model_name"),
        func.coalesce(RequestContentGuardEvent.request_path, RequestLog.request_path).label("request_path"),
        RequestContentGuardEvent.guard_stage,
        RequestContentGuardEvent.guard_result,
        RequestContentGuardEvent.risk_level,
        RequestContentGuardEvent.matched_categories_json,
        RequestContentGuardEvent.matched_rules_json,
        RequestContentGuardEvent.reason,
        RequestContentGuardEvent.action,
        RequestContentGuardEvent.provider_status_after,
        RequestContentGuardEvent.confidence,
        RequestContentGuardEvent.score_delta,
        RequestContentGuardEvent.excerpt,
    )


def _default_runtime_event_start_at(
    *,
    keyword: str | None,
    guard_stage: str | None,
    guard_result: str | None,
    risk_level: str | None,
    action: str | None,
    provider_id: int | None,
    model_name: str | None,
    trace_id: str | None,
    request_log_id: int | None,
    start_at: datetime | None,
    end_at: datetime | None,
) -> datetime | None:
    if start_at is not None or end_at is not None:
        return start_at
    has_filter = any(
        (
            keyword,
            guard_stage,
            guard_result,
            risk_level,
            action,
            provider_id is not None,
            model_name,
            trace_id,
            request_log_id is not None,
        )
    )
    if has_filter:
        return start_at
    return datetime.utcnow() - timedelta(days=DEFAULT_RUNTIME_EVENT_WINDOW_DAYS)


@router.get("/runtime/events")
def content_guard_runtime_events(
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=200),
    keyword: str | None = None,
    guard_stage: str | None = None,
    guard_result: str | None = None,
    risk_level: str | None = None,
    action: str | None = None,
    provider_id: int | None = None,
    model_name: str | None = None,
    trace_id: str | None = None,
    request_log_id: int | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    db: Session = Depends(get_db),
) -> dict:
    start_at = _default_runtime_event_start_at(
        keyword=keyword,
        guard_stage=guard_stage,
        guard_result=guard_result,
        risk_level=risk_level,
        action=action,
        provider_id=provider_id,
        model_name=model_name,
        trace_id=trace_id,
        request_log_id=request_log_id,
        start_at=start_at,
        end_at=end_at,
    )
    base_query = _apply_runtime_event_filters(
        select(RequestContentGuardEvent, RequestLog),
        provider_id=provider_id,
        model_name=model_name,
        keyword=keyword,
        guard_stage=guard_stage,
        guard_result=guard_result,
        risk_level=risk_level,
        action=action,
        trace_id=trace_id,
        request_log_id=request_log_id,
        start_at=start_at,
        end_at=end_at,
    )
    total_query = _apply_runtime_event_filters(
        select(func.count()).select_from(RequestContentGuardEvent),
        provider_id=provider_id,
        model_name=model_name,
        keyword=keyword,
        guard_stage=guard_stage,
        guard_result=guard_result,
        risk_level=risk_level,
        action=action,
        trace_id=trace_id,
        request_log_id=request_log_id,
        start_at=start_at,
        end_at=end_at,
    )
    total = int(db.execute(total_query).scalar() or 0)
    rows = list(
        db.execute(
            base_query
            .order_by(RequestContentGuardEvent.created_at.desc(), RequestContentGuardEvent.id.desc())
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    )
    return {
        "page": page,
        "page_size": page_size,
        "total": total,
        "total_pages": max(1, (total + page_size - 1) // page_size),
        "events": [
            {
                "id": event.id,
                "created_at": event.created_at,
                "trace_id": event.trace_id,
                "request_log_id": event.request_log_id,
                "guard_stage": event.guard_stage,
                "guard_result": event.guard_result,
                "risk_level": event.risk_level,
                "matched_categories_json": event.matched_categories_json,
                "categories_json": event.matched_categories_json,
                "reason": event.reason,
                "action": event.action,
                "matched_rules_json": event.matched_rules_json,
                "provider_status_after": event.provider_status_after,
                "confidence": event.confidence,
                "score_delta": event.score_delta,
                "excerpt": event.excerpt,
                "provider_id": event.provider_id if event.provider_id is not None else (request_log.provider_id if request_log else None),
                "provider_name": event.provider_name or (request_log.provider_name if request_log else None),
                "provider_model_id": event.provider_model_id,
                "model_name": event.model_name or (request_log.model_name if request_log else None),
                "requested_model": event.requested_model or (request_log.requested_model if request_log else None),
                "request_path": event.request_path or (request_log.request_path if request_log else None),
                "is_stream": event.is_stream,
                "content_guard_result": event.guard_result,
                "content_guard_risk_level": event.risk_level,
                "content_guard_reason": event.reason,
                "content_guard_action": event.action,
            }
            for event, request_log in rows
        ],
    }


@router.get("/runtime/events/export")
def export_content_guard_runtime_events(
    keyword: str | None = None,
    guard_stage: str | None = None,
    guard_result: str | None = None,
    risk_level: str | None = None,
    action: str | None = None,
    provider_id: int | None = None,
    model_name: str | None = None,
    trace_id: str | None = None,
    request_log_id: int | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    limit: int = Query(default=5000, ge=1, le=5000),
    db: Session = Depends(get_db),
) -> Response:
    start_at = _default_runtime_event_start_at(
        keyword=keyword,
        guard_stage=guard_stage,
        guard_result=guard_result,
        risk_level=risk_level,
        action=action,
        provider_id=provider_id,
        model_name=model_name,
        trace_id=trace_id,
        request_log_id=request_log_id,
        start_at=start_at,
        end_at=end_at,
    )
    query = _apply_runtime_event_filters(
        _runtime_event_export_query(),
        provider_id=provider_id,
        model_name=model_name,
        keyword=keyword,
        guard_stage=guard_stage,
        guard_result=guard_result,
        risk_level=risk_level,
        action=action,
        trace_id=trace_id,
        request_log_id=request_log_id,
        start_at=start_at,
        end_at=end_at,
    )
    rows = list(
        db.execute(
            query
            .order_by(RequestContentGuardEvent.created_at.desc(), RequestContentGuardEvent.id.desc())
            .limit(limit)
        )
    )
    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(
        [
            "id",
            "created_at",
            "trace_id",
            "request_log_id",
            "provider_id",
            "provider_name",
            "model_name",
            "request_path",
            "guard_stage",
            "guard_result",
            "risk_level",
            "matched_categories_json",
            "matched_rules_json",
            "reason",
            "action",
            "provider_status_after",
            "confidence",
            "score_delta",
            "excerpt",
        ]
    )
    for row in rows:
        writer.writerow(
            [
                row.id,
                row.created_at.isoformat() if row.created_at else "",
                row.trace_id or "",
                row.request_log_id or "",
                row.provider_id or "",
                row.provider_name or "",
                row.model_name or "",
                row.request_path or "",
                row.guard_stage or "",
                row.guard_result or "",
                row.risk_level or "",
                row.matched_categories_json or "",
                row.matched_rules_json or "",
                row.reason or "",
                row.action or "",
                row.provider_status_after or "",
                row.confidence if row.confidence is not None else "",
                row.score_delta if row.score_delta is not None else "",
                row.excerpt or "",
            ]
        )
    return Response(
        content="\ufeff" + output.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="content_guard_runtime_events.csv"'},
    )


@router.get("/runtime/settings")
def content_guard_runtime_settings(db: Session = Depends(get_db)) -> dict:
    return ContentGuardModuleService.build_runtime_settings(db)


@router.post("/providers/{provider_id}/isolate")
def isolate_content_guard_provider(
    provider_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> dict:
    try:
        provider = ContentGuardModuleService.set_provider_content_integrity_status(db, provider_id, status="blocked")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="update",
        entity_type="provider",
        entity_id=provider.id,
        entity_name=provider.name,
        summary=f"内容防护手动隔离提供商 {provider.name}",
        detail={"content_integrity_status": "blocked"},
    )
    return {"provider": ContentGuardModuleService.serialize_provider(provider)}


@router.post("/providers/{provider_id}/restore")
def restore_content_guard_provider(
    provider_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> dict:
    try:
        provider = ContentGuardModuleService.set_provider_content_integrity_status(db, provider_id, status="passed")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="update",
        entity_type="provider",
        entity_id=provider.id,
        entity_name=provider.name,
        summary=f"内容防护手动恢复提供商 {provider.name}",
        detail={"content_integrity_status": "passed"},
    )
    return {"provider": ContentGuardModuleService.serialize_provider(provider)}


@router.post("/probe")
async def run_content_guard_probe(
    payload: ContentGuardRunRequest,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> dict:
    try:
        result = await ContentGuardModuleService.run_probe(db, payload)
        AdminAuditService.create_log(
            db,
            actor_user_id=current_user.id,
            actor_username=current_user.username,
            action="probe",
            entity_type="content_guard",
            entity_id=payload.provider_model_id or payload.provider_id or payload.target_type,
            entity_name="manual_probe",
            summary="执行内容防护手动能力探针",
            detail={
                "target_type": payload.target_type,
                "provider_id": payload.provider_id,
                "provider_model_id": payload.provider_model_id,
                "probe_keys": payload.probe_keys,
                "persist_internal_result": payload.persist_internal_result,
                "status": result.get("summary", {}).get("status"),
                "content_guard_result": result.get("summary", {}).get("content_guard_result"),
            },
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/precheck/probe")
async def run_content_guard_precheck_probe(
    payload: ContentGuardRunRequest,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> dict:
    try:
        result = await ContentGuardModuleService.run_probe(db, payload)
        AdminAuditService.create_log(
            db,
            actor_user_id=current_user.id,
            actor_username=current_user.username,
            action="probe",
            entity_type="content_guard",
            entity_id=payload.provider_model_id or payload.provider_id or payload.target_type,
            entity_name="manual_precheck_capability_probe",
            summary="执行内容防护预检能力探针",
            detail={
                "target_type": payload.target_type,
                "provider_id": payload.provider_id,
                "provider_model_id": payload.provider_model_id,
                "probe_keys": payload.probe_keys,
                "persist_internal_result": payload.persist_internal_result,
                "status": result.get("summary", {}).get("status"),
                "content_guard_result": result.get("summary", {}).get("content_guard_result"),
            },
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/trust-probe")
async def run_content_guard_trust_probe(
    payload: ContentGuardRunRequest,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> dict:
    try:
        result = await ContentGuardModuleService.run_trust_probe(db, payload)
        AdminAuditService.create_log(
            db,
            actor_user_id=current_user.id,
            actor_username=current_user.username,
            action="probe",
            entity_type="content_guard",
            entity_id=payload.provider_model_id or payload.provider_id or payload.target_type,
            entity_name="trust_probe",
            summary="执行内容防护可信探针",
            detail={
                "target_type": payload.target_type,
                "provider_id": payload.provider_id,
                "provider_model_id": payload.provider_model_id,
                "probe_keys": payload.probe_keys,
                "persist_internal_result": payload.persist_internal_result,
                "status": result.get("summary", {}).get("status"),
                "content_guard_result": result.get("summary", {}).get("content_guard_result"),
            },
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/precheck/trust-probe")
async def run_content_guard_precheck_trust_probe(
    payload: ContentGuardRunRequest,
    db: Session = Depends(get_db),
    current_user=Depends(require_admin_api_user),
) -> dict:
    try:
        result = await ContentGuardModuleService.run_trust_probe(db, payload)
        AdminAuditService.create_log(
            db,
            actor_user_id=current_user.id,
            actor_username=current_user.username,
            action="probe",
            entity_type="content_guard",
            entity_id=payload.provider_model_id or payload.provider_id or payload.target_type,
            entity_name="precheck_trust_probe",
            summary="执行内容防护预检可信探针",
            detail={
                "target_type": payload.target_type,
                "provider_id": payload.provider_id,
                "provider_model_id": payload.provider_model_id,
                "probe_keys": payload.probe_keys,
                "persist_internal_result": payload.persist_internal_result,
                "status": result.get("summary", {}).get("status"),
                "content_guard_result": result.get("summary", {}).get("content_guard_result"),
            },
        )
        return result
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/precheck/status")
def content_guard_precheck_status(db: Session = Depends(get_db)) -> dict:
    return ContentGuardModuleService.build_precheck_status(db)
