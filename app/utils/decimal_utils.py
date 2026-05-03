from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP


DB_MONEY_PRECISION = 24
DB_MONEY_SCALE = 9
DB_PRICE_PRECISION = 24
DB_PRICE_SCALE = 12
DB_MULTIPLIER_PRECISION = 24
DB_MULTIPLIER_SCALE = 12

MONEY_QUANT = Decimal("0.000000001")
PRICE_QUANT = Decimal("0.000000000001")
MULTIPLIER_QUANT = Decimal("0.000000000001")
MICRO_COST_SCALE = 10 ** DB_MONEY_SCALE


def to_decimal(value, *, quant: Decimal | None = None) -> Decimal | None:
    if value is None:
        return None
    decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    if quant is not None:
        return decimal_value.quantize(quant, rounding=ROUND_HALF_UP)
    return decimal_value


def to_money_decimal(value) -> Decimal:
    return to_decimal(value, quant=MONEY_QUANT) or Decimal("0").quantize(MONEY_QUANT)


def to_price_decimal(value) -> Decimal | None:
    return to_decimal(value, quant=PRICE_QUANT)


def to_multiplier_decimal(value) -> Decimal:
    decimal_value = to_decimal(value, quant=MULTIPLIER_QUANT)
    if decimal_value is None or decimal_value <= 0:
        return Decimal("1").quantize(MULTIPLIER_QUANT)
    return decimal_value


def quantize_money(value) -> Decimal | None:
    return to_decimal(value, quant=MONEY_QUANT)


def quantize_price(value) -> Decimal | None:
    return to_decimal(value, quant=PRICE_QUANT)


def quantize_multiplier(value) -> Decimal | None:
    return to_decimal(value, quant=MULTIPLIER_QUANT)


def decimal_to_float(value) -> float | None:
    if value is None:
        return None
    return float(value if isinstance(value, Decimal) else Decimal(str(value)))


def multiply_price_and_multiplier(
    price_value,
    multiplier_value,
) -> Decimal | None:
    price_decimal = to_price_decimal(price_value)
    if price_decimal is None:
        return None
    multiplier_decimal = to_multiplier_decimal(multiplier_value)
    return (price_decimal * multiplier_decimal).quantize(PRICE_QUANT, rounding=ROUND_HALF_UP)


def divide_price_by_multiplier(
    price_value,
    multiplier_value,
    *,
    fallback: Decimal | None = None,
) -> Decimal:
    price_decimal = to_price_decimal(price_value)
    multiplier_decimal = to_multiplier_decimal(multiplier_value)
    if price_decimal is None or multiplier_decimal <= 0:
        return fallback if fallback is not None else Decimal("1").quantize(MULTIPLIER_QUANT)
    derived = (price_decimal / multiplier_decimal).quantize(MULTIPLIER_QUANT, rounding=ROUND_HALF_UP)
    minimum = Decimal("0.000001").quantize(MULTIPLIER_QUANT)
    return derived if derived >= minimum else minimum


def decimals_equal(left, right, *, quant: Decimal) -> bool:
    left_decimal = to_decimal(left, quant=quant)
    right_decimal = to_decimal(right, quant=quant)
    return left_decimal == right_decimal


def money_to_scaled_int(value) -> int:
    money_decimal = to_money_decimal(value)
    return int((money_decimal * MICRO_COST_SCALE).to_integral_value(rounding=ROUND_HALF_UP))


def scaled_int_to_money_decimal(value: int | str | None) -> Decimal:
    if value in (None, ""):
        return Decimal("0").quantize(MONEY_QUANT)
    return (Decimal(int(value)) / Decimal(MICRO_COST_SCALE)).quantize(MONEY_QUANT, rounding=ROUND_HALF_UP)
