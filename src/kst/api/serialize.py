"""Domain → JSON-friendly dicts for the web API / WS hub (floats in dollars/contracts for the UI)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from kst.core.fixedpoint import MICROS_PER_DOLLAR, PRICE_SCALE, QTY_SCALE
from kst.domain.orders import Fill, Order
from kst.domain.positions import Position

if TYPE_CHECKING:
    from kst.app import Application
    from kst.scanner.query import ScanQuery


def px(p: int | None) -> float | None:
    return None if p is None else p / PRICE_SCALE


def order_dict(o: Order) -> dict[str, Any]:
    return {
        "coid": o.client_order_id,
        "ticker": o.ticker,
        "side": o.side.value,
        "outcome": o.side.outcome,
        "price": px(o.price),
        "qty": o.qty / QTY_SCALE,
        "filled": o.filled / QTY_SCALE,
        "status": o.status.value,
        "tif": o.tif.value,
        "post_only": o.post_only,
        "strategy": o.strategy,
        "slot": o.slot,
        "fees": o.fees / MICROS_PER_DOLLAR,
        "reason": o.reject_reason,
        "created_ms": o.created_ms,
        "updated_ms": o.updated_ms,
    }


def fill_dict(f: Fill) -> dict[str, Any]:
    return {
        "id": f.fill_id,
        "ticker": f.ticker,
        "side": f.side.value,
        "outcome": f.side.outcome,
        "price": px(f.price),
        "qty": f.qty / QTY_SCALE,
        "fee": f.fee / MICROS_PER_DOLLAR,
        "taker": f.is_taker,
        "strategy": f.strategy,
        "ts_ms": f.ts_ms,
    }


def position_dict(p: Position, mark: int | None, title: str = "") -> dict[str, Any]:
    return {
        "ticker": p.ticker,
        "title": title,
        "event": p.event_ticker,
        "category": p.category,
        "qty": p.qty / QTY_SCALE,
        "outcome": "yes" if p.qty > 0 else "no",
        "avg_price": p.avg_price,
        "mark": px(mark),
        "exposure": p.exposure / MICROS_PER_DOLLAR,
        "unrealized": None if mark is None else p.unrealized(mark) / MICROS_PER_DOLLAR,
        "realized": p.realized / MICROS_PER_DOLLAR,
        "fees": p.fees / MICROS_PER_DOLLAR,
        "strategy": p.strategy,
        "opened_ms": p.opened_ms,
    }


def positions(app: Application) -> list[dict[str, Any]]:
    out = []
    for p in app.portfolio.positions.values():
        if p.qty:
            m = app.catalog.get(p.ticker)
            out.append(position_dict(p, app.portfolio.mark(p.ticker), m.short_title(70) if m else ""))
    return sorted(out, key=lambda d: -abs(d["exposure"]))


def orders(app: Application, n: int = 100) -> list[dict[str, Any]]:
    return [order_dict(o) for o in app.oms.recent(n)]


def book(app: Application, ticker: str, levels: int = 12) -> dict[str, Any] | None:
    from kst.core.types import BookSide

    b = app.md.book(ticker)
    if b is None:
        return None
    return {
        "ticker": ticker,
        "bids": [[px(p), q / QTY_SCALE] for p, q in b.depth(BookSide.BID, levels)],
        "asks": [[px(p), q / QTY_SCALE] for p, q in b.depth(BookSide.ASK, levels)],
        "ts_ms": b.ts_ms,
        "fresh": app.md.is_fresh(ticker, app.clock.now_ms()),
        "mid": b.mid(),
        "micro": b.microprice(),
    }


def market_detail(app: Application, ticker: str) -> dict[str, Any] | None:
    m = app.catalog.get(ticker)
    if m is None:
        return None
    row = app.scanner.latest.by_ticker.get(ticker) if app.scanner.latest else None
    post = app.trader.posterior(ticker)
    pos = app.portfolio.positions.get(ticker)
    v = app.gate.verdicts.get(ticker)
    ev = app.trader.last_evals.get(ticker)
    ser = app.catalog.series_of(m)
    return {
        "ticker": m.ticker,
        "event": m.event_ticker,
        "series": m.series_ticker,
        "title": m.title,
        "subtitle": m.yes_sub_title,
        "category": m.category,
        "status": m.status.value,
        "close_ms": m.close_time_ms,
        "rules": m.rules_primary,
        "strike": {"type": m.strike_type.value, "floor": m.floor_strike, "cap": m.cap_strike},
        "tick": m.grid.tick_size_at(5000) / PRICE_SCALE,
        "fee_type": ser.fee_type if ser else "quadratic",
        "fee_multiplier": ser.fee_multiplier if ser else 1.0,
        "book": book(app, ticker),
        "scan": row.to_dict() if row else None,
        "fair": None
        if post is None
        else {
            "q": post.q,
            "sd": post.sd,
            "w_model": post.w_model,
            "model": post.fv.model if post.fv else None,
            "detail": post.fv.detail if post.fv else {},
        },
        "position": None
        if pos is None or not pos.qty
        else position_dict(pos, app.portfolio.mark(ticker), m.short_title(70)),
        "orders": [order_dict(o) for o in app.oms.live_orders(ticker)],
        "llm": None if v is None else v.to_dict(),
        "last_eval": None
        if ev is None
        else {
            "strategy": ev.strategy,
            "note": ev.note,
            "takes": ev.takes,
            "quotes": ev.quotes,
            "gated": ev.gated,
            "ts_ms": ev.ts_ms,
        },
    }


def scan(app: Application, limit: int = 100, query: ScanQuery | None = None) -> dict[str, Any]:
    """Top-ranked rows, or — with an active column query — matches across every scanned market."""
    snap = app.scanner.latest
    if snap is None:
        return {"ts_ms": 0, "rows": [], "arbs": [], "universe": 0, "priced": 0, "matched": None}
    matched: int | None = None
    picked = snap.rows[:limit]
    if query is not None and query.active:

        def llm_of(t: str) -> str | None:
            v = app.gate.verdicts.get(t)
            return None if v is None else ("E" if v.enter else "S")

        ranked = sorted(snap.by_ticker.values(), key=lambda r: r.score, reverse=True)
        matched, picked = query.run(ranked, llm_of)
    rows = []
    for r in picked:
        d = r.to_dict()
        v = app.gate.verdicts.get(r.ticker)
        d["llm"] = None if v is None else ("E" if v.enter else "S")
        rows.append(d)
    arbs = [
        {
            "event": a.event_ticker,
            "kind": a.kind,
            "edge": a.edge,
            "baskets": a.max_baskets / QTY_SCALE,
            "legs": [
                {"ticker": leg.ticker, "side": leg.side.value, "price": px(leg.price)} for leg in a.legs
            ],
        }
        for a in snap.arbs[:20]
    ]
    return {
        "ts_ms": snap.ts_ms,
        "rows": rows,
        "arbs": arbs,
        "universe": snap.universe,
        "priced": snap.priced,
        "ms": snap.duration_ms,
        "matched": matched,  # None = unfiltered top-N
    }
