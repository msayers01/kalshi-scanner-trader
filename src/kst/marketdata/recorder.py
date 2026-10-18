"""Market-data recorder → data/recordings/YYYY-MM-DD.jsonl.gz (one normalized event per line).

Line kinds: book (snapshot), delta, trade, ref (reference index), mkt (market metadata at first sight).
Prices/qty are integer fixed-point units. Used by `kst replay` (backtest/replay.py).
"""

from __future__ import annotations

import gzip
from pathlib import Path
from typing import IO, Any

import orjson

from kst.core.clock import ms_to_dt


class Recorder:
    def __init__(self, directory: Path, flush_every: int = 500) -> None:
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self._fh: IO[bytes] | None = None
        self._day = ""
        self._n = 0
        self.flush_every = flush_every
        self.written = 0

    def _write(self, ts_ms: int, obj: dict[str, Any]) -> None:
        day = ms_to_dt(ts_ms).strftime("%Y-%m-%d")
        if day != self._day:
            self.close()
            self._fh = gzip.open(self.dir / f"{day}.jsonl.gz", "ab")
            self._day = day
        assert self._fh is not None
        obj["t"] = ts_ms
        self._fh.write(orjson.dumps(obj) + b"\n")
        self.written += 1
        self._n += 1
        if self._n >= self.flush_every:
            self._fh.flush()
            self._n = 0

    def book(self, ts_ms: int, ticker: str, bids: list[tuple[int, int]], asks: list[tuple[int, int]]) -> None:
        self._write(ts_ms, {"k": "book", "m": ticker, "b": bids, "a": asks})

    def delta(self, ts_ms: int, ticker: str, side: str, price: int, delta: int) -> None:
        self._write(ts_ms, {"k": "delta", "m": ticker, "s": side, "p": price, "d": delta})

    def trade(self, ts_ms: int, ticker: str, price: int, qty: int, taker_side: str) -> None:
        self._write(ts_ms, {"k": "trade", "m": ticker, "p": price, "q": qty, "s": taker_side})

    def ref(self, ts_ms: int, index_id: str, value: float, source: str) -> None:
        self._write(ts_ms, {"k": "ref", "i": index_id, "v": value, "src": source})

    def market(self, ts_ms: int, raw: dict[str, Any]) -> None:
        self._write(ts_ms, {"k": "mkt", "raw": raw})

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def read_recording(path: Path) -> list[dict[str, Any]]:
    with gzip.open(path, "rb") as fh:
        return [orjson.loads(line) for line in fh if line.strip()]
