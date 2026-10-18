"""Market universe: events, markets, series metadata, and the universe filter.

Refreshed from REST every `universe.catalog_refresh_s`. Market objects are updated *in place* so
references held elsewhere stay valid; top-of-book stats are refreshed by the market-data service.
"""

from __future__ import annotations

from kst.config import UniverseCfg
from kst.core.bus import EventBus
from kst.core.clock import Clock
from kst.core.events import CatalogRefreshed
from kst.core.logging import get_logger
from kst.domain.fees import FeeModel, FeeSchedule
from kst.domain.market import Event, Market, Series, series_from_event_ticker
from kst.kalshi.parse import parse_event, parse_market, parse_series
from kst.kalshi.rest import KalshiRest

log = get_logger(__name__)

_META_FIELDS = (
    "title",
    "yes_sub_title",
    "status",
    "open_time_ms",
    "close_time_ms",
    "expected_expiration_ms",
    "strike_type",
    "floor_strike",
    "cap_strike",
    "rules_primary",
    "grid",
    "exchange_index",
    "can_close_early",
    "fee_waiver_until_ms",
    "result",
    "category",
    "series_ticker",
)


class MarketCatalog:
    """Implements `kst.domain.views.CatalogView`."""

    def __init__(self, cfg: UniverseCfg, fee_model: FeeModel, bus: EventBus, clock: Clock) -> None:
        self.cfg = cfg
        self.fee_model = fee_model
        self.bus = bus
        self.clock = clock
        self.markets: dict[str, Market] = {}
        self.events: dict[str, Event] = {}
        self.series: dict[str, Series] = {}
        self.universe: set[str] = set()
        self.pinned: set[str] = (
            set()
        )  # markets we must keep (positions/orders) even if they leave the universe
        self._series_loaded_ms = 0
        self.last_refresh_ms = 0

    # ------------------------------------------------------------------ queries

    def get(self, ticker: str) -> Market | None:
        return self.markets.get(ticker)

    def event(self, event_ticker: str) -> Event | None:
        return self.events.get(event_ticker)

    def event_markets(self, event_ticker: str) -> list[Market]:
        ev = self.events.get(event_ticker)
        if ev is None:
            return []
        return [m for t in ev.market_tickers if (m := self.markets.get(t)) is not None]

    def series_of(self, market: Market) -> Series | None:
        return self.series.get(market.series_ticker)

    def fees(self, market: Market) -> FeeSchedule:
        s = self.series.get(market.series_ticker)
        waived = market.fee_waiver_until_ms is not None and market.fee_waiver_until_ms > self.clock.now_ms()
        return self.fee_model.schedule(
            s.fee_type if s else "quadratic", s.fee_multiplier if s else 1.0, waived=waived
        )

    def universe_markets(self) -> list[Market]:
        return [m for t in self.universe if (m := self.markets.get(t)) is not None]

    # ------------------------------------------------------------------ filtering

    def in_universe(self, m: Market, now_ms: int) -> bool:
        c = self.cfg
        if not m.is_tradable:
            return False
        if c.include_series and m.series_ticker not in c.include_series:
            return False
        if m.series_ticker in c.exclude_series:
            return False
        if c.categories and m.category not in c.categories:
            return False
        if m.category in c.exclude_categories:
            return False
        secs = m.seconds_to_close(now_ms)
        if secs is None or secs < c.min_seconds_to_close or secs > c.max_hours_to_close * 3600:
            return False
        return m.volume_24h >= c.min_volume_24h * 100

    # ------------------------------------------------------------------ refresh

    def upsert_market(self, new: Market) -> Market:
        cur = self.markets.get(new.ticker)
        if cur is None:
            self.markets[new.ticker] = new
            return new
        for f in _META_FIELDS:
            setattr(cur, f, getattr(new, f))
        if new.stats_ts_ms >= cur.stats_ts_ms or cur.stats_ts_ms == 0:
            for f in (
                "yes_bid",
                "yes_ask",
                "yes_bid_size",
                "yes_ask_size",
                "last_price",
                "previous_price",
                "volume",
                "volume_24h",
                "open_interest",
            ):
                setattr(cur, f, getattr(new, f))
        return cur

    async def refresh_series(self, rest: KalshiRest) -> None:
        rows = await rest.series_list()
        self.series = {s.ticker: s for s in (parse_series(r) for r in rows)}
        self._series_loaded_ms = self.clock.now_ms()
        log.info("series_loaded", n=len(self.series))

    async def refresh(self, rest: KalshiRest) -> None:
        """Pull open markets closing inside the horizon (also refreshes their top-of-book stats), then
        fetch metadata only for events we haven't seen. Cost ≈ (#markets/500) pages + new events/50."""
        now = self.clock.now_ms()
        if not self.series or now - self._series_loaded_ms > self.cfg.series_refresh_s * 1000:
            try:
                await self.refresh_series(rest)
            except Exception:
                log.exception("series_refresh_failed")
        base: dict[str, object] = {
            "status": "open",
            "mve_filter": "exclude",
            "min_close_ts": now // 1000 + int(self.cfg.min_seconds_to_close),
            "max_close_ts": now // 1000 + int(self.cfg.max_hours_to_close * 3600),
        }
        queries = [dict(base, series_ticker=sr) for sr in self.cfg.include_series] or [base]
        seen: set[str] = set()
        for q in queries:
            async for raw in rest.markets(page_delay_s=self.cfg.page_delay_s, **q):
                ev_t = raw.get("event_ticker", "")
                ser = self.series.get(series_from_event_ticker(ev_t))
                m = parse_market(raw, category=ser.category if ser else "")
                m.stats_ts_ms = now
                if ser is None and (ev := self.events.get(ev_t)) is not None:
                    m.category, m.series_ticker = ev.category, ev.series_ticker
                self.upsert_market(m)
                seen.add(m.ticker)
        # Event metadata (mutually_exclusive, category) for events not cached yet.
        ev_tickers = {self.markets[t].event_ticker for t in seen}
        missing = sorted(e for e in ev_tickers if e and e not in self.events)
        if missing:
            try:
                for raw in await rest.events_by_ticker(missing):
                    ev, _ = parse_event(raw)
                    if not ev.category and (ser := self.series.get(ev.series_ticker)):
                        ev.category = ser.category
                    self.events[ev.event_ticker] = ev
            except Exception:
                log.exception("event_metadata_failed", n=len(missing))
        # Rebuild event membership from the markets we hold.
        members: dict[str, list[str]] = {}
        for t in seen | self.pinned:
            if (mk := self.markets.get(t)) is not None:
                members.setdefault(mk.event_ticker, []).append(t)
                if (ev := self.events.get(mk.event_ticker)) is not None and not mk.category:
                    mk.category = ev.category
        for e, ts in members.items():
            if (ev := self.events.get(e)) is not None:
                ev.market_tickers = sorted(ts)
        for e in [e for e in self.events if e not in members]:
            del self.events[e]
        for t in list(self.markets):
            if t not in seen and t not in self.pinned:
                del self.markets[t]
        self.universe = {t for t in seen if self.in_universe(self.markets[t], now)}
        self.last_refresh_ms = now
        log.info(
            "catalog_refreshed",
            events=len(self.events),
            markets=len(self.markets),
            universe=len(self.universe),
        )
        self.bus.publish(CatalogRefreshed(now, len(self.universe), len(self.events)))

    async def ensure_market(self, rest: KalshiRest, ticker: str) -> Market | None:
        """Fetch a single market (e.g. a position outside the universe) and pin it."""
        m = self.markets.get(ticker)
        if m is not None:
            return m
        try:
            raw = await rest.market(ticker)
        except Exception:
            log.warning("ensure_market_failed", ticker=ticker)
            return None
        if not raw:
            return None
        m = self.upsert_market(parse_market(raw))
        ser = self.series.get(m.series_ticker)
        m.category = m.category or (ser.category if ser else "")
        self.pinned.add(ticker)
        return m
