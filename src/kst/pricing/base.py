"""Fair-value abstractions and the model ⊕ market Bayesian combination (docs/STRATEGIES.md §1.1)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Protocol

from kst.domain.market import Market

EPS = 1e-4


def clip_prob(x: float) -> float:
    return min(1.0 - EPS, max(EPS, x))


def logit(x: float) -> float:
    x = clip_prob(x)
    return math.log(x / (1.0 - x))


def sigmoid(y: float) -> float:
    if y >= 0:
        return 1.0 / (1.0 + math.exp(-y))
    e = math.exp(y)
    return e / (1.0 + e)


@dataclass(frozen=True, slots=True)
class FairValue:
    p: float  # model P(YES)
    sd: float  # model std of p (probability units)
    model: str
    ts_ms: int
    market_based: bool = False  # True if derived from the market price itself (don't re-combine)
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Posterior:
    q: float  # combined P(YES)
    sd: float  # std of q
    w_model: float  # weight on the model in logit space (0..1)
    fv: FairValue | None = None

    @property
    def ok(self) -> bool:
        return 0.0 < self.q < 1.0


class FairValueModel(Protocol):
    name: str

    def applies(self, market: Market) -> bool: ...

    def fair_value(self, market: Market, now_ms: int) -> FairValue | None: ...


def combine_with_market(
    fv: FairValue,
    bid: float | None,
    ask: float | None,
    *,
    s_floor: float = 0.15,
    s_p_base: float = 0.35,
) -> Posterior:
    """Precision-weighted combination of model and market in logit space.

    s_m = sd/(p(1−p)) ⊕ s_floor        (model; ⊕ = root-sum-square)
    s_p = s_p_base + ½(logit(ask) − logit(bid))   (market; wider spread ⇒ noisier mid)
    """
    p = clip_prob(fv.p)
    s_m = math.hypot(fv.sd / (p * (1.0 - p)), s_floor)
    if fv.market_based or bid is None or ask is None or not (0.0 < bid < ask < 1.0):
        # No two-sided market to learn from (or the model already is a market transform).
        return Posterior(q=p, sd=p * (1.0 - p) * s_m, w_model=1.0, fv=fv)
    mid = 0.5 * (bid + ask)
    s_p = s_p_base + 0.5 * (logit(ask) - logit(bid))
    w_m, w_p = 1.0 / s_m**2, 1.0 / s_p**2
    ell = (logit(p) * w_m + logit(mid) * w_p) / (w_m + w_p)
    s = math.sqrt(1.0 / (w_m + w_p))
    q = sigmoid(ell)
    return Posterior(q=q, sd=q * (1.0 - q) * s, w_model=w_m / (w_m + w_p), fv=fv)


def market_only_posterior(
    bid: float | None, ask: float | None, *, s_p_base: float = 0.35
) -> Posterior | None:
    """No model: the mid is our estimate (used for marking and MM microprice fallbacks)."""
    if bid is None or ask is None or not (0.0 < bid < ask < 1.0):
        return None
    mid = 0.5 * (bid + ask)
    s = s_p_base + 0.5 * (logit(ask) - logit(bid))
    return Posterior(q=mid, sd=mid * (1 - mid) * s, w_model=0.0)
