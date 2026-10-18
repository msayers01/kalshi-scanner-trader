"""Fit the production daily-high kernel mixture to integer Fahrenheit report outcomes."""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
from scipy import optimize, special


@dataclass(frozen=True, slots=True)
class WeatherFit:
    bias_f: float
    spread_inflation: float
    n: int
    on_bound: bool


def bandwidth(members: np.ndarray, inflation: float, floor: float) -> float:
    sd = float(np.std(members, ddof=1)) if len(members) > 1 else 0.0
    return max(floor, inflation * sd * len(members) ** (-0.2), inflation * sd / 2)


def log_mass(members: np.ndarray, outcome: float, bias: float, inflation: float, floor: float) -> float:
    h = bandwidth(members, inflation, floor)
    low, high = (outcome - 0.5 - members - bias) / h, (outcome + 0.5 - members - bias) / h
    # Use symmetry in the right tail so subtraction does not lose all precision.
    right = low > 0
    upper = special.log_ndtr(np.where(right, -low, high))
    lower = special.log_ndtr(np.where(right, -high, low))
    mass = upper + np.log(-np.expm1(np.minimum(lower - upper, -1e-15)))
    return float(special.logsumexp(mass) - math.log(len(members)))


def fit_weather(
    members: list[np.ndarray],
    outcomes: np.ndarray,
    *,
    floor: float = 1,
    initial_bias: float = 0.5,
    initial_inflation: float = 1.3,
    max_bias: float = 10,
    min_inflation: float = 0.25,
    max_inflation: float = 5,
) -> WeatherFit:
    if len(members) != len(outcomes) or len(outcomes) < 10 or floor <= 0 or not math.isfinite(floor):
        raise ValueError("Weather fit requires at least ten matched days and a positive bandwidth floor")
    if not np.isfinite(outcomes).all() or any(
        m.ndim != 1 or len(m) < 2 or not np.isfinite(m).all() for m in members
    ):
        raise ValueError("Weather outcomes and ensemble members must be finite one-dimensional arrays")

    def loss(theta: np.ndarray) -> float:
        return -float(
            np.mean(
                [
                    log_mass(m, y, theta[0], math.exp(theta[1]), floor)
                    for m, y in zip(members, outcomes, strict=True)
                ]
            )
        )

    result = optimize.minimize(
        loss,
        [
            np.clip(initial_bias, -max_bias, max_bias),
            math.log(np.clip(initial_inflation, min_inflation, max_inflation)),
        ],
        method="L-BFGS-B",
        bounds=[(-max_bias, max_bias), (math.log(min_inflation), math.log(max_inflation))],
        options={"maxiter": 300, "ftol": 1e-12},
    )
    if not result.success or not math.isfinite(result.fun):
        raise ValueError(f"Weather optimizer did not converge: {result.message}")
    bias, inflation = float(result.x[0]), math.exp(float(result.x[1]))
    return WeatherFit(
        bias,
        inflation,
        len(outcomes),
        abs(bias) >= 0.999 * max_bias
        or inflation <= min_inflation * 1.001
        or inflation >= max_inflation * 0.999,
    )
