"""Scorecard accounting, coverage and persistent execution observations."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from kst.api.app import create_app
from kst.app import Application
from kst.config import Settings
from kst.core.bus import EventBus
from kst.core.clock import ManualClock
from kst.core.events import FillReceived, MarketSettled, OrderUpdated, PortfolioUpdated
from kst.core.fixedpoint import Micros, Price, Qty
from kst.core.types import BookSide, Mode, OrderStatus
from kst.domain.orderbook import OrderBook
from kst.domain.orders import Fill, Order
from kst.persistence.db import Database
from kst.persistence.repo import Repository
from kst.portfolio.scorecard import ScorecardRecorder, paper_scorecard


def fill(
    key: str, side: BookSide, price: int, qty: int, fee: int, ts: int, strategy: str = "edge_taker"
) -> Fill:
    return Fill(key, key, "", "M", side, Price(price), Qty(qty), Micros(fee), True, ts, strategy)


@pytest.fixture
async def repo(tmp_path: Path):
    r = Repository(Database(tmp_path / "scorecard.db"), EventBus(), ManualClock(100_000))
    await r.start()
    yield r
    await r.stop()


def record(repo: Repository, f: Fill, expected: int | None = None, mid: int | None = None) -> None:
    repo.bus.publish(FillReceived(f.ts_ms, f))
    if expected is not None or mid is not None:
        repo.audit(
            "scorecard",
            "scorecard_fill",
            {
                "fill_id": f.fill_id,
                "expected_per_contract_micros": expected,
                "decision_mid": mid,
            },
        )


async def test_partial_exit_and_settlement_compare_same_remaining_contracts(repo: Repository) -> None:
    record(repo, fill("entry", BookSide.BID, 4000, 200, 20_000, 1), 100_000, 3900)
    record(repo, fill("exit", BookSide.ASK, 6000, 100, 10_000, 2, "flatten"))
    repo.bus.publish(MarketSettled(3, "M", "yes", 100, 600_000))
    await repo.flush()
    data = await paper_scorecard(repo, 4)
    entry = next(r for r in data["strategies"] if r["strategy"] == "edge_taker")
    assert data["totals"]["gross_realized_usd"] == pytest.approx(0.8)
    assert data["totals"]["fees_usd"] == pytest.approx(0.03)
    assert data["totals"]["net_realized_usd"] == pytest.approx(0.77)
    assert entry["fees_usd"] == pytest.approx(0.03)  # flatten fee belongs to entry owner
    assert entry["expected_entry_usd"] == pytest.approx(0.2)
    assert entry["paired_settled_contracts"] == 1
    assert entry["settled_expected_usd"] == pytest.approx(0.1)
    assert entry["settled_actual_usd"] == pytest.approx(0.59)
    assert entry["slippage_cents"] == 1
    assert entry["win_rate"] == 1


async def test_no_position_flip_fee_allocation_and_missing_history(repo: Repository) -> None:
    record(repo, fill("no", BookSide.ASK, 7000, 100, 10_000, 1), mid=7200)
    # Buy two YES: one closes NO at a loss, one opens a YES lot.
    record(repo, fill("flip", BookSide.BID, 8000, 200, 20_001, 2, "manual"))
    repo.bus.publish(MarketSettled(3, "M", "yes", 100, 200_000))
    await repo.flush()
    data = await paper_scorecard(repo, 4)
    rows = {r["strategy"]: r for r in data["strategies"]}
    assert rows["edge_taker"]["net_realized_usd"] == pytest.approx(-0.12)
    assert rows["manual"]["net_realized_usd"] == pytest.approx(0.189999)
    assert data["totals"]["fees_usd"] == pytest.approx(0.030001)
    assert rows["edge_taker"]["slippage_cents"] == 2  # selling below decision mid costs
    assert rows["manual"]["expected_entry_usd"] is None
    assert rows["manual"]["settled_expected_usd"] is None
    assert rows["manual"]["slippage_cents"] is None
    assert rows["edge_taker"]["win_rate"] == 0


async def test_order_rates_partial_fills_rejections_and_empty(repo: Repository) -> None:
    assert (await paper_scorecard(repo, 0))["totals"]["order_fill_rate"] is None
    for key, status in [
        ("partial", OrderStatus.CANCELED),
        ("rest", OrderStatus.RESTING),
        ("reject", OrderStatus.REJECTED),
    ]:
        o = Order(key, "M", BookSide.BID, Price(4000), Qty(300), status=status, strategy="manual")
        repo.bus.publish(OrderUpdated(1, o))
    record(repo, fill("partial", BookSide.BID, 4000, 100, 0, 2, "manual"))
    await repo.flush()
    row = (await paper_scorecard(repo, 3))["totals"]
    assert row["orders"] == 3
    assert row["rejected"] == 1
    assert row["order_fill_rate"] == 0.5
    assert row["filled_contracts"] == 1
    assert row["win_rate"] is None


async def test_recorder_restart_markout_window_and_price_improvement(tmp_path: Path) -> None:
    settings = Settings()
    settings.app.data_dir = tmp_path
    clock = ManualClock(100_000)
    app = Application(settings, clock=clock)
    await app.repo.start()
    b = OrderBook("M")
    b.apply_snapshot([(4000, 1000)], [(4200, 1000)], clock.now_ms())
    app.md.books["M"] = b
    o = Order("entry", "M", BookSide.BID, Price(4300), Qty(100), is_entry=True, expected_edge=0.1)
    app.oms.orders[o.client_order_id] = o
    app.bus.publish(OrderUpdated(clock.now_ms(), o))
    # Preserve decision context even though the market moves before the broker fill.
    b.apply_snapshot([(4100, 1000)], [(4300, 1000)], clock.now_ms())
    f = fill("entry", BookSide.BID, 4200, 100, 1000, clock.now_ms())
    app.bus.publish(FillReceived(f.ts_ms, f))
    app.bus.publish(FillReceived(f.ts_ms, f))  # repeated delivery must not overwrite fill-time context
    await app.repo.flush()
    _, _, _, recorded = await app.repo.scorecard_rows()
    assert sum(r.action == "scorecard_fill" for r in recorded) == 1
    restored = ScorecardRecorder(app)
    await restored.restore()
    assert "entry" in restored.pending
    clock.advance(60)
    # Stale book must not be counted as the 60s observation.
    restored.on_tick(PortfolioUpdated(clock.now_ms(), None))
    assert "entry" in restored.pending
    b.apply_snapshot([(3900, 1000)], [(4100, 1000)], clock.now_ms())
    restored.on_tick(PortfolioUpdated(clock.now_ms(), None))
    assert not restored.pending
    await app.repo.flush()
    row = (await paper_scorecard(app.repo, clock.now_ms()))["totals"]
    assert row["markout_60s_cents"] == -2
    assert row["slippage_cents"] == 1
    assert row["expected_entry_usd"] == pytest.approx(0.11)
    await app.repo.stop()
    await app.rest.aclose()


async def test_scorecard_endpoint_auth_and_mode(tmp_path: Path) -> None:
    from pydantic import SecretStr

    settings = Settings()
    settings.app.data_dir = tmp_path
    settings.web.api_token = SecretStr("test-token")
    app = Application(settings)
    await app.repo.start()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=create_app(app)), base_url="http://test"
    ) as client:
        assert (await client.get("/api/scorecard")).status_code == 401
        headers = {"Authorization": "Bearer test-token"}
        response = await client.get("/api/scorecard", headers=headers)
        assert response.status_code == 200
        assert response.json()["strategies"] == []
        app.mode = Mode.LIVE
        assert (await client.get("/api/scorecard", headers=headers)).status_code == 409
    await app.repo.stop()
    await app.rest.aclose()


async def test_scalar_payout_and_legacy_missing_coverage(repo: Repository) -> None:
    record(repo, fill("scalar", BookSide.ASK, 7000, 100, 2000, 1))
    repo.bus.publish(MarketSettled(2, "M", "scalar", -100, 150_000, 5500))
    repo.audit("scorecard", "scorecard_settlement", {"ticker": "M", "ts_ms": 2, "price": 5500})
    await repo.flush()
    data = await paper_scorecard(repo, 3)
    assert data["totals"]["settled_net_usd"] == pytest.approx(0.148)
    assert data["unsupported_settlements"] == 0
    record(repo, fill("legacy", BookSide.BID, 4000, 100, 1000, 4))
    repo.bus.publish(MarketSettled(5, "M", "scalar", 100, 100_000))
    await repo.flush()
    data = await paper_scorecard(repo, 6)
    assert data["unsupported_settlements"] == 1
    assert data["totals"]["settled_contracts"] == 1
