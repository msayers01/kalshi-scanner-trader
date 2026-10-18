"""Offline replay accounting, chronology, determinism, input validation and parameter grids."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from pydantic import SecretStr
from typer.testing import CliRunner

from kst.backtest.metrics import sharpe
from kst.backtest.recordings import expand_paths, read_records
from kst.backtest.replay import replay, run_replays, variants
from kst.cli import cli
from kst.config import Settings
from kst.core.types import Mode
from kst.marketdata.recorder import Recorder
from kst.marketdata.recording_state import catalog_snapshot

from .conftest import Harness


def arb_recording(h: Harness, tmp_path: Path, *, settle: bool = True) -> Path:
    h.add_market("A", "EV-1")
    h.add_market("B", "EV-1")
    h.catalog.events["EV-1"].mutually_exclusive = True
    h.settings.strategies.structural_arb.exhaustive_allowlist = ["EV-1"]
    h.settings.feeds.weather.enabled = False
    r = Recorder(tmp_path / "inputs")
    t = h.clock.now_ms()
    r.catalog(t, catalog_snapshot(h.catalog))
    for m in ("A", "B"):
        r.book(t, m, [(3900, 1000)], [(4000, 1000)])
    if settle:
        r.status(t + 60_000, "A", "determined", "yes", 10_000)
        r.status(t + 60_000, "B", "determined", "no", 0)
    else:
        r.ref(t + 5_000, "BRTI", 100, "test")
    r.close()
    return next((tmp_path / "inputs").glob("*.gz"))


async def test_offline_replay_exact_fees_settlement_and_determinism(
    h: Harness, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = arb_recording(h, tmp_path)
    # Even a live-mode config with invalid credentials must be forced into an isolated replay.
    h.settings.app.mode = Mode.LIVE
    h.settings.kalshi.key_id = SecretStr("never-use-this")
    h.settings.kalshi.private_key_path = tmp_path / "missing-private-key.pem"
    h.settings.llm.provider.api_key = SecretStr("never-output-this")

    async def no_network(*args, **kwargs):
        raise AssertionError("Offline replay attempted an HTTP request")

    monkeypatch.setattr(httpx.AsyncClient, "send", no_network)
    one = await replay(h.settings, [path], tmp_path / "one")
    two = await replay(h.settings, [path], tmp_path / "two")
    assert one == two
    m = one["metrics"]
    assert m["net_pnl_usd"] == pytest.approx(1.664)
    assert m["fees_usd"] == pytest.approx(0.336)
    assert m["gross_realized_usd"] == 2
    assert m["fills"] == 2  # repeated takes cannot reuse the same book liquidity
    assert m["round_trips"] == 2
    assert m["hit_rate"] == 0.5
    assert one["open_positions"] == []
    assert one["scorecard"]["totals"]["net_realized_usd"] == pytest.approx(1.664)
    assert (tmp_path / "one" / "kst_replay.db").is_file()
    assert not (tmp_path / "one" / "kst_live.db").exists()
    for artifact in ("report.json", "equity.csv", "fills.csv"):
        assert (tmp_path / "one" / artifact).is_file()
    text = (tmp_path / "one" / "report.json").read_text()
    assert "never-use-this" not in text and "never-output-this" not in text
    assert h.settings.app.mode is Mode.LIVE  # caller config was not mutated


async def test_warmup_recovers_and_open_positions_stay_marked(h: Harness, tmp_path: Path) -> None:
    path = arb_recording(h, tmp_path, settle=False)
    h.settings.replay.warmup_s = 2
    report = await replay(h.settings, [path], tmp_path / "warm")
    assert report["metrics"]["fills"] == 2
    assert len(report["open_positions"]) == 2
    assert all(r["book_fresh"] for r in report["open_positions"])
    fills = (tmp_path / "warm" / "fills.csv").read_text()
    assert str(h.clock.now_ms() + 2000) in fills
    assert any("not liquidated" in warning for warning in report["warnings"])


async def test_metadata_and_delta_fail_loudly(h: Harness, tmp_path: Path) -> None:
    r = Recorder(tmp_path / "bad")
    r.book(h.clock.now_ms(), "unknown", [(4000, 100)], [(4200, 100)])
    r.close()
    path = next((tmp_path / "bad").glob("*.gz"))
    with pytest.raises(ValueError, match="no preceding market metadata"):
        await replay(h.settings, [path], tmp_path / "missing-meta")
    h.add_market("M")
    r = Recorder(tmp_path / "delta")
    r.catalog(h.clock.now_ms(), catalog_snapshot(h.catalog))
    r.delta(h.clock.now_ms(), "M", "bid", 4000, 100)
    r.close()
    path = next((tmp_path / "delta").glob("*.gz"))
    with pytest.raises(ValueError, match="delta before initial snapshot"):
        await replay(h.settings, [path], tmp_path / "missing-book")


def test_legacy_late_prints_preserve_append_order_and_v2_rejects_time_travel(tmp_path: Path) -> None:
    path = tmp_path / "legacy.jsonl"
    rows = [{"k": "ref", "t": 100, "i": "BRTI", "v": 101}, {"k": "ref", "t": 90, "i": "BRTI", "v": 102}]
    path.write_text("\n".join(json.dumps(r) for r in rows))
    records = list(read_records(path))
    assert [r.ts_ms for r in records] == [100, 100]
    assert records[1].clamped
    rows[1]["schema"] = 2
    path.write_text("\n".join(json.dumps(r) for r in rows))
    with pytest.raises(ValueError, match="receipt timestamps go backwards"):
        list(read_records(path))


def test_recorder_receipt_time_and_new_day_metadata(h: Harness, tmp_path: Path) -> None:
    h.add_market("M")
    r = Recorder(tmp_path)
    t = h.clock.now_ms()
    r.catalog(t, catalog_snapshot(h.catalog))
    r.trade(t - 5000, "M", 4000, 100, "ask", received_ms=t + 1000)
    r.book(t + 86_400_000, "M", [(4000, 100)], [(4200, 100)])
    r.close()
    files = sorted(tmp_path.glob("*.gz"))
    assert len(files) == 2
    first = list(read_records(files[0]))
    assert first[1].ts_ms == t + 1000
    assert first[1].payload["source_t"] == t - 5000
    second = list(read_records(files[1]))
    assert [r.kind for r in second] == ["catalog", "book"]


def test_sweeps_and_sharpe_intervals() -> None:
    settings = Settings()
    grid = variants(settings, ["s_p_base=0.2,0.35", "z=0,1", "kelly=0.1,0.25", "implied_weight=0,1"], True)
    assert len(grid) == 48
    assert settings.pricing.s_p_base == 0.35
    with pytest.raises(ValueError, match="64 variants"):
        variants(settings, ["z=0,1,2,3,4", "kelly=0,.1,.2,.3,.4", "implied_weight=0,.1,.2"])
    for invalid in ("kelly=-1", "implied_weight=2", "s_p_base=0", "z=nan", "unknown=1"):
        with pytest.raises(ValueError, match=r"must be|Invalid|finite"):
            variants(settings, [invalid])
    assert sharpe([(0, 100), (60_000, 100), (120_000, 100)], 60_000)["sharpe_annualized"] is None
    stats = sharpe([(0, 100), (60_000, 101), (120_000, 100), (130_000, 200)], 60_000)
    assert stats["return_samples"] == 2  # partial final interval is excluded
    assert stats["sharpe_annualized"] is not None


async def test_sweep_artifacts_no_overwrite_and_cli_error(h: Harness, tmp_path: Path) -> None:
    path = arb_recording(h, tmp_path, settle=False)
    report = await run_replays(h.settings, [path], tmp_path / "grid", ["z=0,1"], False)
    assert len(report["variants"]) == 2
    assert len(report["sources"][0]["sha256"]) == 64
    assert (tmp_path / "grid" / "comparison.json").is_file()
    with pytest.raises(ValueError, match="already exists"):
        await run_replays(h.settings, [path], tmp_path / "grid", [], False)
    result = CliRunner().invoke(cli, ["replay", str(tmp_path / "missing*.gz")])
    assert result.exit_code == 2 and "No recordings match" in result.output
    with pytest.raises(ValueError, match="No recordings match"):
        expand_paths([str(tmp_path / "missing*.gz")])


async def test_truncated_gzip_rejected(h: Harness, tmp_path: Path) -> None:
    path = arb_recording(h, tmp_path)
    compressed = path.read_bytes()
    path.write_bytes(compressed[:-8])
    with pytest.raises(ValueError, match="complete recording"):
        await replay(h.settings, [path], tmp_path / "truncated")


async def test_trade_at_decision_timestamp_cannot_fill_new_order(h: Harness, tmp_path: Path) -> None:
    m = h.add_market("KXBTCD-TEST-T100", event="KXBTCD-TEST", series="KXBTCD")
    h.settings.strategies.edge_taker.z = 0
    h.settings.strategies.edge_taker.min_edge = 0.2  # require passive execution despite positive scanner edge
    h.settings.pricing.crypto.min_samples = 1
    h.settings.pricing.crypto.implied_weight = 0
    r = Recorder(tmp_path / "maker-input")
    t = h.clock.now_ms()
    r.catalog(t, catalog_snapshot(h.catalog))
    r.seed(t, "BRTI", [100 + (i % 2) * 0.01 for i in range(100)], 60)
    r.ref(t, "BRTI", 100.04, "kalshi_cf")
    # Positive model edge, but the configured taker threshold forces a passive entry at 50c.
    r.book(t, m.ticker, [(5000, 1000)], [(5100, 1000)])
    r.trade(t, m.ticker, 5000, 10000, "ask")
    # No later public prints or through-books: freshly placed quotes cannot fill retroactively.
    r.ref(t + 1000, "BRTI", 100.04, "kalshi_cf")
    r.close()
    path = next((tmp_path / "maker-input").glob("*.gz"))
    report = await replay(h.settings, [path], tmp_path / "chronology")
    assert report["metrics"]["orders"] > 0
    assert report["metrics"]["fills"] == 0
    late = Recorder(tmp_path / "maker-input")
    late.trade(t - 1000, m.ticker, 5000, 1100, "ask", received_ms=t + 2000)
    late.close()
    delayed = await replay(h.settings, [path], tmp_path / "delayed-print")
    assert delayed["metrics"]["fills"] == 0  # late receipt cannot fill an order created after the print
    fresh = Recorder(tmp_path / "maker-input")
    fresh.trade(t + 3000, m.ticker, 5000, 1100, "ask")
    fresh.close()
    base = await replay(h.settings, [path], tmp_path / "queue-one")
    h.settings.paper.maker_queue_haircut = 2
    conservative = await replay(h.settings, [path], tmp_path / "queue-two")
    assert base["metrics"]["fills"] > conservative["metrics"]["fills"]


async def test_risk_breaker_preserved_during_replay(h: Harness, tmp_path: Path) -> None:
    path = arb_recording(h, tmp_path)
    h.settings.risk.limits.daily_loss_limit_usd = 0.1
    report = await replay(h.settings, [path], tmp_path / "breaker")
    assert report["metrics"]["final_state"] == "paused"
    assert report["metrics"]["orders"] == 2
