"""Bankroll presets: one-click, hands-off configurations scaled to starting cash.

Every tier uses the same proportions (chosen so the $1,000 tier reproduces config/default.yaml exactly):

  per order / per market   5 % of bankroll      daily loss → PAUSE   5 %
  per event               10 %                  drawdown   → KILL   10 %
  per category            25 %                  total exposure      50 % (and ≤ 50 % of equity)
  arb basket               5 %

Only the entry throttles (markets per day, open positions) are hand-tuned per tier: small accounts spread
thin get eaten by fees, large ones need more names to deploy capital.

"Hands-off" means a preset also turns on:
  * engine.autotrade      — strategies place orders as soon as kst starts;
  * engine.auto_resume    — a breaker PAUSE (daily loss, losing streak, reject storm) resumes by itself at the
                            next trading day. A KILL (drawdown) still needs a human: that is deliberate;
  * llm.fallback = allow  — if the LLM is unreachable or out of budget, entries proceed on the quant signal
                            alone (risk limits still apply). With a reachable LLM the gate works as configured.

Paper is still the default mode and live still needs --i-understand-live-trading; presets never change mode.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kst.risk.limits import RiskLimits

if TYPE_CHECKING:
    from kst.config import Settings


@dataclass(frozen=True, slots=True)
class Preset:
    key: str
    label: str
    bankroll_usd: float
    markets_per_day: int
    open_positions: int
    summary: str

    def limits(self) -> dict[str, Any]:
        b = self.bankroll_usd
        return {
            "max_markets_per_day": self.markets_per_day,
            "max_open_positions": self.open_positions,
            "max_order_contracts": _r(0.05 * b),
            "max_order_usd": _r(0.05 * b),
            "max_position_contracts": _r(0.10 * b),
            "max_exposure_per_market_usd": _r(0.05 * b),
            "max_exposure_per_event_usd": _r(0.10 * b),
            "max_exposure_per_category_usd": _r(0.25 * b),
            "max_total_exposure_usd": _r(0.50 * b),
            "max_total_exposure_pct": 0.5,
            "daily_loss_limit_usd": _r(0.05 * b),
            "max_drawdown_usd": _r(0.10 * b),
        }

    @property
    def arb_basket_usd(self) -> float:
        return _r(0.05 * self.bankroll_usd)

    @property
    def cash_buffer_usd(self) -> float:
        return max(1.0, _r(0.005 * self.bankroll_usd))

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "bankroll_usd": self.bankroll_usd,
            "summary": self.summary,
            "limits": self.limits(),
            "arb_basket_usd": self.arb_basket_usd,
            "hands_off": {"autotrade": True, "auto_resume": True, "llm_fallback": "allow"},
        }


def _r(x: float) -> float:
    """Round to 2 significant-ish places a human would pick ($5, $25, $1,250 …)."""
    return float(round(x)) if x >= 10 else round(x, 2)


PRESETS: dict[str, Preset] = {
    p.key: p
    for p in (
        Preset(
            "micro", "Micro", 100, 6, 8, "Learning or testing with $100. $5 orders, stops for the day at −$5."
        ),
        Preset("small", "Small", 500, 8, 12, "$500 account. $25 orders, pauses at −$25 for the day."),
        Preset("starter", "Starter", 1_000, 10, 15, "$1,000 account: the shipped defaults, now hands-off."),
        Preset(
            "standard",
            "Standard",
            5_000,
            15,
            20,
            "$5,000 account. $250 orders across up to 15 new markets a day.",
        ),
        Preset(
            "large",
            "Large",
            25_000,
            20,
            30,
            "$25,000 account. $1,250 orders: many Kalshi books are thinner than that, so expect partial fills.",
        ),
        Preset(
            "pro",
            "Pro",
            100_000,
            25,
            40,
            "$100,000 account. $5,000 orders exceed most books' depth; limits act as ceilings, not targets.",
        ),
    )
}


def get_preset(key: str) -> Preset:
    p = PRESETS.get(key.strip().lower())
    if p is None:
        raise ValueError(f"unknown preset {key!r}; choose one of {', '.join(PRESETS)}")
    return p


def apply_preset(settings: Settings, key: str, *, limits: bool = True, paper_balance: bool = True) -> Preset:
    """Mutate `settings` in place for preset `key` (the running app shares these config objects, so a live
    gate / arb strategy picks the change up immediately). `limits=False` when the caller applies the risk
    limits itself (ControlService does, to persist + audit them)."""
    p = get_preset(key)
    if limits:
        settings.risk.limits = RiskLimits.model_validate({**settings.risk.limits.model_dump(), **p.limits()})
    settings.strategies.structural_arb.max_basket_usd = p.arb_basket_usd
    settings.risk.cash_buffer_usd = p.cash_buffer_usd
    settings.engine.autotrade = True
    settings.engine.auto_resume = True
    settings.llm.fallback = "allow"
    if paper_balance:
        settings.paper.starting_balance_usd = p.bankroll_usd
    settings.preset = p.key
    return p
