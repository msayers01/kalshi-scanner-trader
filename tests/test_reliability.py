"""Crash/DB-failure injection, durable kill checkpoints, paper restart and lifecycle cleanup."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select, text

from kst.app import Application, run_application
from kst.config import Settings
from kst.core.bus import EventBus
from kst.core.clock import ManualClock
from kst.core.events import FillReceived, OrderUpdated
from kst.core.fixedpoint import Price, Qty
from kst.core.supervisor import Supervisor
from kst.core.types import Actor, BookSide, EngineState, OrderStatus
from kst.domain.orders import Order, TakeIntent
from kst.engine.state import EngineStateStore
from kst.persistence.db import Database
from kst.persistence.repo import Repository
from kst.persistence.tables import AuditRow, KVRow, OrderRow
from tests.conftest import T0, Harness


@pytest.fixture
async def repo(tmp_path: Path):
    r = Repository(Database(tmp_path / "reliability.db"), EventBus(), ManualClock(T0))
    await r.start()
    try:
        yield r
    finally:
        await r.stop()


def pending_order() -> Order:
    return Order("checkpoint-order", "M", BookSide.BID, Price(4000), Qty(100), status=OrderStatus.RESTING)


async def test_checkpoints_copy_mutable_payloads_and_orders(repo: Repository) -> None:
    payload = {"by_strategy": {"mm": {"trades": 1}}}
    repo.kv_set("portfolio", payload)
    repo.audit("test", "snapshot", payload)
    o = pending_order()
    repo.bus.publish(OrderUpdated(T0, o))
    payload["by_strategy"]["mm"]["trades"] = 2
    repo.kv_get("portfolio")["by_strategy"]["mm"]["trades"] = 3
    o.filled, o.status = Qty(100), OrderStatus.FILLED  # no new event: queued state must remain stable
    await repo.flush()
    async with repo.db.session() as session:
        assert (await session.get(KVRow, "portfolio")).value["v"]["by_strategy"]["mm"]["trades"] == 1
        assert (await session.execute(select(AuditRow))).scalar_one().payload == {
            "by_strategy": {"mm": {"trades": 1}}
        }
        saved = await session.get(OrderRow, o.client_order_id)
        assert saved.filled == 0 and saved.status == "resting"


def blocked_session(repo: Repository):
    """Hold an actual SQLite transaction after its first write until the test releases it."""
    original = repo.db.session
    entered, release = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def factory():
        async with original() as session:
            merge = session.merge

            async def held_merge(row):
                result = await merge(row)
                entered.set()
                await release.wait()
                return result

            session.merge = held_merge
            yield session

    return factory, entered, release


async def test_cancelled_flush_finishes_transaction_without_losing_newer_events(
    repo: Repository, monkeypatch
) -> None:
    factory, entered, release = blocked_session(repo)
    repo.kv_set("checkpoint", {"generation": 1})
    repo.audit("test", "once", {})
    with monkeypatch.context() as patch:
        patch.setattr(repo.db, "session", factory)
        task = asyncio.create_task(repo.flush())
        await asyncio.wait_for(entered.wait(), 2)
        assert repo.health()["inflight"] == 2 and repo.health()["pending"] == 2
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()  # cancellation must wait for the in-flight transaction
        repo.kv_set("checkpoint", {"generation": 2})
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
    async with repo.db.session() as session:
        assert (await session.get(KVRow, "checkpoint")).value["v"] == {"generation": 1}
    assert repo.health()["pending"] == 1
    await repo.flush()
    async with repo.db.session() as session:
        assert (await session.get(KVRow, "checkpoint")).value["v"] == {"generation": 2}
        assert len((await session.execute(select(AuditRow))).scalars().all()) == 1


async def test_concurrent_flushes_commit_in_order(repo: Repository, monkeypatch) -> None:
    factory, entered, release = blocked_session(repo)
    repo.kv_set("checkpoint", 1)
    with monkeypatch.context() as patch:
        patch.setattr(repo.db, "session", factory)
        first = asyncio.create_task(repo.flush())
        await asyncio.wait_for(entered.wait(), 2)
        repo.kv_set("checkpoint", 2)
        second = asyncio.create_task(repo.flush())
        release.set()
        await asyncio.wait_for(asyncio.gather(first, second), 2)
    async with repo.db.session() as session:
        assert (await session.get(KVRow, "checkpoint")).value["v"] == 2


async def test_failed_transaction_retries_rows_and_preserves_latest_state(
    repo: Repository, monkeypatch
) -> None:
    original = repo.db.session
    entered, release = asyncio.Event(), asyncio.Event()

    @asynccontextmanager
    async def failed_session():
        async with original() as session:
            merge = session.merge

            async def fail_after_write(row):
                await merge(row)
                entered.set()
                await release.wait()
                raise OSError("injected disk failure")

            session.merge = fail_after_write
            yield session

    repo.kv_set("checkpoint", 1)
    repo.audit("test", "once", {})
    o = pending_order()
    repo.bus.publish(OrderUpdated(T0, o))
    with monkeypatch.context() as patch:
        patch.setattr(repo.db, "session", failed_session)
        task = asyncio.create_task(repo.flush())
        await asyncio.wait_for(entered.wait(), 2)
        repo.kv_set("checkpoint", 2)
        o.status, o.filled = OrderStatus.FILLED, Qty(100)
        repo.bus.publish(OrderUpdated(T0 + 1, o))
        release.set()
        with pytest.raises(OSError, match="injected disk failure"):
            await task
    assert not repo.health()["healthy"] and repo.health()["pending"] == 3
    await repo.flush()
    assert repo.health()["healthy"] and repo.errors == 1
    async with repo.db.session() as session:
        assert (await session.get(KVRow, "checkpoint")).value["v"] == 2
        assert (await session.get(OrderRow, o.client_order_id)).status == "filled"
        assert len((await session.execute(select(AuditRow))).scalars().all()) == 1


async def test_kill_is_committed_before_exchange_cancel(repo: Repository, h: Harness, monkeypatch) -> None:
    h.state.persist, h.control.repo = repo.kv_set, repo

    async def cancel():
        async with repo.db.session() as session:
            checkpoint = await session.get(KVRow, EngineStateStore.KEY)
            assert checkpoint.value["v"]["state"] == "killed"
        return 0

    monkeypatch.setattr(h.oms, "cancel_all", cancel)
    result = await h.control.kill(Actor.SYSTEM, "durable validation")
    assert result["ok"] and result["persisted"]
    # A separate repository and state store see the committed intent without waiting for shutdown.
    fresh = Repository(Database(url=repo.db.url), EventBus(), repo.clock)
    await fresh.start()
    try:
        state = EngineStateStore(fresh.bus, fresh.clock)
        state.restore(fresh.kv_get(EngineStateStore.KEY))
        assert state.get() is EngineState.KILLED
    finally:
        await fresh.stop()


async def test_kill_attempts_cancel_even_if_checkpoint_fails(
    repo: Repository, h: Harness, monkeypatch
) -> None:
    h.state.persist, h.control.repo = repo.kv_set, repo
    with monkeypatch.context() as patch:
        patch.setattr(repo, "flush", AsyncMock(side_effect=OSError("disk unavailable")))
        cancel = AsyncMock(return_value=0)
        patch.setattr(h.oms, "cancel_all", cancel)
        result = await h.control.kill(Actor.SYSTEM, "validation")
        assert not result["ok"] and not result["persisted"]
        cancel.assert_awaited_once()
        assert h.state.get() is EngineState.KILLED and not h.control.autotrade


async def test_failed_writer_immediately_kills_and_blocks_resume(
    repo: Repository, h: Harness, monkeypatch
) -> None:
    h.state.persist, h.control.repo = repo.kv_set, repo
    repo.flush_s = 0
    notified = asyncio.Event()

    async def on_failure(name, error):
        await h.control.on_critical_task_failure(name, error)
        notified.set()

    sup = Supervisor(on_failure)
    sup.add("db_writer", repo.run, critical=True, max_restarts=0)
    repo.kv_set("pending", 1)

    @asynccontextmanager
    async def unavailable():
        raise OSError("database unavailable")
        yield  # pragma: no cover

    with monkeypatch.context() as patch:
        patch.setattr(repo.db, "session", unavailable)
        sup.start()
        try:
            await asyncio.wait_for(notified.wait(), 2)
            assert h.state.get() is EngineState.KILLED
            assert not repo.health()["healthy"] and repo.writer_failed
            assert not (await h.control.resume(Actor.SYSTEM))["ok"]
        finally:
            await sup.stop()


@pytest.mark.parametrize("returns", [True, False])
async def test_critical_worker_failure_escalates_before_retry_budget_exhausted(returns: bool) -> None:
    notified = asyncio.Event()
    calls = []

    async def worker():
        if not returns:
            raise RuntimeError("worker failed")

    async def failed(name, detail):
        calls.append((name, detail))
        notified.set()

    sup = Supervisor(failed)
    sup.add("critical", worker, critical=True, max_restarts=20)
    sup.start()
    try:
        await asyncio.wait_for(notified.wait(), 1)
        assert calls[0][0] == "critical" and calls[0][1]
        assert sup.health()["critical"]["restarts"] == 1
        with pytest.raises(RuntimeError, match="already started"):
            sup.start()
    finally:
        await sup.stop()
    assert not sup.health()["critical"]["running"]


def settings_for(tmp_path: Path) -> Settings:
    s = Settings(_env_file=None)
    s.app.data_dir = tmp_path
    s.web.enabled = s.discord.enabled = s.feeds.crypto.enabled = s.feeds.weather.enabled = False
    s.llm.mode = "off"
    s.paper.starting_balance_usd = 1000
    return s


async def test_paper_restart_preserves_checkpoint_and_fill_history(tmp_path: Path, h: Harness) -> None:
    s = settings_for(tmp_path)
    market = h.add_market("M")
    h.set_book("M", [(0.38, 100)], [(0.40, 100)])

    async def start_local():
        app = Application(s, clock=ManualClock(T0))
        await app._restore()
        app.catalog.markets["M"] = market
        app.md.books["M"] = h.md.book("M")
        await app.broker.start()
        return app

    first = await start_local()
    fills = []
    first.bus.subscribe(FillReceived, lambda e: fills.append(e.fill))
    intent = TakeIntent("M", BookSide.BID, Price(4000), Qty(100))
    try:
        o1 = await first.oms.take(intent, "edge_taker")
        assert o1.status is OrderStatus.FILLED
        cash = first.portfolio.cash
        await first.repo.flush()
    finally:
        await first.stop()

    second = await start_local()
    second_fills = []
    second.bus.subscribe(FillReceived, lambda e: second_fills.append(e.fill))
    try:
        assert second.portfolio.cash == cash and second.portfolio.positions["M"].qty == 100
        second.oms.on_broker_fill(fills[0])
        second.portfolio.apply_fill(fills[0])  # both ingress points reject a committed duplicate
        assert second.portfolio.cash == cash and not second_fills
        o2 = await second.oms.take(intent, "edge_taker")
        assert o2.status is OrderStatus.FILLED
        assert fills[0].fill_id != second_fills[0].fill_id
        await second.repo.flush()
        assert len(await second.repo.recent_fills()) == 2
        assert second.portfolio.cash == cash - 400_000 - o2.fees
        final_cash = second.portfolio.cash
    finally:
        await second.stop()

    third = await start_local()
    try:
        assert third.portfolio.cash == final_cash
        assert third.portfolio.positions["M"].qty == 200
        assert third.portfolio.daily_report().trades == 2
    finally:
        await third.stop()


async def test_failed_startup_releases_initialized_resources(tmp_path: Path, monkeypatch) -> None:
    app = Application(settings_for(tmp_path), clock=ManualClock(T0))
    closed = AsyncMock(wraps=app.rest.aclose)
    monkeypatch.setattr(app.rest, "aclose", closed)
    monkeypatch.setattr(app.broker, "start", AsyncMock(side_effect=RuntimeError("startup failed")))
    with pytest.raises(RuntimeError, match="startup failed"):
        await run_application(app, asyncio.Event())
    closed.assert_awaited_once()
    assert not app.repo._unsubs


@pytest.mark.parametrize("component", ["supervisor", "broker", "repo"])
async def test_shutdown_releases_other_resources_after_cleanup_failure(
    tmp_path: Path, monkeypatch, component: str
) -> None:
    app = Application(settings_for(tmp_path), clock=ManualClock(T0))
    await app._restore()
    obj = getattr(app, component)
    original = obj.stop

    async def failing_stop():
        await original()
        raise RuntimeError(f"{component} cleanup failed")

    monkeypatch.setattr(obj, "stop", failing_stop)
    closed = AsyncMock(wraps=app.rest.aclose)
    monkeypatch.setattr(app.rest, "aclose", closed)
    with pytest.raises(RuntimeError, match=f"{component} cleanup failed"):
        await app.stop()
    closed.assert_awaited_once()
    assert not app.repo._unsubs


async def test_duplicate_order_submission_keeps_original_tracking(hs: Harness) -> None:
    hs.add_market("M")
    hs.set_book("M", [(0.38, 10)], [(0.42, 10)])
    original = pending_order()
    original.status = OrderStatus.PENDING_NEW
    assert (await hs.oms.submit(original)).ok
    duplicate = pending_order()
    result = await hs.oms.submit(duplicate)
    assert not result.ok and result.check == "DuplicateClientOrderId"
    assert hs.oms.orders[original.client_order_id] is original
    assert hs.oms.live_orders() == [original]


async def test_sqlite_uses_durable_wal_commits(repo: Repository) -> None:
    async with repo.db.session() as session:
        assert (await session.execute(text("PRAGMA journal_mode"))).scalar() == "wal"
        assert (await session.execute(text("PRAGMA synchronous"))).scalar() == 2


async def test_unexpected_critical_cancellation_escalates() -> None:
    alerted = asyncio.Event()

    async def worker():
        raise asyncio.CancelledError

    async def failure(name, error):
        assert name == "critical" and "cancelled unexpectedly" in error
        alerted.set()

    sup = Supervisor(failure)
    sup.add("critical", worker, critical=True, max_restarts=0)
    sup.start()
    try:
        await asyncio.wait_for(alerted.wait(), 1)
    finally:
        await sup.stop()


async def test_repository_shutdown_reports_failure_and_still_closes_database(
    repo: Repository, monkeypatch
) -> None:
    closed = AsyncMock(wraps=repo.db.close)
    with monkeypatch.context() as patch:
        patch.setattr(repo, "flush", AsyncMock(side_effect=OSError("disk failed during shutdown")))
        patch.setattr(repo.db, "close", closed)
        with pytest.raises(OSError, match="disk failed during shutdown"):
            await repo.stop()
        closed.assert_awaited_once()
        assert not repo._unsubs


async def test_database_recovery_requires_writer_restart_and_explicit_resume(
    repo: Repository, h: Harness
) -> None:
    h.control.repo = repo
    h.state.set(EngineState.KILLED, Actor.SYSTEM, "database failed")
    repo.writer_failed = True
    repo.kv_set("recovered", True)
    await repo.flush()
    assert not (await h.control.resume(Actor.SYSTEM))["ok"]  # a successful read/write alone is insufficient
    writer = asyncio.create_task(repo.run())
    try:
        await asyncio.sleep(0)  # the supervised writer re-enters its loop after backoff
        assert repo.health()["healthy"]
        assert h.state.get() is EngineState.KILLED
        assert (await h.control.resume(Actor.SYSTEM))["ok"]
        assert h.state.get() is EngineState.RUNNING
    finally:
        writer.cancel()
        await asyncio.gather(writer, return_exceptions=True)
