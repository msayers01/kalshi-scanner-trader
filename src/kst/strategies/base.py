"""Strategy interface. Strategies are pure: (context) → Decision. No I/O, no clocks, no adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from kst.core.fixedpoint import Price
from kst.core.types import BookSide
from kst.domain.fees import FeeSchedule
from kst.domain.market import Market
from kst.domain.orderbook import OrderBook
from kst.domain.orders import Decision, Order
from kst.domain.positions import Position
from kst.pricing.base import Posterior


@dataclass(slots=True)
class StrategyContext:
    now_ms: int
    market: Market
    book: OrderBook
    fees: FeeSchedule
    posterior: Posterior | None  # q̂ (model ⊕ market); None if unpriced
    position: Position | None  # current position in this market (any strategy)
    open_orders: list[Order] = field(default_factory=list)  # this strategy's live orders in this market
    bankroll_usd: float = 0.0  # equity × bankroll_fraction
    allow_entries: bool = False  # RUNNING + autotrade + gate approved + risk headroom
    side_bias: BookSide | None = None  # optional LLM hint; strategies may only use it to *restrict* entries
    rounding: float = 0.0001  # $ per fill (fee balance-precision allowance)
    max_order_qty: int = 1 << 40  # Qty cap per order (risk.max_order_contracts)
    max_order_usd: float = float("inf")  # $ cost cap per order
    max_position_qty: int = 1 << 40  # Qty cap on |position| in this market
    own_in_book: bool = False  # live/demo books include our resting orders; paper books don't

    def touch(self, side: BookSide) -> Price | None:
        """Best price on `side` excluding our own resting quotes, so a quote never chases itself."""
        best = self.book.best_bid if side is BookSide.BID else self.book.best_ask
        if not self.own_in_book or best is None:
            return best
        own: dict[int, int] = {}
        for o in self.open_orders:
            if o.side is side and o.remaining > 0:
                own[o.price] = own.get(o.price, 0) + o.remaining
        if not own:
            return best
        for px, qty in self.book.depth(side, 25):
            if qty - own.get(px, 0) > 0:
                return Price(px)
        return None

    def clip_qty(self, want: int, cost_per_contract: float) -> int:
        """Clip a desired opening size to the risk caps; whole contracts."""
        room = max(0, self.max_position_qty - abs(self.pos_qty))
        by_usd = want
        if self.max_order_usd != float("inf"):
            by_usd = int(self.max_order_usd / max(cost_per_contract, 1e-6)) * 100
        q = min(want, self.max_order_qty, by_usd, room)
        return (q // 100) * 100

    @property
    def pos_qty(self) -> int:
        return 0 if self.position is None else self.position.qty


class Strategy(ABC):
    name: str = "strategy"

    @abstractmethod
    def evaluate(self, ctx: StrategyContext) -> Decision: ...

    def on_trade(self, ticker: str, price: int, qty: int, taker_side: BookSide, ts_ms: int) -> None:  # noqa: B027
        """Optional hook for strategies that track order flow."""
