"""PortfolioService: positions, cash, equity, realized/unrealized P&L, daily stats and reports.

Consumes FillReceived and MarketStatusChanged from the bus. In paper mode its state (cash + positions) is
persisted to kv_state so P&L continues across restarts. In live/demo mode `sync_from_exchange` adopts
the exchange's balance and positions as the source of truth.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from kst.core.bus import EventBus
from kst.core.clock import Clock, trading_day
from kst.core.events import (
    DailyReport,
    FillReceived,
    MarketSettled,
    MarketStatusChanged,
    PortfolioUpdated,
    PositionChanged,
)
from kst.core.fixedpoint import MICROS_PER_DOLLAR, PRICE_SCALE, Micros, Price, Qty
from kst.core.logging import get_logger
from kst.core.types import MarketStatus, Mode
from kst.domain.orders import Fill
from kst.domain.positions import Position, settlement_price_for
from kst.domain.views import BookView, CatalogView
from kst.portfolio.reports import DailyPnL

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class PortfolioSummary:
    ts_ms: int
    mode: str
    cash: int
    equity: int
    unrealized: int
    exposure: int
    realized_today: int
    fees_today: int
    trades_today: int
    round_trips_today: int
    wins_today: int
    losses_today: int
    n_positions: int
    day: str
    day_start_equity: int
    peak_equity: int
    by_strategy: dict[str, dict[str, float]] = field(default_factory=dict)

    @property
    def pnl_today(self) -> int:
        return self.equity - self.day_start_equity

    def to_dict(self) -> dict[str, Any]:
        u = MICROS_PER_DOLLAR
        return {
            "ts_ms": self.ts_ms,
            "mode": self.mode,
            "cash": self.cash / u,
            "equity": self.equity / u,
            "unrealized": self.unrealized / u,
            "exposure": self.exposure / u,
            "realized_today": self.realized_today / u,
            "fees_today": self.fees_today / u,
            "pnl_today": self.pnl_today / u,
            "trades_today": self.trades_today,
            "round_trips_today": self.round_trips_today,
            "wins_today": self.wins_today,
            "losses_today": self.losses_today,
            "n_positions": self.n_positions,
            "day": self.day,
            "day_start_equity": self.day_start_equity / u,
            "peak_equity": self.peak_equity / u,
            "by_strategy": self.by_strategy,
        }


@dataclass(slots=True)
class _Day:
    day: str
    start_equity: int
    peak_equity: int
    realized: int = 0
    fees: int = 0
    trades: int = 0
    round_trips: int = 0
    wins: int = 0
    losses: int = 0
    by_strategy: dict[str, dict[str, float]] = field(default_factory=dict)


class PortfolioService:
    def __init__(
        self,
        mode: Mode,
        catalog: CatalogView,
        md: BookView,
        bus: EventBus,
        clock: Clock,
        *,
        trading_day_tz: str,
        starting_cash_usd: float,
        persist: Callable[[str, Any], None] | None = None,
        on_round_trip: Callable[[int], None] | None = None,
        on_entry_fill: Callable[[str], None] | None = None,
        markets_entered: Callable[[], int] = lambda: 0,
    ) -> None:
        self.mode = mode
        self.catalog = catalog
        self.md = md
        self.bus = bus
        self.clock = clock
        self.tz = trading_day_tz
        self.persist = persist
        self.on_round_trip = on_round_trip
        self.on_entry_fill = on_entry_fill
        self.markets_entered = markets_entered
        self.positions: dict[str, Position] = {}
        self.cash = int(starting_cash_usd * MICROS_PER_DOLLAR)
        self._seen_fills: set[str] = set()
        self._sync_mismatch: dict[str, tuple[int, int]] = {}  # ticker → (local qty, exchange qty) last sync
        today = trading_day(clock.now_ms(), trading_day_tz).isoformat()
        self._day = _Day(today, self.cash, self.cash)
        self._dirty = True
        self.last_report: DailyPnL | None = None
        bus.subscribe(FillReceived, lambda e: self.apply_fill(e.fill))
        bus.subscribe(MarketStatusChanged, self._on_status)

    # ------------------------------------------------------------------ state persistence (paper)

    def restore_seen_fills(self, fill_ids: set[str]) -> None:
        self._seen_fills.update(fill_ids)

    def state(self) -> dict[str, Any]:
        return {
            "cash": self.cash,
            "positions": [
                {
                    "ticker": p.ticker,
                    "event": p.event_ticker,
                    "category": p.category,
                    "qty": p.qty,
                    "open_cost": p.open_cost,
                    "realized": p.realized,
                    "fees": p.fees,
                    "strategy": p.strategy,
                    "opened_ms": p.opened_ms,
                }
                for p in self.positions.values()
                if p.qty
            ],
            "day": {
                "day": self._day.day,
                "start_equity": self._day.start_equity,
                "peak_equity": self._day.peak_equity,
                "realized": self._day.realized,
                "fees": self._day.fees,
                "trades": self._day.trades,
                "round_trips": self._day.round_trips,
                "wins": self._day.wins,
                "losses": self._day.losses,
                "by_strategy": self._day.by_strategy,
            },
        }

    def restore(self, st: dict[str, Any]) -> None:
        self.cash = int(st.get("cash", self.cash))
        for r in st.get("positions", []):
            self.positions[r["ticker"]] = Position(
                ticker=r["ticker"],
                event_ticker=r.get("event", ""),
                category=r.get("category", ""),
                qty=Qty(r["qty"]),
                open_cost=Micros(r["open_cost"]),
                realized=Micros(r.get("realized", 0)),
                fees=Micros(r.get("fees", 0)),
                strategy=r.get("strategy", "manual"),
                opened_ms=r.get("opened_ms", 0),
            )
        d = st.get("day") or {}
        if d.get("day") == self._day.day:
            self._day = _Day(
                **{
                    k: d[k]
                    for k in (
                        "day",
                        "start_equity",
                        "peak_equity",
                        "realized",
                        "fees",
                        "trades",
                        "round_trips",
                        "wins",
                        "losses",
                    )
                },
                by_strategy=d.get("by_strategy", {}),
            )
        else:
            eq = self.equity()
            self._day = _Day(self._day.day, eq, eq)
        log.info("portfolio_restored", cash=self.cash / MICROS_PER_DOLLAR, positions=len(self.positions))

    def _save(self) -> None:
        self._dirty = True
        if self.persist is not None and self.mode in (Mode.PAPER, Mode.REPLAY):
            self.persist("portfolio", self.state())

    # ------------------------------------------------------------------ marks & equity

    def mark(self, ticker: str) -> Price | None:
        now = self.clock.now_ms()
        b = self.md.book(ticker)
        if b is not None and self.md.is_fresh(ticker, now) and (mid := b.mid()) is not None:
            return Price(round(mid * PRICE_SCALE))
        m = self.catalog.get(ticker)
        if m is not None:
            if m.mid is not None:
                return Price(round(m.mid * PRICE_SCALE))
            if m.last_price is not None:
                return m.last_price
        return None

    def unrealized(self) -> int:
        tot = 0
        for p in self.positions.values():
            if p.qty:
                mk = self.mark(p.ticker)
                tot += p.unrealized(mk) if mk is not None else 0
        return tot

    def exposure(self) -> int:
        return sum(p.exposure for p in self.positions.values() if p.qty)

    def equity(self) -> int:
        return self.cash + self.exposure() + self.unrealized()

    # ------------------------------------------------------------------ events

    def apply_fill(self, f: Fill) -> None:
        if f.fill_id and f.fill_id in self._seen_fills:
            return
        if f.fill_id:
            self._seen_fills.add(f.fill_id)
        self.roll_day_if_needed()
        pos = self.positions.get(f.ticker)
        if pos is None:
            m = self.catalog.get(f.ticker)
            pos = Position(
                f.ticker,
                event_ticker=m.event_ticker if m else "",
                category=m.category if m else "",
                strategy=f.strategy,
            )
            self.positions[f.ticker] = pos
        was_flat = pos.qty == 0
        if was_flat:
            pos.strategy = f.strategy
        eff = pos.apply_fill(f.side, f.price, f.qty, f.fee, f.ts_ms)
        self.cash += eff.cash_delta
        d = self._day
        d.realized += eff.realized
        d.fees += f.fee
        d.trades += 1
        s = d.by_strategy.setdefault(
            pos.strategy or f.strategy, {"net_usd": 0.0, "fees_usd": 0.0, "trades": 0}
        )
        s["net_usd"] += (eff.realized - f.fee) / MICROS_PER_DOLLAR
        s["fees_usd"] += f.fee / MICROS_PER_DOLLAR
        s["trades"] += 1
        if eff.opened and self.on_entry_fill is not None:
            self.on_entry_fill(f.ticker)
        if eff.round_trip_done:
            self._record_round_trip(eff.round_trip_pnl)
        log.info(
            "fill_applied",
            ticker=f.ticker,
            side=f.side.value,
            price=f.price / PRICE_SCALE,
            qty=f.qty / 100,
            fee=f.fee / MICROS_PER_DOLLAR,
            pos=pos.qty / 100,
            strategy=f.strategy,
            taker=f.is_taker,
        )
        self.bus.publish(PositionChanged(f.ts_ms, pos))
        self._save()

    def _record_round_trip(self, pnl: int) -> None:
        d = self._day
        d.round_trips += 1
        if pnl >= 0:
            d.wins += 1
        else:
            d.losses += 1
        if self.on_round_trip is not None:
            self.on_round_trip(pnl)

    def _on_status(self, e: MarketStatusChanged) -> None:
        if e.status not in (MarketStatus.DETERMINED, MarketStatus.FINALIZED):
            return
        pos = self.positions.get(e.ticker)
        if pos is None or pos.qty == 0 or self.mode.uses_exchange_orders:
            return  # live/demo: settlements come from the exchange sync
        sp = settlement_price_for(e.result, e.settlement_value)
        if sp is None:
            return
        self.settle(e.ticker, sp, e.result)

    def settle(self, ticker: str, settlement_price: int, result: str) -> None:
        pos = self.positions.get(ticker)
        if pos is None or pos.qty == 0:
            return
        qty = pos.qty
        eff = pos.settle(settlement_price, self.clock.now_ms())
        self.cash += eff.cash_delta
        self._day.realized += eff.realized
        s = self._day.by_strategy.setdefault(pos.strategy, {"net_usd": 0.0, "fees_usd": 0.0, "trades": 0})
        s["net_usd"] += eff.realized / MICROS_PER_DOLLAR
        self._record_round_trip(eff.round_trip_pnl)
        log.info(
            "position_settled",
            ticker=ticker,
            result=result,
            qty=qty / 100,
            pnl=eff.realized / MICROS_PER_DOLLAR,
        )
        self.bus.publish(
            MarketSettled(self.clock.now_ms(), ticker, result, qty, eff.realized, settlement_price)
        )
        self.bus.publish(PositionChanged(self.clock.now_ms(), pos))
        self._save()

    # ------------------------------------------------------------------ live sync

    def sync_from_exchange(
        self, balance_cents: int | None, positions: list[dict[str, Any]], *, confirm: bool = True
    ) -> None:
        """Adopt the exchange's balance and positions. With `confirm`, a position mismatch is adopted only
        when the previous sync saw the same one: a fill can reach the positions endpoint before our WS
        fill (or the reverse), and adopting it then would count that fill twice."""
        if balance_cents is not None:
            self.cash = balance_cents * 10_000
        seen = set()
        mismatches: dict[str, tuple[int, int]] = {}

        def confirmed(t: str, local: int, exchange: int) -> bool:
            mismatches[t] = (local, exchange)
            return not confirm or self._sync_mismatch.get(t) == (local, exchange)

        for row in positions:
            t, qty = row["ticker"], int(row["qty"])
            seen.add(t)
            pos = self.positions.get(t)
            local = pos.qty if pos is not None else 0
            if local != qty and not confirmed(t, local, qty):
                continue
            if pos is None:
                m = self.catalog.get(t)
                pos = Position(
                    t,
                    event_ticker=m.event_ticker if m else "",
                    category=m.category if m else "",
                    strategy="external",
                )
                self.positions[t] = pos
            if pos.qty != qty:
                log.warning("position_mismatch_adopted", ticker=t, local=pos.qty / 100, exchange=qty / 100)
                pos.qty = Qty(qty)
                # Exchange reports cost of the aggregate position (YES cost for YES, NO cost for NO).
                exp = int(row.get("exposure", 0))
                pos.open_cost = Micros(exp if qty >= 0 else PRICE_SCALE * -qty - exp)
        for t, pos in self.positions.items():
            if t not in seen and pos.qty != 0 and confirmed(t, pos.qty, 0):
                log.warning("position_closed_on_exchange", ticker=t, local=pos.qty / 100)
                pos.qty, pos.open_cost = Qty(0), Micros(0)
        self._sync_mismatch = mismatches
        self._dirty = True

    # ------------------------------------------------------------------ summaries / day roll

    def summary(self) -> PortfolioSummary:
        eq = self.equity()
        d = self._day
        if eq > d.peak_equity:
            d.peak_equity = eq
        return PortfolioSummary(
            ts_ms=self.clock.now_ms(),
            mode=self.mode.value,
            cash=self.cash,
            equity=eq,
            unrealized=self.unrealized(),
            exposure=self.exposure(),
            realized_today=d.realized,
            fees_today=d.fees,
            trades_today=d.trades,
            round_trips_today=d.round_trips,
            wins_today=d.wins,
            losses_today=d.losses,
            n_positions=sum(1 for p in self.positions.values() if p.qty),
            day=d.day,
            day_start_equity=d.start_equity,
            peak_equity=d.peak_equity,
            by_strategy={k: dict(v) for k, v in d.by_strategy.items()},
        )

    def daily_report(self) -> DailyPnL:
        s = self.summary()
        d = self._day
        return DailyPnL(
            day=d.day,
            mode=self.mode.value,
            start_equity=d.start_equity,
            end_equity=s.equity,
            realized=d.realized,
            fees=d.fees,
            unrealized_end=s.unrealized,
            trades=d.trades,
            round_trips=d.round_trips,
            wins=d.wins,
            losses=d.losses,
            markets_entered=self.markets_entered(),
            peak_equity=max(d.peak_equity, s.equity),
            by_strategy={k: dict(v) for k, v in d.by_strategy.items()},
            generated_ms=self.clock.now_ms(),
        )

    def roll_day_if_needed(self) -> DailyPnL | None:
        today = trading_day(self.clock.now_ms(), self.tz).isoformat()
        if today == self._day.day:
            return None
        report = self.daily_report()
        self.last_report = report
        eq = self.equity()
        self._day = _Day(today, eq, eq)
        self.bus.publish(DailyReport(self.clock.now_ms(), report))
        log.info("daily_report", **{k: v for k, v in report.to_dict().items() if k not in ("by_strategy",)})
        self._save()
        return report

    async def run(self, tick_ms: int = 1000, heartbeat_ms: int = 1000) -> None:
        """Re-mark every `tick_ms`; publish PortfolioUpdated when equity or cash moved, or every
        `heartbeat_ms` regardless (risk monitors and the UI rely on a steady beat)."""
        last: tuple[int, int] | None = None
        last_ms = 0
        while True:
            self.roll_day_if_needed()
            s = self.summary()
            key = (s.equity, s.cash)
            if key != last or s.ts_ms - last_ms >= heartbeat_ms:
                self.bus.publish(PortfolioUpdated(s.ts_ms, s))
                last, last_ms = key, s.ts_ms
            await asyncio.sleep(tick_ms / 1000)
