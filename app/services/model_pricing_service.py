from __future__ import annotations

from copy import deepcopy
from datetime import datetime
from decimal import Decimal
from typing import Any

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
    ) -> dict[str, Any]:
        normalized_json = ModelPricingService.normalize_pricing_json(pricing_json)
        tiers = normalized_json.get("tiers", [])
        normalized_input = to_price_decimal(input_price_per_1k)
        normalized_output = to_price_decimal(output_price_per_1k)
        normalized_cache = to_price_decimal(cache_price_per_1k)
        has_base_price = any(item is not None for item in (normalized_input, normalized_output, normalized_cache))
        normalized_mode = ModelPricingService.normalize_pricing_mode(
            pricing_mode,
            has_tiers=bool(tiers),
            has_base_price=has_base_price,
        )

        if normalized_mode == ModelPricingService.PRICING_MODE_TIERED:
            if not tiers:
                raise ValueError("阶梯价模式至少需要一档价格")
            default_tier = ModelPricingService.pick_default_tier(tiers)
            normalized_input = to_price_decimal(default_tier.get("input_price_per_1k"))
            normalized_output = to_price_decimal(default_tier.get("output_price_per_1k"))
            normalized_cache = to_price_decimal(
                default_tier.get("cache_price_per_1k", default_tier.get("input_price_per_1k"))
            )
        elif normalized_mode == ModelPricingService.PRICING_MODE_UNPRICED:
            normalized_input = None
            normalized_output = None
            normalized_cache = None
            normalized_json["tiers"] = []
        else:
            if not has_base_price and tiers:
                default_tier = ModelPricingService.pick_default_tier(tiers)
                normalized_input = to_price_decimal(default_tier.get("input_price_per_1k"))
                normalized_output = to_price_decimal(default_tier.get("output_price_per_1k"))
                normalized_cache = to_price_decimal(
                    default_tier.get("cache_price_per_1k", default_tier.get("input_price_per_1k"))
                )
            if normalized_input is None and normalized_output is None and normalized_cache is None:
                normalized_mode = ModelPricingService.PRICING_MODE_UNPRICED
                normalized_json["tiers"] = []
        if normalized_cache is None and normalized_input is not None:
            normalized_cache = normalized_input
        return {
            "pricing_mode": normalized_mode,
            "pricing_json": normalized_json,
            "input_price_per_1k": normalized_input,
            "output_price_per_1k": normalized_output,
            "cache_price_per_1k": normalized_cache,
        }

    @staticmethod
    def normalize_pricing_json(pricing_json: dict | None) -> dict[str, Any]:
        payload = pricing_json if isinstance(pricing_json, dict) else {}
        normalized = {
            "source_label": ModelPricingService._normalize_optional_text(payload.get("source_label")),
            "source_url": ModelPricingService._normalize_optional_text(payload.get("source_url")),
            "note": ModelPricingService._normalize_optional_text(payload.get("note")),
            "updated_at": ModelPricingService._normalize_datetime_value(payload.get("updated_at")),
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
                "source_note": ModelPricingService._normalize_optional_text(item.get("source_note")),
            }
            if normalized["cache_price_per_1k"] is None and normalized["input_price_per_1k"] is not None:
                normalized["cache_price_per_1k"] = normalized["input_price_per_1k"]
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
    def pricing_json_to_db_value(pricing_json: dict | None) -> str | None:
        normalized = ModelPricingService.normalize_pricing_json(pricing_json)
        has_meaningful_value = any(
            normalized.get(key)
            for key in ("source_label", "source_url", "note", "updated_at", "tiers")
        )
        return dumps_json(normalized) if has_meaningful_value else None

    @staticmethod
    def parse_pricing_json(value: str | dict | None) -> dict[str, Any]:
        if isinstance(value, dict):
            return ModelPricingService.normalize_pricing_json(value)
        return ModelPricingService.normalize_pricing_json(loads_json(value, {}))

    @staticmethod
    def serialize_pricing_json(value: str | dict | None) -> dict[str, Any] | None:
        normalized = ModelPricingService.parse_pricing_json(value)
        if not any(normalized.get(key) for key in ("source_label", "source_url", "note", "updated_at", "tiers")):
            return None
        payload = deepcopy(normalized)
        for tier in payload.get("tiers", []):
            for field in (
                "input_price_per_1k",
                "output_price_per_1k",
                "cache_price_per_1k",
                "cache_write_price_per_1k",
                "cache_storage_price_per_1k",
            ):
                tier[field] = decimal_to_float(tier.get(field))
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

        resolved_input = to_price_decimal(tier.get("input_price_per_1k")) if tier else to_price_decimal(input_price_per_1k)
        resolved_output = to_price_decimal(tier.get("output_price_per_1k")) if tier else to_price_decimal(output_price_per_1k)
        resolved_cache = (
            to_price_decimal(tier.get("cache_price_per_1k"))
            if tier
            else to_price_decimal(cache_price_per_1k)
        )
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
            "cache_write_price_per_1k": to_price_decimal(tier.get("cache_write_price_per_1k")) if tier else None,
            "cache_storage_price_per_1k": to_price_decimal(tier.get("cache_storage_price_per_1k")) if tier else None,
        }

    @staticmethod
    def resolve_catalog_prices_for_provider(
        *,
        pricing_mode: str | None,
        pricing_json: str | dict | None,
        input_price_per_1k,
        output_price_per_1k,
        cache_price_per_1k,
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
    def _normalize_datetime_value(value: Any) -> str | None:
        if value in (None, ""):
            return None
        if isinstance(value, datetime):
            return value.isoformat()
        normalized = str(value).strip()
        return normalized or None
