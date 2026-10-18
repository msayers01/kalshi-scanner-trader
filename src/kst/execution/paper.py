"""PaperBroker: simulated execution against live (or replayed) books and public trades (ADR 0005).

Taker: walks the opposite side up to the limit (liquidity consumed in this book version is remembered,
so repeated orders can't re-use it). Maker: FIFO with queue-ahead = visible size at our price × haircut
when placed; trades at our price consume the queue, trades through our price fill us, and a book that
moves through our resting price fills us (the aggressor would have hit us first).
"""

from __future__ import annotations

import hashlib
import itertools
from collections.abc import Callable
from dataclasses import dataclass

from kst.core.bus import EventBus
from kst.core.clock import Clock
from kst.core.events import BookUpdated, TradePrinted
from kst.core.fixedpoint import Micros, Price, Qty
from kst.core.logging import get_logger
from kst.core.types import BookSide, OrderStatus, TimeInForce
from kst.domain.orders import Fill, Order
from kst.domain.views import BookView, CatalogView
from kst.execution.broker import BrokerListener, BrokerSnapshot

log = get_logger(__name__)


@dataclass(slots=True)
class _Resting:
    order: Order
    queue_ahead: int


class PaperBroker:
    name = "paper"

    def __init__(
        self, md: BookView, catalog: CatalogView, bus: EventBus, clock: Clock, *, queue_haircut: float = 1.0
    ) -> None:
        self.md = md
        self.catalog = catalog
        self.bus = bus
        self.clock = clock
        self.queue_haircut = queue_haircut
        self._listener: BrokerListener | None = None
        self._resting: dict[str, _Resting] = {}
        self._consumed: dict[tuple[str, BookSide, int], int] = {}
        self._ids = itertools.count(1)
        self._unsubs: list[Callable[[], None]] = []

    def bind(self, listener: BrokerListener) -> None:
        self._listener = listener

    async def start(self) -> None:
        self._unsubs = [
            self.bus.subscribe(TradePrinted, self._on_trade),
            self.bus.subscribe(BookUpdated, self._on_book),
        ]

    async def stop(self) -> None:
        for u in self._unsubs:
            u()

    @property
    def listener(self) -> BrokerListener:
        assert self._listener is not None, "PaperBroker not bound to an OMS"
        return self._listener

    # ------------------------------------------------------------------ helpers

    def _fee(self, order: Order, price: int, qty: int, taker: bool) -> int:
        m = self.catalog.get(order.ticker)
        if m is None:
            return 0
        return self.catalog.fees(m).fee_micros(price, qty, is_taker=taker)

    def _fill(self, order: Order, price: int, qty: int, taker: bool) -> None:
        fee = self._fee(order, price, qty, taker)
        f = Fill(
            fill_id=hashlib.sha256(f"paper:{order.client_order_id}:{next(self._ids)}".encode()).hexdigest(),
            client_order_id=order.client_order_id,
            exchange_order_id=order.exchange_order_id or "",
            ticker=order.ticker,
            side=order.side,
            price=Price(price),
            qty=Qty(qty),
            fee=Micros(fee),
            is_taker=taker,
            ts_ms=self.clock.now_ms(),
            strategy=order.strategy,
        )
        self.listener.on_broker_fill(f)

    def _take(self, order: Order, limit: int, qty: int) -> int:
        """Consume opposite-side liquidity at prices no worse than `limit`. Returns filled Qty."""
        book = self.md.book(order.ticker)
        if book is None or qty <= 0:
            return 0
        opp = order.side.opposite
        levels = book.depth(opp, 1000)
        filled = 0
        for px, avail in levels:
            if (order.side is BookSide.BID and px > limit) or (order.side is BookSide.ASK and px < limit):
                break
            key = (order.ticker, opp, px)
            free = avail - self._consumed.get(key, 0)
            if free <= 0:
                continue
            take = min(free, qty - filled)
            self._consumed[key] = self._consumed.get(key, 0) + take
            self._fill(order, px, take, taker=True)
            filled += take
            if filled >= qty:
                break
        return filled

    def _crosses(self, order: Order, price: int) -> bool:
        book = self.md.book(order.ticker)
        if book is None:
            return False
        if order.side is BookSide.BID:
            return book.best_ask is not None and price >= book.best_ask
        return book.best_bid is not None and price <= book.best_bid

    def _visible_at(self, order: Order, price: int) -> int:
        book = self.md.book(order.ticker)
        if book is None:
            return 0
        side_book = book.bids if order.side is BookSide.BID else book.asks
        return side_book.get(price, 0)

    # ------------------------------------------------------------------ Broker API

    async def place(self, order: Order) -> None:
        order.exchange_order_id = f"paper-{next(self._ids)}"
        lst = self.listener
        crosses = self._crosses(order, order.price)
        if order.post_only and crosses:
            lst.on_broker_order(
                order.client_order_id,
                OrderStatus.REJECTED,
                exchange_id=order.exchange_order_id,
                reason="post_only_would_cross",
            )
            return
        if order.tif is TimeInForce.FOK:
            book = self.md.book(order.ticker)
            avail = book.qty_at_or_better(order.side.opposite, order.price) if book else 0
            if avail < order.qty:
                lst.on_broker_order(
                    order.client_order_id,
                    OrderStatus.CANCELED,
                    exchange_id=order.exchange_order_id,
                    reason="fok_unfilled",
                )
                return
        lst.on_broker_order(order.client_order_id, OrderStatus.RESTING, exchange_id=order.exchange_order_id)
        if crosses:
            self._take(order, order.price, order.remaining)
        if order.remaining <= 0:
            return  # FILLED via fills
        if order.tif in (TimeInForce.IOC, TimeInForce.FOK):
            lst.on_broker_order(order.client_order_id, OrderStatus.CANCELED, reason="ioc_remainder")
            return
        self._resting[order.client_order_id] = _Resting(
            order, int(self._visible_at(order, order.price) * self.queue_haircut)
        )

    async def cancel(self, order: Order) -> None:
        self._resting.pop(order.client_order_id, None)
        self.listener.on_broker_order(order.client_order_id, OrderStatus.CANCELED, reason="canceled")

    async def amend(self, order: Order, price: Price, qty: Qty, expiration_ts: int | None = None) -> bool:
        r = self._resting.get(order.client_order_id)
        if r is None or qty < order.filled:
            return False
        if price != order.price:
            if order.post_only and self._crosses(order, price):
                return False
            order.price = price
            r.queue_ahead = int(self._visible_at(order, price) * self.queue_haircut)  # lose queue priority
        order.qty = qty
        if expiration_ts is not None:
            order.expiration_ts = expiration_ts
        if order.remaining <= 0:
            self._resting.pop(order.client_order_id, None)
            self.listener.on_broker_order(
                order.client_order_id, OrderStatus.CANCELED, reason="amended_to_zero"
            )
        return True

    async def cancel_all(self) -> int:
        n = 0
        for coid, r in list(self._resting.items()):
            self._resting.pop(coid, None)
            self.listener.on_broker_order(r.order.client_order_id, OrderStatus.CANCELED, reason="cancel_all")
            n += 1
        return n

    async def snapshot(self) -> BrokerSnapshot:
        return BrokerSnapshot(open_orders=[r.order for r in self._resting.values()])

    # ------------------------------------------------------------------ maker fill simulation

    def _expire(self, now_ms: int) -> None:
        for coid, r in list(self._resting.items()):
            exp = r.order.expiration_ts
            if exp is not None and now_ms >= exp * 1000:
                self._resting.pop(coid, None)
                self.listener.on_broker_order(coid, OrderStatus.CANCELED, reason="expired")

    def _on_trade(self, e: TradePrinted) -> None:
        self._expire(e.ts_ms)
        remaining_print = e.qty
        for coid, r in list(self._resting.items()):
            o = r.order
            if o.ticker != e.ticker or o.remaining <= 0 or remaining_print <= 0 or o.created_ms > e.ts_ms:
                continue
            # A resting BID is hit by taker sells (taker_side ASK) at prices ≤ our bid; ASK by taker buys ≥ our ask.
            if o.side is BookSide.BID and not (e.taker_side is BookSide.ASK and e.price <= o.price):
                continue
            if o.side is BookSide.ASK and not (e.taker_side is BookSide.BID and e.price >= o.price):
                continue
            through = e.price < o.price if o.side is BookSide.BID else e.price > o.price
            if through:
                q = min(o.remaining, remaining_print)
            else:
                consume = min(r.queue_ahead, remaining_print)
                r.queue_ahead -= consume
                q = min(o.remaining, remaining_print - consume)
            if q > 0:
                remaining_print -= q
                self._fill(o, o.price, q, taker=False)
                if o.remaining <= 0:
                    self._resting.pop(coid, None)

    def _on_book(self, e: BookUpdated) -> None:
        # New book version: liquidity we consumed has been reflected (or replenished).
        for k in [k for k in self._consumed if k[0] == e.ticker]:
            del self._consumed[k]
        self._expire(e.ts_ms)
        book = self.md.book(e.ticker)
        if book is None:
            return
        for coid, r in list(self._resting.items()):
            o = r.order
            if o.ticker != e.ticker or o.remaining <= 0 or o.created_ms > book.ts_ms:
                continue
            r.queue_ahead = min(r.queue_ahead, self._visible_at(o, o.price))
            # Book traded through our resting price → we would have been filled first.
            # Asks at or below our resting bid (or bids at/above our ask) would have traded with us.
            if o.side is BookSide.BID and book.best_ask is not None and book.best_ask < o.price:
                q = min(o.remaining, book.qty_at_or_better(BookSide.ASK, o.price))
            elif o.side is BookSide.ASK and book.best_bid is not None and book.best_bid > o.price:
                q = min(o.remaining, book.qty_at_or_better(BookSide.BID, o.price))
            else:
                continue
            if q > 0:
                self._fill(o, o.price, q, taker=False)
                if o.remaining <= 0:
                    self._resting.pop(coid, None)
