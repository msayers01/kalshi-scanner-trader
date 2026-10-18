"""EV-based exits (docs/STRATEGIES.md §1.3): sell when liquidation value ≥ hold value − κ·σ; otherwise rest
a passive take-profit quote at q̂ ± premium. No naive price stop-losses."""

from __future__ import annotations

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
) -> ExitPlan:
    if pos_qty == 0:
        return ExitPlan(None, None, None, 0.0)
    n = Qty(abs(pos_qty))
    if pos_qty > 0:  # long YES → exit by selling YES at the bid
        hold = q
        bid = book.best_bid
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
                Qty(min(n, book.qty_at_or_better(BookSide.BID, bid))),  # bids we sell into
                "ev_exit",
                reduce_only=True,
                expected_edge=liq - hold,
            )
            return ExitPlan(take if take.qty > 0 else None, None, liq, hold)
        target = q + max(premium, fees.per_contract(q, is_taker=False) + rounding)
        px = grid.snap_up(prob_to_price_ceil(min(target, 0.9999)))
        if px is not None and bid is not None and px <= bid:
            px = grid.tick_above(bid)
        if px is None:
            return ExitPlan(None, None, liq, hold)
        return ExitPlan(
            None,
            QuoteTarget(
                ticker,
                slot,
                BookSide.ASK,
                Price(px),
                n,
                reduce_only_intent=True,
                expected_edge=px / PRICE_SCALE - q,
            ),
            liq,
            hold,
        )

    # long NO (short YES) → exit by buying YES at the ask
    hold = 1.0 - q
    ask = book.best_ask
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
            Qty(min(n, book.qty_at_or_better(BookSide.ASK, ask))),  # asks we buy from
            "ev_exit",
            reduce_only=True,
            expected_edge=liq - hold,
        )
        return ExitPlan(take if take.qty > 0 else None, None, liq, hold)
    target = q - max(premium, fees.per_contract(q, is_taker=False) + rounding)
    px = grid.snap_down(prob_to_price_floor(max(target, 0.0001)))
    if px is not None and ask is not None and px >= ask:
        px = grid.tick_below(ask)
    if px is None:
        return ExitPlan(None, None, liq, hold)
    return ExitPlan(
        None,
        QuoteTarget(
            ticker,
            slot,
            BookSide.BID,
            Price(px),
            n,
            reduce_only_intent=True,
            expected_edge=q - px / PRICE_SCALE,
        ),
        liq,
        hold,
    )
