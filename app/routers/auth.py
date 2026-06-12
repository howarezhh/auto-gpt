from app.utils.timezone import now_beijing
from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.admin_audit_service import AdminAuditService
from app.services.setting_service import SettingService
from app.services.user_auth_service import USER_ROLE_USER, UserAuthService
from app.utils.display_format import register_display_filters


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")
register_display_filters(templates)


def _current_user(request: Request, db: Session):
    """读取当前请求对应的登录用户。"""
    return UserAuthService.get_current_user(request, db)


def _record_auth_audit(
    db: Session,
    *,
    request: Request,
    action: str,
    summary: str,
    detail: dict | None = None,
    actor_user_id: int | None = None,
    actor_username: str | None = None,
    target_user_id: int | None = None,
    risk_level: str = "medium",
) -> None:
    AdminAuditService.create_log(
        db,
        actor_user_id=actor_user_id,
        actor_username=actor_username,
        action=action,
        entity_type="auth",
        entity_id=target_user_id or actor_user_id,
        entity_name=actor_username,
        target_user_id=target_user_id,
        summary=summary,
        detail=detail,
        request_trace_id=getattr(request.state, "trace_id", None),
        source_ip=request.client.host if request.client else None,
        risk_level=risk_level,
    )


@router.get("/setup-admin", response_class=HTMLResponse)
def setup_admin_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """展示管理员初始化页面。"""
    if UserAuthService.has_any_admin(db):
        return RedirectResponse("/login", status_code=303)
    return templates.TemplateResponse(
        "setup_admin.html",
        {
            "request": request,
            "title": "初始化管理员",
            "page_name": "setup-admin",
            "error_message": None,
        },
    )


@router.post("/setup-admin", response_class=HTMLResponse)
def setup_admin_submit(request: Request, db: Session = Depends(get_db)):
    """阻止通过网页直接创建管理员，统一要求走服务器脚本。"""
    _record_auth_audit(
        db,
        request=request,
        action="setup_admin_refused",
        summary="拒绝网页初始化管理员",
        detail={"reason": "管理员账号禁止通过网页初始化"},
        risk_level="high",
    )
    return templates.TemplateResponse(
        "setup_admin.html",
        {
            "request": request,
            "title": "初始化管理员",
            "page_name": "setup-admin",
            "error_message": "管理员账号禁止通过网页初始化，请登录服务器后台执行 scripts/create_admin_user.py 创建。",
        },
        status_code=403,
    )


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """展示登录页，并处理已登录用户跳转。"""
    if not UserAuthService.has_any_admin(db):
        return RedirectResponse("/setup-admin", status_code=303)
    user = _current_user(request, db)
    next_path = UserAuthService.normalize_next_path(request.query_params.get("next"))
    if user is not None:
        return RedirectResponse(UserAuthService.resolve_post_login_path(user.role, next_path), status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {
            "request": request,
            "title": "登录",
            "page_name": "login",
            "error_message": None,
            "next_path": next_path,
        },
    )


@router.post("/login", response_class=HTMLResponse)
def login_submit(
    request: Request,
    identifier: str = Form(...),
    password: str = Form(...),
    next_path: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    """校验用户凭证并创建会话。"""
    if not UserAuthService.has_any_admin(db):
        return RedirectResponse("/setup-admin", status_code=303)
    user = UserAuthService.authenticate(db, identifier, password)
    if user is None:
        _record_auth_audit(
            db,
            request=request,
            action="login_failed",
            summary=f"登录失败：{identifier}",
            detail={"identifier": identifier},
            risk_level="medium",
        )
        return templates.TemplateResponse(
            "login.html",
            {
                "request": request,
                "title": "登录",
                "page_name": "login",
                "error_message": "账号或密码错误，或该账号已被禁用",
                "next_path": UserAuthService.normalize_next_path(next_path),
            },
            status_code=400,
        )
    UserAuthService.login_user(request, user)
    _record_auth_audit(
        db,
        request=request,
        action="login_success",
        summary=f"登录成功：{user.username}",
        detail={"role": user.role},
        actor_user_id=user.id,
        actor_username=user.username,
        target_user_id=user.id,
        risk_level="low",
    )
    return RedirectResponse(UserAuthService.resolve_post_login_path(user.role, next_path), status_code=303)


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
    """展示注册页，并根据配置决定是否允许公开注册。"""
    if not UserAuthService.has_any_admin(db):
        return RedirectResponse("/setup-admin", status_code=303)
    user = _current_user(request, db)
    next_path = UserAuthService.normalize_next_path(request.query_params.get("next"))
    if user is not None:
        return RedirectResponse(UserAuthService.resolve_post_login_path(user.role, next_path), status_code=303)
    settings = SettingService.get_or_create(db)
    return templates.TemplateResponse(
        "register.html",
        {
            "request": request,
            "title": "注册",
            "page_name": "register",
            "public_registration_enabled": settings.allow_public_user_registration,
            "error_message": None,
            "next_path": next_path,
        },
    )


@router.post("/register", response_class=HTMLResponse)
def register_submit(
    request: Request,
    username: str = Form(...),
    email: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
    next_path: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    """处理公开注册请求。"""
    settings = SettingService.get_or_create(db)
    if not settings.allow_public_user_registration:
        return templates.TemplateResponse(
            "register.html",
            {
                "request": request,
                "title": "注册",
                "page_name": "register",
                "public_registration_enabled": False,
                "error_message": "当前未开放公开注册，请联系管理员在后台创建账号或开放注册。",
                "next_path": UserAuthService.normalize_next_path(next_path),
            },
            status_code=403,
        )
    if password != password_confirm:
        return templates.TemplateResponse(
            "register.html",
            {
                "request": request,
                "title": "注册",
                "page_name": "register",
                "public_registration_enabled": True,
                "error_message": "两次输入的密码不一致",
                "next_path": UserAuthService.normalize_next_path(next_path),
            },
            status_code=400,
        )
    try:
        user = UserAuthService.create_user(
            db,
            username=username,
            email=email,
            password=password,
            role=USER_ROLE_USER,
            enabled=True,
        )
    except ValueError as exc:
        return templates.TemplateResponse(
            "register.html",
            {
                "request": request,
                "title": "注册",
                "page_name": "register",
                "public_registration_enabled": True,
                "error_message": str(exc),
                "next_path": UserAuthService.normalize_next_path(next_path),
            },
            status_code=400,
        )
    user.last_login_at = now_beijing()
    db.commit()
    db.refresh(user)
    UserAuthService.login_user(request, user)
    _record_auth_audit(
        db,
        request=request,
        action="register_success",
        summary=f"用户公开注册成功：{user.username}",
        detail={"email": user.email, "role": user.role},
        actor_user_id=user.id,
        actor_username=user.username,
        target_user_id=user.id,
        risk_level="medium",
    )
    return RedirectResponse(UserAuthService.resolve_post_login_path(user.role, next_path), status_code=303)


@router.get("/logout")
def logout(request: Request, db: Session = Depends(get_db)) -> RedirectResponse:
    """退出当前登录会话。"""
    user = _current_user(request, db)
    if user is not None:
        _record_auth_audit(
            db,
            request=request,
            action="logout",
            summary=f"退出登录：{user.username}",
            actor_user_id=user.id,
            actor_username=user.username,
            target_user_id=user.id,
            risk_level="low",
        )
    UserAuthService.logout_user(request)
    return RedirectResponse("/login", status_code=303)
