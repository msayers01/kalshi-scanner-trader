"""Fractional Kelly for binary contracts with parameter-uncertainty shrinkage (docs/STRATEGIES.md §1.2)."""

from __future__ import annotations

import math


def kelly_fraction(q: float, cost: float) -> float:
    """Growth-optimal fraction of bankroll for a $1-payoff contract costing `cost` with win prob `q`."""
    if not (0.0 < cost < 1.0) or q <= cost:
        return 0.0
    return (q - cost) / (1.0 - cost)


def uncertainty_shrink(q: float, sd: float, cost: float) -> float:
    """Baker & McHale (2013)-style factor k = e² / (e² + σ²), e = q − cost."""
    e = q - cost
    if e <= 0:
        return 0.0
    return e * e / (e * e + sd * sd)


def kelly_contracts(q: float, sd: float, cost: float, bankroll_usd: float, fraction: float) -> float:
    """Number of contracts (float) to hold; caller floors and applies risk caps."""
    f = fraction * uncertainty_shrink(q, sd, cost) * kelly_fraction(q, cost)
    if f <= 0 or bankroll_usd <= 0:
        return 0.0
    return f * bankroll_usd / cost


def whole_contracts(x: float) -> int:
    """Float contracts → Qty units, whole contracts only (fractional sizing adds fee-rounding noise)."""
    if not math.isfinite(x) or x <= 0:
        return 0
    return math.floor(x) * 100
