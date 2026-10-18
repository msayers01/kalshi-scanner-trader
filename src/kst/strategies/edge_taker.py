"""Model-edge strategy (docs/STRATEGIES.md §5): take liquidity when after-fee EV > θ, else optionally rest a
patient maker quote; manage open positions with the EV exit rule."""

from __future__ import annotations

from kst.config import EdgeTakerCfg
from kst.core.fixedpoint import PRICE_SCALE, Price, Qty
from kst.core.types import BookSide
from kst.domain.orders import Decision, QuoteTarget, TakeIntent
from kst.strategies.base import Strategy, StrategyContext
from kst.strategies.exits import plan_exit
from kst.strategies.sizing import kelly_contracts, whole_contracts


class EdgeTaker(Strategy):
    name = "edge_taker"

    def __init__(self, cfg: EdgeTakerCfg) -> None:
        self.cfg = cfg

    def threshold(self, sd: float) -> float:
        return max(self.cfg.min_edge, self.cfg.z * sd)

    def evaluate(self, ctx: StrategyContext) -> Decision:
        post = ctx.posterior
        if post is None or not post.ok:
            # Can't price it → do nothing (keep existing orders untouched; risk/flatten handle emergencies).
            return Decision(manage_quotes=False, note="unpriced")
        q, sd = post.q, post.sd
        d = Decision()

        # ---- exits first (always allowed, even when PAUSED)
        if ctx.pos_qty != 0:
            ex = plan_exit(
                ctx.market.ticker,
                ctx.pos_qty,
                q,
                sd,
                ctx.book,
                ctx.fees,
                ctx.market.grid,
                slack_k=self.cfg.exit_slack_k,
                premium=self.cfg.exit_premium,
                rounding=ctx.rounding,
            )
            if ex.take is not None:
                d.takes.append(ex.take)
                return d  # exiting now; don't add entries in the same tick
            if ex.quote is not None:
                d.quotes.append(ex.quote)

        if not ctx.allow_entries:
            return d

        # ---- entries: add only in the direction of the current position (or from flat)
        theta = self.threshold(sd)
        buy = self._taker_entry(ctx, BookSide.BID, q, sd, theta)
        sell = self._taker_entry(ctx, BookSide.ASK, q, sd, theta)
        best = max((x for x in (buy, sell) if x is not None), key=lambda t: t.expected_edge, default=None)
        if best is not None and self._side_ok(ctx, best.side):
            d.takes.append(best)
            return d
        if self.cfg.entry_style in ("maker", "auto"):
            for side in (BookSide.BID, BookSide.ASK):
                if self._side_ok(ctx, side) and (qt := self._maker_entry(ctx, side, q, sd)) is not None:
                    d.quotes.append(qt)
        return d

    # ------------------------------------------------------------------ helpers

    def _side_ok(self, ctx: StrategyContext, side: BookSide) -> bool:
        if (ctx.pos_qty > 0 and side is BookSide.ASK) or (ctx.pos_qty < 0 and side is BookSide.BID):
            return False  # never add against our own position (exits handle that)
        return ctx.side_bias is None or ctx.side_bias is side

    def _target_qty(self, ctx: StrategyContext, side: BookSide, q: float, sd: float, cost: float) -> int:
        p_win = q if side is BookSide.BID else 1.0 - q
        n = whole_contracts(kelly_contracts(p_win, sd, cost, ctx.bankroll_usd, self.cfg.kelly_fraction))
        have = abs(ctx.pos_qty)
        return max(0, n - have)

    def _taker_entry(
        self, ctx: StrategyContext, side: BookSide, q: float, sd: float, theta: float
    ) -> TakeIntent | None:
        """Walk up to `max_levels` while the marginal level's EV stays above θ."""
        levels = ctx.book.depth(side.opposite, self.cfg.max_levels)  # buying YES consumes asks
        if not levels:
            return None
        limit: Price | None = None
        avail: int = 0
        worst_edge = 0.0
        for px, qty in levels:
            p = px / PRICE_SCALE
            fee = ctx.fees.per_contract(p, is_taker=True) + ctx.rounding
            ev = (q - p - fee) if side is BookSide.BID else (p - q - fee)
            if ev <= theta:
                break
            limit, worst_edge = px, ev
            avail += qty
        if limit is None:
            return None
        p_lim = limit / PRICE_SCALE
        cost = (p_lim if side is BookSide.BID else 1.0 - p_lim) + ctx.fees.per_contract(p_lim, is_taker=True)
        want = self._target_qty(ctx, side, q, sd, cost)
        n: int = min(want, avail)
        if n < 100:  # < 1 contract
            return None
        return TakeIntent(ctx.market.ticker, side, limit, Qty(n), "model_edge", expected_edge=worst_edge)

    def _maker_entry(self, ctx: StrategyContext, side: BookSide, q: float, sd: float) -> QuoteTarget | None:
        grid, book = ctx.market.grid, ctx.book
        bb, ba = book.best_bid, book.best_ask
        if side is BookSide.BID:
            if bb is None:
                return None
            px = grid.tick_above(bb)
            if px is None or (ba is not None and px >= ba):
                px = bb
            p = px / PRICE_SCALE
            ev = q - p - ctx.fees.per_contract(p, is_taker=False) - ctx.rounding
            cost = p + ctx.fees.per_contract(p, is_taker=False)
        else:
            if ba is None:
                return None
            px = grid.tick_below(ba)
            if px is None or (bb is not None and px <= bb):
                px = ba
            p = px / PRICE_SCALE
            ev = p - q - ctx.fees.per_contract(p, is_taker=False) - ctx.rounding
            cost = 1.0 - p + ctx.fees.per_contract(p, is_taker=False)
        if ev <= max(self.cfg.maker_min_edge, self.cfg.z * sd):
            return None
        qty = self._target_qty(ctx, side, q, sd, cost)
        if qty < 100:
            return None
        slot = "entry_bid" if side is BookSide.BID else "entry_ask"
        return QuoteTarget(ctx.market.ticker, slot, side, Price(px), Qty(qty), expected_edge=ev)
