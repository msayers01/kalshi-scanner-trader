"""Realized volatility estimators and the reference-price series (docs/STRATEGIES.md §3)."""

from __future__ import annotations

import itertools
import math
from collections import deque
from dataclasses import dataclass

SECONDS_PER_YEAR = 365.0 * 24 * 3600  # crypto trades 24/7


class EWMAVariance:
    """EWMA of per-second log-return variance with irregular sampling.

    Each sample r over dt seconds contributes r²/dt. Decay per sample: λ = 0.5^(dt / half_life).
    `n_eff` tracks the effective number of samples ((1+λ)/(1−λ) in steady state).
    """

    __slots__ = ("_s1", "_s2", "half_life_s", "var")

    def __init__(self, half_life_s: float) -> None:
        self.half_life_s = half_life_s
        self.var: float | None = None
        self._s1 = 0.0  # Σ w   (Kish effective sample size = s1² / s2)
        self._s2 = 0.0  # Σ w²

    @property
    def n_eff(self) -> float:
        return 0.0 if self._s2 <= 0 else self._s1 * self._s1 / self._s2

    def seed(self, var_per_s: float, n_eff: float) -> None:
        self.var = var_per_s
        self._s1 = self._s2 = max(n_eff, 1.0)

    def update(self, r: float, dt_s: float) -> None:
        if dt_s <= 0:
            return
        x = r * r / dt_s
        lam = 0.5 ** (dt_s / self.half_life_s)
        if self.var is None:
            self.var, self._s1, self._s2 = x, 1.0, 1.0
            return
        self.var = lam * self.var + (1.0 - lam) * x
        self._s1 = lam * self._s1 + 1.0
        self._s2 = lam * lam * self._s2 + 1.0


@dataclass(frozen=True, slots=True)
class VolEstimate:
    var_per_s: float
    sigma_annual: float
    rel_se_sigma: float  # relative standard error of sigma
    n_eff: float


class BlendedVol:
    def __init__(
        self,
        fast_half_life_s: float = 300.0,
        slow_half_life_s: float = 3600.0,
        fast_weight: float = 0.5,
        sample_s: float = 5.0,
        min_annual: float = 0.15,
        max_annual: float = 4.0,
    ) -> None:
        self.fast = EWMAVariance(fast_half_life_s)
        self.slow = EWMAVariance(slow_half_life_s)
        self.fast_weight = fast_weight
        self.sample_s = sample_s
        self.min_var = (min_annual**2) / SECONDS_PER_YEAR
        self.max_var = (max_annual**2) / SECONDS_PER_YEAR
        self._last_ts: int | None = None
        self._last_logp: float | None = None
        self.samples = 0

    def seed_from_closes(self, closes: list[float], interval_s: float) -> None:
        """Seed the slow (and fast) estimators from evenly spaced closes (e.g. 1-min candles)."""
        rets = [math.log(b / a) for a, b in itertools.pairwise(closes) if a > 0 and b > 0]
        if len(rets) < 10:
            return
        var = sum(r * r for r in rets) / len(rets) / interval_s
        self.slow.seed(var, float(len(rets)))
        self.fast.seed(var, min(float(len(rets)), 2 * self.fast.half_life_s / interval_s))

    def on_price(self, value: float, ts_ms: int) -> None:
        if value <= 0:
            return
        lp = math.log(value)
        if self._last_ts is None or self._last_logp is None:
            self._last_ts, self._last_logp = ts_ms, lp
            return
        dt = (ts_ms - self._last_ts) / 1000.0
        if dt < self.sample_s:
            return
        r = lp - self._last_logp
        self.fast.update(r, dt)
        self.slow.update(r, dt)
        self._last_ts, self._last_logp = ts_ms, lp
        self.samples += 1

    def estimate(self) -> VolEstimate | None:
        fv, sv = self.fast.var, self.slow.var
        if fv is None and sv is None:
            return None
        if fv is None:
            var, n = sv, self.slow.n_eff
        elif sv is None:
            var, n = fv, self.fast.n_eff
        else:
            var = self.fast_weight * fv + (1 - self.fast_weight) * sv
            n = self.fast_weight * self.fast.n_eff + (1 - self.fast_weight) * self.slow.n_eff
        assert var is not None
        var = min(self.max_var, max(self.min_var, var))
        n = max(n, 1.0)
        return VolEstimate(
            var_per_s=var,
            sigma_annual=math.sqrt(var * SECONDS_PER_YEAR),
            rel_se_sigma=math.sqrt(1.0 / (2.0 * n)),
            n_eff=n,
        )


class PriceSeries:
    """Recent reference prints (≈1/s) for settlement-window averages."""

    def __init__(self, keep_s: float = 900.0) -> None:
        self.keep_ms = int(keep_s * 1000)
        self.points: deque[tuple[int, float]] = deque()

    def add(self, ts_ms: int, value: float) -> None:
        if self.points and ts_ms <= self.points[-1][0]:
            return
        self.points.append((ts_ms, value))
        cutoff = ts_ms - self.keep_ms
        while self.points and self.points[0][0] < cutoff:
            self.points.popleft()

    @property
    def last(self) -> tuple[int, float] | None:
        return self.points[-1] if self.points else None

    def average(self, start_ms: int, end_ms: int) -> tuple[float, int] | None:
        """Mean of prints with start ≤ ts < end and the count; None if empty."""
        vals = [v for t, v in self.points if start_ms <= t < end_ms]
        return (sum(vals) / len(vals), len(vals)) if vals else None
