from __future__ import annotations

from typing import Any

import orjson
import pytest

from kst.core.fixedpoint import Price, Qty
from kst.scanner.query import ScanQuery
from kst.scanner.service import ScanRow


def _row(ticker: str, **kw: Any) -> ScanRow:
    base: dict[str, Any] = {
        "ticker": ticker,
        "event_ticker": f"EV-{ticker}",
        "series": "S",
        "title": f"Title {ticker}",
        "category": "Crypto",
        "bid": Price(4000),
        "ask": Price(4200),
        "bid_qty": Qty(100),
        "ask_qty": Qty(100),
        "volume_24h": 100.0,
        "open_interest": 0.0,
        "chg_24h": None,
        "secs_to_close": 7200.0,
        "has_book": True,
    }
    base.update(kw)
    return ScanRow(**base)


ROWS = [
    _row("BTC-1", title="Bitcoin above 80k", score=3.0, edge_buy=0.03, q=0.45, signals=["EDGE+"]),
    _row("ETH-1", title="Ethereum above 3k", score=2.0, bid=Price(9000), ask=Price(9200), volume_24h=5000.0),
    _row(
        "NYC-1", title="NYC high 60-61", category="Climate", score=1.0, secs_to_close=None, signals=["WIDE"]
    ),
    _row("BTC-2", title="Bitcoin above 90k", score=0.5, bid=None, ask=None, edge_sell=-0.01, q=0.1),
]
LLM = {"BTC-1": "E", "ETH-1": "S"}


def run(d: dict[str, Any]) -> tuple[int, list[str]]:
    n, rows = ScanQuery.parse(d).run(ROWS, LLM.get)
    return n, [r.ticker for r in rows]


def test_empty_query_is_inactive_and_keeps_rank() -> None:
    assert not ScanQuery.parse(None).active and not ScanQuery.parse({}).active
    assert not ScanQuery.parse({"text": {"market": "  "}, "ranges": {"bid": [None, ""]}}).active
    assert run({}) == (4, ["BTC-1", "ETH-1", "NYC-1", "BTC-2"])


def test_text_filters_search_title_ticker_and_event() -> None:
    assert run({"text": {"market": "bitcoin"}}) == (2, ["BTC-1", "BTC-2"])
    assert run({"text": {"market": "nyc-1"}}) == (1, ["NYC-1"])  # ticker
    assert run({"text": {"market": "ev-eth"}}) == (1, ["ETH-1"])  # event
    assert run({"text": {"category": "clim"}}) == (1, ["NYC-1"])


def test_ranges_are_in_display_units_and_exclude_missing() -> None:
    assert run({"ranges": {"bid": [50, None]}}) == (1, ["ETH-1"])  # cents
    assert run({"ranges": {"bid": [None, 40]}})[1] == ["BTC-1", "NYC-1"]  # BTC-2 has no bid
    assert run({"ranges": {"edge": [0, None]}}) == (1, ["BTC-1"])  # 3¢ edge
    assert run({"ranges": {"fair": [40, 50]}}) == (1, ["BTC-1"])
    assert run({"ranges": {"vol24h": [1000, None]}}) == (1, ["ETH-1"])
    assert run({"ranges": {"closes": [None, 1.5]}}) == (0, [])  # hours
    assert run({"ranges": {"closes": [1.5, 2.5]}})[0] == 3


def test_sets() -> None:
    assert run({"signals": ["EDGE+", "WIDE"]}) == (2, ["BTC-1", "NYC-1"])
    assert run({"llm": ["none"]}) == (2, ["NYC-1", "BTC-2"])
    assert run({"llm": ["E", "S"]}) == (2, ["BTC-1", "ETH-1"])


def test_sort_puts_missing_last_in_both_directions() -> None:
    assert run({"sort": "bid", "desc": True})[1] == ["ETH-1", "BTC-1", "NYC-1", "BTC-2"]
    assert run({"sort": "bid", "desc": False})[1] == ["BTC-1", "NYC-1", "ETH-1", "BTC-2"]
    assert run({"sort": "market", "desc": False})[1] == ["BTC-1", "BTC-2", "ETH-1", "NYC-1"]
    assert run({"sort": "closes", "desc": False})[1][-1] == "NYC-1"


def test_limit_and_count() -> None:
    n, rows = run({"sort": "vol24h", "limit": 2})
    assert n == 4 and rows == ["ETH-1", "BTC-1"]


@pytest.mark.parametrize(
    "bad",
    [
        {"nope": 1},
        {"text": {"bid": "x"}},
        {"ranges": {"market": [1, 2]}},
        {"ranges": {"bid": [1]}},
        {"ranges": {"bid": ["a", None]}},
        {"llm": ["maybe"]},
        {"sort": "score; drop"},
        "string",
    ],
)
def test_parse_rejects_bad_input(bad: Any) -> None:
    with pytest.raises((ValueError, TypeError)):
        ScanQuery.parse(bad)


class _FakeWs:
    pass


def test_hub_sends_each_client_its_own_scan(tmp_path: Any) -> None:
    from kst.api.hub import _Client
    from kst.app import Application
    from kst.config import Settings
    from kst.scanner.service import ScanSnapshot

    s = Settings()
    s.app.data_dir = tmp_path
    s.llm.mode = "off"
    app = Application(s)
    app.scanner.latest = ScanSnapshot(
        ts_ms=1,
        rows=ROWS[:2],
        arbs=[],
        universe=4,
        priced=4,
        duration_ms=1.0,
        by_ticker={r.ticker: r for r in ROWS},
    )
    plain, filtered = _Client(_FakeWs()), _Client(_FakeWs())  # type: ignore[arg-type]
    filtered.scan_query = ScanQuery.parse({"text": {"category": "climate"}})
    app.hub.clients = {plain, filtered}
    app.hub._dirty.add("scan")
    app.hub.flush()

    def scan_of(c: _Client) -> dict[str, Any]:
        while not c.queue.empty():
            m = orjson.loads(c.queue.get_nowait())
            if m["type"] == "scan":
                return dict(m["data"])
        raise AssertionError("no scan frame")

    p, f = scan_of(plain), scan_of(filtered)
    assert [r["ticker"] for r in p["rows"]] == ["BTC-1", "ETH-1"] and p["matched"] is None
    assert [r["ticker"] for r in f["rows"]] == ["NYC-1"] and f["matched"] == 1
