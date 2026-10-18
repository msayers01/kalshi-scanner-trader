"""Calibration chronology, held-out evaluation, artifact compatibility and acquisition contracts."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import pytest
import respx
from scipy.special import expit, logit
from typer.testing import CliRunner

from kst.calibration.archive import pair_archive
from kst.calibration.artifacts import write_job
from kst.calibration.collect import collect_markets, sample_market
from kst.calibration.data import (
    MarketSample,
    WeatherSample,
    market_split,
    read_samples,
    validate_markets,
    validate_weather,
)
from kst.calibration.probability import calibrate
from kst.calibration.weather import calibrate_weather
from kst.cli import cli
from kst.config import Settings, load_settings
from kst.core.fixedpoint import Price
from kst.domain.market import Market
from kst.kalshi.rest import KalshiRest
from kst.marketdata.recorder import Recorder
from kst.pricing.calibration import CalibratedPriorModel, CalibrationSet, fit_logistic_calibration
from kst.pricing.weather import KernelCDF, climate_day_window_ms
from kst.pricing.weather_calibration import bandwidth, log_mass

BASE = 1_704_067_200_000  # synthetic examples begin 2024-01-01 UTC


def market_samples(n: int = 1200) -> list[MarketSample]:
    rng = np.random.default_rng(771)
    p = rng.uniform(0.02, 0.98, n)
    y = rng.random(n) < expit(0.25 + 1.8 * logit(p))
    return [
        MarketSample(
            ticker=f"T-{i}",
            event_ticker=f"E-{i}",
            category="A" if i % 2 else "B",
            price=float(p[i]),
            outcome=int(y[i]),
            observed_ms=BASE + i * 7_200_000,
            close_ms=BASE + i * 7_200_000 + 3_600_000,
            settled_ms=BASE + i * 7_200_000 + 5_400_000,
            synthetic=True,
        )
        for i in range(n)
    ]


def weather_samples(n: int = 300) -> list[WeatherSample]:
    rng = np.random.default_rng(678)
    rows = []
    for i in range(n):
        day = date(2024, 1, 1) + timedelta(days=i)
        start, end = climate_day_window_ms(day, "America/New_York")
        members = 65 + 8 * np.sin(i / 30) + rng.normal(0, 3, 30)
        high = round(rng.choice(members) + 2.5 + rng.normal(0, bandwidth(members, 2, 1)))
        rows.append(
            WeatherSample(
                series="KXHIGHNY",
                station_id="KNYC",
                day=day,
                issued_ms=start - 6 * 3_600_000,
                members_max_f=members.tolist(),
                observed_high_f=high,
                observed_available_ms=end + 8 * 3_600_000,
                source="synthetic final report",
                synthetic=True,
            )
        )
    return rows


def write_samples(path: Path, rows: list[MarketSample] | list[WeatherSample]) -> None:
    path.write_text("\n".join(s.model_dump_json() for s in rows) + "\n", encoding="utf-8")


def test_probability_candidate_scores_on_later_events_and_runtime_horizon(tmp_path: Path) -> None:
    samples, settings = market_samples(), Settings()
    now = samples[-1].settled_ms + 1
    candidate, report = calibrate(samples, settings.calibration_tools, now)
    assert report["heldout_improves"]
    assert report["train_latest_label_ms"] < report["holdout_first_prediction_ms"]
    assert candidate.default.beta == pytest.approx(1.8, abs=0.4)
    assert set(candidate.by_category) == {"A", "B"}
    assert np.linalg.eigvalsh(candidate.default.cov).min() >= -1e-12
    active = tmp_path / "active.json"
    active.write_text("do not change")
    settings.pricing.calibration.path = active
    output = tmp_path / "candidate"
    result = write_job(settings, samples, [], output, now)
    assert result["synthetic"] and active.read_text() == "do not change"
    assert (output / "reliability.png").stat().st_size > 1000
    assert (output / "reliability.svg").is_file()
    loaded = CalibrationSet.load(output / "calibration.json")
    model = CalibratedPriorModel(loaded)
    market = Market(
        "M", "E", "S", category="A", yes_bid=Price(1900), yes_ask=Price(2100), close_time_ms=now + 3_600_000
    )
    assert model.fair_value(market, now) is not None
    assert model.fair_value(market, now + 1_800_000) is None
    market.close_time_ms = now + 3_600_000 - 1000
    assert model.fair_value(market, now - 1000) is None  # a fit cannot be used before it was produced
    with pytest.raises(ValueError, match="covariance"):
        CalibrationSet.load(_invalid_covariance_file(tmp_path))
    with pytest.raises(ValueError, match="already exists"):
        write_job(settings, samples, [], output, now)


def _invalid_covariance_file(tmp_path: Path) -> Path:
    path = tmp_path / "bad-covariance.json"
    path.write_text(json.dumps({"default": {"n": 100, "cov": [[1, 2], [2, 1]]}}))
    return path


def test_event_split_purges_overlapping_labels_and_sparse_category_falls_back() -> None:
    samples = market_samples()
    samples[0] = samples[0].model_copy(update={"settled_ms": samples[-1].settled_ms, "category": "Sparse"})
    # Add a second contract in the same event; both must remain on the same side of the split.
    samples.append(samples[10].model_copy(update={"ticker": "second-contract"}))
    train, holdout, purged = market_split(samples, 0.2)
    assert purged >= 1
    assert not {s.event_ticker for s in train} & {s.event_ticker for s in holdout}
    candidate, report = calibrate(samples, Settings().calibration_tools, samples[-2].settled_ms + 1)
    assert report["categories"]["Sparse"]["status"] == "global_fallback"
    assert "Sparse" not in candidate.by_category


def test_sample_identity_conflicts_csv_and_future_labels(tmp_path: Path) -> None:
    samples = market_samples(20)
    path = tmp_path / "samples.jsonl"
    write_samples(path, [*samples, samples[0]])
    parsed, sources, duplicates = read_samples([path], MarketSample)
    assert len(parsed) == 20 and duplicates == 1 and len(sources[0]["sha256"]) == 64
    write_samples(path, [samples[0], samples[0].model_copy(update={"price": 0.9})])
    with pytest.raises(ValueError, match="Conflicting duplicate"):
        read_samples([path], MarketSample)
    s = samples[0]
    csv_file = tmp_path / "single.csv"
    row = s.model_dump(mode="json")
    csv_file.write_text(
        ",".join(row)
        + "\n"
        + ",".join(str(v).lower() if isinstance(v, bool) else str(v) for v in row.values())
    )
    assert read_samples([csv_file], MarketSample)[0][0] == s
    with pytest.raises(ValueError, match="outcome not available"):
        validate_markets(samples, Settings().calibration_tools, samples[0].settled_ms)
    with pytest.raises(ValueError, match="horizon window"):
        validate_markets(
            [s.model_copy(update={"observed_ms": s.close_ms - 1000})],
            Settings().calibration_tools,
            samples[-1].settled_ms,
        )


@pytest.mark.parametrize(
    ("p", "y"),
    [
        ([0.1, 0.2, float("nan")], [0, 1, 0]),
        ([0.1, 0.1, 0.1], [0, 1, 0]),
        ([0.1, 0.3, 0.5], [1, 1, 1]),
        ([0, 0.3, 0.5], [0, 1, 0]),
    ],
)
def test_invalid_logistic_inputs_fail(p: list[float], y: list[int]) -> None:
    with pytest.raises(ValueError, match=r"requires|must|needs"):
        fit_logistic_calibration(np.array(p), np.array(y))


def test_weather_likelihood_matches_production_kernel_and_tail_is_finite() -> None:
    members = np.array([70, 73, 74, 75, 77], dtype=float)
    cdf = KernelCDF(members, bias=2, inflation=1.8, h_min=1)
    assert np.exp(log_mass(members, 75, 2, 1.8, 1)) == pytest.approx(cdf(75) - cdf(74))
    assert np.isfinite(log_mass(members, 140, 2, 1.8, 1))


def test_weather_fit_overlay_compatible_and_temporally_valid(tmp_path: Path) -> None:
    samples, settings = weather_samples(), Settings()
    now = samples[-1].observed_available_ms + 1
    overlay, report = calibrate_weather(samples, settings, now)
    fit = report["stations"]["KXHIGHNY"]["fit"]
    assert fit["bias_f"] == pytest.approx(2.5, abs=1)
    assert fit["spread_inflation"] == pytest.approx(2, abs=0.6)
    assert report["train_latest_label_ms"] < report["holdout_first_prediction_ms"]
    assert report["stations"]["KXHIGHNY"]["heldout_improves"]
    output = tmp_path / "weather"
    write_job(settings, samples, [], output, now, weather=True)
    loaded = load_settings(output / "weather.yaml")
    assert loaded.feeds.weather.stations["KXHIGHNY"].bias_f == fit["bias_f"]
    assert loaded.feeds.weather.stations["KXHIGHNY"].spread_inflation == fit["spread_inflation"]
    assert set(loaded.feeds.weather.stations) == set(settings.feeds.weather.stations)
    assert (output / "weather-validation.png").stat().st_size > 1000
    assert overlay["calibration_metadata"]["synthetic"]
    with pytest.raises(ValueError, match="before the climate day"):
        validate_weather(
            [samples[0].model_copy(update={"issued_ms": samples[0].issued_ms + 12 * 3_600_000})],
            settings,
            now,
        )
    with pytest.raises(ValueError, match="station_id"):
        validate_weather([samples[0].model_copy(update={"station_id": "WRONG"})], settings, now)


def test_archived_ensembles_use_complete_pre_day_members(tmp_path: Path) -> None:
    settings = Settings()
    day = date(2026, 7, 4)
    start, end = climate_day_window_ms(day, settings.feeds.weather.stations["KXHIGHNY"].tz)
    times = list(range(start, end, 3_600_000))
    r = Recorder(tmp_path / "recordings")
    members = [[70 + j / 10 + i / 20 for i in range(24)] for j in range(11)]
    members[0][0] = None
    r.weather(
        start - 3_600_000,
        {
            "series": "KXHIGHNY",
            "forecast": {"issued_ms": start - 7_200_000, "times_ms": times, "members_f": members},
        },
    )
    # This later intraday forecast would leak observations; it must not replace the pre-day forecast.
    r.weather(
        start + 3_600_000,
        {
            "series": "KXHIGHNY",
            "forecast": {"issued_ms": start + 3_600_000, "times_ms": times, "members_f": [[99] * 24] * 11},
        },
    )
    r.close()
    observations = tmp_path / "highs.jsonl"
    observations.write_text(
        json.dumps(
            {
                "series": "KXHIGHNY",
                "station_id": "KNYC",
                "day": day.isoformat(),
                "observed_high_f": 76,
                "observed_available_ms": end + 8 * 3_600_000,
                "source": "NWS CLI test fixture",
                "synthetic": True,
            }
        )
    )
    samples, acquisition = pair_archive(
        settings, sorted((tmp_path / "recordings").glob("*.gz")), [observations]
    )
    assert acquisition["paired_days"] == 1
    assert len(samples[0].members_max_f) == 10
    assert max(samples[0].members_max_f) < 80
    assert samples[0].issued_ms == start - 3_600_000


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, UTC).isoformat()


async def test_history_collection_reads_both_partitions_and_never_after_horizon() -> None:
    cfg = Settings().calibration_tools
    cfg.max_markets = 10
    close = BASE + 3_600_000
    market = {
        "ticker": "S-E-M",
        "event_ticker": "S-E",
        "market_type": "binary",
        "result": "yes",
        "status": "settled",
        "close_time": _iso(close),
        "settlement_ts": _iso(close + 3_600_000),
        "settlement_value_dollars": "1.0000",
    }

    def candle(t: int, p: str) -> dict:
        return {
            "end_period_ts": t // 1000,
            "yes_bid": {"close": p},
            "yes_ask": {"close": p},
            "price": {"close": p},
            "volume": "10",
        }

    candles = [candle(BASE, ".2"), candle(BASE + 60_000, ".99")]
    with respx.mock(assert_all_called=True) as mock:
        mock.get("https://test/series/S").respond(200, json={"series": {"category": "Test"}})
        mock.get("https://test/markets").respond(200, json={"markets": [market]})
        mock.get("https://test/historical/markets").respond(200, json={"markets": [market]})
        # Archival moved the market after listing; fall back from the recent candles endpoint.
        mock.get("https://test/series/S/markets/S-E-M/candlesticks").respond(404, json={"error": "moved"})
        route = mock.get("https://test/historical/markets/S-E-M/candlesticks").respond(
            200, json={"candlesticks": candles}
        )
        rest = KalshiRest("https://test")
        try:
            samples, provenance, evidence = await collect_markets(rest, ["S"], cfg, close + 7_200_000)
        finally:
            await rest.aclose()
    assert len(samples) == 1 and samples[0].price == 0.2
    assert samples[0].observed_ms == BASE
    assert int(route.calls.last.request.url.params["end_ts"]) == BASE // 1000
    assert provenance["counts"]["archive_boundary_fallback"] == 1 and len(evidence) == 1
    assert sample_market(market, [candle(BASE + 1000, ".9")], "Test", cfg, close + 7_200_000) is None


def test_calibration_cli_real_commands_and_synthetic_labels(tmp_path: Path) -> None:
    samples = market_samples()
    path = tmp_path / "markets.jsonl"
    write_samples(path, samples)
    result = CliRunner().invoke(
        cli,
        [
            "calibrate",
            "--input",
            str(path),
            "--output",
            str(tmp_path / "cli-candidate"),
            "--as-of-ms",
            str(samples[-1].settled_ms + 1),
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["synthetic"]
    invalid = CliRunner().invoke(cli, ["calibrate-weather"])
    assert invalid.exit_code == 2 and "Choose --input" in invalid.output
