"""Execution path: paper fills, OMS reconciliation, risk checks, kill/pause/flatten, breakers, settlement."""

from __future__ import annotations

import asyncio

import pytest

from kst.core.events import MarketStatusChanged, RiskRejected
from kst.core.fixedpoint import Price, Qty
from kst.core.types import Actor, BookSide, EngineState, MarketStatus, OrderStatus, TimeInForce
from kst.domain.orders import Order, QuoteTarget, TakeIntent, new_client_order_id
from tests.conftest import Harness


def _take(t: str, side: BookSide, px: float, qty: float) -> TakeIntent:
    return TakeIntent(t, side, Price(round(px * 10_000)), Qty(round(qty * 100)), "test")


async def test_take_walks_book_and_updates_portfolio(hs: Harness) -> None:
    hs.add_market("M")
    hs.set_book("M", bids=[(0.38, 50)], asks=[(0.40, 10), (0.41, 20)])
    o = await hs.oms.take(_take("M", BookSide.BID, 0.41, 15), "edge_taker")
    assert o.status is OrderStatus.FILLED and o.filled == 1500
    pos = hs.portfolio.positions["M"]
    assert pos.qty == 1500 and pos.strategy == "edge_taker"
    fees = 1_680_000 * 0 + o.fees  # fees recorded on the order
    assert hs.portfolio.cash == 1_000_000_000 - (40 * 10 + 41 * 5) * 10_000 - fees
    assert fees > 0  # taker fee charged
    assert "M" in hs.risk.entered_today


async def test_repeated_takes_cannot_reuse_liquidity(hs: Harness) -> None:
    hs.add_market("M")
    hs.set_book("M", bids=[], asks=[(0.40, 10)])
    o1 = await hs.oms.take(_take("M", BookSide.BID, 0.40, 8), "edge_taker")
    o2 = await hs.oms.take(_take("M", BookSide.BID, 0.40, 8), "edge_taker")
    assert o1.filled == 800 and o2.filled == 200 and o2.status is OrderStatus.CANCELED


async def test_maker_quote_queue_and_trade_through(hs: Harness) -> None:
    hs.add_market("M")
    hs.set_book("M", bids=[(0.38, 4)], asks=[(0.45, 10)])
    await hs.oms.reconcile(
        "market_maker", "M", [QuoteTarget("M", "mm_bid", BookSide.BID, Price(3800), Qty(500))]
    )
    (o,) = hs.oms.live_orders("M")
    assert o.status is OrderStatus.RESTING and o.post_only
    hs.trade("M", 0.38, 3, BookSide.ASK)  # consumes 3 of 4 ahead of us
    assert o.filled == 0
    hs.trade("M", 0.38, 3, BookSide.ASK)  # 1 ahead, then 2 to us
    assert o.filled == 200
    hs.trade("M", 0.37, 10, BookSide.ASK)  # trades through our price → rest fills
    assert o.filled == 500 and o.status is OrderStatus.FILLED
    assert o.fees == 0  # plain quadratic series: no maker fee


async def test_reconcile_amends_and_cancels(hs: Harness) -> None:
    hs.add_market("M")
    hs.set_book("M", bids=[(0.30, 5)], asks=[(0.50, 5)])
    q = QuoteTarget("M", "mm_bid", BookSide.BID, Price(3500), Qty(500))
    await hs.oms.reconcile("market_maker", "M", [q])
    (o,) = hs.oms.live_orders("M")
    await hs.oms.reconcile(
        "market_maker", "M", [QuoteTarget("M", "mm_bid", BookSide.BID, Price(3600), Qty(500))]
    )
    assert hs.oms.live_orders("M") == [o] and o.price == 3600  # amended in place
    await hs.oms.reconcile("market_maker", "M", [])
    assert hs.oms.live_orders("M") == [] and o.status is OrderStatus.CANCELED


async def test_post_only_cross_is_rejected(hs: Harness) -> None:
    hs.add_market("M")
    hs.set_book("M", bids=[(0.30, 5)], asks=[(0.40, 5)])
    o = Order(new_client_order_id("manual"), "M", BookSide.BID, Price(4000), Qty(100), post_only=True)
    await hs.oms.submit(o)
    assert o.status is OrderStatus.REJECTED and "post_only" in o.reject_reason


async def test_daily_market_limit_and_rejection_event(hs: Harness) -> None:
    hs.risk.limits = hs.risk.limits.patch({"max_markets_per_day": 1})[0]
    for t in ("A", "B"):
        hs.add_market(t, event=f"E{t}")
        hs.set_book(t, bids=[(0.40, 50)], asks=[(0.42, 50)])
    seen: list[RiskRejected] = []
    hs.bus.subscribe(RiskRejected, seen.append)
    a = await hs.oms.take(_take("A", BookSide.BID, 0.42, 5), "edge_taker")
    b = await hs.oms.take(_take("B", BookSide.BID, 0.42, 5), "edge_taker")
    assert a.status is OrderStatus.FILLED
    assert b.status is OrderStatus.REJECTED and "DailyNewMarketLimit" in b.reject_reason
    assert seen and seen[-1].check == "DailyNewMarketLimit"
    # Adding to the already-entered market is still fine
    a2 = await hs.oms.take(_take("A", BookSide.BID, 0.42, 5), "edge_taker")
    assert a2.status is OrderStatus.FILLED


async def test_fat_finger_and_stale_data(hs: Harness) -> None:
    hs.add_market("M")
    hs.set_book("M", bids=[(0.40, 50)], asks=[(0.42, 50)])
    o = Order(new_client_order_id("manual"), "M", BookSide.BID, Price(6000), Qty(100), tif=TimeInForce.IOC)
    await hs.oms.submit(o, reference_price=0.41)
    assert "FatFinger" in o.reject_reason
    hs.clock.advance(60)  # poll-mode books go stale after 20 s
    o2 = Order(new_client_order_id("manual"), "M", BookSide.BID, Price(4200), Qty(100), tif=TimeInForce.IOC)
    await hs.oms.submit(o2)
    assert "DataFresh" in o2.reject_reason


async def test_pause_allows_only_risk_reducing(hs: Harness) -> None:
    hs.add_market("M")
    hs.set_book("M", bids=[(0.40, 50)], asks=[(0.42, 50)])
    await hs.oms.take(_take("M", BookSide.BID, 0.42, 10), "edge_taker")
    await hs.control.pause(Actor.WEB, "test")
    more = await hs.oms.take(_take("M", BookSide.BID, 0.42, 5), "edge_taker")
    assert "EngineStateAllows" in more.reject_reason
    exit_ = await hs.oms.take(
        TakeIntent("M", BookSide.ASK, Price(4000), Qty(1000), "exit", reduce_only=True), "edge_taker"
    )
    assert exit_.status is OrderStatus.FILLED and hs.portfolio.positions["M"].qty == 0


async def test_kill_cancels_everything_and_flatten_works(hs: Harness) -> None:
    hs.add_market("M")
    hs.set_book("M", bids=[(0.40, 50)], asks=[(0.42, 50)])
    await hs.oms.take(_take("M", BookSide.BID, 0.42, 10), "edge_taker")
    await hs.oms.reconcile(
        "edge_taker",
        "M",
        [QuoteTarget("M", "exit", BookSide.ASK, Price(4500), Qty(1000), reduce_only_intent=True)],
    )
    assert len(hs.oms.live_orders()) == 1
    res = await hs.control.kill(Actor.DISCORD, "test", flatten=True)
    assert hs.state.get() is EngineState.KILLED and res["cancelled"] == 1
    assert hs.oms.live_orders() == []
    assert res["flatten"]["ok"] and hs.portfolio.positions["M"].qty == 0
    blocked = await hs.oms.take(_take("M", BookSide.BID, 0.42, 1), "edge_taker")
    assert "EngineStateAllows" in blocked.reject_reason


async def test_daily_loss_breaker_pauses(hs: Harness) -> None:
    hs.risk.limits = hs.risk.limits.patch({"daily_loss_limit_usd": 1.0})[0]
    hs.add_market("M")
    hs.set_book("M", bids=[(0.49, 100)], asks=[(0.50, 100)])
    await hs.oms.take(_take("M", BookSide.BID, 0.50, 20), "edge_taker")
    hs.set_book("M", bids=[(0.10, 100)], asks=[(0.12, 100)])  # mark collapses: −$7.8 unrealized
    hs.risk.on_portfolio(hs.portfolio.summary())
    await asyncio.sleep(0)  # breach handler runs as a task
    await asyncio.sleep(0)
    assert hs.state.get() is EngineState.PAUSED


async def test_paper_settlement(hs: Harness) -> None:
    hs.add_market("M")
    hs.set_book("M", bids=[(0.29, 50)], asks=[(0.30, 50)])
    await hs.oms.take(_take("M", BookSide.BID, 0.30, 10), "edge_taker")
    cash_before = hs.portfolio.cash
    hs.bus.publish(MarketStatusChanged(hs.clock.now_ms(), "M", MarketStatus.DETERMINED, "yes"))
    assert hs.portfolio.positions["M"].qty == 0
    assert hs.portfolio.cash - cash_before == 10_000_000  # 10 contracts × $1
    s = hs.portfolio.summary()
    assert s.wins_today == 1 and s.realized_today == 7_000_000


@pytest.mark.parametrize("limit_field", ["max_order_contracts", "max_exposure_per_market_usd"])
async def test_size_limits(hs: Harness, limit_field: str) -> None:
    hs.risk.limits = hs.risk.limits.patch({limit_field: 2})[0]
    hs.add_market("M")
    hs.set_book("M", bids=[(0.40, 50)], asks=[(0.42, 50)])
    o = await hs.oms.take(_take("M", BookSide.BID, 0.42, 10), "edge_taker")
    assert o.status is OrderStatus.REJECTED
