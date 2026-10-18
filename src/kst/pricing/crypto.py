"""Crypto above/below & range markets priced as digitals on a 60-s average (docs/STRATEGIES.md §3).

Pure math + an in-memory `ReferenceStore` that feeds (adapters) write into.
"""

from __future__ import annotations

import math
from collections.abc import Callable
from dataclasses import dataclass, field

import numpy as np
from scipy import optimize, special

from kst.core.types import StrikeType
from kst.domain.market import Market
from kst.pricing.base import FairValue
from kst.pricing.volatility import BlendedVol, PriceSeries

# ------------------------------------------------------------------ distribution helpers

_ABOVE = frozenset({StrikeType.GREATER, StrikeType.GREATER_OR_EQUAL})
_BELOW = frozenset({StrikeType.LESS, StrikeType.LESS_OR_EQUAL})


def _or(x: float | None, default: float) -> float:
    return default if x is None else x


def std_cdf(z: float, dist: str = "normal", nu: float = 5.0) -> float:
    """CDF of a unit-variance distribution (normal or scaled Student-t)."""
    if math.isinf(z):
        return 1.0 if z > 0 else 0.0
    if dist == "student_t":
        return float(special.stdtr(nu, z * math.sqrt(nu / (nu - 2.0))))
    return float(special.ndtr(z))


@dataclass(frozen=True, slots=True)
class SettlementDist:
    """Distribution of the settlement average A: either lognormal (kind='log') or normal (kind='lin')."""

    kind: str
    loc: float  # log: E[ln A]; lin: E[A]
    scale: float  # std of ln A or A
    dist: str = "normal"
    nu: float = 5.0

    def cdf(self, k: float) -> float:
        """P(A ≤ k)."""
        if k <= 0:
            return 0.0
        x = math.log(k) if self.kind == "log" else k
        if self.scale <= 1e-12:
            return 1.0 if x >= self.loc else 0.0
        return std_cdf((x - self.loc) / self.scale, self.dist, self.nu)


def settlement_distribution(
    spot: float,
    var_per_s: float,
    tau_s: float,
    *,
    window_s: float = 60.0,
    observed_avg: float | None = None,
    dist: str = "normal",
    nu: float = 5.0,
) -> SettlementDist:
    """Distribution of A = mean of the index over the final `window_s` seconds before close.

    τ > w : ln A ~ N(ln S − v/2, v),  v = σ²((τ − w) + w/3)          (Turnbull–Wakeman moment match)
    τ ≤ w : A ~ N(μ_A, s_A²),  μ_A = ((w−τ)Ā_obs + τS)/w,  s_A = (τ/w)·S·σ·√(τ/3)
    """
    w = max(window_s, 0.0)
    tau = max(tau_s, 0.0)
    if w == 0.0 or tau > w:
        v = var_per_s * ((tau - w) + w / 3.0) if w > 0 else var_per_s * tau
        return SettlementDist("log", math.log(spot) - 0.5 * v, math.sqrt(max(v, 0.0)), dist, nu)
    obs = observed_avg if observed_avg is not None else spot
    mu = ((w - tau) * obs + tau * spot) / w
    s = (tau / w) * spot * math.sqrt(var_per_s * tau / 3.0)
    return SettlementDist("lin", mu, s, dist, nu)


def strike_probability(
    sd: SettlementDist, strike_type: StrikeType, floor: float | None, cap: float | None
) -> float | None:
    """P(YES) for Kalshi strike semantics on a continuous underlying."""
    if strike_type in (StrikeType.GREATER, StrikeType.GREATER_OR_EQUAL) and floor is not None:
        return 1.0 - sd.cdf(floor)
    if strike_type in (StrikeType.LESS, StrikeType.LESS_OR_EQUAL) and cap is not None:
        return sd.cdf(cap)
    if strike_type is StrikeType.BETWEEN and floor is not None and cap is not None:
        return max(0.0, sd.cdf(cap) - sd.cdf(floor))
    return None


def horizon_nu(tau_s: float, nu_min: float = 5.0, excess_kurtosis_1h: float = 3.0) -> float:
    """Student-t ν for a horizon τ.

    For i.i.d. increments, excess kurtosis of an n-period sum scales as κ₁/n, so κ(τ) = κ_1h·(3600/τ).
    A unit-variance t_ν has excess kurtosis 6/(ν−4)  ⇒  ν(τ) = 4 + 6/κ(τ), clamped to [ν_min, 200].
    Short horizons keep fat tails; multi-hour horizons converge to (near-)normal, as the CLT requires.
    """
    kappa = excess_kurtosis_1h * 3600.0 / max(tau_s, 1.0)
    if kappa <= 0:
        return 200.0
    return min(200.0, max(nu_min, 4.0 + 6.0 / kappa))


def fit_implied_sigma(
    points: list[tuple[StrikeType, float | None, float | None, float, float]],
    spot: float,
    tau_s: float,
    *,
    window_s: float = 60.0,
    dist: str = "normal",
    nu: float = 5.0,
    lo: float = 0.05,
    hi: float = 5.0,
) -> float | None:
    """Annualized σ that best reprices an event's strike ladder (weighted least squares on mids).

    points: (strike_type, floor, cap, mid, weight). Weight ≈ 1/spread² so tight quotes dominate. Fitting one
    σ per event turns the model into a *relative-value* model: edge comes from strikes that are out of line
    with the event's own implied distribution, not from a global disagreement about volatility.
    """
    if len(points) < 3 or tau_s <= 0:
        return None
    # Vectorized: every strike becomes P(A ≤ hi) − P(A ≤ lo) with ±inf bounds for one-sided strikes.
    lo_k = np.array(
        [
            (f if st is StrikeType.BETWEEN else (f if st in _ABOVE else 0.0)) or 0.0
            for st, f, c, _, _ in points
        ]
    )
    hi_k = np.array(
        [
            (c if st is StrikeType.BETWEEN else (c if st in _BELOW else np.inf)) or np.inf
            for st, f, c, _, _ in points
        ]
    )
    above = np.array([st in _ABOVE for st, *_ in points])
    mids = np.array([m for *_, m, _ in points])
    wts = np.array([w for *_, w in points])

    def cdf(sd: SettlementDist, k: np.ndarray) -> np.ndarray:
        with np.errstate(divide="ignore"):
            x = np.log(np.maximum(k, 1e-300)) if sd.kind == "log" else k
        z = (x - sd.loc) / max(sd.scale, 1e-12)
        if sd.dist == "student_t":
            out = special.stdtr(sd.nu, z * math.sqrt(sd.nu / (sd.nu - 2.0)))
        else:
            out = special.ndtr(z)
        return np.where(k <= 0, 0.0, np.where(np.isinf(k), 1.0, out))

    def loss(sig: float) -> float:
        var = sig * sig / 31_536_000.0
        sd = settlement_distribution(spot, var, tau_s, window_s=window_s, dist=dist, nu=nu)
        p_hi, p_lo = cdf(sd, hi_k), cdf(sd, lo_k)
        p = np.where(above, 1.0 - p_lo, p_hi - p_lo)
        return float(np.sum(wts * (p - mids) ** 2))

    res = optimize.minimize_scalar(loss, bounds=(lo, hi), method="bounded", options={"xatol": 1e-3})
    if not res.success:
        return None
    sig = float(res.x)
    return None if sig <= lo * 1.01 or sig >= hi * 0.99 else sig  # pinned at a bound ⇒ fit not credible


# ------------------------------------------------------------------ reference store (fed by adapters)


@dataclass(slots=True)
class ReferenceState:
    index_id: str
    series: PriceSeries = field(default_factory=PriceSeries)
    vol: BlendedVol = field(default_factory=BlendedVol)
    source: str = ""  # "kalshi_cf" (settlement index) or "coinbase" (proxy → basis risk)

    def on_price(self, ts_ms: int, value: float, source: str) -> None:
        self.series.add(ts_ms, value)
        self.vol.on_price(value, ts_ms)
        self.source = source


class ReferenceStore:
    def __init__(self, **vol_kwargs: float) -> None:
        self._states: dict[str, ReferenceState] = {}
        self._vol_kwargs = vol_kwargs  # forwarded to BlendedVol (half-lives, sample_s, clamps)

    def get(self, index_id: str) -> ReferenceState:
        st = self._states.get(index_id)
        if st is None:
            st = ReferenceState(index_id, vol=BlendedVol(**self._vol_kwargs))
            self._states[index_id] = st
        return st

    def on_price(self, index_id: str, ts_ms: int, value: float, source: str) -> None:
        self.get(index_id).on_price(ts_ms, value, source)

    def ids(self) -> list[str]:
        return list(self._states)


# ------------------------------------------------------------------ model


class CryptoDigitalModel:
    name = "crypto_digital"
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
        store: ReferenceStore,
        series_index: dict[str, str],
        *,
        distribution: str = "student_t",
        nu: float = 5.0,
        window_s: float = 60.0,
        max_ref_age_s: float = 10.0,
        min_samples: int = 30,
        proxy_basis_sd: float = 0.0005,
        excess_kurtosis_1h: float = 3.0,
        peers: Callable[[Market], list[Market]] | None = None,
        implied_weight: float = 0.7,
        max_fit_spread: float = 0.06,
    ) -> None:
        self.store = store
        self.series_index = series_index
        self.distribution = distribution
        self.nu = nu
        self.window_s = window_s
        self.max_ref_age_ms = int(max_ref_age_s * 1000)
        self.min_samples = min_samples
        self.proxy_basis_sd = proxy_basis_sd
        self.excess_kurtosis_1h = excess_kurtosis_1h
        self.peers = peers
        self.implied_weight = implied_weight
        self.max_fit_spread = max_fit_spread
        self._iv_cache: dict[str, tuple[int, float | None]] = {}  # event → (second, σ_implied)

    def index_for(self, market: Market) -> str | None:
        return self.series_index.get(market.series_ticker)

    def applies(self, market: Market) -> bool:
        return (
            market.series_ticker in self.series_index
            and market.strike_type in self._STRIKES
            and market.close_time_ms is not None
        )

    def _prob(
        self, market: Market, spot: float, var: float, tau_s: float, obs_avg: float | None
    ) -> float | None:
        sd = settlement_distribution(
            spot,
            var,
            tau_s,
            window_s=self.window_s,
            observed_avg=obs_avg,
            dist=self.distribution,
            nu=horizon_nu(tau_s, self.nu, self.excess_kurtosis_1h),
        )
        return strike_probability(sd, market.strike_type, market.floor_strike, market.cap_strike)

    def implied_sigma(self, market: Market, spot: float, tau_s: float, now_ms: int) -> float | None:
        """σ implied by the market's own event ladder (cached per event per second)."""
        if self.peers is None or self.implied_weight <= 0 or tau_s <= self.window_s:
            return None
        key = market.event_ticker
        hit = self._iv_cache.get(key)
        if hit is not None and hit[0] == now_ms // 1000:
            return hit[1]
        pts: list[tuple[StrikeType, float | None, float | None, float, float]] = []
        for m in self.peers(market):
            if m.strike_type not in self._STRIKES or m.yes_bid is None or m.yes_ask is None:
                continue
            bid, ask = m.yes_bid / 10_000, m.yes_ask / 10_000
            spread = ask - bid
            mid = 0.5 * (bid + ask)
            if spread <= 0 or spread > self.max_fit_spread or not 0.03 <= mid <= 0.97:
                continue
            pts.append((m.strike_type, m.floor_strike, m.cap_strike, mid, 1.0 / max(spread, 0.01) ** 2))
        nu = horizon_nu(tau_s, self.nu, self.excess_kurtosis_1h)
        sig = fit_implied_sigma(pts, spot, tau_s, window_s=self.window_s, dist=self.distribution, nu=nu)
        self._iv_cache[key] = (now_ms // 1000, sig)
        return sig

    def fair_value(self, market: Market, now_ms: int) -> FairValue | None:
        idx = self.index_for(market)
        if idx is None or market.close_time_ms is None:
            return None
        st = self.store.get(idx)
        last = st.series.last
        est = st.vol.estimate()
        if last is None or est is None or now_ms - last[0] > self.max_ref_age_ms:
            return None
        if st.vol.samples < self.min_samples and est.n_eff < self.min_samples:
            return None
        spot = last[1]
        close = market.close_time_ms
        tau_s = (close - now_ms) / 1000.0
        obs_avg = None
        if tau_s <= self.window_s:
            got = st.series.average(close - int(self.window_s * 1000), now_ms + 1)
            obs_avg = got[0] if got else None
        # Variance: blend the event-implied σ (relative value) with realized σ (anchor).
        var = est.var_per_s
        iv = self.implied_sigma(market, spot, tau_s, now_ms)
        if iv is not None:
            w = self.implied_weight
            var = w * (iv * iv / 31_536_000.0) + (1 - w) * est.var_per_s
        p = self._prob(market, spot, var, tau_s, obs_avg)
        if p is None:
            return None
        # Parameter uncertainty: σ ± se(σ) (plus implied/realized disagreement), and basis risk on proxies.
        k = est.rel_se_sigma
        if iv is not None:
            k = math.hypot(k, (1 - self.implied_weight) * abs(math.log(iv / max(est.sigma_annual, 1e-6))))
        p_hi = _or(self._prob(market, spot, var * (1 + k) ** 2, tau_s, obs_avg), p)
        p_lo = _or(self._prob(market, spot, var * max(1 - k, 0.05) ** 2, tau_s, obs_avg), p)
        sd = abs(p_hi - p_lo) / 2.0
        if st.source != "kalshi_cf" and self.proxy_basis_sd > 0:
            b = self.proxy_basis_sd
            pu = _or(self._prob(market, spot * (1 + b), var, tau_s, obs_avg), p)
            pd = _or(self._prob(market, spot * (1 - b), var, tau_s, obs_avg), p)
            sd = math.hypot(sd, abs(pu - pd) / 2.0)
        return FairValue(
            p=p,
            sd=sd,
            model=self.name,
            ts_ms=now_ms,
            detail={
                "index": idx,
                "spot": round(spot, 2),
                "sigma_rv": round(est.sigma_annual, 4),
                "sigma_iv": None if iv is None else round(iv, 4),
                "tau_s": round(tau_s, 1),
                "src": st.source,
            },
        )
