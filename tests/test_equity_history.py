from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from kst.api.app import create_app
from kst.app import Application
from kst.config import Settings
from kst.persistence.tables import EquitySnapshotRow
from kst.portfolio.history import DAY_MS, build_series, downsample, range_start


def test_downsample_keeps_last_point_per_bucket_and_newest() -> None:
    pts = [(t, float(t)) for t in range(0, 1000, 10)]  # 100 points
    out = downsample(pts, 0, 1000, 10)
    assert len(out) == 10
    assert out[-1] == (990, 990.0)  # newest always kept
    assert all(t % 100 == 90 for t, _ in out)  # last in each 100 ms bucket
    assert downsample(pts, 0, 1000, 500) == pts  # already small enough
    assert downsample(pts, 500, 1000, 500)[0] == (500, 500.0)  # window filter


def test_range_start() -> None:
    assert range_start("1D", 10 * DAY_MS, None) == 9 * DAY_MS
    assert range_start("ALL", 10 * DAY_MS, 123) == 123
    assert range_start("ALL", 10 * DAY_MS, None) == 10 * DAY_MS
    with pytest.raises(ValueError, match="unknown range"):
        range_start("5Y", 0, None)


def test_series_baseline_is_last_point_before_window() -> None:
    now = 10 * DAY_MS
    pts = [(now - 2 * DAY_MS, 900.0), (now - DAY_MS // 2, 950.0)]
    d = build_series("1D", pts, now, 1000.0).to_dict()
    assert d["baseline"] == 900.0
    assert d["points"][0] == [now - DAY_MS, 900.0]  # drawn from the window's left edge
    assert d["points"][-1] == [now, 1000.0]  # live equity appended
    assert d["change"] == pytest.approx(100.0)
    assert d["change_pct"] == pytest.approx(100.0 / 900.0)


def test_series_without_history_uses_first_point_or_live() -> None:
    now = 10 * DAY_MS
    d = build_series("1W", [(now - DAY_MS, 500.0)], now, 450.0).to_dict()
    assert d["baseline"] == 500.0 and d["change"] == pytest.approx(-50.0)
    empty = build_series("1D", [], now, 1000.0).to_dict()
    assert empty["points"] == [[now, 1000.0]] and empty["change"] == 0.0
    none = build_series("1D", [], now, None).to_dict()
    assert none["points"] == [] and none["change"] is None and none["change_pct"] is None


async def test_equity_endpoint(tmp_path: Path) -> None:
    s = Settings()
    s.app.data_dir = tmp_path
    s.llm.mode = "off"
    s.web.serve_ui = False
    app = Application(s)
    await app.repo.start()
    now = app.clock.now_ms()
    async with app.repo.db.session() as sess:
        for i, eq in enumerate((1000.0, 990.0, 1010.0)):
            sess.add(
                EquitySnapshotRow(
                    ts_ms=now - (3 - i) * 3_600_000,
                    cash=eq,
                    equity=eq,
                    unrealized=0.0,
                    exposure=0.0,
                    pnl_today=0.0,
                    n_positions=0,
                )
            )
        await sess.commit()
    client = httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)), base_url="http://test")
    async with client as c:
        d = (await c.get("/api/equity", params={"range": "1D"})).json()
        assert [p[1] for p in d["points"]][:3] == [1000.0, 990.0, 1010.0]
        assert d["baseline"] == 1000.0
        assert d["current"] == pytest.approx(app.portfolio.summary().equity / 1e6)
        assert (await c.get("/api/equity", params={"range": "ALL"})).status_code == 200
        assert (await c.get("/api/equity", params={"range": "bogus"})).status_code == 422
