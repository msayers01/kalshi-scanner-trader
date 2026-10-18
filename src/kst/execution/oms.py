"""OrderManager: the ONLY path to a broker (CLAUDE.md rule 5).

* `submit()` runs RiskEngine.check() before every placement — strategies, manual UI/Discord orders, and
  flatten all go through here.
* `reconcile()` converges a strategy's resting quotes in a market to its declarative targets (ADR 0003):
  keep within tolerance, amend (or cancel/replace) on change, cancel what is no longer wanted.
* Broker callbacks update order state and publish OrderUpdated / FillReceived.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import replace

from kst.core.bus import EventBus
from kst.core.clock import Clock
from kst.core.events import Alert, FillReceived, OrderUpdated
from kst.core.fixedpoint import Price, Qty
from kst.core.logging import get_logger
from kst.core.types import OrderStatus, TimeInForce
from kst.domain.orders import (
    Fill,
    Order,
    QuoteTarget,
    TakeIntent,
    new_client_order_id,
    strategy_from_client_order_id,
)
from kst.execution.broker import Broker, OrderSubmissionUnknownError
from kst.risk.checks import CheckResult, OrderProposal
from kst.risk.engine import RiskEngine

log = get_logger(__name__)


class OrderManager:
    def __init__(
        self,
        broker: Broker,
        risk: RiskEngine,
        bus: EventBus,
        clock: Clock,
        *,
        min_quote_life_ms: int = 1500,
        qty_tolerance: float = 0.2,
        order_ttl_s: int = 600,
        tick_of: Callable[[str], int] | None = None,
        reject_backoff_ms: int = 5000,
    ) -> None:
        self.broker = broker
        self.risk = risk
        self.bus = bus
        self.clock = clock
        self.min_quote_life_ms = min_quote_life_ms
        self.qty_tolerance = qty_tolerance
        self.order_ttl_s = order_ttl_s
        self.tick_of = tick_of or (lambda _t: 100)
        self.orders: dict[str, Order] = {}
        self._ex_to_coid: dict[str, str] = {}
        self._cancelling: set[str] = set()
        # (strategy, ticker, slot) → ms until which a risk-rejected quote slot is not re-placed (no reject spam)
        self.reject_backoff_ms = reject_backoff_ms
        self._slot_backoff: dict[tuple[str, str, str], int] = {}
        self.unknown_fills = 0
        self._seen_fills: set[str] = set()
        self.new_client_id = new_client_order_id
        broker.bind(self)

    # ------------------------------------------------------------------ queries

    def live_orders(self, ticker: str | None = None, strategy: str | None = None) -> list[Order]:
        return [
            o
            for o in self.orders.values()
            if o.is_live
            and (ticker is None or o.ticker == ticker)
            and (strategy is None or o.strategy == strategy)
        ]

    def recent(self, n: int = 100) -> list[Order]:
        return sorted(self.orders.values(), key=lambda o: o.updated_ms or o.created_ms, reverse=True)[:n]

    # ------------------------------------------------------------------ placement

    def restore_seen_fills(self, fill_ids: Iterable[str]) -> None:
        self._seen_fills.update(fill_ids)

    async def submit(
        self, order: Order, *, reference_price: float | None = None, manual: bool = False, replaces: str = ""
    ) -> CheckResult:
        if order.client_order_id in self.orders:
            return CheckResult(False, "DuplicateClientOrderId", "Client order ID is already tracked")
        prop = OrderProposal(
            ticker=order.ticker,
            side=order.side,
            price=order.price,
            qty=order.qty,
            strategy=order.strategy,
            tif=order.tif,
            post_only=order.post_only,
            reference_price=reference_price,
            basket_id=order.basket_id,
            manual=manual,
            flatten=order.strategy == "flatten",
            replaces=replaces,
        )
        res = self.risk.check(prop)
        now = self.clock.now_ms()
        order.created_ms = order.created_ms or now
        order.updated_ms = now
        if not res.ok:
            order.status = OrderStatus.REJECTED
            order.reject_reason = f"risk:{res.check}: {res.detail}"
            if order.slot:
                self._slot_backoff[(order.strategy, order.ticker, order.slot)] = now + self.reject_backoff_ms
            self.bus.publish(OrderUpdated(now, order))
            return res
        order.is_entry = not self.risk.is_risk_reducing(order.ticker, order.side, order.qty)
        self.orders[order.client_order_id] = order
        self.risk.on_order_sent(
            order.ticker, order.client_order_id, order.is_entry, basket=bool(order.basket_id)
        )
        self.bus.publish(OrderUpdated(now, order))
        try:
            await self.broker.place(order)
        except OrderSubmissionUnknownError as exc:
            self.bus.publish(
                Alert(now, "critical", "Order acceptance unknown", f"{order.client_order_id}: {exc}")
            )
            return CheckResult(False, "BrokerUnknown", str(exc))
        except Exception as exc:
            log.exception("broker_place_failed", coid=order.client_order_id)
            self.on_broker_order(order.client_order_id, OrderStatus.REJECTED, reason=f"broker_error: {exc}")
            return CheckResult(False, "Broker", str(exc))
        return res

    async def take(self, intent: TakeIntent, strategy: str, *, reference_price: float | None = None) -> Order:
        o = Order(
            client_order_id=self.new_client_id(strategy),
            ticker=intent.ticker,
            side=intent.side,
            price=intent.limit,
            qty=intent.qty,
            tif=TimeInForce.IOC,
            reduce_only=intent.reduce_only,
            strategy=strategy,
            basket_id=intent.basket_id,
            expected_edge=intent.expected_edge,
        )
        await self.submit(o, reference_price=reference_price)
        log.info(
            "take_sent",
            ticker=o.ticker,
            side=o.side.value,
            limit=o.price / 10_000,
            qty=o.qty / 100,
            strategy=strategy,
            reason=intent.reason,
            edge=round(intent.expected_edge, 4),
            status=o.status.value,
            reject=o.reject_reason or None,
        )
        return o

    async def reconcile(
        self,
        strategy: str,
        ticker: str,
        targets: list[QuoteTarget],
        *,
        reference_price: float | None = None,
        cancel_others: bool = True,
    ) -> None:
        now = self.clock.now_ms()
        live = {o.slot: o for o in self.live_orders(ticker, strategy) if o.slot}
        wanted = {t.slot: t for t in targets}
        tasks = []
        for slot, o in live.items():
            if slot not in wanted and cancel_others:
                tasks.append(self.cancel(o))
        for slot, t in wanted.items():
            cur = live.get(slot)
            if cur is None:
                if now >= self._slot_backoff.get((strategy, ticker, slot), 0):
                    tasks.append(self._place_quote(strategy, t, reference_price))
                continue
            o = cur
            if o.status is not OrderStatus.RESTING or o.client_order_id in self._cancelling:
                continue  # in flight
            same_side = o.side is t.side
            price_moved = o.price != t.price
            qty_moved = abs(o.remaining - t.qty) > self.qty_tolerance * max(t.qty, 1)
            if not (price_moved or qty_moved) and same_side:
                continue
            if (
                now - o.created_ms < self.min_quote_life_ms
                and same_side
                and abs(o.price - t.price) < 2 * self.tick_of(ticker)
            ):
                continue  # respect minimum quote life for small moves (rate limits / churn)
            tasks.append(self._requote(strategy, o, t, reference_price))
        if tasks:
            await asyncio.gather(*tasks)

    async def _place_quote(
        self, strategy: str, t: QuoteTarget, reference_price: float | None, replaces: str = ""
    ) -> None:
        o = Order(
            client_order_id=self.new_client_id(strategy),
            ticker=t.ticker,
            side=t.side,
            price=t.price,
            qty=t.qty,
            tif=TimeInForce.GTC,
            post_only=True,
            strategy=strategy,
            slot=t.slot,
            expected_edge=t.expected_edge,
        )
        await self.submit(o, reference_price=reference_price, replaces=replaces)

    async def _requote(self, strategy: str, o: Order, t: QuoteTarget, reference_price: float | None) -> None:
        if o.side is t.side:
            new_total = Qty(o.filled + t.qty)
            prop = OrderProposal(
                t.ticker,
                t.side,
                t.price,
                t.qty,
                strategy=strategy,
                post_only=True,
                reference_price=reference_price,
                replaces=o.client_order_id,
            )
            res = self.risk.check(prop)
            if not res.ok:
                await self.cancel(o)
                self._slot_backoff[(strategy, t.ticker, t.slot)] = (
                    self.clock.now_ms() + self.reject_backoff_ms
                )
                return
            exp = self.clock.now_ms() // 1000 + self.order_ttl_s
            if await self.broker.amend(o, Price(t.price), new_total, expiration_ts=exp):
                o.expected_edge = t.expected_edge
                o.updated_ms = self.clock.now_ms()
                self.bus.publish(OrderUpdated(o.updated_ms, o))
                return
        await self.cancel(o)
        if o.is_live:
            return  # cancellation was not confirmed; replacing would duplicate exposure
        await self._place_quote(strategy, t, reference_price, replaces=o.client_order_id)

    # ------------------------------------------------------------------ cancels

    async def cancel(self, o: Order) -> None:
        if not o.is_live or o.client_order_id in self._cancelling:
            return
        self._cancelling.add(o.client_order_id)
        try:
            if o.status is OrderStatus.PENDING_NEW and not o.exchange_order_id:
                return  # placement still in flight; reconcile will retry next tick
            o.transition(OrderStatus.PENDING_CANCEL, self.clock.now_ms())
            await self.broker.cancel(o)
        except Exception:
            log.exception("cancel_failed", coid=o.client_order_id)
            if o.status is OrderStatus.PENDING_CANCEL:
                o.transition(OrderStatus.RESTING, self.clock.now_ms())
        finally:
            self._cancelling.discard(o.client_order_id)

    async def cancel_where(self, pred: Callable[[Order], bool]) -> int:
        targets = [o for o in self.live_orders() if pred(o)]
        await asyncio.gather(*(self.cancel(o) for o in targets))
        return len(targets)

    async def cancel_all(self) -> int:
        """Exchange-wide cancel (kill switch) + mark every local live order canceled. Returns #local orders."""
        n = len(self.live_orders())
        await self.broker.cancel_all()  # retain local exposure if exchange cancellation fails
        now = self.clock.now_ms()
        for o in self.live_orders():
            o.status = OrderStatus.CANCELED
            o.updated_ms = now
            self.risk.on_order_done(o.ticker, o.client_order_id, o.filled)
            self.bus.publish(OrderUpdated(now, o))
        return n

    # ------------------------------------------------------------------ BrokerListener

    def on_broker_order(
        self, coid: str, status: OrderStatus, *, exchange_id: str | None = None, reason: str = ""
    ) -> None:
        o = self.orders.get(coid)
        if o is None:
            return
        if exchange_id:
            o.exchange_order_id = exchange_id
            self._ex_to_coid[exchange_id] = coid
        now = self.clock.now_ms()
        try:
            changed = o.transition(status, now)
        except Exception:
            log.warning("order_bad_transition", coid=coid, frm=o.status.value, to=status.value)
            return
        if status is OrderStatus.REJECTED:
            o.reject_reason = reason
            self.risk.on_exchange_reject()
            self.bus.publish(Alert(now, "warning", f"Order rejected: {o.ticker}", reason))
        if status.is_terminal:
            self.risk.on_order_done(o.ticker, coid, o.filled)
        if changed:
            self.bus.publish(OrderUpdated(now, o))

    def on_broker_fill(self, fill: Fill) -> None:
        if fill.fill_id:
            if fill.fill_id in self._seen_fills:
                return
            self._seen_fills.add(fill.fill_id)
        coid = fill.client_order_id or self._ex_to_coid.get(fill.exchange_order_id, "")
        o = self.orders.get(coid)
        if o is None:
            self.unknown_fills += 1
            fill = replace(fill, strategy=strategy_from_client_order_id(coid) if coid else "external")
        else:
            fill = replace(fill, client_order_id=coid, strategy=o.strategy)
            o.apply_fill(fill.qty, fill.price, fill.fee, fill.ts_ms)
            if o.status is OrderStatus.FILLED:
                self.risk.on_order_done(o.ticker, coid, o.filled)
            self.bus.publish(OrderUpdated(fill.ts_ms, o))
        self.bus.publish(FillReceived(fill.ts_ms, fill))

    # ------------------------------------------------------------------ live hygiene

    async def refresh_ttls(self) -> None:
        """Extend expiration of quotes we still want before the exchange-side TTL kills them."""
        now_s = self.clock.now_ms() // 1000
        for o in self.live_orders():
            if (
                o.tif is TimeInForce.GTC
                and o.status is OrderStatus.RESTING
                and o.expiration_ts
                and o.expiration_ts - now_s < self.order_ttl_s / 3
            ):
                await self.broker.amend(o, o.price, o.qty, expiration_ts=now_s + self.order_ttl_s)

    def adopt_open_orders(self, orders: list[Order]) -> None:
        """Startup/reconcile: track exchange orders we don't know (e.g. after a restart)."""
        for o in orders:
            existing = self.orders.get(o.client_order_id)
            if existing is not None and existing.status is OrderStatus.PENDING_NEW:
                self.on_broker_order(o.client_order_id, o.status, exchange_id=o.exchange_order_id)
            if o.client_order_id not in self.orders:
                o.strategy = strategy_from_client_order_id(o.client_order_id)
                self.orders[o.client_order_id] = o
                if o.exchange_order_id:
                    self._ex_to_coid[o.exchange_order_id] = o.client_order_id
