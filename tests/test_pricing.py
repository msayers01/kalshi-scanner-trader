from __future__ import annotations

import math
from datetime import date

import numpy as np
import pytest
from scipy import stats

from kst.core.types import MarketStatus, StrikeType
from kst.domain.market import Market
from kst.pricing.base import FairValue, combine_with_market, logit, sigmoid
from kst.pricing.calibration import CalibratedPriorModel, CalibrationFit, CalibrationSet, fit_logistic_calibration
from kst.pricing.crypto import CryptoDigitalModel, ReferenceStore, settlement_distribution, strike_probability
from kst.pricing.volatility import SECONDS_PER_YEAR, BlendedVol, EWMAVariance
from kst.pricing.weather import (
    EnsembleForecast,
    KernelCDF,
    Observation,
    WeatherEnsembleModel,
    WeatherStore,
    climate_day_window_ms,
    market_date,
    strike_probability_int,
)

# ------------------------------------------------------------------ combination


def test_combine_agrees_when_model_equals_mid() -> None:
    fv = FairValue(p=0.5, sd=0.05, model="m", ts_ms=0)
    post = combine_with_market(fv, 0.48, 0.52)
    assert post.q == pytest.approx(0.5, abs=1e-9)
    assert 0 < post.w_model < 1


def test_combine_shrinks_toward_market_and_spread_matters() -> None:
    fv = FairValue(p=0.70, sd=0.02, model="m", ts_ms=0)
    tight = combine_with_market(fv, 0.49, 0.51)
    wide = combine_with_market(fv, 0.40, 0.60)
    assert 0.5 < tight.q < 0.70 and 0.5 < wide.q < 0.70
    assert wide.q > tight.q  # wide spread → market less informative → closer to model
    assert wide.w_model > tight.w_model


def test_combine_without_market_and_market_based() -> None:
    fv = FairValue(p=0.3, sd=0.0, model="m", ts_ms=0)
    post = combine_with_market(fv, None, 0.4, s_floor=0.2)
    assert post.q == pytest.approx(0.3) and post.sd == pytest.approx(0.3 * 0.7 * 0.2)
    prior = FairValue(p=0.3, sd=0.01, model="cal", ts_ms=0, market_based=True)
    assert combine_with_market(prior, 0.2, 0.4).q == pytest.approx(0.3)


def test_logit_sigmoid_inverse() -> None:
    for x in (0.01, 0.3, 0.5, 0.9):
        assert sigmoid(logit(x)) == pytest.approx(x)


# ------------------------------------------------------------------ crypto digitals


def test_point_settlement_matches_black_scholes_digital() -> None:
    S, K, sig_ann, tau = 100_000.0, 101_000.0, 0.6, 3600.0
    var = sig_ann**2 / SECONDS_PER_YEAR
    sd = settlement_distribution(S, var, tau, window_s=0.0)
    p = strike_probability(sd, StrikeType.GREATER, K, None)
    v = var * tau
    d2 = (math.log(S / K) - 0.5 * v) / math.sqrt(v)
    assert p == pytest.approx(stats.norm.cdf(d2), abs=1e-9)


def test_averaging_window_matches_monte_carlo() -> None:
    rng = np.random.default_rng(7)
    S, K, sig_ann, tau, w = 100.0, 100.3, 0.8, 300.0, 60.0
    var = sig_ann**2 / SECONDS_PER_YEAR
    n_paths, steps = 40_000, int(tau)
    z = rng.standard_normal((n_paths, steps)) * math.sqrt(var)
    logp = np.log(S) + np.cumsum(z - 0.5 * var, axis=1)
    avg = np.exp(logp[:, -int(w) :]).mean(axis=1)
    mc = float((avg > K).mean())
    model = strike_probability(settlement_distribution(S, var, tau, window_s=w), StrikeType.GREATER, K, None)
    assert model == pytest.approx(mc, abs=0.01)
    # averaging reduces variance → OTM digital is cheaper than the point-settlement digital
    point = strike_probability(settlement_distribution(S, var, tau, window_s=0), StrikeType.GREATER, K, None)
    assert model is not None and point is not None and model < point


def test_inside_window_uses_observed_average() -> None:
    S, var = 100.0, (0.5**2) / SECONDS_PER_YEAR
    # 50 s of the 60-s window observed with average 101 → settlement very likely > 100.5
    sd = settlement_distribution(S, var, 10.0, window_s=60.0, observed_avg=101.0)
    assert sd.kind == "lin" and sd.loc == pytest.approx((50 * 101 + 10 * 100) / 60)
    assert strike_probability(sd, StrikeType.GREATER, 100.5, None) > 0.99
    done = settlement_distribution(S, var, 0.0, window_s=60.0, observed_avg=99.0)
    assert strike_probability(done, StrikeType.GREATER, 100.0, None) == 0.0


def test_student_t_fattens_tails_and_between() -> None:
    S, var, tau = 100.0, (0.6**2) / SECONDS_PER_YEAR, 3600.0
    n = settlement_distribution(S, var, tau, dist="normal")
    t = settlement_distribution(S, var, tau, dist="student_t", nu=4)
    far = 103.0
    assert strike_probability(t, StrikeType.GREATER, far, None) > strike_probability(n, StrikeType.GREATER, far, None)
    total = sum(strike_probability(n, StrikeType.BETWEEN, a, a + 1) for a in np.arange(90, 110, 1.0))
    below = strike_probability(n, StrikeType.LESS, None, 90.0)
    above = strike_probability(n, StrikeType.GREATER, 110.0, None)
    assert total + below + above == pytest.approx(1.0, abs=1e-9)


def _crypto_market(strike_type: StrikeType, floor: float | None, cap: float | None, close_ms: int) -> Market:
    return Market(
        ticker="KXBTCD-X", event_ticker="KXBTCD-X", series_ticker="KXBTCD", status=MarketStatus.ACTIVE,
        strike_type=strike_type, floor_strike=floor, cap_strike=cap, close_time_ms=close_ms,
    )


def test_crypto_model_end_to_end() -> None:
    store = ReferenceStore(sample_s=1.0)
    rng = np.random.default_rng(1)
    t0, price = 1_700_000_000_000, 100_000.0
    for i in range(600):  # 10 minutes of 1-s prints
        price *= math.exp(rng.normal(0, 0.6 / math.sqrt(SECONDS_PER_YEAR)))
        store.on_price("BRTI", t0 + i * 1000, price, "kalshi_cf")
    now = t0 + 599_000
    model = CryptoDigitalModel(store, {"KXBTCD": "BRTI"}, min_samples=30)
    atm = _crypto_market(StrikeType.GREATER, price, None, now + 3_600_000)
    fv = model.fair_value(atm, now)
    assert fv is not None and 0.4 < fv.p < 0.6 and fv.sd > 0
    deep = model.fair_value(_crypto_market(StrikeType.GREATER, price * 0.9, None, now + 3_600_000), now)
    assert deep is not None and deep.p > 0.99
    assert model.fair_value(atm, now + 60_000) is None  # stale reference


def test_ewma_n_eff_steady_state() -> None:
    e = EWMAVariance(half_life_s=10.0)
    for _ in range(2000):
        e.update(0.001, 1.0)
    lam = 0.5 ** (1 / 10)
    assert e.n_eff == pytest.approx((1 + lam) / (1 - lam), rel=1e-3)
    assert e.var == pytest.approx(1e-6)


def test_blended_vol_seed() -> None:
    bv = BlendedVol()
    closes = [100 * math.exp(0.001 * ((-1) ** i)) for i in range(200)]
    bv.seed_from_closes(closes, 60.0)
    est = bv.estimate()
    assert est is not None and est.sigma_annual > 0


# ------------------------------------------------------------------ weather


def test_market_date_and_climate_window_dst() -> None:
    assert market_date("KXHIGHNY-26OCT18") == date(2026, 10, 18)
    start, end = climate_day_window_ms(date(2026, 10, 18), "America/New_York")
    # EDT in October; climate day uses EST (UTC−5) → starts 05:00 UTC
    assert (start // 1000) % 86400 == 5 * 3600 and end - start == 86_400_000
    s2, _ = climate_day_window_ms(date(2026, 1, 15), "America/Chicago")
    assert (s2 // 1000) % 86400 == 6 * 3600


def test_integer_strike_semantics_and_conditioning() -> None:
    cdf = KernelCDF(np.array([60.0, 61.0, 62.0, 63.0, 64.0]), bias=0.0, inflation=1.0, h_min=1.0)
    p_gt = strike_probability_int(cdf, StrikeType.GREATER, 61, None)  # ≥ 62
    p_lt = strike_probability_int(cdf, StrikeType.LESS, None, 60)  # ≤ 59
    p_btw = strike_probability_int(cdf, StrikeType.BETWEEN, 60, 61)  # 60 or 61
    assert p_gt + p_lt + p_btw == pytest.approx(1.0, abs=1e-9)
    # observed max 63 so far → T ≥ 63 surely
    assert strike_probability_int(cdf, StrikeType.LESS, None, 63, observed_max=63.2) == 0.0
    assert strike_probability_int(cdf, StrikeType.GREATER, 62, None, observed_max=63.0) == pytest.approx(1.0)


def test_weather_model() -> None:
    d = date(2026, 10, 18)
    start, end = climate_day_window_ms(d, "America/New_York")
    hours = np.arange(start, end, 3_600_000)
    rng = np.random.default_rng(3)
    members = 58.0 + 6.0 * np.sin(np.linspace(0, math.pi, len(hours)))[None, :] + rng.normal(0, 1.0, (31, 1))
    store = WeatherStore(forecasts={"KXHIGHNY": EnsembleForecast(start - 3_600_000, hours, members)})
    model = WeatherEnsembleModel(store, {"KXHIGHNY": ("America/New_York", 0.0)})

    def mk(st: StrikeType, f: float | None, c: float | None) -> Market:
        return Market(ticker="t", event_ticker="KXHIGHNY-26OCT18", series_ticker="KXHIGHNY", strike_type=st, floor_strike=f, cap_strike=c)

    fv = model.fair_value(mk(StrikeType.GREATER, 63, None), start)
    assert fv is not None and 0.2 < fv.p < 0.8
    later = start + 15 * 3_600_000
    assert model.fair_value(mk(StrikeType.GREATER, 63, None), later) is None  # forecast too old
    store.forecasts["KXHIGHNY"] = EnsembleForecast(later - 3_600_000, hours, members)
    store.observations["KXHIGHNY"] = Observation(d, 66.0, later)
    fv2 = model.fair_value(mk(StrikeType.GREATER, 63, None), later)
    assert fv2 is not None and fv2.p > 0.99


# ------------------------------------------------------------------ calibration


def test_calibration_fit_recovers_parameters() -> None:
    rng = np.random.default_rng(11)
    p = rng.uniform(0.02, 0.98, 20_000)
    true_q = 1 / (1 + np.exp(-(-0.1 + 1.25 * np.log(p / (1 - p)))))
    y = rng.uniform(size=p.size) < true_q
    fit = fit_logistic_calibration(p, y.astype(float))
    assert fit.alpha == pytest.approx(-0.1, abs=0.06) and fit.beta == pytest.approx(1.25, abs=0.06)
    q, sd = fit.predict(0.05)
    assert q < 0.05 and sd > 0  # longshot overpriced
    assert CalibrationFit().is_identity


def test_calibrated_prior_requires_fit() -> None:
    m = Market(ticker="t", event_ticker="E-1", series_ticker="E", category="Sports", yes_bid=500, yes_ask=700)  # type: ignore[arg-type]
    assert not CalibratedPriorModel(CalibrationSet()).applies(m)
    cs = CalibrationSet(default=CalibrationFit(alpha=0.0, beta=1.3, n=1000))
    model = CalibratedPriorModel(cs)
    fv = model.fair_value(m, 0)
    assert model.applies(m) and fv is not None and fv.market_based and fv.p < 0.06
