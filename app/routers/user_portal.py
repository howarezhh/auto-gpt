from __future__ import annotations

from math import ceil

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.model_catalog_service import ModelCatalogService
from app.services.user_auth_service import USER_ROLE_ADMIN, UserAuthService
from app.services.user_portal_service import UserPortalService


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def require_user_html(request: Request, db: Session):
    user = UserAuthService.get_current_user(request, db)
    if user is None:
        return RedirectResponse(UserAuthService.build_login_redirect_path(request), status_code=303)
    if user.role == USER_ROLE_ADMIN:
        return RedirectResponse("/", status_code=303)
    return user


def _pagination(total: int, page: int, page_size: int) -> dict:
    total_pages = max(1, ceil(total / page_size)) if page_size else 1
    normalized_page = min(max(1, page), total_pages)
    return {
        "page": normalized_page,
        "page_size": page_size,
        "total": total,
        "total_pages": total_pages,
        "has_prev": normalized_page > 1,
        "has_next": normalized_page < total_pages,
        "prev_page": normalized_page - 1,
        "next_page": normalized_page + 1,
    }


@router.get("/user", response_class=HTMLResponse)
def user_home(request: Request, db: Session = Depends(get_db)):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    overview = UserPortalService.get_overview(db, user=current_user)
    return templates.TemplateResponse(
        "user_home.html",
        {
            "request": request,
            "title": "用户中心",
            "page_name": "user-home",
            "portal_type": "user",
            "current_user": current_user,
            **overview,
        },
    )


@router.get("/user/profile", response_class=HTMLResponse)
def user_profile_page(request: Request, db: Session = Depends(get_db)):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    overview = UserPortalService.get_overview(db, user=current_user)
    return templates.TemplateResponse(
        "user_profile.html",
        {
            "request": request,
            "title": "个人资料",
            "page_name": "user-profile",
            "portal_type": "user",
            "current_user": current_user,
            "error_message": request.query_params.get("error"),
            "success_message": request.query_params.get("success"),
            **overview,
        },
    )


@router.get("/user/models", response_class=HTMLResponse)
def user_models_page(request: Request, db: Session = Depends(get_db)):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    models = ModelCatalogService.list_user_models(db, user=current_user)
    return templates.TemplateResponse(
        "user_models.html",
        {
            "request": request,
            "title": "可用模型",
            "page_name": "user-models",
            "portal_type": "user",
            "current_user": current_user,
            "models": models,
        },
    )


@router.post("/user/profile", response_class=HTMLResponse)
def update_user_profile(
    request: Request,
    username: str = Form(...),
    email: str = Form(...),
    db: Session = Depends(get_db),
):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    try:
        UserAuthService.update_profile(db, current_user, username=username, email=email)
    except ValueError as exc:
        return RedirectResponse(f"/user/profile?error={str(exc)}", status_code=303)
    return RedirectResponse("/user/profile?success=profile_updated", status_code=303)


@router.post("/user/password", response_class=HTMLResponse)
def update_user_password(
    request: Request,
    current_password: str = Form(...),
    new_password: str = Form(...),
    confirm_password: str = Form(...),
    db: Session = Depends(get_db),
):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    if not UserAuthService.verify_password(current_password, current_user.password_hash):
        return RedirectResponse("/user/profile?error=current_password_invalid", status_code=303)
    if new_password != confirm_password:
        return RedirectResponse("/user/profile?error=password_not_match", status_code=303)
    try:
        UserAuthService.update_password(db, current_user, password=new_password)
    except ValueError as exc:
        return RedirectResponse(f"/user/profile?error={str(exc)}", status_code=303)
    return RedirectResponse("/user/profile?success=password_updated", status_code=303)


@router.get("/user/logs", response_class=HTMLResponse)
def user_logs_page(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=10, le=100),
    log_type: str | None = None,
    api_client_key_id: int | None = None,
    conversation_key: str | None = None,
    success: str | None = None,
    db: Session = Depends(get_db),
):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    success_value = None
    if success == "true":
        success_value = True
    elif success == "false":
        success_value = False
    total, items, summary, owned_api_keys = UserPortalService.list_logs(
        db,
        user=current_user,
        page=page,
        page_size=page_size,
        log_type=log_type,
        api_client_key_id=api_client_key_id,
        conversation_key=conversation_key,
        success=success_value,
    )
    pager = _pagination(total, page, page_size)
    return templates.TemplateResponse(
        "user_logs.html",
        {
            "request": request,
            "title": "我的日志",
            "page_name": "user-logs",
            "portal_type": "user",
            "current_user": current_user,
            "logs": items,
            "summary": summary,
            "owned_api_keys": owned_api_keys,
            "filters": {
                "log_type": log_type or "",
                "api_client_key_id": api_client_key_id,
                "conversation_key": conversation_key or "",
                "success": success or "",
            },
            "pagination": pager,
        },
    )


@router.get("/user/billing", response_class=HTMLResponse)
def user_billing_page(
    request: Request,
    limit: int = Query(default=50, ge=10, le=200),
    db: Session = Depends(get_db),
):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    billing = UserPortalService.get_billing_overview(db, user=current_user, limit=limit)
    return templates.TemplateResponse(
        "user_billing.html",
        {
            "request": request,
            "title": "我的账单",
            "page_name": "user-billing",
            "portal_type": "user",
            "current_user": current_user,
            "limit": limit,
            **billing,
        },
    )


@router.get("/user/conversations", response_class=HTMLResponse)
def user_conversations_page(
    request: Request,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=10, le=100),
    query: str | None = None,
    conversation_key: str | None = None,
    db: Session = Depends(get_db),
):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    total, conversations = UserPortalService.list_conversations(
        db,
        user=current_user,
        page=page,
        page_size=page_size,
        query=query,
    )
    active_key = conversation_key or (conversations[0].conversation_key if conversations else None)
    replay = None
    if active_key:
        replay = UserPortalService.get_conversation_replay(db, user=current_user, conversation_key=active_key)
    pager = _pagination(total, page, page_size)
    return templates.TemplateResponse(
        "user_conversations.html",
        {
            "request": request,
            "title": "我的会话回放",
            "page_name": "user-conversations",
            "portal_type": "user",
            "current_user": current_user,
            "conversations": conversations,
            "active_key": active_key,
            "replay": replay,
            "filters": {"query": query or ""},
            "pagination": pager,
        },
    )
