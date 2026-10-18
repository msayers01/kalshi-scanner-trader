"""Small shared enums and value types."""

from __future__ import annotations

from enum import StrEnum


class BookSide(StrEnum):
    """Single-book direction in YES-price space (matches Kalshi V2 `side`)."""

    BID = "bid"  # buy YES  (≡ sell NO)
    ASK = "ask"  # sell YES (≡ buy NO)

    @property
    def sign(self) -> int:
        return 1 if self is BookSide.BID else -1

    @property
    def opposite(self) -> BookSide:
        return BookSide.ASK if self is BookSide.BID else BookSide.BID

    @property
    def outcome(self) -> str:
        return "yes" if self is BookSide.BID else "no"

    @classmethod
    def from_outcome(cls, outcome_side: str) -> BookSide:
        return cls.BID if outcome_side.lower() == "yes" else cls.ASK


class Mode(StrEnum):
    PAPER = "paper"
    DEMO = "demo"
    LIVE = "live"
    REPLAY = "replay"

    @property
    def uses_exchange_orders(self) -> bool:
        return self in (Mode.DEMO, Mode.LIVE)


class EngineState(StrEnum):
    RUNNING = "running"  # entries + exits
    PAUSED = "paused"  # exits / risk-reducing only
    KILLED = "killed"  # nothing except explicit flatten; persisted across restarts


class TimeInForce(StrEnum):
    GTC = "good_till_canceled"
    IOC = "immediate_or_cancel"
    FOK = "fill_or_kill"


class OrderStatus(StrEnum):
    PENDING_NEW = "pending_new"  # created locally, not yet acked
    RESTING = "resting"  # live on the book (possibly partially filled)
    PENDING_CANCEL = "pending_cancel"
    FILLED = "filled"
    CANCELED = "canceled"  # includes IOC remainder / expiry
    REJECTED = "rejected"

    @property
    def is_terminal(self) -> bool:
        return self in (OrderStatus.FILLED, OrderStatus.CANCELED, OrderStatus.REJECTED)

    @property
    def is_live(self) -> bool:
        return self in (OrderStatus.PENDING_NEW, OrderStatus.RESTING, OrderStatus.PENDING_CANCEL)


class MarketStatus(StrEnum):
    """Kalshi market lifecycle statuses (Market.status)."""

    INITIALIZED = "initialized"
    INACTIVE = "inactive"
    ACTIVE = "active"
    CLOSED = "closed"
    DETERMINED = "determined"
    DISPUTED = "disputed"
    AMENDED = "amended"
    FINALIZED = "finalized"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, s: str | None) -> MarketStatus:
        try:
            return cls((s or "").lower())
        except ValueError:
            return cls.UNKNOWN


class StrikeType(StrEnum):
    GREATER = "greater"
    GREATER_OR_EQUAL = "greater_or_equal"
    LESS = "less"
    LESS_OR_EQUAL = "less_or_equal"
    BETWEEN = "between"
    FUNCTIONAL = "functional"
    CUSTOM = "custom"
    STRUCTURED = "structured"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, s: str | None) -> StrikeType:
        try:
            return cls((s or "").lower())
        except ValueError:
            return cls.UNKNOWN


class Actor(StrEnum):
    """Who issued a control command (for the audit log)."""

    SYSTEM = "system"
    WEB = "web"
    DISCORD = "discord"
    CLI = "cli"
