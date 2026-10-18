"""Injectable clocks. Domain/strategy code must use a Clock, never time.time()."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, date, datetime
from typing import Protocol
from zoneinfo import ZoneInfo


class Clock(Protocol):
    def now_ms(self) -> int: ...

    def now(self) -> datetime: ...

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now_ms(self) -> int:
        return time.time_ns() // 1_000_000

    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class ManualClock:
    """Deterministic clock for tests and replay. `advance()` moves time forward."""

    def __init__(self, start_ms: int = 1_700_000_000_000) -> None:
        self._ms = start_ms

    def now_ms(self) -> int:
        return self._ms

    def now(self) -> datetime:
        return datetime.fromtimestamp(self._ms / 1000, tz=UTC)

    def set(self, ms: int) -> None:
        if ms > self._ms:
            self._ms = ms

    def advance(self, seconds: float) -> None:
        self._ms += int(seconds * 1000)

    async def sleep(self, seconds: float) -> None:
        # In replay, sleeping yields control without real waiting.
        self.advance(seconds)
        await asyncio.sleep(0)


def ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=UTC)


def dt_to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return int(dt.timestamp() * 1000)


def parse_iso_ms(s: str | None) -> int | None:
    """Parse Kalshi ISO-8601 timestamps ('2026-10-18T21:00:00Z') to epoch ms."""
    if not s:
        return None
    try:
        return dt_to_ms(datetime.fromisoformat(s.replace("Z", "+00:00")))
    except ValueError:
        return None


def trading_day(ms: int, tz: str) -> date:
    """Trading day (for daily limits / P&L) in the configured timezone."""
    return ms_to_dt(ms).astimezone(ZoneInfo(tz)).date()
