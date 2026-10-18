"""Validated calibration samples and chronological splits with label-availability purging."""

from __future__ import annotations

import csv
import hashlib
import json
import math
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from kst.config import CalibrationToolsCfg, Settings
from kst.pricing.weather import climate_day_window_ms


class MarketSample(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)
    ticker: str = Field(min_length=1)
    event_ticker: str = Field(min_length=1)
    category: str = Field(min_length=1)
    price: float = Field(gt=0, lt=1)
    outcome: Literal[0, 1]
    observed_ms: int = Field(gt=0)
    close_ms: int = Field(gt=0)
    settled_ms: int = Field(gt=0)
    synthetic: bool = False

    @model_validator(mode="after")
    def chronology(self) -> MarketSample:
        if not self.observed_ms < self.close_ms <= self.settled_ms:
            raise ValueError("Require observed_ms < close_ms <= settled_ms")
        return self


class WeatherSample(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)
    series: str = Field(min_length=1)
    station_id: str = Field(min_length=1)
    day: date
    issued_ms: int = Field(gt=0)  # availability time, including archive receipt delay
    members_max_f: list[float] = Field(min_length=2)
    observed_high_f: float = Field(ge=-150, le=150)
    observed_available_ms: int = Field(gt=0)
    source: str = Field(min_length=1)  # final report/source identifier, not an intraday high
    synthetic: bool = False


def read_samples[S: BaseModel](
    paths: list[Path], model: type[S]
) -> tuple[list[S], list[dict[str, Any]], int]:
    samples: list[S] = []
    sources = []
    duplicates = 0
    seen: dict[tuple[str, ...], S] = {}
    for path in paths:
        raw = path.read_bytes()
        sources.append({"path": str(path.resolve()), "sha256": hashlib.sha256(raw).hexdigest()})
        text = raw.decode("utf-8-sig")
        rows = (
            list(csv.DictReader(text.splitlines()))
            if path.suffix.lower() == ".csv"
            else [json.loads(line) for line in text.splitlines() if line.strip()]
        )
        for i, row in enumerate(rows, 1):
            try:
                if path.suffix.lower() == ".csv" and "members_max_f" in row:
                    row["members_max_f"] = json.loads(row["members_max_f"])
                for field in (
                    "observed_ms",
                    "close_ms",
                    "settled_ms",
                    "issued_ms",
                    "observed_available_ms",
                    "outcome",
                ):
                    if field in row:
                        if path.suffix.lower() == ".csv":
                            row[field] = int(row[field])
                        if type(row[field]) is not int:
                            raise ValueError(f"{field} must be an integer")
                if type(row.get("price")) is bool or type(row.get("outcome")) is bool:
                    raise ValueError("Boolean prices/outcomes are not calibration observations")
                sample = model.model_validate(row)
                identity = sample.model_dump(mode="json")
                key = (
                    (sample.ticker,)
                    if isinstance(sample, MarketSample)
                    else (identity["series"], identity["day"])
                )
                if key in seen:
                    if seen[key] != sample:
                        raise ValueError(f"Conflicting duplicate sample {key}")
                    duplicates += 1
                    continue
                seen[key] = sample
                samples.append(sample)
            except (ValueError, TypeError, AttributeError) as exc:
                raise ValueError(f"{path}: sample {i}: {exc}") from exc
    if not samples:
        raise ValueError("No calibration samples")
    if any(getattr(s, "synthetic", False) for s in samples) and not all(
        getattr(s, "synthetic", False) for s in samples
    ):
        raise ValueError("Do not mix synthetic and historical samples in one calibration")
    return samples, sources, duplicates


def validate_markets(samples: list[MarketSample], cfg: CalibrationToolsCfg, as_of_ms: int) -> None:
    event_categories: dict[str, str] = {}
    for s in samples:
        target = s.close_ms - cfg.horizon_h * 3_600_000
        if not target - cfg.max_quote_age_s * 1000 <= s.observed_ms <= target:
            raise ValueError(f"{s.ticker}: price is outside the configured pre-close horizon window")
        if s.settled_ms > as_of_ms:
            raise ValueError(f"{s.ticker}: outcome not available as of the calibration timestamp")
        if event_categories.setdefault(s.event_ticker, s.category) != s.category:
            raise ValueError(f"{s.event_ticker}: conflicting categories in one event")


def validate_weather(samples: list[WeatherSample], settings: Settings, as_of_ms: int) -> None:
    for s in samples:
        station = settings.feeds.weather.stations.get(s.series)
        if station is None or s.station_id.upper() != station.nws_station.upper():
            raise ValueError(f"{s.series}: station_id must match the configured station")
        start, end = climate_day_window_ms(s.day, station.tz)
        if not start - settings.calibration_tools.weather_max_lead_h * 3_600_000 <= s.issued_ms <= start:
            raise ValueError(
                f"{s.series}/{s.day}: forecast must be available before the climate day, inside the lead window"
            )
        if not end <= s.observed_available_ms <= as_of_ms:
            raise ValueError(
                f"{s.series}/{s.day}: final high must be available after the climate day and before calibration"
            )
        if len(s.members_max_f) < settings.calibration_tools.weather_min_members or not all(
            math.isfinite(v) and -150 <= v <= 150 for v in s.members_max_f
        ):
            raise ValueError(f"{s.series}/{s.day}: insufficient or invalid complete ensemble members")
        if s.observed_high_f != round(s.observed_high_f):
            raise ValueError(
                f"{s.series}/{s.day}: final daily high must use integer Fahrenheit report precision"
            )


def market_split(
    samples: list[MarketSample], fraction: float
) -> tuple[list[MarketSample], list[MarketSample], int]:
    events: dict[str, list[MarketSample]] = defaultdict(list)
    for s in samples:
        events[s.event_ticker].append(s)
    ordered = sorted(events, key=lambda k: (min(s.observed_ms for s in events[k]), k))
    n_holdout = max(1, math.ceil(len(ordered) * fraction))
    holdout = [s for key in ordered[-n_holdout:] for s in events[key]]
    cutoff = min(s.observed_ms for s in holdout)
    train = [
        s
        for key in ordered[:-n_holdout]
        if max(v.settled_ms for v in events[key]) < cutoff
        for s in events[key]
    ]
    return train, holdout, len(samples) - len(train) - len(holdout)


def weather_split(
    samples: list[WeatherSample], fraction: float
) -> tuple[list[WeatherSample], list[WeatherSample], int]:
    days = sorted({s.day for s in samples})
    test_days = set(days[-max(1, math.ceil(len(days) * fraction)) :])
    holdout = [s for s in samples if s.day in test_days]
    cutoff = min(s.issued_ms for s in holdout)
    # Purge whole days across stations, so final reports were known before validation forecasts.
    availability = {d: max(s.observed_available_ms for s in samples if s.day == d) for d in days}
    train = [s for s in samples if s.day not in test_days and availability[s.day] < cutoff]
    return train, holdout, len(samples) - len(train) - len(holdout)
