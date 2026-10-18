"""Replay return statistics. Accounting inputs remain integer micros."""

from __future__ import annotations

import itertools
import math
import statistics


def sharpe(samples: list[tuple[int, int]], interval_ms: int) -> dict[str, float | int | None]:
    returns = [
        (b - a) / a for (ta, a), (tb, b) in itertools.pairwise(samples) if tb - ta == interval_ms and a > 0
    ]
    value = None
    annualized = None
    if len(returns) >= 2:
        sd = statistics.stdev(returns)
        if sd > 0:
            value = statistics.mean(returns) / sd
            annualized = value * math.sqrt(365 * 86_400_000 / interval_ms)
    return {"return_samples": len(returns), "sharpe_per_interval": value, "sharpe_annualized": annualized}
