from __future__ import annotations

from math import ceil
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.config import get_settings
from app.database import get_db
from app.schemas.api_key import ApiKeyCreate, ApiKeyUpdate, RouteMode
from app.services.api_key_admin_service import ApiKeyAdminService
from app.services.api_key_service import ApiClientAuthError, ApiKeyService
from app.services.model_catalog_service import ModelCatalogService
from app.services.provider_service import ProviderService
from app.services.proxy_service import ProxyService
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


def _redirect_user_profile(**updates) -> RedirectResponse:
    params = {key: value for key, value in updates.items() if value not in (None, "", [])}
    query = urlencode(params)
    return RedirectResponse(f"/user/profile?{query}" if query else "/user/profile", status_code=303)


def _redirect_user_api_keys(**updates) -> RedirectResponse:
    params = {key: value for key, value in updates.items() if value not in (None, "", [])}
    query = urlencode(params)
    return RedirectResponse(f"/user/api-keys?{query}" if query else "/user/api-keys", status_code=303)


def _parse_optional_int(value: str | None) -> int | None:
    normalized = (value or "").strip()
    if not normalized:
        return None
    try:
        return int(normalized)
    except ValueError:
        return None


def _parse_optional_int_form(value: str | None, *, field_label: str) -> int | None:
    normalized = (value or "").strip()
    if not normalized:
        return None
    try:
        return int(normalized)
    except ValueError as exc:
        raise ValueError(f"{field_label}必须为数字") from exc


def _build_user_api_key_payload(
    *,
    current_user_id: int,
    selectable_provider_ids: set[int],
    name: str,
    raw_api_key: str | None,
    remark: str | None,
    enabled: bool,
    route_mode: RouteMode,
    default_provider_id: str | None,
    manual_allow_fallback: bool,
    allowed_provider_ids: list[int],
) -> dict:
    parsed_default_provider_id = _parse_optional_int_form(default_provider_id, field_label="默认中转站")
    normalized_allowed_provider_ids: list[int] = []
    seen_provider_ids: set[int] = set()
    for provider_id in allowed_provider_ids:
        if provider_id in seen_provider_ids:
            continue
        seen_provider_ids.add(provider_id)
        normalized_allowed_provider_ids.append(provider_id)
    invalid_provider_ids = [provider_id for provider_id in normalized_allowed_provider_ids if provider_id not in selectable_provider_ids]
    if invalid_provider_ids:
        raise ValueError("仅允许选择管理员已启用的中转站")
    if parsed_default_provider_id is not None:
        if parsed_default_provider_id not in selectable_provider_ids:
            raise ValueError("默认中转站未启用，当前不可选择")
        if parsed_default_provider_id not in seen_provider_ids:
            normalized_allowed_provider_ids.append(parsed_default_provider_id)
    return {
        "name": name,
        "raw_api_key": raw_api_key,
        "remark": remark,
        "enabled": enabled,
        "route_mode": route_mode,
        "default_provider_id": parsed_default_provider_id,
        "owner_user_id": current_user_id,
        "manual_allow_fallback": manual_allow_fallback,
        "allowed_provider_ids": normalized_allowed_provider_ids,
    }


def _resolve_external_base_url(request: Request) -> str:
    configured = get_settings().normalized_external_base_url()
    if configured:
        return configured
    return str(request.base_url).rstrip("/")


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
            "external_base_url": _resolve_external_base_url(request),
            **overview,
        },
    )


@router.get("/user/profile", response_class=HTMLResponse)
def user_profile_page(request: Request, db: Session = Depends(get_db)):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    overview = UserPortalService.get_overview(db, user=current_user)
    selectable_providers = [item for item in ProviderService.list_provider_dicts(db) if item.get("enabled")]
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
            "editing_api_key_id": _parse_optional_int(request.query_params.get("edit")),
            "providers": selectable_providers,
            "external_base_url": _resolve_external_base_url(request),
            **overview,
        },
    )


@router.get("/user/api-keys", response_class=HTMLResponse)
def user_api_keys_page(request: Request, db: Session = Depends(get_db)):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    overview = UserPortalService.get_overview(db, user=current_user)
    selectable_providers = [item for item in ProviderService.list_provider_dicts(db) if item.get("enabled")]
    return templates.TemplateResponse(
        "user_api_keys.html",
        {
            "request": request,
            "title": "API Key 管理",
            "page_name": "user-api-keys",
            "portal_type": "user",
            "current_user": current_user,
            "error_message": request.query_params.get("error"),
            "success_message": request.query_params.get("success"),
            "editing_api_key_id": _parse_optional_int(request.query_params.get("edit")),
            "providers": selectable_providers,
            "external_base_url": _resolve_external_base_url(request),
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


@router.get("/user/docs", response_class=HTMLResponse)
def user_docs_page(request: Request, db: Session = Depends(get_db)):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    external_base_url = _resolve_external_base_url(request)
    external_v1_base_url = f"{external_base_url}/v1"
    return templates.TemplateResponse(
        "docs.html",
        {
            "request": request,
            "title": "使用文档",
            "page_name": "user-docs",
            "portal_type": "user",
            "current_user": current_user,
            "external_base_url": external_base_url,
            "external_v1_base_url": external_v1_base_url,
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


@router.post("/user/api-keys/create", response_class=HTMLResponse)
def create_user_api_key(
    request: Request,
    name: str = Form(...),
    raw_api_key: str | None = Form(default=None),
    remark: str | None = Form(default=None),
    enabled: str | None = Form(default=None),
    route_mode: RouteMode = Form(default="failover"),
    default_provider_id: str | None = Form(default=None),
    manual_allow_fallback: str | None = Form(default=None),
    allowed_provider_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    selectable_provider_ids = {item["id"] for item in ProviderService.list_provider_dicts(db) if item.get("enabled")}
    try:
        payload = ApiKeyCreate(
            **_build_user_api_key_payload(
                current_user_id=current_user.id,
                selectable_provider_ids=selectable_provider_ids,
                name=name,
                raw_api_key=raw_api_key,
                remark=remark,
                enabled=enabled == "on",
                route_mode=route_mode,
                default_provider_id=default_provider_id,
                manual_allow_fallback=manual_allow_fallback == "on",
                allowed_provider_ids=allowed_provider_ids,
            )
        )
        ApiKeyAdminService.create_api_key(db, payload)
    except (ValueError, TypeError) as exc:
        return _redirect_user_api_keys(error=str(exc), edit="new")
    return _redirect_user_api_keys(success="api_key_created")


@router.post("/user/api-keys/{api_key_id}/update", response_class=HTMLResponse)
def update_user_api_key(
    api_key_id: int,
    request: Request,
    name: str = Form(...),
    raw_api_key: str | None = Form(default=None),
    remark: str | None = Form(default=None),
    enabled: str | None = Form(default=None),
    route_mode: RouteMode = Form(default="failover"),
    default_provider_id: str | None = Form(default=None),
    manual_allow_fallback: str | None = Form(default=None),
    allowed_provider_ids: list[int] = Form(default=[]),
    db: Session = Depends(get_db),
):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    api_key = ApiKeyAdminService.get_api_key(db, api_key_id)
    if api_key is None or api_key.owner_user_id != current_user.id:
        return _redirect_user_api_keys(error="api_key_not_found")
    selectable_provider_ids = {item["id"] for item in ProviderService.list_provider_dicts(db) if item.get("enabled")}
    try:
        payload = ApiKeyUpdate(
            **_build_user_api_key_payload(
                current_user_id=current_user.id,
                selectable_provider_ids=selectable_provider_ids,
                name=name,
                raw_api_key=raw_api_key,
                remark=remark,
                enabled=enabled == "on",
                route_mode=route_mode,
                default_provider_id=default_provider_id,
                manual_allow_fallback=manual_allow_fallback == "on",
                allowed_provider_ids=allowed_provider_ids,
            )
        )
        ApiKeyAdminService.update_api_key(db, api_key, payload)
    except (ValueError, TypeError) as exc:
        return _redirect_user_api_keys(error=str(exc), edit=api_key_id)
    return _redirect_user_api_keys(success="api_key_updated", edit=api_key_id)


@router.post("/user/api-keys/{api_key_id}/toggle", response_class=HTMLResponse)
def toggle_user_api_key(
    api_key_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    api_key = ApiKeyAdminService.get_api_key(db, api_key_id)
    if api_key is None or api_key.owner_user_id != current_user.id:
        return _redirect_user_api_keys(error="api_key_not_found")
    ApiKeyAdminService.set_enabled(db, api_key, not api_key.enabled)
    return _redirect_user_api_keys(success="api_key_status_updated", edit=api_key_id)


@router.post("/user/api-keys/{api_key_id}/delete", response_class=HTMLResponse)
def delete_user_api_key(
    api_key_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    api_key = ApiKeyAdminService.get_api_key(db, api_key_id)
    if api_key is None or api_key.owner_user_id != current_user.id:
        return _redirect_user_api_keys(error="api_key_not_found")
    ApiKeyAdminService.delete_api_key(db, api_key)
    return _redirect_user_api_keys(success="api_key_deleted")


@router.get("/user/api-keys/{api_key_id}", response_class=HTMLResponse)
def user_api_key_detail_page(
    api_key_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    payload = UserPortalService.get_api_key_detail_payload(db, user=current_user, api_key_id=api_key_id)
    if payload is None:
        return RedirectResponse("/user/api-keys?error=api_key_not_found", status_code=303)
    return templates.TemplateResponse(
        "user_api_key_detail.html",
        {
            "request": request,
            "title": f"API Key 详情 · {payload['api_key'].name}",
            "page_name": "user-api-key-detail",
            "portal_type": "user",
            "current_user": current_user,
            "success_message": request.query_params.get("success"),
            "external_base_url": _resolve_external_base_url(request),
            **payload,
        },
    )


@router.post("/user/api-keys/{api_key_id}/rotate", response_class=HTMLResponse)
def rotate_user_api_key(
    api_key_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    api_key = ApiKeyAdminService.get_api_key(db, api_key_id)
    if api_key is None or api_key.owner_user_id != current_user.id:
        return _redirect_user_api_keys(error="api_key_not_found")
    ApiKeyAdminService.rotate_api_key(db, api_key)
    return RedirectResponse(f"/user/api-keys/{api_key_id}?success=api_key_rotated", status_code=303)


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


@router.get("/user/logs/export")
def export_user_logs(
    request: Request,
    log_type: str | None = None,
    api_client_key_id: int | None = None,
    conversation_key: str | None = None,
    success: str | None = None,
    limit: int = Query(default=5000, ge=1, le=10000),
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
    csv_text = LogService.export_logs_csv(
        db,
        log_type=log_type if log_type in LogService.USER_VISIBLE_LOG_TYPES else None,
        log_types=list(LogService.USER_VISIBLE_LOG_TYPES),
        provider_id=None,
        model_name=None,
        conversation_key=conversation_key,
        api_client_key_id=api_client_key_id,
        api_client_key_query=None,
        user_account_id=current_user.id,
        success=success_value,
        exclude_health_checks=True,
        api_client_key_ids=UserPortalService.list_owned_api_key_ids(db, user_id=current_user.id),
        limit=limit,
    )
    filename = f"user-logs-{current_user.username}-{request.query_params.get('page_size', 'all')}-{request.query_params.get('page', '1')}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/user/logs/{log_id}", response_class=HTMLResponse)
def user_log_detail_page(
    log_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    log = UserPortalService.get_log_detail(db, user=current_user, log_id=log_id)
    if log is None:
        return RedirectResponse("/user/logs", status_code=303)
    return templates.TemplateResponse(
        "user_log_detail.html",
        {
            "request": request,
            "title": f"日志详情 · #{log.id}",
            "page_name": "user-log-detail",
            "portal_type": "user",
            "current_user": current_user,
            "log": log,
        },
    )


@router.get("/api/user/logs/{log_id}")
def user_log_detail_api(
    log_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    log = UserPortalService.get_log_detail(db, user=current_user, log_id=log_id)
    if log is None:
        return JSONResponse({"detail": "not_found"}, status_code=404)
    return JSONResponse(log.model_dump(mode="json"))


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


@router.get("/user/billing/export")
def export_user_billing(
    request: Request,
    limit: int = Query(default=5000, ge=1, le=10000),
    db: Session = Depends(get_db),
):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    csv_text = UserPortalService.export_billing_csv(db, user=current_user, limit=limit)
    filename = f"user-billing-{current_user.username}.csv"
    return Response(
        content=csv_text,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
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


@router.get("/user/self-test", response_class=HTMLResponse)
def user_self_test_page(
    request: Request,
    api_key_id: int | None = Query(default=None),
    model_name: str | None = Query(default=None),
    db: Session = Depends(get_db),
):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    payload = UserPortalService.get_self_test_payload(
        db,
        user=current_user,
        selected_api_key_id=api_key_id,
        selected_model_name=model_name,
    )
    return templates.TemplateResponse(
        "user_self_test.html",
        {
            "request": request,
            "title": "接入自检",
            "page_name": "user-self-test",
            "portal_type": "user",
            "current_user": current_user,
            "external_base_url": _resolve_external_base_url(request),
            **payload,
        },
    )


@router.post("/api/user/self-test/run")
async def run_user_self_test(
    request: Request,
    api_key_id: int = Form(...),
    model_name: str = Form(...),
    db: Session = Depends(get_db),
):
    current_user = require_user_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return JSONResponse({"success": False, "message": "未登录或权限不足"}, status_code=401)
    api_key = ApiKeyAdminService.get_api_key(db, api_key_id)
    if api_key is None or api_key.owner_user_id != current_user.id:
        return JSONResponse({"success": False, "message": "目标 API Key 不存在"}, status_code=404)
    raw_api_key = ApiKeyService.decrypt_raw_api_key(api_key.raw_key_encrypted)
    if not raw_api_key:
        return JSONResponse({"success": False, "message": "当前密钥无法回显明文，请先轮换后再测试"}, status_code=400)
    try:
        auth_context = ApiKeyService.authenticate_request(db, f"Bearer {raw_api_key}")
        response, provider, trace, latency_ms = await ProxyService.forward_json_request(
            db,
            endpoint_path="/responses",
            payload={
                "model": model_name.strip(),
                "input": "ping",
                "max_output_tokens": 16,
            },
            log_type="responses",
            route_context=auth_context.route_context,
            api_client_auth=auth_context,
        )
        output_text = ProxyService._extract_response_text(response, limit_bytes=500)
        return JSONResponse(
            {
                "success": True,
                "provider_name": provider.name,
                "model_name": model_name.strip(),
                "latency_ms": latency_ms,
                "trace": trace,
                "output_text": output_text,
                "response_preview": response,
            }
        )
    except ApiClientAuthError as exc:
        return JSONResponse(
            {
                "success": False,
                "message": exc.message,
                "code": exc.code,
                "status_code": exc.status_code,
            }
        )
    except HTTPException as exc:
        return JSONResponse(
            {
                "success": False,
                "message": str(exc.detail),
                "status_code": exc.status_code,
            }
        )
