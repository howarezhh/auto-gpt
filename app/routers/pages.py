from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from app.config import get_settings
from app.database import get_db
from app.models.provider_model import ProviderModel
from app.models.request_log import RequestLog
from app.services.api_key_admin_service import ApiKeyAdminService
from app.services.provider_service import ProviderService
from app.services.setting_service import SettingService
from app.services.user_auth_service import USER_ROLE_ADMIN, UserAuthService


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


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
    recent_since = datetime.utcnow() - timedelta(hours=24)
    recent_requests = db.scalar(select(func.count()).select_from(RequestLog).where(RequestLog.created_at >= recent_since)) or 0
    recent_failures = db.scalar(
        select(func.count()).select_from(RequestLog).where(RequestLog.created_at >= recent_since, RequestLog.success.is_(False))
    ) or 0
    stats = {
        "provider_count": len(providers),
        "healthy_count": len([item for item in providers if item.health_status == "healthy"]),
        "degraded_count": len([item for item in providers if item.health_status == "degraded"]),
        "unhealthy_count": len([item for item in providers if item.health_status == "unhealthy"]),
        "model_count": db.scalar(select(func.count()).select_from(ProviderModel)) or 0,
        "healthy_model_count": db.scalar(select(func.count()).select_from(ProviderModel).where(ProviderModel.health_status == "healthy")) or 0,
        "degraded_model_count": db.scalar(select(func.count()).select_from(ProviderModel).where(ProviderModel.health_status == "degraded")) or 0,
        "unhealthy_model_count": db.scalar(select(func.count()).select_from(ProviderModel).where(ProviderModel.health_status == "unhealthy")) or 0,
        "recent_requests": recent_requests,
        "recent_tokens": db.scalar(select(func.sum(RequestLog.total_tokens)).where(RequestLog.created_at >= recent_since)) or 0,
        "conversation_count": db.scalar(
            select(func.count(func.distinct(RequestLog.conversation_key))).where(RequestLog.conversation_key.is_not(None))
        ) or 0,
        "recent_failure_rate": round((recent_failures / recent_requests) * 100, 2) if recent_requests else 0.0,
        "total_failures": db.scalar(select(func.count()).select_from(RequestLog).where(RequestLog.success.is_(False))) or 0,
        "api_key_total": api_key_summary.total_keys,
        "api_key_enabled": api_key_summary.enabled_keys,
        "api_key_disabled": api_key_summary.disabled_keys,
        "api_key_total_requests": api_key_summary.total_requests,
        "api_key_total_prompt_tokens": api_key_summary.total_prompt_tokens,
        "api_key_total_completion_tokens": api_key_summary.total_completion_tokens,
        "api_key_total_tokens": api_key_summary.total_tokens,
    }
    return templates.TemplateResponse("dashboard.html", {"request": request, "providers": providers, "settings": SettingService.get_or_create(db), "stats": stats, "page_name": "dashboard", "portal_type": "admin", "current_user": current_user})


@router.get("/providers", response_class=HTMLResponse)
def providers_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    current_user = require_admin_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    return templates.TemplateResponse("providers.html", {"request": request, "providers": ProviderService.list_providers(db), "page_name": "providers", "portal_type": "admin", "current_user": current_user})


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
