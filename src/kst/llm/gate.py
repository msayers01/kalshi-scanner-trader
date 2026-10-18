"""LLMGate: decides whether the bot may ENTER a market, using a (local-first) LLM — frugally.

The trader only asks when a strategy actually wants to enter a market that has no fresh verdict, so
token use scales with *entry opportunities*, not with the size of the universe. Verdicts are cached per
(ticker, state bucket) with a TTL; calls are batched and capped by daily budgets (docs/LLM_GATE.md).
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from kst.config import LLMCfg
from kst.core.bus import EventBus
from kst.core.clock import Clock, trading_day
from kst.core.events import Alert, LLMVerdictsUpdated
from kst.core.logging import get_logger
from kst.core.types import BookSide
from kst.llm.packet import SYSTEM_PROMPT, Candidate, build_packet, packet_hash, parse_reply
from kst.llm.providers import LLMProvider, LLMResult, LLMUnavailableError

log = get_logger(__name__)

Bucket = tuple[int, int, int, int]


@dataclass(frozen=True, slots=True)
class Verdict:
    ticker: str
    enter: bool
    confidence: int
    side: BookSide | None
    reason: str
    ts_ms: int
    bucket: Bucket
    provider: str
    model: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "enter": self.enter,
            "confidence": self.confidence,
            "side": None if self.side is None else self.side.outcome,
            "reason": self.reason,
            "ts_ms": self.ts_ms,
            "provider": self.provider,
            "model": self.model,
        }


def state_bucket(c: Candidate) -> Bucket:
    """Coarse market state; a verdict stays valid while the bucket is unchanged (and the TTL holds)."""
    mid = None if c.bid is None or c.ask is None else (c.bid + c.ask) / 2 / 10_000
    mid_b = -1 if mid is None else int(mid * 20)  # 5¢ buckets
    spread = None if c.bid is None or c.ask is None else (c.ask - c.bid) / 10_000
    spr_b = 3 if spread is None else (0 if spread <= 0.02 else 1 if spread <= 0.05 else 2)
    s = c.close_s or 0
    ttc_b = 0 if s < 3600 else 1 if s < 6 * 3600 else 2 if s < 86400 else 3 if s < 7 * 86400 else 4
    side_b = 0 if c.want_side is None else (1 if c.want_side is BookSide.BID else 2)
    return (mid_b, spr_b, ttc_b, side_b)


class LLMGate:
    def __init__(
        self,
        cfg: LLMCfg,
        provider: LLMProvider | None,
        escalation: LLMProvider | None,
        bus: EventBus,
        clock: Clock,
        *,
        trading_day_tz: str,
        persist: Callable[[str, Any], None] | None = None,
        record: Callable[..., None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.provider = provider
        self.escalation = escalation
        self.bus = bus
        self.clock = clock
        self.tz = trading_day_tz
        self.persist = persist
        self.record = record
        self.verdicts: dict[str, Verdict] = {}
        self.pending: dict[str, tuple[Candidate, Bucket, int]] = {}
        self.day = trading_day(clock.now_ms(), trading_day_tz).isoformat()
        self.calls = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.failures = 0
        self.last_call_ms = 0
        self.last_error = ""
        self.last_latency_ms = 0.0
        self._wake = asyncio.Event()

    # ------------------------------------------------------------------ budget

    def restore(self, saved: dict[str, Any] | None) -> None:
        if saved and saved.get("day") == self.day:
            self.calls = int(saved.get("calls", 0))
            self.tokens_in = int(saved.get("tokens_in", 0))
            self.tokens_out = int(saved.get("tokens_out", 0))

    def _roll(self) -> None:
        d = trading_day(self.clock.now_ms(), self.tz).isoformat()
        if d != self.day:
            self.day, self.calls, self.tokens_in, self.tokens_out = d, 0, 0, 0

    def budget_left(self) -> bool:
        self._roll()
        b = self.cfg.budget
        return (
            self.calls < b.max_calls_per_day
            and self.tokens_in < b.max_input_tokens_per_day
            and self.tokens_out < b.max_output_tokens_per_day
        )

    # ------------------------------------------------------------------ policy

    @property
    def enabled(self) -> bool:
        return self.cfg.mode != "off" and self.provider is not None

    def requires_verdict(self, modeled: bool) -> bool:
        return self.cfg.mode == "gate" and (self.cfg.scope == "all" or not modeled)

    def current(self, c: Candidate) -> Verdict | None:
        v = self.verdicts.get(c.ticker)
        if v is None:
            return None
        if self.clock.now_ms() - v.ts_ms > self.cfg.cache_ttl_s * 1000 or v.bucket != state_bucket(c):
            return None
        return v

    def decide(self, c: Candidate, modeled: bool) -> tuple[bool, Verdict | None]:
        """(allowed, verdict). Enqueues a request when a gated entry has no fresh verdict."""
        if not self.enabled:
            return (
                self.cfg.mode != "gate" or self.cfg.fallback == "allow" or not self.requires_verdict(modeled)
            ), None
        v = self.current(c)
        if v is None and (self.cfg.mode == "advisory" or self.requires_verdict(modeled)):
            self.request(c)
        if not self.requires_verdict(modeled):
            return True, v
        if v is not None:
            return v.enter, v
        if not self.budget_left() or self.failures >= 3:
            return self.cfg.fallback == "allow", None
        return False, None  # wait for the verdict

    def side_bias(self, ticker: str) -> BookSide | None:
        v = self.verdicts.get(ticker)
        return None if v is None else v.side

    def request(self, c: Candidate) -> None:
        if c.ticker not in self.pending:
            self.pending[c.ticker] = (c, state_bucket(c), self.clock.now_ms())
            if len(self.pending) >= self.cfg.min_batch:
                self._wake.set()
        else:
            _, _, since = self.pending[c.ticker]
            self.pending[c.ticker] = (c, state_bucket(c), since)  # refresh data, keep queue age

    # ------------------------------------------------------------------ calling

    def _due(self) -> bool:
        if not self.pending or not self.enabled or not self.budget_left():
            return False
        now = self.clock.now_ms()
        if now - self.last_call_ms < self.cfg.min_interval_s * 1000:
            return False
        oldest = min(t for _, _, t in self.pending.values())
        return len(self.pending) >= self.cfg.min_batch or now - oldest >= self.cfg.max_wait_s * 1000

    async def run(self) -> None:
        while True:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), timeout=5.0)
            self._wake.clear()
            if self._due():
                try:
                    await self.call_once()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.exception("llm_gate_call_failed")

    async def _complete(self, system: str, user: str, max_tokens: int) -> LLMResult:
        assert self.provider is not None
        try:
            return await self.provider.complete(system, user, max_tokens)
        except LLMUnavailableError:
            if self.escalation is not None and self.cfg.escalate_on_unavailable:
                log.warning("llm_escalating", to=self.escalation.name)
                return await self.escalation.complete(system, user, max_tokens)
            raise

    async def call_once(self) -> dict[str, Verdict]:
        batch = sorted(self.pending.values(), key=lambda x: x[2])[: self.cfg.batch_size]
        cands = [c for c, _, _ in batch]
        now = self.clock.now_ms()
        user, ids = build_packet(cands, now)
        max_tokens = 20 + 28 * len(cands)
        self.last_call_ms = now
        t0 = time.monotonic()
        try:
            res = await self._complete(SYSTEM_PROMPT, user, max_tokens)
        except LLMUnavailableError as exc:
            self.failures += 1
            self.last_error = str(exc)
            log.warning("llm_unavailable", error=str(exc), failures=self.failures)
            if self.failures == 3:
                self.bus.publish(
                    Alert(
                        now,
                        "warning",
                        "LLM gate unavailable",
                        f"{exc} — fallback policy: {self.cfg.fallback}",
                    )
                )
            return {}
        self.failures = 0
        self.last_error = ""
        self.calls += 1
        self.tokens_in += res.tokens_in
        self.tokens_out += res.tokens_out
        self.last_latency_ms = (time.monotonic() - t0) * 1000
        parsed = parse_reply(res.text, ids) if not res.refused else {}
        h = packet_hash(user)
        out: dict[str, Verdict] = {}
        for c, bucket, _ in batch:
            pv = parsed.get(c.ticker)
            v = Verdict(
                ticker=c.ticker,
                enter=bool(pv and pv.enter),
                confidence=pv.confidence if pv else 0,
                side=pv.side if pv else None,
                reason=pv.reason if pv else ("refused" if res.refused else "unparseable reply → skip"),
                ts_ms=now,
                bucket=bucket,
                provider=res.provider,
                model=res.model,
            )
            out[c.ticker] = v
            self.verdicts[c.ticker] = v
            self.pending.pop(c.ticker, None)
            if self.record is not None:
                self.record(
                    provider=res.provider,
                    model=res.model,
                    ticker=c.ticker,
                    verdict="E" if v.enter else "S",
                    confidence=v.confidence,
                    side="-" if v.side is None else ("Y" if v.side is BookSide.BID else "N"),
                    reason=v.reason,
                    tokens_in=res.tokens_in // max(len(batch), 1),
                    tokens_out=res.tokens_out // max(len(batch), 1),
                    packet_hash=h,
                )
        if self.persist is not None:
            self.persist(
                "llm_budget",
                {
                    "day": self.day,
                    "calls": self.calls,
                    "tokens_in": self.tokens_in,
                    "tokens_out": self.tokens_out,
                },
            )
        log.info(
            "llm_verdicts",
            n=len(out),
            entered=sum(v.enter for v in out.values()),
            tokens_in=res.tokens_in,
            tokens_out=res.tokens_out,
            cached=res.cached_tokens,
            latency_ms=round(res.latency_ms),
            model=res.model,
        )
        self.bus.publish(LLMVerdictsUpdated(now, dict(out)))
        return out

    # ------------------------------------------------------------------ manual export / status

    @staticmethod
    def export_packet(cands: list[Candidate], now_ms: int) -> str:
        user, _ = build_packet(cands, now_ms)
        return f"{SYSTEM_PROMPT}\n\n{user}"

    def status(self) -> dict[str, Any]:
        self._roll()
        b = self.cfg.budget
        return {
            "mode": self.cfg.mode,
            "scope": self.cfg.scope,
            "provider": None if self.provider is None else self.provider.name,
            "model": None if self.provider is None else self.provider.model,
            "calls_today": self.calls,
            "max_calls": b.max_calls_per_day,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "pending": len(self.pending),
            "verdicts": len(self.verdicts),
            "last_error": self.last_error,
            "last_latency_ms": round(self.last_latency_ms),
            "budget_left": self.budget_left(),
        }
