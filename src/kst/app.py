"""Composition root: builds and wires every component from Settings (manual DI — read this file to see
how the system fits together), then runs them under one Supervisor on one asyncio loop."""

from __future__ import annotations

import asyncio
import contextlib
import time
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import uvicorn

from kst.config import Settings
from kst.core.bus import EventBus
from kst.core.clock import Clock, SystemClock
from kst.core.events import CatalogRefreshed, DailyReport, FeedHealth, MarketStatusChanged
from kst.core.logging import get_logger
from kst.core.supervisor import Supervisor
from kst.core.types import Actor, EngineState, Mode
from kst.domain.fees import FeeModel
from kst.engine.control import ControlService
from kst.engine.state import EngineStateStore
from kst.engine.trader import AutoTrader
from kst.execution.broker import Broker
from kst.execution.live import LiveBroker
from kst.execution.oms import OrderManager
from kst.execution.paper import PaperBroker
from kst.feeds.crypto import CryptoFeed
from kst.feeds.weather import WeatherFeed
from kst.kalshi.auth import KalshiSigner
from kst.kalshi.ratelimit import KalshiRateLimiter
from kst.kalshi.rest import KalshiRest
from kst.kalshi.ws import KalshiWs
from kst.llm.gate import LLMGate
from kst.llm.packet import Candidate
from kst.llm.providers import build_provider
from kst.marketdata.catalog import MarketCatalog
from kst.marketdata.recorder import Recorder
from kst.marketdata.recording_state import catalog_snapshot
from kst.marketdata.service import MarketDataService
from kst.persistence.db import Database
from kst.persistence.repo import Repository
from kst.portfolio.reports import write_ledger
from kst.portfolio.service import PortfolioService
from kst.pricing.calibration import CalibratedPriorModel, CalibrationSet
from kst.pricing.crypto import CryptoDigitalModel, ReferenceStore
from kst.pricing.resolver import ModelResolver
from kst.pricing.weather import WeatherEnsembleModel, WeatherStore
from kst.risk.engine import RiskEngine
from kst.risk.limits import RiskLimits
from kst.scanner.service import ScannerService
from kst.strategies.edge_taker import EdgeTaker
from kst.strategies.market_maker import MarketMaker
from kst.strategies.router import StrategyRouter
from kst.strategies.structural_arb import StructuralArb

log = get_logger(__name__)


class _EmbeddedServer(uvicorn.Server):
    """uvicorn inside our loop: signals are handled by the application, not by uvicorn."""

    def capture_signals(self):  # type: ignore[no-untyped-def]
        return contextlib.nullcontext()


def build_signer(settings: Settings) -> KalshiSigner | None:
    k = settings.kalshi
    if not k.has_credentials or k.key_id is None:
        return None
    if k.private_key_pem is not None:
        return KalshiSigner.from_pem(k.key_id.get_secret_value(), k.private_key_pem.get_secret_value())
    assert k.private_key_path is not None
    return KalshiSigner.from_file(k.key_id.get_secret_value(), k.private_key_path)


class Application:
    def __init__(
        self,
        settings: Settings,
        *,
        live_armed: bool = False,
        scan_only: bool = False,
        clock: Clock | None = None,
    ) -> None:
        self.settings = s = settings
        self.mode = s.app.mode
        self.scan_only = scan_only
        self.clock: Clock = clock or SystemClock()
        self.bus = EventBus()
        self.started_ms = self.clock.now_ms()
        self.feed_health: dict[str, dict[str, Any]] = {}
        self.bus.subscribe(
            FeedHealth,
            lambda e: self.feed_health.__setitem__(
                e.source, {"connected": e.connected, "detail": e.detail, "ts_ms": e.ts_ms}
            ),
        )
        self.discord_bot: Any = None
        self._loop_lag_ms = 0.0

        # ---- persistence
        self.db = Database(s.db_path)
        self.repo = Repository(self.db, self.bus, self.clock)

        # ---- exchange adapter
        self.signer = build_signer(s)
        if self.mode.uses_exchange_orders and self.signer is None:
            raise RuntimeError(
                f"{self.mode.value} mode needs Kalshi API credentials (KST_KALSHI__KEY_ID / PRIVATE_KEY_PATH)"
            )
        k = s.kalshi
        self.rest = KalshiRest(
            k.rest_url,
            signer=self.signer,
            limiter=KalshiRateLimiter(
                k.read_tokens_per_s, k.write_tokens_per_s, k.read_bucket_seconds, k.write_bucket_seconds
            ),
            timeout_s=k.request_timeout_s,
            default_cost=k.default_cost,
            cancel_cost=k.cancel_cost,
            subaccount=k.subaccount,
        )
        use_ws = self.signer is not None and s.marketdata.source in ("auto", "ws")
        if s.marketdata.source == "ws" and self.signer is None:
            raise RuntimeError("marketdata.source=ws requires API credentials")
        self.recorder = Recorder(s.data_dir / "recordings") if s.marketdata.record else None

        # ---- market data
        self.catalog = MarketCatalog(
            s.universe,
            FeeModel(s.fees.taker_coef, s.fees.maker_ratio, s.fees.combo_maker_ratio),
            self.bus,
            self.clock,
        )
        if self.recorder is not None:
            recorder = self.recorder
            self.bus.subscribe(
                CatalogRefreshed, lambda e: recorder.catalog(e.ts_ms, catalog_snapshot(self.catalog))
            )
            self.bus.subscribe(
                MarketStatusChanged,
                lambda e: recorder.status(e.ts_ms, e.ticker, e.status.value, e.result, e.settlement_value),
            )
        self.md = MarketDataService(
            s.marketdata,
            self.catalog,
            self.rest,
            self.bus,
            self.clock,
            recorder=self.recorder,
            stale_ws_s=s.risk.limits.stale_ws_s,
            stale_poll_s=s.risk.limits.stale_poll_s,
        )
        if use_ws:
            assert self.signer is not None
            self.ws: KalshiWs | None = KalshiWs(
                k.ws_url,
                self.signer,
                self.md.ws_handlers(),
                on_state=self.md.on_ws_state,
                on_gap=self.md.on_ws_gap,
            )
            self.md.ws, self.md.mode = self.ws, "ws"
            self.ws.want_channel("ticker")
            self.ws.want_channel("market_lifecycle_v2")
        else:
            self.ws = None

        # ---- reference feeds + pricing models
        cp = s.pricing.crypto
        self.ref_store = ReferenceStore(
            fast_half_life_s=cp.fast_half_life_s,
            slow_half_life_s=cp.slow_half_life_s,
            fast_weight=cp.fast_weight,
            sample_s=cp.vol_sample_s,
            min_annual=cp.min_vol_annual,
            max_annual=cp.max_vol_annual,
        )
        self.crypto_feed = CryptoFeed(
            s.feeds.crypto, self.ref_store, self.bus, self.clock, kalshi_ws=self.ws, recorder=self.recorder
        )
        self.crypto_feed.attach()
        self.weather_store = WeatherStore()
        self.weather_feed = WeatherFeed(
            s.feeds.weather, self.weather_store, self.catalog, self.bus, self.clock, recorder=self.recorder
        )
        wp = s.pricing.weather
        models: list[Any] = []
        if s.feeds.crypto.enabled:
            models.append(
                CryptoDigitalModel(
                    self.ref_store,
                    s.feeds.crypto.series_index,
                    distribution=cp.distribution,
                    nu=cp.nu,
                    window_s=cp.averaging_window_s,
                    max_ref_age_s=cp.max_ref_age_s,
                    min_samples=cp.min_samples,
                    excess_kurtosis_1h=cp.excess_kurtosis_1h,
                    peers=lambda m: self.catalog.event_markets(m.event_ticker),
                    implied_weight=cp.implied_weight,
                    max_fit_spread=cp.max_fit_spread,
                )
            )
        if s.feeds.weather.enabled:
            models.append(
                WeatherEnsembleModel(
                    self.weather_store,
                    {k_: (v.tz, v.bias_f) for k_, v in s.feeds.weather.stations.items()},
                    bias_f=wp.bias_f,
                    spread_inflation=wp.spread_inflation,
                    min_bandwidth_f=wp.min_bandwidth_f,
                    model_floor=wp.model_floor,
                    max_forecast_age_s=wp.max_forecast_age_s,
                    station_inflation={
                        k_: v.spread_inflation
                        for k_, v in s.feeds.weather.stations.items()
                        if v.spread_inflation is not None
                    },
                )
            )
        self.calibration = CalibrationSet.load(s.pricing.calibration.path)
        self.resolver = ModelResolver(models, prior=CalibratedPriorModel(self.calibration))

        # ---- scanner + LLM gate
        self.scanner = ScannerService(
            s.scanner,
            s.pricing,
            s.strategies.structural_arb,
            self.catalog,
            self.md,
            self.resolver,
            self.bus,
            self.clock,
            rounding=s.fees.rounding_allowance,
            max_disagreement=s.strategies.edge_taker.max_disagreement,
        )
        provider = build_provider(s.llm.provider) if s.llm.mode != "off" else None
        escalation = build_provider(s.llm.escalation) if s.llm.escalation is not None else None
        self.gate = LLMGate(
            s.llm,
            provider,
            escalation,
            self.bus,
            self.clock,
            trading_day_tz=s.app.trading_day_tz,
            persist=self.repo.kv_set,
            record=self.repo.llm_decision,
        )

        # ---- engine state, risk, portfolio, execution
        self.state = EngineStateStore(self.bus, self.clock, persist=self.repo.kv_set)
        self.risk = RiskEngine(
            s.risk,
            self.catalog,
            self.md,
            self.bus,
            self.clock,
            mode=self.mode,
            live_armed=live_armed,
            trading_day_tz=s.app.trading_day_tz,
            allowed_categories=s.universe.categories,
            state_getter=self.state.get,
            persist=self.repo.kv_set,
        )
        self.portfolio = PortfolioService(
            self.mode,
            self.catalog,
            self.md,
            self.bus,
            self.clock,
            trading_day_tz=s.app.trading_day_tz,
            starting_cash_usd=s.paper.starting_balance_usd,
            persist=self.repo.kv_set,
            on_round_trip=self.risk.on_round_trip,
            on_entry_fill=self.risk.on_entry_fill,
            markets_entered=lambda: len(self.risk.entered_today),
        )
        if self.mode.uses_exchange_orders:
            og = s.risk.order_group.contracts_limit if s.risk.order_group.enabled else None
            self.broker: Broker = LiveBroker(
                self.rest, self.ws, self.clock, order_ttl_s=s.risk.order_ttl_s, order_group_limit=og
            )
        else:
            self.broker = PaperBroker(
                self.md, self.catalog, self.bus, self.clock, queue_haircut=s.paper.maker_queue_haircut
            )
        self.oms = OrderManager(
            self.broker,
            self.risk,
            self.bus,
            self.clock,
            min_quote_life_ms=s.strategies.market_maker.min_quote_life_ms,
            order_ttl_s=s.risk.order_ttl_s,
            tick_of=lambda t: m.grid.tick_size_at(5000) if (m := self.catalog.get(t)) else 100,
        )
        self.risk.bind(self.oms, self.portfolio)
        self.control = ControlService(
            s,
            self.state,
            self.oms,
            self.risk,
            self.portfolio,
            self.md,
            self.catalog,
            self.bus,
            self.clock,
            self.repo,
        )
        if scan_only:
            self.control.autotrade = False

        # ---- strategies + trader
        st = s.strategies
        self.edge_taker = EdgeTaker(st.edge_taker) if st.edge_taker.enabled else None
        self.market_maker = MarketMaker(st.market_maker) if st.market_maker.enabled else None
        self.arb = StructuralArb(st.structural_arb) if st.structural_arb.enabled else None
        self.router = StrategyRouter(self.resolver, self.edge_taker, self.market_maker, st.market_maker)
        self.trader = AutoTrader(
            s,
            self.scanner,
            self.router,
            self.arb,
            self.gate,
            self.oms,
            self.risk,
            self.portfolio,
            self.md,
            self.catalog,
            self.resolver,
            self.state,
            self.control,
            self.bus,
            self.clock,
        )
        self.supervisor = Supervisor(on_critical_failure=self.control.on_critical_task_failure)
        from kst.portfolio.scorecard import ScorecardRecorder

        self.scorecard = ScorecardRecorder(self)
        from kst.api.hub import WsHub

        self.hub = WsHub(self, push_hz=s.web.push_hz)
        self.bus.subscribe(DailyReport, self._on_daily_report)

    # ------------------------------------------------------------------ helpers

    def _on_daily_report(self, e: DailyReport) -> None:
        try:
            path = write_ledger(self.settings.data_dir / "pnl", e.report)
            log.info("pnl_ledger_written", path=str(path))
        except Exception:
            log.exception("pnl_ledger_failed")

    def health(self) -> dict[str, Any]:
        return {
            "mode": self.mode.value,
            "uptime_s": round((self.clock.now_ms() - self.started_ms) / 1000),
            "loop_lag_ms": round(self._loop_lag_ms, 1),
            "marketdata": self.md.health(),
            "feeds": self.feed_health,
            "scanner": None
            if self.scanner.latest is None
            else {
                "universe": self.scanner.latest.universe,
                "priced": self.scanner.latest.priced,
                "age_s": round((self.clock.now_ms() - self.scanner.latest.ts_ms) / 1000, 1),
            },
            "llm": self.gate.status(),
            "db": self.repo.health(),
            "tasks": self.supervisor.health(),
            "risk_rejections": self.risk.rejections,
            "authenticated": self.signer is not None,
        }

    def export_llm_packet(self, top: int = 20) -> str:
        snap = self.scanner.latest
        cands: list[Candidate] = []
        for r in (snap.rows if snap else [])[:top]:
            side = None
            edge = r.best_edge
            if edge is not None and r.edge_buy is not None and r.edge_sell is not None:
                from kst.core.types import BookSide

                side = BookSide.BID if r.edge_buy >= r.edge_sell else BookSide.ASK
            note = " ".join(f"{k}={v}" for k, v in list(r.detail.items())[:4])
            m = self.catalog.get(r.ticker)
            cands.append(
                Candidate(
                    r.ticker,
                    r.title,
                    r.category,
                    r.secs_to_close,
                    r.bid,
                    r.ask,
                    r.volume_24h,
                    r.open_interest,
                    r.chg_24h,
                    r.q,
                    side,
                    edge,
                    note or (m.rules_primary[:160] if m else ""),
                )
            )
        return LLMGate.export_packet(cands, self.clock.now_ms())

    # ------------------------------------------------------------------ lifecycle

    async def _restore(self) -> None:
        await self.repo.start()
        await self.scorecard.restore()
        self.state.restore(self.repo.kv_get(EngineStateStore.KEY))
        if self.state.get() is EngineState.KILLED:
            log.warning("engine_starts_killed", reason=self.state.reason)
        override = self.repo.kv_get("risk_limits_override")
        if override:
            try:
                self.risk.limits = RiskLimits.model_validate(
                    {**self.settings.risk.limits.model_dump(), **override}
                )
            except Exception:
                log.warning("risk_limits_override_invalid")
        self.risk.restore(self.repo.kv_get("risk_counters") or {})
        self.gate.restore(self.repo.kv_get("llm_budget"))
        if self.mode in (Mode.PAPER, Mode.REPLAY) and (saved := self.repo.kv_get("portfolio")):
            self.portfolio.restore(saved)
            seen = await self.repo.fill_ids()
            self.oms.restore_seen_fills(seen)
            self.portfolio.restore_seen_fills(seen)

    async def _sync_exchange(self, *, confirm: bool = True) -> None:
        started_ms = self.clock.now_ms()
        snap = await self.broker.snapshot()
        for row in snap.positions:
            await self.catalog.ensure_market(self.rest, row["ticker"])
        self.portfolio.sync_from_exchange(snap.balance_cents, snap.positions, confirm=confirm)
        # Orders placed while the snapshot was in flight may be missing from it: only prune older ones.
        self.oms.adopt_open_orders(snap.open_orders, prune_before_ms=started_ms - 60_000)

    async def _initial_catalog(self) -> None:
        for attempt in range(3):
            try:
                await self.catalog.refresh(self.rest)
                return
            except Exception as exc:
                log.warning("initial_catalog_failed", attempt=attempt, error=str(exc))
                await asyncio.sleep(2 * (attempt + 1))

    async def start(self) -> None:
        s = self.settings
        log.info(
            "kst_starting",
            mode=self.mode.value,
            md="ws" if self.ws else "poll",
            scan_only=self.scan_only,
            autotrade=self.control.autotrade,
            db=str(s.db_path),
        )
        await self._restore()
        await self.broker.start()
        if self.mode.uses_exchange_orders:
            await self._sync_exchange(confirm=False)  # startup: the exchange is the only truth we have
        await self._initial_catalog()
        if s.feeds.crypto.enabled:
            await self.crypto_feed.seed_vol()
        sup = self.supervisor
        sup.add("db_writer", self.repo.run, critical=True)
        sup.add("catalog", self._catalog_loop)
        if self.ws is not None:
            sup.add("kalshi_ws", self.md.run_ws, critical=True)
        else:
            sup.add("poll_books", self.md.run_poll_books, critical=True)
            sup.add("poll_trades", self.md.run_poll_trades)
        sup.add("settlement_watch", self._settlement_watch)
        if s.feeds.crypto.enabled and self.crypto_feed.source == "coinbase":
            sup.add("crypto_coinbase", self.crypto_feed.run_coinbase)
        if s.feeds.weather.enabled:
            sup.add("weather", self.weather_feed.run)
        sup.add("scanner", self.scanner.run, critical=True)
        sup.add("portfolio", self.portfolio.run, critical=True)
        sup.add("risk_monitor", self._risk_loop, critical=True)
        sup.add("llm_gate", self.gate.run)
        sup.add("loop_lag", self._lag_loop)
        sup.add("daily_report_timer", self._report_timer)
        if not self.scan_only:
            sup.add("trader", self.trader.run, critical=True)
        if self.mode.uses_exchange_orders:
            assert isinstance(self.broker, LiveBroker)
            sup.add("fill_reconcile", self.broker.run_reconcile)
            sup.add("exchange_sync", self._exchange_sync_loop)
            sup.add("order_ttl", self._ttl_loop)
            sup.add("dead_mans_switch", self._dead_mans_switch, critical=True)
        if s.web.enabled:
            sup.add("web", self._serve_web, max_restarts=3)
            sup.add("ws_hub", self.hub.run)
        if s.discord.enabled:
            from kst.discord_bot.bot import run_bot

            sup.add("discord", lambda: run_bot(self), max_restarts=50)
        sup.start()
        log.info("kst_started", web=f"http://{s.web.host}:{s.web.port}" if s.web.enabled else None)

    async def stop(self) -> None:
        log.info("kst_stopping")
        if self.mode.uses_exchange_orders and self.settings.engine.cancel_on_shutdown:
            with contextlib.suppress(Exception):
                await self.oms.cancel_where(lambda o: o.strategy not in ("manual",))
        report = self.portfolio.daily_report()
        with contextlib.suppress(Exception):
            write_ledger(self.settings.data_dir / "pnl", report)
        async with contextlib.AsyncExitStack() as cleanup:
            cleanup.push_async_callback(self.rest.aclose)
            cleanup.push_async_callback(self.repo.stop)
            if self.recorder:
                cleanup.callback(self.recorder.close)
            cleanup.push_async_callback(self.broker.stop)
            await self.supervisor.stop()
        log.info("kst_stopped")

    # ------------------------------------------------------------------ background loops

    async def _catalog_loop(self) -> None:
        # REST mode: the catalog pull is also the universe-wide top-of-book refresh.
        every = (
            self.settings.universe.catalog_refresh_s
            if self.ws
            else self.settings.marketdata.poll_markets_interval_s
        )
        while True:
            await asyncio.sleep(every)
            pinned = {p.ticker for p in self.portfolio.positions.values() if p.qty} | {
                o.ticker for o in self.oms.live_orders()
            }
            self.catalog.pinned = pinned
            await self.catalog.refresh(self.rest)

    async def _settlement_watch(self) -> None:
        """Backstop for settlement: poll markets we hold once they have closed (lifecycle WS may be absent)."""
        from kst.core.events import MarketStatusChanged
        from kst.core.fixedpoint import price_from_str
        from kst.core.types import MarketStatus

        while True:
            await asyncio.sleep(60)
            now = self.clock.now_ms()
            for p in list(self.portfolio.positions.values()):
                m = self.catalog.get(p.ticker)
                if not p.qty or m is None or m.close_time_ms is None or now < m.close_time_ms:
                    continue
                try:
                    raw = await self.rest.market(p.ticker)
                except Exception:
                    continue
                result = raw.get("result") or ""
                status = MarketStatus.parse(raw.get("status"))
                if result and status in (
                    MarketStatus.DETERMINED,
                    MarketStatus.FINALIZED,
                    MarketStatus.AMENDED,
                ):
                    sv = raw.get("settlement_value_dollars")
                    self.bus.publish(
                        MarketStatusChanged(
                            now, p.ticker, MarketStatus.DETERMINED, result, price_from_str(sv) if sv else None
                        )
                    )

    async def _risk_loop(self) -> None:
        while True:
            await asyncio.sleep(1.0)
            self.risk.on_portfolio(self.portfolio.summary())

    async def _lag_loop(self) -> None:
        while True:
            t0 = time.monotonic()
            await asyncio.sleep(0.5)
            self._loop_lag_ms = max(0.0, (time.monotonic() - t0 - 0.5) * 1000)
            if self._loop_lag_ms > 500:
                log.warning("event_loop_lag", lag_ms=round(self._loop_lag_ms))

    async def _report_timer(self) -> None:
        """Publish an intraday P&L report at discord.daily_report_time (trading-day tz)."""
        tz = ZoneInfo(self.settings.app.trading_day_tz)
        hh, mm = (int(x) for x in self.settings.discord.daily_report_time.split(":"))
        last = ""
        while True:
            await asyncio.sleep(20)
            now = datetime.now(tz)
            key = now.strftime("%Y-%m-%d")
            if (now.hour, now.minute) >= (hh, mm) and key != last:
                last = key
                self.bus.publish(DailyReport(self.clock.now_ms(), self.portfolio.daily_report()))

    async def _exchange_sync_loop(self) -> None:
        while True:
            await asyncio.sleep(30)
            try:
                await self._sync_exchange()
            except Exception as exc:
                log.warning("exchange_sync_failed", error=str(exc))

    async def _ttl_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            await self.oms.refresh_ttls()

    async def _dead_mans_switch(self) -> None:
        """Exchange WS down with resting orders for too long → cancel everything and pause."""
        down_since: float | None = None
        while True:
            await asyncio.sleep(1.0)
            if self.ws is None or self.ws.connected:
                down_since = None
                continue
            down_since = down_since or time.monotonic()
            if (
                time.monotonic() - down_since >= self.settings.risk.ws_down_cancel_after_s
                and self.oms.live_orders()
            ):
                log.error("dead_mans_switch", down_s=round(time.monotonic() - down_since))
                with contextlib.suppress(Exception):
                    await self.oms.cancel_all()
                if self.state.get() is EngineState.RUNNING:
                    await self.control.pause(Actor.SYSTEM, "exchange WS down — orders cancelled")
                down_since = None

    async def _serve_web(self) -> None:
        from kst.api.app import create_app

        w = self.settings.web
        config = uvicorn.Config(
            create_app(self),
            host=w.host,
            port=w.port,
            log_level="warning",
            lifespan="off",
            ws="websockets-sansio",
        )
        await _EmbeddedServer(config).serve()


async def run_application(app: Application, stop: asyncio.Event) -> None:
    try:
        await app.start()
        await stop.wait()
    finally:
        await app.stop()
