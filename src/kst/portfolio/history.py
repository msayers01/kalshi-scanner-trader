"""Equity history for the portfolio chart: ranges, intervals, trading-day opens, and the live tick tape.

Two sources, merged per request:
  * the DB, one snapshot per `equity_every_s` (60 s) — survives restarts, covers any range;
  * `EquityTape`, in memory, every equity change (≤ engine.equity_publish_ms apart) for the last
    `web.equity_tape_hours` — what makes "1 tick" and second-level intervals real.

Buckets are aligned to absolute time (shifted by the trading-day timezone's UTC offset, so 1 h / 1 d buckets
start on local hours and local midnight), which lets the UI keep extending the newest bucket from live pushes
and land on exactly the buckets a refetch would return. Pure: no I/O, no clock.
"""

from __future__ import annotations

import re
from bisect import bisect_left
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

SEC_MS = 1_000
MIN_MS = 60 * SEC_MS
HOUR_MS = 60 * MIN_MS
DAY_MS = 24 * HOUR_MS

# Range label → window length. TODAY = since the current trading day began; None = everything recorded.
RANGES: dict[str, int | None] = {
    "TODAY": 0,
    "24H": DAY_MS,
    "1W": 7 * DAY_MS,
    "1M": 30 * DAY_MS,
    "3M": 90 * DAY_MS,
    "1Y": 365 * DAY_MS,
    "ALL": None,
}
RANGE_ALIASES = {"1D": "24H"}  # older UIs

# "auto" picks the finest of these that fits max_points over the window.
AUTO_LADDER_MS = (
    SEC_MS,
    5 * SEC_MS,
    15 * SEC_MS,
    30 * SEC_MS,
    MIN_MS,
    5 * MIN_MS,
    15 * MIN_MS,
    30 * MIN_MS,
    HOUR_MS,
    4 * HOUR_MS,
    DAY_MS,
)
_UNIT_MS = {"s": SEC_MS, "m": MIN_MS, "h": HOUR_MS, "d": DAY_MS}
_INTERVAL_RE = re.compile(r"^(\d{1,6})([smhd])$")
MAX_INTERVAL_MS = 7 * DAY_MS

Point = tuple[int, float]


# ------------------------------------------------------------------ live tape


@dataclass(slots=True)
class EquityTape:
    """Every equity change for the last `retention_ms`, plus a heartbeat point while equity is flat (so a
    quiet period still reads as "flat", not as a gap). Appends are O(1); old points fall off the left."""

    retention_ms: int
    heartbeat_ms: int = 5 * SEC_MS
    points: deque[Point] = field(default_factory=deque)

    def append(self, ts_ms: int, equity: float) -> bool:
        pts = self.points
        if pts:
            last_t, last_e = pts[-1]
            if ts_ms < last_t:
                return False
            if abs(equity - last_e) < 1e-9 and ts_ms - last_t < self.heartbeat_ms:
                return False
        pts.append((ts_ms, equity))
        cutoff = ts_ms - self.retention_ms
        while pts and pts[0][0] < cutoff:
            pts.popleft()
        return True

    def since(self, ts_ms: int) -> list[Point]:
        """Points at/after ts_ms, plus the last one before it (the window's baseline), like repo.equity_points."""
        pts = list(self.points)
        i = bisect_left(pts, (ts_ms, float("-inf")))
        return pts[max(i - 1, 0) :]


# ------------------------------------------------------------------ parsing


def normalize_range(range_: str) -> str:
    r = RANGE_ALIASES.get(range_.upper(), range_.upper())
    if r not in RANGES:
        raise ValueError(f"unknown range {range_!r}; expected one of {', '.join(RANGES)}")
    return r


def parse_interval(spec: str) -> int | str | None:
    """'tick' → None (every point), 'auto' → 'auto', '<n><s|m|h|d>' → milliseconds."""
    s = spec.strip().lower()
    if s in ("tick", "1tick", "1t"):
        return None
    if s == "auto":
        return "auto"
    m = _INTERVAL_RE.match(s)
    if not m:
        raise ValueError(f"bad interval {spec!r}: use tick, auto, or <n>s / <n>m / <n>h / <n>d")
    ms = int(m.group(1)) * _UNIT_MS[m.group(2)]
    if not SEC_MS <= ms <= MAX_INTERVAL_MS:
        raise ValueError("interval must be between 1s and 7d")
    return ms


def fmt_interval(ms: int | None) -> str:
    if ms is None:
        return "tick"
    for unit, size in (("d", DAY_MS), ("h", HOUR_MS), ("m", MIN_MS), ("s", SEC_MS)):
        if ms % size == 0:
            return f"{ms // size}{unit}"
    return f"{ms}ms"


# ------------------------------------------------------------------ time helpers


def tz_offset_ms(ts_ms: int, tz: str) -> int:
    off = datetime.fromtimestamp(ts_ms / 1000, ZoneInfo(tz)).utcoffset()
    return int(off.total_seconds() * 1000) if off else 0


def day_start_ms(ts_ms: int, tz: str) -> int:
    """Local midnight (in `tz`) of the trading day containing ts_ms — matches core.clock.trading_day."""
    z = ZoneInfo(tz)
    d = datetime.fromtimestamp(ts_ms / 1000, z).date()
    return int(datetime(d.year, d.month, d.day, tzinfo=z).timestamp() * 1000)


def day_starts(start_ms: int, end_ms: int, tz: str) -> list[int]:
    """Midnights of every trading day overlapping [start_ms, end_ms] (first one may be before start_ms)."""
    z = ZoneInfo(tz)
    d = datetime.fromtimestamp(start_ms / 1000, z).date()
    last = datetime.fromtimestamp(end_ms / 1000, z).date()
    out: list[int] = []
    while d <= last:
        out.append(int(datetime(d.year, d.month, d.day, tzinfo=z).timestamp() * 1000))
        d += timedelta(days=1)
    return out


def range_start(range_: str, now_ms: int, first_ms: int | None, tz: str) -> int:
    r = normalize_range(range_)
    span = RANGES[r]
    if r == "TODAY":
        return day_start_ms(now_ms, tz)
    if span is None:
        return first_ms if first_ms is not None else now_ms
    return now_ms - span


# ------------------------------------------------------------------ resampling


def bucket(points: list[Point], interval_ms: int, align_offset_ms: int) -> list[Point]:
    """Last value per aligned bucket, stamped at the bucket's start. Input sorted by time."""
    out: list[Point] = []
    last_key: int | None = None
    for t, e in points:
        key = (t + align_offset_ms) // interval_ms
        start = key * interval_ms - align_offset_ms
        if key == last_key:
            out[-1] = (start, e)
        else:
            out.append((start, e))
            last_key = key
    return out


def fit(points: list[Point], finest_ms: int, max_points: int, offset_ms: int) -> tuple[int, list[Point]]:
    """Bucket at `finest_ms`, coarsening along the ladder until the RESULT fits max_points. Judged on what
    is actually drawn, so a fine interval over a mostly-empty window (e.g. 1 s over Today, engine started a
    minute ago) is honored."""
    candidates = [finest_ms, *(iv for iv in AUTO_LADDER_MS if iv > finest_ms)]
    out: list[Point] = []
    for iv in candidates:
        out = bucket(points, iv, offset_ms)
        if len(out) <= max_points:
            return iv, out
    return candidates[-1], out


def merge_sources(db: list[Point], tape: list[Point]) -> list[Point]:
    """DB snapshots up to where the tape begins, then the tape (finer, and authoritative where it overlaps)."""
    if not tape:
        return list(db)
    first = tape[0][0]
    return [p for p in db if p[0] < first] + list(tape)


def _value_before(points: list[Point], ts_ms: int) -> float | None:
    i = bisect_left(points, (ts_ms, float("-inf")))
    return points[i - 1][1] if i > 0 else None


def _value_at_or_after(points: list[Point], ts_ms: int, end_ms: int) -> float | None:
    i = bisect_left(points, (ts_ms, float("-inf")))
    return points[i][1] if i < len(points) and points[i][0] < end_ms else None


@dataclass(frozen=True, slots=True)
class DayOpen:
    start_ms: int
    open: float


@dataclass(frozen=True, slots=True)
class EquitySeries:
    range: str
    interval: str  # what was asked for
    interval_ms: int | None  # what was used (None = tick)
    align_offset_ms: int
    start_ms: int
    baseline: float | None  # equity at the start of the range ("change" is measured from here)
    points: list[Point]
    days: list[DayOpen]
    note: str = ""

    def to_dict(self) -> dict[str, object]:
        last = self.points[-1][1] if self.points else None
        change = None if last is None or self.baseline is None else last - self.baseline
        return {
            "range": self.range,
            "interval": self.interval,
            "interval_ms": self.interval_ms,
            "interval_label": fmt_interval(self.interval_ms),
            "align_offset_ms": self.align_offset_ms,
            "start_ms": self.start_ms,
            "baseline": self.baseline,
            "current": last,
            "change": change,
            "change_pct": None if change is None or not self.baseline else change / self.baseline,
            "points": [[t, round(e, 6)] for t, e in self.points],
            "days": [{"start_ms": d.start_ms, "open": round(d.open, 6)} for d in self.days],
            "note": self.note,
        }


def build_series(
    range_: str,
    points: list[Point],
    now_ms: int,
    current_equity: float | None,
    *,
    tz: str = "America/New_York",
    interval: str = "auto",
    max_points: int = 1500,
    today_open: float | None = None,
) -> EquitySeries:
    """Window + resample equity points, anchor the baseline and day opens, append the live equity at `now`.

    `points` = merged DB + tape history, sorted, and may start before the window (repo.equity_points
    includes the last snapshot before it): that point becomes the range baseline and the day open of a day
    that began before the first in-window point. `today_open` is the portfolio's own day_start_equity, so
    the dashed line for today is exactly what "Today" P&L is measured from.
    """
    r = normalize_range(range_)
    req = parse_interval(interval)
    first_ms = points[0][0] if points else None
    start = range_start(r, now_ms, first_ms, tz)
    pts = [p for p in points if p[0] <= now_ms]
    if current_equity is not None and (not pts or pts[-1][0] < now_ms):
        pts.append((now_ms, current_equity))
    inside = [p for p in pts if p[0] >= start]
    offset = tz_offset_ms(now_ms, tz)
    note = ""
    iv: int | None = None
    drawn = inside
    if len(inside) > max_points or isinstance(req, int):
        finest = req if isinstance(req, int) else AUTO_LADDER_MS[0]
        iv, drawn = fit(inside, finest, max_points, offset)
        if req is None:
            note = f"{len(inside)} ticks is too many to draw; showing {fmt_interval(iv)}"
        elif isinstance(req, int) and iv != req:
            note = f"{fmt_interval(req)} over this range is too many points; showing {fmt_interval(iv)}"
    if drawn and drawn[0][0] < start:
        drawn[0] = (start, drawn[0][1])  # the first bucket may begin before the window: clamp to its edge

    before = _value_before(pts, start)
    baseline = before if before is not None else (drawn[0][1] if drawn else None)
    if r == "TODAY" and today_open is not None:
        baseline = today_open  # Today's change == the header's "Today" P&L
    if before is not None and (not drawn or drawn[0][0] > start):
        drawn.insert(0, (start, before))  # draw from the window's left edge

    days: list[DayOpen] = []
    starts = day_starts(start, now_ms, tz)
    for i, ds in enumerate(starts):
        nxt = starts[i + 1] if i + 1 < len(starts) else now_ms + 1
        if i == len(starts) - 1 and today_open is not None:
            days.append(DayOpen(ds, today_open))
            continue
        v = _value_before(pts, ds)
        if v is None:
            v = _value_at_or_after(pts, ds, nxt)
        if v is not None:
            days.append(DayOpen(ds, v))
    return EquitySeries(r, interval, iv, offset, start, baseline, drawn, days, note)
