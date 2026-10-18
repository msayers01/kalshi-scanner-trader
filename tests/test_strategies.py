from __future__ import annotations

from dataclasses import replace

import pytest

from kst.config import EdgeTakerCfg, LLMCfg, MarketMakerCfg
from kst.core.bus import EventBus
from kst.core.clock import ManualClock
from kst.core.fixedpoint import Price, Qty
from kst.core.types import BookSide, MarketStatus, StrikeType
from kst.domain.fees import FeeModel
from kst.domain.market import Market
from kst.domain.orderbook import OrderBook
from kst.domain.positions import Position
from kst.llm.gate import LLMGate, state_bucket
from kst.llm.packet import Candidate, build_packet, parse_reply
from kst.llm.providers import LLMResult
from kst.pricing.base import FairValue, Posterior
from kst.strategies.base import StrategyContext
from kst.strategies.edge_taker import EdgeTaker
from kst.strategies.exits import plan_exit
from kst.strategies.market_maker import MarketMaker
from kst.strategies.sizing import kelly_contracts, kelly_fraction, uncertainty_shrink

FEES = FeeModel().schedule("quadratic")
T = 1_800_000_000_000


def _market() -> Market:
    return Market(
        ticker="M",
        event_ticker="E-1",
        series_ticker="S",
        status=MarketStatus.ACTIVE,
        strike_type=StrikeType.GREATER,
        close_time_ms=T + 3_600_000,
    )


def _book(bids: list[tuple[int, int]], asks: list[tuple[int, int]]) -> OrderBook:
    b = OrderBook("M")
    b.apply_snapshot(bids, asks, T)
    return b


def _ctx(q: float, sd: float, book: OrderBook, pos: int = 0, allow: bool = True) -> StrategyContext:
    p = Position("M", qty=Qty(pos), open_cost=0) if pos else None  # type: ignore[arg-type]
    return StrategyContext(
        now_ms=T,
        market=_market(),
        book=book,
        fees=FEES,
        posterior=Posterior(q=q, sd=sd, w_model=0.5, fv=FairValue(q, sd, "m", T)),
        position=p,
        bankroll_usd=1000.0,
        allow_entries=allow,
    )


# ------------------------------------------------------------------ sizing


def test_kelly_basics() -> None:
    assert kelly_fraction(0.6, 0.5) == pytest.approx(0.2)
    assert kelly_fraction(0.4, 0.5) == 0.0
    assert uncertainty_shrink(0.6, 0.0, 0.5) == 1.0
    assert uncertainty_shrink(0.6, 0.1, 0.5) == pytest.approx(0.5)
    # λ=0.25, f*=0.2, k=1 → 5% of $1000 = $50 → 100 contracts @ 0.50
    assert kelly_contracts(0.6, 0.0, 0.5, 1000.0, 0.25) == pytest.approx(100.0)


# ------------------------------------------------------------------ exits


def test_exit_takes_when_liquidation_beats_hold() -> None:
    book = _book([(6500, 1000)], [(6700, 1000)])
    ex = plan_exit("M", 500, 0.60, 0.02, book, FEES, _market().grid)
    assert ex.take is not None and ex.take.side is BookSide.ASK and ex.take.reduce_only
    ex2 = plan_exit("M", 500, 0.75, 0.02, book, FEES, _market().grid, premium=0.02)
    assert ex2.take is None and ex2.quote is not None and ex2.quote.price == 7700  # q + 2¢


def test_exit_for_no_position() -> None:
    book = _book([(3000, 1000)], [(3200, 1000)])
    # NO worth 1−q = 0.60 to hold; buying YES back at 0.32 returns 0.68 − fee → exit now
    ex = plan_exit("M", -500, 0.40, 0.02, book, FEES, _market().grid)
    assert ex.take is not None and ex.take.side is BookSide.BID and ex.take.qty == 500
    # NO worth 0.80 to hold → keep it, rest a passive buy-back at q − premium
    ex2 = plan_exit("M", -500, 0.20, 0.02, book, FEES, _market().grid)
    assert (
        ex2.take is None
        and ex2.quote is not None
        and ex2.quote.side is BookSide.BID
        and ex2.quote.price == 1800
    )


# ------------------------------------------------------------------ edge taker


def test_edge_taker_takes_positive_edge() -> None:
    et = EdgeTaker(EdgeTakerCfg())
    d = et.evaluate(_ctx(0.60, 0.01, _book([(4900, 5000)], [(5100, 2000), (5300, 5000), (5800, 5000)])))
    assert len(d.takes) == 1
    t = d.takes[0]
    assert t.side is BookSide.BID and t.limit == 5300  # walks to 53¢, not 58¢ (EV ≤ θ there)
    assert t.qty % 100 == 0 and t.qty > 0


def test_edge_taker_distrusts_large_disagreement_and_clips_size() -> None:
    et = EdgeTaker(EdgeTakerCfg(max_disagreement=0.15))
    d = et.evaluate(_ctx(0.60, 0.01, _book([(4300, 5000)], [(4500, 5000)])))
    assert d.empty and d.note.startswith("disagree")
    ctx = _ctx(0.60, 0.01, _book([(4900, 50_000)], [(5100, 50_000)]))
    ctx.max_order_qty = 700
    d2 = et.evaluate(ctx)
    assert d2.takes and d2.takes[0].qty == 700  # clipped to the cap, not rejected downstream


def test_edge_taker_no_trade_without_edge_and_respects_allow() -> None:
    et = EdgeTaker(EdgeTakerCfg())
    book = _book([(4900, 5000)], [(5100, 5000)])
    assert et.evaluate(_ctx(0.50, 0.01, book)).empty
    strong = _book([(4300, 5000)], [(4500, 5000)])
    assert et.evaluate(_ctx(0.60, 0.01, strong, allow=False)).empty


def test_horizon_nu_thins_tails_with_time() -> None:
    from kst.pricing.crypto import horizon_nu

    assert horizon_nu(300) == 5.0  # minutes: fat tails (clamped at ν_min)
    assert 5.0 < horizon_nu(3 * 3600) < 20 < horizon_nu(24 * 3600)  # hours: toward normal
    assert horizon_nu(3600) == pytest.approx(4 + 6 / 3.0)


def test_edge_taker_sells_overpriced_yes() -> None:
    et = EdgeTaker(EdgeTakerCfg())
    d = et.evaluate(_ctx(0.30, 0.01, _book([(4000, 3000)], [(4200, 3000)])))
    assert d.takes and d.takes[0].side is BookSide.ASK


def test_edge_taker_maker_entry_when_no_taker_edge() -> None:
    et = EdgeTaker(EdgeTakerCfg(entry_style="auto", min_edge=0.05, maker_min_edge=0.02))
    d = et.evaluate(_ctx(0.53, 0.005, _book([(4600, 3000)], [(5000, 3000)])))
    assert not d.takes and d.quotes and d.quotes[0].side is BookSide.BID and d.quotes[0].price == 4700


# ------------------------------------------------------------------ market maker


def test_market_maker_quotes_and_skews_with_inventory() -> None:
    mm = MarketMaker(
        MarketMakerCfg(enabled=True, gamma=1.0, kappa=15.0, min_edge=0.005, max_inventory=100, quote_size=10)
    )
    book = _book([(4000, 5000)], [(6000, 5000)])
    flat = mm.evaluate(_ctx(0.50, 0.02, book))
    sides = {q.side: q for q in flat.quotes}
    assert BookSide.BID in sides and BookSide.ASK in sides
    assert sides[BookSide.BID].price < 5000 < sides[BookSide.ASK].price
    mm._st["M"].var_x = 1e-4  # active market → meaningful inventory skew
    long_ = mm.evaluate(_ctx(0.50, 0.02, book, pos=5000))
    ls = {q.side: q for q in long_.quotes}
    assert ls[BookSide.ASK].price <= sides[BookSide.ASK].price  # long inventory → lean to sell


def test_market_maker_pulls_on_toxic_flow() -> None:
    mm = MarketMaker(MarketMakerCfg(enabled=True))
    for i in range(10):
        mm.on_trade("M", 5000, 1000, BookSide.BID, T - 1000 * i)
    d = mm.evaluate(_ctx(0.50, 0.02, _book([(4000, 5000)], [(6000, 5000)])))
    assert d.note == "pulled" and not d.quotes


# ------------------------------------------------------------------ LLM gate


def _cand(t: str = "M") -> Candidate:
    return Candidate(
        t,
        "Will X happen?",
        "Politics",
        7200,
        Price(4000),
        Price(4200),
        1000,
        5000,
        0.02,
        0.5,
        BookSide.BID,
        0.03,
        "rules",
    )


def test_packet_roundtrip() -> None:
    user, ids = build_packet([_cand("A"), _cand("B")], T)
    assert "1|Will X happen?|POL|2.0h|40|42|" in user and ids == {1: "A", 2: "B"}
    got = parse_reply(
        '<think>hmm</think>```json\n{"d":[{"i":1,"v":"E","c":80,"s":"Y","r":"ok"},{"i":2,"v":"S","c":60,"s":"-","r":"no"}]}```',
        ids,
    )
    assert got["A"].enter and got["A"].side is BookSide.BID and not got["B"].enter


class _FakeProvider:
    name = "fake"
    model = "fake-1"

    def __init__(self) -> None:
        self.calls = 0

    async def available(self) -> bool:
        return True

    async def complete(self, system: str, user: str, max_tokens: int) -> LLMResult:
        self.calls += 1
        return LLMResult('{"d":[{"i":1,"v":"E","c":90,"s":"-","r":"fine"}]}', 900, 30, "fake", "fake-1", 5.0)


async def test_gate_caches_and_budgets() -> None:
    clock, bus, prov = ManualClock(T), EventBus(), _FakeProvider()
    gate = LLMGate(
        LLMCfg(mode="gate", scope="all", min_batch=1, min_interval_s=0),
        prov,
        None,
        bus,
        clock,
        trading_day_tz="UTC",
    )
    c = _cand()
    ok, v = gate.decide(c, modeled=True)
    assert not ok and v is None and "M" in gate.pending  # waits for a verdict
    await gate.call_once()
    ok, v = gate.decide(c, modeled=True)
    assert ok and v is not None and v.enter and prov.calls == 1
    gate.decide(c, modeled=True)
    assert prov.calls == 1 and not gate.pending  # cached: no new request
    moved = replace(c, bid=Price(6000), ask=Price(6200))
    assert state_bucket(moved) != state_bucket(c)
    ok, _ = gate.decide(moved, modeled=True)
    assert not ok and "M" in gate.pending  # material change → re-ask
    gate.calls = gate.cfg.budget.max_calls_per_day
    ok, _ = gate.decide(moved, modeled=True)
    assert not ok  # budget exhausted + fallback deny


def test_gate_scope_unmodeled_only() -> None:
    gate = LLMGate(
        LLMCfg(mode="gate", scope="unmodeled_only"),
        _FakeProvider(),
        None,
        EventBus(),
        ManualClock(T),
        trading_day_tz="UTC",
    )
    assert gate.decide(_cand(), modeled=True)[0] is True
    assert gate.decide(_cand(), modeled=False)[0] is False
