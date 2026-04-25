from __future__ import annotations

from math import ceil
from urllib.parse import parse_qsl, urlencode

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.user_account import UserAccount
from app.services.setting_service import SettingService
from app.services.user_auth_service import USER_ROLE_ADMIN, USER_ROLE_USER, require_admin_api_user, UserAuthService
from app.services.user_portal_service import UserPortalService


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def require_admin_html(request: Request, db: Session) -> UserAccount | RedirectResponse:
    user = UserAuthService.get_current_user(request, db)
    if user is None:
        return RedirectResponse("/login", status_code=303)
    if user.role != USER_ROLE_ADMIN:
        return RedirectResponse("/user", status_code=303)
    return user


def _merge_query(query_string: str | None, **updates) -> str:
    params = dict(parse_qsl(query_string or "", keep_blank_values=False))
    for key, value in updates.items():
        if value is None or value == "":
            params.pop(key, None)
        else:
            params[key] = str(value)
    return urlencode(params)


def _redirect_users(return_to: str | None = None, **updates):
    query = _merge_query(return_to, **updates)
    return RedirectResponse(f"/users?{query}" if query else "/users", status_code=303)


def _users_page_response(
    request: Request,
    db: Session,
    *,
    current_user: UserAccount,
    error_message: str | None = None,
    success_message: str | None = None,
    status_code: int = 200,
):
    keyword = (request.query_params.get("keyword") or "").strip()
    role = request.query_params.get("role") or ""
    enabled_text = request.query_params.get("enabled") or ""
    page = max(1, int(request.query_params.get("page") or 1))
    page_size = min(100, max(10, int(request.query_params.get("page_size") or 20)))
    enabled = None
    if enabled_text == "true":
        enabled = True
    elif enabled_text == "false":
        enabled = False

    total, users = UserPortalService.list_users(
        db,
        keyword=keyword or None,
        role=role or None,
        enabled=enabled,
        page=page,
        page_size=page_size,
    )
    key_counts = UserPortalService.count_user_key_map(db, user_ids=[item.id for item in users])
    admin_count = sum(1 for item in users if item.role == USER_ROLE_ADMIN)
    enabled_count = sum(1 for item in users if item.enabled)
    total_pages = max(1, ceil(total / page_size)) if page_size else 1

    return templates.TemplateResponse(
        "users.html",
        {
            "request": request,
            "title": "用户管理",
            "page_name": "users",
            "portal_type": "admin",
            "current_user": current_user,
            "users": users,
            "key_counts": key_counts,
            "admin_count": admin_count,
            "enabled_count": enabled_count,
            "allow_public_user_registration": SettingService.get_or_create(db).allow_public_user_registration,
            "error_message": error_message,
            "success_message": success_message,
            "filters": {
                "keyword": keyword,
                "role": role,
                "enabled": enabled_text,
            },
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
            "current_query": request.url.query,
        },
        status_code=status_code,
    )


@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request, db: Session = Depends(get_db)):
    current_user = require_admin_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    return _users_page_response(
        request,
        db,
        current_user=current_user,
        error_message=request.query_params.get("error"),
        success_message=request.query_params.get("success"),
    )


@router.get("/users/{user_id}", response_class=HTMLResponse)
def user_detail_page(user_id: int, request: Request, db: Session = Depends(get_db)):
    current_user = require_admin_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    target_user = db.get(UserAccount, user_id)
    if target_user is None:
        return RedirectResponse("/users?error=not_found", status_code=303)
    payload = UserPortalService.get_user_detail_payload(db, user=target_user)
    return templates.TemplateResponse(
        "user_detail.html",
        {
            "request": request,
            "title": f"用户详情 · {target_user.username}",
            "page_name": "users",
            "portal_type": "admin",
            "current_user": current_user,
            **payload,
        },
    )


@router.post("/users/create", response_class=HTMLResponse)
def create_user_submit(
    request: Request,
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    role: str = Form(default=USER_ROLE_USER),
    enabled: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    current_user = require_admin_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    try:
        UserAuthService.create_user(
            db,
            username=username,
            email=email,
            password=password,
            role=USER_ROLE_ADMIN if role == USER_ROLE_ADMIN else USER_ROLE_USER,
            enabled=enabled == "on",
            created_by_user_id=current_user.id,
        )
    except ValueError as exc:
        return _users_page_response(
            request,
            db,
            current_user=current_user,
            error_message=str(exc),
            status_code=400,
        )
    return _redirect_users(request.url.query, success="created")


@router.post("/users/{user_id}/toggle")
def toggle_user_enabled(
    user_id: int,
    request: Request,
    return_to: str = Form(default=""),
    db: Session = Depends(get_db),
):
    current_user = require_admin_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    user = db.get(UserAccount, user_id)
    if user is None:
        return _redirect_users(return_to, error="not_found")
    if user.id == current_user.id and user.enabled:
        return _redirect_users(return_to, error="self_disable")
    UserAuthService.set_enabled(db, user, not user.enabled)
    return _redirect_users(return_to, success="updated")


@router.post("/users/{user_id}/role")
def change_user_role(
    user_id: int,
    request: Request,
    role: str = Form(...),
    return_to: str = Form(default=""),
    db: Session = Depends(get_db),
):
    current_user = require_admin_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    user = db.get(UserAccount, user_id)
    if user is None:
        return _redirect_users(return_to, error="not_found")
    target_role = USER_ROLE_ADMIN if role == USER_ROLE_ADMIN else USER_ROLE_USER
    if user.id == current_user.id and target_role != USER_ROLE_ADMIN:
        return _redirect_users(return_to, error="self_downgrade")
    UserAuthService.set_role(db, user, target_role)
    return _redirect_users(return_to, success="updated")


@router.post("/users/{user_id}/password")
def reset_user_password(
    user_id: int,
    request: Request,
    password: str = Form(...),
    return_to: str = Form(default=""),
    db: Session = Depends(get_db),
):
    current_user = require_admin_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    user = db.get(UserAccount, user_id)
    if user is None:
        return _redirect_users(return_to, error="not_found")
    try:
        UserAuthService.update_password(db, user, password=password)
    except ValueError as exc:
        return _redirect_users(return_to, error=str(exc))
    return _redirect_users(return_to, success="updated")


@router.get("/api/users/options")
def user_options(
    _: UserAccount = Depends(require_admin_api_user),
    db: Session = Depends(get_db),
):
    items = [
        {
            "id": user.id,
            "username": user.username,
            "email": user.email,
            "role": user.role,
            "enabled": user.enabled,
        }
        for user in UserAuthService.list_users(db)
        if user.enabled
    ]
    return JSONResponse(items)
