"""WebSocket hub: push-based, per-topic coalescing, ≤ push_hz flushes per client.

Bus events only mark topics dirty; the flush loop builds each dirty payload ONCE and fans it out, so
a burst of 1 000 book deltas costs one serialization per flush. Fills and alerts are append-only
streams (never dropped). Clients subscribe to one order book at a time: {"op":"sub_book","ticker":...}.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

import orjson
from fastapi import WebSocket, WebSocketDisconnect

from kst.api import serialize as ser
from kst.core.events import (
    Alert,
    AutotradeChanged,
    BookUpdated,
    EngineStateChanged,
    FillReceived,
    LLMVerdictsUpdated,
    OrderUpdated,
    PortfolioUpdated,
    PositionChanged,
    RiskLimitsChanged,
    RiskRejected,
    ScanUpdated,
)
from kst.core.logging import get_logger
from kst.scanner.query import ScanQuery

if TYPE_CHECKING:
    from kst.app import Application

log = get_logger(__name__)


class _Client:
    def __init__(self, ws: WebSocket) -> None:
        self.ws = ws
        self.book: str | None = None
        self.scan_query: ScanQuery | None = None  # column filters/sort from the scanner table
        self.queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
        self.dropped = 0

    def offer(self, data: bytes) -> None:
        try:
            self.queue.put_nowait(data)
        except asyncio.QueueFull:
            # Slow client: drop the oldest frame, never block the engine.
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            self.dropped += 1
            self.queue.put_nowait(data)


class WsHub:
    def __init__(self, app: Application, push_hz: float = 10.0) -> None:
        self.app = app
        self.interval = 1.0 / max(push_hz, 1.0)
        self.clients: set[_Client] = set()
        self._dirty: set[str] = set()
        self._dirty_books: set[str] = set()
        self._appends: list[tuple[str, Any]] = []
        self._last_scan_ms = 0
        bus = app.bus
        bus.subscribe(ScanUpdated, lambda _: self._dirty.add("scan"))
        bus.subscribe(PortfolioUpdated, lambda _: self._dirty.add("portfolio"))
        bus.subscribe(PositionChanged, lambda _: self._dirty.add("positions"))
        bus.subscribe(OrderUpdated, lambda _: self._dirty.add("orders"))
        bus.subscribe(EngineStateChanged, lambda _: self._dirty.add("status"))
        bus.subscribe(AutotradeChanged, lambda _: self._dirty.add("status"))
        bus.subscribe(RiskLimitsChanged, lambda _: self._dirty.update({"limits", "status"}))
        bus.subscribe(LLMVerdictsUpdated, lambda _: self._dirty.add("llm"))
        bus.subscribe(BookUpdated, lambda e: self._dirty_books.add(e.ticker))
        bus.subscribe(FillReceived, lambda e: self._appends.append(("fill", ser.fill_dict(e.fill))))
        bus.subscribe(
            Alert,
            lambda e: self._appends.append(
                ("alert", {"level": e.level, "title": e.title, "message": e.message, "ts_ms": e.ts_ms})
            ),
        )
        bus.subscribe(
            RiskRejected,
            lambda e: self._appends.append(
                (
                    "reject",
                    {
                        "ticker": e.ticker,
                        "check": e.check,
                        "detail": e.detail,
                        "strategy": e.strategy,
                        "ts_ms": e.ts_ms,
                    },
                )
            ),
        )

    # ------------------------------------------------------------------ payloads

    def builders(self) -> dict[str, Callable[[], Any]]:
        a = self.app
        return {
            "status": lambda: {**a.control.status(), "health": a.health()},
            "portfolio": lambda: a.portfolio.summary().to_dict(),
            "positions": lambda: ser.positions(a),
            "orders": lambda: ser.orders(a, 60),
            "scan": lambda: ser.scan(a, 100),
            "limits": lambda: a.risk.limits.model_dump(),
            "llm": lambda: {
                "status": a.gate.status(),
                "verdicts": [v.to_dict() for v in list(a.gate.verdicts.values())[-50:]],
            },
        }

    def _scan_frame(self, query: ScanQuery | None) -> bytes:
        return orjson.dumps(
            {"type": "scan", "data": ser.scan(self.app, 100, query)}, option=orjson.OPT_NON_STR_KEYS
        )

    def snapshot(self) -> bytes:
        data = {k: f() for k, f in self.builders().items()}
        return orjson.dumps({"type": "snapshot", "data": data}, option=orjson.OPT_NON_STR_KEYS)

    # ------------------------------------------------------------------ connection handling

    async def handle(self, ws: WebSocket) -> None:
        await ws.accept()
        c = _Client(ws)
        self.clients.add(c)
        sender = asyncio.create_task(self._sender(c))
        try:
            c.offer(self.snapshot())
            while True:
                msg = orjson.loads(await ws.receive_text())
                op = msg.get("op")
                if op == "sub_book":
                    c.book = str(msg.get("ticker") or "") or None
                    if c.book:
                        self.app.md.add_focus("ui", {c.book})
                        b = ser.book(self.app, c.book)
                        c.offer(orjson.dumps({"type": "book", "data": b}))
                elif op == "unsub_book":
                    c.book = None
                elif op == "scan_query":
                    try:
                        q = ScanQuery.parse(msg.get("query"))
                    except (ValueError, TypeError) as exc:
                        c.offer(orjson.dumps({"type": "scan_query_error", "data": {"error": str(exc)}}))
                        continue
                    c.scan_query = q if q.active else None
                    c.offer(self._scan_frame(c.scan_query))
                elif op == "ping":
                    c.offer(orjson.dumps({"type": "pong", "data": {"ts": msg.get("ts")}}))
        except (WebSocketDisconnect, RuntimeError):
            pass
        except Exception:
            log.exception("ws_client_error")
        finally:
            self.clients.discard(c)
            sender.cancel()
            self._refresh_ui_focus()

    def _refresh_ui_focus(self) -> None:
        books = {c.book for c in self.clients if c.book}
        self.app.md.request_focus("ui", books)

    async def _sender(self, c: _Client) -> None:
        try:
            while True:
                data = await c.queue.get()
                await c.ws.send_text(data.decode())
        except Exception:
            return

    # ------------------------------------------------------------------ flush loop

    async def run(self) -> None:
        while True:
            await asyncio.sleep(self.interval)
            if not self.clients:
                self._dirty.clear()
                self._dirty_books.clear()
                self._appends.clear()
                continue
            try:
                self.flush()
            except Exception:
                log.exception("ws_flush_failed")

    def flush(self) -> None:
        now = self.app.clock.now_ms()
        dirty, self._dirty = self._dirty, set()
        if "scan" in dirty and now - self._last_scan_ms < 500:
            self._dirty.add("scan")  # scanner table ≤ 2 Hz
            dirty.discard("scan")
        elif "scan" in dirty:
            self._last_scan_ms = now
        # The scanner table is per client (each may have its own column query): one frame per distinct query.
        scan_dirty = "scan" in dirty
        dirty.discard("scan")
        scan_frames: dict[Any, bytes] = {}
        builders = self.builders()
        frames: list[bytes] = []
        for topic in dirty:
            f = builders.get(topic)
            if f is not None:
                frames.append(orjson.dumps({"type": topic, "data": f()}, option=orjson.OPT_NON_STR_KEYS))
        appends, self._appends = self._appends, []
        for kind, payload in appends:
            frames.append(orjson.dumps({"type": kind, "data": payload}))
        books, self._dirty_books = self._dirty_books, set()
        book_frames: dict[str, bytes] = {}
        for c in list(self.clients):
            for fr in frames:
                c.offer(fr)
            if scan_dirty:
                k = c.scan_query.key() if c.scan_query is not None else None
                if k not in scan_frames:
                    scan_frames[k] = self._scan_frame(c.scan_query)
                c.offer(scan_frames[k])
            if c.book and c.book in books:
                if c.book not in book_frames:
                    book_frames[c.book] = orjson.dumps({"type": "book", "data": ser.book(self.app, c.book)})
                c.offer(book_frames[c.book])
