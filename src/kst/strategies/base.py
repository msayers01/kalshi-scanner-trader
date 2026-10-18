"""Strategy interface. Strategies are pure: (context) → Decision. No I/O, no clocks, no adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from kst.core.fixedpoint import PRICE_SCALE, Price
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

    def top(self, side: BookSide) -> tuple[Price | None, int]:
        """(best price, Qty there) on `side` excluding our own resting quotes."""
        if side is BookSide.BID:
            best, best_qty = self.book.best_bid, self.book.best_bid_qty
        else:
            best, best_qty = self.book.best_ask, self.book.best_ask_qty
        if not self.own_in_book or best is None:
            return best, best_qty
        own: dict[int, int] = {}
        for o in self.open_orders:
            if o.side is side and o.remaining > 0:
                own[o.price] = own.get(o.price, 0) + o.remaining
        if not own:
            return best, best_qty
        for px, qty in self.book.depth(side, 25):
            if (left := qty - own.get(px, 0)) > 0:
                return Price(px), left
        return None, 0

    def touch(self, side: BookSide) -> Price | None:
        """Best price on `side` excluding our own resting quotes, so a quote never chases itself."""
        return self.top(side)[0]

    def mid(self) -> float | None:
        """Book mid excluding our own quotes (live books contain them)."""
        bb, ba = self.touch(BookSide.BID), self.touch(BookSide.ASK)
        return None if bb is None or ba is None else (bb + ba) / (2 * PRICE_SCALE)

    def microprice(self) -> float | None:
        """`OrderBook.microprice` excluding our own quotes."""
        (bb, qb), (ba, qa) = self.top(BookSide.BID), self.top(BookSide.ASK)
        if bb is None or ba is None:
            return None
        if qb + qa == 0:
            return (bb + ba) / (2 * PRICE_SCALE)
        return (bb * qa + ba * qb) / ((qa + qb) * PRICE_SCALE)

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
