"""L2 order book in YES-price space.

bids: YES bids (price → qty).  asks: YES asks (price → qty) — i.e. NO bids converted to YES prices.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from kst.core.fixedpoint import PRICE_SCALE, Micros, Price, Qty
from kst.core.types import BookSide


@dataclass(frozen=True, slots=True)
class SweepResult:
    filled: Qty
    cost: Micros  # Σ price × qty (YES leg)
    worst_price: Price | None
    levels: tuple[tuple[Price, Qty], ...]

    @property
    def avg_price(self) -> float | None:
        return None if self.filled == 0 else self.cost / self.filled / PRICE_SCALE


class OrderBook:
    __slots__ = ("_best_ask", "_best_bid", "asks", "bids", "seq", "ticker", "ts_ms")

    def __init__(self, ticker: str) -> None:
        self.ticker = ticker
        self.bids: dict[int, int] = {}
        self.asks: dict[int, int] = {}
        self.ts_ms = 0
        self.seq = 0
        self._best_bid: int | None = None
        self._best_ask: int | None = None

    # ------------------------------------------------------------------ mutation

    def apply_snapshot(
        self, bids: Iterable[tuple[int, int]], asks: Iterable[tuple[int, int]], ts_ms: int, seq: int = 0
    ) -> None:
        self.bids = {p: q for p, q in bids if q > 0}
        self.asks = {p: q for p, q in asks if q > 0}
        self.ts_ms = ts_ms
        self.seq = seq
        self._recompute()

    def apply_delta(self, side: BookSide, price: int, delta: int, ts_ms: int, seq: int = 0) -> None:
        levels = self.bids if side is BookSide.BID else self.asks
        new = levels.get(price, 0) + delta
        if new > 0:
            levels[price] = new
            if side is BookSide.BID:
                if self._best_bid is None or price > self._best_bid:
                    self._best_bid = price
            elif self._best_ask is None or price < self._best_ask:
                self._best_ask = price
        else:
            levels.pop(price, None)
            if side is BookSide.BID and price == self._best_bid:
                self._best_bid = max(self.bids) if self.bids else None
            elif side is BookSide.ASK and price == self._best_ask:
                self._best_ask = min(self.asks) if self.asks else None
        self.ts_ms = ts_ms
        if seq:
            self.seq = seq

    def _recompute(self) -> None:
        self._best_bid = max(self.bids) if self.bids else None
        self._best_ask = min(self.asks) if self.asks else None

    def clear(self) -> None:
        self.apply_snapshot((), (), self.ts_ms)

    # ------------------------------------------------------------------ queries

    @property
    def best_bid(self) -> Price | None:
        return None if self._best_bid is None else Price(self._best_bid)

    @property
    def best_ask(self) -> Price | None:
        return None if self._best_ask is None else Price(self._best_ask)

    @property
    def best_bid_qty(self) -> Qty:
        return Qty(self.bids.get(self._best_bid, 0)) if self._best_bid is not None else Qty(0)

    @property
    def best_ask_qty(self) -> Qty:
        return Qty(self.asks.get(self._best_ask, 0)) if self._best_ask is not None else Qty(0)

    @property
    def is_two_sided(self) -> bool:
        return self._best_bid is not None and self._best_ask is not None

    @property
    def is_crossed(self) -> bool:
        return self.is_two_sided and self._best_bid >= self._best_ask  # type: ignore[operator]

    def mid(self) -> float | None:
        if not self.is_two_sided:
            return None
        return (self._best_bid + self._best_ask) / (2 * PRICE_SCALE)  # type: ignore[operator]

    def spread(self) -> int | None:
        if not self.is_two_sided:
            return None
        return self._best_ask - self._best_bid  # type: ignore[operator]

    def microprice(self) -> float | None:
        """Size-weighted mid: leans toward the side with less resting size (it's about to be taken)."""
        if not self.is_two_sided:
            return None
        qb, qa = self.best_bid_qty, self.best_ask_qty
        if qb + qa == 0:
            return self.mid()
        return (self._best_bid * qa + self._best_ask * qb) / ((qa + qb) * PRICE_SCALE)  # type: ignore[operator]

    def depth(self, side: BookSide, levels: int = 10) -> list[tuple[Price, Qty]]:
        """Best-first levels for one side."""
        book = self.bids if side is BookSide.BID else self.asks
        prices = sorted(book, reverse=(side is BookSide.BID))[:levels]
        return [(Price(p), Qty(book[p])) for p in prices]

    def qty_at_or_better(self, side: BookSide, limit: int) -> Qty:
        """Liquidity on `side` at prices at least as good (for the taker) as `limit`."""
        if side is BookSide.ASK:
            return Qty(sum(q for p, q in self.asks.items() if p <= limit))
        return Qty(sum(q for p, q in self.bids.items() if p >= limit))

    def imbalance(self, levels: int = 3) -> float | None:
        """(bid_qty − ask_qty)/(bid_qty + ask_qty) over the top `levels`; ∈ [−1, 1]."""
        b = sum(q for _, q in self.depth(BookSide.BID, levels))
        a = sum(q for _, q in self.depth(BookSide.ASK, levels))
        return None if a + b == 0 else (b - a) / (a + b)

    def sweep(self, taker_side: BookSide, qty: int, limit: int | None = None) -> SweepResult:
        """Simulate a taker order. taker_side=BID buys YES (consumes asks); ASK sells YES (consumes bids)."""
        if taker_side is BookSide.BID:
            book, ordered = self.asks, sorted(self.asks)
            ok = (lambda p: limit is None or p <= limit)
        else:
            book, ordered = self.bids, sorted(self.bids, reverse=True)
            ok = (lambda p: limit is None or p >= limit)
        remaining, cost, filled, worst = qty, 0, 0, None
        used: list[tuple[Price, Qty]] = []
        for p in ordered:
            if remaining <= 0 or not ok(p):
                break
            take = min(remaining, book[p])
            remaining -= take
            filled += take
            cost += p * take
            worst = p
            used.append((Price(p), Qty(take)))
        return SweepResult(Qty(filled), Micros(cost), None if worst is None else Price(worst), tuple(used))

    def age_ms(self, now_ms: int) -> int:
        return now_ms - self.ts_ms if self.ts_ms else 1 << 62
