"""EV-based exits (docs/STRATEGIES.md §1.3): sell when liquidation value ≥ hold value − κ·σ; otherwise rest
a passive take-profit quote at q̂ ± premium. No naive price stop-losses."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from kst.core.fixedpoint import PRICE_SCALE, Price, Qty, prob_to_price_ceil, prob_to_price_floor
from kst.core.types import BookSide
from kst.domain.fees import FeeSchedule
from kst.domain.market import PriceGrid
from kst.domain.orderbook import OrderBook
from kst.domain.orders import QuoteTarget, TakeIntent


@dataclass(frozen=True, slots=True)
class ExitPlan:
    take: TakeIntent | None
    quote: QuoteTarget | None
    liquidation_value: float | None  # $/contract
    hold_value: float


def plan_exit(
    ticker: str,
    pos_qty: int,
    q: float,
    sd: float,
    book: OrderBook,
    fees: FeeSchedule,
    grid: PriceGrid,
    *,
    slack_k: float = 0.0,
    premium: float = 0.02,
    rounding: float = 0.0001,
    slot: str = "exit",
    max_qty: int | None = None,
    max_usd: float | None = None,
    top: Callable[[BookSide], tuple[Price | None, int]] | None = None,
) -> ExitPlan:
    """`max_qty`/`max_usd` are the per-order caps (risk.max_order_*): large positions exit in slices.

    `top(side)` → (best price, Qty) to exit against; pass `StrategyContext.top` so live books (which hold our
    own quotes) don't value or size the exit off our own orders. Defaults to the raw book.
    """
    if pos_qty == 0:
        return ExitPlan(None, None, None, 0.0)
    n = abs(pos_qty)
    if top is not None:
        bid, bid_qty = top(BookSide.BID)
        ask, ask_qty = top(BookSide.ASK)
    else:
        bid, bid_qty = book.best_bid, book.best_bid_qty
        ask, ask_qty = book.best_ask, book.best_ask_qty

    def cap(side: BookSide, px: int, want: int) -> Qty:
        if max_qty is not None:
            want = min(want, max_qty)
        if max_usd is not None and max_usd != float("inf"):
            per = max(px if side is BookSide.BID else PRICE_SCALE - px, 1)  # Price × Qty = micros, exactly
            want = min(want, (round(max_usd * 1_000_000) // per) // 100 * 100)
        return Qty(max(want, 0))

    if pos_qty > 0:  # long YES → exit by selling YES at the bid
        hold = q
        liq = (
            None
            if bid is None
            else bid / PRICE_SCALE - fees.per_contract(bid / PRICE_SCALE, is_taker=True) - rounding
        )
        if liq is not None and bid is not None and liq >= hold - slack_k * sd:
            take = TakeIntent(
                ticker,
                BookSide.ASK,
                bid,
                cap(BookSide.ASK, bid, min(n, bid_qty)),  # bids we sell into
                "ev_exit",
                reduce_only=True,
                expected_edge=liq - hold,
            )
            return ExitPlan(take if take.qty > 0 else None, None, liq, hold)
        target = q + max(premium, fees.per_contract(q, is_taker=False) + rounding)
        px = grid.snap_up(prob_to_price_ceil(min(target, 0.9999)))
        raw_bid = book.best_bid  # post-only must clear every resting bid, our own included
        if px is not None and raw_bid is not None and px <= raw_bid:
            px = grid.tick_above(raw_bid)
        if px is None or (qn := cap(BookSide.ASK, px, n)) <= 0:
            return ExitPlan(None, None, liq, hold)
        return ExitPlan(
            None,
            QuoteTarget(
                ticker,
                slot,
                BookSide.ASK,
                Price(px),
                qn,
                reduce_only_intent=True,
                expected_edge=px / PRICE_SCALE - q,
            ),
            liq,
            hold,
        )

    # long NO (short YES) → exit by buying YES at the ask
    hold = 1.0 - q
    liq = (
        None
        if ask is None
        else 1.0 - ask / PRICE_SCALE - fees.per_contract(ask / PRICE_SCALE, is_taker=True) - rounding
    )
    if liq is not None and ask is not None and liq >= hold - slack_k * sd:
        take = TakeIntent(
            ticker,
            BookSide.BID,
            ask,
            cap(BookSide.BID, ask, min(n, ask_qty)),  # asks we buy from
            "ev_exit",
            reduce_only=True,
            expected_edge=liq - hold,
        )
        return ExitPlan(take if take.qty > 0 else None, None, liq, hold)
    target = q - max(premium, fees.per_contract(q, is_taker=False) + rounding)
    px = grid.snap_down(prob_to_price_floor(max(target, 0.0001)))
    raw_ask = book.best_ask
    if px is not None and raw_ask is not None and px >= raw_ask:
        px = grid.tick_below(raw_ask)
    if px is None or (qn := cap(BookSide.BID, px, n)) <= 0:
        return ExitPlan(None, None, liq, hold)
    return ExitPlan(
        None,
        QuoteTarget(
            ticker,
            slot,
            BookSide.BID,
            Price(px),
            qn,
            reduce_only_intent=True,
            expected_edge=q - px / PRICE_SCALE,
        ),
        liq,
        hold,
    )
