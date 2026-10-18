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
        data = {"default": asdict(self.default), "by_category": {k: asdict(v) for k, v in self.by_category.items()}}
        path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def fit_logistic_calibration(prices: np.ndarray, outcomes: np.ndarray, *, l2: float = 1e-3, iters: int = 50) -> CalibrationFit:
    """Logistic regression y ~ σ(α + β·logit(p)) by IRLS (Newton). Returns fit with covariance."""
    p = np.clip(np.asarray(prices, dtype=float), 1e-4, 1 - 1e-4)
    y = np.asarray(outcomes, dtype=float)
    x = np.log(p / (1 - p))
    xmat = np.column_stack([np.ones_like(x), x])
    theta = np.array([0.0, 1.0])
    prior = np.array([0.0, 1.0])  # ridge toward the identity map, not toward zero
    hess = np.eye(2)
    for _ in range(iters):
        eta = xmat @ theta
        mu = 1.0 / (1.0 + np.exp(-eta))
        w = mu * (1 - mu)
        grad = xmat.T @ (y - mu) - l2 * (theta - prior)
        hess = (xmat * w[:, None]).T @ xmat + l2 * np.eye(2)
        step = np.linalg.solve(hess, grad)
        theta = theta + step
        if np.max(np.abs(step)) < 1e-8:
            break
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
        q, sd = fit.predict(mid)
        return FairValue(p=q, sd=sd, model=self.name, ts_ms=now_ms, market_based=True, detail={"mid": round(mid, 4)})
