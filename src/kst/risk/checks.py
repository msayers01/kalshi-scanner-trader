"""Pre-trade risk checks (docs/RISK.md). Pure functions: (OrderProposal, RiskContext) → failure | None.

Order matters: cheap, absolute checks first. Risk-reducing orders skip entry-only checks (marked ★).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from kst.core.fixedpoint import MICROS_PER_DOLLAR, PRICE_SCALE, Price, Qty
from kst.core.types import BookSide, EngineState, Mode, TimeInForce
from kst.domain.market import Market
from kst.domain.orders import Order
from kst.risk.limits import RiskLimits


@dataclass(frozen=True, slots=True)
class OrderProposal:
    ticker: str
    side: BookSide
    price: Price
    qty: Qty
    strategy: str = "manual"
    tif: TimeInForce = TimeInForce.GTC
    post_only: bool = False
    reference_price: float | None = None  # fair value (or mid) in dollars for fat-finger checks
    basket_id: str = ""
    manual: bool = False
    flatten: bool = False  # explicit operator flatten: the only orders allowed while KILLED

    def opening_qty(self, pos_qty: int) -> int:
        """Contracts (Qty) of this order that open/increase exposure given the current signed position."""
        if pos_qty == 0 or (pos_qty > 0) == (self.side is BookSide.BID):
            return self.qty
        return max(0, self.qty - abs(pos_qty))

    def is_risk_reducing(self, pos_qty: int) -> bool:
        return self.opening_qty(pos_qty) == 0

    def added_exposure_micros(self, pos_qty: int) -> int:
        """Worst-case loss added: YES costs p, NO costs (1 − p), per contract opened."""
        n = self.opening_qty(pos_qty)
        per = self.price if self.side is BookSide.BID else PRICE_SCALE - self.price
        return per * n


@dataclass(slots=True)
class RiskContext:
    now_ms: int
    state: EngineState
    mode: Mode
    live_armed: bool
    limits: RiskLimits
    market: Market | None
    book_fresh: bool
    category: str = ""
    pos_qty: int = 0
    market_exposure: int = 0  # micros, position + resting opening orders in this market
    market_resting_opening_qty: int = 0  # Qty of resting orders that would open in this market
    event_exposure: int = 0
    category_exposure: int = 0
    total_exposure: int = 0
    equity: int = 0  # micros
    cash: int = 0  # micros
    cash_buffer: int = 0
    open_positions: int = 0
    has_position: bool = False
    markets_entered_today: frozenset[str] = frozenset()
    orders_last_minute: int = 0
    blocked_series: frozenset[str] = frozenset()
    allowed_categories: frozenset[str] = frozenset()  # empty = all
    in_blackout: bool = False
    own_live_orders: list[Order] = field(default_factory=list)  # all strategies, this market


@dataclass(frozen=True, slots=True)
class CheckResult:
    ok: bool
    check: str = ""
    detail: str = ""


OK = CheckResult(True)
Check = Callable[[OrderProposal, RiskContext], CheckResult | None]


def _fail(name: str, detail: str) -> CheckResult:
    return CheckResult(False, name, detail)


def _usd(m: int) -> float:
    return m / MICROS_PER_DOLLAR


# ------------------------------------------------------------------ checks (always)


def engine_state_allows(p: OrderProposal, c: RiskContext) -> CheckResult | None:
    if c.state is EngineState.KILLED and not (p.flatten and p.is_risk_reducing(c.pos_qty)):
        return _fail("EngineStateAllows", "engine KILLED")
    if c.state is EngineState.PAUSED and not p.is_risk_reducing(c.pos_qty):
        return _fail("EngineStateAllows", "engine PAUSED: only risk-reducing orders")
    return None


def live_trading_armed(p: OrderProposal, c: RiskContext) -> CheckResult | None:
    if c.mode is Mode.LIVE and not c.live_armed:
        return _fail("LiveTradingArmed", "live mode requires --i-understand-live-trading")
    return None


def market_tradable(p: OrderProposal, c: RiskContext) -> CheckResult | None:
    m = c.market
    if m is None:
        return _fail("MarketTradable", "unknown market")
    if not m.is_tradable:
        return _fail("MarketTradable", f"status {m.status.value}")
    if m.close_time_ms is not None and c.now_ms >= m.close_time_ms:
        return _fail("MarketTradable", "market closed")
    return None


def data_fresh(p: OrderProposal, c: RiskContext) -> CheckResult | None:
    if not c.book_fresh:
        return _fail("DataFresh", "order book stale or missing")
    return None


def price_on_grid(p: OrderProposal, c: RiskContext) -> CheckResult | None:
    if not 0 < p.price < PRICE_SCALE:
        return _fail("PriceOnGrid", f"price {p.price} outside (0, 1)")
    if c.market is not None and not c.market.grid.is_valid(p.price):
        return _fail("PriceOnGrid", f"price {p.price / PRICE_SCALE:.4f} not on the market's tick grid")
    if p.qty <= 0:
        return _fail("PriceOnGrid", "non-positive quantity")
    return None


def fat_finger(p: OrderProposal, c: RiskContext) -> CheckResult | None:
    ref = p.reference_price
    if ref is None and c.market is not None:
        ref = c.market.mid
    if ref is None:
        return None
    dev = abs(p.price / PRICE_SCALE - ref)
    # Only penalize prices that are *worse* for us than the reference.
    worse = (p.side is BookSide.BID and p.price / PRICE_SCALE > ref) or (
        p.side is BookSide.ASK and p.price / PRICE_SCALE < ref
    )
    if worse and dev > c.limits.max_price_deviation:
        return _fail("FatFinger", f"price {p.price / PRICE_SCALE:.4f} is {dev:.3f} from reference {ref:.4f}")
    return None


def max_order_size(p: OrderProposal, c: RiskContext) -> CheckResult | None:
    if p.qty > c.limits.max_order_contracts * 100:
        return _fail("MaxOrderSize", f"{p.qty / 100:.2f} > {c.limits.max_order_contracts} contracts")
    per = p.price if p.side is BookSide.BID else PRICE_SCALE - p.price
    if _usd(per * p.qty) > c.limits.max_order_usd:
        return _fail("MaxOrderSize", f"order cost ${_usd(per * p.qty):.2f} > ${c.limits.max_order_usd}")
    return None


def order_rate(p: OrderProposal, c: RiskContext) -> CheckResult | None:
    if c.orders_last_minute >= c.limits.max_orders_per_minute:
        return _fail("OrderRate", f"{c.orders_last_minute} orders in the last minute")
    return None


def self_cross(p: OrderProposal, c: RiskContext) -> CheckResult | None:
    for o in c.own_live_orders:
        if o.side is p.side.opposite and o.remaining > 0:
            crosses = p.price >= o.price if p.side is BookSide.BID else p.price <= o.price
            if crosses:
                return _fail(
                    "SelfCross",
                    f"would cross own {o.side.value} @ {o.price / PRICE_SCALE:.4f} ({o.strategy})",
                )
    return None


def sufficient_balance(p: OrderProposal, c: RiskContext) -> CheckResult | None:
    need = p.added_exposure_micros(c.pos_qty)
    if need and need > c.cash - c.cash_buffer:
        return _fail(
            "SufficientBalance", f"needs ${_usd(need):.2f}, available ${_usd(c.cash - c.cash_buffer):.2f}"
        )
    return None


# ------------------------------------------------------------------ ★ entry-only checks


def price_band(p: OrderProposal, c: RiskContext) -> CheckResult | None:
    if p.basket_id:
        return None  # hedged arb legs may sit at extreme prices
    x = p.price / PRICE_SCALE
    # For NO entries the economically relevant price is the NO price.
    eff = x if p.side is BookSide.BID else 1 - x
    if eff < c.limits.min_entry_price or eff > c.limits.max_entry_price:
        return _fail(
            "PriceBand",
            f"entry at {eff:.4f} outside [{c.limits.min_entry_price}, {c.limits.max_entry_price}]",
        )
    return None


def min_time_to_close(p: OrderProposal, c: RiskContext) -> CheckResult | None:
    m = c.market
    if m is not None and m.close_time_ms is not None:
        secs = (m.close_time_ms - c.now_ms) / 1000
        if secs < c.limits.min_seconds_to_close:
            return _fail("MinTimeToClose", f"{secs:.0f}s to close < {c.limits.min_seconds_to_close}s")
    return None


def max_position_per_market(p: OrderProposal, c: RiskContext) -> CheckResult | None:
    after = abs(c.pos_qty) + c.market_resting_opening_qty + p.opening_qty(c.pos_qty)
    if after > c.limits.max_position_contracts * 100:
        return _fail(
            "MaxPositionPerMarket", f"{after / 100:.0f} > {c.limits.max_position_contracts} contracts"
        )
    exp = c.market_exposure + p.added_exposure_micros(c.pos_qty)
    if _usd(exp) > c.limits.max_exposure_per_market_usd:
        return _fail(
            "MaxPositionPerMarket",
            f"market exposure ${_usd(exp):.2f} > ${c.limits.max_exposure_per_market_usd}",
        )
    return None


def max_exposure_per_event(p: OrderProposal, c: RiskContext) -> CheckResult | None:
    exp = c.event_exposure + p.added_exposure_micros(c.pos_qty)
    if _usd(exp) > c.limits.max_exposure_per_event_usd:
        return _fail(
            "MaxExposurePerEvent", f"event exposure ${_usd(exp):.2f} > ${c.limits.max_exposure_per_event_usd}"
        )
    return None


def max_exposure_per_category(p: OrderProposal, c: RiskContext) -> CheckResult | None:
    exp = c.category_exposure + p.added_exposure_micros(c.pos_qty)
    if _usd(exp) > c.limits.max_exposure_per_category_usd:
        return _fail(
            "MaxExposurePerCategory",
            f"{c.category or '?'} exposure ${_usd(exp):.2f} > ${c.limits.max_exposure_per_category_usd}",
        )
    return None


def max_total_exposure(p: OrderProposal, c: RiskContext) -> CheckResult | None:
    exp = c.total_exposure + p.added_exposure_micros(c.pos_qty)
    if _usd(exp) > c.limits.max_total_exposure_usd:
        return _fail(
            "MaxTotalExposure", f"total exposure ${_usd(exp):.2f} > ${c.limits.max_total_exposure_usd}"
        )
    if c.equity > 0 and exp > c.limits.max_total_exposure_pct * c.equity:
        return _fail(
            "MaxTotalExposure",
            f"total exposure {exp / c.equity:.0%} of equity > {c.limits.max_total_exposure_pct:.0%}",
        )
    return None


def max_open_positions(p: OrderProposal, c: RiskContext) -> CheckResult | None:
    if p.basket_id:
        return None  # arb basket legs are hedged as a unit; exposure checks still apply
    if not c.has_position and c.open_positions >= c.limits.max_open_positions:
        return _fail("MaxOpenPositions", f"{c.open_positions} open positions")
    return None


def daily_new_market_limit(p: OrderProposal, c: RiskContext) -> CheckResult | None:
    if p.basket_id:
        return None  # an arb basket is one hedged trade, not N directional entries
    if (
        p.ticker not in c.markets_entered_today
        and len(c.markets_entered_today) >= c.limits.max_markets_per_day
    ):
        return _fail(
            "DailyNewMarketLimit",
            f"{len(c.markets_entered_today)}/{c.limits.max_markets_per_day} markets entered today",
        )
    return None


def category_allowed(p: OrderProposal, c: RiskContext) -> CheckResult | None:
    m = c.market
    if m is not None and m.series_ticker in c.blocked_series:
        return _fail("CategoryAllowed", f"series {m.series_ticker} blocked")
    if c.allowed_categories and c.category not in c.allowed_categories:
        return _fail("CategoryAllowed", f"category {c.category} not allowed")
    return None


def blackout_window(p: OrderProposal, c: RiskContext) -> CheckResult | None:
    if c.in_blackout:
        return _fail("BlackoutWindow", "inside a configured blackout window")
    return None


ALWAYS: tuple[Check, ...] = (
    engine_state_allows,
    live_trading_armed,
    market_tradable,
    data_fresh,
    price_on_grid,
    fat_finger,
    max_order_size,
    order_rate,
    self_cross,
    sufficient_balance,
)
ENTRY_ONLY: tuple[Check, ...] = (
    price_band,
    min_time_to_close,
    max_position_per_market,
    max_exposure_per_event,
    max_exposure_per_category,
    max_total_exposure,
    max_open_positions,
    daily_new_market_limit,
    category_allowed,
    blackout_window,
)


def run_checks(
    p: OrderProposal,
    c: RiskContext,
    *,
    always: tuple[Check, ...] = ALWAYS,
    entry: tuple[Check, ...] = ENTRY_ONLY,
) -> CheckResult:
    for chk in always:
        r = chk(p, c)
        if r is not None:
            return r
    if not p.is_risk_reducing(c.pos_qty):
        for chk in entry:
            r = chk(p, c)
            if r is not None:
                return r
    return OK
