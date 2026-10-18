"""Position accounting in YES-price space (pure, exact integer micros).

qty > 0: long YES; qty < 0: long NO (|qty| contracts). `open_cost` is the YES-leg price × |qty| paid/
received for the open contracts (average-cost method). Cash effects follow Kalshi collateral rules:
buying YES costs p, buying NO costs (1 − p); closing returns the corresponding leg value.
"""

from __future__ import annotations

from dataclasses import dataclass

from kst.core.fixedpoint import PRICE_SCALE, Micros, Price, Qty
from kst.core.types import BookSide


@dataclass(frozen=True, slots=True)
class FillEffect:
    cash_delta: Micros  # change in cash (negative = paid), fees included
    realized: Micros  # gross realized P&L from the closing part (fees excluded)
    opened: Qty  # contracts opened (unsigned)
    closed: Qty  # contracts closed (unsigned)
    round_trip_done: bool  # position went flat (or flipped) with this fill
    round_trip_pnl: Micros  # net P&L of the completed round trip (incl. fees), if done


@dataclass(slots=True)
class Position:
    ticker: str
    event_ticker: str = ""
    category: str = ""
    qty: Qty = Qty(0)
    open_cost: Micros = Micros(0)
    realized: Micros = Micros(0)  # gross, lifetime in this market
    fees: Micros = Micros(0)
    strategy: str = "manual"
    opened_ms: int = 0
    updated_ms: int = 0
    _rt_pnl: int = 0  # running net P&L of the current round trip

    @property
    def is_flat(self) -> bool:
        return self.qty == 0

    @property
    def side(self) -> BookSide | None:
        if self.qty == 0:
            return None
        return BookSide.BID if self.qty > 0 else BookSide.ASK

    @property
    def avg_price(self) -> float | None:
        """Average YES-leg entry price in dollars."""
        return None if self.qty == 0 else self.open_cost / abs(self.qty) / PRICE_SCALE

    @property
    def exposure(self) -> Micros:
        """Worst-case loss of the open position (= collateral at risk)."""
        if self.qty > 0:
            return self.open_cost
        if self.qty < 0:
            return Micros(PRICE_SCALE * -self.qty - self.open_cost)
        return Micros(0)

    def market_value(self, yes_mark: int) -> Micros:
        if self.qty >= 0:
            return Micros(yes_mark * self.qty)
        return Micros((PRICE_SCALE - yes_mark) * -self.qty)

    def unrealized(self, yes_mark: int) -> Micros:
        return Micros(self.market_value(yes_mark) - self.exposure)

    def apply_fill(self, side: BookSide, price: int, qty: int, fee: int, ts_ms: int) -> FillEffect:
        if qty <= 0:
            raise ValueError("fill qty must be positive")
        sign = side.sign
        cash = -fee
        realized = 0
        closed = 0
        rt_done = False
        rt_pnl = 0
        self._rt_pnl -= fee
        self.fees = Micros(self.fees + fee)

        if self.qty != 0 and (self.qty > 0) != (sign > 0):
            # closing (part of) the existing position
            closed = min(qty, abs(self.qty))
            cost_removed = self.open_cost * closed // abs(self.qty)
            if self.qty > 0:  # selling YES at `price`
                realized = price * closed - cost_removed
                cash += price * closed
            else:  # buying YES closes NO: receive (1 − p) per contract
                realized = cost_removed - price * closed
                cash += (PRICE_SCALE - price) * closed
            self.open_cost = Micros(self.open_cost - cost_removed)
            self.qty = Qty(self.qty + sign * closed)
            self.realized = Micros(self.realized + realized)
            self._rt_pnl += realized
            if self.qty == 0:
                self.open_cost = Micros(0)
                rt_done, rt_pnl = True, self._rt_pnl
                self._rt_pnl = 0

        opened = qty - closed
        if opened > 0:
            if self.qty == 0:
                self.opened_ms = ts_ms
            self.open_cost = Micros(self.open_cost + price * opened)
            cash -= price * opened if sign > 0 else (PRICE_SCALE - price) * opened
            self.qty = Qty(self.qty + sign * opened)

        self.updated_ms = ts_ms
        return FillEffect(Micros(cash), Micros(realized), Qty(opened), Qty(closed), rt_done, Micros(rt_pnl))

    def settle(self, settlement_price: int, ts_ms: int) -> FillEffect:
        """Market resolved; YES pays `settlement_price` (10_000 = $1, 0 = $0)."""
        if self.qty == 0:
            return FillEffect(Micros(0), Micros(0), Qty(0), Qty(0), False, Micros(0))
        n = abs(self.qty)
        if self.qty > 0:
            payout = settlement_price * n
            realized = payout - self.open_cost
        else:
            payout = (PRICE_SCALE - settlement_price) * n
            realized = self.open_cost - settlement_price * n
        self.realized = Micros(self.realized + realized)
        rt = self._rt_pnl + realized
        self._rt_pnl = 0
        self.qty = Qty(0)
        self.open_cost = Micros(0)
        self.updated_ms = ts_ms
        return FillEffect(Micros(payout), Micros(realized), Qty(0), Qty(n), True, Micros(rt))


def settlement_price_for(result: str, settlement_value: int | None = None) -> Price | None:
    r = (result or "").lower()
    if r == "yes":
        return Price(PRICE_SCALE)
    if r == "no":
        return Price(0)
    if settlement_value is not None:
        return Price(settlement_value)
    return None
