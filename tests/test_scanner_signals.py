from __future__ import annotations

import json
from pathlib import Path

import pytest

from kst.core.fixedpoint import Price, Qty
from kst.core.types import BookSide, MarketStatus, StrikeType
from kst.domain.fees import FeeModel
from kst.domain.market import Event, Market
from kst.kalshi.parse import parse_event
from kst.scanner.signals import detect_event_arbs, is_exhaustive, taker_edge

FEES = FeeModel().schedule("quadratic")


def mk(t: str, st: StrikeType, floor: float | None, cap: float | None, bid: int | None, ask: int | None, qty: int = 1000) -> Market:
    return Market(
        ticker=t, event_ticker="E", series_ticker="S", status=MarketStatus.ACTIVE, strike_type=st,
        floor_strike=floor, cap_strike=cap,
        yes_bid=None if bid is None else Price(bid), yes_ask=None if ask is None else Price(ask),
        yes_bid_size=Qty(qty), yes_ask_size=Qty(qty),
    )


def top(m: Market):  # noqa: ANN201
    return m.yes_bid, m.yes_bid_size, m.yes_ask, m.yes_ask_size


def test_taker_edge() -> None:
    e = taker_edge(0.60, Price(5000), Price(5200), FEES, rounding=0.0)
    assert e.buy == pytest.approx(0.60 - 0.52 - 0.07 * 0.52 * 0.48)
    assert e.sell == pytest.approx(0.50 - 0.60 - 0.07 * 0.25)
    best, side = e.best
    assert side is BookSide.BID and best == e.buy


def test_exhaustive_integer_and_continuous() -> None:
    weather = [
        mk("a", StrikeType.LESS, None, 62, None, None), mk("b", StrikeType.BETWEEN, 62, 63, None, None),
        mk("c", StrikeType.BETWEEN, 64, 65, None, None), mk("d", StrikeType.GREATER, 65, None, None, None),
    ]
    assert is_exhaustive(weather)
    gap = [*weather[:2], mk("c", StrikeType.BETWEEN, 66, 67, None, None), mk("d", StrikeType.GREATER, 67, None, None, None)]
    assert not is_exhaustive(gap)
    crypto = [
        mk("a", StrikeType.LESS, None, 100, None, None), mk("b", StrikeType.BETWEEN, 100, 199.99, None, None),
        mk("c", StrikeType.BETWEEN, 200, 299.99, None, None), mk("d", StrikeType.GREATER, 299.99, None, None, None),
    ]
    assert is_exhaustive(crypto)
    assert not is_exhaustive(crypto[:3])  # missing upper tail


def test_real_weather_event_is_exhaustive() -> None:
    d = json.loads((Path(__file__).parent / "fixtures" / "event_highny.json").read_text())
    _, markets = parse_event(d)
    assert is_exhaustive(markets)


def test_sell_all_arb() -> None:
    ev = Event("E", "S", mutually_exclusive=True, market_tickers=["a", "b", "c"])
    ms = [mk("a", StrikeType.CUSTOM, None, None, 4200, 4400), mk("b", StrikeType.CUSTOM, None, None, 3600, 3800),
          mk("c", StrikeType.CUSTOM, None, None, 3200, 3400, qty=500)]
    arbs = detect_event_arbs(ev, ms, top, lambda _: FEES, margin=0.005, rounding=0.0)
    sell = [a for a in arbs if a.kind == "sell_all"]
    assert len(sell) == 1
    fee_sum = sum(0.07 * p * (1 - p) for p in (0.42, 0.36, 0.32))
    assert sell[0].edge == pytest.approx(1.10 - 1 - fee_sum)
    # Σ bids = 1.05 is NOT enough once fees are paid
    ms[0].yes_bid = Price(3700)
    assert not [a for a in detect_event_arbs(ev, ms, top, lambda _: FEES, margin=0.005, rounding=0.0) if a.kind == "sell_all"]
    assert sell[0].max_baskets == 500 and all(leg.side is BookSide.ASK for leg in sell[0].legs)
    # No buy_all: custom strikes aren't provably exhaustive
    assert not [a for a in arbs if a.kind == "buy_all"]


def test_buy_all_arb_requires_exhaustive() -> None:
    ev = Event("E", "S", mutually_exclusive=True)
    ms = [mk("a", StrikeType.LESS, None, 62, 1000, 1500), mk("b", StrikeType.BETWEEN, 62, 63, 2500, 3000),
          mk("c", StrikeType.GREATER, 63, None, 4000, 4500)]
    arbs = detect_event_arbs(ev, ms, top, lambda _: FEES, margin=0.0, rounding=0.0)
    buy = [a for a in arbs if a.kind == "buy_all"]
    assert len(buy) == 1 and buy[0].edge == pytest.approx(1 - 0.90 - sum(0.07 * p * (1 - p) for p in (0.15, 0.30, 0.45)))


def test_ladder_arb() -> None:
    ev = Event("E", "S", mutually_exclusive=False)
    # P(>100) must be ≥ P(>110); here bid(>110)=0.55 > ask(>100)=0.45 → buy YES >100, sell YES >110
    ms = [mk("k100", StrikeType.GREATER, 100, None, 4300, 4500), mk("k110", StrikeType.GREATER, 110, None, 5500, 5700),
          mk("k120", StrikeType.GREATER, 120, None, 2000, 2200)]
    arbs = detect_event_arbs(ev, ms, top, lambda _: FEES, margin=0.0, rounding=0.0)
    lad = [a for a in arbs if a.kind == "ladder"]
    assert len(lad) == 1
    legs = {leg.ticker: leg for leg in lad[0].legs}
    assert legs["k100"].side is BookSide.BID and legs["k110"].side is BookSide.ASK
    assert lad[0].edge == pytest.approx(0.55 - 0.45 - 0.07 * 0.55 * 0.45 * 2)


def test_no_arb_in_coherent_event() -> None:
    ev = Event("E", "S", mutually_exclusive=True)
    ms = [mk("a", StrikeType.LESS, None, 62, 2900, 3100), mk("b", StrikeType.BETWEEN, 62, 63, 3900, 4100),
          mk("c", StrikeType.GREATER, 63, None, 2900, 3100)]
    assert detect_event_arbs(ev, ms, top, lambda _: FEES, margin=0.005) == []
