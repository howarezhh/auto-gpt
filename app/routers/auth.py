from datetime import datetime

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.services.setting_service import SettingService
from app.services.user_auth_service import USER_ROLE_USER, UserAuthService


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def _current_user(request: Request, db: Session):
    return UserAuthService.get_current_user(request, db)


@router.get("/setup-admin", response_class=HTMLResponse)
def setup_admin_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
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
    if not UserAuthService.has_any_admin(db):
        return RedirectResponse("/setup-admin", status_code=303)
    user = UserAuthService.authenticate(db, identifier, password)
    if user is None:
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
    return RedirectResponse(UserAuthService.resolve_post_login_path(user.role, next_path), status_code=303)


@router.get("/register", response_class=HTMLResponse)
def register_page(request: Request, db: Session = Depends(get_db)) -> HTMLResponse:
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
    user.last_login_at = datetime.utcnow()
    db.commit()
    db.refresh(user)
    UserAuthService.login_user(request, user)
    return RedirectResponse(UserAuthService.resolve_post_login_path(user.role, next_path), status_code=303)


@router.get("/logout")
def logout(request: Request) -> RedirectResponse:
    UserAuthService.logout_user(request)
    return RedirectResponse("/login", status_code=303)
