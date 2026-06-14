from app.utils.timezone import now_beijing
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.models.api_client_billing_record import ApiClientBillingRecord
from app.models.api_client_key import ApiClientKey
from app.models.model_catalog import ModelCatalog
from app.models.provider_model import ProviderModel
from app.models.request_log import RequestLog
from app.models.user_account import UserAccount
from app.models.user_account_billing_record import UserAccountBillingRecord
from app.schemas.api_key import ApiKeyBillingRecordOut, ApiKeyBillingSummaryOut
from app.logging.adapters.billing_adapter import BillingLogRecorder
from app.services.currency_service import CurrencyService
from app.services.log_service import LogService
from app.services.model_pricing_service import ModelPricingService
from app.utils.decimal_utils import (
    MONEY_QUANT,
    decimal_to_float,
    quantize_money,
    to_money_decimal,
    to_multiplier_decimal,
    to_price_decimal,
)
from app.utils.json_utils import loads_json


class BillingService:
    MONEY_QUANT = MONEY_QUANT

    @staticmethod
    def to_decimal(value) -> Decimal:
        return to_money_decimal(value)

    @staticmethod
    def to_float(value) -> float | None:
        return decimal_to_float(quantize_money(value))

    @staticmethod
    def to_price_float(value) -> float | None:
        return decimal_to_float(to_price_decimal(value))

    @staticmethod
    def to_decimal_string(value, *, price: bool = False) -> str | None:
        decimal_value = to_price_decimal(value) if price else quantize_money(value)
        return format(decimal_value, "f") if decimal_value is not None else None

    @staticmethod
    def _parse_exchange_rate_at(value: str | None) -> datetime | None:
        if value in (None, ""):
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
        except ValueError:
            return None

    @staticmethod
    def compute_log_cost(db: Session, log: RequestLog, *, billing_currency: str | None = None) -> dict[str, Decimal | str | None]:
        if not log.success:
            return {"prompt_cost": BillingService.to_decimal(0), "completion_cost": BillingService.to_decimal(0), "total_cost": BillingService.to_decimal(0), "billing_status": "no_charge"}
        if log.api_client_key_id is None:
            return {"prompt_cost": BillingService.to_decimal(0), "completion_cost": BillingService.to_decimal(0), "total_cost": BillingService.to_decimal(0), "billing_status": "internal_request"}
        if LogService.is_model_list_request_path(log.request_path):
            return {"prompt_cost": BillingService.to_decimal(0), "completion_cost": BillingService.to_decimal(0), "total_cost": BillingService.to_decimal(0), "billing_status": "no_charge"}
        if log.prompt_tokens is None and log.completion_tokens is None and log.total_tokens is None:
            return {"prompt_cost": None, "completion_cost": None, "total_cost": None, "billing_status": "pending_tokens"}
        if log.resolved_provider_model_id is None:
            return {"prompt_cost": None, "completion_cost": None, "total_cost": None, "billing_status": "price_unresolved"}

        provider_model = db.get(ProviderModel, log.resolved_provider_model_id)
        if provider_model is None:
            return {"prompt_cost": None, "completion_cost": None, "total_cost": None, "billing_status": "price_unresolved"}
        catalog = db.scalar(select(ModelCatalog).where(ModelCatalog.model_name == provider_model.model_name))

        prompt_tokens = max(0, int(log.prompt_tokens or 0))
        completion_tokens = max(0, int(log.completion_tokens or 0))
        cache_read_tokens = max(0, int(log.cache_read_tokens or 0))
        cache_write_tokens = max(0, int(log.cache_write_tokens or 0))
        if catalog is not None:
            resolved_prices = ModelPricingService.resolve_catalog_prices_for_provider(
                pricing_mode=catalog.pricing_mode,
                pricing_json=catalog.pricing_json,
                input_price_per_1k=catalog.input_price_per_1k,
                output_price_per_1k=catalog.output_price_per_1k,
                cache_price_per_1k=catalog.cache_price_per_1k,
                cache_write_price_per_1k=catalog.cache_write_price_per_1k,
                price_multiplier=provider_model.price_multiplier,
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
            )
        else:
            resolved_prices = {
                "pricing_mode": "fixed",
                "pricing_json": None,
                "tier_key": None,
                "tier_name": None,
                "input_price_per_1k": to_price_decimal(provider_model.input_price_per_1k),
                "output_price_per_1k": to_price_decimal(provider_model.output_price_per_1k),
                "cache_price_per_1k": to_price_decimal(provider_model.cache_price_per_1k)
                if provider_model.cache_price_per_1k is not None
                else to_price_decimal(provider_model.input_price_per_1k),
                "cache_write_price_per_1k": to_price_decimal(provider_model.cache_write_price_per_1k),
                "source_currency": getattr(provider_model, "source_currency", None) or "USD",
                "billing_currency": getattr(provider_model, "billing_currency", None) or "USD",
                "source_input_price_per_1k": to_price_decimal(getattr(provider_model, "source_input_price_per_1k", None) or provider_model.input_price_per_1k),
                "source_output_price_per_1k": to_price_decimal(getattr(provider_model, "source_output_price_per_1k", None) or provider_model.output_price_per_1k),
                "source_cache_price_per_1k": to_price_decimal(getattr(provider_model, "source_cache_price_per_1k", None) or provider_model.cache_price_per_1k),
                "source_cache_write_price_per_1k": to_price_decimal(getattr(provider_model, "source_cache_write_price_per_1k", None) or provider_model.cache_write_price_per_1k),
                "exchange_rate_snapshot": CurrencyService.resolve_conversion_snapshot(
                    source_currency=getattr(provider_model, "source_currency", None) or "USD",
                    billing_currency=getattr(provider_model, "billing_currency", None) or "USD",
                    pricing_metadata={},
                ),
            }
        account_currency = CurrencyService.normalize_currency(billing_currency or resolved_prices.get("billing_currency"))
        source_currency = CurrencyService.normalize_currency(resolved_prices.get("source_currency"))
        price_currency = CurrencyService.normalize_currency(resolved_prices.get("billing_currency"))
        pricing_metadata = resolved_prices.get("pricing_json") if isinstance(resolved_prices.get("pricing_json"), dict) else {}
        exchange_snapshot = CurrencyService.resolve_conversion_snapshot(
            source_currency=source_currency,
            billing_currency=account_currency,
            pricing_metadata=pricing_metadata,
        )

        def resolve_account_price(component: str) -> Decimal | None:
            source_value = resolved_prices.get(f"source_{component}_price_per_1k")
            if source_value is not None:
                converted, _ = CurrencyService.convert_price(
                    source_value,
                    source_currency=source_currency,
                    billing_currency=account_currency,
                    pricing_metadata=pricing_metadata,
                )
                return converted
            converted_price = resolved_prices.get(f"{component}_price_per_1k")
            if price_currency != account_currency:
                converted, _ = CurrencyService.convert_price(
                    converted_price,
                    source_currency=price_currency,
                    billing_currency=account_currency,
                    pricing_metadata=pricing_metadata,
                )
                return converted
            return to_price_decimal(converted_price)

        log.billing_multiplier = to_multiplier_decimal(provider_model.price_multiplier)
        log.source_currency = source_currency
        log.billing_currency = account_currency
        log.source_price_input_per_1k = resolved_prices.get("source_input_price_per_1k")
        log.source_price_output_per_1k = resolved_prices.get("source_output_price_per_1k")
        log.source_price_cache_per_1k = resolved_prices.get("source_cache_price_per_1k")
        log.source_price_cache_write_per_1k = resolved_prices.get("source_cache_write_price_per_1k")
        log.exchange_rate_to_billing_currency = exchange_snapshot.exchange_rate
        log.exchange_rate_source = exchange_snapshot.exchange_rate_source
        log.exchange_rate_at = BillingService._parse_exchange_rate_at(exchange_snapshot.exchange_rate_at)
        log.exchange_rate_version = exchange_snapshot.exchange_rate_version
        log.rounding_strategy = exchange_snapshot.rounding_strategy
        log.channel_price_input_per_1k = resolve_account_price("input")
        log.channel_price_output_per_1k = resolve_account_price("output")
        log.channel_price_cache_per_1k = resolve_account_price("cache")
        log.channel_price_cache_write_per_1k = resolve_account_price("cache_write")
        log.pricing_tier_key = resolved_prices.get("tier_key")
        log.pricing_tier_name = resolved_prices.get("tier_name")
        input_price = log.channel_price_input_per_1k
        output_price = log.channel_price_output_per_1k
        cache_price = log.channel_price_cache_per_1k if log.channel_price_cache_per_1k is not None else input_price
        cache_write_price = log.channel_price_cache_write_per_1k

        regular_prompt_tokens = max(0, prompt_tokens - cache_read_tokens - cache_write_tokens)
        effective_cache_price = cache_price if cache_price is not None else input_price
        effective_cache_write_price = cache_write_price
        missing_price_components = []
        if regular_prompt_tokens > 0 and input_price is None:
            missing_price_components.append("input_price")
        if cache_read_tokens > 0 and effective_cache_price is None:
            missing_price_components.append("cache_read_price")
        if cache_write_tokens > 0 and effective_cache_write_price is None:
            missing_price_components.append("cache_write_price")
        if completion_tokens > 0 and output_price is None:
            missing_price_components.append("output_price")
        if missing_price_components:
            log.billing_error = f"price_unset:{','.join(missing_price_components)}"
            return {"prompt_cost": None, "completion_cost": None, "total_cost": None, "billing_status": "price_unset"}

        prompt_cost = (Decimal(regular_prompt_tokens) / Decimal("1000")) * (input_price or Decimal("0"))
        if cache_read_tokens > 0:
            prompt_cost += (Decimal(cache_read_tokens) / Decimal("1000")) * (effective_cache_price or Decimal("0"))
        if cache_write_tokens > 0:
            prompt_cost += (Decimal(cache_write_tokens) / Decimal("1000")) * (effective_cache_write_price or Decimal("0"))
        completion_cost = (Decimal(completion_tokens) / Decimal("1000")) * (output_price or Decimal("0"))
        component_cost, source_component_cost = BillingService._compute_fee_component_costs(
            log,
            resolved_prices.get("fee_components") or [],
            pricing_metadata=pricing_metadata,
        )
        total_cost = BillingService.to_decimal(prompt_cost + completion_cost + component_cost)
        source_input_price = to_price_decimal(log.source_price_input_per_1k)
        source_output_price = to_price_decimal(log.source_price_output_per_1k)
        source_cache_price = to_price_decimal(log.source_price_cache_per_1k) if log.source_price_cache_per_1k is not None else source_input_price
        source_cache_write_price = to_price_decimal(log.source_price_cache_write_per_1k)
        source_prompt_cost = (Decimal(regular_prompt_tokens) / Decimal("1000")) * (source_input_price or Decimal("0"))
        if cache_read_tokens > 0:
            source_prompt_cost += (Decimal(cache_read_tokens) / Decimal("1000")) * (source_cache_price or Decimal("0"))
        if cache_write_tokens > 0:
            source_prompt_cost += (Decimal(cache_write_tokens) / Decimal("1000")) * (source_cache_write_price or Decimal("0"))
        source_completion_cost = (Decimal(completion_tokens) / Decimal("1000")) * (source_output_price or Decimal("0"))
        source_total_cost = BillingService.to_decimal(source_prompt_cost + source_completion_cost + source_component_cost)
        log.source_prompt_cost = BillingService.to_decimal(source_prompt_cost)
        log.source_completion_cost = BillingService.to_decimal(source_completion_cost)
        log.source_total_cost = source_total_cost
        return {
            "prompt_cost": BillingService.to_decimal(prompt_cost),
            "completion_cost": BillingService.to_decimal(completion_cost),
            "total_cost": total_cost,
            "source_total_cost": source_total_cost,
            "component_cost": BillingService.to_decimal(component_cost),
            "source_component_cost": BillingService.to_decimal(source_component_cost),
            "billing_status": "billed" if total_cost > 0 else "no_charge",
        }

    @staticmethod
    def _compute_fee_component_costs(
        log: RequestLog,
        fee_components: list[dict],
        *,
        pricing_metadata: dict | None = None,
    ) -> tuple[Decimal, Decimal]:
        total = Decimal("0")
        source_total = Decimal("0")
        if not fee_components:
            return total, source_total
        request_body = loads_json(log.request_body_json, {})
        response_body = loads_json(log.response_body_json, {})
        if not isinstance(request_body, dict):
            request_body = {}
        if not isinstance(response_body, dict):
            response_body = {}
        generated_image_count = BillingService._count_generated_images(response_body)
        file_count = 1 if str(log.request_path or "").startswith("/v1/files") else 0
        duration_seconds = Decimal(max(0, int(log.duration_ms or log.latency_ms or 0))) / Decimal("1000")
        account_currency = CurrencyService.normalize_currency(log.billing_currency)
        log_source_currency = CurrencyService.normalize_currency(log.source_currency)
        for component in fee_components:
            if not isinstance(component, dict):
                continue
            unit = str(component.get("unit") or "").strip()
            amount = to_price_decimal(component.get("amount")) or Decimal("0")
            amount_currency = CurrencyService.normalize_currency(component.get("currency"))
            source_amount = to_price_decimal(component.get("source_amount"))
            component_source_currency = CurrencyService.normalize_currency(
                component.get("source_currency"),
                default=log_source_currency,
            )
            quantity = Decimal("0")
            if unit == "per_request":
                quantity = Decimal("1")
            elif unit == "per_image":
                quantity = Decimal(generated_image_count or int(request_body.get("n") or 0) or 0)
            elif unit == "per_file":
                quantity = Decimal(file_count)
            elif unit == "per_second":
                quantity = duration_seconds
            if quantity <= 0:
                continue
            if amount > 0:
                if amount_currency != account_currency:
                    converted_amount, _ = CurrencyService.convert_price(
                        amount,
                        source_currency=amount_currency,
                        billing_currency=account_currency,
                        pricing_metadata=pricing_metadata,
                    )
                    amount = converted_amount or Decimal("0")
                total += amount * quantity
            if source_amount is None:
                source_amount = amount if amount_currency == component_source_currency else Decimal("0")
            if source_amount > 0:
                if component_source_currency != log_source_currency:
                    converted_source_amount, _ = CurrencyService.convert_price(
                        source_amount,
                        source_currency=component_source_currency,
                        billing_currency=log_source_currency,
                        pricing_metadata=pricing_metadata,
                    )
                    source_amount = converted_source_amount or Decimal("0")
                source_total += source_amount * quantity
        return BillingService.to_decimal(total), BillingService.to_decimal(source_total)

    @staticmethod
    def _count_generated_images(response_body: dict | list | None) -> int:
        if not isinstance(response_body, dict):
            return 0
        data = response_body.get("data")
        if isinstance(data, list):
            return sum(1 for item in data if isinstance(item, dict) and (item.get("b64_json") or item.get("url")))
        output = response_body.get("output")
        if isinstance(output, list):
            return sum(1 for item in output if isinstance(item, dict) and item.get("type") == "image_generation_call")
        return 0

    @staticmethod
    def _apply_api_key_billing_delta(db: Session, *, api_key_id: int, delta: Decimal) -> Decimal | None:
        if delta == 0:
            api_key = db.get(ApiClientKey, api_key_id)
            return BillingService.to_decimal(api_key.balance_amount) if api_key and api_key.balance_amount is not None else None
        if delta > 0:
            result = db.execute(
                update(ApiClientKey)
                .where(
                    ApiClientKey.id == api_key_id,
                    ApiClientKey.balance_amount.is_not(None),
                    ApiClientKey.balance_amount >= delta,
                )
                .values(
                    balance_amount=ApiClientKey.balance_amount - delta,
                    total_cost_used=func.coalesce(ApiClientKey.total_cost_used, 0) + delta,
                )
            )
            if result.rowcount != 1:
                raise ValueError("Api key balance is insufficient for request billing")
        else:
            result = db.execute(
                update(ApiClientKey)
                .where(ApiClientKey.id == api_key_id, ApiClientKey.balance_amount.is_not(None))
                .values(
                    balance_amount=ApiClientKey.balance_amount + abs(delta),
                    total_cost_used=func.coalesce(ApiClientKey.total_cost_used, 0) + delta,
                )
            )
            if result.rowcount != 1:
                raise ValueError("Api key balance update failed")
        db.flush()
        api_key = db.get(ApiClientKey, api_key_id)
        return BillingService.to_decimal(api_key.balance_amount) if api_key and api_key.balance_amount is not None else None

    @staticmethod
    def _apply_user_billing_delta(db: Session, *, user_id: int, delta: Decimal) -> Decimal:
        if delta > 0:
            result = db.execute(
                update(UserAccount)
                .where(
                    UserAccount.id == user_id,
                    func.coalesce(UserAccount.balance_amount, 0) - func.coalesce(UserAccount.frozen_amount, 0) >= delta,
                )
                .values(balance_amount=func.coalesce(UserAccount.balance_amount, 0) - delta)
            )
            if result.rowcount != 1:
                raise ValueError("Owner account balance is insufficient for request billing")
        elif delta < 0:
            result = db.execute(
                update(UserAccount)
                .where(UserAccount.id == user_id)
                .values(balance_amount=func.coalesce(UserAccount.balance_amount, 0) + abs(delta))
            )
            if result.rowcount != 1:
                raise ValueError("Owner account balance update failed")
        db.flush()
        owner_user = db.get(UserAccount, user_id)
        if owner_user is None:
            raise ValueError("Owner account not found")
        return BillingService.to_decimal(owner_user.balance_amount)

    @staticmethod
    def _apply_api_key_cost_delta(db: Session, *, api_key_id: int, delta: Decimal) -> None:
        if delta == 0:
            return
        result = db.execute(
            update(ApiClientKey)
            .where(ApiClientKey.id == api_key_id)
            .values(total_cost_used=func.coalesce(ApiClientKey.total_cost_used, 0) + delta)
        )
        if result.rowcount != 1:
            raise ValueError("Api key cost usage update failed")
        db.flush()

    @staticmethod
    def sync_request_billing(db: Session, log: RequestLog) -> Decimal:
        if log.api_client_key_id is None:
            return Decimal("0")
        api_key = db.scalar(
            select(ApiClientKey)
            .where(ApiClientKey.id == log.api_client_key_id)
            .with_for_update()
        )
        if api_key is None:
            return Decimal("0")
        owner_user = (
            db.scalar(
                select(UserAccount)
                .where(UserAccount.id == api_key.owner_user_id)
                .with_for_update()
            )
            if api_key.owner_user_id is not None
            else None
        )

        billing_data = BillingService.compute_log_cost(
            db,
            log,
            billing_currency=getattr(owner_user, "currency_code", None) if owner_user is not None else None,
        )
        prompt_cost = billing_data["prompt_cost"]
        completion_cost = billing_data["completion_cost"]
        total_cost = billing_data["total_cost"]
        billing_status = billing_data["billing_status"]

        existing_record = db.scalar(
            select(ApiClientBillingRecord).where(ApiClientBillingRecord.request_log_id == log.id)
        )
        previous_amount = BillingService.to_decimal(abs(existing_record.amount)) if existing_record is not None else BillingService.to_decimal(0)
        new_amount = BillingService.to_decimal(total_cost) if isinstance(total_cost, Decimal) else BillingService.to_decimal(0)
        delta = BillingService.to_decimal(new_amount - previous_amount)

        if delta:
            if owner_user is not None:
                BillingService._apply_api_key_cost_delta(db, api_key_id=api_key.id, delta=delta)
                BillingService._apply_user_billing_delta(db, user_id=owner_user.id, delta=delta)
            else:
                BillingService._apply_api_key_cost_delta(db, api_key_id=api_key.id, delta=delta)

        balance_after = None
        if owner_user is not None:
            db.refresh(owner_user)
            balance_after = BillingService.to_decimal(owner_user.balance_amount)

        if new_amount > 0:
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
            existing_record.cache_read_tokens = log.cache_read_tokens
            existing_record.cache_write_tokens = log.cache_write_tokens
            existing_record.unit_input_price_per_1k = to_price_decimal(log.channel_price_input_per_1k)
            existing_record.unit_output_price_per_1k = to_price_decimal(log.channel_price_output_per_1k)
            existing_record.unit_cache_read_price_per_1k = to_price_decimal(log.channel_price_cache_per_1k)
            existing_record.unit_cache_write_price_per_1k = to_price_decimal(log.channel_price_cache_write_per_1k)
            existing_record.source_currency = log.source_currency
            existing_record.billing_currency = log.billing_currency
            existing_record.source_amount = -BillingService.to_decimal(log.source_total_cost)
            existing_record.unit_source_input_price_per_1k = to_price_decimal(log.source_price_input_per_1k)
            existing_record.unit_source_output_price_per_1k = to_price_decimal(log.source_price_output_per_1k)
            existing_record.unit_source_cache_read_price_per_1k = to_price_decimal(log.source_price_cache_per_1k)
            existing_record.unit_source_cache_write_price_per_1k = to_price_decimal(log.source_price_cache_write_per_1k)
            existing_record.exchange_rate_to_billing_currency = log.exchange_rate_to_billing_currency
            existing_record.exchange_rate_source = log.exchange_rate_source
            existing_record.exchange_rate_at = log.exchange_rate_at
            existing_record.exchange_rate_version = log.exchange_rate_version
            existing_record.rounding_strategy = log.rounding_strategy
            existing_record.remark = log.message
        elif existing_record is not None:
            db.delete(existing_record)

        existing_user_record = db.scalar(
            select(UserAccountBillingRecord).where(UserAccountBillingRecord.request_log_id == log.id)
        ) if owner_user is not None else None
        if owner_user is not None and new_amount > 0:
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
            existing_user_record.cache_read_tokens = log.cache_read_tokens
            existing_user_record.cache_write_tokens = log.cache_write_tokens
            existing_user_record.unit_input_price_per_1k = to_price_decimal(log.channel_price_input_per_1k)
            existing_user_record.unit_output_price_per_1k = to_price_decimal(log.channel_price_output_per_1k)
            existing_user_record.unit_cache_read_price_per_1k = to_price_decimal(log.channel_price_cache_per_1k)
            existing_user_record.unit_cache_write_price_per_1k = to_price_decimal(log.channel_price_cache_write_per_1k)
            existing_user_record.source_currency = log.source_currency
            existing_user_record.billing_currency = log.billing_currency
            existing_user_record.source_amount = -BillingService.to_decimal(log.source_total_cost)
            existing_user_record.unit_source_input_price_per_1k = to_price_decimal(log.source_price_input_per_1k)
            existing_user_record.unit_source_output_price_per_1k = to_price_decimal(log.source_price_output_per_1k)
            existing_user_record.unit_source_cache_read_price_per_1k = to_price_decimal(log.source_price_cache_per_1k)
            existing_user_record.unit_source_cache_write_price_per_1k = to_price_decimal(log.source_price_cache_write_per_1k)
            existing_user_record.exchange_rate_to_billing_currency = log.exchange_rate_to_billing_currency
            existing_user_record.exchange_rate_source = log.exchange_rate_source
            existing_user_record.exchange_rate_at = log.exchange_rate_at
            existing_user_record.exchange_rate_version = log.exchange_rate_version
            existing_user_record.rounding_strategy = log.rounding_strategy
            existing_user_record.remark = log.message
        elif existing_user_record is not None:
            db.delete(existing_user_record)

        log.prompt_cost = BillingService.to_decimal(prompt_cost) if isinstance(prompt_cost, Decimal) else None
        log.completion_cost = BillingService.to_decimal(completion_cost) if isinstance(completion_cost, Decimal) else None
        log.total_cost = BillingService.to_decimal(total_cost) if isinstance(total_cost, Decimal) else None
        log.billing_status = str(billing_status) if billing_status is not None else None
        log.api_client_balance_after = balance_after
        return delta

    @staticmethod
    def finalize_request_log_billing(db: Session, log: RequestLog) -> Decimal | None:
        if log.api_client_key_id is None:
            return None
        if log.billing_finalized_at is not None and log.billing_status != "pending_tokens":
            return None
        log.billing_attempt_count = int(log.billing_attempt_count or 0) + 1
        log.billing_event_id = log.billing_event_id or f"billing-{log.id}-{uuid4().hex}"
        try:
            billing_delta = BillingService.sync_request_billing(db, log)
            existing_record = db.scalar(
                select(ApiClientBillingRecord).where(ApiClientBillingRecord.request_log_id == log.id)
            )
            if log.billing_status in {"pending_tokens", "price_unresolved", "price_unset"}:
                if log.billing_status == "pending_tokens":
                    log.billing_error = "pending_tokens"
                else:
                    log.billing_error = log.billing_error or log.billing_status
                BillingLogRecorder.record_billing_process(
                    db,
                    request_log_id=log.id,
                    api_client_key_id=log.api_client_key_id,
                    user_account_id=log.user_account_id,
                    pricing_source="provider_model" if log.resolved_provider_model_id is not None else None,
                    cost_snapshot={
                        "prompt_cost": BillingService.to_decimal_string(log.prompt_cost),
                        "completion_cost": BillingService.to_decimal_string(log.completion_cost),
                        "total_cost": BillingService.to_decimal_string(log.total_cost),
                        "source_total_cost": BillingService.to_decimal_string(log.source_total_cost),
                        "source_currency": log.source_currency,
                        "billing_currency": log.billing_currency,
                    },
                    balance_after=BillingService.to_decimal(log.api_client_balance_after),
                    billing_status=log.billing_status,
                    error=log.billing_error,
                    auto_commit=False,
                )
                return None
            log.billing_finalized_at = now_beijing()
            log.billing_error = None
            BillingLogRecorder.record_billing_process(
                db,
                request_log_id=log.id,
                api_client_key_id=log.api_client_key_id,
                user_account_id=log.user_account_id,
                pricing_source="provider_model" if log.resolved_provider_model_id is not None else None,
                pricing_snapshot={
                    "billing_multiplier": BillingService.to_decimal_string(log.billing_multiplier, price=True),
                    "channel_price_input_per_1k": BillingService.to_decimal_string(log.channel_price_input_per_1k, price=True),
                    "channel_price_output_per_1k": BillingService.to_decimal_string(log.channel_price_output_per_1k, price=True),
                    "channel_price_cache_per_1k": BillingService.to_decimal_string(log.channel_price_cache_per_1k, price=True),
                    "channel_price_cache_write_per_1k": BillingService.to_decimal_string(log.channel_price_cache_write_per_1k, price=True),
                    "source_price_input_per_1k": BillingService.to_decimal_string(log.source_price_input_per_1k, price=True),
                    "source_price_output_per_1k": BillingService.to_decimal_string(log.source_price_output_per_1k, price=True),
                    "source_price_cache_per_1k": BillingService.to_decimal_string(log.source_price_cache_per_1k, price=True),
                    "source_price_cache_write_per_1k": BillingService.to_decimal_string(log.source_price_cache_write_per_1k, price=True),
                    "source_currency": log.source_currency,
                    "billing_currency": log.billing_currency,
                    "exchange_rate_to_billing_currency": BillingService.to_decimal_string(log.exchange_rate_to_billing_currency, price=True),
                    "exchange_rate_source": log.exchange_rate_source,
                    "exchange_rate_at": log.exchange_rate_at.isoformat() if log.exchange_rate_at else None,
                    "exchange_rate_version": log.exchange_rate_version,
                    "rounding_strategy": log.rounding_strategy,
                },
                cost_snapshot={
                    "prompt_cost": BillingService.to_decimal_string(log.prompt_cost),
                    "completion_cost": BillingService.to_decimal_string(log.completion_cost),
                    "total_cost": BillingService.to_decimal_string(log.total_cost),
                    "source_prompt_cost": BillingService.to_decimal_string(log.source_prompt_cost),
                    "source_completion_cost": BillingService.to_decimal_string(log.source_completion_cost),
                    "source_total_cost": BillingService.to_decimal_string(log.source_total_cost),
                    "fee_components_supported": True,
                },
                balance_delta=BillingService.to_decimal(billing_delta),
                balance_after=BillingService.to_decimal(log.api_client_balance_after),
                billing_status=log.billing_status or "billed",
                billing_record_id=existing_record.id if existing_record is not None else None,
                auto_commit=False,
            )
            return billing_delta
        except Exception as exc:
            log.billing_error = str(exc)[:1000]
            BillingLogRecorder.record_billing_process(
                db,
                request_log_id=log.id,
                api_client_key_id=log.api_client_key_id,
                user_account_id=log.user_account_id,
                pricing_source="provider_model" if log.resolved_provider_model_id is not None else None,
                billing_status="failed",
                error=log.billing_error,
                auto_commit=False,
            )
            raise

    @staticmethod
    def create_balance_adjustment(
        db: Session,
        *,
        api_key: ApiClientKey,
        amount: Decimal | str,
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
                cache_read_tokens=record.cache_read_tokens,
                cache_write_tokens=record.cache_write_tokens,
                unit_cache_read_price_per_1k=record.unit_cache_read_price_per_1k,
                unit_cache_write_price_per_1k=record.unit_cache_write_price_per_1k,
                remark=record.remark,
                created_at=record.created_at,
            )
        raise ValueError("API Key 已取消独立余额，请先绑定归属用户后在用户账户余额中调账")

    @staticmethod
    def create_user_balance_adjustment(
        db: Session,
        *,
        user: UserAccount | None,
        amount: Decimal | str,
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
            cache_read_tokens=None,
            cache_write_tokens=None,
            unit_input_price_per_1k=None,
            unit_output_price_per_1k=None,
            unit_cache_read_price_per_1k=None,
            unit_cache_write_price_per_1k=None,
            source_currency=getattr(user, "currency_code", None) or "USD",
            billing_currency=getattr(user, "currency_code", None) or "USD",
            source_amount=delta,
            exchange_rate_to_billing_currency=Decimal("1"),
            exchange_rate_source="same_currency",
            exchange_rate_version="same_currency",
            rounding_strategy="ROUND_HALF_UP",
            remark=remark,
        )
        db.add(record)
        db.commit()
        db.refresh(record)
        db.refresh(user)
        from app.services.api_key_auth_cache import ApiKeyAuthCache

        ApiKeyAuthCache.invalidate_user(user.id)
        return record

    @staticmethod
    def list_billing_records(
        db: Session,
        *,
        api_key_id: int,
        limit: int = 50,
    ) -> ApiKeyBillingSummaryOut:
        normalized_limit = max(1, min(int(limit or 50), 500))
        api_key = db.get(ApiClientKey, api_key_id)
        if api_key is None:
            raise ValueError("API key not found")
        owner_user = db.get(UserAccount, api_key.owner_user_id) if api_key.owner_user_id is not None else None
        record_model = UserAccountBillingRecord if owner_user is not None else ApiClientBillingRecord
        items = list(
            db.scalars(
                select(record_model)
                .where(record_model.api_client_key_id == api_key_id)
                .order_by(record_model.created_at.desc(), record_model.id.desc())
                .limit(normalized_limit)
            )
        )
        recent_since = now_beijing() - timedelta(hours=24)
        recent_billed_cost = db.scalar(
            select(func.sum(func.abs(record_model.amount))).where(
                record_model.api_client_key_id == api_key_id,
                record_model.record_type == "request_charge",
                record_model.created_at >= recent_since,
            )
        ) or 0
        total_records = db.scalar(
            select(func.count(record_model.id)).where(record_model.api_client_key_id == api_key_id)
        ) or 0
        return ApiKeyBillingSummaryOut(
            api_client_key_id=api_key.id,
            balance_amount=BillingService.to_decimal_string(owner_user.balance_amount) if owner_user is not None else None,
            total_cost_used=BillingService.to_decimal_string(api_key.total_cost_used) or "0",
            total_recharge_amount=(
                BillingService.to_decimal_string(owner_user.total_recharge_amount)
                if owner_user is not None
                else "0"
            ) or "0",
            recent_billed_cost=BillingService.to_decimal_string(recent_billed_cost) or "0",
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
            amount=BillingService.to_decimal_string(item.amount) or "0",
            balance_after=BillingService.to_decimal_string(item.balance_after),
            provider_id=item.provider_id,
            provider_name=item.provider_name,
            model_name=item.model_name,
            prompt_tokens=item.prompt_tokens,
            completion_tokens=item.completion_tokens,
            total_tokens=item.total_tokens,
            cache_read_tokens=item.cache_read_tokens,
            cache_write_tokens=item.cache_write_tokens,
            unit_input_price_per_1k=BillingService.to_decimal_string(item.unit_input_price_per_1k, price=True),
            unit_output_price_per_1k=BillingService.to_decimal_string(item.unit_output_price_per_1k, price=True),
            unit_cache_read_price_per_1k=BillingService.to_decimal_string(item.unit_cache_read_price_per_1k, price=True),
            unit_cache_write_price_per_1k=BillingService.to_decimal_string(item.unit_cache_write_price_per_1k, price=True),
            source_currency=item.source_currency,
            billing_currency=item.billing_currency,
            source_amount=BillingService.to_decimal_string(item.source_amount),
            unit_source_input_price_per_1k=BillingService.to_decimal_string(item.unit_source_input_price_per_1k, price=True),
            unit_source_output_price_per_1k=BillingService.to_decimal_string(item.unit_source_output_price_per_1k, price=True),
            unit_source_cache_read_price_per_1k=BillingService.to_decimal_string(item.unit_source_cache_read_price_per_1k, price=True),
            unit_source_cache_write_price_per_1k=BillingService.to_decimal_string(item.unit_source_cache_write_price_per_1k, price=True),
            exchange_rate_to_billing_currency=BillingService.to_decimal_string(item.exchange_rate_to_billing_currency, price=True),
            exchange_rate_source=item.exchange_rate_source,
            exchange_rate_at=item.exchange_rate_at,
            exchange_rate_version=item.exchange_rate_version,
            rounding_strategy=item.rounding_strategy,
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
            "amount": BillingService.to_decimal_string(item.amount) or "0",
            "balance_after": BillingService.to_decimal_string(item.balance_after),
            "provider_id": item.provider_id,
            "provider_name": item.provider_name,
            "model_name": item.model_name,
            "prompt_tokens": item.prompt_tokens,
            "completion_tokens": item.completion_tokens,
            "total_tokens": item.total_tokens,
            "cache_read_tokens": item.cache_read_tokens,
            "cache_write_tokens": item.cache_write_tokens,
            "unit_input_price_per_1k": BillingService.to_decimal_string(item.unit_input_price_per_1k, price=True),
            "unit_output_price_per_1k": BillingService.to_decimal_string(item.unit_output_price_per_1k, price=True),
            "unit_cache_read_price_per_1k": BillingService.to_decimal_string(item.unit_cache_read_price_per_1k, price=True),
            "unit_cache_write_price_per_1k": BillingService.to_decimal_string(item.unit_cache_write_price_per_1k, price=True),
            "source_currency": item.source_currency,
            "billing_currency": item.billing_currency,
            "source_amount": BillingService.to_decimal_string(item.source_amount),
            "unit_source_input_price_per_1k": BillingService.to_decimal_string(item.unit_source_input_price_per_1k, price=True),
            "unit_source_output_price_per_1k": BillingService.to_decimal_string(item.unit_source_output_price_per_1k, price=True),
            "unit_source_cache_read_price_per_1k": BillingService.to_decimal_string(item.unit_source_cache_read_price_per_1k, price=True),
            "unit_source_cache_write_price_per_1k": BillingService.to_decimal_string(item.unit_source_cache_write_price_per_1k, price=True),
            "exchange_rate_to_billing_currency": BillingService.to_decimal_string(item.exchange_rate_to_billing_currency, price=True),
            "exchange_rate_source": item.exchange_rate_source,
            "exchange_rate_at": item.exchange_rate_at,
            "exchange_rate_version": item.exchange_rate_version,
            "rounding_strategy": item.rounding_strategy,
            "remark": item.remark,
            "created_at": item.created_at,
        }
