"""Streaming recording readers. Preserve arrival order; never sort late prints into the past."""

from __future__ import annotations

import asyncio
import glob
import gzip
import heapq
import itertools
from collections.abc import AsyncGenerator, Generator, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import orjson

KINDS = frozenset({"catalog", "mkt", "book", "delta", "trade", "ref", "seed", "status", "weather", "ticker"})


@dataclass(frozen=True, slots=True)
class Record:
    ts_ms: int
    kind: str
    payload: dict[str, Any]
    location: str
    legacy: bool
    clamped: bool = False


def integer(value: Any, name: str, minimum: int = 0, maximum: int | None = None) -> int:
    if type(value) is not int or value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"{name} must be an integer in [{minimum}, {maximum or 'unbounded'}]")
    return value


def expand_paths(patterns: list[str]) -> list[Path]:
    paths = set()
    for pattern in patterns:
        matches = glob.glob(pattern)
        if not matches:
            raise ValueError(f"No recordings match {pattern!r}. Enable marketdata.record to collect inputs.")
        for match in matches:
            path = Path(match).resolve()
            if not path.is_file():
                raise ValueError(f"Not a recording file: {path}")
            paths.add(path)
    return sorted(paths)


def read_records(path: Path) -> Iterator[Record]:
    previous = 0
    opener = gzip.open if path.suffix == ".gz" else open
    try:
        with opener(path, "rb") as stream:
            for line_no, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                location = f"{path}:{line_no}"
                try:
                    row = orjson.loads(line)
                    if not isinstance(row, dict) or row.get("k") not in KINDS:
                        raise ValueError("unknown or missing recording kind")
                    ts = integer(row.get("t"), "t", 1)
                    schema = row.get("schema", 1)
                    if type(schema) is not int or schema not in (1, 2):
                        raise ValueError(f"unsupported recording schema {schema}")
                    if schema == 2 and ts < previous:
                        raise ValueError("receipt timestamps go backwards")
                    source = integer(row.get("source_t", ts), "source_t", 0)
                    if source > ts:
                        raise ValueError("source timestamp is later than receipt time")
                    effective = max(previous, ts)
                    yield Record(effective, row["k"], row, location, schema == 1, effective != ts)
                    previous = effective
                except (ValueError, TypeError) as exc:
                    raise ValueError(f"{location}: {exc}") from exc
    except (OSError, EOFError) as exc:
        raise ValueError(f"Cannot read complete recording {path}: {exc}") from exc


def merge_recordings(paths: list[Path]) -> Generator[Record, None, None]:
    # Equal timestamps preserve source-file order and line order.
    yield from heapq.merge(*(read_records(path) for path in paths), key=lambda r: r.ts_ms)


async def record_groups(paths: list[Path]) -> AsyncGenerator[tuple[int, list[Record]], None]:
    """Read/decompress off the event loop with bounded batches, preserving timestamp groups."""
    stream = merge_recordings(paths)
    pending: list[Record] = []
    try:
        while batch := await asyncio.to_thread(lambda: list(itertools.islice(stream, 256))):
            for record in batch:
                if pending and record.ts_ms != pending[0].ts_ms:
                    yield pending[0].ts_ms, pending
                    pending = []
                pending.append(record)
        if pending:
            yield pending[0].ts_ms, pending
    finally:
        await asyncio.to_thread(stream.close)
