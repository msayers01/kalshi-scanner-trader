"""Market / Event / Series domain objects and the per-market price grid."""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field

from kst.core.fixedpoint import PRICE_SCALE, Price, Qty
from kst.core.types import MarketStatus, StrikeType


@dataclass(frozen=True, slots=True)
class PriceBand:
    start: int  # Price, inclusive
    end: int  # Price, inclusive
    step: int  # Price


_GRID_CACHE: dict[tuple[tuple[int, int, int], ...], PriceGrid] = {}


class PriceGrid:
    """Valid order prices for a market (from Kalshi `price_ranges`).

    Order prices must lie strictly inside (0, $1). `snap_down`/`snap_up` return the nearest valid
    price at or below / at or above the input, or None if none exists.
    """

    __slots__ = ("_levels", "bands")

    def __init__(self, bands: list[PriceBand]) -> None:
        if not bands:
            bands = [PriceBand(0, PRICE_SCALE, 100)]  # default: whole cents
        self.bands = tuple(sorted(bands, key=lambda b: b.start))
        levels: set[int] = set()
        for b in self.bands:
            if b.step <= 0:
                raise ValueError(f"invalid price band step {b.step}")
            levels.update(range(b.start, b.end + 1, b.step))
        self._levels = sorted(p for p in levels if 0 < p < PRICE_SCALE)

    @classmethod
    def cents(cls) -> PriceGrid:
        return cls.of([PriceBand(0, PRICE_SCALE, 100)])

    @classmethod
    def of(cls, bands: list[PriceBand]) -> PriceGrid:
        """Interned constructor — markets share a handful of grid structures; don't duplicate levels."""
        key = tuple(sorted((b.start, b.end, b.step) for b in bands))
        grid = _GRID_CACHE.get(key)
        if grid is None:
            grid = cls(bands)
            _GRID_CACHE[key] = grid
        return grid

    @property
    def levels(self) -> list[int]:
        return self._levels

    @property
    def min_price(self) -> Price:
        return Price(self._levels[0])

    @property
    def max_price(self) -> Price:
        return Price(self._levels[-1])

    def is_valid(self, p: int) -> bool:
        i = bisect.bisect_left(self._levels, p)
        return i < len(self._levels) and self._levels[i] == p

    def snap_down(self, p: int) -> Price | None:
        i = bisect.bisect_right(self._levels, p) - 1
        return Price(self._levels[i]) if i >= 0 else None

    def snap_up(self, p: int) -> Price | None:
        i = bisect.bisect_left(self._levels, p)
        return Price(self._levels[i]) if i < len(self._levels) else None

    def tick_above(self, p: int) -> Price | None:
        i = bisect.bisect_right(self._levels, p)
        return Price(self._levels[i]) if i < len(self._levels) else None

    def tick_below(self, p: int) -> Price | None:
        i = bisect.bisect_left(self._levels, p) - 1
        return Price(self._levels[i]) if i >= 0 else None

    def tick_size_at(self, p: int) -> int:
        """Local tick size around p (distance to the next level up, or down at the top)."""
        up = self.tick_above(p)
        if up is not None:
            return up - (self.snap_down(p) or 0)
        down = self.tick_below(p)
        return p - down if down is not None else 100

    def __repr__(self) -> str:
        return f"PriceGrid({[(b.start, b.end, b.step) for b in self.bands]})"


_CENT_GRID = PriceGrid.cents()


@dataclass(slots=True)
class Series:
    ticker: str
    title: str = ""
    category: str = ""
    frequency: str = ""
    fee_type: str = "quadratic"
    fee_multiplier: float = 1.0
    tags: tuple[str, ...] = ()


@dataclass(slots=True)
class Event:
    event_ticker: str
    series_ticker: str
    title: str = ""
    sub_title: str = ""
    category: str = ""
    mutually_exclusive: bool = False
    strike_date_ms: int | None = None
    market_tickers: list[str] = field(default_factory=list)


@dataclass(slots=True)
class Market:
    """Static-ish market metadata plus the latest top-of-book stats (from REST or the ticker channel)."""

    ticker: str
    event_ticker: str
    series_ticker: str
    title: str = ""
    yes_sub_title: str = ""
    category: str = ""
    status: MarketStatus = MarketStatus.UNKNOWN
    market_type: str = "binary"
    open_time_ms: int | None = None
    close_time_ms: int | None = None
    expected_expiration_ms: int | None = None
    strike_type: StrikeType = StrikeType.UNKNOWN
    floor_strike: float | None = None
    cap_strike: float | None = None
    rules_primary: str = ""
    grid: PriceGrid = field(default_factory=lambda: _CENT_GRID)
    exchange_index: int = 0
    can_close_early: bool = False
    fee_waiver_until_ms: int | None = None
    result: str = ""
    # --- top of book / stats (YES leg)
    yes_bid: Price | None = None
    yes_ask: Price | None = None
    yes_bid_size: Qty = Qty(0)
    yes_ask_size: Qty = Qty(0)
    last_price: Price | None = None
    previous_price: Price | None = None
    volume: Qty = Qty(0)
    volume_24h: Qty = Qty(0)
    open_interest: Qty = Qty(0)
    stats_ts_ms: int = 0

    @property
    def is_tradable(self) -> bool:
        return self.status is MarketStatus.ACTIVE and self.market_type == "binary"

    def seconds_to_close(self, now_ms: int) -> float | None:
        return None if self.close_time_ms is None else (self.close_time_ms - now_ms) / 1000.0

    @property
    def mid(self) -> float | None:
        if self.yes_bid is None or self.yes_ask is None or self.yes_bid <= 0 or self.yes_ask >= PRICE_SCALE:
            return None
        return (self.yes_bid + self.yes_ask) / (2 * PRICE_SCALE)

    @property
    def spread(self) -> int | None:
        if self.yes_bid is None or self.yes_ask is None:
            return None
        return self.yes_ask - self.yes_bid

    def short_title(self, n: int = 60) -> str:
        t = self.title
        if self.yes_sub_title and self.yes_sub_title not in t:
            t = f"{t} — {self.yes_sub_title}"
        return t if len(t) <= n else t[: n - 1] + "…"


def series_from_event_ticker(event_ticker: str) -> str:
    """Kalshi convention: series ticker is the prefix before the first '-'."""
    return event_ticker.split("-", 1)[0]
