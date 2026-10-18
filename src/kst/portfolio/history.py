"""Equity history for the portfolio chart: range windows and time-bucketed downsampling.

Snapshots are written every `equity_every_s` (60 s) while the engine runs, so a year is ~500k points;
the chart needs a few hundred. Pure functions: no I/O, no clock.
"""

from __future__ import annotations

from dataclasses import dataclass

DAY_MS = 86_400_000

# Range label → window length (None = everything recorded).
RANGES: dict[str, int | None] = {
    "1D": DAY_MS,
    "1W": 7 * DAY_MS,
    "1M": 30 * DAY_MS,
    "3M": 90 * DAY_MS,
    "1Y": 365 * DAY_MS,
    "ALL": None,
}


@dataclass(frozen=True, slots=True)
class EquitySeries:
    range: str
    start_ms: int
    baseline: float | None  # equity at the start of the range (what "change" is measured from)
    points: list[tuple[int, float]]

    def to_dict(self) -> dict[str, object]:
        last = self.points[-1][1] if self.points else None
        change = None if last is None or self.baseline is None else last - self.baseline
        return {
            "range": self.range,
            "start_ms": self.start_ms,
            "baseline": self.baseline,
            "current": last,
            "change": change,
            "change_pct": None if change is None or not self.baseline else change / self.baseline,
            "points": [[t, round(e, 6)] for t, e in self.points],
        }


def range_start(range_: str, now_ms: int, first_ms: int | None) -> int:
    """Start of the window. ALL starts at the first recorded point (or now, with no history)."""
    if range_ not in RANGES:
        raise ValueError(f"unknown range {range_!r}; expected one of {', '.join(RANGES)}")
    span = RANGES[range_]
    if span is None:
        return first_ms if first_ms is not None else now_ms
    return now_ms - span


def downsample(
    points: list[tuple[int, float]], start_ms: int, end_ms: int, max_points: int
) -> list[tuple[int, float]]:
    """Keep the LAST point in each of `max_points` equal time buckets over [start_ms, end_ms].

    Last-in-bucket (not mean) so every plotted value is a real recorded equity, and the newest point is
    always kept. Input must be sorted by time; points outside the window are dropped.
    """
    pts = [p for p in points if start_ms <= p[0] <= end_ms]
    if max_points <= 0 or len(pts) <= max_points or end_ms <= start_ms:
        return pts
    width = (end_ms - start_ms) / max_points
    out: list[tuple[int, float]] = []
    last_bucket = -1
    for t, e in pts:
        b = min(int((t - start_ms) / width), max_points - 1)
        if b == last_bucket:
            out[-1] = (t, e)
        else:
            out.append((t, e))
            last_bucket = b
    return out


def build_series(
    range_: str,
    points: list[tuple[int, float]],
    now_ms: int,
    current_equity: float | None,
    max_points: int = 400,
) -> EquitySeries:
    """Window + downsample stored snapshots, anchor the baseline, and append the live equity as `now`.

    `points` may include one point before the window (repo.equity_points does): it becomes the baseline,
    so the 1D change is measured from where equity stood 24 h ago, not from the first snapshot today.
    """
    first_ms = points[0][0] if points else None
    start = range_start(range_, now_ms, first_ms)
    before = [p for p in points if p[0] < start]
    inside = downsample(points, start, now_ms, max_points)
    if current_equity is not None and (not inside or inside[-1][0] < now_ms):
        inside.append((now_ms, current_equity))
    baseline: float | None
    if before:
        baseline = before[-1][1]
        inside.insert(0, (start, before[-1][1]))  # draw from the window's left edge
    else:
        baseline = inside[0][1] if inside else None
    return EquitySeries(range_, start, baseline, inside)
