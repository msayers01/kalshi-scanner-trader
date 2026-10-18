"""Shared test harness: a fully wired paper engine with no network (books are set by the test)."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from kst.config import Settings
from kst.core.bus import EventBus
from kst.core.clock import ManualClock
from kst.core.events import BookUpdated, TradePrinted
from kst.core.fixedpoint import Price, Qty
from kst.core.types import BookSide, MarketStatus, Mode, StrikeType
from kst.domain.fees import FeeModel
from kst.domain.market import Event, Market
from kst.domain.orderbook import OrderBook
from kst.engine.control import ControlService
from kst.engine.state import EngineStateStore
from kst.execution.oms import OrderManager
from kst.execution.paper import PaperBroker
from kst.kalshi.rest import KalshiRest
from kst.marketdata.catalog import MarketCatalog
from kst.marketdata.service import MarketDataService
from kst.portfolio.service import PortfolioService
from kst.risk.engine import RiskEngine

T0 = 1_792_300_000_000  # 2026-10-18-ish


@dataclass
class Harness:
    settings: Settings
    clock: ManualClock
    bus: EventBus
    catalog: MarketCatalog
    md: MarketDataService
    risk: RiskEngine
    portfolio: PortfolioService
    broker: PaperBroker
    oms: OrderManager
    state: EngineStateStore
    control: ControlService

    def add_market(
        self,
        ticker: str,
        event: str = "EV-1",
        *,
        category: str = "Crypto",
        close_in_s: float = 7200,
        series: str = "S",
    ) -> Market:
        m = Market(
            ticker=ticker,
            event_ticker=event,
            series_ticker=series,
            title=ticker,
            category=category,
            status=MarketStatus.ACTIVE,
            strike_type=StrikeType.GREATER,
            floor_strike=100.0,
            close_time_ms=self.clock.now_ms() + int(close_in_s * 1000),
        )
        self.catalog.markets[ticker] = m
        ev = self.catalog.events.setdefault(event, Event(event, series, category=category))
        ev.market_tickers.append(ticker)
        self.catalog.universe.add(ticker)
        return m

    def set_book(self, ticker: str, bids: list[tuple[float, float]], asks: list[tuple[float, float]]) -> None:
        b = self.md.books.setdefault(ticker, OrderBook(ticker))
        b.apply_snapshot(
            [(Price(round(p * 10_000)), Qty(round(q * 100))) for p, q in bids],
            [(Price(round(p * 10_000)), Qty(round(q * 100))) for p, q in asks],
            self.clock.now_ms(),
        )
        m = self.catalog.get(ticker)
        if m is not None:
            m.yes_bid, m.yes_ask = b.best_bid, b.best_ask
        self.bus.publish(BookUpdated(self.clock.now_ms(), ticker))

    def trade(self, ticker: str, price: float, qty: float, taker: BookSide) -> None:
        self.bus.publish(
            TradePrinted(
                self.clock.now_ms(), ticker, Price(round(price * 10_000)), Qty(round(qty * 100)), taker
            )
        )


@pytest.fixture
def h() -> Harness:
    settings = Settings()
    settings.app.mode = Mode.PAPER
    clock = ManualClock(T0)
    bus = EventBus()
    catalog = MarketCatalog(settings.universe, FeeModel(), bus, clock)
    md = MarketDataService(
        settings.marketdata, catalog, KalshiRest("http://unused.invalid/trade-api/v2"), bus, clock
    )
    state = EngineStateStore(bus, clock)
    risk = RiskEngine(
        settings.risk,
        catalog,
        md,
        bus,
        clock,
        mode=Mode.PAPER,
        live_armed=False,
        trading_day_tz=settings.app.trading_day_tz,
        state_getter=state.get,
    )
    portfolio = PortfolioService(
        Mode.PAPER,
        catalog,
        md,
        bus,
        clock,
        trading_day_tz=settings.app.trading_day_tz,
        starting_cash_usd=1000.0,
        on_round_trip=risk.on_round_trip,
        on_entry_fill=risk.on_entry_fill,
        markets_entered=lambda: len(risk.entered_today),
    )
    broker = PaperBroker(md, catalog, bus, clock)
    oms = OrderManager(broker, risk, bus, clock, min_quote_life_ms=0)
    risk.bind(oms, portfolio)
    control = ControlService(settings, state, oms, risk, portfolio, md, catalog, bus, clock)
    return Harness(settings, clock, bus, catalog, md, risk, portfolio, broker, oms, state, control)


@pytest.fixture
async def hs(h: Harness) -> Harness:
    """Harness with the paper broker started (subscribed to trades/books)."""
    await h.broker.start()
    return h
