from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import case, func, or_, select
from sqlalchemy.orm import Session

from app.models.api_client_billing_record import ApiClientBillingRecord
from app.models.api_client_key import ApiClientKey
from app.models.request_log import RequestLog
from app.models.user_account import UserAccount
from app.schemas.conversation import ConversationReplay, ConversationSummaryItem
from app.schemas.log import RequestLogOut
from app.services.api_key_admin_service import ApiKeyAdminService
from app.services.billing_service import BillingService
from app.services.conversation_service import ConversationService
from app.services.log_service import LogService


class UserPortalService:
    @staticmethod
    def list_owned_api_keys(db: Session, *, user_id: int) -> list[ApiClientKey]:
        return list(
            db.scalars(
                select(ApiClientKey)
                .where(ApiClientKey.owner_user_id == user_id)
                .order_by(ApiClientKey.id.desc())
            )
        )

    @staticmethod
    def list_owned_api_key_ids(db: Session, *, user_id: int) -> list[int]:
        return list(
            db.scalars(
                select(ApiClientKey.id)
                .where(ApiClientKey.owner_user_id == user_id)
                .order_by(ApiClientKey.id.desc())
            )
        )

    @staticmethod
    def get_overview(db: Session, *, user: UserAccount) -> dict:
        owned_keys = UserPortalService.list_owned_api_keys(db, user_id=user.id)
        serialized_keys = [ApiKeyAdminService.serialize_api_key(item) for item in owned_keys]
        key_ids = [item.id for item in owned_keys]
        totals = {
            "total_requests": 0,
            "total_tokens": 0,
            "total_cost": 0.0,
            "available_balance": sum(float(item.balance_amount or 0) for item in owned_keys),
            "conversation_count": 0,
        }
        if key_ids:
            aggregate_row = db.execute(
                select(
                    func.count(RequestLog.id).label("total_requests"),
                    func.sum(RequestLog.total_tokens).label("total_tokens"),
                    func.sum(RequestLog.total_cost).label("total_cost"),
                    func.count(func.distinct(RequestLog.conversation_key)).label("conversation_count"),
                ).where(RequestLog.api_client_key_id.in_(key_ids))
            ).one()
            totals = {
                "total_requests": int(aggregate_row.total_requests or 0),
                "total_tokens": int(aggregate_row.total_tokens or 0),
                "total_cost": float(aggregate_row.total_cost or 0),
                "available_balance": sum(float(item.balance_amount or 0) for item in owned_keys),
                "conversation_count": int(aggregate_row.conversation_count or 0),
            }

        return {
            "owned_api_keys": serialized_keys,
            "totals": totals,
        }

    @staticmethod
    def list_logs(
        db: Session,
        *,
        user: UserAccount,
        page: int,
        page_size: int,
        log_type: str | None = None,
        api_client_key_id: int | None = None,
        conversation_key: str | None = None,
        success: bool | None = None,
        exclude_health_checks: bool = True,
    ) -> tuple[int, list[RequestLogOut], dict[str, int], list[dict]]:
        owned_keys = UserPortalService.list_owned_api_keys(db, user_id=user.id)
        key_ids = [item.id for item in owned_keys]
        if not key_ids:
            return 0, [], {"total_requests": 0, "success_requests": 0, "failed_requests": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "matched_api_keys": 0}, []
        total, items, summary = LogService.list_logs(
            db,
            page=page,
            page_size=page_size,
            log_type=log_type,
            provider_id=None,
            model_name=None,
            conversation_key=conversation_key,
            api_client_key_id=api_client_key_id,
            api_client_key_query=None,
            success=success,
            exclude_health_checks=exclude_health_checks,
            api_client_key_ids=key_ids,
        )
        return total, [RequestLogOut.model_validate(item) for item in items], summary, [ApiKeyAdminService.serialize_api_key(item) for item in owned_keys]

    @staticmethod
    def get_billing_overview(
        db: Session,
        *,
        user: UserAccount,
        limit: int = 50,
    ) -> dict:
        owned_keys = UserPortalService.list_owned_api_keys(db, user_id=user.id)
        key_ids = [item.id for item in owned_keys]
        if not key_ids:
            return {
                "summary": {
                    "total_keys": 0,
                    "balance_amount": 0.0,
                    "total_cost_used": 0.0,
                    "total_recharge_amount": 0.0,
                    "recent_billed_cost": 0.0,
                    "total_billing_records": 0,
                },
                "key_summaries": [],
                "records": [],
            }

        recent_since = datetime.utcnow() - timedelta(hours=24)
        summary_row = db.execute(
            select(
                func.count(ApiClientBillingRecord.id).label("total_billing_records"),
                func.sum(case((ApiClientBillingRecord.record_type == "request_charge", func.abs(ApiClientBillingRecord.amount)), else_=0)).label("total_request_charge"),
                func.sum(case((ApiClientBillingRecord.record_type == "request_charge", func.abs(ApiClientBillingRecord.amount)), else_=0)).filter(ApiClientBillingRecord.created_at >= recent_since).label("recent_billed_cost"),
            ).where(ApiClientBillingRecord.api_client_key_id.in_(key_ids))
        ).one()

        records = list(
            db.scalars(
                select(ApiClientBillingRecord)
                .where(ApiClientBillingRecord.api_client_key_id.in_(key_ids))
                .order_by(ApiClientBillingRecord.created_at.desc(), ApiClientBillingRecord.id.desc())
                .limit(max(1, limit))
            )
        )
        key_summaries = [ApiKeyAdminService.serialize_api_key(item) for item in owned_keys]
        return {
            "summary": {
                "total_keys": len(owned_keys),
                "balance_amount": sum(float(item.balance_amount or 0) for item in owned_keys),
                "total_cost_used": sum(float(item.total_cost_used or 0) for item in owned_keys),
                "total_recharge_amount": sum(float(item.total_recharge_amount or 0) for item in owned_keys),
                "recent_billed_cost": float(summary_row.recent_billed_cost or 0),
                "total_billing_records": int(summary_row.total_billing_records or 0),
            },
            "key_summaries": key_summaries,
            "records": [BillingService.serialize_billing_record(item) for item in records],
        }

    @staticmethod
    def list_conversations(
        db: Session,
        *,
        user: UserAccount,
        page: int,
        page_size: int,
        query: str | None = None,
    ) -> tuple[int, list[ConversationSummaryItem]]:
        key_ids = UserPortalService.list_owned_api_key_ids(db, user_id=user.id)
        if not key_ids:
            return 0, []
        return ConversationService.list_conversations(
            db,
            page=page,
            page_size=page_size,
            query=query,
            api_client_key_ids=key_ids,
        )

    @staticmethod
    def get_conversation_replay(
        db: Session,
        *,
        user: UserAccount,
        conversation_key: str,
    ) -> ConversationReplay | None:
        key_ids = UserPortalService.list_owned_api_key_ids(db, user_id=user.id)
        if not key_ids:
            return None
        return ConversationService.get_replay(
            db,
            conversation_key,
            api_client_key_ids=key_ids,
        )

    @staticmethod
    def get_user_detail_payload(db: Session, *, user: UserAccount) -> dict:
        overview = UserPortalService.get_overview(db, user=user)
        key_ids = [item["id"] for item in overview["owned_api_keys"]]
        conversation_count = overview["totals"]["conversation_count"]
        recent_logs = []
        recent_billing = []
        if key_ids:
            recent_logs = list(
                db.scalars(
                    select(RequestLog)
                    .where(RequestLog.api_client_key_id.in_(key_ids))
                    .order_by(RequestLog.created_at.desc(), RequestLog.id.desc())
                    .limit(20)
                )
            )
            recent_billing = list(
                db.scalars(
                    select(ApiClientBillingRecord)
                    .where(ApiClientBillingRecord.api_client_key_id.in_(key_ids))
                    .order_by(ApiClientBillingRecord.created_at.desc(), ApiClientBillingRecord.id.desc())
                    .limit(20)
                )
            )
        return {
            "user": user,
            "owned_api_keys": overview["owned_api_keys"],
            "totals": overview["totals"],
            "conversation_count": conversation_count,
            "recent_logs": [RequestLogOut.model_validate(item) for item in recent_logs],
            "recent_billing": [BillingService.serialize_billing_record(item) for item in recent_billing],
        }

    @staticmethod
    def list_users(
        db: Session,
        *,
        keyword: str | None,
        role: str | None,
        enabled: bool | None,
        page: int,
        page_size: int,
    ) -> tuple[int, list[UserAccount]]:
        stmt = select(UserAccount)
        count_stmt = select(func.count()).select_from(UserAccount)
        if keyword:
            like_value = f"%{keyword.strip().lower()}%"
            filters = or_(
                func.lower(UserAccount.username).like(like_value),
                func.lower(UserAccount.email).like(like_value),
            )
            stmt = stmt.where(filters)
            count_stmt = count_stmt.where(filters)
        if role:
            stmt = stmt.where(UserAccount.role == role)
            count_stmt = count_stmt.where(UserAccount.role == role)
        if enabled is not None:
            stmt = stmt.where(UserAccount.enabled == enabled)
            count_stmt = count_stmt.where(UserAccount.enabled == enabled)
        total = int(db.scalar(count_stmt) or 0)
        items = list(
            db.scalars(
                stmt.order_by(UserAccount.id.desc()).offset((page - 1) * page_size).limit(page_size)
            )
        )
        return total, items

    @staticmethod
    def count_user_key_map(db: Session, *, user_ids: list[int]) -> dict[int, int]:
        if not user_ids:
            return {}
        rows = db.execute(
            select(ApiClientKey.owner_user_id, func.count(ApiClientKey.id))
            .where(ApiClientKey.owner_user_id.in_(user_ids))
            .group_by(ApiClientKey.owner_user_id)
        ).all()
        return {int(row[0]): int(row[1]) for row in rows if row[0] is not None}

    @staticmethod
    def format_billing_amount(value: Decimal | float | None) -> float:
        return float(value or 0)
