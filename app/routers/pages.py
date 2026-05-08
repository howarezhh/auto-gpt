from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.database import get_db
from app.models.model_catalog import ModelCatalog
from app.models.provider_model import ProviderModel
from app.models.request_log import RequestLog
from app.services.admin_audit_service import AdminAuditService
from app.services.alert_service import AlertService
from app.services.api_key_admin_service import ApiKeyAdminService
from app.routers.dashboard import _dashboard_usage_overview
from app.services.log_service import LogService
from app.services.provider_service import ProviderService
from app.services.setting_service import SettingService
from app.services.user_auth_service import USER_ROLE_ADMIN, UserAuthService


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _build_dashboard_stat_cards(stats: dict) -> list[dict]:
    return [
        {"id": "provider_count", "label": "总中转站数", "value": stats["provider_count"]},
        {"id": "healthy_count", "label": "健康中转站", "value": stats["healthy_count"]},
        {"id": "unhealthy_count", "label": "异常中转站", "value": stats["unhealthy_count"]},
        {"id": "model_count", "label": "模型总数", "value": stats["model_count"]},
        {"id": "recent_requests", "label": "24h 请求量", "value": stats["recent_requests"]},
        {"id": "recent_tokens", "label": "24h Token 用量", "value": stats["recent_tokens"]},
        {"id": "total_requests", "label": "累计请求量", "value": stats["total_requests"]},
        {"id": "total_tokens", "label": "累计 Token", "value": stats["total_tokens"]},
        {"id": "total_cost", "label": "累计成本", "value": f'{stats["total_cost"]:.6f}'},
        {"id": "conversation_count", "label": "会话数", "value": stats["conversation_count"]},
        {"id": "api_key_total", "label": "API 密钥总数", "value": stats["api_key_total"]},
        {
            "id": "recent_failure_rate",
            "label": "24h 失败率",
            "value": f'{stats["recent_failure_rate"]}%',
            "tone": "alert",
        },
        {
            "id": "total_failures",
            "label": "累计失败数",
            "value": stats["total_failures"],
            "tone": "alert",
        },
    ]


def _build_provider_page_content(provider_dicts: list[dict]) -> dict:
    enabled_provider_count = sum(1 for item in provider_dicts if item.get("enabled"))
    model_configs = [model for item in provider_dicts for model in item.get("model_configs", [])]
    enabled_models = [item for item in model_configs if item.get("enabled")]
    stream_model_count = sum(1 for item in enabled_models if item.get("supports_stream"))
    vision_model_count = sum(1 for item in enabled_models if item.get("supports_vision"))
    image_generation_model_count = sum(1 for item in enabled_models if item.get("supports_image_generation"))
    priced_model_count = sum(
        1
        for item in enabled_models
        if (item.get("input_price_per_1k") or 0) > 0 or (item.get("output_price_per_1k") or 0) > 0
    )
    stability_scores = [item.get("stability_score") for item in provider_dicts if item.get("stability_score") is not None]
    average_stability = round(sum(stability_scores) / len(stability_scores), 1) if stability_scores else 0

    return {
        "summary": {
            "provider_count": len(provider_dicts),
            "enabled_provider_count": enabled_provider_count,
            "model_count": len(enabled_models),
            "stream_model_count": stream_model_count,
            "vision_model_count": vision_model_count,
            "image_generation_model_count": image_generation_model_count,
            "priced_model_count": priced_model_count,
            "avg_stability_score": average_stability,
        },
        "telemetry_cards": [
            {"id": "provider_count", "label": "中转站总数", "value": len(provider_dicts)},
            {"id": "enabled_provider_count", "label": "已启用中转站", "value": enabled_provider_count},
            {"id": "model_count", "label": "挂载模型数", "value": len(enabled_models)},
            {"id": "stream_model_count", "label": "支持 Stream", "value": stream_model_count},
            {"id": "vision_model_count", "label": "支持图像理解", "value": vision_model_count},
            {"id": "image_generation_model_count", "label": "支持图片生成", "value": image_generation_model_count},
            {"id": "priced_model_count", "label": "已同步价格", "value": priced_model_count},
            {"id": "avg_stability_score", "label": "平均稳定性", "value": average_stability},
        ],
    }


def _resolve_external_base_url(request: Request) -> str:
    configured = get_settings().normalized_external_base_url()
    if configured:
        return configured
    return str(request.base_url).rstrip("/")


def require_admin_html(request: Request, db: Session):
    user = UserAuthService.get_current_user(request, db)
    if user is None:
        return RedirectResponse(UserAuthService.build_login_redirect_path(request), status_code=303)
    if user.role != USER_ROLE_ADMIN:
        return RedirectResponse("/user", status_code=303)
    return user


@router.get("/", response_class=HTMLResponse)
def dashboard_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    current_user = require_admin_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    providers = ProviderService.list_providers(db)
    api_key_summary = ApiKeyAdminService.get_summary(db)
    usage_overview = _dashboard_usage_overview(db)
    usage_summary = usage_overview["summary"]
    recent_since = datetime.utcnow() - timedelta(hours=24)
    recent_requests = db.scalar(select(func.count()).select_from(RequestLog).where(RequestLog.created_at >= recent_since, LogService._route_traffic_expr())) or 0
    recent_failures = db.scalar(
        select(func.count()).select_from(RequestLog).where(RequestLog.created_at >= recent_since, LogService._route_traffic_expr(), RequestLog.success.is_(False))
    ) or 0
    stats = {
        "provider_count": len(providers),
        "healthy_count": len([item for item in providers if item.health_status == "healthy"]),
        "degraded_count": len([item for item in providers if item.health_status == "degraded"]),
        "unhealthy_count": len([item for item in providers if item.health_status == "unhealthy"]),
        "model_count": db.scalar(select(func.count()).select_from(ModelCatalog)) or 0,
        "healthy_model_count": db.scalar(select(func.count()).select_from(ProviderModel).where(ProviderModel.health_status == "healthy")) or 0,
        "degraded_model_count": db.scalar(select(func.count()).select_from(ProviderModel).where(ProviderModel.health_status == "degraded")) or 0,
        "unhealthy_model_count": db.scalar(select(func.count()).select_from(ProviderModel).where(ProviderModel.health_status == "unhealthy")) or 0,
        "recent_requests": recent_requests,
        "recent_tokens": db.scalar(select(func.sum(RequestLog.total_tokens)).where(RequestLog.created_at >= recent_since, LogService._route_traffic_expr())) or 0,
        "total_requests": usage_summary["total_requests"],
        "total_tokens": usage_summary["total_tokens"],
        "total_cost": usage_summary["total_cost"],
        "conversation_count": db.scalar(
            select(func.count(func.distinct(RequestLog.conversation_key))).where(RequestLog.conversation_key.is_not(None), LogService._route_traffic_expr())
        ) or 0,
        "recent_failure_rate": round((recent_failures / recent_requests) * 100, 2) if recent_requests else 0.0,
        "total_failures": usage_summary["failed_requests"],
        "usage_overview": usage_overview,
        "api_key_total": api_key_summary.total_keys,
        "api_key_enabled": api_key_summary.enabled_keys,
        "api_key_disabled": api_key_summary.disabled_keys,
        "api_key_total_requests": api_key_summary.total_requests,
        "api_key_total_prompt_tokens": api_key_summary.total_prompt_tokens,
        "api_key_total_completion_tokens": api_key_summary.total_completion_tokens,
        "api_key_total_tokens": api_key_summary.total_tokens,
    }
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "providers": providers,
            "settings": SettingService.get_or_create(db),
            "stats": stats,
            "dashboard_stat_cards": _build_dashboard_stat_cards(stats),
            "page_name": "dashboard",
            "portal_type": "admin",
            "current_user": current_user,
            "title": "概览",
        },
    )


@router.get("/providers", response_class=HTMLResponse)
def providers_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    current_user = require_admin_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    provider_page_content = _build_provider_page_content(ProviderService.list_provider_dicts(db))
    return templates.TemplateResponse(
        "providers.html",
        {
            "request": request,
            "providers": ProviderService.list_providers(db),
            "page_name": "providers",
            "portal_type": "admin",
            "current_user": current_user,
            "title": "中转站管理",
            "provider_telemetry_cards": provider_page_content["telemetry_cards"],
            "provider_summary": provider_page_content["summary"],
        },
    )


@router.get("/models", response_class=HTMLResponse)
def models_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    current_user = require_admin_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    return templates.TemplateResponse(
        "models.html",
        {
            "request": request,
            "page_name": "models",
            "title": "模型配置",
            "portal_type": "admin",
            "current_user": current_user,
        },
    )


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    current_user = require_admin_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    return templates.TemplateResponse("settings.html", {"request": request, "settings": SettingService.get_or_create(db), "page_name": "settings", "portal_type": "admin", "current_user": current_user})


@router.get("/playground", response_class=HTMLResponse)
def playground_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    current_user = require_admin_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    return templates.TemplateResponse("playground.html", {"request": request, "page_name": "playground", "portal_type": "admin", "current_user": current_user})


@router.get("/docs", response_class=HTMLResponse)
def docs_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    current_user = require_admin_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    external_base_url = _resolve_external_base_url(request)
    external_v1_base_url = f"{external_base_url}/v1"
    return templates.TemplateResponse(
        "docs.html",
        {
            "request": request,
            "page_name": "docs",
            "title": "使用文档",
            "portal_type": "admin",
            "current_user": current_user,
            "external_base_url": external_base_url,
            "external_v1_base_url": external_v1_base_url,
        },
    )


@router.get("/api-keys", response_class=HTMLResponse)
def api_keys_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    current_user = require_admin_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    return templates.TemplateResponse(
        "api_keys.html",
        {
            "request": request,
            "page_name": "api-keys",
            "title": "API 密钥管理",
            "portal_type": "admin",
            "current_user": current_user,
        },
    )


@router.get("/api-keys/{api_key_id}", response_class=HTMLResponse)
def api_key_detail_page(request: Request, api_key_id: int, db: Session = Depends(get_db)) -> HTMLResponse:
    current_user = require_admin_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    return templates.TemplateResponse(
        "api_key_detail.html",
        {
            "request": request,
            "page_name": "api-key-detail",
            "title": "API 密钥详情",
            "api_key_id": api_key_id,
            "portal_type": "admin",
            "current_user": current_user,
        },
    )


@router.get("/logs", response_class=HTMLResponse)
def logs_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    current_user = require_admin_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    logs = db.scalars(select(RequestLog).order_by(RequestLog.created_at.desc()).limit(100)).all()
    return templates.TemplateResponse("logs.html", {"request": request, "logs": logs, "page_name": "logs", "portal_type": "admin", "current_user": current_user})


@router.get("/conversations", response_class=HTMLResponse)
def conversations_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    current_user = require_admin_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    return templates.TemplateResponse("conversations.html", {"request": request, "page_name": "conversations", "portal_type": "admin", "current_user": current_user})


@router.get("/alerts", response_class=HTMLResponse)
def alerts_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    current_user = require_admin_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    payload = AlertService.build_dashboard_payload(db)
    subscription = AlertService.get_or_create_subscription(db, user=current_user)
    return templates.TemplateResponse(
        "alerts.html",
        {
            "request": request,
            "title": "告警中心",
            "page_name": "alerts",
            "portal_type": "admin",
            "current_user": current_user,
            "subscription": subscription,
            **payload,
        },
    )


@router.get("/api/alerts/feed")
def alerts_feed(request: Request, db: Session = Depends(get_db)):
    current_user = require_admin_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    payload = AlertService.build_dashboard_payload(db)
    subscription = AlertService.get_or_create_subscription(db, user=current_user)
    return JSONResponse(
        {
            **payload,
            "subscription": {
                "enabled": subscription.enabled,
                "delivery_channel": subscription.delivery_channel,
                "notify_provider_alerts": subscription.notify_provider_alerts,
                "notify_api_key_alerts": subscription.notify_api_key_alerts,
                "notify_account_alerts": subscription.notify_account_alerts,
                "notify_failure_rate_alerts": subscription.notify_failure_rate_alerts,
                "browser_notifications_enabled": subscription.browser_notifications_enabled,
                "poll_interval_seconds": subscription.poll_interval_seconds,
            },
        }
    )


@router.post("/api/alerts/subscription")
def update_alert_subscription(
    request: Request,
    enabled: str = Form(default="true"),
    notify_provider_alerts: str = Form(default="true"),
    notify_api_key_alerts: str = Form(default="true"),
    notify_account_alerts: str = Form(default="true"),
    notify_failure_rate_alerts: str = Form(default="true"),
    browser_notifications_enabled: str = Form(default="false"),
    poll_interval_seconds: int = Form(default=30),
    db: Session = Depends(get_db),
):
    current_user = require_admin_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    subscription = AlertService.update_subscription(
        db,
        user=current_user,
        enabled=enabled == "true",
        notify_provider_alerts=notify_provider_alerts == "true",
        notify_api_key_alerts=notify_api_key_alerts == "true",
        notify_account_alerts=notify_account_alerts == "true",
        notify_failure_rate_alerts=notify_failure_rate_alerts == "true",
        browser_notifications_enabled=browser_notifications_enabled == "true",
        poll_interval_seconds=poll_interval_seconds,
    )
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="update_alert_subscription",
        entity_type="alert",
        entity_id=subscription.id,
        entity_name="alert_subscription",
        summary=f"更新告警订阅设置 {current_user.username}",
        detail={
            "enabled": subscription.enabled,
            "notify_provider_alerts": subscription.notify_provider_alerts,
            "notify_api_key_alerts": subscription.notify_api_key_alerts,
            "notify_account_alerts": subscription.notify_account_alerts,
            "notify_failure_rate_alerts": subscription.notify_failure_rate_alerts,
            "browser_notifications_enabled": subscription.browser_notifications_enabled,
            "poll_interval_seconds": subscription.poll_interval_seconds,
        },
    )
    return JSONResponse({"success": True})


@router.post("/api/alerts/{event_id}/ack")
def acknowledge_alert_event(
    event_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    current_user = require_admin_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    event = AlertService.acknowledge_event(db, event_id=event_id)
    if event is None:
        return JSONResponse({"detail": "not_found"}, status_code=404)
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="acknowledge_alert",
        entity_type="alert",
        entity_id=event.id,
        entity_name=event.alert_key,
        summary=f"确认告警 {event.alert_key}",
    )
    return JSONResponse({"success": True})


@router.get("/audit-logs", response_class=HTMLResponse)
def audit_logs_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    current_user = require_admin_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    keyword = (request.query_params.get("keyword") or "").strip()
    action = request.query_params.get("action") or ""
    entity_type = request.query_params.get("entity_type") or ""
    page = max(1, int(request.query_params.get("page") or 1))
    page_size = min(100, max(10, int(request.query_params.get("page_size") or 20)))
    total, items = AdminAuditService.list_logs(
        db,
        keyword=keyword or None,
        action=action or None,
        entity_type=entity_type or None,
        page=page,
        page_size=page_size,
    )
    total_pages = max(1, (total + page_size - 1) // page_size)
    filter_options = AdminAuditService.get_filter_options(db)
    return templates.TemplateResponse(
        "audit_logs.html",
        {
            "request": request,
            "title": "操作审计日志",
            "page_name": "audit-logs",
            "portal_type": "admin",
            "current_user": current_user,
            "logs": items,
            "filters": {
                "keyword": keyword,
                "action": action,
                "entity_type": entity_type,
            },
            "filter_options": filter_options,
            "pagination": {
                "page": page,
                "page_size": page_size,
                "total": total,
                "total_pages": total_pages,
                "has_prev": page > 1,
                "has_next": page < total_pages,
                "prev_page": max(1, page - 1),
                "next_page": min(total_pages, page + 1),
            },
            "serialize_audit_detail": AdminAuditService.serialize_detail,
        },
    )
