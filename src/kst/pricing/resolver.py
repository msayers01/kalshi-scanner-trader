"""Maps a market to the fair-value model that prices it (first match wins, in priority order)."""

from __future__ import annotations

from kst.domain.market import Market
from kst.pricing.base import FairValue, FairValueModel


class ModelResolver:
    def __init__(self, models: list[FairValueModel], prior: FairValueModel | None = None) -> None:
        self.models = models  # quant models (crypto, weather, ...): "modeled" markets
        self.prior = prior  # calibrated prior: weaker, market-based fallback

    def quant_model(self, market: Market) -> FairValueModel | None:
        for m in self.models:
            if m.applies(market):
                return m
        return None

    def has_quant_model(self, market: Market) -> bool:
        return self.quant_model(market) is not None

    def fair_value(self, market: Market, now_ms: int, *, allow_prior: bool = True) -> FairValue | None:
        m = self.quant_model(market)
        if m is not None:
            return m.fair_value(market, now_ms)
        if allow_prior and self.prior is not None and self.prior.applies(market):
            return self.prior.fair_value(market, now_ms)
        return None
