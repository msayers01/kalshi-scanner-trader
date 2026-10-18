"""Typed events published on the in-process bus.

Add new events here (frozen dataclasses deriving from `Event`). Never publish ad-hoc dicts.
Domain types are referenced only for annotations to keep `core` import-light.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from kst.core.types import Actor, BookSide, EngineState, MarketStatus

if TYPE_CHECKING:
    from kst.domain.orders import Fill, Order
    from kst.domain.positions import Position


@dataclass(frozen=True, slots=True)
class Event:
    ts_ms: int


# ------------------------------------------------------------------ market data


@dataclass(frozen=True, slots=True)
class BookUpdated(Event):
    ticker: str


@dataclass(frozen=True, slots=True)
class TopOfBookUpdated(Event):
    """Top-of-book / stats changed for a market (ticker channel or REST poll)."""

    ticker: str


@dataclass(frozen=True, slots=True)
class TradePrinted(Event):
    ticker: str
    price: int  # Price (YES leg)
    qty: int  # Qty
    taker_side: BookSide  # BID = taker bought YES


@dataclass(frozen=True, slots=True)
class MarketStatusChanged(Event):
    ticker: str
    status: MarketStatus
    result: str = ""  # "yes" | "no" | "scalar" | ""
    settlement_value: int | None = None  # Price of YES at settlement, if known


@dataclass(frozen=True, slots=True)
class FeedHealth(Event):
    source: str  # "kalshi_ws" | "kalshi_poll" | "crypto:BRTI" | "weather" ...
    connected: bool
    detail: str = ""


@dataclass(frozen=True, slots=True)
class ReferencePriceUpdated(Event):
    """External/reference index value (e.g. CF Benchmarks BRTI)."""

    index_id: str
    value: float
    source: str = ""


@dataclass(frozen=True, slots=True)
class CatalogRefreshed(Event):
    n_markets: int
    n_events: int


# ------------------------------------------------------------------ scanner / llm


@dataclass(frozen=True, slots=True)
class ScanUpdated(Event):
    snapshot: Any  # kst.scanner.service.ScanSnapshot (Any avoids an import cycle)


@dataclass(frozen=True, slots=True)
class LLMVerdictsUpdated(Event):
    verdicts: dict[str, Any]  # ticker -> kst.llm.gate.Verdict


# ------------------------------------------------------------------ orders / fills


@dataclass(frozen=True, slots=True)
class OrderUpdated(Event):
    order: Order


@dataclass(frozen=True, slots=True)
class FillReceived(Event):
    fill: Fill


# ------------------------------------------------------------------ portfolio


@dataclass(frozen=True, slots=True)
class PositionChanged(Event):
    position: Position


@dataclass(frozen=True, slots=True)
class PortfolioUpdated(Event):
    summary: Any  # kst.portfolio.service.PortfolioSummary


@dataclass(frozen=True, slots=True)
class MarketSettled(Event):
    ticker: str
    result: str
    qty: int  # signed position at settlement
    pnl_micros: int


@dataclass(frozen=True, slots=True)
class DailyReport(Event):
    report: Any  # kst.portfolio.reports.DailyPnL


# ------------------------------------------------------------------ engine / control / risk


@dataclass(frozen=True, slots=True)
class EngineStateChanged(Event):
    old: EngineState
    new: EngineState
    actor: Actor
    reason: str = ""


@dataclass(frozen=True, slots=True)
class RiskLimitsChanged(Event):
    changes: dict[str, Any]
    actor: Actor


@dataclass(frozen=True, slots=True)
class RiskRejected(Event):
    ticker: str
    check: str
    detail: str
    strategy: str = ""


@dataclass(frozen=True, slots=True)
class Alert(Event):
    level: str  # "info" | "warning" | "critical"
    title: str
    message: str = ""
    fields: dict[str, str] = field(default_factory=dict)
