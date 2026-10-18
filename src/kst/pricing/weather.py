"""Daily-high temperature markets via ensemble dressing + intraday conditioning (docs/STRATEGIES.md §4)."""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import numpy as np
from scipy import special

from kst.core.clock import ms_to_dt
from kst.core.types import StrikeType
from kst.domain.market import Market
from kst.pricing.base import FairValue

_DATE_RE = re.compile(r"-(\d{2})([A-Z]{3})(\d{2})")
_MONTHS = {
    m: i
    for i, m in enumerate(
        ["JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"], 1
    )
}


def market_date(event_ticker: str) -> date | None:
    """KXHIGHNY-26OCT18 → 2026-10-18."""
    m = _DATE_RE.search(event_ticker)
    if not m or m.group(2) not in _MONTHS:
        return None
    return date(2000 + int(m.group(1)), _MONTHS[m.group(2)], int(m.group(3)))


def climate_day_window_ms(d: date, tz: str) -> tuple[int, int]:
    """NWS climate day = local *standard* time midnight→midnight (01:00→01:00 local during DST)."""
    z = ZoneInfo(tz)
    noon = datetime(d.year, d.month, d.day, 12, tzinfo=z)
    std_offset = (noon.utcoffset() or timedelta(0)) - (noon.dst() or timedelta(0))
    start_utc = datetime(d.year, d.month, d.day, tzinfo=UTC) - std_offset
    start = int(start_utc.timestamp() * 1000)
    return start, start + 24 * 3600 * 1000


@dataclass(slots=True)
class EnsembleForecast:
    """Hourly 2 m temperature (°F) per ensemble member."""

    issued_ms: int
    times_ms: np.ndarray  # shape (H,)
    members_f: np.ndarray  # shape (J, H)

    def remaining_max(self, start_ms: int, end_ms: int) -> np.ndarray | None:
        mask = (self.times_ms >= start_ms) & (self.times_ms < end_ms)
        if not mask.any():
            return None
        sub = self.members_f[:, mask]
        ok = ~np.all(np.isnan(sub), axis=1)
        if not ok.any():
            return None
        return np.nanmax(sub[ok], axis=1)


@dataclass(slots=True)
class Observation:
    day: date
    max_f: float  # observed max so far in the climate day
    ts_ms: int


class KernelCDF:
    """k ↦ P(round(X) ≤ k) under a Gaussian-kernel dressed ensemble (Roulston & Smith 2003).

    Bandwidth h = max(h_min, α·sd·J^(−1/5), α·sd/2): Silverman-style with spread inflation α, floored so
    large ensembles don't collapse onto their (under-dispersed) members.
    """

    __slots__ = ("bandwidth", "x")

    def __init__(self, member_max: np.ndarray, *, bias: float, inflation: float, h_min: float) -> None:
        self.x = np.asarray(member_max, dtype=float) + bias
        j = len(self.x)
        spread = float(np.std(self.x, ddof=1)) if j > 1 else 0.0
        self.bandwidth = max(h_min, inflation * spread * j ** (-0.2), inflation * spread * 0.5)

    def __call__(self, k: float) -> float:
        return float(np.mean(special.ndtr((k + 0.5 - self.x) / self.bandwidth)))


def strike_probability_int(
    cdf: Callable[[float], float],
    strike_type: StrikeType,
    floor: float | None,
    cap: float | None,
    observed_max: float | None = None,
) -> float | None:
    """P(YES) for integer-°F markets with T = max(M, X).

    greater X ⇒ T ≥ X+1 ; less X ⇒ T ≤ X−1 ; between [lo, hi] ⇒ lo ≤ T ≤ hi (inclusive).
    """
    m = None if observed_max is None else round(observed_max)

    def cdf_t(k: float) -> float:  # P(T ≤ k) for T = max(M, X)
        if m is not None and k < m:
            return 0.0
        return cdf(k)

    if strike_type in (StrikeType.GREATER, StrikeType.GREATER_OR_EQUAL) and floor is not None:
        thr = math.floor(floor) if strike_type is StrikeType.GREATER else math.ceil(floor) - 1
        return 1.0 - cdf_t(thr)
    if strike_type in (StrikeType.LESS, StrikeType.LESS_OR_EQUAL) and cap is not None:
        thr = math.ceil(cap) - 1 if strike_type is StrikeType.LESS else math.floor(cap)
        return cdf_t(thr)
    if strike_type is StrikeType.BETWEEN and floor is not None and cap is not None:
        return max(0.0, cdf_t(math.floor(cap)) - cdf_t(math.ceil(floor) - 1))
    return None


@dataclass(slots=True)
class WeatherStore:
    forecasts: dict[str, EnsembleForecast] = field(default_factory=dict)  # series → forecast
    observations: dict[str, Observation] = field(default_factory=dict)  # series → today's obs


class WeatherEnsembleModel:
    name = "weather_ensemble"
    _STRIKES = frozenset(
        {
            StrikeType.GREATER,
            StrikeType.GREATER_OR_EQUAL,
            StrikeType.LESS,
            StrikeType.LESS_OR_EQUAL,
            StrikeType.BETWEEN,
        }
    )

    def __init__(
        self,
        store: WeatherStore,
        stations: dict[str, tuple[str, float | None]],  # series → (tz, bias override)
        *,
        bias_f: float = 0.5,
        spread_inflation: float = 1.3,
        min_bandwidth_f: float = 1.0,
        model_floor: float = 0.02,
        max_forecast_age_s: float = 4 * 3600.0,
        obs_required_after_s: float = 2 * 3600.0,
        station_inflation: dict[str, float] | None = None,
    ) -> None:
        self.store = store
        self.stations = stations
        self.bias_f = bias_f
        self.inflation = spread_inflation
        self.station_inflation = station_inflation or {}
        self.h_min = min_bandwidth_f
        self.model_floor = model_floor
        self.max_age_ms = int(max_forecast_age_s * 1000)
        self.obs_required_after_ms = int(obs_required_after_s * 1000)

    def applies(self, market: Market) -> bool:
        return market.series_ticker in self.stations and market.strike_type in self._STRIKES

    def fair_value(self, market: Market, now_ms: int) -> FairValue | None:
        st = self.stations.get(market.series_ticker)
        fc = self.store.forecasts.get(market.series_ticker)
        d = market_date(market.event_ticker)
        if st is None or fc is None or d is None or now_ms - fc.issued_ms > self.max_age_ms:
            return None
        tz, bias_override = st
        start, end = climate_day_window_ms(d, tz)
        if now_ms >= end:
            return None  # day over; settlement pending — nothing to model
        obs = self.store.observations.get(market.series_ticker)
        m_obs = obs.max_f if obs is not None and obs.day == d and now_ms >= start else None
        if m_obs is None and now_ms >= start + self.obs_required_after_ms:
            return None  # day underway but we don't know the max so far: forecast-only would be wrong
        rem = fc.remaining_max(max(start, now_ms), end)
        bias = self.bias_f if bias_override is None else bias_override
        if rem is None:
            if m_obs is None:
                return None
            # No forecast hours left: the observed max (almost) is the answer.
            rem = np.array([m_obs - bias])
        cdf = KernelCDF(
            rem,
            bias=bias,
            inflation=self.station_inflation.get(market.series_ticker, self.inflation),
            h_min=self.h_min,
        )
        p = strike_probability_int(cdf, market.strike_type, market.floor_strike, market.cap_strike, m_obs)
        if p is None:
            return None
        j = len(rem)
        sd = math.hypot(math.sqrt(max(p * (1 - p), 1e-6) / max(j, 1)), self.model_floor)
        return FairValue(
            p=min(max(p, 0.0), 1.0),
            sd=sd,
            model=self.name,
            ts_ms=now_ms,
            detail={
                "mu": round(float(np.mean(rem)) + bias, 2),
                "sd": round(float(np.std(rem)), 2),
                "h": round(cdf.bandwidth, 2),
                "obs_max": m_obs,
                "members": j,
                "day": d.isoformat(),
                "issued": ms_to_dt(fc.issued_ms).strftime("%H:%MZ"),
            },
        )
