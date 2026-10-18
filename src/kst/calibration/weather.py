"""Per-station daily-high fits from pre-day archived forecasts and final report labels."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

import numpy as np

from kst.calibration.data import WeatherSample, validate_weather, weather_split
from kst.config import Settings
from kst.pricing.weather_calibration import WeatherFit, log_mass
from kst.pricing.weather_calibration import fit_weather as fit


def evaluate(samples: list[WeatherSample], bias: float, inflation: float, floor: float) -> dict[str, Any]:
    errors = np.array([np.mean(s.members_max_f) + bias - s.observed_high_f for s in samples])
    return {
        "n": len(samples),
        "mae_f": float(np.mean(np.abs(errors))),
        "rmse_f": float(np.sqrt(np.mean(errors**2))),
        "log_loss": -float(
            np.mean(
                [
                    log_mass(np.array(s.members_max_f), s.observed_high_f, bias, inflation, floor)
                    for s in samples
                ]
            )
        ),
    }


def calibrate_weather(
    samples: list[WeatherSample], settings: Settings, as_of_ms: int
) -> tuple[dict[str, Any], dict[str, Any]]:
    if not samples:
        raise ValueError("No paired weather samples; check station/day coverage and forecast availability")
    cfg, wp = settings.calibration_tools, settings.pricing.weather
    validate_weather(samples, settings, as_of_ms)
    train, holdout, purged = weather_split(samples, cfg.holdout_fraction)
    stations: dict[str, Any] = {}
    # Emit complete station definitions, so the overlay loads even when YAML defaults omit stations.
    station_config = {k: v.model_dump(mode="json") for k, v in settings.feeds.weather.stations.items()}
    fitted = 0
    for series in sorted({s.series for s in samples}):
        tr = [s for s in train if s.series == series]
        te = [s for s in holdout if s.series == series]
        if len(tr) < cfg.weather_min_train_days or len(te) < cfg.weather_min_holdout_days:
            stations[series] = {
                "status": "insufficient_history",
                "train_days": len(tr),
                "holdout_days": len(te),
            }
            continue
        station = settings.feeds.weather.stations[series]
        bias = wp.bias_f if station.bias_f is None else station.bias_f
        inflation = station.spread_inflation or wp.spread_inflation
        candidate: WeatherFit = fit(
            [np.array(s.members_max_f) for s in tr],
            np.array([s.observed_high_f for s in tr]),
            floor=wp.min_bandwidth_f,
            initial_bias=bias,
            initial_inflation=inflation,
            max_bias=cfg.weather_max_bias_f,
            min_inflation=cfg.weather_min_inflation,
            max_inflation=cfg.weather_max_inflation,
        )
        baseline = evaluate(te, bias, inflation, wp.min_bandwidth_f)
        calibrated = evaluate(te, candidate.bias_f, candidate.spread_inflation, wp.min_bandwidth_f)
        stations[series] = {
            "status": "fitted",
            "station_id": station.nws_station,
            "fit": asdict(candidate),
            "train_days": len(tr),
            "holdout_days": len(te),
            "baseline": baseline,
            "calibrated": calibrated,
            "heldout_log_loss_improvement": baseline["log_loss"] - calibrated["log_loss"],
            "heldout_improves": calibrated["log_loss"] < baseline["log_loss"],
        }
        station_config[series].update(bias_f=candidate.bias_f, spread_inflation=candidate.spread_inflation)
        fitted += 1
    if not fitted:
        raise ValueError(
            "No station has enough independent training and holdout days after chronology purging"
        )
    report: dict[str, Any] = {
        "kind": "weather",
        "synthetic": all(s.synthetic for s in samples),
        "as_of_ms": as_of_ms,
        "settings": cfg.model_dump(),
        "samples": len(samples),
        "purged_samples": purged,
        "stations": stations,
        "train_latest_label_ms": max(s.observed_available_ms for s in train),
        "holdout_first_prediction_ms": min(s.issued_ms for s in holdout),
        "warnings": [
            "Candidate only; no active weather or trading settings were changed.",
            "Fits use pre-day forecasts and source-labeled final integer Fahrenheit highs, not intraday maxima.",
            "Validate the observation station/source against each traded market's settlement rules.",
            "Bias and spread are bounded by calibration_tools settings; bound hits require review.",
        ],
    }
    if report["synthetic"]:
        report["warnings"].append("SYNTHETIC inputs; this fit is a functionality demonstration.")
    overlay = {
        "feeds": {"weather": {"stations": station_config}},
        "calibration_metadata": {
            "as_of_ms": as_of_ms,
            "synthetic": report["synthetic"],
            "bandwidth_floor_f": wp.min_bandwidth_f,
        },
    }
    return overlay, report
