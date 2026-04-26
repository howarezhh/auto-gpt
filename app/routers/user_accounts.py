from __future__ import annotations

import csv
import io
from decimal import Decimal, InvalidOperation
from math import ceil
from urllib.parse import parse_qsl, urlencode

from fastapi import APIRouter, Depends, Form, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.models.api_client_billing_record import ApiClientBillingRecord
from app.models.api_client_key import ApiClientKey
from app.models.user_account import UserAccount
from app.models.user_account_billing_record import UserAccountBillingRecord
from app.schemas.api_key import ApiKeyBalanceAdjustmentIn
from app.services.admin_audit_service import AdminAuditService
from app.services.api_key_admin_service import ApiKeyAdminService
from app.services.billing_service import BillingService
from app.services.setting_service import SettingService
from app.services.user_auth_service import USER_ROLE_ADMIN, USER_ROLE_USER, require_admin_api_user, UserAuthService
from app.services.user_portal_service import UserPortalService
from app.services.user_quota_service import UserQuotaService


router = APIRouter()
templates = Jinja2Templates(directory="app/templates")


def require_admin_html(request: Request, db: Session) -> UserAccount | RedirectResponse:
    user = UserAuthService.get_current_user(request, db)
    if user is None:
        return RedirectResponse(UserAuthService.build_login_redirect_path(request), status_code=303)
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


def _redirect_user_detail(user_id: int, **updates):
    query = _merge_query(None, **updates)
    return RedirectResponse(f"/users/{user_id}?{query}" if query else f"/users/{user_id}", status_code=303)


def _parse_optional_int(value: str | None, *, field_label: str) -> int | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = int(text)
    except ValueError as exc:
        raise ValueError(f"{field_label}必须为整数") from exc
    if parsed < 0:
        raise ValueError(f"{field_label}不能为负数")
    return parsed


def _parse_optional_decimal(value: str | None, *, field_label: str) -> Decimal | None:
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"{field_label}必须为数字") from exc
    if parsed < 0:
        raise ValueError(f"{field_label}不能为负数")
    return parsed


def _parse_form_user_ids(user_ids: list[int] | None, user_ids_text: str | None) -> list[int]:
    normalized: list[int] = []
    seen: set[int] = set()
    for item in user_ids or []:
        if item in seen:
            continue
        seen.add(item)
        normalized.append(int(item))
    for chunk in (user_ids_text or "").replace("，", ",").split(","):
        text = chunk.strip()
        if not text:
            continue
        value = int(text)
        if value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    if not normalized:
        raise ValueError("至少选择一个用户")
    return normalized


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
            "error_message": request.query_params.get("error"),
            "success_message": request.query_params.get("success"),
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
        created_user = UserAuthService.create_user(
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
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="create",
        entity_type="user",
        entity_id=created_user.id,
        entity_name=created_user.username,
        target_user_id=created_user.id,
        summary=f"创建用户 {created_user.username}",
        detail={"email": email.strip().lower(), "role": role, "enabled": enabled == "on"},
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
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="toggle_enabled",
        entity_type="user",
        entity_id=user.id,
        entity_name=user.username,
        target_user_id=user.id,
        summary=f"{'启用' if user.enabled else '禁用'}用户 {user.username}",
        detail={"enabled": user.enabled},
    )
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
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="change_role",
        entity_type="user",
        entity_id=user.id,
        entity_name=user.username,
        target_user_id=user.id,
        summary=f"修改用户 {user.username} 角色为 {target_role}",
        detail={"role": target_role},
    )
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
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="reset_password",
        entity_type="user",
        entity_id=user.id,
        entity_name=user.username,
        target_user_id=user.id,
        summary=f"重置用户 {user.username} 密码",
    )
    return _redirect_users(return_to, success="updated")


@router.post("/users/{user_id}/quota-policy")
def update_user_quota_policy(
    user_id: int,
    request: Request,
    frozen_amount: str = Form(default="0"),
    request_limit_total: str = Form(default=""),
    request_limit_daily: str = Form(default=""),
    request_limit_monthly: str = Form(default=""),
    token_limit_total: str = Form(default=""),
    token_limit_daily: str = Form(default=""),
    token_limit_monthly: str = Form(default=""),
    cost_limit_total: str = Form(default=""),
    cost_limit_daily: str = Form(default=""),
    cost_limit_monthly: str = Form(default=""),
    db: Session = Depends(get_db),
):
    current_user = require_admin_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    user = db.get(UserAccount, user_id)
    if user is None:
        return _redirect_users(error="not_found")
    try:
        UserQuotaService.update_limits(
            db,
            user=user,
            frozen_amount=_parse_optional_decimal(frozen_amount, field_label="冻结金额") or Decimal("0"),
            request_limit_total=_parse_optional_int(request_limit_total, field_label="总调用次数上限"),
            request_limit_daily=_parse_optional_int(request_limit_daily, field_label="日调用次数上限"),
            request_limit_monthly=_parse_optional_int(request_limit_monthly, field_label="月调用次数上限"),
            token_limit_total=_parse_optional_int(token_limit_total, field_label="总 Token 上限"),
            token_limit_daily=_parse_optional_int(token_limit_daily, field_label="日 Token 上限"),
            token_limit_monthly=_parse_optional_int(token_limit_monthly, field_label="月 Token 上限"),
            cost_limit_total=_parse_optional_decimal(cost_limit_total, field_label="总金额上限"),
            cost_limit_daily=_parse_optional_decimal(cost_limit_daily, field_label="日金额上限"),
            cost_limit_monthly=_parse_optional_decimal(cost_limit_monthly, field_label="月金额上限"),
        )
    except ValueError as exc:
        return _redirect_user_detail(user_id, error=str(exc))
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="update_quota_policy",
        entity_type="user",
        entity_id=user.id,
        entity_name=user.username,
        target_user_id=user.id,
        summary=f"更新用户 {user.username} 账户额度",
        detail={
            "frozen_amount": str(user.frozen_amount),
            "request_limit_total": user.request_limit_total,
            "request_limit_daily": user.request_limit_daily,
            "request_limit_monthly": user.request_limit_monthly,
            "token_limit_total": user.token_limit_total,
            "token_limit_daily": user.token_limit_daily,
            "token_limit_monthly": user.token_limit_monthly,
            "cost_limit_total": str(user.cost_limit_total) if user.cost_limit_total is not None else None,
            "cost_limit_daily": str(user.cost_limit_daily) if user.cost_limit_daily is not None else None,
            "cost_limit_monthly": str(user.cost_limit_monthly) if user.cost_limit_monthly is not None else None,
        },
    )
    return _redirect_user_detail(user_id, success="quota_updated")


@router.post("/users/{user_id}/balance-adjust")
def adjust_user_balance(
    user_id: int,
    request: Request,
    amount: str = Form(...),
    remark: str | None = Form(default=None),
    db: Session = Depends(get_db),
):
    current_user = require_admin_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    user = db.get(UserAccount, user_id)
    if user is None:
        return _redirect_users(error="not_found")
    try:
        payload = ApiKeyBalanceAdjustmentIn(
            amount=float((amount or "").strip()),
            remark=remark,
        )
    except ValueError as exc:
        return _redirect_user_detail(user_id, error=str(exc))
    try:
        BillingService.create_user_balance_adjustment(db, user=user, amount=payload.amount, remark=payload.remark)
    except ValueError as exc:
        return _redirect_user_detail(user_id, error=str(exc))
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="adjust_balance",
        entity_type="user",
        entity_id=user.id,
        entity_name=user.username,
        target_user_id=user.id,
        summary=f"从用户详情页调整账户共享余额",
        detail={"amount": payload.amount, "remark": payload.remark},
    )
    return _redirect_user_detail(user_id, success="balance_adjusted")


@router.post("/users/batch/quota-policy")
def batch_update_user_quota_policy(
    request: Request,
    user_ids: list[int] = Form(default=[]),
    user_ids_text: str = Form(default=""),
    frozen_amount: str = Form(default="0"),
    request_limit_total: str = Form(default=""),
    request_limit_daily: str = Form(default=""),
    request_limit_monthly: str = Form(default=""),
    token_limit_total: str = Form(default=""),
    token_limit_daily: str = Form(default=""),
    token_limit_monthly: str = Form(default=""),
    cost_limit_total: str = Form(default=""),
    cost_limit_daily: str = Form(default=""),
    cost_limit_monthly: str = Form(default=""),
    db: Session = Depends(get_db),
):
    current_user = require_admin_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    try:
        target_user_ids = _parse_form_user_ids(user_ids, user_ids_text)
        frozen_value = _parse_optional_decimal(frozen_amount, field_label="冻结金额") or Decimal("0")
        request_total_value = _parse_optional_int(request_limit_total, field_label="总调用次数上限")
        request_daily_value = _parse_optional_int(request_limit_daily, field_label="日调用次数上限")
        request_monthly_value = _parse_optional_int(request_limit_monthly, field_label="月调用次数上限")
        token_total_value = _parse_optional_int(token_limit_total, field_label="总 Token 上限")
        token_daily_value = _parse_optional_int(token_limit_daily, field_label="日 Token 上限")
        token_monthly_value = _parse_optional_int(token_limit_monthly, field_label="月 Token 上限")
        cost_total_value = _parse_optional_decimal(cost_limit_total, field_label="总金额上限")
        cost_daily_value = _parse_optional_decimal(cost_limit_daily, field_label="日金额上限")
        cost_monthly_value = _parse_optional_decimal(cost_limit_monthly, field_label="月金额上限")
    except ValueError as exc:
        return _redirect_users(request.url.query, error=str(exc))
    affected_user_ids: list[int] = []
    for target_user_id in target_user_ids:
        user = db.get(UserAccount, target_user_id)
        if user is None:
            continue
        UserQuotaService.update_limits(
            db,
            user=user,
            frozen_amount=frozen_value,
            request_limit_total=request_total_value,
            request_limit_daily=request_daily_value,
            request_limit_monthly=request_monthly_value,
            token_limit_total=token_total_value,
            token_limit_daily=token_daily_value,
            token_limit_monthly=token_monthly_value,
            cost_limit_total=cost_total_value,
            cost_limit_daily=cost_daily_value,
            cost_limit_monthly=cost_monthly_value,
        )
        affected_user_ids.append(user.id)
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="batch_update_quota_policy",
        entity_type="user",
        entity_id=None,
        entity_name="batch",
        summary=f"批量更新用户账户额度 {len(affected_user_ids)} 个",
        detail={
            "user_ids": affected_user_ids,
            "frozen_amount": str(frozen_value),
            "request_limit_total": request_total_value,
            "request_limit_daily": request_daily_value,
            "request_limit_monthly": request_monthly_value,
            "token_limit_total": token_total_value,
            "token_limit_daily": token_daily_value,
            "token_limit_monthly": token_monthly_value,
            "cost_limit_total": str(cost_total_value) if cost_total_value is not None else None,
            "cost_limit_daily": str(cost_daily_value) if cost_daily_value is not None else None,
            "cost_limit_monthly": str(cost_monthly_value) if cost_monthly_value is not None else None,
        },
    )
    return _redirect_users(request.url.query, success="updated")


@router.post("/users/{user_id}/delete")
def delete_user(
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
    if user.id == current_user.id:
        return _redirect_users(return_to, error="self_delete")
    admin_total = sum(1 for item in UserAuthService.list_users(db) if item.role == USER_ROLE_ADMIN)
    if user.role == USER_ROLE_ADMIN and admin_total <= 1:
        return _redirect_users(return_to, error="last_admin")
    username = user.username
    UserAuthService.delete_user(db, user)
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="delete",
        entity_type="user",
        entity_id=user_id,
        entity_name=username,
        target_user_id=user_id,
        summary=f"删除用户 {username}",
    )
    return _redirect_users(return_to, success="deleted")


@router.get("/users/export")
def export_users(
    request: Request,
    db: Session = Depends(get_db),
):
    current_user = require_admin_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    users = UserAuthService.list_users(db)
    key_counts = UserPortalService.count_user_key_map(db, user_ids=[item.id for item in users])
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["id", "username", "email", "role", "enabled", "api_key_count", "last_login_at", "created_at"])
    for item in users:
        writer.writerow([
            item.id,
            item.username,
            item.email,
            item.role,
            "true" if item.enabled else "false",
            key_counts.get(item.id, 0),
            item.last_login_at.isoformat() if item.last_login_at else "",
            item.created_at.isoformat() if item.created_at else "",
        ])
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="export",
        entity_type="user",
        entity_id=None,
        entity_name="all_users",
        summary="导出用户列表 CSV",
    )
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="users-export.csv"'},
    )


@router.get("/users/billing-export")
def export_user_billing(
    request: Request,
    limit: int = Query(default=5000, ge=1, le=10000),
    db: Session = Depends(get_db),
):
    current_user = require_admin_html(request, db)
    if isinstance(current_user, RedirectResponse):
        return current_user
    key_rows = list(db.scalars(select(ApiClientKey).order_by(ApiClientKey.id.asc())))
    key_name_map = {item.id: item.name for item in key_rows}
    user_map = {item.id: item.username for item in UserAuthService.list_users(db)}
    items = list(
        db.scalars(
            select(UserAccountBillingRecord)
            .order_by(UserAccountBillingRecord.created_at.desc(), UserAccountBillingRecord.id.desc())
            .limit(max(1, limit))
        )
    )
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(["created_at", "user_account_id", "owner_username", "api_client_key_id", "api_client_key_name", "record_type", "amount", "balance_after", "provider_name", "model_name", "total_tokens", "remark"])
    for item in items:
        writer.writerow([
            item.created_at.isoformat() if item.created_at else "",
            item.user_account_id,
            user_map.get(item.user_account_id, ""),
            item.api_client_key_id,
            key_name_map.get(item.api_client_key_id, ""),
            item.record_type,
            item.amount,
            item.balance_after if item.balance_after is not None else "",
            item.provider_name or "",
            item.model_name or "",
            item.total_tokens or "",
            item.remark or "",
        ])
    AdminAuditService.create_log(
        db,
        actor_user_id=current_user.id,
        actor_username=current_user.username,
        action="export",
        entity_type="billing",
        entity_id=None,
        entity_name="all_user_billing",
        summary="导出用户账单 CSV",
        detail={"limit": limit},
    )
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="user-billing-export.csv"'},
    )


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
