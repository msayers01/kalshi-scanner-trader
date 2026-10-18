"""Favorite–longshot calibration prior: logit(q) = α + β·logit(p)  (docs/STRATEGIES.md §7).

Identity (α=0, β=1) ⇒ no edge. Fit with `fit_logistic_calibration` on settled markets, save to
data/calibration.json, review the fit, then the prior can generate (maker-only) trades.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from pathlib import Path

import numpy as np
from scipy import optimize, special

from kst.core.clock import parse_iso_ms
from kst.domain.market import Market
from kst.pricing.base import FairValue, logit, sigmoid


@dataclass(slots=True)
class CalibrationFit:
    alpha: float = 0.0
    beta: float = 1.0
    cov: list[list[float]] = field(default_factory=lambda: [[0.0, 0.0], [0.0, 0.0]])
    n: int = 0
    horizon_h: float = 0.0
    fitted_at: str = ""
    horizon_tolerance_h: float = 0.25

    def __post_init__(self) -> None:
        if (
            not all(
                math.isfinite(v) for v in (self.alpha, self.beta, self.horizon_h, self.horizon_tolerance_h)
            )
            or self.horizon_h < 0
            or self.horizon_tolerance_h <= 0
            or type(self.n) is not int
            or self.n < 0
        ):
            raise ValueError("Invalid calibration coefficients, sample count or horizon")
        covariance = np.asarray(self.cov, dtype=float)
        if (
            covariance.shape != (2, 2)
            or not np.isfinite(covariance).all()
            or not np.allclose(covariance, covariance.T, atol=1e-10)
            or np.linalg.eigvalsh(covariance).min() < -1e-10
        ):
            raise ValueError("Calibration covariance must be a finite positive-semidefinite 2x2 matrix")
        if self.fitted_at and parse_iso_ms(self.fitted_at) is None:
            raise ValueError("Invalid calibration fit timestamp")

    @property
    def is_identity(self) -> bool:
        return self.n == 0 or (abs(self.alpha) < 1e-9 and abs(self.beta - 1.0) < 1e-9)

    def predict(self, p: float) -> tuple[float, float]:
        """(q, sd_q) by the delta method on η = α + β·logit(p)."""
        x = logit(p)
        eta = self.alpha + self.beta * x
        q = sigmoid(eta)
        c = self.cov
        var_eta = c[0][0] + 2 * x * c[0][1] + x * x * c[1][1]
        return q, q * (1 - q) * math.sqrt(max(var_eta, 0.0))


@dataclass(slots=True)
class CalibrationSet:
    default: CalibrationFit = field(default_factory=CalibrationFit)
    by_category: dict[str, CalibrationFit] = field(default_factory=dict)

    def for_category(self, category: str) -> CalibrationFit:
        return self.by_category.get(category, self.default)

    @classmethod
    def load(cls, path: Path) -> CalibrationSet:
        if not path.is_file():
            return cls()
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            default=CalibrationFit(**raw.get("default", {})),
            by_category={k: CalibrationFit(**v) for k, v in (raw.get("by_category") or {}).items()},
        )

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "default": asdict(self.default),
            "by_category": {k: asdict(v) for k, v in self.by_category.items()},
        }
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def fit_logistic_calibration(
    prices: np.ndarray, outcomes: np.ndarray, *, l2: float = 1e-3, iters: int = 50
) -> CalibrationFit:
    """Penalized logistic likelihood with a stable objective and final-Hessian covariance."""
    p = np.asarray(prices, dtype=float)
    y = np.asarray(outcomes, dtype=float)
    if (
        p.ndim != 1
        or y.shape != p.shape
        or len(p) < 3
        or not np.isfinite(p).all()
        or not np.isfinite(y).all()
    ):
        raise ValueError(
            "Calibration requires matching, finite one-dimensional arrays of at least three samples"
        )
    if not ((p > 0) & (p < 1)).all() or not np.isin(y, [0, 1]).all() or len(np.unique(y)) < 2:
        raise ValueError("Prices must be inside (0, 1) and outcomes must contain both 0 and 1")
    if np.ptp(p) < 1e-8 or not math.isfinite(l2) or l2 <= 0 or iters < 1:
        raise ValueError("Calibration needs varying prices, positive finite regularization and iterations")
    x = np.log(p / (1 - p))
    xmat = np.column_stack([np.ones_like(x), x])
    prior = np.array([0.0, 1.0])  # ridge toward the identity map, not toward zero

    def objective(theta: np.ndarray) -> tuple[float, np.ndarray]:
        eta = xmat @ theta
        loss = np.sum(np.logaddexp(0, eta) - y * eta) + l2 / 2 * np.sum((theta - prior) ** 2)
        return float(loss), xmat.T @ (special.expit(eta) - y) + l2 * (theta - prior)

    result = optimize.minimize(
        objective, prior, jac=True, method="L-BFGS-B", options={"maxiter": iters, "ftol": 1e-12, "gtol": 1e-8}
    )
    if not result.success:
        raise ValueError(f"Calibration optimizer did not converge: {result.message}")
    theta = result.x
    mu = special.expit(xmat @ theta)
    hess = (xmat * (mu * (1 - mu))[:, None]).T @ xmat + l2 * np.eye(2)
    cov = np.linalg.inv(hess)
    return CalibrationFit(alpha=float(theta[0]), beta=float(theta[1]), cov=cov.tolist(), n=len(y))


class CalibratedPriorModel:
    """Fair value from the market mid passed through the fitted calibration (market-based)."""

    name = "calibrated_prior"

    def __init__(self, calib: CalibrationSet) -> None:
        self.calib = calib

    def applies(self, market: Market) -> bool:
        return not self.calib.for_category(market.category).is_identity

    def fair_value(self, market: Market, now_ms: int) -> FairValue | None:
        mid = market.mid
        if mid is None:
            return None
        fit = self.calib.for_category(market.category)
        fitted_ms = parse_iso_ms(fit.fitted_at)
        if fitted_ms is not None and fitted_ms > now_ms:
            return None
        if fit.horizon_h > 0:
            seconds = market.seconds_to_close(now_ms)
            if seconds is None or abs(seconds / 3600 - fit.horizon_h) > fit.horizon_tolerance_h:
                return None
        q, sd = fit.predict(mid)
        return FairValue(
            p=q, sd=sd, model=self.name, ts_ms=now_ms, market_based=True, detail={"mid": round(mid, 4)}
        )
