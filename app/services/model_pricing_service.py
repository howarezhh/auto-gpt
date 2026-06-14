from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from typing import Any

from app.services.currency_service import CurrencyService
from app.utils.decimal_utils import decimal_to_float, multiply_price_and_multiplier, to_price_decimal
from app.utils.json_utils import dumps_json, loads_json


class ModelPricingService:
    PRICING_MODE_FIXED = "fixed"
    PRICING_MODE_TIERED = "tiered"
    PRICING_MODE_UNPRICED = "unpriced"
    VALID_PRICING_MODES = {
        PRICING_MODE_FIXED,
        PRICING_MODE_TIERED,
        PRICING_MODE_UNPRICED,
    }

    @staticmethod
    def normalize_pricing_mode(value: str | None, *, has_tiers: bool, has_base_price: bool) -> str:
        normalized = str(value or "").strip().lower()
        if normalized in ModelPricingService.VALID_PRICING_MODES:
            return normalized
        if has_tiers:
            return ModelPricingService.PRICING_MODE_TIERED
        if has_base_price:
            return ModelPricingService.PRICING_MODE_FIXED
        return ModelPricingService.PRICING_MODE_UNPRICED

    @staticmethod
    def normalize_catalog_pricing(
        *,
        pricing_mode: str | None,
        pricing_json: dict | None,
        input_price_per_1k,
        output_price_per_1k,
        cache_price_per_1k,
        cache_write_price_per_1k=None,
    ) -> dict[str, Any]:
        normalized_json = ModelPricingService.normalize_pricing_json(pricing_json)
        tiers = normalized_json.get("tiers", [])
        normalized_input = to_price_decimal(input_price_per_1k)
        normalized_output = to_price_decimal(output_price_per_1k)
        normalized_cache = to_price_decimal(cache_price_per_1k)
        normalized_cache_write = to_price_decimal(cache_write_price_per_1k)
        source_input = to_price_decimal(normalized_json.get("source_input_price_per_1k")) or normalized_input
        source_output = to_price_decimal(normalized_json.get("source_output_price_per_1k")) or normalized_output
        source_cache = to_price_decimal(normalized_json.get("source_cache_price_per_1k")) or normalized_cache
        source_cache_write = to_price_decimal(normalized_json.get("source_cache_write_price_per_1k")) or normalized_cache_write
        has_base_price = any(item is not None for item in (normalized_input, normalized_output, normalized_cache, normalized_cache_write, source_input, source_output, source_cache, source_cache_write))
        normalized_mode = ModelPricingService.normalize_pricing_mode(
            pricing_mode,
            has_tiers=bool(tiers),
            has_base_price=has_base_price,
        )

        if normalized_mode == ModelPricingService.PRICING_MODE_TIERED:
            if not tiers:
                raise ValueError("阶梯价模式至少需要一档价格")
            default_tier = ModelPricingService.pick_default_tier(tiers)
            default_prices = ModelPricingService._resolve_prices_from_item(default_tier, normalized_json)
            normalized_input = default_prices["input_price_per_1k"]
            normalized_output = default_prices["output_price_per_1k"]
            normalized_cache = default_prices["cache_price_per_1k"]
            normalized_cache_write = default_prices["cache_write_price_per_1k"]
            source_input = default_prices["source_input_price_per_1k"]
            source_output = default_prices["source_output_price_per_1k"]
            source_cache = default_prices["source_cache_price_per_1k"]
            source_cache_write = default_prices["source_cache_write_price_per_1k"]
        elif normalized_mode == ModelPricingService.PRICING_MODE_UNPRICED:
            normalized_input = None
            normalized_output = None
            normalized_cache = None
            normalized_cache_write = None
            source_input = None
            source_output = None
            source_cache = None
            source_cache_write = None
            normalized_json["tiers"] = []
        else:
            if not has_base_price and tiers:
                default_tier = ModelPricingService.pick_default_tier(tiers)
                default_prices = ModelPricingService._resolve_prices_from_item(default_tier, normalized_json)
                normalized_input = default_prices["input_price_per_1k"]
                normalized_output = default_prices["output_price_per_1k"]
                normalized_cache = default_prices["cache_price_per_1k"]
                normalized_cache_write = default_prices["cache_write_price_per_1k"]
                source_input = default_prices["source_input_price_per_1k"]
                source_output = default_prices["source_output_price_per_1k"]
                source_cache = default_prices["source_cache_price_per_1k"]
                source_cache_write = default_prices["source_cache_write_price_per_1k"]
            if normalized_input is None and source_input is not None:
                normalized_input, _ = CurrencyService.convert_price(
                    source_input,
                    source_currency=normalized_json.get("source_currency"),
                    billing_currency=normalized_json.get("billing_currency"),
                    pricing_metadata=normalized_json,
                )
            if normalized_output is None and source_output is not None:
                normalized_output, _ = CurrencyService.convert_price(
                    source_output,
                    source_currency=normalized_json.get("source_currency"),
                    billing_currency=normalized_json.get("billing_currency"),
                    pricing_metadata=normalized_json,
                )
            if normalized_cache is None and source_cache is not None:
                normalized_cache, _ = CurrencyService.convert_price(
                    source_cache,
                    source_currency=normalized_json.get("source_currency"),
                    billing_currency=normalized_json.get("billing_currency"),
                    pricing_metadata=normalized_json,
                )
            if normalized_cache_write is None and source_cache_write is not None:
                normalized_cache_write, _ = CurrencyService.convert_price(
                    source_cache_write,
                    source_currency=normalized_json.get("source_currency"),
                    billing_currency=normalized_json.get("billing_currency"),
                    pricing_metadata=normalized_json,
                )
            if normalized_input is None and normalized_output is None and normalized_cache is None and normalized_cache_write is None:
                normalized_mode = ModelPricingService.PRICING_MODE_UNPRICED
                normalized_json["tiers"] = []
        if normalized_cache is None and normalized_input is not None:
            normalized_cache = normalized_input
        if source_cache is None and source_input is not None:
            source_cache = source_input
        return {
            "pricing_mode": normalized_mode,
            "pricing_json": normalized_json,
            "input_price_per_1k": normalized_input,
            "output_price_per_1k": normalized_output,
            "cache_price_per_1k": normalized_cache,
            "cache_write_price_per_1k": normalized_cache_write,
            "source_currency": CurrencyService.normalize_currency(normalized_json.get("source_currency")),
            "billing_currency": CurrencyService.normalize_currency(normalized_json.get("billing_currency")),
            "source_input_price_per_1k": source_input,
            "source_output_price_per_1k": source_output,
            "source_cache_price_per_1k": source_cache,
            "source_cache_write_price_per_1k": source_cache_write,
            "exchange_rate_snapshot": CurrencyService.resolve_conversion_snapshot(
                source_currency=normalized_json.get("source_currency"),
                billing_currency=normalized_json.get("billing_currency"),
                pricing_metadata=normalized_json,
            ),
        }

    @staticmethod
    def normalize_pricing_json(pricing_json: dict | None) -> dict[str, Any]:
        payload = pricing_json if isinstance(pricing_json, dict) else {}
        normalized = {
            "source_label": ModelPricingService._normalize_optional_text(payload.get("source_label")),
            "source_url": ModelPricingService._normalize_optional_text(payload.get("source_url")),
            "note": ModelPricingService._normalize_optional_text(payload.get("note")),
            "updated_at": ModelPricingService._normalize_datetime_value(payload.get("updated_at")),
            "source_currency": CurrencyService.normalize_currency(payload.get("source_currency")),
            "billing_currency": CurrencyService.normalize_currency(payload.get("billing_currency")),
            "exchange_rate_to_usd": ModelPricingService._normalize_optional_decimal_text(payload.get("exchange_rate_to_usd")),
            "exchange_rate_to_cny": ModelPricingService._normalize_optional_decimal_text(payload.get("exchange_rate_to_cny")),
            "exchange_rate_source": ModelPricingService._normalize_optional_text(payload.get("exchange_rate_source")),
            "exchange_rate_at": ModelPricingService._normalize_datetime_value(payload.get("exchange_rate_at")),
            "exchange_rate_version": ModelPricingService._normalize_optional_text(payload.get("exchange_rate_version")),
            "rounding_strategy": ModelPricingService._normalize_optional_text(payload.get("rounding_strategy")) or "ROUND_HALF_UP",
            "source_input_price_per_1k": to_price_decimal(payload.get("source_input_price_per_1k")),
            "source_output_price_per_1k": to_price_decimal(payload.get("source_output_price_per_1k")),
            "source_cache_price_per_1k": to_price_decimal(payload.get("source_cache_price_per_1k")),
            "source_cache_write_price_per_1k": to_price_decimal(payload.get("source_cache_write_price_per_1k")),
            "fee_components": ModelPricingService.normalize_fee_components(
                payload.get("fee_components"),
                source_currency=payload.get("source_currency"),
                billing_currency=payload.get("billing_currency"),
                pricing_metadata=payload,
            ),
            "tiers": ModelPricingService.normalize_pricing_tiers(payload.get("tiers")),
        }
        return normalized

    @staticmethod
    def normalize_pricing_tiers(value: Any) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        tiers: list[dict[str, Any]] = []
        for index, item in enumerate(value, start=1):
            if not isinstance(item, dict):
                continue
            tier_key = ModelPricingService._normalize_optional_text(item.get("tier_key")) or f"tier-{index}"
            tier_name = ModelPricingService._normalize_optional_text(item.get("tier_name")) or tier_key
            normalized = {
                "tier_key": tier_key,
                "tier_name": tier_name,
                "min_prompt_tokens": ModelPricingService._normalize_optional_int(item.get("min_prompt_tokens")),
                "max_prompt_tokens": ModelPricingService._normalize_optional_int(item.get("max_prompt_tokens")),
                "min_completion_tokens": ModelPricingService._normalize_optional_int(item.get("min_completion_tokens")),
                "max_completion_tokens": ModelPricingService._normalize_optional_int(item.get("max_completion_tokens")),
                "input_price_per_1k": to_price_decimal(item.get("input_price_per_1k")),
                "output_price_per_1k": to_price_decimal(item.get("output_price_per_1k")),
                "cache_price_per_1k": to_price_decimal(item.get("cache_price_per_1k")),
                "cache_write_price_per_1k": to_price_decimal(item.get("cache_write_price_per_1k")),
                "cache_storage_price_per_1k": to_price_decimal(item.get("cache_storage_price_per_1k")),
                "source_input_price_per_1k": to_price_decimal(item.get("source_input_price_per_1k")),
                "source_output_price_per_1k": to_price_decimal(item.get("source_output_price_per_1k")),
                "source_cache_price_per_1k": to_price_decimal(item.get("source_cache_price_per_1k")),
                "source_cache_write_price_per_1k": to_price_decimal(item.get("source_cache_write_price_per_1k")),
                "source_cache_storage_price_per_1k": to_price_decimal(item.get("source_cache_storage_price_per_1k")),
                "source_note": ModelPricingService._normalize_optional_text(item.get("source_note")),
            }
            if normalized["cache_price_per_1k"] is None and normalized["input_price_per_1k"] is not None:
                normalized["cache_price_per_1k"] = normalized["input_price_per_1k"]
            if normalized["source_cache_price_per_1k"] is None and normalized["source_input_price_per_1k"] is not None:
                normalized["source_cache_price_per_1k"] = normalized["source_input_price_per_1k"]
            tiers.append(normalized)
        tiers.sort(
            key=lambda item: (
                item.get("min_prompt_tokens") if item.get("min_prompt_tokens") is not None else -1,
                item.get("max_prompt_tokens") if item.get("max_prompt_tokens") is not None else 10**18,
                item.get("min_completion_tokens") if item.get("min_completion_tokens") is not None else -1,
                item.get("max_completion_tokens") if item.get("max_completion_tokens") is not None else 10**18,
                item.get("tier_key") or "",
            )
        )
        return tiers

    @staticmethod
    def normalize_fee_components(
        value: Any,
        *,
        source_currency: str | None = None,
        billing_currency: str | None = None,
        pricing_metadata: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        if not isinstance(value, list):
            return []
        default_source_currency = CurrencyService.normalize_currency(source_currency)
        default_billing_currency = CurrencyService.normalize_currency(billing_currency)
        components: list[dict[str, Any]] = []
        for item in value:
            if not isinstance(item, dict):
                continue
            unit = ModelPricingService._normalize_optional_text(item.get("unit"))
            amount = to_price_decimal(item.get("amount"))
            source_amount = to_price_decimal(item.get("source_amount"))
            if unit is None or (amount is None and source_amount is None):
                continue
            amount_currency = CurrencyService.normalize_currency(item.get("currency"), default=default_billing_currency)
            component_source_currency = CurrencyService.normalize_currency(
                item.get("source_currency") or item.get("currency"),
                default=default_source_currency,
            )
            if source_amount is None:
                source_amount = amount
                component_source_currency = amount_currency
            if amount is None and source_amount is not None:
                amount, _ = CurrencyService.convert_price(
                    source_amount,
                    source_currency=component_source_currency,
                    billing_currency=default_billing_currency,
                    pricing_metadata=pricing_metadata,
                )
                amount_currency = default_billing_currency
            components.append(
                {
                    "component_key": ModelPricingService._normalize_optional_text(item.get("component_key")) or unit,
                    "unit": unit,
                    "amount": amount,
                    "currency": amount_currency,
                    "source_amount": source_amount,
                    "source_currency": component_source_currency,
                    "note": ModelPricingService._normalize_optional_text(item.get("note")),
                }
            )
        return components

    @staticmethod
    def _pricing_json_has_meaningful_value(normalized: dict[str, Any]) -> bool:
        meaningful_keys = (
            "source_label",
            "source_url",
            "note",
            "updated_at",
            "tiers",
            "fee_components",
            "source_input_price_per_1k",
            "source_output_price_per_1k",
            "source_cache_price_per_1k",
            "source_cache_write_price_per_1k",
            "exchange_rate_to_usd",
            "exchange_rate_to_cny",
            "exchange_rate_source",
            "exchange_rate_at",
            "exchange_rate_version",
        )
        if any(normalized.get(key) for key in meaningful_keys):
            return True
        return normalized.get("source_currency") != normalized.get("billing_currency")

    @staticmethod
    def pricing_json_to_db_value(pricing_json: dict | None) -> str | None:
        normalized = ModelPricingService.normalize_pricing_json(pricing_json)
        has_meaningful_value = ModelPricingService._pricing_json_has_meaningful_value(normalized)
        return dumps_json(normalized) if has_meaningful_value else None

    @staticmethod
    def parse_pricing_json(value: str | dict | None) -> dict[str, Any]:
        if isinstance(value, dict):
            return ModelPricingService.normalize_pricing_json(value)
        return ModelPricingService.normalize_pricing_json(loads_json(value, {}))

    @staticmethod
    def serialize_pricing_json(value: str | dict | None) -> dict[str, Any] | None:
        normalized = ModelPricingService.parse_pricing_json(value)
        if not ModelPricingService._pricing_json_has_meaningful_value(normalized):
            return None
        payload = deepcopy(normalized)
        for tier in payload.get("tiers", []):
            for field in (
                "input_price_per_1k",
                "output_price_per_1k",
                "cache_price_per_1k",
                "cache_write_price_per_1k",
                "cache_storage_price_per_1k",
                "source_input_price_per_1k",
                "source_output_price_per_1k",
                "source_cache_price_per_1k",
                "source_cache_write_price_per_1k",
                "source_cache_storage_price_per_1k",
            ):
                tier[field] = decimal_to_float(tier.get(field))
        for field in (
            "source_input_price_per_1k",
            "source_output_price_per_1k",
            "source_cache_price_per_1k",
            "source_cache_write_price_per_1k",
        ):
            payload[field] = decimal_to_float(payload.get(field))
        for component in payload.get("fee_components", []):
            component["amount"] = decimal_to_float(component.get("amount"))
            component["source_amount"] = decimal_to_float(component.get("source_amount"))
        return payload

    @staticmethod
    def pick_default_tier(tiers: list[dict[str, Any]]) -> dict[str, Any]:
        if not tiers:
            return {}
        for tier in tiers:
            if tier.get("min_prompt_tokens") in (None, 0):
                return tier
        return tiers[0]

    @staticmethod
    def resolve_catalog_pricing(
        *,
        pricing_mode: str | None,
        pricing_json: str | dict | None,
        input_price_per_1k,
        output_price_per_1k,
        cache_price_per_1k,
        cache_write_price_per_1k=None,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> dict[str, Any]:
        normalized_json = ModelPricingService.parse_pricing_json(pricing_json)
        tiers = normalized_json.get("tiers", [])
        normalized_mode = ModelPricingService.normalize_pricing_mode(
            pricing_mode,
            has_tiers=bool(tiers),
            has_base_price=any(
                item is not None
                for item in (
                    to_price_decimal(input_price_per_1k),
                    to_price_decimal(output_price_per_1k),
                    to_price_decimal(cache_price_per_1k),
                    to_price_decimal(cache_write_price_per_1k),
                    normalized_json.get("source_input_price_per_1k"),
                    normalized_json.get("source_output_price_per_1k"),
                    normalized_json.get("source_cache_price_per_1k"),
                    normalized_json.get("source_cache_write_price_per_1k"),
                )
            ),
        )
        tier: dict[str, Any] | None = None
        if normalized_mode == ModelPricingService.PRICING_MODE_TIERED and tiers:
            normalized_prompt_tokens = max(0, int(prompt_tokens or 0))
            normalized_completion_tokens = max(0, int(completion_tokens or 0))
            tier = next(
                (
                    item
                    for item in tiers
                    if (item.get("min_prompt_tokens") is None or normalized_prompt_tokens >= item["min_prompt_tokens"])
                    and (item.get("max_prompt_tokens") is None or normalized_prompt_tokens <= item["max_prompt_tokens"])
                    and (
                        item.get("min_completion_tokens") is None
                        or normalized_completion_tokens >= item["min_completion_tokens"]
                    )
                    and (
                        item.get("max_completion_tokens") is None
                        or normalized_completion_tokens <= item["max_completion_tokens"]
                    )
                ),
                None,
            )
            if tier is None:
                tier = ModelPricingService.pick_default_tier(tiers)
        elif normalized_mode == ModelPricingService.PRICING_MODE_FIXED and tiers:
            tier = ModelPricingService.pick_default_tier(tiers)

        price_item = tier or {
            "input_price_per_1k": input_price_per_1k,
            "output_price_per_1k": output_price_per_1k,
            "cache_price_per_1k": cache_price_per_1k,
            "cache_write_price_per_1k": cache_write_price_per_1k,
            "source_input_price_per_1k": normalized_json.get("source_input_price_per_1k"),
            "source_output_price_per_1k": normalized_json.get("source_output_price_per_1k"),
            "source_cache_price_per_1k": normalized_json.get("source_cache_price_per_1k"),
            "source_cache_write_price_per_1k": normalized_json.get("source_cache_write_price_per_1k"),
        }
        resolved_prices = ModelPricingService._resolve_prices_from_item(price_item, normalized_json)
        resolved_input = resolved_prices["input_price_per_1k"]
        resolved_output = resolved_prices["output_price_per_1k"]
        resolved_cache = resolved_prices["cache_price_per_1k"]
        if resolved_cache is None and resolved_input is not None:
            resolved_cache = resolved_input
        return {
            "pricing_mode": normalized_mode,
            "pricing_json": normalized_json,
            "tier_key": tier.get("tier_key") if tier else None,
            "tier_name": tier.get("tier_name") if tier else None,
            "tier_source_note": tier.get("source_note") if tier else None,
            "input_price_per_1k": resolved_input,
            "output_price_per_1k": resolved_output,
            "cache_price_per_1k": resolved_cache,
            "cache_write_price_per_1k": resolved_prices["cache_write_price_per_1k"],
            "cache_storage_price_per_1k": resolved_prices["cache_storage_price_per_1k"],
            "source_currency": resolved_prices["source_currency"],
            "billing_currency": resolved_prices["billing_currency"],
            "source_input_price_per_1k": resolved_prices["source_input_price_per_1k"],
            "source_output_price_per_1k": resolved_prices["source_output_price_per_1k"],
            "source_cache_price_per_1k": resolved_prices["source_cache_price_per_1k"],
            "source_cache_write_price_per_1k": resolved_prices["source_cache_write_price_per_1k"],
            "exchange_rate_snapshot": resolved_prices["exchange_rate_snapshot"],
            "fee_components": normalized_json.get("fee_components", []),
        }

    @staticmethod
    def resolve_catalog_prices_for_provider(
        *,
        pricing_mode: str | None,
        pricing_json: str | dict | None,
        input_price_per_1k,
        output_price_per_1k,
        cache_price_per_1k,
        cache_write_price_per_1k=None,
        price_multiplier,
        prompt_tokens: int | None = None,
        completion_tokens: int | None = None,
    ) -> dict[str, Any]:
        resolved = ModelPricingService.resolve_catalog_pricing(
            pricing_mode=pricing_mode,
            pricing_json=pricing_json,
            input_price_per_1k=input_price_per_1k,
            output_price_per_1k=output_price_per_1k,
            cache_price_per_1k=cache_price_per_1k,
            cache_write_price_per_1k=cache_write_price_per_1k,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
        return {
            **resolved,
            "input_price_per_1k": multiply_price_and_multiplier(resolved.get("input_price_per_1k"), price_multiplier),
            "output_price_per_1k": multiply_price_and_multiplier(resolved.get("output_price_per_1k"), price_multiplier),
            "cache_price_per_1k": multiply_price_and_multiplier(resolved.get("cache_price_per_1k"), price_multiplier),
            "cache_write_price_per_1k": multiply_price_and_multiplier(
                resolved.get("cache_write_price_per_1k"),
                price_multiplier,
            ),
            "cache_storage_price_per_1k": multiply_price_and_multiplier(
                resolved.get("cache_storage_price_per_1k"),
                price_multiplier,
            ),
            "source_input_price_per_1k": multiply_price_and_multiplier(resolved.get("source_input_price_per_1k"), price_multiplier),
            "source_output_price_per_1k": multiply_price_and_multiplier(resolved.get("source_output_price_per_1k"), price_multiplier),
            "source_cache_price_per_1k": multiply_price_and_multiplier(resolved.get("source_cache_price_per_1k"), price_multiplier),
            "source_cache_write_price_per_1k": multiply_price_and_multiplier(resolved.get("source_cache_write_price_per_1k"), price_multiplier),
            "fee_components": [
                {
                    **component,
                    "amount": multiply_price_and_multiplier(component.get("amount"), price_multiplier),
                    "source_amount": multiply_price_and_multiplier(component.get("source_amount"), price_multiplier),
                }
                for component in resolved.get("fee_components", [])
                if isinstance(component, dict)
            ],
        }

    @staticmethod
    def _resolve_prices_from_item(item: dict[str, Any], pricing_metadata: dict[str, Any]) -> dict[str, Any]:
        source_currency = CurrencyService.normalize_currency(pricing_metadata.get("source_currency"))
        billing_currency = CurrencyService.normalize_currency(pricing_metadata.get("billing_currency"))
        snapshot = CurrencyService.resolve_conversion_snapshot(
            source_currency=source_currency,
            billing_currency=billing_currency,
            pricing_metadata=pricing_metadata,
        )

        def resolve(field: str, source_field: str):
            direct_value = to_price_decimal(item.get(field))
            source_value = to_price_decimal(item.get(source_field))
            if source_value is None:
                source_value = direct_value
            if direct_value is None and source_value is not None:
                direct_value, _ = CurrencyService.convert_price(
                    source_value,
                    source_currency=source_currency,
                    billing_currency=billing_currency,
                    pricing_metadata=pricing_metadata,
                )
            return direct_value, source_value

        input_price, source_input = resolve("input_price_per_1k", "source_input_price_per_1k")
        output_price, source_output = resolve("output_price_per_1k", "source_output_price_per_1k")
        cache_price, source_cache = resolve("cache_price_per_1k", "source_cache_price_per_1k")
        cache_write_price, source_cache_write = resolve("cache_write_price_per_1k", "source_cache_write_price_per_1k")
        cache_storage_price, _source_cache_storage = resolve("cache_storage_price_per_1k", "source_cache_storage_price_per_1k")
        if cache_price is None and input_price is not None:
            cache_price = input_price
        if source_cache is None and source_input is not None:
            source_cache = source_input
        return {
            "input_price_per_1k": input_price,
            "output_price_per_1k": output_price,
            "cache_price_per_1k": cache_price,
            "cache_write_price_per_1k": cache_write_price,
            "cache_storage_price_per_1k": cache_storage_price,
            "source_currency": source_currency,
            "billing_currency": billing_currency,
            "source_input_price_per_1k": source_input,
            "source_output_price_per_1k": source_output,
            "source_cache_price_per_1k": source_cache,
            "source_cache_write_price_per_1k": source_cache_write,
            "exchange_rate_snapshot": snapshot,
        }

    @staticmethod
    def _normalize_optional_text(value: Any) -> str | None:
        if value is None:
            return None
        normalized = str(value).strip()
        return normalized or None

    @staticmethod
    def _normalize_optional_int(value: Any) -> int | None:
        if value in (None, ""):
            return None
        try:
            normalized = int(value)
        except (TypeError, ValueError):
            return None
        return normalized if normalized >= 0 else None

    @staticmethod
    def _normalize_optional_decimal_text(value: Any) -> str | None:
        decimal_value = to_price_decimal(value)
        return format(decimal_value, "f") if decimal_value is not None else None

    @staticmethod
    def _normalize_datetime_value(value: Any) -> str | None:
        if value in (None, ""):
            return None
        if isinstance(value, datetime):
            return value.isoformat()
        normalized = str(value).strip()
        return normalized or None
