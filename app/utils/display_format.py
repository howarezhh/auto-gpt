from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from fastapi.templating import Jinja2Templates


def _to_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _trim_decimal(value: Decimal, *, max_decimals: int = 9, min_decimals: int = 0) -> str:
    quantized = value.quantize(Decimal(1).scaleb(-max_decimals))
    text = format(quantized, "f")
    if "." in text and max_decimals > min_decimals:
        text = text.rstrip("0").rstrip(".")
    if min_decimals > 0:
        integer, _, fraction = text.partition(".")
        text = f"{integer}.{fraction.ljust(min_decimals, '0')}"
    return text


def _currency_symbol(currency: str | None) -> str:
    normalized = str(currency or "USD").strip().upper() or "USD"
    return {"USD": "$", "CNY": "¥"}.get(normalized, normalized)


def display_money(value: Any, none_label: str = "不限", currency: str = "USD") -> str:
    numeric = _to_decimal(value)
    if numeric is None:
        return none_label
    return f"{_trim_decimal(numeric, max_decimals=9)} {_currency_symbol(currency)}"


def display_tokens(value: Any, none_label: str = "-") -> str:
    numeric = _to_decimal(value)
    if numeric is None:
        return none_label
    token_value = max(Decimal(0), numeric)
    if token_value < Decimal(1000):
        return f"{int(token_value.to_integral_value())} token"
    if token_value < Decimal(1000000):
        return f"{(token_value / Decimal(1000)).quantize(Decimal('0.01'))}k"
    return f"{(token_value / Decimal(1000000)).quantize(Decimal('0.01'))}m"


def display_price_per_1m_from_per_1k(value: Any, none_label: str = "-", currency: str = "USD") -> str:
    numeric = _to_decimal(value)
    if numeric is None:
        return none_label
    return f"{_trim_decimal(numeric * Decimal(1000), max_decimals=9)} {_currency_symbol(currency)}/1M"


def register_display_filters(templates: Jinja2Templates) -> None:
    templates.env.filters["display_money"] = display_money
    templates.env.filters["display_tokens"] = display_tokens
    templates.env.filters["display_price_per_1m"] = display_price_per_1m_from_per_1k
