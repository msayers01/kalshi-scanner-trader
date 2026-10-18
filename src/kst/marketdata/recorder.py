"""Market-data recorder → data/recordings/YYYY-MM-DD.jsonl.gz (one normalized event per line).

Schema 2 records receipt time plus source time, catalog/fee snapshots, books/deltas/trades/tickers,
reference prices and volatility seeds, weather inputs and lifecycle settlements. Legacy raw `mkt`
records remain readable. Prices/qty are integer fixed-point units. See docs/REPLAY.md.
"""

from __future__ import annotations

import gzip
from pathlib import Path
from typing import Any

import orjson

from kst.core.clock import ms_to_dt


class Recorder:
    def __init__(self, directory: Path, flush_every: int = 500) -> None:
        self.dir = directory
        self.dir.mkdir(parents=True, exist_ok=True)
        self._fh: gzip.GzipFile | None = None
        self._day = ""
        self._n = 0
        self.flush_every = flush_every
        self.written = 0
        self._last_t = 0
        self._catalog: dict[str, Any] | None = None

    def _write(self, ts_ms: int, obj: dict[str, Any]) -> None:
        # t is availability/receipt time, not an exchange timestamp that can arrive late.
        ts_ms = max(ts_ms, self._last_t)
        self._last_t = ts_ms
        day = ms_to_dt(ts_ms).strftime("%Y-%m-%d")
        if day != self._day:
            self.close()
            self._fh = gzip.open(self.dir / f"{day}.jsonl.gz", "ab")  # noqa: SIM115 — long-lived handle, closed in close()
            self._day = day
            if self._catalog is not None and obj["k"] != "catalog":
                self._fh.write(
                    orjson.dumps({"k": "catalog", "schema": 2, "t": ts_ms, **self._catalog}) + b"\n"
                )
        assert self._fh is not None
        obj["t"] = ts_ms
        obj["schema"] = 2
        self._fh.write(orjson.dumps(obj) + b"\n")
        self.written += 1
        self._n += 1
        if self._n >= self.flush_every:
            self._fh.flush()
            self._n = 0

    def book(
        self,
        ts_ms: int,
        ticker: str,
        bids: list[tuple[int, int]],
        asks: list[tuple[int, int]],
        *,
        received_ms: int | None = None,
    ) -> None:
        self._write(received_ms or ts_ms, {"k": "book", "m": ticker, "b": bids, "a": asks, "source_t": ts_ms})

    def delta(
        self, ts_ms: int, ticker: str, side: str, price: int, delta: int, *, received_ms: int | None = None
    ) -> None:
        self._write(
            received_ms or ts_ms,
            {"k": "delta", "m": ticker, "s": side, "p": price, "d": delta, "source_t": ts_ms},
        )

    def trade(
        self,
        ts_ms: int,
        ticker: str,
        price: int,
        qty: int,
        taker_side: str,
        *,
        received_ms: int | None = None,
    ) -> None:
        self._write(
            received_ms or ts_ms,
            {"k": "trade", "m": ticker, "p": price, "q": qty, "s": taker_side, "source_t": ts_ms},
        )

    def ref(
        self, ts_ms: int, index_id: str, value: float, source: str, *, received_ms: int | None = None
    ) -> None:
        self._write(
            received_ms or ts_ms,
            {"k": "ref", "i": index_id, "value": value, "src": source, "source_t": ts_ms},
        )

    def catalog(self, ts_ms: int, snapshot: dict[str, Any]) -> None:
        self._catalog = snapshot
        self._write(ts_ms, {"k": "catalog", **snapshot})

    def status(self, ts_ms: int, ticker: str, status: str, result: str, payout: int | None) -> None:
        self._write(ts_ms, {"k": "status", "m": ticker, "status": status, "result": result, "payout": payout})

    def ticker(self, ts_ms: int, ticker: str, raw: dict[str, Any], *, received_ms: int | None = None) -> None:
        self._write(received_ms or ts_ms, {"k": "ticker", "m": ticker, "raw": raw, "source_t": ts_ms})

    def seed(self, ts_ms: int, index_id: str, closes: list[float], interval_s: float) -> None:
        self._write(ts_ms, {"k": "seed", "i": index_id, "closes": closes, "interval_s": interval_s})

    def weather(self, ts_ms: int, payload: dict[str, Any]) -> None:
        self._write(ts_ms, {"k": "weather", **payload})

    def market(self, ts_ms: int, raw: dict[str, Any]) -> None:
        self._write(ts_ms, {"k": "mkt", "raw": raw})

    def close(self) -> None:
        if self._fh is not None:
            self._fh.close()
            self._fh = None


def read_recording(path: Path) -> list[dict[str, Any]]:
    with gzip.open(path, "rb") as fh:
        return [orjson.loads(line) for line in fh if line.strip()]
