from __future__ import annotations

import asyncio

import pytest
from hypothesis import given
from hypothesis import strategies as st

from kst.core.bus import EventBus
from kst.core.events import Alert, BookUpdated, Event
from kst.core.fixedpoint import (
    FixedPointError,
    Price,
    Qty,
    fmt_cents,
    fmt_usd,
    micros_from_str,
    notional,
    price_from_str,
    price_to_str,
    qty_from_str,
    qty_to_str,
)
from kst.core.types import BookSide, OrderStatus
from kst.domain.fees import FeeModel
from kst.domain.market import PriceBand, PriceGrid
from kst.domain.orderbook import OrderBook
from kst.domain.orders import IllegalTransition, Order, strategy_from_client_order_id, new_client_order_id
from kst.domain.positions import Position

# ------------------------------------------------------------------ fixed point


def test_price_qty_roundtrip() -> None:
    assert price_from_str("0.5600") == 5600
    assert price_from_str("0.56") == 5600
    assert price_from_str("0.0001") == 1
    assert price_to_str(Price(5600)) == "0.5600"
    assert qty_from_str("10.00") == 1000
    assert qty_from_str("1.55") == 155
    assert qty_to_str(Qty(155)) == "1.55"
    assert micros_from_str("0.010000") == 10_000
    with pytest.raises(FixedPointError):
        price_from_str("0.12345")
    with pytest.raises(FixedPointError):
        qty_from_str("1.001")


@given(st.integers(0, 10_000), st.integers(0, 10_000_000))
def test_notional_exact(p: int, q: int) -> None:
    # Price × Qty must equal the exact decimal product in micros.
    from decimal import Decimal

    exact = Decimal(p) / 10_000 * (Decimal(q) / 100) * 1_000_000
    assert notional(Price(p), Qty(q)) == int(exact)


def test_formatting() -> None:
    assert fmt_usd(1_234_560_000) == "$1,234.56"
    assert fmt_usd(-420_000) == "-$0.42"
    assert fmt_cents(Price(5600)) == "56¢"
    assert fmt_cents(Price(550)) == "5.5¢"
    assert fmt_cents(Price(5000)) == "50¢"


# ------------------------------------------------------------------ price grid


def test_tapered_grid() -> None:
    # tapered_deci_cent: 0.001 below $0.10 and above $0.90, 0.01 in between
    g = PriceGrid.of([PriceBand(0, 1000, 10), PriceBand(1000, 9000, 100), PriceBand(9000, 10_000, 10)])
    assert g.min_price == 10 and g.max_price == 9990
    assert g.is_valid(10) and g.is_valid(1000) and g.is_valid(1100) and not g.is_valid(1050)
    assert g.snap_down(1050) == 1000 and g.snap_up(1050) == 1100
    assert g.snap_down(5) is None
    assert g.tick_above(1000) == 1100 and g.tick_below(1000) == 990
    assert PriceGrid.of([PriceBand(0, 1000, 10), PriceBand(1000, 9000, 100), PriceBand(9000, 10_000, 10)]) is g


def test_cent_grid_excludes_bounds() -> None:
    g = PriceGrid.cents()
    assert g.min_price == 100 and g.max_price == 9900
    assert not g.is_valid(0) and not g.is_valid(10_000)


# ------------------------------------------------------------------ order book


def _book() -> OrderBook:
    b = OrderBook("T")
    b.apply_snapshot(bids=[(4000, 1000), (4100, 500)], asks=[(4300, 200), (4500, 1000)], ts_ms=1)
    return b


def test_book_basics() -> None:
    b = _book()
    assert b.best_bid == 4100 and b.best_ask == 4300 and b.spread() == 200
    assert b.mid() == pytest.approx(0.42)
    # microprice leans to the ask (smaller ask size → ask about to be taken)
    assert b.microprice() == pytest.approx((4100 * 200 + 4300 * 500) / 700 / 10_000)
    b.apply_delta(BookSide.ASK, 4300, -200, ts_ms=2)
    assert b.best_ask == 4500
    b.apply_delta(BookSide.BID, 4200, 300, ts_ms=3)
    assert b.best_bid == 4200
    assert b.depth(BookSide.BID, 2) == [(4200, 300), (4100, 500)]


def test_book_sweep() -> None:
    b = _book()
    r = b.sweep(BookSide.BID, 500, limit=4500)
    assert r.filled == 500 and r.worst_price == 4500
    assert r.cost == 4300 * 200 + 4500 * 300
    r2 = b.sweep(BookSide.BID, 500, limit=4300)
    assert r2.filled == 200
    r3 = b.sweep(BookSide.ASK, 600)
    assert r3.filled == 600 and r3.levels[0] == (4100, 500)


# ------------------------------------------------------------------ fees


def test_fee_matches_kalshi_example() -> None:
    # docs: buy 1 contract @ $0.055, model fee $0.00363825 → trade fee $0.003639
    sched = FeeModel().schedule("quadratic")
    assert sched.fee_micros(price_from_str("0.055"), qty_from_str("1"), is_taker=True) == 3639
    assert sched.fee_micros(price_from_str("0.055"), qty_from_str("1"), is_taker=False) == 0


def test_fee_types() -> None:
    fm = FeeModel()
    s = fm.schedule("quadratic_with_maker_fees")
    assert s.maker_coef == pytest.approx(0.0175)
    # 100 contracts @ 50¢: taker 0.07*100*0.25 = $1.75
    assert s.fee_micros(5000, 10_000, is_taker=True) == 1_750_000
    assert fm.schedule("quadratic", 0.5).taker_coef == pytest.approx(0.035)
    assert fm.schedule("quadratic", waived=True).fee_micros(5000, 10_000, is_taker=True) == 0
    assert fm.schedule("flat").maker_coef == pytest.approx(0.07)  # conservative


# ------------------------------------------------------------------ orders


def test_order_fsm() -> None:
    o = Order(client_order_id=new_client_order_id("edge_taker"), ticker="T", side=BookSide.BID, price=Price(5000), qty=Qty(1000))
    assert strategy_from_client_order_id(o.client_order_id) == "edgetaker"
    assert o.transition(OrderStatus.RESTING, 1)
    o.apply_fill(400, 5000, 0, 2)
    assert o.remaining == 600 and o.status is OrderStatus.RESTING
    o.apply_fill(600, 5000, 0, 3)
    assert o.status is OrderStatus.FILLED
    assert not o.transition(OrderStatus.CANCELED, 4)  # late cancel after fill is ignored
    o2 = Order(client_order_id="x", ticker="T", side=BookSide.BID, price=Price(5000), qty=Qty(100), status=OrderStatus.RESTING)
    o2.transition(OrderStatus.CANCELED, 1)
    o3 = Order(client_order_id="y", ticker="T", side=BookSide.BID, price=Price(5000), qty=Qty(100), status=OrderStatus.PENDING_CANCEL)
    with pytest.raises(IllegalTransition):
        o3.transition(OrderStatus.REJECTED, 1)


# ------------------------------------------------------------------ positions


def test_position_long_round_trip() -> None:
    p = Position("T")
    e1 = p.apply_fill(BookSide.BID, 4000, 1000, fee=0, ts_ms=1)  # buy 10 YES @ 40¢
    assert e1.cash_delta == -4_000_000 and p.qty == 1000 and p.exposure == 4_000_000
    assert p.unrealized(4500) == 500_000
    e2 = p.apply_fill(BookSide.ASK, 4500, 1000, fee=10_000, ts_ms=2)  # sell 10 @ 45¢, $0.01 fee
    assert e2.realized == 500_000 and e2.cash_delta == 4_500_000 - 10_000
    assert e2.round_trip_done and e2.round_trip_pnl == 490_000
    assert p.is_flat and p.open_cost == 0


def test_position_no_side_and_flip() -> None:
    p = Position("T")
    e1 = p.apply_fill(BookSide.ASK, 7000, 500, fee=0, ts_ms=1)  # buy 5 NO @ 30¢ (sell YES @ 70¢)
    assert p.qty == -500 and e1.cash_delta == -1_500_000 and p.exposure == 1_500_000
    # buy 8 YES @ 60¢: closes 5 NO (receive 40¢ each), opens 3 YES (pay 60¢ each)
    e2 = p.apply_fill(BookSide.BID, 6000, 800, fee=0, ts_ms=2)
    assert e2.closed == 500 and e2.opened == 300
    assert e2.realized == (7000 - 6000) * 500  # +$0.50
    assert e2.cash_delta == 4000 * 500 - 6000 * 300
    assert p.qty == 300 and p.avg_price == pytest.approx(0.60)


def test_position_settlement() -> None:
    p = Position("T")
    p.apply_fill(BookSide.BID, 3000, 1000, fee=0, ts_ms=1)
    e = p.settle(10_000, 2)
    assert e.cash_delta == 10_000_000 and e.realized == 7_000_000 and p.is_flat
    q = Position("U")
    q.apply_fill(BookSide.ASK, 2000, 1000, fee=0, ts_ms=1)  # 10 NO @ 80¢
    e = q.settle(0, 2)  # NO wins
    assert e.cash_delta == 10_000_000 and e.realized == 2_000_000


# ------------------------------------------------------------------ bus


async def test_bus_sync_and_stream() -> None:
    bus = EventBus()
    seen: list[Event] = []
    bus.subscribe(BookUpdated, seen.append)
    allsub = bus.stream(Event, maxsize=2)
    bus.publish(BookUpdated(1, "A"))
    bus.publish(Alert(2, "info", "x"))
    bus.publish(BookUpdated(3, "B"))
    assert [e.ts_ms for e in seen] == [1, 3]
    assert allsub.dropped == 1
    got = [await asyncio.wait_for(allsub.get(), 1), await asyncio.wait_for(allsub.get(), 1)]
    assert [e.ts_ms for e in got] == [2, 3]


def test_bus_handler_errors_isolated() -> None:
    bus = EventBus()

    def bad(_: Event) -> None:
        raise RuntimeError("boom")

    ok: list[Event] = []
    bus.subscribe(Event, bad)
    bus.subscribe(BookUpdated, ok.append)
    bus.publish(BookUpdated(1, "A"))
    assert len(ok) == 1
