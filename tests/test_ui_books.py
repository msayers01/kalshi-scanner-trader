"""REST mode: books open in the UI get a fast refresh of their own (marketdata.ui_book_interval_s)."""

from __future__ import annotations

import asyncio
from typing import Any

from kst.core.events import BookUpdated
from tests.conftest import Harness


def _row(ticker: str, yes_bid: str, no_bid: str) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "orderbook_fp": {"yes_dollars": [[yes_bid, "10.00"]], "no_dollars": [[no_bid, "10.00"]]},
    }


async def test_ui_live_books_poll_fast_and_only_those(hs: Harness) -> None:
    md = hs.md
    md.cfg.ui_book_interval_s = 0.2
    asked: list[list[str]] = []

    async def fake_orderbooks(tickers: list[str]) -> list[dict[str, Any]]:
        asked.append(list(tickers))
        return [_row(t, "0.4000", "0.5800") for t in tickers]

    md.rest.orderbooks = fake_orderbooks  # type: ignore[method-assign]
    md.request_focus("scanner", ["A", "B", "C"])  # the 3 s loop's job, not this one's
    md.request_focus(md.UI_LIVE, ["B"])
    seen: list[str] = []
    hs.bus.subscribe(BookUpdated, lambda e: seen.append(e.ticker))

    task = asyncio.create_task(md.run_poll_ui_books())
    await asyncio.sleep(0.5)
    task.cancel()

    assert len(asked) >= 2 and all(a == ["B"] for a in asked)
    b = md.books["B"]
    assert b.best_bid == 4000 and b.best_ask == 4200  # NO bid 0.58 → YES ask 0.42
    assert set(seen) == {"B"}


async def test_ui_live_poll_disabled_and_idle(hs: Harness) -> None:
    hs.md.cfg.ui_book_interval_s = 0
    await asyncio.wait_for(hs.md.run_poll_ui_books(), 0.1)  # returns immediately when off
