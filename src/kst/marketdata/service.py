"""Market-data service: maintains YES-space order books and top-of-book stats; publishes bus events.

Sources:
  ws   — authenticated Kalshi WS: `ticker` (whole universe), `orderbook_delta` + `trade` (focus set),
         `market_lifecycle_v2` (status/settlement).
  poll — unauthenticated REST: batched orderbooks for the focus set, /markets stats for the universe,
         exchange-wide /markets/trades since the last poll.

Focus set = union of named requests ("scanner", "positions", "ui", ...). Only focus markets have depth.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Iterable
from typing import Any

from kst.config import MarketDataCfg
from kst.core.bus import EventBus
from kst.core.clock import Clock
from kst.core.events import BookUpdated, FeedHealth, MarketStatusChanged, TopOfBookUpdated, TradePrinted
from kst.core.fixedpoint import price_from_str
from kst.core.logging import get_logger
from kst.core.types import MarketStatus
from kst.domain.orderbook import OrderBook
from kst.kalshi.parse import (
    parse_rest_orderbook,
    parse_trade,
    parse_ws_delta,
    parse_ws_snapshot,
    update_market_stats,
)
from kst.kalshi.rest import KalshiRest
from kst.kalshi.ws import KalshiWs
from kst.marketdata.catalog import MarketCatalog
from kst.marketdata.recorder import Recorder

log = get_logger(__name__)


class MarketDataService:
    """Implements `kst.domain.views.BookView`."""

    def __init__(
        self,
        cfg: MarketDataCfg,
        catalog: MarketCatalog,
        rest: KalshiRest,
        bus: EventBus,
        clock: Clock,
        *,
        ws: KalshiWs | None = None,
        recorder: Recorder | None = None,
        stale_ws_s: float = 5.0,
        stale_poll_s: float = 20.0,
    ) -> None:
        self.cfg = cfg
        self.catalog = catalog
        self.rest = rest
        self.bus = bus
        self.clock = clock
        self.ws = ws
        self.recorder = recorder
        self.mode = "ws" if ws is not None else "poll"
        self.stale_ws_ms = int(stale_ws_s * 1000)
        self.stale_poll_ms = int(stale_poll_s * 1000)
        self.books: dict[str, OrderBook] = {}
        self._focus_requests: dict[str, set[str]] = {}
        self._focus: set[str] = set()
        self._ws_epoch = 0
        self._book_epoch: dict[str, int] = {}
        self._last_trade_ts = 0
        self._seen_trades: deque[str] = deque(maxlen=20_000)
        self._seen_trade_set: set[str] = set()
        self.last_book_poll_ms = 0
        self.last_stats_poll_ms = 0
        self.errors = 0

    # ------------------------------------------------------------------ BookView

    def book(self, ticker: str) -> OrderBook | None:
        return self.books.get(ticker)

    def is_fresh(self, ticker: str, now_ms: int) -> bool:
        b = self.books.get(ticker)
        if b is None or b.ts_ms == 0:
            return False
        if self.mode == "ws":
            return bool(self.ws and self.ws.connected) and self._book_epoch.get(ticker) == self._ws_epoch
        return now_ms - b.ts_ms <= self.stale_poll_ms

    @property
    def connected(self) -> bool:
        if self.mode == "ws":
            return bool(self.ws and self.ws.connected)
        return self.clock.now_ms() - self.last_book_poll_ms < self.stale_poll_ms or not self._focus

    # ------------------------------------------------------------------ focus management

    def request_focus(self, source: str, tickers: Iterable[str]) -> None:
        """Replace `source`'s focus request. Scanner requests are rank-ordered and capped at focus_size."""
        wanted = list(tickers)
        if source == "scanner":
            wanted = wanted[: self.cfg.focus_size]
        self._focus_requests[source] = set(wanted)
        new = set().union(*self._focus_requests.values()) if self._focus_requests else set()
        if new != self._focus:
            # Books leaving the focus set stop receiving deltas: they must not keep reading as fresh.
            for t in self._focus - new:
                self._book_epoch.pop(t, None)
            self._focus = new
            for t in new:
                self.books.setdefault(t, OrderBook(t))
            if self.ws is not None:
                self.ws.set_focus(new)

    def add_focus(self, source: str, tickers: Iterable[str]) -> None:
        """Union `tickers` into `source`'s request (e.g. markets the UI has opened)."""
        self.request_focus(source, self._focus_requests.get(source, set()) | set(tickers))

    @property
    def focus(self) -> set[str]:
        return set(self._focus)

    # ------------------------------------------------------------------ WS handlers

    def ws_handlers(self) -> dict[str, Any]:
        return {
            "orderbook_snapshot": self._on_ws_snapshot,
            "orderbook_delta": self._on_ws_delta,
            "ticker": self._on_ws_ticker,
            "trade": self._on_ws_trade,
            "market_lifecycle_v2": self._on_ws_lifecycle,
        }

    def on_ws_state(self, connected: bool, detail: str) -> None:
        if connected:
            self._ws_epoch += 1
        self.bus.publish(FeedHealth(self.clock.now_ms(), "kalshi_ws", connected, detail))

    def on_ws_gap(self, channel: str) -> None:
        if channel == "orderbook_delta":
            # Sequence numbers span the subscription, so every book on it needs a fresh snapshot.
            self._book_epoch.clear()

    def _on_ws_snapshot(self, msg: dict[str, Any]) -> None:
        body = msg["msg"]
        t = body["market_ticker"]
        bids, asks = parse_ws_snapshot(body, use_yes_price=True)
        ts = int(body.get("ts_ms") or msg.get("sending_ts_ms") or self.clock.now_ms())
        b = self.books.setdefault(t, OrderBook(t))
        b.apply_snapshot(bids, asks, ts, int(msg.get("seq") or 0))
        self._book_epoch[t] = self._ws_epoch
        if self.recorder:
            self.recorder.book(ts, t, bids, asks, received_ms=self.clock.now_ms())
        self.bus.publish(BookUpdated(ts, t))

    def _on_ws_delta(self, msg: dict[str, Any]) -> None:
        body = msg["msg"]
        t = body["market_ticker"]
        b = self.books.get(t)
        if b is None or self._book_epoch.get(t) != self._ws_epoch:
            return  # no snapshot yet in this connection epoch
        side, price, delta = parse_ws_delta(body, use_yes_price=True)
        ts = int(body.get("ts_ms") or msg.get("sending_ts_ms") or self.clock.now_ms())
        b.apply_delta(side, price, delta, ts, int(msg.get("seq") or 0))
        if self.recorder:
            self.recorder.delta(ts, t, side.value, price, delta, received_ms=self.clock.now_ms())
        self.bus.publish(BookUpdated(ts, t))

    def _on_ws_ticker(self, msg: dict[str, Any]) -> None:
        body = msg["msg"]
        m = self.catalog.get(body.get("market_ticker", ""))
        if m is None:
            return
        ts = int(body.get("ts_ms") or self.clock.now_ms())
        update_market_stats(m, body, ts)
        if self.recorder:
            self.recorder.ticker(ts, m.ticker, body, received_ms=self.clock.now_ms())
        self.bus.publish(TopOfBookUpdated(ts, m.ticker))

    def _on_ws_trade(self, msg: dict[str, Any]) -> None:
        t, price, qty, taker, ts = parse_trade(msg["msg"])
        if self.recorder:
            self.recorder.trade(ts, t, price, qty, taker.value, received_ms=self.clock.now_ms())
        self.bus.publish(TradePrinted(ts or self.clock.now_ms(), t, price, qty, taker))

    def _on_ws_lifecycle(self, msg: dict[str, Any]) -> None:
        body = msg["msg"]
        t = body.get("market_ticker", "")
        etype = body.get("event_type", "")
        m = self.catalog.get(t)
        status = {
            "activated": MarketStatus.ACTIVE,
            "deactivated": MarketStatus.INACTIVE,
            "determined": MarketStatus.DETERMINED,
            "settled": MarketStatus.FINALIZED,
        }.get(etype)
        if m is not None and etype == "close_date_updated" and body.get("close_ts"):
            m.close_time_ms = int(body["close_ts"]) * 1000
        if status is None:
            return
        result = body.get("result", "") or ""
        sv = body.get("settlement_value")
        if m is not None:
            m.status = status
            m.result = result or m.result
        self.bus.publish(
            MarketStatusChanged(self.clock.now_ms(), t, status, result, price_from_str(sv) if sv else None)
        )

    # ------------------------------------------------------------------ runners

    async def run_ws(self) -> None:
        assert self.ws is not None
        await self.ws.run()

    async def run_catalog(self) -> None:
        while True:
            try:
                await self.catalog.refresh(self.rest)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.errors += 1
                log.exception("catalog_refresh_error")
            await asyncio.sleep(self.catalog.cfg.catalog_refresh_s)

    async def run_poll_books(self) -> None:
        while True:
            t0 = time.monotonic()
            tickers = sorted(self._focus)
            if tickers:
                try:
                    rows = await self.rest.orderbooks(tickers)
                    now = self.clock.now_ms()
                    for row in rows:
                        t = row.get("ticker", "")
                        bids, asks = parse_rest_orderbook(row)
                        b = self.books.setdefault(t, OrderBook(t))
                        b.apply_snapshot(bids, asks, now)
                        if self.recorder:
                            self.recorder.book(now, t, bids, asks)
                        self.bus.publish(BookUpdated(now, t))
                    self.last_book_poll_ms = now
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.errors += 1
                    log.warning("poll_books_error", error=str(exc))
            await asyncio.sleep(max(0.2, self.cfg.poll_books_interval_s - (time.monotonic() - t0)))

    UI_LIVE = "ui_live"  # focus source: books a UI client is watching right now

    async def run_poll_ui_books(self) -> None:
        """REST mode: refresh only the books open in the UI every `ui_book_interval_s` (one request), so the
        manual ticket's Bid/Mid/Ask track the market far faster than the 3 s focus-set poll."""
        interval = self.cfg.ui_book_interval_s
        if interval <= 0:
            return
        while True:
            t0 = time.monotonic()
            tickers = sorted(self._focus_requests.get(self.UI_LIVE, set()))[:5]
            if tickers:
                try:
                    now = self.clock.now_ms()
                    for row in await self.rest.orderbooks(tickers):
                        t = row.get("ticker", "")
                        if t not in self._focus:
                            continue  # closed while the request was in flight
                        bids, asks = parse_rest_orderbook(row)
                        self.books.setdefault(t, OrderBook(t)).apply_snapshot(bids, asks, now)
                        self.bus.publish(BookUpdated(now, t))
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self.errors += 1
                    log.warning("poll_ui_books_error", error=str(exc))
            await asyncio.sleep(max(0.2, interval - (time.monotonic() - t0)))

    async def run_poll_trades(self, interval_s: float = 5.0) -> None:
        """Exchange-wide public trades since the last poll (feeds the paper maker-fill model)."""
        self._last_trade_ts = self.clock.now_ms() // 1000
        while True:
            await asyncio.sleep(interval_s)
            if not self._focus:
                continue
            try:
                newest = self._last_trade_ts
                n = 0
                async for raw in self.rest.trades(min_ts=self._last_trade_ts):
                    tid = raw.get("trade_id", "")
                    if tid in self._seen_trade_set:
                        continue
                    if len(self._seen_trades) == self._seen_trades.maxlen:
                        self._seen_trade_set.discard(self._seen_trades[0])
                    self._seen_trades.append(tid)
                    self._seen_trade_set.add(tid)
                    t, price, qty, taker, ts = parse_trade(raw)
                    n += 1
                    newest = max(newest, ts // 1000)
                    if t in self._focus:
                        if self.recorder:
                            self.recorder.trade(
                                ts, t, price, qty, taker.value, received_ms=self.clock.now_ms()
                            )
                        self.bus.publish(TradePrinted(ts, t, price, qty, taker))
                    if n >= 3000:
                        break
                self._last_trade_ts = newest
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.errors += 1
                log.warning("poll_trades_error", error=str(exc))

    def health(self) -> dict[str, Any]:
        now = self.clock.now_ms()
        return {
            "mode": self.mode,
            "connected": self.connected,
            "focus": len(self._focus),
            "books": len(self.books),
            "universe": len(self.catalog.universe),
            "ws_messages": self.ws.messages if self.ws else 0,
            "last_book_poll_age_s": round((now - self.last_book_poll_ms) / 1000, 1)
            if self.last_book_poll_ms
            else None,
            "errors": self.errors,
        }
