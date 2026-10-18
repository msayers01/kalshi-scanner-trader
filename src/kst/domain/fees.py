"""Kalshi trading fees (docs/STRATEGIES.md §0, docs/KALSHI_API_NOTES.md#fees).

fee = coef × C × P × (1 − P), rounded *up* to $0.000001 (exchange then aligns balances to $0.0001 for
direct members; we add `rounding_allowance` per fill in EV math to stay conservative).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal

from kst.core.fixedpoint import MICROS_PER_DOLLAR, PRICE_SCALE, QTY_SCALE, Micros

_MAKER_FEE_TYPES = {"quadratic_with_maker_fees", "quadratic_with_combo_maker_fees"}


@dataclass(frozen=True, slots=True)
class FeeSchedule:
    """Fee parameters for one market (from its series + market fields)."""

    taker_coef: float  # $ per (contract × P(1−P))
    maker_coef: float

    def fee_micros(self, price: int, qty: int, *, is_taker: bool) -> Micros:
        """Exact-ish fee for a fill: price in Price units, qty in Qty units."""
        coef = self.taker_coef if is_taker else self.maker_coef
        if coef <= 0 or qty <= 0:
            return Micros(0)
        # coef × (qty/100) × (p/1e4) × (1 − p/1e4) dollars → micros, ceil. Decimal keeps it exact.
        num = Decimal(repr(coef)) * qty * price * (PRICE_SCALE - price) * MICROS_PER_DOLLAR
        den = QTY_SCALE * PRICE_SCALE * PRICE_SCALE
        return Micros(math.ceil(num / den))

    def per_contract(self, prob: float, *, is_taker: bool) -> float:
        """Continuous fee per contract in dollars at probability-price `prob` (for EV math)."""
        coef = self.taker_coef if is_taker else self.maker_coef
        return coef * prob * (1.0 - prob)


ZERO_FEES = FeeSchedule(0.0, 0.0)


class FeeModel:
    def __init__(self, taker_coef: float = 0.07, maker_ratio: float = 0.25, combo_maker_ratio: float = 0.5) -> None:
        self.taker_coef = taker_coef
        self.maker_ratio = maker_ratio
        self.combo_maker_ratio = combo_maker_ratio

    def schedule(
        self, fee_type: str, fee_multiplier: float = 1.0, *, waived: bool = False
    ) -> FeeSchedule:
        if waived:
            return ZERO_FEES
        taker = self.taker_coef * fee_multiplier
        ft = (fee_type or "quadratic").lower()
        if ft == "quadratic":
            maker = 0.0
        elif ft == "quadratic_with_maker_fees":
            maker = taker * self.maker_ratio
        elif ft == "quadratic_with_combo_maker_fees":
            maker = taker * self.combo_maker_ratio
        else:
            # 'flat' and unknown types: not modelled precisely → assume taker-level fees both ways
            # (conservative: understating fees is how backtests lie).
            maker = taker
        return FeeSchedule(taker_coef=taker, maker_coef=maker)
