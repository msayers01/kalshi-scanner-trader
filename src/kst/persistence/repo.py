"""Repository with a write-behind queue and explicit durable control checkpoints.

* kv_state is cached in memory (loaded at startup); `kv_set` updates the cache and enqueues a write.
* Orders are coalesced (latest state per client_order_id) and flushed every `flush_s`.
* Bus subscriptions persist fills, settlements, daily reports, and sampled equity snapshots.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from copy import deepcopy
from dataclasses import replace
from typing import Any

from sqlalchemy import desc, select

from kst.core.bus import EventBus
from kst.core.clock import Clock
from kst.core.events import DailyReport, FillReceived, MarketSettled, OrderUpdated, PortfolioUpdated
from kst.core.logging import get_logger
from kst.domain.orders import Order
from kst.persistence.db import Database
from kst.persistence.tables import (
    AuditRow,
    DailyPnLRow,
    EquitySnapshotRow,
    FillRow,
    KVRow,
    LLMDecisionRow,
    OrderRow,
    SettlementRow,
)

log = get_logger(__name__)


class Repository:
    def __init__(
        self, db: Database, bus: EventBus, clock: Clock, *, flush_s: float = 0.5, equity_every_s: float = 60.0
    ) -> None:
        self.db = db
        self.bus = bus
        self.clock = clock
        self.flush_s = flush_s
        self.equity_every_ms = int(equity_every_s * 1000)
        self._kv: dict[str, Any] = {}
        self._pending_kv: dict[str, Any] = {}
        self._pending_orders: dict[str, Order] = {}
        self._rows: list[Any] = []
        self._last_equity_ms = 0
        self.written = 0
        self.errors = 0
        self.last_error = ""
        self.last_flush_ms: int | None = None
        self.writer_failed = False
        self._flush_lock = asyncio.Lock()
        self._unsubs: list[Callable[[], None]] = []
        self._inflight = 0

    def health(self) -> dict[str, Any]:
        return {
            "healthy": not self.last_error and not self.writer_failed,
            "pending": len(self._pending_kv) + len(self._pending_orders) + len(self._rows) + self._inflight,
            "inflight": self._inflight,
            "written": self.written,
            "errors": self.errors,
            "last_error": self.last_error,
            "last_flush_ms": self.last_flush_ms,
            "writer_failed": self.writer_failed,
        }

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if self._unsubs:
            raise RuntimeError("Repository is already started")
        await self.db.init()
        async with self.db.session() as s:
            for row in (await s.execute(select(KVRow))).scalars():
                self._kv[row.key] = (row.value or {}).get("v")
        self._unsubs = [
            self.bus.subscribe(
                OrderUpdated,
                lambda e: self._pending_orders.__setitem__(e.order.client_order_id, replace(e.order)),
            ),
            self.bus.subscribe(FillReceived, self._on_fill),
            self.bus.subscribe(MarketSettled, self._on_settled),
            self.bus.subscribe(DailyReport, self._on_daily),
            self.bus.subscribe(PortfolioUpdated, self._on_portfolio),
        ]

    async def run(self) -> None:
        self.writer_failed = False
        try:
            while True:
                await asyncio.sleep(self.flush_s)
                await self.flush()
        except Exception:
            self.writer_failed = True
            raise

    async def stop(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        try:
            await self.flush()
        finally:
            await self.db.close()

    # ------------------------------------------------------------------ kv

    def kv_get(self, key: str, default: Any = None) -> Any:
        return deepcopy(self._kv.get(key, default))

    def kv_set(self, key: str, value: Any) -> None:
        self._kv[key] = deepcopy(value)
        self._pending_kv[key] = self._kv[key]

    # ------------------------------------------------------------------ explicit writes

    def audit(self, actor: str, action: str, payload: dict[str, Any]) -> None:
        self._rows.append(
            AuditRow(ts_ms=self.clock.now_ms(), actor=actor, action=action, payload=deepcopy(payload))
        )

    def llm_decision(self, **fields: Any) -> None:
        self._rows.append(LLMDecisionRow(ts_ms=self.clock.now_ms(), **fields))

    # ------------------------------------------------------------------ bus handlers

    def _on_fill(self, e: FillReceived) -> None:
        f = e.fill
        self._rows.append(
            FillRow(
                fill_id=f.fill_id or f"{f.client_order_id}-{f.ts_ms}",
                client_order_id=f.client_order_id,
                ticker=f.ticker,
                side=f.side.value,
                price=f.price,
                qty=f.qty,
                fee=f.fee,
                is_taker=f.is_taker,
                strategy=f.strategy,
                ts_ms=f.ts_ms,
            )
        )

    def _on_settled(self, e: MarketSettled) -> None:
        self._rows.append(
            SettlementRow(ticker=e.ticker, result=e.result, qty=e.qty, pnl=e.pnl_micros, ts_ms=e.ts_ms)
        )

    def _on_daily(self, e: DailyReport) -> None:
        r = e.report
        d = r.to_dict()
        self._rows.append(
            DailyPnLRow(
                day=r.day,
                net_usd=d["net_usd"],
                realized_usd=d["realized_usd"],
                fees_usd=d["fees_usd"],
                unrealized_usd=d["unrealized_usd"],
                end_equity_usd=d["end_equity_usd"],
                trades=r.trades,
                round_trips=r.round_trips,
                wins=r.wins,
                losses=r.losses,
                markets_entered=r.markets_entered,
                report=d,
            )
        )

    def _on_portfolio(self, e: PortfolioUpdated) -> None:
        if e.ts_ms - self._last_equity_ms < self.equity_every_ms:
            return
        self._last_equity_ms = e.ts_ms
        s = e.summary.to_dict()
        self._rows.append(
            EquitySnapshotRow(
                ts_ms=e.ts_ms,
                cash=s["cash"],
                equity=s["equity"],
                unrealized=s["unrealized"],
                exposure=s["exposure"],
                pnl_today=s["pnl_today"],
                n_positions=s["n_positions"],
            )
        )

    # ------------------------------------------------------------------ flush

    async def flush(self) -> None:
        # A kill checkpoint, writer tick and shutdown must never commit out of order. Once a
        # transaction starts, finish it even if its caller is cancelled (e.g. supervisor shutdown).
        async with self._flush_lock:
            task = asyncio.create_task(self._flush_batch())
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    await task
                # Batch restored its buffers and recorded any failure; preserve caller cancellation.
                raise

    async def _flush_batch(self) -> None:
        if not (self._pending_kv or self._pending_orders or self._rows):
            return
        kv, self._pending_kv = self._pending_kv, {}
        orders, self._pending_orders = self._pending_orders, {}
        rows, self._rows = self._rows, []
        self._inflight = len(kv) + len(orders) + len(rows)
        now = self.clock.now_ms()
        try:
            async with self.db.session() as s, s.begin():
                for k, v in kv.items():
                    await s.merge(KVRow(key=k, value={"v": v}, updated_ms=now))
                for o in orders.values():
                    await s.merge(
                        OrderRow(
                            client_order_id=o.client_order_id,
                            exchange_order_id=o.exchange_order_id,
                            ticker=o.ticker,
                            side=o.side.value,
                            price=o.price,
                            qty=o.qty,
                            filled=o.filled,
                            fees=o.fees,
                            status=o.status.value,
                            tif=o.tif.value,
                            post_only=o.post_only,
                            strategy=o.strategy,
                            slot=o.slot,
                            basket_id=o.basket_id,
                            is_entry=o.is_entry,
                            reject_reason=o.reject_reason,
                            created_ms=o.created_ms,
                            updated_ms=o.updated_ms,
                        )
                    )
                for r in rows:
                    await s.merge(r)
            self.written += len(kv) + len(orders) + len(rows)
            self.last_flush_ms = self.clock.now_ms()
            self.last_error = ""
        except (Exception, asyncio.CancelledError) as exc:
            if not isinstance(exc, asyncio.CancelledError):
                self.errors += 1
                # SQLAlchemy errors may embed statements/parameters; health needs only the cause.
                detail = str(exc).splitlines()[0] if str(exc) else ""
                self.last_error = f"{type(exc).__name__}: {detail[:250]}"
                log.exception("db_flush_failed", kv=len(kv), orders=len(orders), rows=len(rows))
            # Put back for retry (kv/orders are latest-wins; rows are appended).
            for k, v in kv.items():
                self._pending_kv.setdefault(k, v)
            for c, o in orders.items():
                self._pending_orders.setdefault(c, o)
            self._rows[:0] = rows
            raise
        finally:
            self._inflight = 0

    # ------------------------------------------------------------------ queries (UI / Discord)

    async def fill_ids(self) -> set[str]:
        """Committed identifiers for deduplicating events against a restored paper checkpoint."""
        async with self.db.session() as s:
            return set((await s.execute(select(FillRow.fill_id))).scalars())

    async def scorecard_rows(
        self,
    ) -> tuple[list[OrderRow], list[FillRow], list[SettlementRow], list[AuditRow]]:
        """Committed history. Do not truncate fills: cost basis needs full history."""
        async with self.db.session() as s, s.begin():
            orders = list((await s.execute(select(OrderRow))).scalars())
            fills = list(
                (await s.execute(select(FillRow).order_by(FillRow.ts_ms, FillRow.fill_id))).scalars()
            )
            settlements = list(
                (
                    await s.execute(select(SettlementRow).order_by(SettlementRow.ts_ms, SettlementRow.id))
                ).scalars()
            )
            context = list(
                (
                    await s.execute(
                        select(AuditRow)
                        .where(
                            AuditRow.action.in_(
                                ["scorecard_fill", "scorecard_markout", "scorecard_settlement"]
                            )
                        )
                        .order_by(AuditRow.id)
                    )
                ).scalars()
            )
        return orders, fills, settlements, context

    async def recent_fills(self, n: int = 100) -> list[FillRow]:
        async with self.db.session() as s:
            return list((await s.execute(select(FillRow).order_by(desc(FillRow.ts_ms)).limit(n))).scalars())

    async def daily_pnl(self, n: int = 30) -> list[DailyPnLRow]:
        async with self.db.session() as s:
            return list(
                (await s.execute(select(DailyPnLRow).order_by(desc(DailyPnLRow.day)).limit(n))).scalars()
            )

    async def equity_history(self, since_ms: int) -> list[EquitySnapshotRow]:
        async with self.db.session() as s:
            q = (
                select(EquitySnapshotRow)
                .where(EquitySnapshotRow.ts_ms >= since_ms)
                .order_by(EquitySnapshotRow.ts_ms)
            )
            return list((await s.execute(q)).scalars())

    async def equity_points(self, since_ms: int) -> list[tuple[int, float]]:
        """(ts_ms, equity) since `since_ms`, oldest first, plus the last point before it (the range baseline)."""
        async with self.db.session() as s:
            prev = (
                await s.execute(
                    select(EquitySnapshotRow.ts_ms, EquitySnapshotRow.equity)
                    .where(EquitySnapshotRow.ts_ms < since_ms)
                    .order_by(desc(EquitySnapshotRow.ts_ms))
                    .limit(1)
                )
            ).first()
            rows = await s.execute(
                select(EquitySnapshotRow.ts_ms, EquitySnapshotRow.equity)
                .where(EquitySnapshotRow.ts_ms >= since_ms)
                .order_by(EquitySnapshotRow.ts_ms)
            )
            out = [(int(t), float(e)) for t, e in rows]
        return ([(int(prev[0]), float(prev[1]))] if prev is not None else []) + out

    async def audit_log(self, n: int = 100) -> list[AuditRow]:
        async with self.db.session() as s:
            return list((await s.execute(select(AuditRow).order_by(desc(AuditRow.ts_ms)).limit(n))).scalars())

    async def llm_decisions(self, n: int = 100) -> list[LLMDecisionRow]:
        async with self.db.session() as s:
            return list(
                (
                    await s.execute(select(LLMDecisionRow).order_by(desc(LLMDecisionRow.ts_ms)).limit(n))
                ).scalars()
            )
