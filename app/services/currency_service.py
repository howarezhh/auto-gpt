from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

from app.utils.decimal_utils import PRICE_QUANT, to_price_decimal


DEFAULT_ACCOUNT_CURRENCY = "USD"
DEFAULT_ROUNDING_STRATEGY = "ROUND_HALF_UP"


@dataclass(frozen=True, slots=True)
class CurrencyConversionSnapshot:
    source_currency: str
    billing_currency: str
    exchange_rate: Decimal
    exchange_rate_source: str
    exchange_rate_at: str | None
    exchange_rate_version: str
    rounding_strategy: str = DEFAULT_ROUNDING_STRATEGY


class CurrencyService:
    """Resolve deterministic currency conversion snapshots for billing."""

    DEFAULT_EXCHANGE_RATES: dict[tuple[str, str], dict[str, str]] = {
        ("CNY", "USD"): {
            "rate": "0.1471713577725329730343978813",
            "source": "official_pricing_import_snapshot",
            "rate_at": "2026-06-01T00:00:00+08:00",
            "version": "official_pricing_2026_06_01_usd_cny_6_7948",
        },
        ("USD", "CNY"): {
            "rate": "6.7948",
            "source": "official_pricing_import_snapshot",
            "rate_at": "2026-06-01T00:00:00+08:00",
            "version": "official_pricing_2026_06_01_usd_cny_6_7948",
        },
    }

    @staticmethod
    def normalize_currency(value: str | None, *, default: str = DEFAULT_ACCOUNT_CURRENCY) -> str:
        normalized = str(value or default).strip().upper()
        return normalized or default

    @staticmethod
    def currency_symbol(currency: str | None) -> str:
        normalized = CurrencyService.normalize_currency(currency)
        return {"USD": "$", "CNY": "¥"}.get(normalized, normalized)

    @staticmethod
    def resolve_conversion_snapshot(
        *,
        source_currency: str | None,
        billing_currency: str | None,
        pricing_metadata: dict[str, Any] | None = None,
    ) -> CurrencyConversionSnapshot:
        source = CurrencyService.normalize_currency(source_currency)
        billing = CurrencyService.normalize_currency(billing_currency)
        metadata = pricing_metadata if isinstance(pricing_metadata, dict) else {}
        rounding_strategy = str(metadata.get("rounding_strategy") or DEFAULT_ROUNDING_STRATEGY)
        if source == billing:
            return CurrencyConversionSnapshot(
                source_currency=source,
                billing_currency=billing,
                exchange_rate=Decimal("1"),
                exchange_rate_source="same_currency",
                exchange_rate_at=metadata.get("updated_at"),
                exchange_rate_version="same_currency",
                rounding_strategy=rounding_strategy,
            )

        target_key = f"exchange_rate_to_{billing.lower()}"
        rate_value = metadata.get(target_key) or metadata.get("exchange_rate")
        source_value = metadata.get("exchange_rate_source")
        at_value = metadata.get("exchange_rate_at")
        version_value = metadata.get("exchange_rate_version")
        if rate_value in (None, ""):
            fallback = CurrencyService.DEFAULT_EXCHANGE_RATES.get((source, billing))
            if fallback is None:
                raise ValueError(f"缺少 {source}->{billing} 汇率快照，无法精确计费")
            rate_value = fallback["rate"]
            source_value = source_value or fallback["source"]
            at_value = at_value or fallback["rate_at"]
            version_value = version_value or fallback["version"]

        rate = Decimal(str(rate_value))
        if rate <= 0:
            raise ValueError(f"无效 {source}->{billing} 汇率快照")
        return CurrencyConversionSnapshot(
            source_currency=source,
            billing_currency=billing,
            exchange_rate=rate,
            exchange_rate_source=str(source_value or "pricing_snapshot"),
            exchange_rate_at=str(at_value) if at_value not in (None, "") else None,
            exchange_rate_version=str(version_value or "pricing_snapshot"),
            rounding_strategy=rounding_strategy,
        )

    @staticmethod
    def convert_price(
        value,
        *,
        source_currency: str | None,
        billing_currency: str | None,
        pricing_metadata: dict[str, Any] | None = None,
    ) -> tuple[Decimal | None, CurrencyConversionSnapshot]:
        snapshot = CurrencyService.resolve_conversion_snapshot(
            source_currency=source_currency,
            billing_currency=billing_currency,
            pricing_metadata=pricing_metadata,
        )
        price = to_price_decimal(value)
        if price is None:
            return None, snapshot
        return (price * snapshot.exchange_rate).quantize(PRICE_QUANT, rounding=ROUND_HALF_UP), snapshot
