"""Scanner column filters + sort, evaluated over EVERY scanned market (not just the top-N the UI is sent).

The web UI sends a query per client over the WS (`{"op": "scan_query", "query": {...}}`). Units are the
ones the table displays, so the UI never converts:

  text    market (title / ticker / event, case-insensitive substring), category (substring)
  ranges  bid, ask, fair, edge (cents) · vol24h (contracts) · closes (hours) — [min, max], either may be null
  sets    signals (row has ANY of them), llm ("E" | "S" | "none")
  sort    any column above (+ "signals"/"llm"); missing values always sort last. No sort = scanner rank.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from kst.scanner.service import ScanRow

TEXT_COLS = ("market", "category")
RANGE_COLS = ("bid", "ask", "fair", "edge", "vol24h", "closes")
SORT_COLS = (*TEXT_COLS, *RANGE_COLS, "signals", "llm")
LLM_VALUES = frozenset({"E", "S", "none"})
MAX_LIMIT = 500

LlmOf = Callable[[str], str | None]


def _num(r: ScanRow, col: str) -> float | None:
    if col == "bid":
        return None if r.bid is None else r.bid / 100  # Price units ($0.0001) → cents
    if col == "ask":
        return None if r.ask is None else r.ask / 100
    if col == "fair":
        return None if r.q is None else r.q * 100
    if col == "edge":
        e = r.best_edge
        return None if e is None else e * 100
    if col == "vol24h":
        return r.volume_24h
    if col == "closes":
        return None if r.secs_to_close is None else r.secs_to_close / 3600
    raise KeyError(col)


@dataclass(frozen=True, slots=True)
class ScanQuery:
    text: tuple[tuple[str, str], ...] = ()
    ranges: tuple[tuple[str, float | None, float | None], ...] = ()
    signals: frozenset[str] = frozenset()
    llm: frozenset[str] = frozenset()
    sort: str | None = None
    desc: bool = True
    limit: int = 100

    @property
    def filters(self) -> bool:
        return bool(self.text or self.ranges or self.signals or self.llm)

    @property
    def active(self) -> bool:
        return self.filters or self.sort is not None

    @classmethod
    def parse(cls, d: dict[str, Any] | None) -> ScanQuery:
        """Validate a client-supplied query. Raises ValueError on anything unexpected."""
        if not d:
            return cls()
        if not isinstance(d, dict):
            raise ValueError("query must be an object")
        unknown = set(d) - {"text", "ranges", "signals", "llm", "sort", "desc", "limit"}
        if unknown:
            raise ValueError(f"unknown query keys: {sorted(unknown)}")
        text: list[tuple[str, str]] = []
        for k, v in (d.get("text") or {}).items():
            if k not in TEXT_COLS:
                raise ValueError(f"no text filter for column {k!r}")
            s = str(v).strip().lower()[:100]
            if s:
                text.append((k, s))
        ranges: list[tuple[str, float | None, float | None]] = []
        for k, v in (d.get("ranges") or {}).items():
            if k not in RANGE_COLS:
                raise ValueError(f"no range filter for column {k!r}")
            if not isinstance(v, list | tuple) or len(v) != 2:
                raise ValueError(f"range for {k!r} must be [min, max]")
            lo, hi = (None if x is None or x == "" else float(x) for x in v)
            if lo is not None or hi is not None:
                ranges.append((k, lo, hi))
        signals = frozenset(str(s) for s in d.get("signals") or ())
        llm = frozenset(str(s) for s in d.get("llm") or ())
        if llm - LLM_VALUES:
            raise ValueError(f"llm filter values must be in {sorted(LLM_VALUES)}")
        sort = d.get("sort")
        if sort is not None and sort not in SORT_COLS:
            raise ValueError(f"cannot sort by {sort!r}")
        limit = int(d.get("limit") or 100)
        return cls(
            tuple(sorted(text)),
            tuple(sorted(ranges, key=lambda r: r[0])),
            signals,
            llm,
            sort,
            bool(d.get("desc", True)),
            max(1, min(limit, MAX_LIMIT)),
        )

    def key(self) -> tuple[Any, ...]:
        """Hashable identity, so clients sharing a query share one serialization."""
        return (
            self.text,
            self.ranges,
            tuple(sorted(self.signals)),
            tuple(sorted(self.llm)),
            self.sort,
            self.desc,
            self.limit,
        )

    # ------------------------------------------------------------------ evaluation

    def matches(self, r: ScanRow, llm_of: LlmOf) -> bool:
        for col, needle in self.text:
            hay = f"{r.title}\n{r.ticker}\n{r.event_ticker}" if col == "market" else r.category
            if needle not in hay.lower():
                return False
        for col, lo, hi in self.ranges:
            v = _num(r, col)
            if v is None or (lo is not None and v < lo) or (hi is not None and v > hi):
                return False
        if self.signals and not self.signals.intersection(r.signals):
            return False
        return not self.llm or (llm_of(r.ticker) or "none") in self.llm

    def run(self, ranked: Iterable[ScanRow], llm_of: LlmOf) -> tuple[int, list[ScanRow]]:
        """(number of matches, first `limit` matches in the requested order). `ranked` = scanner order."""
        hits = [r for r in ranked if self.matches(r, llm_of)]
        if self.sort is not None:
            keyed = [(self._sort_key(r, self.sort, llm_of), r) for r in hits]
            present = [kr for kr in keyed if kr[0] is not None]
            present.sort(key=lambda kr: kr[0], reverse=self.desc)  # stable: ties keep scanner rank
            hits = [r for _, r in present] + [r for k, r in keyed if k is None]
        return len(hits), hits[: self.limit]

    @staticmethod
    def _sort_key(r: ScanRow, col: str, llm_of: LlmOf) -> Any:
        if col == "market":
            return r.title.lower()
        if col == "category":
            return r.category.lower() or None
        if col == "signals":
            return len(r.signals) or None
        if col == "llm":
            return llm_of(r.ticker)
        return _num(r, col)
