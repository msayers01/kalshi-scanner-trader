"""Offline replay through the production scanner, strategies, risk engine, OMS and PaperBroker.

No Application.start(), network feeds, exchange reconciliation, LLM calls or wall-clock runners
are started. Each variant gets a fresh database and deterministic manual-clock schedule.
"""

from __future__ import annotations

import asyncio
import csv
import hashlib
import itertools
import json
import math
from collections import Counter
from collections.abc import AsyncGenerator
from contextlib import aclosing
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np

from kst.app import Application
from kst.backtest.metrics import sharpe
from kst.backtest.recordings import Record, integer, record_groups
from kst.config import Settings
from kst.core.clock import ManualClock
from kst.core.events import (
    BookUpdated,
    FillReceived,
    MarketStatusChanged,
    OrderUpdated,
    PortfolioUpdated,
    ScanUpdated,
    TopOfBookUpdated,
    TradePrinted,
)
from kst.core.types import BookSide, MarketStatus, Mode
from kst.domain.market import Event
from kst.domain.orderbook import OrderBook
from kst.kalshi.parse import parse_market, update_market_stats
from kst.marketdata.recording_state import restore_catalog
from kst.portfolio.scorecard import paper_scorecard
from kst.pricing.calibration import CalibratedPriorModel, CalibrationSet
from kst.pricing.weather import EnsembleForecast, Observation

SWEEPS = {
    "s_p_base": "pricing.s_p_base",
    "z": "strategies.edge_taker.z",
    "kelly": "strategies.edge_taker.kelly_fraction",
    "implied_weight": "pricing.crypto.implied_weight",
    "queue_haircut": "paper.maker_queue_haircut",
}


def variants(
    settings: Settings, sweeps: list[str], sensitivity: bool = False
) -> list[tuple[Settings, dict[str, float]]]:
    axes: dict[str, list[float]] = {}
    for spec in sweeps:
        key, sep, text = spec.partition("=")
        key = SWEEPS.get(key, key)
        if not sep or key not in SWEEPS.values() or key in axes:
            raise ValueError(
                f"Invalid or repeated sweep {spec!r}; use one of {', '.join(SWEEPS)}=value,value"
            )
        try:
            values = list(dict.fromkeys(float(v) for v in text.split(",")))
        except ValueError as exc:
            raise ValueError(f"Invalid numeric sweep {spec!r}") from exc
        for value in values:
            if not math.isfinite(value) or value < 0:
                raise ValueError(f"Sweep values must be finite and nonnegative: {spec}")
            if key == "pricing.s_p_base" and value == 0:
                raise ValueError("s_p_base must be positive")
            if key in ("strategies.edge_taker.kelly_fraction", "pricing.crypto.implied_weight") and value > 1:
                raise ValueError(f"{key} must be between 0 and 1")
        axes[key] = values
    if sensitivity:
        axes.setdefault(
            "paper.maker_queue_haircut", list(dict.fromkeys([settings.paper.maker_queue_haircut, 2.0, 5.0]))
        )
    if math.prod(len(v) for v in axes.values()) > 64:
        raise ValueError("Sweep exceeds 64 variants; narrow the grid")
    out = []
    for combination in itertools.product(*axes.values()):
        candidate = settings.model_copy(deep=True)
        params = dict(zip(axes, combination, strict=True))
        for key, value in params.items():
            parts = key.split(".")
            obj: Any = candidate
            for part in parts[:-1]:
                obj = getattr(obj, part)
            setattr(obj, parts[-1], value)
        out.append((candidate, params))
    return out


def _levels(raw: Any) -> list[tuple[int, int]]:
    if not isinstance(raw, list):
        raise ValueError("book levels must be lists of [price, quantity]")
    return [(integer(p, "price", 0, 10_000), integer(q, "quantity", 1)) for p, q in raw]


def apply_record(app: Application, record: Record) -> None:
    r, now = record.payload, record.ts_ms
    source = r.get("source_t", r["t"])
    kind = record.kind
    if kind == "catalog":
        restore_catalog(app.catalog, r, now)
    elif kind == "mkt":
        m = parse_market(r["raw"], category=r["raw"].get("category", ""))
        app.catalog.upsert_market(m)
        ev = app.catalog.events.setdefault(
            m.event_ticker, Event(m.event_ticker, m.series_ticker, category=m.category)
        )
        if m.ticker not in ev.market_tickers:
            ev.market_tickers.append(m.ticker)
        if app.catalog.in_universe(m, now):
            app.catalog.universe.add(m.ticker)
    elif kind in ("book", "delta", "trade", "status", "ticker"):
        ticker = r["m"]
        market = app.catalog.get(ticker)
        if market is None:
            raise ValueError(
                f"{ticker}: no preceding market metadata; collect a new recording with catalog snapshots"
            )
        m = market
        if kind == "ticker":
            update_market_stats(m, r["raw"], source)
            app.bus.publish(TopOfBookUpdated(now, ticker))
            return
        if kind == "book":
            b = app.md.books.setdefault(ticker, OrderBook(ticker))
            b.apply_snapshot(_levels(r["b"]), _levels(r["a"]), source)
        elif kind == "delta":
            existing_book = app.md.book(ticker)
            if existing_book is None or existing_book.ts_ms == 0:
                raise ValueError(f"{ticker}: delta before initial snapshot")
            b = existing_book
            delta = r["d"]
            if type(delta) is not int:
                raise ValueError("delta must be an integer")
            b.apply_delta(BookSide(r["s"]), integer(r["p"], "price", 0, 10_000), delta, source)
        elif kind == "trade":
            p = integer(r["p"], "price", 0, 10_000)
            q = integer(r["q"], "quantity", 1)
            app.bus.publish(TradePrinted(source, ticker, p, q, BookSide(r["s"])))
            return
        else:
            status = MarketStatus(r["status"])
            payout = r.get("payout")
            if payout is not None:
                integer(payout, "payout", 0, 10_000)
            m.status, m.result = status, r.get("result", "")
            app.bus.publish(MarketStatusChanged(now, ticker, status, m.result, payout))
            return
        m.yes_bid, m.yes_ask = b.best_bid, b.best_ask
        m.yes_bid_size = b.best_bid_qty
        m.yes_ask_size = b.best_ask_qty
        m.stats_ts_ms = source
        app.md.last_book_poll_ms = source
        app.bus.publish(BookUpdated(now, ticker))
    elif kind == "ref":
        value = float(r["value"] if "value" in r else r["v"])
        if not math.isfinite(value) or value <= 0:
            raise ValueError("reference price must be finite and positive")
        app.ref_store.on_price(r["i"], source, value, r.get("src", "recorded"))
    elif kind == "seed":
        closes = [float(v) for v in r["closes"]]
        interval = float(r["interval_s"])
        if (
            not closes
            or any(not math.isfinite(v) or v <= 0 for v in closes)
            or not math.isfinite(interval)
            or interval <= 0
        ):
            raise ValueError("invalid volatility seed")
        app.ref_store.get(r["i"]).vol.seed_from_closes(closes, interval)
    elif kind == "weather":
        series = r["series"]
        if "forecast" in r:
            f = r["forecast"]
            issued = integer(f["issued_ms"], "forecast issue time", 0, now)
            times = np.asarray(f["times_ms"], dtype=np.int64)
            members = np.asarray(f["members_f"], dtype=float)
            if times.ndim != 1 or members.ndim != 2 or members.shape[1] != len(times) or not len(times):
                raise ValueError("invalid forecast array shapes")
            app.weather_store.forecasts[series] = EnsembleForecast(issued, times, members)
        if "observation" in r:
            o = r["observation"]
            ts = integer(o["ts_ms"], "observation time", 0, now)
            value = float(o["max_f"])
            if not math.isfinite(value):
                raise ValueError("invalid weather observation")
            app.weather_store.observations[series] = Observation(date.fromisoformat(o["day"]), value, ts)


async def replay(
    settings: Settings, paths: list[Path], output: Path, params: dict[str, float] | None = None
) -> dict[str, Any]:
    async with aclosing(record_groups(paths)) as groups:
        return await _replay(settings, groups, output, params)


async def _replay(
    settings: Settings,
    groups: AsyncGenerator[tuple[int, list[Record]], None],
    output: Path,
    params: dict[str, float] | None,
) -> dict[str, Any]:
    first_group = await anext(groups, None)
    if first_group is None:
        raise ValueError("Recording is empty")
    first = first_group[1][0]
    s = settings.model_copy(deep=True)
    s.app.mode, s.app.data_dir = Mode.REPLAY, await asyncio.to_thread(output.resolve)
    s.pricing.calibration.path = s.app.data_dir / "disabled-calibration.json"
    s.web.enabled = s.discord.enabled = s.marketdata.record = False
    s.kalshi.key_id = s.kalshi.private_key_pem = s.kalshi.private_key_path = None
    s.marketdata.source = "poll"
    s.llm.mode, s.llm.escalation = "off", None
    s.engine.autotrade = True
    if not math.isfinite(s.paper.starting_balance_usd) or s.paper.starting_balance_usd <= 0:
        raise ValueError("Starting balance must be finite and positive")
    intervals = (s.engine.tick_s, s.scanner.interval_s, s.replay.equity_sample_s)
    if (
        any(not math.isfinite(v) for v in (*intervals, s.replay.warmup_s, s.paper.maker_queue_haircut))
        or s.replay.warmup_s < 0
        or s.paper.maker_queue_haircut < 0
    ):
        raise ValueError("Replay timing and queue settings must be finite and nonnegative")
    tick_ms, scan_ms, sample_ms = (round(v * 1000) for v in intervals)
    if min(tick_ms, scan_ms, sample_ms) < 1:
        raise ValueError("Replay tick, scan and sampling intervals must be at least 1 ms")
    await asyncio.to_thread(output.mkdir, parents=True, exist_ok=False)
    clock = ManualClock(first.ts_ms)
    app = Application(s, clock=clock)
    # Today's fitted calibration could leak future outcomes into an older recording.
    app.calibration = CalibrationSet()
    app.resolver.prior = CalibratedPriorModel(app.calibration)
    ids = itertools.count(1)
    app.oms.new_client_id = lambda strategy: f"kst-{strategy}-{next(ids):012d}"
    fills: list[dict[str, Any]] = []
    outcomes: list[int] = []
    order_ids: set[str] = set()

    def on_fill(e: FillReceived) -> None:
        f = e.fill
        fills.append(
            {
                "ts_ms": f.ts_ms,
                "ticker": f.ticker,
                "strategy": f.strategy,
                "side": f.side.value,
                "price_units": f.price,
                "qty_units": f.qty,
                "fee_micros": f.fee,
                "taker": f.is_taker,
            }
        )

    def on_round_trip(pnl: int) -> None:
        outcomes.append(pnl)
        app.risk.on_round_trip(pnl)

    app.portfolio.on_round_trip = on_round_trip
    app.bus.subscribe(FillReceived, on_fill)
    app.bus.subscribe(OrderUpdated, lambda e: order_ids.add(e.order.client_order_id))
    breaches: list[tuple[str, str]] = []

    async def on_breach(action: str, reason: str) -> None:
        breaches.append((action, reason))

    app.risk.on_breach = on_breach

    async def drain_breaches() -> None:
        await asyncio.sleep(0)
        while breaches:
            action, reason = breaches.pop(0)
            await app.control._on_breach(action, reason)

    start = first.ts_ms
    next_tick = next_scan = next_risk = start
    next_sample = start + sample_ms
    starting = app.portfolio.equity()
    peak, max_dd = starting, 0
    equity: list[tuple[int, int]] = [(start, starting)]
    counts: Counter[str] = Counter()
    legacy = clamped = 0
    synthetic = False

    async def step(now: int) -> None:
        nonlocal next_tick, next_scan, next_risk, next_sample, peak, max_dd
        clock.set(now)
        app.portfolio.roll_day_if_needed()
        app.risk.roll_day_if_needed()
        # Cancel expired orders before a new strategy evaluation, even without book/trade events.
        for o in list(app.oms.live_orders()):
            if o.expiration_ts is not None and now >= o.expiration_ts * 1000:
                await app.oms.cancel(o)
        if now >= next_risk:
            summary = app.portfolio.summary()
            app.risk.on_portfolio(summary)
            app.bus.publish(PortfolioUpdated(now, summary))
            await drain_breaches()
            next_risk += 1000
        if now >= next_scan:
            app.catalog.universe = {
                t for t in app.catalog.universe if app.catalog.in_universe(app.catalog.markets[t], now)
            }
            snap = app.scanner.scan_once()
            app.scanner.latest = snap
            app.bus.publish(ScanUpdated(now, snap))
            next_scan += scan_ms
        if now >= next_tick:
            if now - start >= round(s.replay.warmup_s * 1000):
                await app.trader.tick()
            next_tick += tick_ms
            await drain_breaches()
        if now >= next_sample:
            equity.append((now, app.portfolio.equity()))
            next_sample += sample_ms
            await app.repo.flush()
        value = app.portfolio.equity()
        peak = max(peak, value)
        max_dd = max(max_dd, peak - value)

    try:
        await app.repo.start()
        await app.broker.start()

        async def all_groups() -> AsyncGenerator[tuple[int, list[Record]], None]:
            assert first_group is not None
            yield first_group
            async for group in groups:
                yield group

        async for now, group in all_groups():
            while min(next_tick, next_scan, next_risk, next_sample) < now:
                await step(min(next_tick, next_scan, next_risk, next_sample))
            clock.set(now)
            app.portfolio.roll_day_if_needed()
            app.risk.roll_day_if_needed()
            for record in group:
                counts[record.kind] += 1
                legacy += record.legacy
                clamped += record.clamped
                synthetic = synthetic or bool(record.payload.get("synthetic"))
                try:
                    apply_record(app, record)
                except (ValueError, TypeError, KeyError, IndexError) as exc:
                    raise ValueError(f"{record.location}: {exc}") from exc
                if record.kind in ("status", "catalog", "mkt"):
                    await app.oms.cancel_where(
                        lambda o: (market := app.catalog.get(o.ticker)) is None or not market.is_tradable
                    )
            await drain_breaches()
            if min(next_tick, next_scan, next_risk, next_sample) <= now:
                await step(now)
            value = app.portfolio.equity()
            peak = max(peak, value)
            max_dd = max(max_dd, peak - value)
        if not counts["catalog"] and not counts["mkt"]:
            raise ValueError("No recorded market metadata; this recording cannot be backtested")
        await app.oms.cancel_all()
        await app.repo.flush()
        summary = app.portfolio.summary()
        warnings = [
            "LLM gate disabled for offline quantitative-strategy comparison; fitted calibration disabled.",
            "Replay covers only recorded markets/books; queue and replenishment assumptions affect simulated fills.",
        ]
        if synthetic:
            warnings.append("SYNTHETIC demonstration inputs; these results are not historical performance.")
        if counts["mkt"]:
            warnings.append(
                "Raw mkt records lack full event/series snapshots; default fees and event metadata may differ from historical settings."
            )
        if legacy:
            warnings.append(
                f"{legacy} legacy records lack receipt-time guarantees and may omit fees or event metadata; {clamped} late timestamps clamped in file order."
            )
        if not counts["seed"]:
            warnings.append("No volatility seed; crypto models wait for enough recorded reference samples.")
        if not counts["weather"]:
            warnings.append("No recorded weather inputs; weather models cannot price markets.")
        if summary.n_positions:
            warnings.append(
                "Open positions are marked, not liquidated. End P&L includes unrealized value; review stale marks."
            )
        if not fills:
            warnings.append("No simulated fills; this run provides no evidence of strategy profitability.")
        config = {
            k: s.model_dump(mode="json")[k]
            for k in (
                "universe",
                "pricing",
                "scanner",
                "strategies",
                "risk",
                "engine",
                "paper",
                "fees",
                "replay",
            )
        }
        config["pricing"]["calibration"] = {"source": "identity", "enabled": False}
        config["app"] = {"mode": "replay", "trading_day_tz": s.app.trading_day_tz}
        config["marketdata"] = s.marketdata.model_dump(mode="json")
        config["feeds"] = s.feeds.model_dump(mode="json")
        config["feeds"]["weather"].pop("user_agent", None)
        report: dict[str, Any] = {
            "schema": 1,
            "synthetic": synthetic,
            "mode": "replay",
            "start_ms": start,
            "end_ms": clock.now_ms(),
            "parameters": params or {},
            "record_counts": dict(sorted(counts.items())),
            "configuration": config,
            "warnings": warnings,
            "metrics": {
                "starting_equity_usd": starting / 1e6,
                "ending_equity_usd": summary.equity / 1e6,
                "net_pnl_usd": (summary.equity - starting) / 1e6,
                "return_pct": (summary.equity - starting) / starting * 100,
                "gross_realized_usd": sum(p.realized for p in app.portfolio.positions.values()) / 1e6,
                "fees_usd": sum(f["fee_micros"] for f in fills) / 1e6,
                "unrealized_usd": summary.unrealized / 1e6,
                "max_drawdown_usd": max_dd / 1e6,
                "orders": len(order_ids),
                "fills": len(fills),
                "round_trips": len(outcomes),
                "hit_rate": sum(p > 0 for p in outcomes) / len(outcomes) if outcomes else None,
                "equity_sample_seconds": sample_ms / 1000,
                **sharpe(equity, sample_ms),
                "risk_rejections": dict(sorted(app.risk.rejections.items())),
                "final_state": app.state.get().value,
            },
            "open_positions": [
                {
                    "ticker": p.ticker,
                    "qty_units": p.qty,
                    "mark_units": app.portfolio.mark(p.ticker),
                    "book_fresh": app.md.is_fresh(p.ticker, clock.now_ms()),
                }
                for p in app.portfolio.positions.values()
                if p.qty
            ],
            "scorecard": await paper_scorecard(app.repo, clock.now_ms(), "replay"),
        }
        if not equity or equity[-1][0] != clock.now_ms():
            equity.append((clock.now_ms(), summary.equity))
        else:
            equity[-1] = (clock.now_ms(), summary.equity)

        def write() -> None:
            (output / "report.json").write_text(
                json.dumps(report, indent=2, allow_nan=False) + "\n", encoding="utf-8"
            )
            with (output / "equity.csv").open("w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerow(["ts_ms", "equity_micros"])
                writer.writerows(equity)
            with (output / "fills.csv").open("w", newline="", encoding="utf-8") as fh:
                fill_writer = csv.DictWriter(
                    fh,
                    fieldnames=[
                        "ts_ms",
                        "ticker",
                        "strategy",
                        "side",
                        "price_units",
                        "qty_units",
                        "fee_micros",
                        "taker",
                    ],
                )
                fill_writer.writeheader()
                fill_writer.writerows(fills)

        await asyncio.to_thread(write)
        return report
    finally:
        await app.broker.stop()
        await app.repo.stop()
        await app.rest.aclose()
        await app.crypto_feed.http.aclose()
        await app.weather_feed.http.aclose()


async def run_replays(
    settings: Settings, paths: list[Path], output: Path, sweeps: list[str], sensitivity: bool = False
) -> dict[str, Any]:
    grid = variants(settings, sweeps, sensitivity)
    if await asyncio.to_thread(output.exists):
        raise ValueError(f"Output directory already exists: {output}; choose a new directory")
    await asyncio.to_thread(output.mkdir, parents=True)

    def fingerprints() -> list[dict[str, Any]]:
        result = []
        for path in paths:
            with path.open("rb") as stream:
                result.append(
                    {"path": str(path), "sha256": hashlib.file_digest(stream, "sha256").hexdigest()}
                )
        return result

    sources = await asyncio.to_thread(fingerprints)
    results = []
    warnings: set[str] = set()
    for i, (candidate, params) in enumerate(grid, 1):
        result = await replay(candidate, paths, output / f"variant-{i:03}", params)
        warnings.update(result["warnings"])
        results.append(
            {
                "variant": f"variant-{i:03}",
                "synthetic": result["synthetic"],
                "parameters": params,
                **result["metrics"],
            }
        )
    if sources != await asyncio.to_thread(fingerprints):
        raise ValueError("Input recordings changed during replay; use completed, immutable recording files")
    comparison = {
        "mode": "replay",
        "synthetic": any(r["synthetic"] for r in results),
        "warnings": sorted(warnings),
        "sources": sources,
        "variants": results,
    }
    await asyncio.to_thread(
        (output / "comparison.json").write_text,
        json.dumps(comparison, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return comparison
