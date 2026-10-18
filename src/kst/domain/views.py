"""Read-only views (ports) that pure layers (scanner, strategies, risk checks) depend on."""

from __future__ import annotations

from typing import Protocol

from kst.domain.fees import FeeSchedule
from kst.domain.market import Event, Market
from kst.domain.orderbook import OrderBook


class CatalogView(Protocol):
    def get(self, ticker: str) -> Market | None: ...

    def event(self, event_ticker: str) -> Event | None: ...

    def event_markets(self, event_ticker: str) -> list[Market]: ...

    def fees(self, market: Market) -> FeeSchedule: ...


class BookView(Protocol):
    def book(self, ticker: str) -> OrderBook | None: ...

    def is_fresh(self, ticker: str, now_ms: int) -> bool: ...
