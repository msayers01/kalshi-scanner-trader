"""Risk limits. Defaults from config; editable at runtime via ControlService (persisted + audited)."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class RiskLimits(BaseModel):
    """All USD amounts are dollars (floats are fine here: they're thresholds, not ledgers)."""

    model_config = ConfigDict(validate_assignment=True, extra="forbid")

    # --- entry throttles
    max_markets_per_day: int = Field(10, ge=0, description="Distinct markets newly entered per trading day.")
    max_open_positions: int = Field(15, ge=0)
    max_orders_per_minute: int = Field(60, ge=1)

    # --- size / exposure (exposure = worst-case loss = cost basis incl. resting buy orders)
    max_order_contracts: float = Field(50, gt=0)
    max_order_usd: float = Field(50, gt=0)
    max_position_contracts: float = Field(100, gt=0)
    max_exposure_per_market_usd: float = Field(50, gt=0)
    max_exposure_per_event_usd: float = Field(100, gt=0)
    max_exposure_per_category_usd: float = Field(250, gt=0)
    max_total_exposure_usd: float = Field(500, gt=0)
    max_total_exposure_pct: float = Field(0.5, gt=0, le=1.0, description="Fraction of equity.")

    # --- price sanity
    min_entry_price: float = Field(0.03, ge=0, le=1)
    max_entry_price: float = Field(0.97, ge=0, le=1)
    max_price_deviation: float = Field(0.10, gt=0, le=1, description="Fat-finger band vs fair/mid.")

    # --- timing / data
    min_seconds_to_close: int = Field(120, ge=0)
    stale_ws_s: float = Field(5.0, gt=0)
    stale_poll_s: float = Field(20.0, gt=0)

    # --- breakers
    daily_loss_limit_usd: float = Field(50, gt=0)
    max_drawdown_usd: float = Field(100, gt=0)
    max_consecutive_losses: int = Field(5, ge=1)

    def patch(self, changes: dict[str, Any]) -> tuple[RiskLimits, dict[str, Any]]:
        """Return a validated copy with `changes` applied and the effective diff."""
        data = self.model_dump()
        unknown = set(changes) - set(data)
        if unknown:
            raise ValueError(f"unknown risk limit(s): {sorted(unknown)}")
        data.update(changes)
        new = RiskLimits.model_validate(data)
        diff = {k: getattr(new, k) for k in changes if getattr(new, k) != getattr(self, k)}
        return new, diff

    @classmethod
    def describe(cls) -> dict[str, str]:
        return {name: (f.description or "") for name, f in cls.model_fields.items()}
