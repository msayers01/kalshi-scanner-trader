"""Production OMS/RiskEngine/LiveBroker with signed, intercepted REST and real WS dispatch.

No exchange hosts are contacted. See docs/EXECUTION_VALIDATION.md for remaining demo evidence.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from unittest.mock import AsyncMock

import httpx
import pytest
import respx
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from kst.core.events import Alert, FillReceived
from kst.core.fixedpoint import Price, Qty
from kst.core.types import Actor, BookSide, EngineState, Mode, OrderStatus, TimeInForce
from kst.domain.orders import Order, QuoteTarget
from kst.execution.live import LiveBroker
from kst.execution.oms import OrderManager
from kst.kalshi.auth import KalshiSigner
from kst.kalshi.ratelimit import KalshiRateLimiter
from kst.kalshi.rest import KalshiApiError, KalshiRest
from kst.kalshi.ws import KalshiWs
from tests.conftest import Harness

BASE = "https://execution.test/trade-api/v2"
ORDERS = f"{BASE}/portfolio/events/orders"


@dataclass
class Execution:
    h: Harness
    rest: KalshiRest
    broker: LiveBroker
    ws: KalshiWs
    router: respx.MockRouter
    create: respx.Route


@pytest.fixture
async def execution(h: Harness):
    signer = KalshiSigner("ephemeral-test-key", Ed25519PrivateKey.generate())
    rest = KalshiRest(BASE, signer, KalshiRateLimiter(100_000, 100_000))
    ws = KalshiWs("ws://unused.invalid", signer, {})
    broker = LiveBroker(rest, ws, h.clock)
    h.oms = OrderManager(broker, h.risk, h.bus, h.clock, min_quote_life_ms=0)
    h.risk.mode = Mode.DEMO
    h.risk.bind(h.oms, h.portfolio)
    h.control.oms = h.oms
    h.add_market("M")
    h.set_book("M", [(0.38, 100)], [(0.42, 100)])
    with respx.mock(assert_all_called=False) as router:
        create = router.post(ORDERS).respond(
            201, json={"order_id": "ex-1", "fill_count": "0.00", "remaining_count": "1.00"}
        )
        await broker.start()
        try:
            yield Execution(h, rest, broker, ws, router, create)
        finally:
            await broker.stop()
            await rest.aclose()
            await h.md.rest.aclose()


def order(*, tif=TimeInForce.GTC, qty=100, side=BookSide.BID) -> Order:
    return Order("kst-mm-test", "M", side, Price(4000), Qty(qty), tif=tif, strategy="market_maker")


def fill(*, fill_id="f-1", qty="1.00", coid="kst-mm-test", side="bid") -> dict:
    return {
        "trade_id": fill_id,
        "order_id": "ex-1",
        "client_order_id": coid,
        "ticker": "M",
        "book_side": side,
        "yes_price_dollars": "0.4000",
        "count_fp": qty,
        "fee_cost": "0.010000",
        "ts_ms": 1_792_300_000_000,
    }


def dispatch(e: Execution, kind: str, body: dict) -> None:
    e.ws._dispatch(json.dumps({"type": kind, "msg": body}))


async def test_gtc_has_ttl_and_exchange_safety_flags(execution: Execution) -> None:
    e = execution
    o = order()
    o.post_only = True
    assert (await e.h.oms.submit(o)).ok
    body = json.loads(e.create.calls.last.request.content)
    assert o.status is OrderStatus.RESTING and o.exchange_order_id == "ex-1"
    assert body["post_only"] and body["cancel_order_on_pause"]
    assert body["expiration_time"] == e.h.clock.now_ms() // 1000 + 600
    assert body["count"] == "1.00" and body["price"] == "0.4000"


async def test_risk_rejection_never_reaches_exchange(execution: Execution) -> None:
    e = execution
    e.h.state.set(EngineState.PAUSED, Actor.SYSTEM, "validation")
    o = order()
    result = await e.h.oms.submit(o)
    assert not result.ok and o.status is OrderStatus.REJECTED
    assert not e.create.called


async def test_exchange_rejection_is_terminal(execution: Execution) -> None:
    e = execution
    e.create.respond(400, json={"code": "invalid_price", "message": "off grid"})
    o = order()
    await e.h.oms.submit(o)
    assert o.status is OrderStatus.REJECTED and "invalid_price" in o.reject_reason
    assert not e.h.oms.live_orders()


@pytest.mark.parametrize("failure", ["conflict", "transport", "server", "bad_ack", "bad_counts"])
async def test_uncertain_create_retains_exposure(execution: Execution, monkeypatch, failure: str) -> None:
    e = execution
    if failure == "bad_ack":
        e.create.respond(201, json={})
    elif failure == "bad_counts":
        e.create.respond(201, json={"order_id": "ex-1", "fill_count": "invalid"})
    else:
        error = (
            httpx.ReadTimeout("lost acknowledgement")
            if failure == "transport"
            else KalshiApiError(409 if failure == "conflict" else 503, "unknown", "retry", "/orders")
        )
        monkeypatch.setattr(e.rest, "create_order", AsyncMock(side_effect=error))
    alerts = []
    e.h.bus.subscribe(Alert, alerts.append)
    o = order()
    result = await e.h.oms.submit(o)
    assert not result.ok and result.check == "BrokerUnknown"
    assert o.status is OrderStatus.PENDING_NEW and e.h.oms.live_orders() == [o]
    assert e.h.risk._reservations["M"] == {o.client_order_id}
    assert alerts[-1].level == "critical"
    # An exchange resting-order snapshot resolves the missing acknowledgement without a new create.
    adopted = order()
    adopted.status, adopted.exchange_order_id = OrderStatus.RESTING, "ex-recovered"
    e.h.oms.adopt_open_orders([adopted])
    assert o.status is OrderStatus.RESTING and o.exchange_order_id == "ex-recovered"


@pytest.mark.parametrize("side", [BookSide.BID, BookSide.ASK])
async def test_ws_and_rest_duplicates_account_exactly_once(execution: Execution, side: BookSide) -> None:
    e = execution
    o = order(side=side)
    await e.h.oms.submit(o)
    received = []
    e.h.bus.subscribe(FillReceived, received.append)
    before = e.h.portfolio.cash
    raw = fill(coid="", side=side.value)  # recover attribution from exchange id
    dispatch(e, "fill", raw)
    dispatch(e, "fill", raw)
    rest_raw = {**raw, "fill_id": "different-rest-fill-id"}
    e.router.get(f"{BASE}/portfolio/fills").respond(200, json={"fills": [rest_raw], "cursor": ""})
    await e.broker.poll_fills()
    assert len(received) == 1 and received[0].fill.strategy == "market_maker"
    assert o.status is OrderStatus.FILLED and o.filled == 100 and o.fees == 10_000
    expected_qty, expected_cost = (100, 400_000) if side is BookSide.BID else (-100, 600_000)
    assert e.h.portfolio.positions["M"].qty == expected_qty
    assert e.h.portfolio.cash == before - expected_cost - 10_000


async def test_partial_fill_then_cancel_and_late_fill(execution: Execution) -> None:
    e = execution
    o = order(qty=200)
    await e.h.oms.submit(o)
    dispatch(e, "fill", fill(qty="0.50"))
    e.router.delete(f"{ORDERS}/ex-1").respond(200, json={"reduced_by": "1.50"})
    await e.h.oms.cancel(o)
    assert o.status is OrderStatus.CANCELED and o.filled == 50
    dispatch(e, "fill", fill(fill_id="f-2", qty="0.25"))  # cancel/fill race
    dispatch(e, "user_order", {"order_id": "ex-1", "status": "resting"})
    assert o.status is OrderStatus.CANCELED and o.filled == 75
    assert e.h.portfolio.positions["M"].qty == 75


@pytest.mark.parametrize("qty", ["0.00", "0.40", "1.00"])
async def test_ioc_finalization_with_rest_fill_fallback(execution: Execution, qty: str) -> None:
    e = execution
    e.create.respond(201, json={"order_id": "ex-1", "fill_count": qty, "remaining_count": "0.00"})
    raw = fill(qty=qty)
    e.router.get(f"{BASE}/portfolio/fills").respond(200, json={"fills": [raw], "cursor": ""})
    o = order(tif=TimeInForce.IOC)
    o.reduce_only = True
    await e.h.oms.submit(o)
    body = json.loads(e.create.calls.last.request.content)
    assert "expiration_time" not in body and body["reduce_only"]
    if qty != "0.00":
        await asyncio.wait_for(asyncio.gather(*e.broker._ioc_tasks), timeout=2)
    assert o.filled == round(float(qty) * 100)
    assert o.status is (OrderStatus.FILLED if qty == "1.00" else OrderStatus.CANCELED)


async def test_ioc_ws_fill_before_rest_ack(execution: Execution) -> None:
    e = execution

    def response(request):
        dispatch(e, "fill", fill())
        return httpx.Response(201, json={"order_id": "ex-1", "fill_count": "1.00", "remaining_count": "0.00"})

    e.create.mock(side_effect=response)
    o = order(tif=TimeInForce.IOC)
    await e.h.oms.submit(o)
    await asyncio.gather(*e.broker._ioc_tasks)
    assert o.status is OrderStatus.FILLED and o.filled == 100


async def test_shutdown_cancels_pending_ioc_work(execution: Execution) -> None:
    e = execution
    e.create.respond(201, json={"order_id": "ex-1", "fill_count": "1.00", "remaining_count": "0.00"})
    await e.h.oms.submit(order(tif=TimeInForce.IOC))
    tasks = list(e.broker._ioc_tasks)
    await e.broker.stop()
    assert tasks and all(t.done() for t in tasks) and not e.broker._ioc_tasks


async def test_amend_uses_filled_plus_remaining_and_tracks_new_id(execution: Execution) -> None:
    e = execution
    o = order(qty=200)
    o.post_only, o.slot = True, "bid"
    await e.h.oms.submit(o)
    dispatch(e, "fill", fill(qty="0.50"))
    amend = e.router.post(f"{ORDERS}/ex-1/amend").respond(200, json={"order_id": "ex-2"})
    await e.h.oms.reconcile(
        "market_maker", "M", [QuoteTarget("M", "bid", BookSide.BID, Price(3900), Qty(100))]
    )
    body = json.loads(amend.calls.last.request.content)
    assert body["count"] == "1.50" and body["price"] == "0.3900"
    assert o.qty == 150 and o.remaining == 100 and o.exchange_order_id == "ex-2"
    cancel = e.router.delete(f"{ORDERS}/ex-2").respond(200, json={})
    await e.h.oms.cancel(o)
    assert cancel.called and cancel.calls.last.request.url.params["market_ticker"] == "M"


async def test_failed_cancel_cannot_create_replacement(execution: Execution) -> None:
    e = execution
    o = order()
    o.post_only, o.slot = True, "bid"
    await e.h.oms.submit(o)
    e.router.post(f"{ORDERS}/ex-1/amend").respond(400, json={"code": "cannot_amend"})
    e.router.delete(f"{ORDERS}/ex-1").respond(403, json={"code": "forbidden"})
    await e.h.oms.reconcile(
        "market_maker", "M", [QuoteTarget("M", "bid", BookSide.BID, Price(3900), Qty(100))]
    )
    assert e.create.call_count == 1 and o.status is OrderStatus.RESTING


@pytest.mark.parametrize("status", [404, 403])
async def test_cancel_not_found_or_failure(execution: Execution, status: int) -> None:
    e = execution
    o = order()
    await e.h.oms.submit(o)
    e.router.delete(f"{ORDERS}/ex-1").respond(status, json={"code": "cancel_error"})
    await e.h.oms.cancel(o)
    assert o.status is (OrderStatus.CANCELED if status == 404 else OrderStatus.RESTING)


@pytest.mark.parametrize("status", [200, 403])
async def test_kill_preserves_exposure_when_cancel_all_fails(execution: Execution, status: int) -> None:
    e = execution
    o = order()
    await e.h.oms.submit(o)
    e.router.delete(ORDERS).respond(status, json={})
    if status == 403:
        with pytest.raises(KalshiApiError):
            await e.h.control.kill(Actor.SYSTEM, "validation")
        assert o.is_live and e.h.risk._reservations["M"]
    else:
        assert (await e.h.control.kill(Actor.SYSTEM, "validation"))["cancelled"] == 1
        assert o.status is OrderStatus.CANCELED
    assert e.h.state.get() is EngineState.KILLED and not e.h.control.autotrade


@pytest.mark.parametrize("result", ["created", "missing_id", "denied"])
async def test_order_group_setup_is_required_when_configured(execution: Execution, result: str) -> None:
    e = execution
    e.broker.order_group_limit = 10
    e.router.post(f"{BASE}/portfolio/order_groups/create").respond(
        403 if result == "denied" else 200,
        json={"order_group_id": "group-1"} if result == "created" else {},
    )
    if result != "created":
        with pytest.raises(RuntimeError):
            await e.broker.start()
        assert not e.create.called
    else:
        await e.broker.start()
        await e.h.oms.submit(order())
        assert json.loads(e.create.calls.last.request.content)["order_group_id"] == "group-1"


async def test_restart_snapshot_adopts_order_and_routes_fill_without_client_id(execution: Execution) -> None:
    e = execution
    e.router.get(f"{BASE}/portfolio/balance").respond(200, json={"balance": 100_000})
    e.router.get(f"{BASE}/portfolio/positions").respond(200, json={"market_positions": [], "cursor": ""})
    e.router.get(f"{BASE}/portfolio/orders").respond(
        200,
        json={
            "orders": [
                {
                    "order_id": "ex-1",
                    "client_order_id": "kst-mm-test",
                    "ticker": "M",
                    "book_side": "bid",
                    "status": "resting",
                    "yes_price_dollars": "0.4000",
                    "initial_count_fp": "1.00",
                    "fill_count_fp": "0.00",
                    "remaining_count_fp": "1.00",
                }
            ],
            "cursor": "",
        },
    )
    snap = await e.broker.snapshot()
    e.h.portfolio.sync_from_exchange(snap.balance_cents, snap.positions)
    e.h.oms.adopt_open_orders(snap.open_orders)
    dispatch(e, "fill", fill(coid=""))
    o = e.h.oms.orders["kst-mm-test"]
    assert o.status is OrderStatus.FILLED and o.strategy == "market_maker"
    assert e.h.oms.unknown_fills == 0 and e.h.portfolio.positions["M"].qty == 100


async def test_user_order_update_teaches_exchange_id_for_later_fill(execution: Execution) -> None:
    e = execution
    o = order(qty=200)
    await e.h.oms.submit(o)
    dispatch(
        e, "user_order", {"client_order_id": o.client_order_id, "order_id": "new-id", "status": "resting"}
    )
    raw = {**fill(coid=""), "order_id": "new-id"}
    dispatch(e, "fill", raw)
    assert o.exchange_order_id == "new-id" and o.filled == 100
    assert e.h.oms.unknown_fills == 0


async def test_rest_fill_first_then_ws_with_different_id_is_one_fill(execution: Execution) -> None:
    e = execution
    o = order()
    await e.h.oms.submit(o)
    raw = {**fill(), "fill_id": "rest-fill-id"}
    e.router.get(f"{BASE}/portfolio/fills").respond(200, json={"fills": [raw], "cursor": ""})
    await e.broker.poll_fills()
    dispatch(e, "fill", fill())
    assert o.filled == 100 and o.fees == 10_000


async def test_same_trade_for_two_orders_does_not_drop_second_fill(execution: Execution) -> None:
    e = execution
    received = []
    e.h.bus.subscribe(FillReceived, received.append)
    dispatch(e, "fill", fill())
    dispatch(e, "fill", {**fill(), "order_id": "ex-2", "client_order_id": "second", "book_side": "ask"})
    assert len(received) == 2 and received[0].fill.fill_id != received[1].fill.fill_id
    assert e.h.portfolio.positions["M"].qty == 0


async def test_missing_ioc_fills_keep_exposure_reserved(execution: Execution, monkeypatch) -> None:
    e = execution
    o = order(tif=TimeInForce.IOC)
    e.create.respond(201, json={"order_id": "ex-1", "fill_count": "1.00", "remaining_count": "0.00"})
    poll = AsyncMock()
    monkeypatch.setattr(e.broker, "poll_fills", poll)
    monkeypatch.setattr("kst.execution.live._IOC_FILL_DELAYS", (0, 0, 0, 0))
    await e.h.oms.submit(o)
    await asyncio.gather(*e.broker._ioc_tasks)
    assert poll.await_count == 4 and o.is_live
    assert e.h.risk._reservations["M"] == {o.client_order_id}


async def test_rest_recovery_failure_does_not_advance_cursor(execution: Execution, monkeypatch) -> None:
    e = execution
    before = e.broker._last_fill_poll_s
    monkeypatch.setattr(e.rest, "fills", AsyncMock(side_effect=RuntimeError("temporarily unavailable")))
    await e.broker.poll_fills()
    assert e.broker._last_fill_poll_s == before


async def test_ttl_refresh_amends_existing_order(execution: Execution) -> None:
    e = execution
    o = order()
    await e.h.oms.submit(o)
    initial = o.expiration_ts
    e.h.clock.advance(500_000)
    amend = e.router.post(f"{ORDERS}/ex-1/amend").respond(200, json={"order_id": "ex-1"})
    await e.h.oms.refresh_ttls()
    assert e.create.call_count == 1 and o.expiration_ts > initial
    assert json.loads(amend.calls.last.request.content)["expiration_time"] == o.expiration_ts
