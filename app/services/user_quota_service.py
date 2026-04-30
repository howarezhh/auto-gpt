from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.models.api_client_key import ApiClientKey
from app.models.request_log import RequestLog
from app.models.user_account import UserAccount
from app.services.api_key_auth_cache import ApiKeyAuthCache
from app.services.billing_service import BillingService
from app.services.log_service import LogService


@dataclass(slots=True)
class UserQuotaUsageSnapshot:
    key_ids: list[int]
    has_balance_limit: bool
    balance_amount: Decimal | None
    frozen_amount: Decimal
    available_balance: Decimal | None
    total_recharge_amount: Decimal
    total_cost_used: Decimal
    day_cost_used: Decimal
    month_cost_used: Decimal
    total_requests: int
    day_requests: int
    month_requests: int
    total_tokens: int
    day_tokens: int
    month_tokens: int


@dataclass(slots=True)
class UserQuotaViolation:
    code: str
    message: str


class UserQuotaService:
    @staticmethod
    def get_realtime_usage_snapshot(db: Session, *, user: UserAccount) -> UserQuotaUsageSnapshot:
        owned_keys = list(
            db.scalars(
                select(ApiClientKey)
                .where(ApiClientKey.owner_user_id == user.id)
                .order_by(ApiClientKey.id.asc())
            )
        )
        key_ids = [item.id for item in owned_keys if item.id is not None]
        balance_amount = BillingService.to_decimal(user.balance_amount)
        frozen_amount = BillingService.to_decimal(user.frozen_amount)
        total_recharge_amount = BillingService.to_decimal(user.total_recharge_amount)
        total_cost_used = sum(
            (BillingService.to_decimal(item.total_cost_used) for item in owned_keys),
            start=Decimal("0"),
        )
        total_tokens = sum((int(item.total_tokens_used or 0) for item in owned_keys), start=0)
        return UserQuotaUsageSnapshot(
            key_ids=key_ids,
            has_balance_limit=True,
            balance_amount=balance_amount,
            frozen_amount=frozen_amount,
            available_balance=balance_amount - frozen_amount,
            total_recharge_amount=total_recharge_amount,
            total_cost_used=total_cost_used,
            day_cost_used=Decimal("0"),
            month_cost_used=Decimal("0"),
            total_requests=0,
            day_requests=0,
            month_requests=0,
            total_tokens=total_tokens,
            day_tokens=0,
            month_tokens=0,
        )

    @staticmethod
    def get_usage_snapshot(db: Session, *, user: UserAccount) -> UserQuotaUsageSnapshot:
        owned_keys = list(
            db.scalars(
                select(ApiClientKey)
                .where(ApiClientKey.owner_user_id == user.id)
                .order_by(ApiClientKey.id.asc())
            )
        )
        key_ids = [item.id for item in owned_keys if item.id is not None]
        has_balance_limit = True
        balance_amount = BillingService.to_decimal(user.balance_amount)
        frozen_amount = BillingService.to_decimal(user.frozen_amount)
        available_balance = balance_amount - frozen_amount
        total_recharge_amount = BillingService.to_decimal(user.total_recharge_amount)
        total_cost_used = sum(
            (BillingService.to_decimal(item.total_cost_used) for item in owned_keys),
            start=Decimal("0"),
        )

        if not key_ids:
            return UserQuotaUsageSnapshot(
                key_ids=[],
                has_balance_limit=has_balance_limit,
                balance_amount=balance_amount,
                frozen_amount=frozen_amount,
                available_balance=available_balance,
                total_recharge_amount=total_recharge_amount,
                total_cost_used=total_cost_used,
                day_cost_used=Decimal("0"),
                month_cost_used=Decimal("0"),
                total_requests=0,
                day_requests=0,
                month_requests=0,
                total_tokens=0,
                day_tokens=0,
                month_tokens=0,
            )

        now = datetime.utcnow()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

        usage_row = db.execute(
            select(
                func.count(RequestLog.id).label("total_requests"),
                func.sum(case((RequestLog.created_at >= day_start, 1), else_=0)).label("day_requests"),
                func.sum(case((RequestLog.created_at >= month_start, 1), else_=0)).label("month_requests"),
                func.sum(RequestLog.total_tokens).label("total_tokens"),
                func.sum(case((RequestLog.created_at >= day_start, RequestLog.total_tokens), else_=0)).label("day_tokens"),
                func.sum(case((RequestLog.created_at >= month_start, RequestLog.total_tokens), else_=0)).label("month_tokens"),
                func.sum(RequestLog.total_cost).label("total_cost_used"),
                func.sum(case((RequestLog.created_at >= day_start, RequestLog.total_cost), else_=0)).label("day_cost_used"),
                func.sum(case((RequestLog.created_at >= month_start, RequestLog.total_cost), else_=0)).label("month_cost_used"),
            ).where(
                RequestLog.api_client_key_id.in_(key_ids),
                LogService._route_traffic_expr(),
                RequestLog.request_path != "/v1/models",
            )
        ).one()

        return UserQuotaUsageSnapshot(
            key_ids=key_ids,
            has_balance_limit=has_balance_limit,
            balance_amount=balance_amount,
            frozen_amount=frozen_amount,
            available_balance=available_balance,
            total_recharge_amount=total_recharge_amount,
            total_cost_used=BillingService.to_decimal(usage_row.total_cost_used),
            day_cost_used=BillingService.to_decimal(usage_row.day_cost_used),
            month_cost_used=BillingService.to_decimal(usage_row.month_cost_used),
            total_requests=int(usage_row.total_requests or 0),
            day_requests=int(usage_row.day_requests or 0),
            month_requests=int(usage_row.month_requests or 0),
            total_tokens=int(usage_row.total_tokens or 0),
            day_tokens=int(usage_row.day_tokens or 0),
            month_tokens=int(usage_row.month_tokens or 0),
        )

    @staticmethod
    def evaluate_violation(*, user: UserAccount, snapshot: UserQuotaUsageSnapshot) -> UserQuotaViolation | None:
        if not user.enabled:
            return UserQuotaViolation(
                code="owner_user_disabled",
                message="Owner account is disabled",
            )
        if snapshot.available_balance is not None and snapshot.available_balance <= Decimal("0"):
            return UserQuotaViolation(
                code="insufficient_balance",
                message="Owner account available balance exhausted",
            )

        request_limits = (
            ("account_request_quota_exhausted", "Owner account total request quota exhausted", snapshot.total_requests, user.request_limit_total),
            ("account_daily_request_quota_exhausted", "Owner account daily request quota exhausted", snapshot.day_requests, user.request_limit_daily),
            ("account_monthly_request_quota_exhausted", "Owner account monthly request quota exhausted", snapshot.month_requests, user.request_limit_monthly),
        )
        for code, message, current, limit in request_limits:
            if limit is not None and current >= limit:
                return UserQuotaViolation(code=code, message=message)

        token_limits = (
            ("account_token_quota_exhausted", "Owner account total token quota exhausted", snapshot.total_tokens, user.token_limit_total),
            ("account_daily_token_quota_exhausted", "Owner account daily token quota exhausted", snapshot.day_tokens, user.token_limit_daily),
            ("account_monthly_token_quota_exhausted", "Owner account monthly token quota exhausted", snapshot.month_tokens, user.token_limit_monthly),
        )
        for code, message, current, limit in token_limits:
            if limit is not None and current >= limit:
                return UserQuotaViolation(code=code, message=message)

        cost_limits = (
            ("account_cost_quota_exhausted", "Owner account total cost quota exhausted", snapshot.total_cost_used, user.cost_limit_total),
            ("account_daily_cost_quota_exhausted", "Owner account daily cost quota exhausted", snapshot.day_cost_used, user.cost_limit_daily),
            ("account_monthly_cost_quota_exhausted", "Owner account monthly cost quota exhausted", snapshot.month_cost_used, user.cost_limit_monthly),
        )
        for code, message, current, limit in cost_limits:
            if limit is not None and current >= BillingService.to_decimal(limit):
                return UserQuotaViolation(code=code, message=message)
        return None

    @staticmethod
    def serialize_policy(*, user: UserAccount, snapshot: UserQuotaUsageSnapshot) -> dict:
        return {
            "user_id": user.id,
            "enabled": user.enabled,
            "balance_amount": BillingService.to_float(snapshot.balance_amount),
            "frozen_amount": BillingService.to_float(snapshot.frozen_amount) or 0,
            "available_balance": BillingService.to_float(snapshot.available_balance),
            "total_recharge_amount": BillingService.to_float(snapshot.total_recharge_amount) or 0,
            "total_cost_used": BillingService.to_float(snapshot.total_cost_used) or 0,
            "day_cost_used": BillingService.to_float(snapshot.day_cost_used) or 0,
            "month_cost_used": BillingService.to_float(snapshot.month_cost_used) or 0,
            "total_requests": snapshot.total_requests,
            "day_requests": snapshot.day_requests,
            "month_requests": snapshot.month_requests,
            "total_tokens": snapshot.total_tokens,
            "day_tokens": snapshot.day_tokens,
            "month_tokens": snapshot.month_tokens,
            "request_limit_total": user.request_limit_total,
            "request_limit_daily": user.request_limit_daily,
            "request_limit_monthly": user.request_limit_monthly,
            "token_limit_total": user.token_limit_total,
            "token_limit_daily": user.token_limit_daily,
            "token_limit_monthly": user.token_limit_monthly,
            "cost_limit_total": BillingService.to_float(user.cost_limit_total),
            "cost_limit_daily": BillingService.to_float(user.cost_limit_daily),
            "cost_limit_monthly": BillingService.to_float(user.cost_limit_monthly),
            "has_balance_limit": snapshot.has_balance_limit,
        }

    @staticmethod
    def update_limits(
        db: Session,
        *,
        user: UserAccount,
        frozen_amount: Decimal,
        request_limit_total: int | None,
        request_limit_daily: int | None,
        request_limit_monthly: int | None,
        token_limit_total: int | None,
        token_limit_daily: int | None,
        token_limit_monthly: int | None,
        cost_limit_total: Decimal | None,
        cost_limit_daily: Decimal | None,
        cost_limit_monthly: Decimal | None,
    ) -> UserAccount:
        user.frozen_amount = BillingService.to_decimal(frozen_amount)
        user.request_limit_total = request_limit_total
        user.request_limit_daily = request_limit_daily
        user.request_limit_monthly = request_limit_monthly
        user.token_limit_total = token_limit_total
        user.token_limit_daily = token_limit_daily
        user.token_limit_monthly = token_limit_monthly
        user.cost_limit_total = BillingService.to_decimal(cost_limit_total) if cost_limit_total is not None else None
        user.cost_limit_daily = BillingService.to_decimal(cost_limit_daily) if cost_limit_daily is not None else None
        user.cost_limit_monthly = BillingService.to_decimal(cost_limit_monthly) if cost_limit_monthly is not None else None
        db.commit()
        db.refresh(user)
        ApiKeyAuthCache.invalidate_user(user.id)
        return user
