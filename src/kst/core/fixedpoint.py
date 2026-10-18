"""Integer fixed-point money and quantities (see ADR 0002).

Units:
    Price  — $0.0001   (10_000 == $1.00)       Kalshi `*_dollars` fields have ≤ 4 dp.
    Qty    — 0.01 contract (100 == 1 contract)  Kalshi `*_fp` fields have 2 dp.
    Micros — $0.000001                           Price × Qty == Micros exactly.

Floats are only produced for display and probability math (`price_to_prob`).
"""

from __future__ import annotations

from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import NewType

Price = NewType("Price", int)
Qty = NewType("Qty", int)
Micros = NewType("Micros", int)

PRICE_SCALE = 10_000
QTY_SCALE = 100
MICROS_PER_DOLLAR = 1_000_000
ONE_DOLLAR = Price(PRICE_SCALE)
ONE_CONTRACT = Qty(QTY_SCALE)

_D_PRICE = Decimal(PRICE_SCALE)
_D_QTY = Decimal(QTY_SCALE)
_D_MICROS = Decimal(MICROS_PER_DOLLAR)


class FixedPointError(ValueError):
    """Raised when a string cannot be represented exactly at the required precision."""


def _to_decimal(s: str | int | float | Decimal) -> Decimal:
    try:
        return s if isinstance(s, Decimal) else Decimal(str(s).strip())
    except (InvalidOperation, ValueError) as exc:
        raise FixedPointError(f"not a number: {s!r}") from exc


def _exact(d: Decimal, scale: Decimal, what: str) -> int:
    scaled = d * scale
    if scaled != scaled.to_integral_value():
        raise FixedPointError(f"{what} {d} has more precision than 1/{scale}")
    return int(scaled)


# ---------------------------------------------------------------- Price


def price_from_str(s: str | int | float | Decimal) -> Price:
    """'0.5600' → Price(5600). Raises if more than 4 dp of precision."""
    return Price(_exact(_to_decimal(s), _D_PRICE, "price"))


def price_from_cents(cents: int) -> Price:
    return Price(cents * 100)


def price_to_str(p: int) -> str:
    """Price(5600) → '0.5600' (Kalshi request format, 4 dp)."""
    return f"{Decimal(p) / _D_PRICE:.4f}"


def price_to_prob(p: int) -> float:
    return p / PRICE_SCALE


def prob_to_price_floor(x: float) -> Price:
    """Largest Price ≤ x (dollars). Use PriceGrid.snap_down for order prices."""
    return Price(int(Decimal(repr(x)).scaleb(4).to_integral_value(rounding=ROUND_FLOOR)))


def prob_to_price_ceil(x: float) -> Price:
    return Price(int(Decimal(repr(x)).scaleb(4).to_integral_value(rounding=ROUND_CEILING)))


def complement(p: int) -> Price:
    """YES price → NO price (1 − p)."""
    return Price(PRICE_SCALE - p)


# ---------------------------------------------------------------- Qty


def qty_from_str(s: str | int | float | Decimal) -> Qty:
    """'10.00' → Qty(1000). Raises if more than 2 dp of precision."""
    return Qty(_exact(_to_decimal(s), _D_QTY, "qty"))


def qty_from_contracts(n: int | float) -> Qty:
    return qty_from_str(n)


def qty_to_str(q: int) -> str:
    """Qty(1000) → '10.00' (Kalshi request format)."""
    return f"{Decimal(q) / _D_QTY:.2f}"


def qty_to_float(q: int) -> float:
    return q / QTY_SCALE


def contracts_floor(x: float) -> Qty:
    """Float contracts → Qty, rounding toward zero (sizing must never round up)."""
    d = Decimal(repr(x)).scaleb(2)
    return Qty(int(d.to_integral_value(rounding=ROUND_FLOOR if d >= 0 else ROUND_CEILING)))


def whole_contracts_floor(q: int) -> Qty:
    """Round a Qty down to whole contracts (toward zero)."""
    sign = -1 if q < 0 else 1
    return Qty(sign * (abs(q) // QTY_SCALE) * QTY_SCALE)


# ---------------------------------------------------------------- Micros


def notional(p: int, q: int) -> Micros:
    """Exact dollar value of q contracts at price p, in micros."""
    return Micros(p * q)


def micros_from_str(s: str | int | float | Decimal) -> Micros:
    """'0.010000' → Micros(10000). Accepts up to 6 dp."""
    return Micros(_exact(_to_decimal(s), _D_MICROS, "amount"))


def micros_from_dollars(x: float) -> Micros:
    return Micros(round(x * MICROS_PER_DOLLAR))


def micros_from_cents(cents: int) -> Micros:
    return Micros(cents * 10_000)


def micros_to_dollars(m: int) -> float:
    return m / MICROS_PER_DOLLAR


def micros_to_str(m: int, dp: int = 2) -> str:
    return f"{Decimal(m) / _D_MICROS:.{dp}f}"


def fmt_usd(m: int) -> str:
    """Micros → '$1,234.56' / '-$0.42' for humans."""
    d = Decimal(m) / _D_MICROS
    sign = "-" if d < 0 else ""
    return f"{sign}${abs(d):,.2f}"


def fmt_cents(p: int | None) -> str:
    """Price → '56¢' or '5.5¢' for compact displays."""
    if p is None:
        return "—"
    c = Decimal(p) / Decimal(100)
    return f"{c.normalize():f}¢"
