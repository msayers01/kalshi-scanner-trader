"""Crypto above/below & range markets priced as digitals on a 60-s average (docs/STRATEGIES.md §3).

Pure math + an in-memory `ReferenceStore` that feeds (adapters) write into.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

from scipy import stats

from kst.core.types import StrikeType
from kst.domain.market import Market
from kst.pricing.base import FairValue
from kst.pricing.volatility import BlendedVol, PriceSeries

# ------------------------------------------------------------------ distribution helpers


def _or(x: float | None, default: float) -> float:
    return default if x is None else x


def std_cdf(z: float, dist: str = "normal", nu: float = 5.0) -> float:
    """CDF of a unit-variance distribution (normal or scaled Student-t)."""
    if math.isinf(z):
        return 1.0 if z > 0 else 0.0
    if dist == "student_t":
        return float(stats.t.cdf(z * math.sqrt(nu / (nu - 2.0)), df=nu))
    return float(stats.norm.cdf(z))


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


def strike_probability(sd: SettlementDist, strike_type: StrikeType, floor: float | None, cap: float | None) -> float | None:
    """P(YES) for Kalshi strike semantics on a continuous underlying."""
    if strike_type in (StrikeType.GREATER, StrikeType.GREATER_OR_EQUAL) and floor is not None:
        return 1.0 - sd.cdf(floor)
    if strike_type in (StrikeType.LESS, StrikeType.LESS_OR_EQUAL) and cap is not None:
        return sd.cdf(cap)
    if strike_type is StrikeType.BETWEEN and floor is not None and cap is not None:
        return max(0.0, sd.cdf(cap) - sd.cdf(floor))
    return None


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
        {StrikeType.GREATER, StrikeType.GREATER_OR_EQUAL, StrikeType.LESS, StrikeType.LESS_OR_EQUAL, StrikeType.BETWEEN}
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
    ) -> None:
        self.store = store
        self.series_index = series_index
        self.distribution = distribution
        self.nu = nu
        self.window_s = window_s
        self.max_ref_age_ms = int(max_ref_age_s * 1000)
        self.min_samples = min_samples
        self.proxy_basis_sd = proxy_basis_sd

    def index_for(self, market: Market) -> str | None:
        return self.series_index.get(market.series_ticker)

    def applies(self, market: Market) -> bool:
        return (
            market.series_ticker in self.series_index
            and market.strike_type in self._STRIKES
            and market.close_time_ms is not None
        )

    def _prob(self, market: Market, spot: float, var: float, tau_s: float, obs_avg: float | None) -> float | None:
        sd = settlement_distribution(
            spot, var, tau_s, window_s=self.window_s, observed_avg=obs_avg, dist=self.distribution, nu=self.nu
        )
        return strike_probability(sd, market.strike_type, market.floor_strike, market.cap_strike)

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
        p = self._prob(market, spot, est.var_per_s, tau_s, obs_avg)
        if p is None:
            return None
        # Parameter uncertainty: σ ± se(σ), plus basis risk when pricing off a proxy feed.
        k = est.rel_se_sigma
        p_hi = _or(self._prob(market, spot, est.var_per_s * (1 + k) ** 2, tau_s, obs_avg), p)
        p_lo = _or(self._prob(market, spot, est.var_per_s * max(1 - k, 0.05) ** 2, tau_s, obs_avg), p)
        sd = abs(p_hi - p_lo) / 2.0
        if st.source != "kalshi_cf" and self.proxy_basis_sd > 0:
            b = self.proxy_basis_sd
            pu = _or(self._prob(market, spot * (1 + b), est.var_per_s, tau_s, obs_avg), p)
            pd = _or(self._prob(market, spot * (1 - b), est.var_per_s, tau_s, obs_avg), p)
            sd = math.hypot(sd, abs(pu - pd) / 2.0)
        return FairValue(
            p=p,
            sd=sd,
            model=self.name,
            ts_ms=now_ms,
            detail={
                "index": idx,
                "spot": round(spot, 2),
                "sigma_ann": round(est.sigma_annual, 4),
                "tau_s": round(tau_s, 1),
                "src": st.source,
            },
        )
