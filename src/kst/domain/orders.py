"""Orders, fills, and the strategy → OMS intent types (ADR 0003)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from kst.core.fixedpoint import Micros, Price, Qty
from kst.core.types import BookSide, OrderStatus, TimeInForce

# Legal order state transitions. Anything else is a bug (or an out-of-order exchange message we ignore).
_TRANSITIONS: dict[OrderStatus, frozenset[OrderStatus]] = {
    OrderStatus.PENDING_NEW: frozenset(
        {OrderStatus.RESTING, OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED, OrderStatus.PENDING_CANCEL}
    ),
    OrderStatus.RESTING: frozenset({OrderStatus.RESTING, OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.PENDING_CANCEL}),
    OrderStatus.PENDING_CANCEL: frozenset({OrderStatus.CANCELED, OrderStatus.FILLED, OrderStatus.RESTING}),
    OrderStatus.FILLED: frozenset(),
    OrderStatus.CANCELED: frozenset(),
    OrderStatus.REJECTED: frozenset(),
}


class IllegalTransition(RuntimeError):
    pass


def new_client_order_id(strategy: str) -> str:
    """`kst-<strategy>-<uuid12>` — the prefix lets us attribute exchange orders/fills after a restart."""
    tag = "".join(c for c in strategy if c.isalnum())[:12] or "manual"
    return f"kst-{tag}-{uuid.uuid4().hex[:12]}"


def strategy_from_client_order_id(coid: str | None) -> str:
    if coid and coid.startswith("kst-"):
        parts = coid.split("-")
        if len(parts) >= 3:
            return parts[1]
    return "external"


@dataclass(slots=True)
class Order:
    client_order_id: str
    ticker: str
    side: BookSide
    price: Price
    qty: Qty
    tif: TimeInForce = TimeInForce.GTC
    post_only: bool = False
    reduce_only: bool = False
    strategy: str = "manual"
    slot: str = ""  # strategy-defined key for declarative quotes, e.g. "mm_bid", "exit"
    basket_id: str = ""
    is_entry: bool = False  # counts toward the daily new-market limit
    expiration_ts: int | None = None  # unix seconds
    exchange_order_id: str | None = None
    status: OrderStatus = OrderStatus.PENDING_NEW
    filled: Qty = Qty(0)
    fill_cost: Micros = Micros(0)  # Σ price × qty of fills (YES leg)
    fees: Micros = Micros(0)
    created_ms: int = 0
    updated_ms: int = 0
    reject_reason: str = ""

    @property
    def remaining(self) -> Qty:
        return Qty(max(0, self.qty - self.filled))

    @property
    def avg_fill_price(self) -> float | None:
        return None if self.filled == 0 else self.fill_cost / self.filled / 10_000

    @property
    def is_live(self) -> bool:
        return self.status.is_live

    def transition(self, new: OrderStatus, ts_ms: int) -> bool:
        """Apply a status change. Returns False (no-op) for stale/duplicate updates after terminal states."""
        if new == self.status and new is not OrderStatus.RESTING:
            return False
        if new not in _TRANSITIONS[self.status]:
            if self.status.is_terminal:
                return False  # late message for a finished order — ignore
            raise IllegalTransition(f"{self.client_order_id}: {self.status} → {new}")
        self.status = new
        self.updated_ms = ts_ms
        return True

    def apply_fill(self, qty: int, price: int, fee: int, ts_ms: int) -> None:
        self.filled = Qty(self.filled + qty)
        self.fill_cost = Micros(self.fill_cost + price * qty)
        self.fees = Micros(self.fees + fee)
        self.updated_ms = ts_ms
        if self.filled >= self.qty and not self.status.is_terminal:
            self.status = OrderStatus.FILLED


@dataclass(frozen=True, slots=True)
class Fill:
    fill_id: str
    client_order_id: str
    exchange_order_id: str
    ticker: str
    side: BookSide
    price: Price
    qty: Qty
    fee: Micros
    is_taker: bool
    ts_ms: int
    strategy: str = "manual"


# ------------------------------------------------------------------ strategy intents


@dataclass(frozen=True, slots=True)
class TakeIntent:
    """Immediate-or-cancel order: take liquidity up to `qty` at prices no worse than `limit`."""

    ticker: str
    side: BookSide
    limit: Price
    qty: Qty
    reason: str = ""
    reduce_only: bool = False
    basket_id: str = ""
    expected_edge: float = 0.0  # $ per contract (for logging/analytics)


@dataclass(frozen=True, slots=True)
class QuoteTarget:
    """A resting (post-only) order the strategy wants to exist right now."""

    ticker: str
    slot: str  # unique per (strategy, ticker)
    side: BookSide
    price: Price
    qty: Qty
    reduce_only_intent: bool = False  # exit quote — allowed while PAUSED
    expected_edge: float = 0.0


@dataclass(slots=True)
class Decision:
    """What a strategy wants for one market (or one event basket) right now."""

    takes: list[TakeIntent] = field(default_factory=list)
    quotes: list[QuoteTarget] = field(default_factory=list)
    # When True the OMS cancels this strategy's quotes in the market that are not in `quotes`.
    manage_quotes: bool = True
    note: str = ""

    @property
    def empty(self) -> bool:
        return not self.takes and not self.quotes
