"""Market-probability calibration with event-clustered uncertainty and chronological validation."""

from __future__ import annotations

from dataclasses import asdict
from datetime import UTC, datetime
from typing import Any

import numpy as np
from scipy import special

from kst.calibration.data import MarketSample, market_split, validate_markets
from kst.config import CalibrationToolsCfg
from kst.pricing.calibration import CalibrationFit, CalibrationSet, fit_logistic_calibration


def reliability(p: np.ndarray, y: np.ndarray) -> list[dict[str, Any]]:
    indices = np.minimum((p * 10).astype(int), 9)
    return [
        {
            "bin": i,
            "count": int(np.sum(indices == i)),
            "predicted": float(np.mean(p[indices == i])) if np.any(indices == i) else None,
            "observed": float(np.mean(y[indices == i])) if np.any(indices == i) else None,
        }
        for i in range(10)
    ]


def metrics(p: np.ndarray, y: np.ndarray) -> dict[str, Any]:
    q = np.clip(p, 1e-12, 1 - 1e-12)
    bins = reliability(p, y)
    return {
        "n": len(y),
        "brier": float(np.mean((p - y) ** 2)),
        "log_loss": float(-np.mean(y * np.log(q) + (1 - y) * np.log1p(-q))),
        "ece": sum(b["count"] * abs(b["predicted"] - b["observed"]) for b in bins if b["count"]) / len(y),
        "reliability": bins,
    }


def fit(samples: list[MarketSample], cfg: CalibrationToolsCfg, fitted_ms: int) -> CalibrationFit:
    p = np.array([s.price for s in samples])
    y = np.array([s.outcome for s in samples], dtype=float)
    result = fit_logistic_calibration(p, y, l2=cfg.l2, iters=200)
    if result.beta <= 0:
        raise ValueError("Nonpositive fitted slope; review the sample labels and price convention")
    # Cluster sandwich covariance: strikes in the same event are not independent observations.
    x = np.column_stack([np.ones(len(p)), special.logit(p)])
    errors = y - special.expit(result.alpha + result.beta * x[:, 1])
    clusters, cluster_indices = np.unique([s.event_ticker for s in samples], return_inverse=True)
    scores = np.zeros((len(clusters), 2))
    np.add.at(scores, cluster_indices, x * errors[:, None])
    bread = np.array(result.cov)
    meat = sum((np.outer(v, v) for v in scores), start=np.zeros((2, 2)))
    correction = len(clusters) / (len(clusters) - 1) * (len(samples) - 1) / (len(samples) - 2)
    result.cov = (bread @ meat @ bread * correction).tolist()
    result.horizon_h = cfg.horizon_h
    result.horizon_tolerance_h = cfg.max_quote_age_s / 3600
    result.fitted_at = datetime.fromtimestamp(fitted_ms / 1000, UTC).isoformat()
    return result


def enough(train: list[MarketSample], holdout: list[MarketSample], cfg: CalibrationToolsCfg) -> bool:
    return (
        len(train) >= cfg.min_train_samples
        and len(holdout) >= cfg.min_holdout_samples
        and len({s.event_ticker for s in train}) >= cfg.min_train_events
        and len({s.event_ticker for s in holdout}) >= cfg.min_holdout_events
        and len({s.outcome for s in train}) == 2
        and len({s.outcome for s in holdout}) == 2
    )


def evaluate(samples: list[MarketSample], calib: CalibrationSet) -> dict[str, Any]:
    p = np.array([s.price for s in samples])
    y = np.array([s.outcome for s in samples], dtype=float)
    q = np.array([calib.for_category(s.category).predict(s.price)[0] for s in samples])
    baseline, calibrated = metrics(p, y), metrics(q, y)
    return {
        "events": len({s.event_ticker for s in samples}),
        "baseline": baseline,
        "calibrated": calibrated,
        "brier_improvement": baseline["brier"] - calibrated["brier"],
        "log_loss_improvement": baseline["log_loss"] - calibrated["log_loss"],
    }


def calibrate(
    samples: list[MarketSample], cfg: CalibrationToolsCfg, as_of_ms: int
) -> tuple[CalibrationSet, dict[str, Any]]:
    if not samples:
        raise ValueError("No calibration samples")
    validate_markets(samples, cfg, as_of_ms)
    train, holdout, purged = market_split(samples, cfg.holdout_fraction)
    if not enough(train, holdout, cfg):
        raise ValueError(
            "Insufficient independent training/holdout samples or missing outcome classes after chronological purging"
        )
    candidate = CalibrationSet(default=fit(train, cfg, as_of_ms))
    categories: dict[str, Any] = {}
    for category in sorted({s.category for s in samples}):
        tr = [s for s in train if s.category == category]
        te = [s for s in holdout if s.category == category]
        if not enough(tr, te, cfg):
            categories[category] = {"status": "global_fallback", "train_n": len(tr), "holdout_n": len(te)}
            continue
        category_fit = fit(tr, cfg, as_of_ms)
        candidate.by_category[category] = category_fit
        categories[category] = {
            "status": "fitted",
            "fit": asdict(category_fit),
            "holdout": evaluate(te, candidate),
        }
    report: dict[str, Any] = {
        "kind": "probability",
        "synthetic": all(s.synthetic for s in samples),
        "as_of_ms": as_of_ms,
        "settings": cfg.model_dump(),
        "samples": len(samples),
        "purged_samples": purged,
        "train": evaluate(train, candidate),
        "holdout": evaluate(holdout, candidate),
        "default_fit": asdict(candidate.default),
        "categories": categories,
        "train_latest_label_ms": max(s.settled_ms for s in train),
        "holdout_first_prediction_ms": min(s.observed_ms for s in holdout),
        "warnings": [
            "Candidate only; no active calibration or trading settings were changed.",
            "Coefficients use training events only; validation events and unavailable labels were excluded.",
            "Probability calibration and held-out score improvements do not establish after-fee trading profit.",
        ],
    }
    if report["synthetic"]:
        report["warnings"].append("SYNTHETIC inputs; this fit is a functionality demonstration.")
    report["heldout_improves"] = (
        report["holdout"]["brier_improvement"] > 0 and report["holdout"]["log_loss_improvement"] > 0
    )
    return candidate, report
