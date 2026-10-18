"""RiskEngine: builds RiskContext from live state, runs the check chain, tracks daily counters, and trips
breakers (daily loss → PAUSE, drawdown → KILL, consecutive losses → PAUSE, reject storms → PAUSE)."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Coroutine
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from kst.config import RiskCfg
from kst.core.bus import EventBus
from kst.core.clock import Clock, trading_day
from kst.core.events import Alert, RiskRejected
from kst.core.fixedpoint import MICROS_PER_DOLLAR, Price, Qty
from kst.core.logging import get_logger
from kst.core.types import BookSide, EngineState, Mode
from kst.domain.views import BookView, CatalogView
from kst.risk.checks import CheckResult, OrderProposal, RiskContext, run_checks
from kst.risk.limits import RiskLimits

if TYPE_CHECKING:
    from kst.execution.oms import OrderManager
    from kst.portfolio.service import PortfolioService, PortfolioSummary

log = get_logger(__name__)

BreachHandler = Callable[[str, str], Coroutine[Any, Any, None]]  # (action "pause"|"kill", reason)


class RiskEngine:
    def __init__(
        self,
        cfg: RiskCfg,
        catalog: CatalogView,
        md: BookView,
        bus: EventBus,
        clock: Clock,
        *,
        mode: Mode,
        live_armed: bool,
        trading_day_tz: str,
        allowed_categories: list[str] | None = None,
        state_getter: Callable[[], EngineState] = lambda: EngineState.RUNNING,
        persist: Callable[[str, Any], None] | None = None,
    ) -> None:
        self.cfg = cfg
        self.limits: RiskLimits = cfg.limits
        self.catalog = catalog
        self.md = md
        self.bus = bus
        self.clock = clock
        self.mode = mode
        self.live_armed = live_armed
        self.tz = trading_day_tz
        self.allowed_categories = frozenset(allowed_categories or [])
        self.state_getter = state_getter
        self.persist = persist
        self.oms: OrderManager | None = None
        self.portfolio: PortfolioService | None = None
        self.on_breach: BreachHandler | None = None
        self._order_ts: deque[int] = deque()
        self._reject_ts: deque[int] = deque()
        self.day = trading_day(clock.now_ms(), trading_day_tz).isoformat()
        self.entered_today: set[str] = set()
        self._reservations: dict[str, set[str]] = {}
        self._basket_tickers: set[str] = set()
        self.consecutive_losses = 0
        self._tripped: set[str] = set()  # breakers already fired today
        self.rejections: dict[str, int] = {}
        self._breach_tasks: set[asyncio.Task[None]] = set()  # strong refs: an unreferenced task can be GC'd

    def bind(self, oms: OrderManager, portfolio: PortfolioService) -> None:
        self.oms = oms
        self.portfolio = portfolio

    # ------------------------------------------------------------------ persistence of counters

    def restore(self, state: dict[str, Any]) -> None:
        if state.get("day") == self.day:
            self.entered_today = set(state.get("entered", []))
            self.consecutive_losses = int(state.get("consecutive_losses", 0))
            self._tripped = set(state.get("tripped", []))

    def _save(self) -> None:
        if self.persist:
            self.persist(
                "risk_counters",
                {
                    "day": self.day,
                    "entered": sorted(self.entered_today),
                    "consecutive_losses": self.consecutive_losses,
                    "tripped": sorted(self._tripped),
                },
            )

    def roll_day_if_needed(self) -> bool:
        d = trading_day(self.clock.now_ms(), self.tz).isoformat()
        if d == self.day:
            return False
        self.day = d
        self.entered_today.clear()
        self._reservations.clear()
        self.consecutive_losses = 0
        self._tripped.clear()
        self._save()
        return True

    # ------------------------------------------------------------------ context

    def _in_blackout(self, category: str, now_ms: int) -> bool:
        for w in self.cfg.blackout_windows:
            try:
                s = int(
                    datetime.fromisoformat(w.start_utc.replace("Z", "+00:00")).astimezone(UTC).timestamp()
                    * 1000
                )
                e = int(
                    datetime.fromisoformat(w.end_utc.replace("Z", "+00:00")).astimezone(UTC).timestamp()
                    * 1000
                )
            except ValueError:
                continue
            if s <= now_ms <= e and (not w.categories or category in w.categories):
                return True
        return False

    def build_context(self, p: OrderProposal) -> RiskContext:
        now = self.clock.now_ms()
        m = self.catalog.get(p.ticker)
        category = m.category if m else ""
        event = m.event_ticker if m else ""
        pf, oms = self.portfolio, self.oms
        pos_qty = 0
        exposures: dict[str, int] = {}
        if pf is not None:
            for t, pos in pf.positions.items():
                if pos.qty:
                    exposures[t] = exposures.get(t, 0) + pos.exposure
            here = pf.positions.get(p.ticker)
            pos_qty = here.qty if here else 0
        resting_open_qty = 0
        resting_close_qty = 0
        live_here = []
        if oms is not None:
            for o in oms.live_orders():
                if p.replaces and o.client_order_id == p.replaces:
                    continue  # being amended/replaced by this proposal
                pq = pf.positions[o.ticker].qty if pf is not None and o.ticker in pf.positions else 0
                prop = OrderProposal(o.ticker, o.side, o.price, o.remaining)
                add = prop.added_exposure_micros(pq)
                if add:
                    exposures[o.ticker] = exposures.get(o.ticker, 0) + add
                if o.ticker == p.ticker:
                    live_here.append(o)
                    resting_open_qty += prop.opening_qty(pq)
                    resting_close_qty += o.remaining - prop.opening_qty(pq)
        ev_exp = cat_exp = 0
        for t, e in exposures.items():
            mk = self.catalog.get(t)
            if mk is not None and event and mk.event_ticker == event:
                ev_exp += e
            if mk is not None and category and mk.category == category:
                cat_exp += e
        now_minute = now - 60_000
        while self._order_ts and self._order_ts[0] < now_minute:
            self._order_ts.popleft()
        summary = pf.summary() if pf is not None else None
        n_open = sum(1 for pos in pf.positions.values() if pos.qty) if pf is not None else 0
        return RiskContext(
            now_ms=now,
            state=self.state_getter(),
            mode=self.mode,
            live_armed=self.live_armed,
            limits=self.limits,
            market=m,
            book_fresh=self.md.is_fresh(p.ticker, now),
            category=category,
            pos_qty=pos_qty,
            resting_close_qty=resting_close_qty,
            market_exposure=exposures.get(p.ticker, 0),
            market_resting_opening_qty=resting_open_qty,
            event_exposure=ev_exp,
            category_exposure=cat_exp,
            total_exposure=sum(exposures.values()),
            equity=summary.equity if summary else 0,
            cash=summary.cash if summary else 0,
            cash_buffer=int(self.cfg.cash_buffer_usd * MICROS_PER_DOLLAR),
            open_positions=n_open,
            has_position=pos_qty != 0,
            markets_entered_today=frozenset(self.entered_today | set(self._reservations)),
            orders_last_minute=len(self._order_ts),
            blocked_series=frozenset(self.cfg.blocked_series),
            allowed_categories=self.allowed_categories,
            in_blackout=self._in_blackout(category, now),
            book_mid=self._book_mid(p.ticker, now),
            own_live_orders=live_here,
        )

    def _book_mid(self, ticker: str, now_ms: int) -> float | None:
        b = self.md.book(ticker)
        return b.mid() if b is not None and self.md.is_fresh(ticker, now_ms) else None

    # ------------------------------------------------------------------ check API

    def check(self, p: OrderProposal) -> CheckResult:
        self.roll_day_if_needed()
        ctx = self.build_context(p)
        res = run_checks(p, ctx)
        if not res.ok:
            self.rejections[res.check] = self.rejections.get(res.check, 0) + 1
            self.bus.publish(RiskRejected(ctx.now_ms, p.ticker, res.check, res.detail, p.strategy))
            log.info(
                "risk_rejected", ticker=p.ticker, check=res.check, detail=res.detail, strategy=p.strategy
            )
        return res

    def can_enter(self, ticker: str) -> bool:
        """Cheap pre-filter for the trader (full checks still run per order)."""
        entered = self.entered_today | set(self._reservations)
        return ticker in entered or len(entered) < self.limits.max_markets_per_day

    def is_risk_reducing(self, ticker: str, side: BookSide, qty: int) -> bool:
        pf = self.portfolio
        pq = pf.positions[ticker].qty if pf is not None and ticker in pf.positions else 0
        return OrderProposal(ticker, side, Price(1), Qty(qty)).is_risk_reducing(pq)

    # ------------------------------------------------------------------ order lifecycle hooks (from OMS)

    def on_order_sent(self, ticker: str, coid: str, is_entry: bool, basket: bool = False) -> None:
        self._order_ts.append(self.clock.now_ms())
        if basket:
            self._basket_tickers.add(ticker)
            return
        if is_entry and ticker not in self.entered_today:
            self._reservations.setdefault(ticker, set()).add(coid)

    def on_entry_fill(self, ticker: str) -> None:
        if ticker in self._basket_tickers and ticker not in self._reservations:
            return
        if ticker not in self.entered_today:
            self.entered_today.add(ticker)
            self._reservations.pop(ticker, None)
            self._save()

    def on_order_done(self, ticker: str, coid: str, filled: int) -> None:
        res = self._reservations.get(ticker)
        if res is not None:
            res.discard(coid)
            if not res and filled == 0:
                self._reservations.pop(ticker, None)

    def on_exchange_reject(self) -> None:
        now = self.clock.now_ms()
        self._reject_ts.append(now)
        while self._reject_ts and self._reject_ts[0] < now - self.cfg.reject_storm_window_s * 1000:
            self._reject_ts.popleft()
        if len(self._reject_ts) >= self.cfg.reject_storm_count:
            self._trip(
                "reject_storm",
                "pause",
                f"{len(self._reject_ts)} exchange rejects in {self.cfg.reject_storm_window_s:.0f}s",
            )

    def on_round_trip(self, pnl_micros: int) -> None:
        self.consecutive_losses = self.consecutive_losses + 1 if pnl_micros < 0 else 0
        self._save()
        if self.consecutive_losses >= self.limits.max_consecutive_losses:
            self._trip(
                "consecutive_losses", "pause", f"{self.consecutive_losses} losing round trips in a row"
            )

    # ------------------------------------------------------------------ breakers

    def on_portfolio(self, s: PortfolioSummary) -> None:
        self.roll_day_if_needed()
        pnl_today = (s.equity - s.day_start_equity) / MICROS_PER_DOLLAR
        dd = (s.peak_equity - s.equity) / MICROS_PER_DOLLAR
        if pnl_today <= -self.limits.daily_loss_limit_usd:
            self._trip(
                "daily_loss", "pause", f"day P&L ${pnl_today:.2f} ≤ −${self.limits.daily_loss_limit_usd}"
            )
        if dd >= self.limits.max_drawdown_usd:
            self._trip("drawdown", "kill", f"drawdown ${dd:.2f} ≥ ${self.limits.max_drawdown_usd}")

    def _trip(self, name: str, action: str, reason: str) -> None:
        if name in self._tripped:
            return
        self._tripped.add(name)
        self._save()
        log.warning("risk_breaker", breaker=name, action=action, reason=reason)
        self.bus.publish(
            Alert(self.clock.now_ms(), "critical", f"Risk breaker: {name} → {action.upper()}", reason)
        )
        if self.on_breach is not None:
            task = asyncio.get_running_loop().create_task(self.on_breach(action, f"{name}: {reason}"))
            self._breach_tasks.add(task)
            task.add_done_callback(self._breach_tasks.discard)

    def reset_breaker(self, name: str | None = None) -> None:
        if name is None:
            self._tripped.clear()
        else:
            self._tripped.discard(name)
        self._save()

    def exposure_summary(self) -> dict[str, float]:
        ctx = self.build_context(OrderProposal("__none__", BookSide.BID, Price(1), Qty(0)))
        return {
            "total_usd": ctx.total_exposure / MICROS_PER_DOLLAR,
            "orders_last_minute": float(ctx.orders_last_minute),
        }
