from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import pytest

from kst.api.app import create_app
from kst.app import Application
from kst.config import Settings
from kst.persistence.tables import EquitySnapshotRow
from kst.portfolio.history import (
    DAY_MS,
    HOUR_MS,
    MIN_MS,
    SEC_MS,
    EquityTape,
    bucket,
    build_series,
    day_start_ms,
    day_starts,
    fit,
    fmt_interval,
    merge_sources,
    normalize_range,
    parse_interval,
    range_start,
)

UTC = "UTC"
NOW = 20_000 * DAY_MS + 15 * HOUR_MS  # 15:00 UTC on some day


# ------------------------------------------------------------------ parsing / time


def test_parse_and_format_intervals() -> None:
    assert parse_interval("tick") is None and parse_interval("auto") == "auto"
    assert parse_interval("1s") == SEC_MS and parse_interval("15m") == 15 * MIN_MS
    assert parse_interval("4h") == 4 * HOUR_MS and parse_interval("1d") == DAY_MS
    assert parse_interval(" 90S ") == 90 * SEC_MS
    for bad in ("0s", "8d", "5x", "1.5m", "", "-1s"):
        with pytest.raises(ValueError, match="interval"):
            parse_interval(bad)
    assert [fmt_interval(x) for x in (None, SEC_MS, 90 * SEC_MS, 2 * HOUR_MS, DAY_MS)] == [
        "tick",
        "1s",
        "90s",
        "2h",
        "1d",
    ]


def test_ranges() -> None:
    assert normalize_range("1d") == "24H" and normalize_range("today") == "TODAY"
    with pytest.raises(ValueError, match="unknown range"):
        normalize_range("5Y")
    assert range_start("TODAY", NOW, None, UTC) == NOW - 15 * HOUR_MS
    assert range_start("24H", NOW, None, UTC) == NOW - DAY_MS
    assert range_start("ALL", NOW, 123, UTC) == 123


def test_day_boundaries_follow_the_trading_timezone_across_dst() -> None:
    ny = "America/New_York"
    t = int(datetime(2026, 11, 1, 12, tzinfo=ZoneInfo(ny)).timestamp() * 1000)  # DST ends 2026-11-01
    assert day_start_ms(t, ny) == int(datetime(2026, 11, 1, tzinfo=ZoneInfo(ny)).timestamp() * 1000)
    starts = day_starts(t - DAY_MS, t + DAY_MS, ny)
    assert len(starts) == 3
    assert starts[2] - starts[1] == 25 * HOUR_MS  # the fall-back day is 25 h long
    assert day_starts(NOW - 2 * DAY_MS, NOW, UTC) == [NOW - 15 * HOUR_MS - d * DAY_MS for d in (2, 1, 0)]


# ------------------------------------------------------------------ resampling


def test_bucket_is_aligned_and_keeps_last_value() -> None:
    pts = [(1_000, 1.0), (59_000, 2.0), (61_000, 3.0), (119_999, 4.0), (120_000, 5.0)]
    assert bucket(pts, MIN_MS, 0) == [(0, 2.0), (60_000, 4.0), (120_000, 5.0)]
    # A −5 h offset shifts hour buckets to local hours.
    off = -5 * HOUR_MS
    assert bucket([(5 * HOUR_MS + 1, 1.0)], HOUR_MS, off) == [(5 * HOUR_MS, 1.0)]
    assert bucket([(5 * HOUR_MS + 1, 1.0)], DAY_MS, off) == [(5 * HOUR_MS, 1.0)]  # local midnight


def test_fit_coarsens_until_the_result_fits() -> None:
    pts = [(i * SEC_MS, float(i)) for i in range(600)]  # 10 minutes of 1 s ticks
    assert fit(pts, SEC_MS, 1000, 0)[0] == SEC_MS
    iv, out = fit(pts, SEC_MS, 100, 0)
    assert iv == 15 * SEC_MS and len(out) == 40


def test_merge_prefers_tape_where_it_exists() -> None:
    db = [(0, 1.0), (60_000, 2.0), (120_000, 3.0)]
    tape = [(90_000, 2.5), (91_000, 2.6)]
    assert merge_sources(db, tape) == [(0, 1.0), (60_000, 2.0), (90_000, 2.5), (91_000, 2.6)]
    assert merge_sources(db, []) == db


def test_tape_records_changes_heartbeats_and_drops_old() -> None:
    tape = EquityTape(retention_ms=10 * SEC_MS, heartbeat_ms=5 * SEC_MS)
    assert tape.append(0, 100.0)
    assert not tape.append(1_000, 100.0)  # unchanged, before the heartbeat
    assert tape.append(1_250, 100.5)  # changed
    assert tape.append(6_250, 100.5)  # heartbeat while flat
    assert not tape.append(5_000, 99.0)  # out of order
    assert tape.append(12_000, 101.0)
    assert [t for t, _ in tape.points] == [6_250, 12_000]  # 0 and 1 250 aged out
    assert tape.since(10_000) == [(6_250, 100.5), (12_000, 101.0)]  # includes the point before


# ------------------------------------------------------------------ series


def test_tick_interval_returns_every_point_and_live_tip() -> None:
    pts = [(NOW - 3_000, 10.0), (NOW - 2_000, 11.0), (NOW - 1_000, 9.0)]
    d = build_series("TODAY", pts, NOW, 12.0, tz=UTC, interval="tick").to_dict()
    assert d["interval_ms"] is None and d["interval_label"] == "tick"
    assert [p[1] for p in d["points"]] == [10.0, 11.0, 9.0, 12.0]


def test_explicit_interval_buckets_and_too_fine_falls_back() -> None:
    pts = [(NOW - 50 * SEC_MS + i * SEC_MS, float(i)) for i in range(50)]
    d = build_series("TODAY", pts, NOW, 99.0, tz=UTC, interval="10s").to_dict()
    assert d["interval_ms"] == 10 * SEC_MS
    assert all(p[0] % (10 * SEC_MS) == 0 for p in d["points"])  # aligned starts
    assert d["points"][-1] == [NOW, 99.0]
    # Sparse data: a fine interval over a long window is honored (the result is small)...
    sparse = build_series("1Y", pts, NOW, 99.0, tz=UTC, interval="1s").to_dict()
    assert sparse["interval_ms"] == SEC_MS and sparse["note"] == ""
    # ...dense data coarsens until it fits.
    dense = [(NOW - 3_000 * SEC_MS + i * SEC_MS, float(i)) for i in range(3_000)]
    wide = build_series("TODAY", dense, NOW, 99.0, tz=UTC, interval="1s", max_points=1500).to_dict()
    assert wide["interval_ms"] == 5 * SEC_MS and "too many points" in wide["note"]
    assert len(wide["points"]) <= 1500


def test_too_many_ticks_fall_back_to_auto() -> None:
    pts = [(NOW - 5_000 + i, 1.0 + i) for i in range(5_000)]
    d = build_series("TODAY", pts, NOW, None, tz=UTC, interval="tick", max_points=100).to_dict()
    assert d["interval_ms"] is not None and len(d["points"]) <= 101 and "too many" in d["note"]


def test_day_opens_and_baseline() -> None:
    d0 = NOW - 15 * HOUR_MS  # today's midnight
    pts = [
        (d0 - 2 * DAY_MS + HOUR_MS, 900.0),  # 2 days ago
        (d0 - DAY_MS - MIN_MS, 950.0),  # just before yesterday began → yesterday's open
        (d0 - DAY_MS + HOUR_MS, 960.0),
        (d0 - MIN_MS, 980.0),  # just before today
        (d0 + HOUR_MS, 990.0),
    ]
    d = build_series("1W", pts, NOW, 1000.0, tz=UTC, today_open=975.0).to_dict()
    opens = {x["start_ms"]: x["open"] for x in d["days"]}
    assert opens[d0 - 2 * DAY_MS] == 900.0  # no point before it: first point inside the day
    assert opens[d0 - DAY_MS] == 950.0
    assert opens[d0] == 975.0  # the portfolio's own day_start_equity wins for today
    assert d["baseline"] == 900.0 and d["change"] == pytest.approx(100.0)
    today = build_series("TODAY", pts, NOW, 1000.0, tz=UTC).to_dict()
    assert today["baseline"] == 980.0 and today["points"][0] == [d0, 980.0]
    assert [x["start_ms"] for x in today["days"]] == [d0]
    pinned = build_series("TODAY", pts, NOW, 1000.0, tz=UTC, today_open=975.0).to_dict()
    assert pinned["baseline"] == 975.0 and pinned["change"] == pytest.approx(25.0)


def test_empty_history() -> None:
    d = build_series("24H", [], NOW, 1000.0, tz=UTC).to_dict()
    assert d["points"] == [[NOW, 1000.0]] and d["change"] == 0.0
    none = build_series("24H", [], NOW, None, tz=UTC).to_dict()
    assert none["points"] == [] and none["change"] is None and none["days"] == []


# ------------------------------------------------------------------ endpoint


async def test_equity_endpoint_merges_db_and_tape(tmp_path: Path) -> None:
    s = Settings()
    s.app.data_dir = tmp_path
    s.llm.mode = "off"
    s.web.serve_ui = False
    s.app.trading_day_tz = UTC
    app = Application(s)
    await app.repo.start()
    now = app.clock.now_ms()
    async with app.repo.db.session() as sess:
        for i, eq in enumerate((1000.0, 990.0, 1010.0)):
            sess.add(
                EquitySnapshotRow(
                    ts_ms=now - (3 - i) * HOUR_MS,
                    cash=eq,
                    equity=eq,
                    unrealized=0.0,
                    exposure=0.0,
                    pnl_today=0.0,
                    n_positions=0,
                )
            )
        await sess.commit()
    for i in range(5):
        app.equity_tape.append(now - 5_000 + i * 1_000, 1010.0 + i)
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)), base_url="http://test")
    async with client as c:
        d = (await c.get("/api/equity", params={"range": "24H", "interval": "tick"})).json()
        vals = [p[1] for p in d["points"]]
        assert vals[:3] == [1000.0, 990.0, 1010.0] and vals[3:8] == [1010.0, 1011.0, 1012.0, 1013.0, 1014.0]
        assert d["current"] == pytest.approx(app.portfolio.summary().equity / 1e6)
        assert d["days"][-1]["open"] == pytest.approx(app.portfolio.summary().day_start_equity / 1e6)
        m = (await c.get("/api/equity", params={"range": "24H", "interval": "1m"})).json()
        assert m["interval_ms"] == MIN_MS
        for bad in ({"range": "bogus"}, {"interval": "3x"}, {"interval": "0s"}):
            assert (await c.get("/api/equity", params=bad)).status_code == 422


async def test_portfolio_publishes_on_change_and_heartbeat(tmp_path: Path) -> None:
    from kst.core.events import PortfolioUpdated

    s = Settings()
    s.app.data_dir = tmp_path
    s.llm.mode = "off"
    app = Application(s)
    seen: list[int] = []
    app.bus.subscribe(PortfolioUpdated, lambda e: seen.append(e.summary.equity))
    task = asyncio.create_task(app.portfolio.run(tick_ms=20, heartbeat_ms=10_000))
    await asyncio.sleep(0.15)
    n_flat = len(seen)
    app.portfolio.cash += 1_000_000  # +$1
    await asyncio.sleep(0.1)
    task.cancel()
    assert n_flat == 1  # flat equity: published once, not every 20 ms tick
    assert len(seen) == 2 and seen[-1] - seen[0] == 1_000_000
    assert len(app.equity_tape.points) == 2  # the tape followed every published change
