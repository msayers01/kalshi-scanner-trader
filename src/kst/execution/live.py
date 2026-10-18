"""LiveBroker: Kalshi demo/prod via V2 REST order endpoints + WS `fill` / `user_orders` channels.

Safety nets (docs/RISK.md): every resting order carries `cancel_order_on_pause` and an `expiration_time`
TTL; an optional order group caps contracts matched per rolling 15 s window exchange-side.
Missed WS fills are recovered by periodic REST reconciliation (deduped by fill id).
"""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import replace
from typing import Any

import httpx

from kst.core.clock import Clock
from kst.core.fixedpoint import Price, Qty, qty_from_str
from kst.core.logging import get_logger
from kst.core.types import OrderStatus, TimeInForce
from kst.domain.orders import Order
from kst.execution.broker import BrokerListener, BrokerSnapshot, OrderSubmissionUnknownError
from kst.kalshi.parse import order_status, parse_fill, parse_order, parse_position
from kst.kalshi.rest import KalshiApiError, KalshiRest
from kst.kalshi.ws import KalshiWs

log = get_logger(__name__)
_IOC_FILL_DELAYS = (0.3, 0.7, 1.5, 3.0)


class LiveBroker:
    def __init__(
        self,
        rest: KalshiRest,
        ws: KalshiWs | None,
        clock: Clock,
        *,
        order_ttl_s: int = 600,
        order_group_limit: int | None = None,
    ) -> None:
        self.name = "live"
        self.rest = rest
        self.ws = ws
        self.clock = clock
        self.order_ttl_s = order_ttl_s
        self.order_group_limit = order_group_limit
        self.order_group_id: str | None = None
        self._listener: BrokerListener | None = None
        self._ex_to_coid: dict[str, str] = {}
        self._seen_fills: set[str] = set()
        self._last_fill_poll_s = 0
        self._ioc_tasks: set[asyncio.Task[None]] = set()

    def bind(self, listener: BrokerListener) -> None:
        self._listener = listener

    @property
    def listener(self) -> BrokerListener:
        assert self._listener is not None
        return self._listener

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self.ws is not None:
            self.ws.handlers["fill"] = self._on_ws_fill
            self.ws.handlers["user_order"] = self._on_ws_order
            for ch in ("fill", "user_orders", "market_positions"):
                self.ws.want_channel(ch)
        if self.order_group_limit:
            res = await self.rest.create_order_group(self.order_group_limit)
            self.order_group_id = res.get("order_group_id")
            if not self.order_group_id:
                raise RuntimeError("Configured order group was not created; refusing to trade")
            log.info("order_group_created", id=self.order_group_id, limit=self.order_group_limit)
        self._last_fill_poll_s = self.clock.now_ms() // 1000 - 60

    async def stop(self) -> None:
        tasks = list(self._ioc_tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self._ioc_tasks.clear()

    def _ioc_done(self, task: asyncio.Task[None]) -> None:
        self._ioc_tasks.discard(task)
        if not task.cancelled() and (exc := task.exception()) is not None:
            log.error("ioc_finalize_failed", error=str(exc))

    # ------------------------------------------------------------------ orders

    async def place(self, order: Order) -> None:
        exp = None
        if order.tif is TimeInForce.GTC:
            exp = order.expiration_ts or (self.clock.now_ms() // 1000 + self.order_ttl_s)
            order.expiration_ts = exp
        try:
            res = await self.rest.create_order(
                ticker=order.ticker,
                side=order.side,
                price=order.price,
                qty=order.qty,
                client_order_id=order.client_order_id,
                tif=order.tif,
                post_only=order.post_only,
                reduce_only=order.reduce_only and order.tif is TimeInForce.IOC,
                expiration_ts=exp,
                cancel_on_pause=True,
                order_group_id=self.order_group_id,
            )
        except KalshiApiError as exc:
            if exc.is_conflict or exc.status >= 500:
                raise OrderSubmissionUnknownError(
                    f"Create returned {exc.status}; retain order pending WS/REST reconciliation"
                ) from exc
            self.listener.on_broker_order(
                order.client_order_id, OrderStatus.REJECTED, reason=f"{exc.code}: {exc.message}"
            )
            return
        except httpx.TransportError as exc:
            raise OrderSubmissionUnknownError(
                "Create transport failed; exchange acceptance is unknown"
            ) from exc
        ex_id = res.get("order_id")
        if not ex_id:
            raise OrderSubmissionUnknownError(
                "Create response omitted order_id; exchange acceptance is unknown"
            )
        self._ex_to_coid[ex_id] = order.client_order_id
        try:
            remaining = qty_from_str(res.get("remaining_count", "0"))
            filled = qty_from_str(res.get("fill_count", "0"))
        except (ValueError, TypeError) as exc:
            raise OrderSubmissionUnknownError("Create response has invalid fill/remaining counts") from exc
        # Accepted. Fills (immediate or later) arrive via WS `fill` / REST reconciliation and complete the order.
        self.listener.on_broker_order(order.client_order_id, OrderStatus.RESTING, exchange_id=ex_id)
        if order.tif is not TimeInForce.GTC:
            if filled == 0:
                self.listener.on_broker_order(
                    order.client_order_id, OrderStatus.CANCELED, reason="ioc_unfilled"
                )
            else:
                task = asyncio.create_task(self._finalize_ioc(order, filled, remaining))
                self._ioc_tasks.add(task)
                task.add_done_callback(self._ioc_done)

    async def _finalize_ioc(self, order: Order, filled: int, remaining: int) -> None:
        """Wait for the IOC's fills to arrive (WS, else REST) before closing out the remainder."""
        for delay in _IOC_FILL_DELAYS:
            if order.filled >= filled:
                break
            await asyncio.sleep(delay)
            if order.filled < filled:
                await self.poll_fills()
        if order.filled < filled:
            log.warning(
                "ioc_fills_missing", coid=order.client_order_id, expected=filled / 100, got=order.filled / 100
            )
            return  # keep exposure reserved; missing fills must not look like an unfilled IOC
        if remaining > 0 or order.filled < order.qty:
            self.listener.on_broker_order(order.client_order_id, OrderStatus.CANCELED, reason="ioc_remainder")

    async def cancel(self, order: Order) -> None:
        if not order.exchange_order_id:
            return
        try:
            await self.rest.cancel_order(order.exchange_order_id, order.ticker)
            self.listener.on_broker_order(order.client_order_id, OrderStatus.CANCELED, reason="canceled")
        except KalshiApiError as exc:
            if exc.status == 404:
                self.listener.on_broker_order(order.client_order_id, OrderStatus.CANCELED, reason="not_found")
            else:
                raise

    async def amend(self, order: Order, price: Price, qty: Qty, expiration_ts: int | None = None) -> bool:
        if not order.exchange_order_id:
            return False
        try:
            res = await self.rest.amend_order(
                order.exchange_order_id,
                ticker=order.ticker,
                side=order.side,
                price=price,
                total_qty=qty,
                expiration_ts=expiration_ts,
            )
        except KalshiApiError as exc:
            log.warning("amend_failed", coid=order.client_order_id, error=str(exc))
            return False
        order.price, order.qty = price, qty
        if ex_id := res.get("order_id"):
            self._ex_to_coid[ex_id] = order.client_order_id
            self.listener.on_broker_order(order.client_order_id, order.status, exchange_id=ex_id)
        if expiration_ts:
            order.expiration_ts = expiration_ts
        return True

    async def cancel_all(self) -> int:
        await self.rest.cancel_all()
        return -1  # exchange doesn't return a count; OMS marks local orders canceled

    async def snapshot(self) -> BrokerSnapshot:
        bal = await self.rest.balance()
        positions = [parse_position(p) for p in await self.rest.positions()]
        orders = [parse_order(o) for o in await self.rest.open_orders()]
        for o in orders:
            if o.exchange_order_id:
                self._ex_to_coid.setdefault(o.exchange_order_id, o.client_order_id)
        return BrokerSnapshot(
            balance_cents=int(bal.get("balance", 0)), positions=positions, open_orders=orders
        )

    # ------------------------------------------------------------------ fills / order updates

    def _emit_fill(self, raw: dict[str, Any]) -> None:
        f = parse_fill(raw)
        # WS identifies a match by trade_id; REST additionally has a different fill_id. Use the
        # order+trade pair on both paths (one trade may have two of our orders as counterparties).
        if raw.get("trade_id") and f.exchange_order_id:
            key = f"{f.exchange_order_id}:{raw['trade_id']}"
            f = replace(f, fill_id=hashlib.sha256(key.encode()).hexdigest())
        if not f.fill_id or f.fill_id in self._seen_fills:
            return
        self._seen_fills.add(f.fill_id)
        coid = f.client_order_id or self._ex_to_coid.get(f.exchange_order_id, "")
        if coid and coid != f.client_order_id:
            f = replace(f, client_order_id=coid)
        self.listener.on_broker_fill(f)

    def _on_ws_fill(self, msg: dict[str, Any]) -> None:
        self._emit_fill(msg.get("msg") or {})

    def _on_ws_order(self, msg: dict[str, Any]) -> None:
        body = msg.get("msg") or {}
        ex_id = body.get("order_id", "")
        coid = body.get("client_order_id") or self._ex_to_coid.get(ex_id, "")
        if not coid:
            return
        if ex_id:
            self._ex_to_coid[ex_id] = coid
        status = order_status(body.get("status"))
        if status is OrderStatus.FILLED:
            return  # completion is driven by fills
        self.listener.on_broker_order(coid, status, exchange_id=ex_id or None)

    async def poll_fills(self) -> None:
        """REST reconciliation for fills the WS may have missed."""
        try:
            rows = await self.rest.fills(min_ts=self._last_fill_poll_s)
        except Exception as exc:
            log.warning("fill_poll_failed", error=str(exc))
            return
        for raw in sorted(rows, key=lambda r: r.get("created_time", "")):
            self._emit_fill(raw)
        self._last_fill_poll_s = self.clock.now_ms() // 1000 - 120

    async def run_reconcile(self, interval_s: float = 15.0) -> None:
        while True:
            await asyncio.sleep(interval_s)
            await self.poll_fills()
