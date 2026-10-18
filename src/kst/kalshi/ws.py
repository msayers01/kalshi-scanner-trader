"""Kalshi WebSocket client with auto-reconnect, desired-state subscriptions, and seq-gap detection.

Usage:
    ws = KalshiWs(url, signer, handlers={"orderbook_snapshot": f, "orderbook_delta": g, "ticker": h, ...})
    ws.want_channel("ticker"); ws.want_channel("fill"); ws.set_focus({"KXBTCD-..."})
    await ws.run()   # supervised task; reconnects forever

Orderbook subscriptions always send `use_yes_price: true` (docs/KALSHI_API_NOTES.md#orderbook).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import time
from collections.abc import Callable, Coroutine
from typing import Any

import orjson
import websockets
from websockets.asyncio.client import ClientConnection

from kst.core.logging import get_logger
from kst.kalshi.auth import KalshiSigner

log = get_logger(__name__)

Handler = Callable[[dict[str, Any]], None]
MARKET_CHANNELS = ("orderbook_delta", "trade")  # focus-set channels (need market tickers)


class KalshiWs:
    def __init__(
        self,
        url: str,
        signer: KalshiSigner,
        handlers: dict[str, Handler],
        on_state: Callable[[bool, str], None] | None = None,
        on_gap: Callable[[str], None] | None = None,
    ) -> None:
        self.url = url
        self.signer = signer
        self.handlers = handlers
        self.on_state = on_state
        self.on_gap = on_gap
        self._ids = itertools.count(1)
        self._ws: ClientConnection | None = None
        self._channels: set[str] = set()  # global channels wanted (ticker, fill, ...)
        self._index_ids: set[str] = set()  # cfbenchmarks_value index ids
        self._focus: set[str] = set()  # tickers wanted on MARKET_CHANNELS
        self._sid_channel: dict[int, str] = {}
        self._channel_sid: dict[str, int] = {}
        self._subscribed_focus: dict[str, set[str]] = {c: set() for c in MARKET_CHANNELS}
        self._pending: dict[int, str] = {}  # cmd id → channel
        self._last_seq: dict[int, int] = {}
        self._invalid_sids: set[int] = set()  # quarantined until a fresh subscription replaces them
        self._tasks: set[asyncio.Task[None]] = set()
        self.connected = False
        self.last_msg_ms = 0
        self.messages = 0
        self._send_lock = asyncio.Lock()
        self._focus_lock = asyncio.Lock()

    # ------------------------------------------------------------------ desired state

    def want_channel(self, channel: str) -> None:
        self._channels.add(channel)

    def want_indices(self, index_ids: set[str]) -> None:
        self._index_ids |= index_ids

    def set_focus(self, tickers: set[str]) -> None:
        self._focus = set(tickers)
        if self.connected:
            self._spawn(self._sync_focus())

    @property
    def focus(self) -> set[str]:
        return set(self._focus)

    # ------------------------------------------------------------------ run loop

    async def run(self) -> None:
        backoff = 1.0
        while True:
            try:
                headers = self.signer.headers("GET", "/trade-api/ws/v2")
                async with websockets.connect(
                    self.url,
                    additional_headers=headers,
                    ping_interval=10,
                    ping_timeout=20,
                    max_size=2**24,
                    open_timeout=15,
                ) as ws:
                    self._on_connected(ws)
                    backoff = 1.0
                    await self._subscribe_all()
                    async for raw in ws:
                        self._dispatch(raw)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("kalshi_ws_disconnected", error=f"{type(exc).__name__}: {exc}")
            finally:
                self._on_disconnected("disconnected")
                tasks = list(self._tasks)
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)

    def _on_connected(self, ws: ClientConnection) -> None:
        self._ws = ws
        self.connected = True
        self._sid_channel.clear()
        self._channel_sid.clear()
        self._last_seq.clear()
        self._invalid_sids.clear()
        self._pending.clear()
        self._subscribed_focus = {c: set() for c in MARKET_CHANNELS}
        log.info("kalshi_ws_connected", url=self.url)
        if self.on_state:
            self.on_state(True, "connected")

    def _on_disconnected(self, detail: str) -> None:
        was = self.connected
        self.connected = False
        self._ws = None
        if was and self.on_state:
            self.on_state(False, detail)

    async def close(self) -> None:
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()

    # ------------------------------------------------------------------ commands

    def _spawn(self, work: Coroutine[Any, Any, None]) -> None:
        task = asyncio.create_task(work)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)

    def _task_done(self, task: asyncio.Task[None]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            log.warning("kalshi_ws_command_failed", error=str(exc))

    async def _send(self, cmd: str, params: dict[str, Any], channel: str = "") -> int:
        if self._ws is None:
            return -1
        cid = next(self._ids)
        if channel:
            self._pending[cid] = channel
        async with self._send_lock:
            await self._ws.send(orjson.dumps({"id": cid, "cmd": cmd, "params": params}).decode())
        return cid

    async def _subscribe_all(self) -> None:
        for ch in sorted(self._channels):
            await self._send("subscribe", {"channels": [ch]}, ch)
        if self._index_ids:
            await self._send(
                "subscribe",
                {"channels": ["cfbenchmarks_value"], "index_ids": sorted(self._index_ids)},
                "cfbenchmarks_value",
            )
        await self._sync_focus()

    async def _sync_focus(self) -> None:
        async with self._focus_lock:
            await self._sync_focus_locked()

    async def _sync_focus_locked(self) -> None:
        if not self.connected:
            return
        for ch in MARKET_CHANNELS:
            want, have = self._focus, self._subscribed_focus[ch]
            sid = self._channel_sid.get(ch)
            if sid is None:
                if want and ch not in self._pending.values():
                    params: dict[str, Any] = {"channels": [ch], "market_tickers": sorted(want)}
                    if ch == "orderbook_delta":
                        params["use_yes_price"] = True
                    await self._send("subscribe", params, ch)
                    self._subscribed_focus[ch] = set(want)
                continue
            add, rem = want - have, have - want
            if add:
                await self._send(
                    "update_subscription",
                    {"sids": [sid], "market_tickers": sorted(add), "action": "add_markets"},
                )
            if rem and want:
                await self._send(
                    "update_subscription",
                    {"sids": [sid], "market_tickers": sorted(rem), "action": "delete_markets"},
                )
            elif rem and not want:
                self._invalid_sids.add(sid)
                await self._send("unsubscribe", {"sids": [sid]})
                self._channel_sid.pop(ch, None)
                self._sid_channel.pop(sid, None)
            self._subscribed_focus[ch] = set(want)

    async def resubscribe(self, channel: str) -> None:
        """Drop and recreate a channel subscription (e.g. after an orderbook seq gap → fresh snapshots)."""
        sid = self._channel_sid.pop(channel, None)
        if sid is not None:
            self._invalid_sids.add(sid)
            self._sid_channel.pop(sid, None)
            self._last_seq.pop(sid, None)
            await self._send("unsubscribe", {"sids": [sid]})
        if channel in MARKET_CHANNELS:
            self._subscribed_focus[channel] = set()
            await self._sync_focus()
        elif channel == "cfbenchmarks_value":
            await self._send(
                "subscribe", {"channels": [channel], "index_ids": sorted(self._index_ids)}, channel
            )
        else:
            await self._send("subscribe", {"channels": [channel]}, channel)

    # ------------------------------------------------------------------ dispatch

    def _dispatch(self, raw: str | bytes) -> None:
        try:
            msg = orjson.loads(raw)
        except orjson.JSONDecodeError:
            log.warning("kalshi_ws_bad_json", raw=str(raw)[:200])
            return
        self.messages += 1
        self.last_msg_ms = time.time_ns() // 1_000_000
        mtype = msg.get("type", "")
        if mtype == "subscribed":
            body = msg.get("msg") or {}
            ch, sid = body.get("channel") or self._pending.get(msg.get("id", -1), ""), body.get("sid")
            self._pending.pop(msg.get("id", -1), None)
            if sid is not None and ch:
                self._invalid_sids.discard(sid)
                self._sid_channel[sid] = ch
                self._channel_sid[ch] = sid
                if ch in MARKET_CHANNELS:
                    self._spawn(self._sync_focus())  # focus may have changed while subscribe was pending
            return
        if mtype == "error":
            self._pending.pop(msg.get("id", -1), None)
            log.warning(
                "kalshi_ws_error",
                code=(msg.get("msg") or {}).get("code"),
                detail=(msg.get("msg") or {}).get("msg"),
            )
            return
        sid, seq = msg.get("sid"), msg.get("seq")
        if sid is not None and sid in self._invalid_sids:
            return
        if sid is not None and seq is not None:
            last = self._last_seq.get(sid)
            if last is not None and seq != last + 1:
                ch = self._sid_channel.get(sid, "")
                log.warning("kalshi_ws_seq_gap", sid=sid, channel=ch, expected=last + 1, got=seq)
                self._last_seq.pop(sid, None)
                self._invalid_sids.add(sid)
                if self.on_gap and ch:
                    self.on_gap(ch)
                if ch:
                    self._spawn(self.resubscribe(ch))
                return
            self._last_seq[sid] = seq
        handler = self.handlers.get(mtype)
        if handler is not None:
            try:
                handler(msg)
            except Exception:
                log.exception("kalshi_ws_handler_error", msg_type=mtype)
