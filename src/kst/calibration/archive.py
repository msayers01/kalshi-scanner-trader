"""Pair recorder ensembles with externally supplied final station/day climatological reports."""

from __future__ import annotations

from collections import Counter
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from kst.backtest.recordings import merge_recordings
from kst.calibration.data import WeatherSample, read_samples
from kst.config import Settings
from kst.pricing.weather import climate_day_window_ms


class FinalHigh(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    series: str
    station_id: str
    day: date
    observed_high_f: float = Field(ge=-150, le=150)
    observed_available_ms: int = Field(gt=0)
    source: str = Field(min_length=1)
    synthetic: bool = False


def pair_archive(
    settings: Settings, recordings: list[Path], observations: list[Path]
) -> tuple[list[WeatherSample], dict[str, Any]]:
    # FinalHigh is not a paired sample yet; use its own series/day identity.
    labels, sources, duplicates = read_samples(observations, FinalHigh)
    selected: dict[tuple[str, date], tuple[int, WeatherSample]] = {}
    starts: dict[tuple[str, date], tuple[int, int]] = {}
    for label in labels:
        station = settings.feeds.weather.stations.get(label.series)
        if station is None or station.nws_station.upper() != label.station_id.upper():
            raise ValueError(f"{label.series}: observation station does not match configuration")
        starts[label.series, label.day] = climate_day_window_ms(label.day, station.tz)
    for record in merge_recordings(recordings):
        if record.kind != "weather" or "forecast" not in record.payload:
            continue
        r = record.payload
        f = r["forecast"]
        issued = int(f["issued_ms"])
        if issued > record.ts_ms:
            raise ValueError(f"{record.location}: forecast issue time is later than receipt")
        times = np.array(f["times_ms"], dtype=np.int64)
        members = np.array(f["members_f"], dtype=float)
        if times.ndim != 1 or members.ndim != 2 or members.shape[1] != len(times):
            raise ValueError(f"{record.location}: forecast shape mismatch")
        for label in labels:
            if label.series != r["series"]:
                continue
            start, end = starts[label.series, label.day]
            # Both issue time and receipt time must meet the fixed pre-day lead window.
            oldest = start - settings.calibration_tools.weather_max_lead_h * 3_600_000
            if not oldest <= issued <= record.ts_ms <= start:
                continue
            expected = np.arange(start, end, 3_600_000)
            if not set(expected) <= set(times) or len(np.unique(times)) != len(times):
                continue
            indices = [int(np.flatnonzero(times == t)[0]) for t in expected]
            window = members[:, indices]
            complete = window[np.isfinite(window).all(axis=1)]
            if len(complete) < settings.calibration_tools.weather_min_members:
                continue
            key = label.series, label.day
            if key not in selected or selected[key][0] < record.ts_ms:
                sample = WeatherSample(
                    **label.model_dump(),
                    issued_ms=record.ts_ms,
                    members_max_f=np.max(complete, axis=1).tolist(),
                )
                selected[key] = record.ts_ms, sample
    counts = Counter(label.series for label in labels if (label.series, label.day) not in selected)
    return [v[1] for _, v in sorted(selected.items())], {
        "observation_sources": sources,
        "duplicate_observations": duplicates,
        "unpaired_days_by_series": dict(counts),
        "paired_days": len(selected),
    }
