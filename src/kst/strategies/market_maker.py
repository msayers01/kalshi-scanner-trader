"""Inventory-aware market making: Avellaneda–Stoikov in logit space (docs/STRATEGIES.md §6).

    r = x̂ − I·γ·σ_x²·τ          Δ = γσ_x²τ + (2/γ)·ln(1 + γ/κ)
    bid = snap_down(σ(r − Δ/2))   ask = snap_up(σ(r + Δ/2))

x̂ = logit(q̂) (model posterior, calibrated prior, or microprice). σ_x is an EWMA of per-second variance
of logit(mid). Quotes are pulled on toxic flow, jumps, stale data, or proximity to close.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass, field

from kst.config import MarketMakerCfg
from kst.core.fixedpoint import PRICE_SCALE, Price, Qty, prob_to_price_ceil, prob_to_price_floor
from kst.core.types import BookSide
from kst.domain.orders import Decision, QuoteTarget
from kst.pricing.base import logit, sigmoid
from kst.strategies.base import Strategy, StrategyContext
from kst.strategies.exits import plan_exit


@dataclass(slots=True)
class _MMState:
    last_x: float | None = None
    last_ts: int = 0
    var_x: float = 2e-6  # per-second variance of logit(mid); prior ≈ quiet market
    flow: deque[tuple[int, int]] = field(default_factory=lambda: deque(maxlen=50))  # (signed qty, ts)
    jump_until_ms: int = 0


class MarketMaker(Strategy):
    name = "market_maker"

    def __init__(self, cfg: MarketMakerCfg, half_life_s: float = 300.0) -> None:
        self.cfg = cfg
        self.half_life_s = half_life_s
        self._st: dict[str, _MMState] = {}

    # ------------------------------------------------------------------ state updates

    def on_trade(self, ticker: str, price: int, qty: int, taker_side: BookSide, ts_ms: int) -> None:
        st = self._st.setdefault(ticker, _MMState())
        st.flow.append((qty * taker_side.sign, ts_ms))

    def _update_vol(self, st: _MMState, x: float, ts_ms: int) -> bool:
        """Update σ_x; returns True if this tick looks like a jump."""
        jump = False
        if st.last_x is not None and ts_ms > st.last_ts:
            dt = (ts_ms - st.last_ts) / 1000.0
            dx = x - st.last_x
            if abs(dx) > self.cfg.jump_k * math.sqrt(st.var_x * dt) and abs(dx) > 0.15:
                jump = True
            lam = 0.5 ** (dt / self.half_life_s)
            st.var_x = lam * st.var_x + (1 - lam) * (dx * dx / dt)
        st.last_x, st.last_ts = x, ts_ms
        return jump

    def flow_imbalance(self, ticker: str, now_ms: int, window_ms: int = 120_000) -> float:
        st = self._st.get(ticker)
        if st is None:
            return 0.0
        recent = [(q, t) for q, t in st.flow if now_ms - t <= window_ms]
        tot = sum(abs(q) for q, _ in recent)
        return 0.0 if tot == 0 else sum(q for q, _ in recent) / tot

    # ------------------------------------------------------------------ quoting

    def evaluate(self, ctx: StrategyContext) -> Decision:
        m, book = ctx.market, ctx.book
        tkr = m.ticker
        st = self._st.setdefault(tkr, _MMState())
        fair = ctx.posterior.q if ctx.posterior is not None and ctx.posterior.ok else book.microprice()
        if fair is None or not 0.0 < fair < 1.0:
            return Decision(note="no fair")
        mid = book.mid()
        if mid is not None and self._update_vol(st, logit(mid), ctx.now_ms):
            st.jump_until_ms = ctx.now_ms + 30_000
        d = Decision()

        secs = m.seconds_to_close(ctx.now_ms) or 0.0
        toxic = abs(self.flow_imbalance(tkr, ctx.now_ms)) >= self.cfg.flow_threshold
        pulled = toxic or ctx.now_ms < st.jump_until_ms or secs < self.cfg.min_seconds_to_close
        sd = ctx.posterior.sd if ctx.posterior is not None else 0.05

        if pulled:
            # Only keep a risk-reducing exit while conditions are hostile.
            if ctx.pos_qty:
                ex = plan_exit(
                    tkr,
                    ctx.pos_qty,
                    fair,
                    sd,
                    book,
                    ctx.fees,
                    m.grid,
                    premium=self.cfg.min_edge,
                    rounding=ctx.rounding,
                    max_qty=ctx.max_order_qty,
                    max_usd=ctx.max_order_usd,
                )
                if ex.quote is not None:
                    d.quotes.append(ex.quote)
            d.note = "pulled"
            return d

        unit = 100 * max(self.cfg.quote_size, 1.0)  # Qty units per "inventory unit"
        inv = ctx.pos_qty / unit
        tau = min(max(secs, 0.0), self.cfg.horizon_s)
        g, k = self.cfg.gamma, self.cfg.kappa
        x = logit(fair)
        r = x - inv * g * st.var_x * tau
        delta = g * st.var_x * tau + (2.0 / g) * math.log1p(g / k)
        bid_p = sigmoid(r - delta / 2)
        ask_p = sigmoid(r + delta / 2)

        max_inv = 100 * self.cfg.max_inventory
        taper = max(0.0, 1.0 - abs(ctx.pos_qty) / max_inv)
        size = Qty(ctx.clip_qty(int(self.cfg.quote_size * max(taper, 0.2)) * 100, max(fair, 1 - fair)))
        bb, ba = book.best_bid, book.best_ask

        for side in (BookSide.BID, BookSide.ASK):
            qt = self._quote(ctx, side, bid_p if side is BookSide.BID else ask_p, fair, size, max_inv, bb, ba)
            if qt is not None:
                d.quotes.append(qt)
        d.note = f"r={sigmoid(r):.3f} Δ={delta:.3f} σx²={st.var_x:.2e}"
        return d

    def _quote(
        self,
        ctx: StrategyContext,
        side: BookSide,
        target: float,
        fair: float,
        size: Qty,
        max_inv: float,
        bb: Price | None,
        ba: Price | None,
    ) -> QuoteTarget | None:
        m, pos = ctx.market, ctx.pos_qty
        reduces = (pos < 0) if side is BookSide.BID else (pos > 0)
        at_cap = (pos >= max_inv) if side is BookSide.BID else (pos <= -max_inv)
        if at_cap or not (ctx.allow_entries or reduces):
            return None
        # EV floor: never quote through fair ± (min_edge + maker fee). Inventory skew can push the
        # risk-reducing side right up to that floor, but not beyond it.
        edge = self.cfg.min_edge + ctx.fees.per_contract(fair, is_taker=False) + ctx.rounding
        if side is BookSide.BID:
            px = m.grid.snap_down(prob_to_price_floor(min(target, fair - edge)))
            if px is not None and ba is not None and px >= ba:
                px = m.grid.tick_below(ba)  # stay post-only
        else:
            px = m.grid.snap_up(prob_to_price_ceil(max(target, fair + edge)))
            if px is not None and bb is not None and px <= bb:
                px = m.grid.tick_above(bb)
        if px is None or not 0 < px < 10_000:
            return None
        p = px / PRICE_SCALE
        fee = ctx.fees.per_contract(p, is_taker=False) + ctx.rounding
        ev = (fair - p - fee) if side is BookSide.BID else (p - fair - fee)
        if ev < self.cfg.min_edge - 1e-9:
            return None
        reduce_only = reduces and not ctx.allow_entries
        qty = Qty(min(size, abs(pos))) if reduce_only else size
        if qty <= 0:
            return None
        slot = "mm_bid" if side is BookSide.BID else "mm_ask"
        return QuoteTarget(
            m.ticker, slot, side, Price(px), qty, reduce_only_intent=reduce_only, expected_edge=ev
        )
