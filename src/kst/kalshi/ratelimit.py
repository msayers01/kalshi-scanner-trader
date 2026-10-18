"""Token-bucket limiter mirroring Kalshi's read/write budgets (docs/KALSHI_API_NOTES.md#rate-limits)."""

from __future__ import annotations

import asyncio
import time


class TokenBucket:
    def __init__(self, rate_per_s: float, capacity: float) -> None:
        if rate_per_s <= 0 or capacity <= 0:
            raise ValueError("rate and capacity must be positive")
        self.rate = rate_per_s
        self.capacity = capacity
        self._tokens = capacity
        self._last = time.monotonic()
        self._lock = asyncio.Lock()

    def _refill(self) -> None:
        now = time.monotonic()
        self._tokens = min(self.capacity, self._tokens + (now - self._last) * self.rate)
        self._last = now

    @property
    def available(self) -> float:
        self._refill()
        return self._tokens

    async def acquire(self, cost: float) -> None:
        """Wait until `cost` tokens are available, then spend them (FIFO via the lock)."""
        cost = min(cost, self.capacity)
        async with self._lock:
            while True:
                self._refill()
                if self._tokens >= cost:
                    self._tokens -= cost
                    return
                await asyncio.sleep((cost - self._tokens) / self.rate)

    def drain(self) -> None:
        """After a 429 our local view was optimistic (other clients share the budget): empty the bucket."""
        self._refill()
        self._tokens = 0.0


class KalshiRateLimiter:
    def __init__(
        self,
        read_rate: float = 200,
        write_rate: float = 100,
        read_bucket_s: float = 3.0,
        write_bucket_s: float = 1.0,
        balance_per_s: float = 1.0,
    ) -> None:
        # Leave 10 % headroom: other clients (the Kalshi website/app) share the same budget.
        self.read = TokenBucket(read_rate * 0.9, read_rate * read_bucket_s * 0.9)
        self.write = TokenBucket(write_rate * 0.9, max(write_rate * write_bucket_s * 0.9, 10))
        self.balance = TokenBucket(balance_per_s, 1.0)
