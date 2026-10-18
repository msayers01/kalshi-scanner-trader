"""Structural (coherence) arbitrage executor (docs/STRATEGIES.md §2). Detection lives in scanner/signals.py.

Re-validates an opportunity against current books, sizes the basket, and emits simultaneous IOC takes
sharing a basket id. Residual legs after partial fills are left to the normal EV exit logic.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

from kst.config import StructuralArbCfg
from kst.core.fixedpoint import PRICE_SCALE, Qty
from kst.core.types import BookSide
from kst.domain.orderbook import OrderBook
from kst.domain.orders import TakeIntent
from kst.scanner.signals import ArbOpportunity


class StructuralArb:
    name = "structural_arb"

    def __init__(self, cfg: StructuralArbCfg) -> None:
        self.cfg = cfg

    def plan(
        self,
        opp: ArbOpportunity,
        book_of: Callable[[str], OrderBook | None],
        *,
        max_usd: float | None = None,
        max_contracts: float | None = None,
        max_leg_usd: float | None = None,
    ) -> list[TakeIntent]:
        if opp.edge <= self.cfg.margin:
            return []
        qty = int(opp.max_baskets)
        for leg in opp.legs:
            b = book_of(leg.ticker)
            if b is None:
                return []
            # Liquidity still available at (or better than) the leg price?
            avail = b.qty_at_or_better(leg.side.opposite, leg.price)
            qty = min(qty, avail)
        budget = min(self.cfg.max_basket_usd, max_usd if max_usd is not None else self.cfg.max_basket_usd)
        cost = max(opp.cost_per_basket, 1e-6)
        qty = min(qty, int(budget / cost) * 100)
        if max_contracts is not None:
            qty = min(qty, int(max_contracts * 100))
        if max_leg_usd is not None:
            worst_leg = max(self.leg_cost(leg.side, leg.price) for leg in opp.legs)
            qty = min(qty, int(max_leg_usd / max(worst_leg, 1e-6)) * 100)
        qty = (qty // 100) * 100  # whole baskets
        if qty < 100:
            return []
        basket = f"arb-{uuid.uuid4().hex[:8]}"
        return [
            TakeIntent(
                leg.ticker,
                leg.side,
                leg.price,
                Qty(qty),
                f"arb:{opp.kind}",
                basket_id=basket,
                expected_edge=opp.edge / len(opp.legs),
            )
            for leg in opp.legs
        ]

    @staticmethod
    def leg_cost(side: BookSide, price: int) -> float:
        return (price if side is BookSide.BID else PRICE_SCALE - price) / PRICE_SCALE
