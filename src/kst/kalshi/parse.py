"""Kalshi JSON → domain objects. All fixed-point parsing happens here (and in core.fixedpoint)."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from kst.core.clock import parse_iso_ms
from kst.core.fixedpoint import (
    PRICE_SCALE,
    Micros,
    Price,
    Qty,
    micros_from_str,
    price_from_str,
    qty_from_str,
)
from kst.core.types import BookSide, MarketStatus, OrderStatus, StrikeType, TimeInForce
from kst.domain.market import Event, Market, PriceBand, PriceGrid, Series, series_from_event_ticker
from kst.domain.orders import Fill, Order, strategy_from_client_order_id

Levels = list[tuple[int, int]]


def _p(d: dict[str, Any], key: str) -> Price | None:
    v = d.get(key)
    if v is None or v == "":
        return None
    return price_from_str(v)


def _q(d: dict[str, Any], key: str) -> Qty:
    v = d.get(key)
    return Qty(0) if v in (None, "") else qty_from_str(v)


def _m(d: dict[str, Any], key: str) -> Micros:
    v = d.get(key)
    return Micros(0) if v in (None, "") else micros_from_str(v)


def parse_price_grid(ranges: Iterable[dict[str, Any]] | None) -> PriceGrid:
    bands = [
        PriceBand(price_from_str(r["start"]), price_from_str(r["end"]), price_from_str(r["step"]))
        for r in (ranges or [])
        if r.get("step")
    ]
    return PriceGrid.of(bands) if bands else PriceGrid.cents()


def parse_series(d: dict[str, Any]) -> Series:
    return Series(
        ticker=d["ticker"],
        title=d.get("title", ""),
        category=d.get("category", ""),
        frequency=d.get("frequency", ""),
        fee_type=d.get("fee_type") or "quadratic",
        fee_multiplier=float(d.get("fee_multiplier", 1) if d.get("fee_multiplier") is not None else 1),
        tags=tuple(d.get("tags") or ()),
    )


def parse_market(d: dict[str, Any], *, series_ticker: str | None = None, category: str = "") -> Market:
    ev = d.get("event_ticker", "")
    m = Market(
        ticker=d["ticker"],
        event_ticker=ev,
        series_ticker=series_ticker or series_from_event_ticker(ev),
        title=d.get("title", ""),
        yes_sub_title=d.get("yes_sub_title", "") or d.get("subtitle", "") or "",
        category=category,
        status=MarketStatus.parse(d.get("status")),
        market_type=d.get("market_type", "binary") or "binary",
        open_time_ms=parse_iso_ms(d.get("open_time")),
        close_time_ms=parse_iso_ms(d.get("close_time")),
        expected_expiration_ms=parse_iso_ms(d.get("expected_expiration_time")),
        strike_type=StrikeType.parse(d.get("strike_type")),
        floor_strike=_float_or_none(d.get("floor_strike")),
        cap_strike=_float_or_none(d.get("cap_strike")),
        rules_primary=d.get("rules_primary", "") or "",
        grid=parse_price_grid(d.get("price_ranges")),
        exchange_index=int(d.get("exchange_index") or 0),
        can_close_early=bool(d.get("can_close_early", False)),
        fee_waiver_until_ms=parse_iso_ms(d.get("fee_waiver_expiration_time")),
        result=d.get("result", "") or "",
    )
    update_market_stats(m, d)
    return m


def update_market_stats(m: Market, d: dict[str, Any], ts_ms: int = 0) -> None:
    """Refresh top-of-book/stats from a Market REST object or a `ticker` WS message."""
    bid = _p(d, "yes_bid_dollars")
    ask = _p(d, "yes_ask_dollars")
    # Kalshi reports 0 bid / $1 ask for an empty side.
    m.yes_bid = bid if bid else None
    m.yes_ask = ask if ask is not None and 0 < ask < PRICE_SCALE else None
    if "yes_bid_size_fp" in d:
        m.yes_bid_size = _q(d, "yes_bid_size_fp")
    if "yes_ask_size_fp" in d:
        m.yes_ask_size = _q(d, "yes_ask_size_fp")
    lp = _p(d, "last_price_dollars") if "last_price_dollars" in d else _p(d, "price_dollars")
    if lp:
        m.last_price = lp
    if d.get("previous_price_dollars"):
        m.previous_price = _p(d, "previous_price_dollars")
    if "volume_fp" in d:
        m.volume = _q(d, "volume_fp")
    if "volume_24h_fp" in d:
        m.volume_24h = _q(d, "volume_24h_fp")
    if "open_interest_fp" in d:
        m.open_interest = _q(d, "open_interest_fp")
    if "status" in d:
        m.status = MarketStatus.parse(d.get("status"))
    m.stats_ts_ms = ts_ms or int(d.get("ts_ms") or 0) or m.stats_ts_ms


def _float_or_none(v: Any) -> float | None:
    try:
        return None if v is None or v == "" else float(v)
    except (TypeError, ValueError):
        return None


def parse_event(d: dict[str, Any]) -> tuple[Event, list[Market]]:
    series = d.get("series_ticker") or series_from_event_ticker(d["event_ticker"])
    category = d.get("category", "") or ""
    markets = [parse_market(md, series_ticker=series, category=category) for md in d.get("markets") or []]
    ev = Event(
        event_ticker=d["event_ticker"],
        series_ticker=series,
        title=d.get("title", ""),
        sub_title=d.get("sub_title", ""),
        category=category,
        mutually_exclusive=bool(d.get("mutually_exclusive", False)),
        strike_date_ms=parse_iso_ms(d.get("strike_date")),
        market_tickers=[m.ticker for m in markets],
    )
    return ev, markets


# ------------------------------------------------------------------ order books


def _levels(raw: Iterable[Iterable[str]] | None) -> Levels:
    out: Levels = []
    for lvl in raw or []:
        p, q = list(lvl)[:2]
        out.append((price_from_str(p), qty_from_str(q)))
    return out


def parse_rest_orderbook(d: dict[str, Any]) -> tuple[Levels, Levels]:
    """REST book: YES bids + NO bids (NO-leg prices) → (YES bids, YES asks)."""
    ob = d.get("orderbook_fp") or d.get("orderbook") or {}
    bids = _levels(ob.get("yes_dollars"))
    asks = [(PRICE_SCALE - p, q) for p, q in _levels(ob.get("no_dollars"))]
    return bids, asks


def parse_ws_snapshot(msg: dict[str, Any], *, use_yes_price: bool = True) -> tuple[Levels, Levels]:
    bids = _levels(msg.get("yes_dollars_fp"))
    no = _levels(msg.get("no_dollars_fp"))
    asks = no if use_yes_price else [(PRICE_SCALE - p, q) for p, q in no]
    return bids, asks


def parse_ws_delta(msg: dict[str, Any], *, use_yes_price: bool = True) -> tuple[BookSide, Price, Qty]:
    price = price_from_str(msg["price_dollars"])
    delta = qty_from_str(msg["delta_fp"])
    if msg.get("side") == "yes":
        return BookSide.BID, price, delta
    return BookSide.ASK, (price if use_yes_price else Price(PRICE_SCALE - price)), delta


# ------------------------------------------------------------------ orders / fills / trades

_ORDER_STATUS = {
    "resting": OrderStatus.RESTING,
    "canceled": OrderStatus.CANCELED,
    "cancelled": OrderStatus.CANCELED,
    "executed": OrderStatus.FILLED,
    "filled": OrderStatus.FILLED,
    "pending": OrderStatus.PENDING_NEW,
}


def order_status(s: str | None) -> OrderStatus:
    return _ORDER_STATUS.get((s or "").lower(), OrderStatus.RESTING)


def book_side_of(d: dict[str, Any]) -> BookSide:
    bs = d.get("book_side")
    if bs in ("bid", "ask"):
        return BookSide(bs)
    os_ = d.get("outcome_side") or d.get("purchased_side")
    if os_:
        return BookSide.from_outcome(os_)
    # legacy (action, side)
    action, side = d.get("action", "buy"), d.get("side", "yes")
    return BookSide.BID if (action == "buy") == (side == "yes") else BookSide.ASK


def parse_order(d: dict[str, Any]) -> Order:
    coid = d.get("client_order_id") or d.get("order_id") or ""
    initial = _q(d, "initial_count_fp") or Qty(_q(d, "fill_count_fp") + _q(d, "remaining_count_fp"))
    filled = _q(d, "fill_count_fp")
    price = _p(d, "yes_price_dollars") or Price(0)
    ts = parse_iso_ms(d.get("last_update_time")) or parse_iso_ms(d.get("created_time")) or 0
    o = Order(
        client_order_id=coid,
        ticker=d.get("ticker") or d.get("market_ticker", ""),
        side=book_side_of(d),
        price=price,
        qty=initial,
        strategy=strategy_from_client_order_id(d.get("client_order_id")),
        exchange_order_id=d.get("order_id"),
        status=order_status(d.get("status")),
        filled=filled,
        fill_cost=Micros(price * filled),
        fees=Micros(_m(d, "taker_fees_dollars") + _m(d, "maker_fees_dollars")),
        created_ms=parse_iso_ms(d.get("created_time")) or int(d.get("created_ts_ms") or 0),
        updated_ms=ts,
    )
    exp = parse_iso_ms(d.get("expiration_time"))
    o.expiration_ts = exp // 1000 if exp else None
    return o


def parse_fill(d: dict[str, Any]) -> Fill:
    ts = int(d.get("ts_ms") or 0) or parse_iso_ms(d.get("created_time")) or int(d.get("ts") or 0) * 1000
    return Fill(
        fill_id=d.get("fill_id") or d.get("trade_id", ""),
        client_order_id=d.get("client_order_id", "") or "",
        exchange_order_id=d.get("order_id", ""),
        ticker=d.get("ticker") or d.get("market_ticker", ""),
        side=book_side_of(d),
        price=price_from_str(d["yes_price_dollars"]),
        qty=qty_from_str(d["count_fp"]),
        fee=_m(d, "fee_cost"),
        is_taker=bool(d.get("is_taker", True)),
        ts_ms=ts,
        strategy=strategy_from_client_order_id(d.get("client_order_id")),
    )


def parse_trade(d: dict[str, Any]) -> tuple[str, Price, Qty, BookSide, int]:
    side = d.get("taker_book_side")
    taker = BookSide(side) if side in ("bid", "ask") else BookSide.from_outcome(d.get("taker_outcome_side") or d.get("taker_side", "yes"))
    ts = int(d.get("ts_ms") or 0) or parse_iso_ms(d.get("created_time")) or int(d.get("ts") or 0) * 1000
    return d.get("market_ticker") or d.get("ticker", ""), price_from_str(d["yes_price_dollars"]), qty_from_str(d["count_fp"]), taker, ts


def parse_position(d: dict[str, Any]) -> dict[str, Any]:
    return {
        "ticker": d.get("ticker") or d.get("market_ticker", ""),
        "qty": _q(d, "position_fp"),
        "exposure": _m(d, "market_exposure_dollars") if "market_exposure_dollars" in d else _m(d, "position_cost_dollars"),
        "realized": _m(d, "realized_pnl_dollars"),
        "fees": _m(d, "fees_paid_dollars"),
    }
