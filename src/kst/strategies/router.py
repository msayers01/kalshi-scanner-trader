"""Maps a market to the strategy that trades it (docs/STRATEGIES.md §8). "No model, no trade" by default."""

from __future__ import annotations

from kst.config import MarketMakerCfg
from kst.domain.market import Market
from kst.pricing.resolver import ModelResolver
from kst.strategies.base import Strategy


class StrategyRouter:
    def __init__(
        self,
        resolver: ModelResolver,
        edge_taker: Strategy | None,
        market_maker: Strategy | None,
        mm_cfg: MarketMakerCfg,
    ) -> None:
        self.resolver = resolver
        self.edge_taker = edge_taker
        self.market_maker = market_maker
        self.mm_cfg = mm_cfg
        self.by_name: dict[str, Strategy] = {s.name: s for s in (edge_taker, market_maker) if s is not None}

    def route(self, market: Market) -> Strategy | None:
        mm_on = self.market_maker is not None and self.mm_cfg.enabled
        if mm_on and market.series_ticker in self.mm_cfg.series:
            return self.market_maker  # explicitly market-made series (quotes around q̂ or microprice)
        if self.resolver.has_quant_model(market):
            return self.edge_taker
        prior = self.resolver.prior
        if mm_on and self.mm_cfg.use_calibrated_prior and prior is not None and prior.applies(market):
            return self.market_maker  # unmodeled market, fitted favorite-longshot prior, maker-only
        return None

    def get(self, name: str) -> Strategy | None:
        return self.by_name.get(name)
