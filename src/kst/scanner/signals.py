"""Pure signal detectors: per-market edge and event-level structural arbitrage (docs/STRATEGIES.md §2)."""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass

from kst.core.fixedpoint import PRICE_SCALE, Price, Qty
from kst.core.types import BookSide, StrikeType
from kst.domain.fees import FeeSchedule
from kst.domain.market import Event, Market

# (bid, bid_qty, ask, ask_qty) for a market; None entries when that side is empty.
TopOfBook = tuple[Price | None, Qty, Price | None, Qty]


@dataclass(frozen=True, slots=True)
class Edge:
    buy: float | None  # $/contract EV of buying YES at the ask (after taker fee + rounding)
    sell: float | None  # $/contract EV of selling YES at the bid (buying NO)

    @property
    def best(self) -> tuple[float, BookSide | None]:
        b = self.buy if self.buy is not None else -math.inf
        s = self.sell if self.sell is not None else -math.inf
        if b == s == -math.inf:
            return -math.inf, None
        return (b, BookSide.BID) if b >= s else (s, BookSide.ASK)


def taker_edge(q: float, bid: Price | None, ask: Price | None, fees: FeeSchedule, rounding: float = 0.0001) -> Edge:
    buy = sell = None
    if ask is not None and 0 < ask < PRICE_SCALE:
        a = ask / PRICE_SCALE
        buy = q - a - fees.per_contract(a, is_taker=True) - rounding
    if bid is not None and 0 < bid < PRICE_SCALE:
        b = bid / PRICE_SCALE
        sell = b - q - fees.per_contract(b, is_taker=True) - rounding
    return Edge(buy, sell)


# ------------------------------------------------------------------ structural arbitrage


@dataclass(frozen=True, slots=True)
class ArbLeg:
    ticker: str
    side: BookSide  # BID = buy YES at `price`; ASK = sell YES (buy NO) at `price`
    price: Price
    max_qty: Qty


@dataclass(frozen=True, slots=True)
class ArbOpportunity:
    event_ticker: str
    kind: str  # "sell_all" | "buy_all" | "ladder"
    legs: tuple[ArbLeg, ...]
    edge: float  # guaranteed $ profit per basket (one contract of each leg), after fees
    max_baskets: Qty

    @property
    def cost_per_basket(self) -> float:
        return sum(
            (leg.price if leg.side is BookSide.BID else PRICE_SCALE - leg.price) / PRICE_SCALE for leg in self.legs
        )


def is_exhaustive(markets: list[Market]) -> bool:
    """Numeric-strike coverage check: less | between… | greater with no gaps."""
    lows = [m for m in markets if m.strike_type is StrikeType.LESS and m.cap_strike is not None]
    highs = [m for m in markets if m.strike_type is StrikeType.GREATER and m.floor_strike is not None]
    mids = sorted(
        (m for m in markets if m.strike_type is StrikeType.BETWEEN and m.floor_strike is not None and m.cap_strike is not None),
        key=lambda m: m.floor_strike or 0.0,
    )
    if len(lows) != 1 or len(highs) != 1 or len(lows) + len(highs) + len(mids) != len(markets):
        return False
    strikes = [lows[0].cap_strike, highs[0].floor_strike] + [x for m in mids for x in (m.floor_strike, m.cap_strike)]
    integral = all(s is not None and float(s).is_integer() for s in strikes)
    unit = 1.0 if integral else 0.01
    edge = lows[0].cap_strike
    assert edge is not None
    for m in mids:
        assert m.floor_strike is not None and m.cap_strike is not None
        gap = m.floor_strike - edge
        first = m is mids[0]
        if not (-1e-9 <= gap <= (0 if first else unit) + 1e-6):
            return False
        edge = m.cap_strike
    last_gap = (highs[0].floor_strike or 0.0) - edge
    return abs(last_gap) <= 1e-6


def detect_event_arbs(
    event: Event,
    markets: list[Market],
    top: Callable[[Market], TopOfBook],
    fees: Callable[[Market], FeeSchedule],
    *,
    margin: float = 0.005,
    rounding: float = 0.0001,
    exhaustive_allowlist: frozenset[str] = frozenset(),
) -> list[ArbOpportunity]:
    out: list[ArbOpportunity] = []
    active = [m for m in markets if m.is_tradable]
    if len(active) < 2:
        return out
    tops = {m.ticker: top(m) for m in active}

    def fee(m: Market, p: Price) -> float:
        x = p / PRICE_SCALE
        return fees(m).per_contract(x, is_taker=True) + rounding

    if event.mutually_exclusive:
        # Sell-all: NO on every market with a bid. Worst-case profit Σ(b − f) − 1.
        legs, total = [], -1.0
        for m in active:
            bid, bq, _, _ = tops[m.ticker]
            if bid is not None and bq > 0:
                gain = bid / PRICE_SCALE - fee(m, bid)
                if gain > 0:
                    legs.append(ArbLeg(m.ticker, BookSide.ASK, bid, bq))
                    total += gain
        if len(legs) >= 2 and total > margin:
            out.append(ArbOpportunity(event.event_ticker, "sell_all", tuple(legs), total, Qty(min(leg.max_qty for leg in legs))))

        # Buy-all: YES on every market; requires exhaustiveness (exactly one wins).
        if len(active) == len(markets) and (event.event_ticker in exhaustive_allowlist or is_exhaustive(markets)):
            cost, ylegs, ok = 0.0, [], True
            for m in active:
                _, _, ask, aq = tops[m.ticker]
                if ask is None or aq <= 0:
                    ok = False
                    break
                cost += ask / PRICE_SCALE + fee(m, ask)
                ylegs.append(ArbLeg(m.ticker, BookSide.BID, ask, aq))
            if ok and 1.0 - cost > margin:
                out.append(ArbOpportunity(event.event_ticker, "buy_all", tuple(ylegs), 1.0 - cost, Qty(min(leg.max_qty for leg in ylegs))))

    out.extend(_ladder_arbs(event, active, tops, fee, margin))
    return out


def _ladder_arbs(
    event: Event,
    markets: list[Market],
    tops: dict[str, TopOfBook],
    fee: Callable[[Market, Price], float],
    margin: float,
) -> list[ArbOpportunity]:
    """Monotonicity: P(>K) non-increasing in K; P(<K) non-decreasing in K."""
    out: list[ArbOpportunity] = []
    for kind in (StrikeType.GREATER, StrikeType.LESS):
        if kind is StrikeType.GREATER:
            ladder = sorted((m for m in markets if m.strike_type is kind and m.floor_strike is not None), key=lambda m: m.floor_strike or 0.0)
        else:
            # For "less", P(<K) increases with K → reverse so the same "buy low-index, sell high-index" logic applies.
            ladder = sorted((m for m in markets if m.strike_type is kind and m.cap_strike is not None), key=lambda m: -(m.cap_strike or 0.0))
        best: tuple[float, Market, Market] | None = None
        cheapest: tuple[float, Market] | None = None  # (all-in cost of buying YES, market) over earlier rungs
        for m in ladder:
            bid, bq, ask, aq = tops[m.ticker]
            if cheapest is not None and bid is not None and bq > 0:
                profit = bid / PRICE_SCALE - fee(m, bid) - cheapest[0]
                if profit > margin and (best is None or profit > best[0]):
                    best = (profit, cheapest[1], m)
            if ask is not None and aq > 0:
                c = ask / PRICE_SCALE + fee(m, ask)
                if cheapest is None or c < cheapest[0]:
                    cheapest = (c, m)
        if best is not None:
            profit, lo, hi = best
            _, _, a_lo, aq_lo = tops[lo.ticker]
            b_hi, bq_hi, _, _ = tops[hi.ticker]
            assert a_lo is not None and b_hi is not None
            legs = (ArbLeg(lo.ticker, BookSide.BID, a_lo, aq_lo), ArbLeg(hi.ticker, BookSide.ASK, b_hi, bq_hi))
            out.append(ArbOpportunity(event.event_ticker, "ladder", legs, profit, Qty(min(aq_lo, bq_hi))))
    return out
