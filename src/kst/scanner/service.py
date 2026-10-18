"""Scanner: features + fair values + signals for the whole universe → ranked ScanSnapshot.

Runs standalone (`kst scan`) as an information tool and drives the auto-trader. Every `interval_s` it:
  1. reads top-of-book (fresh focus book if available, else ticker/REST stats),
  2. prices markets that have a model and computes after-fee taker edge,
  3. detects event-level structural arbs,
  4. ranks, publishes `ScanUpdated`, and requests the next focus set from market data.

Score (higher = more interesting; see config scanner.w_*):
  score = w_edge·max(edge,0)·100 + w_liquidity·log10(1+vol24h) + w_spread·[spread ≥ wide] + w_arb·[in arb]
"""

from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from typing import Any

from kst.config import PricingCfg, ScannerCfg, StructuralArbCfg
from kst.core.bus import EventBus
from kst.core.clock import Clock
from kst.core.events import ScanUpdated
from kst.core.fixedpoint import PRICE_SCALE, Price, Qty, qty_to_float
from kst.core.logging import get_logger
from kst.domain.market import Market
from kst.marketdata.catalog import MarketCatalog
from kst.marketdata.service import MarketDataService
from kst.pricing.base import Posterior, combine_with_market
from kst.pricing.resolver import ModelResolver
from kst.scanner.signals import ArbOpportunity, TopOfBook, detect_event_arbs, taker_edge

log = get_logger(__name__)


@dataclass(slots=True)
class ScanRow:
    ticker: str
    event_ticker: str
    series: str
    title: str
    category: str
    bid: Price | None
    ask: Price | None
    bid_qty: Qty
    ask_qty: Qty
    volume_24h: float
    open_interest: float
    chg_24h: float | None
    secs_to_close: float | None
    has_book: bool
    model: str = ""
    fair: float | None = None
    fair_sd: float | None = None
    q: float | None = None  # posterior after market combination
    q_sd: float | None = None
    edge_buy: float | None = None
    edge_sell: float | None = None
    signals: list[str] = field(default_factory=list)
    score: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def mid(self) -> float | None:
        if self.bid is None or self.ask is None:
            return None
        return (self.bid + self.ask) / (2 * PRICE_SCALE)

    @property
    def spread(self) -> int | None:
        return None if self.bid is None or self.ask is None else self.ask - self.bid

    @property
    def best_edge(self) -> float | None:
        vals = [v for v in (self.edge_buy, self.edge_sell) if v is not None]
        return max(vals) if vals else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "event": self.event_ticker,
            "series": self.series,
            "title": self.title,
            "category": self.category,
            "bid": self.bid,
            "ask": self.ask,
            "bid_qty": qty_to_float(self.bid_qty),
            "ask_qty": qty_to_float(self.ask_qty),
            "mid": self.mid,
            "spread": self.spread,
            "vol24h": self.volume_24h,
            "oi": self.open_interest,
            "chg24h": self.chg_24h,
            "close_s": self.secs_to_close,
            "book": self.has_book,
            "model": self.model,
            "fair": self.fair,
            "q": self.q,
            "q_sd": self.q_sd,
            "edge_buy": self.edge_buy,
            "edge_sell": self.edge_sell,
            "edge": self.best_edge,
            "signals": self.signals,
            "score": round(self.score, 3),
            "detail": self.detail,
        }


@dataclass(slots=True)
class ScanSnapshot:
    ts_ms: int
    rows: list[ScanRow]  # top_n, ranked
    arbs: list[ArbOpportunity]
    universe: int
    priced: int
    duration_ms: float
    by_ticker: dict[str, ScanRow] = field(default_factory=dict)  # every scanned market (not only top_n)


class ScannerService:
    def __init__(
        self,
        cfg: ScannerCfg,
        pricing: PricingCfg,
        arb_cfg: StructuralArbCfg,
        catalog: MarketCatalog,
        md: MarketDataService,
        resolver: ModelResolver,
        bus: EventBus,
        clock: Clock,
        *,
        rounding: float = 0.0001,
    ) -> None:
        self.cfg = cfg
        self.pricing = pricing
        self.arb_cfg = arb_cfg
        self.catalog = catalog
        self.md = md
        self.resolver = resolver
        self.bus = bus
        self.clock = clock
        self.rounding = rounding
        self.latest: ScanSnapshot | None = None
        self.extra_focus: set[str] = set()

    # ------------------------------------------------------------------ helpers

    def top_of_book(self, m: Market) -> TopOfBook:
        now = self.clock.now_ms()
        b = self.md.book(m.ticker)
        if b is not None and self.md.is_fresh(m.ticker, now):
            return b.best_bid, b.best_bid_qty, b.best_ask, b.best_ask_qty
        return m.yes_bid, m.yes_bid_size, m.yes_ask, m.yes_ask_size

    def posterior(
        self, m: Market, bid: Price | None, ask: Price | None, now_ms: int
    ) -> tuple[Posterior | None, str]:
        fv = self.resolver.fair_value(m, now_ms)
        if fv is None:
            return None, ""
        post = combine_with_market(
            fv,
            None if bid is None else bid / PRICE_SCALE,
            None if ask is None else ask / PRICE_SCALE,
            s_floor=self.pricing.s_floor,
            s_p_base=self.pricing.s_p_base,
        )
        return post, fv.model

    # ------------------------------------------------------------------ one scan

    def scan_once(self) -> ScanSnapshot:
        t0 = self.clock.now_ms()
        rows: dict[str, ScanRow] = {}
        priced = 0
        for m in self.catalog.universe_markets():
            bid, bq, ask, aq = self.top_of_book(m)
            secs = m.seconds_to_close(t0)
            chg = None
            if m.last_price is not None and m.previous_price:
                chg = (m.last_price - m.previous_price) / PRICE_SCALE
            row = ScanRow(
                ticker=m.ticker,
                event_ticker=m.event_ticker,
                series=m.series_ticker,
                title=m.short_title(80),
                category=m.category,
                bid=bid,
                ask=ask,
                bid_qty=bq,
                ask_qty=aq,
                volume_24h=qty_to_float(m.volume_24h),
                open_interest=qty_to_float(m.open_interest),
                chg_24h=chg,
                secs_to_close=secs,
                has_book=self.md.is_fresh(m.ticker, t0),
            )
            post, model = self.posterior(m, bid, ask, t0)
            if post is not None and post.fv is not None:
                priced += 1
                row.model, row.fair, row.fair_sd = model, post.fv.p, post.fv.sd
                row.q, row.q_sd = post.q, post.sd
                row.detail = dict(post.fv.detail)
                e = taker_edge(post.q, bid, ask, self.catalog.fees(m), self.rounding)
                row.edge_buy, row.edge_sell = e.buy, e.sell
            self._tag(row)
            rows[m.ticker] = row

        arbs: list[ArbOpportunity] = []
        if self.arb_cfg.enabled:
            allow = frozenset(self.arb_cfg.exhaustive_allowlist)
            for ev in self.catalog.events.values():
                mkts = self.catalog.event_markets(ev.event_ticker)
                if len(mkts) < 2:
                    continue
                found = detect_event_arbs(
                    ev,
                    mkts,
                    self.top_of_book,
                    self.catalog.fees,
                    margin=self.arb_cfg.margin,
                    rounding=self.rounding,
                    exhaustive_allowlist=allow,
                )
                for a in found:
                    arbs.append(a)
                    for leg in a.legs:
                        if (r := rows.get(leg.ticker)) is not None and "ARB" not in r.signals:
                            r.signals.append("ARB")
                            r.score += self.cfg.w_arb

        ranked = sorted(rows.values(), key=lambda r: r.score, reverse=True)
        snap = ScanSnapshot(
            ts_ms=t0,
            rows=ranked[: self.cfg.top_n],
            arbs=sorted(arbs, key=lambda a: a.edge, reverse=True),
            universe=len(rows),
            priced=priced,
            duration_ms=0.0,
            by_ticker=rows,
        )
        snap.duration_ms = float(self.clock.now_ms() - t0)
        return snap

    def _tag(self, r: ScanRow) -> None:
        c = self.cfg
        score = c.w_liquidity * math.log10(1.0 + max(r.volume_24h, 0.0))
        be = r.best_edge
        if be is not None and be >= c.min_edge_signal:
            r.signals.append("EDGE+" if (r.edge_buy or -1) >= (r.edge_sell or -1) else "EDGE-")
        if be is not None:
            score += c.w_edge * max(be, 0.0) * 100
        if r.spread is not None and r.spread >= c.wide_spread * PRICE_SCALE:
            r.signals.append("WIDE")
            score += c.w_spread
        if r.chg_24h is not None and abs(r.chg_24h) >= c.mover_threshold:
            r.signals.append("MOVER")
        if r.secs_to_close is not None and r.secs_to_close < 3600:
            r.signals.append("CLOSING")
        if r.bid is None or r.ask is None:
            r.signals.append("ONESIDED")
            score -= 1.0
        r.score = score

    # ------------------------------------------------------------------ loop

    async def run(self) -> None:
        while True:
            try:
                snap = self.scan_once()
                self.latest = snap
                focus = [r.ticker for r in snap.rows]
                arb_legs = [leg.ticker for a in snap.arbs for leg in a.legs]
                self.md.request_focus("scanner", [*arb_legs, *focus])
                self.bus.publish(ScanUpdated(snap.ts_ms, snap))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("scan_failed")
            await asyncio.sleep(self.cfg.interval_s)
