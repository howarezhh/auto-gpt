from datetime import datetime, timedelta
from decimal import Decimal, ROUND_HALF_UP

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models.api_client_billing_record import ApiClientBillingRecord
from app.models.api_client_key import ApiClientKey
from app.models.provider_model import ProviderModel
from app.models.request_log import RequestLog
from app.models.user_account import UserAccount
from app.models.user_account_billing_record import UserAccountBillingRecord
from app.schemas.api_key import ApiKeyBillingRecordOut, ApiKeyBillingSummaryOut


class BillingService:
    MONEY_QUANT = Decimal("0.000001")

    @staticmethod
    def to_decimal(value) -> Decimal:
        if value is None:
            return Decimal("0").quantize(BillingService.MONEY_QUANT)
        if isinstance(value, Decimal):
            return value.quantize(BillingService.MONEY_QUANT, rounding=ROUND_HALF_UP)
        return Decimal(str(value)).quantize(BillingService.MONEY_QUANT, rounding=ROUND_HALF_UP)

    @staticmethod
    def to_float(value) -> float | None:
        if value is None:
            return None
        return float(BillingService.to_decimal(value))

    @staticmethod
    def compute_log_cost(db: Session, log: RequestLog) -> dict[str, Decimal | str | None]:
        if not log.success:
            return {"prompt_cost": Decimal("0"), "completion_cost": Decimal("0"), "total_cost": Decimal("0"), "billing_status": "no_charge"}
        if log.api_client_key_id is None:
            return {"prompt_cost": Decimal("0"), "completion_cost": Decimal("0"), "total_cost": Decimal("0"), "billing_status": "internal_request"}
        if log.request_path == "/v1/models":
            return {"prompt_cost": Decimal("0"), "completion_cost": Decimal("0"), "total_cost": Decimal("0"), "billing_status": "no_charge"}
        if log.prompt_tokens is None and log.completion_tokens is None and log.total_tokens is None:
            return {"prompt_cost": None, "completion_cost": None, "total_cost": None, "billing_status": "pending_tokens"}
        if log.resolved_provider_model_id is None:
            return {"prompt_cost": Decimal("0"), "completion_cost": Decimal("0"), "total_cost": Decimal("0"), "billing_status": "price_unresolved"}

        provider_model = db.get(ProviderModel, log.resolved_provider_model_id)
        if provider_model is None:
            return {"prompt_cost": Decimal("0"), "completion_cost": Decimal("0"), "total_cost": Decimal("0"), "billing_status": "price_unresolved"}

        prompt_tokens = max(0, int(log.prompt_tokens or 0))
        completion_tokens = max(0, int(log.completion_tokens or 0))
        input_price = BillingService.to_decimal(provider_model.input_price_per_1k) if provider_model.input_price_per_1k is not None else None
        output_price = BillingService.to_decimal(provider_model.output_price_per_1k) if provider_model.output_price_per_1k is not None else None

        if input_price is None and output_price is None:
            return {"prompt_cost": Decimal("0"), "completion_cost": Decimal("0"), "total_cost": Decimal("0"), "billing_status": "price_unset"}

        prompt_cost = (BillingService.to_decimal(prompt_tokens) / Decimal("1000")) * (input_price or Decimal("0"))
        completion_cost = (BillingService.to_decimal(completion_tokens) / Decimal("1000")) * (output_price or Decimal("0"))
        total_cost = (prompt_cost + completion_cost).quantize(BillingService.MONEY_QUANT, rounding=ROUND_HALF_UP)
        return {
            "prompt_cost": prompt_cost.quantize(BillingService.MONEY_QUANT, rounding=ROUND_HALF_UP),
            "completion_cost": completion_cost.quantize(BillingService.MONEY_QUANT, rounding=ROUND_HALF_UP),
            "total_cost": total_cost,
            "billing_status": "billed" if total_cost > 0 else "no_charge",
        }

    @staticmethod
    def sync_request_billing(db: Session, log: RequestLog) -> None:
        if log.api_client_key_id is None:
            return
        api_key = db.get(ApiClientKey, log.api_client_key_id)
        if api_key is None:
            return
        owner_user = db.get(UserAccount, api_key.owner_user_id) if api_key.owner_user_id is not None else None

        billing_data = BillingService.compute_log_cost(db, log)
        prompt_cost = billing_data["prompt_cost"]
        completion_cost = billing_data["completion_cost"]
        total_cost = billing_data["total_cost"]
        billing_status = billing_data["billing_status"]

        existing_record = db.scalar(
            select(ApiClientBillingRecord).where(ApiClientBillingRecord.request_log_id == log.id)
        )
        previous_amount = BillingService.to_decimal(abs(existing_record.amount)) if existing_record is not None else Decimal("0")
        new_amount = BillingService.to_decimal(total_cost) if isinstance(total_cost, Decimal) else Decimal("0")
        delta = (new_amount - previous_amount).quantize(BillingService.MONEY_QUANT, rounding=ROUND_HALF_UP)

        if delta:
            api_key.total_cost_used = BillingService.to_decimal(api_key.total_cost_used) + delta
            if owner_user is not None:
                owner_user.balance_amount = BillingService.to_decimal(owner_user.balance_amount) - delta
            elif api_key.balance_amount is not None:
                api_key.balance_amount = BillingService.to_decimal(api_key.balance_amount) - delta

        balance_after = None
        if owner_user is not None:
            balance_after = BillingService.to_decimal(owner_user.balance_amount)
        elif api_key.balance_amount is not None:
            balance_after = BillingService.to_decimal(api_key.balance_amount)

        if new_amount > 0:
            provider_model = db.get(ProviderModel, log.resolved_provider_model_id) if log.resolved_provider_model_id else None
            if existing_record is None:
                existing_record = ApiClientBillingRecord(
                    api_client_key_id=api_key.id,
                    request_log_id=log.id,
                    record_type="request_charge",
                )
                db.add(existing_record)
            existing_record.amount = -new_amount
            existing_record.balance_after = balance_after
            existing_record.provider_id = log.provider_id
            existing_record.provider_name = log.provider_name
            existing_record.model_name = log.requested_model or log.model_name
            existing_record.prompt_tokens = log.prompt_tokens
            existing_record.completion_tokens = log.completion_tokens
            existing_record.total_tokens = log.total_tokens
            existing_record.unit_input_price_per_1k = (
                BillingService.to_decimal(provider_model.input_price_per_1k) if provider_model and provider_model.input_price_per_1k is not None else None
            )
            existing_record.unit_output_price_per_1k = (
                BillingService.to_decimal(provider_model.output_price_per_1k) if provider_model and provider_model.output_price_per_1k is not None else None
            )
            existing_record.remark = log.message
        elif existing_record is not None:
            db.delete(existing_record)

        existing_user_record = db.scalar(
            select(UserAccountBillingRecord).where(UserAccountBillingRecord.request_log_id == log.id)
        ) if owner_user is not None else None
        if owner_user is not None and new_amount > 0:
            provider_model = db.get(ProviderModel, log.resolved_provider_model_id) if log.resolved_provider_model_id else None
            if existing_user_record is None:
                existing_user_record = UserAccountBillingRecord(
                    user_account_id=owner_user.id,
                    api_client_key_id=api_key.id,
                    request_log_id=log.id,
                    record_type="request_charge",
                )
                db.add(existing_user_record)
            existing_user_record.amount = -new_amount
            existing_user_record.balance_after = balance_after
            existing_user_record.provider_id = log.provider_id
            existing_user_record.provider_name = log.provider_name
            existing_user_record.model_name = log.requested_model or log.model_name
            existing_user_record.prompt_tokens = log.prompt_tokens
            existing_user_record.completion_tokens = log.completion_tokens
            existing_user_record.total_tokens = log.total_tokens
            existing_user_record.unit_input_price_per_1k = (
                BillingService.to_decimal(provider_model.input_price_per_1k) if provider_model and provider_model.input_price_per_1k is not None else None
            )
            existing_user_record.unit_output_price_per_1k = (
                BillingService.to_decimal(provider_model.output_price_per_1k) if provider_model and provider_model.output_price_per_1k is not None else None
            )
            existing_user_record.remark = log.message
        elif existing_user_record is not None:
            db.delete(existing_user_record)

        log.prompt_cost = BillingService.to_decimal(prompt_cost) if isinstance(prompt_cost, Decimal) else None
        log.completion_cost = BillingService.to_decimal(completion_cost) if isinstance(completion_cost, Decimal) else None
        log.total_cost = BillingService.to_decimal(total_cost) if isinstance(total_cost, Decimal) else None
        log.billing_status = str(billing_status) if billing_status is not None else None
        log.api_client_balance_after = balance_after

    @staticmethod
    def create_balance_adjustment(
        db: Session,
        *,
        api_key: ApiClientKey,
        amount: float,
        remark: str | None,
    ) -> ApiClientBillingRecord:
        if api_key.owner_user_id is not None:
            record = BillingService.create_user_balance_adjustment(
                db,
                user=db.get(UserAccount, api_key.owner_user_id),
                amount=amount,
                remark=remark,
                source_api_key=api_key,
            )
            return ApiClientBillingRecord(
                api_client_key_id=api_key.id,
                request_log_id=record.request_log_id,
                record_type=record.record_type,
                amount=record.amount,
                balance_after=record.balance_after,
                provider_id=record.provider_id,
                provider_name=record.provider_name,
                model_name=record.model_name,
                prompt_tokens=record.prompt_tokens,
                completion_tokens=record.completion_tokens,
                total_tokens=record.total_tokens,
                unit_input_price_per_1k=record.unit_input_price_per_1k,
                unit_output_price_per_1k=record.unit_output_price_per_1k,
                remark=record.remark,
                created_at=record.created_at,
            )
        delta = BillingService.to_decimal(amount)
        current_balance = BillingService.to_decimal(api_key.balance_amount) if api_key.balance_amount is not None else Decimal("0")
        new_balance = current_balance + delta
        api_key.balance_amount = new_balance
        if delta > 0:
            api_key.total_recharge_amount = BillingService.to_decimal(api_key.total_recharge_amount) + delta
            record_type = "top_up"
        else:
            record_type = "manual_adjustment"
        record = ApiClientBillingRecord(
            api_client_key_id=api_key.id,
            request_log_id=None,
            record_type=record_type,
            amount=delta,
            balance_after=new_balance,
            provider_id=None,
            provider_name=None,
            model_name=None,
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            unit_input_price_per_1k=None,
            unit_output_price_per_1k=None,
            remark=remark,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        db.refresh(api_key)
        return record

    @staticmethod
    def create_user_balance_adjustment(
        db: Session,
        *,
        user: UserAccount | None,
        amount: float,
        remark: str | None,
        source_api_key: ApiClientKey | None = None,
    ) -> UserAccountBillingRecord:
        if user is None:
            raise ValueError("user not found")
        delta = BillingService.to_decimal(amount)
        new_balance = BillingService.to_decimal(user.balance_amount) + delta
        user.balance_amount = new_balance
        if delta > 0:
            user.total_recharge_amount = BillingService.to_decimal(user.total_recharge_amount) + delta
            record_type = "top_up"
        else:
            record_type = "manual_adjustment"
        record = UserAccountBillingRecord(
            user_account_id=user.id,
            api_client_key_id=source_api_key.id if source_api_key is not None else None,
            request_log_id=None,
            record_type=record_type,
            amount=delta,
            balance_after=new_balance,
            provider_id=None,
            provider_name=None,
            model_name=None,
            prompt_tokens=None,
            completion_tokens=None,
            total_tokens=None,
            unit_input_price_per_1k=None,
            unit_output_price_per_1k=None,
            remark=remark,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        db.refresh(user)
        return record

    @staticmethod
    def list_billing_records(
        db: Session,
        *,
        api_key_id: int,
        limit: int = 50,
    ) -> ApiKeyBillingSummaryOut:
        api_key = db.get(ApiClientKey, api_key_id)
        if api_key is None:
            raise ValueError("API key not found")
        items = list(
            db.scalars(
                select(ApiClientBillingRecord)
                .where(ApiClientBillingRecord.api_client_key_id == api_key_id)
                .order_by(ApiClientBillingRecord.created_at.desc(), ApiClientBillingRecord.id.desc())
                .limit(max(1, limit))
            )
        )
        recent_since = datetime.utcnow() - timedelta(hours=24)
        recent_billed_cost = db.scalar(
            select(func.sum(func.abs(ApiClientBillingRecord.amount))).where(
                ApiClientBillingRecord.api_client_key_id == api_key_id,
                ApiClientBillingRecord.record_type == "request_charge",
                ApiClientBillingRecord.created_at >= recent_since,
            )
        ) or 0
        total_records = db.scalar(
            select(func.count(ApiClientBillingRecord.id)).where(ApiClientBillingRecord.api_client_key_id == api_key_id)
        ) or 0
        remaining_cost_quota = None
        if api_key.cost_limit_total is not None:
            remaining_cost_quota = max(
                Decimal("0"),
                BillingService.to_decimal(api_key.cost_limit_total) - BillingService.to_decimal(api_key.total_cost_used),
            )
        owner_user = db.get(UserAccount, api_key.owner_user_id) if api_key.owner_user_id is not None else None
        return ApiKeyBillingSummaryOut(
            api_client_key_id=api_key.id,
            balance_amount=BillingService.to_float(owner_user.balance_amount) if owner_user is not None else BillingService.to_float(api_key.balance_amount),
            total_cost_used=BillingService.to_float(api_key.total_cost_used) or 0,
            total_recharge_amount=BillingService.to_float(owner_user.total_recharge_amount) if owner_user is not None else (BillingService.to_float(api_key.total_recharge_amount) or 0),
            cost_limit_total=BillingService.to_float(api_key.cost_limit_total),
            remaining_cost_quota=BillingService.to_float(remaining_cost_quota),
            recent_billed_cost=BillingService.to_float(recent_billed_cost) or 0,
            total_billing_records=int(total_records),
            items=[BillingService.serialize_billing_record(item) for item in items],
        )

    @staticmethod
    def serialize_billing_record(item: ApiClientBillingRecord) -> ApiKeyBillingRecordOut:
        return ApiKeyBillingRecordOut(
            id=item.id,
            api_client_key_id=item.api_client_key_id,
            request_log_id=item.request_log_id,
            record_type=item.record_type,
            amount=BillingService.to_float(item.amount) or 0,
            balance_after=BillingService.to_float(item.balance_after),
            provider_id=item.provider_id,
            provider_name=item.provider_name,
            model_name=item.model_name,
            prompt_tokens=item.prompt_tokens,
            completion_tokens=item.completion_tokens,
            total_tokens=item.total_tokens,
            unit_input_price_per_1k=BillingService.to_float(item.unit_input_price_per_1k),
            unit_output_price_per_1k=BillingService.to_float(item.unit_output_price_per_1k),
            remark=item.remark,
            created_at=item.created_at,
        )

    @staticmethod
    def serialize_user_billing_record(item: UserAccountBillingRecord) -> dict:
        return {
            "id": item.id,
            "user_account_id": item.user_account_id,
            "api_client_key_id": item.api_client_key_id,
            "request_log_id": item.request_log_id,
            "record_type": item.record_type,
            "amount": BillingService.to_float(item.amount) or 0,
            "balance_after": BillingService.to_float(item.balance_after),
            "provider_id": item.provider_id,
            "provider_name": item.provider_name,
            "model_name": item.model_name,
            "prompt_tokens": item.prompt_tokens,
            "completion_tokens": item.completion_tokens,
            "total_tokens": item.total_tokens,
            "unit_input_price_per_1k": BillingService.to_float(item.unit_input_price_per_1k),
            "unit_output_price_per_1k": BillingService.to_float(item.unit_output_price_per_1k),
            "remark": item.remark,
            "created_at": item.created_at,
        }
