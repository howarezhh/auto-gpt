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
        return UserQuotaService.get_usage_snapshot(db, user=user)

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

        if not key_ids:
            return UserQuotaUsageSnapshot(
                key_ids=[],
                has_balance_limit=has_balance_limit,
                balance_amount=balance_amount,
                frozen_amount=frozen_amount,
                available_balance=available_balance,
                total_recharge_amount=total_recharge_amount,
                total_cost_used=Decimal("0"),
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
            "has_balance_limit": snapshot.has_balance_limit,
            "request_limit_total": user.request_limit_total,
            "request_limit_daily": user.request_limit_daily,
            "request_limit_monthly": user.request_limit_monthly,
            "token_limit_total": user.token_limit_total,
            "token_limit_daily": user.token_limit_daily,
            "token_limit_monthly": user.token_limit_monthly,
            "cost_limit_total": BillingService.to_float(user.cost_limit_total),
            "cost_limit_daily": BillingService.to_float(user.cost_limit_daily),
            "cost_limit_monthly": BillingService.to_float(user.cost_limit_monthly),
        }

    @staticmethod
    def update_balance_policy(
        db: Session,
        *,
        user: UserAccount,
        frozen_amount: Decimal,
    ) -> UserAccount:
        user.frozen_amount = BillingService.to_decimal(frozen_amount)
        db.commit()
        db.refresh(user)
        ApiKeyAuthCache.invalidate_user(user.id)
        return user
