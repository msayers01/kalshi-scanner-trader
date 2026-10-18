"""ControlService — the ONE command entry point for the web UI, Discord, CLI, and automatic breakers.

Every command is validated, executed, audited, and published on the bus (so both UIs update at once).
"""

from __future__ import annotations

import asyncio
from typing import Any

from kst.config import Settings
from kst.core.bus import EventBus
from kst.core.clock import Clock
from kst.core.events import Alert, RiskLimitsChanged
from kst.core.fixedpoint import MICROS_PER_DOLLAR, PRICE_SCALE, Price, Qty, fmt_usd
from kst.core.logging import get_logger
from kst.core.types import Actor, BookSide, EngineState, OrderStatus, TimeInForce
from kst.domain.orders import Order, new_client_order_id
from kst.domain.positions import Position
from kst.engine.state import EngineStateStore
from kst.execution.oms import OrderManager
from kst.marketdata.catalog import MarketCatalog
from kst.marketdata.service import MarketDataService
from kst.persistence.repo import Repository
from kst.portfolio.service import PortfolioService
from kst.risk.engine import RiskEngine

log = get_logger(__name__)


class ControlService:
    def __init__(
        self,
        settings: Settings,
        state: EngineStateStore,
        oms: OrderManager,
        risk: RiskEngine,
        portfolio: PortfolioService,
        md: MarketDataService,
        catalog: MarketCatalog,
        bus: EventBus,
        clock: Clock,
        repo: Repository | None = None,
    ) -> None:
        self.settings = settings
        self.state = state
        self.oms = oms
        self.risk = risk
        self.portfolio = portfolio
        self.md = md
        self.catalog = catalog
        self.bus = bus
        self.clock = clock
        self.repo = repo
        self.autotrade = settings.engine.autotrade
        self._lock = asyncio.Lock()
        risk.on_breach = self._on_breach

    def _audit(self, actor: str, action: str, payload: dict[str, Any] | None = None) -> None:
        log.info("control", actor=actor, action=action, **(payload or {}))
        if self.repo is not None:
            self.repo.audit(actor, action, payload or {})

    # ------------------------------------------------------------------ engine state

    async def pause(self, actor: Actor, reason: str = "", who: str = "") -> dict[str, Any]:
        async with self._lock:
            changed = (
                self.state.set(EngineState.PAUSED, actor, reason)
                if self.state.get() is not EngineState.PAUSED
                else False
            )
            n = await self.oms.cancel_where(lambda o: o.is_entry and o.strategy not in ("manual", "flatten"))
        self._audit(f"{actor.value}:{who}", "pause", {"reason": reason, "cancelled": n})
        return {"ok": True, "changed": changed, "state": self.state.get().value, "cancelled_entries": n}

    async def resume(self, actor: Actor, who: str = "") -> dict[str, Any]:
        async with self._lock:
            if self.repo is not None and not self.repo.health()["healthy"]:
                return {"ok": False, "reason": "Persistence is unhealthy; repair it before resuming"}
            changed = self.state.set(EngineState.RUNNING, actor, "resumed")
        self._audit(f"{actor.value}:{who}", "resume")
        return {"ok": True, "changed": changed, "state": self.state.get().value}

    async def kill(
        self, actor: Actor, reason: str = "", *, flatten: bool = False, who: str = ""
    ) -> dict[str, Any]:
        async with self._lock:
            self.state.set(EngineState.KILLED, actor, reason or "kill switch")
            self.autotrade = False
            self._audit(f"{actor.value}:{who}", "kill_intent", {"reason": reason, "flatten": flatten})
            persisted = True
            if self.repo is not None:
                try:
                    await self.repo.flush()  # acknowledge a durable kill before awaiting exchange writes
                except Exception:
                    persisted = False
                    log.exception("kill_checkpoint_failed")
                    self.bus.publish(
                        Alert(
                            self.clock.now_ms(),
                            "critical",
                            "Kill state could not be saved",
                            "Trading is stopped; exchange cancellation will still be attempted. Repair persistence before restart.",
                        )
                    )
            try:
                n = await self.oms.cancel_all()
            except Exception as exc:
                self._audit(f"{actor.value}:{who}", "kill_cancel_failed", {"error": str(exc)})
                self.bus.publish(Alert(self.clock.now_ms(), "critical", "Kill cancellation failed", str(exc)))
                raise
        self.bus.publish(
            Alert(
                self.clock.now_ms(),
                "critical",
                "KILL SWITCH",
                f"by {actor.value}{(':' + who) if who else ''} — {reason or 'manual'}; {n} orders cancelled",
            )
        )
        self._audit(f"{actor.value}:{who}", "kill", {"reason": reason, "flatten": flatten, "cancelled": n})
        out: dict[str, Any] = {
            "ok": persisted,
            "state": self.state.get().value,
            "cancelled": n,
            "persisted": persisted,
        }
        if flatten:
            out["flatten"] = await self.flatten(actor, who=who)
        return out

    async def flatten(self, actor: Actor, who: str = "") -> dict[str, Any]:
        """IOC exits for every position, at most `flatten_max_slippage` through the touch.

        Resting orders in the market are cancelled first (a resting exit plus the flatten could flip the
        position), and exits are sliced to the per-order caps so MaxOrderSize doesn't reject them.
        """
        results: list[dict[str, Any]] = []
        self.md.request_focus("flatten", {p.ticker for p in self.portfolio.positions.values() if p.qty})
        try:
            for pos in list(self.portfolio.positions.values()):
                if pos.qty != 0:
                    results.append(await self._exit_position(pos))
        finally:
            self.md.request_focus("flatten", set())
        self._audit(f"{actor.value}:{who}", "flatten", {"results": results})
        return {"ok": all(r["ok"] for r in results), "results": results}

    async def close_position(self, ticker: str, actor: Actor, who: str = "") -> dict[str, Any]:
        """ "Sell all": exit one position with the same capped IOC slices as flatten (reduce-only, risk-checked).

        Engine state is untouched, so with autotrade on a strategy may enter the market again later.
        """
        pos = self.portfolio.positions.get(ticker)
        if pos is None or pos.qty == 0:
            return {"ok": False, "ticker": ticker, "reason": "no open position"}
        held = abs(pos.qty) / 100
        outcome = "YES" if pos.qty > 0 else "NO"
        self.md.add_focus("close", {ticker})
        try:
            res = await self._exit_position(pos)
        finally:
            self.md.request_focus("close", set())
        self._audit(f"{actor.value}:{who}", "close_position", res)
        sold = res.get("filled", 0.0)
        self.bus.publish(
            Alert(
                self.clock.now_ms(),
                "info" if res["ok"] and not res.get("remaining") else "warning",
                f"Close {ticker}",
                f"sold {sold:g}/{held:g} {outcome} by {actor.value}"
                + (f"; {res['remaining']:g} left: {res['reason']}" if res.get("remaining") else ""),
            )
        )
        return res

    async def _exit_position(self, pos: Position) -> dict[str, Any]:
        """Cancel the market's resting orders, then IOC out of `pos` at most `flatten_max_slippage` through
        the touch, sliced to the per-order caps so MaxOrderSize doesn't reject it."""
        slip = int(self.settings.risk.flatten_max_slippage * PRICE_SCALE)
        await asyncio.gather(*(self.oms.cancel(o) for o in self.oms.live_orders(pos.ticker)))
        m = self.catalog.get(pos.ticker)
        book = self.md.book(pos.ticker)
        if m is None or book is None or not self.md.is_fresh(pos.ticker, self.clock.now_ms()):
            await self._await_book(pos.ticker)
            book = self.md.book(pos.ticker)
        if m is None or book is None:
            return {"ticker": pos.ticker, "ok": False, "reason": "no market/book"}
        if pos.qty > 0:  # sell YES into bids
            side, touch = BookSide.ASK, book.best_bid
            limit = None if touch is None else m.grid.snap_up(max(touch - slip, m.grid.min_price))
        else:  # buy YES from asks
            side, touch = BookSide.BID, book.best_ask
            limit = None if touch is None else m.grid.snap_down(min(touch + slip, m.grid.max_price))
        if limit is None or touch is None:
            return {"ticker": pos.ticker, "ok": False, "reason": "no liquidity"}
        lim = self.risk.limits
        per = max(limit if side is BookSide.BID else PRICE_SCALE - limit, 1)  # Price × Qty = micros
        cap = min(
            int(lim.max_order_contracts * 100),
            int(lim.max_order_usd * MICROS_PER_DOLLAR) // per,
        )
        cap = cap // 100 * 100 or cap  # whole contracts unless the caps allow less than one
        todo = abs(pos.qty)
        filled = 0
        res = None
        reason = ""
        # Fixed total (never more than the position we started with), sent as capped IOC slices.
        while todo > 0 and cap > 0:
            o = Order(
                new_client_order_id("flatten"),
                pos.ticker,
                side,
                Price(limit),
                Qty(min(todo, cap)),
                tif=TimeInForce.IOC,
                reduce_only=True,
                strategy="flatten",
            )
            todo -= o.qty
            # Fat-finger is measured from the touch: the slippage band is the operator's choice.
            res = await self.oms.submit(o, reference_price=touch / PRICE_SCALE)
            filled += o.filled
            reason = res.detail or o.reject_reason
            if not res.ok or (o.status.is_terminal and o.filled < o.qty):
                break  # rejected or out of liquidity inside the band: report, don't chase
        return {
            "ticker": pos.ticker,
            "ok": res is not None and res.ok,
            "filled": filled / 100,
            "remaining": abs(pos.qty) / 100,
            "reason": reason if res is not None else "order caps too small",
        }

    async def _await_book(self, ticker: str, timeout_s: float = 4.0) -> bool:
        self.md.add_focus("ui", {ticker})
        deadline = self.clock.now_ms() + int(timeout_s * 1000)
        while self.clock.now_ms() < deadline:
            if self.md.is_fresh(ticker, self.clock.now_ms()):
                return True
            await asyncio.sleep(0.2)
        return False

    async def _on_breach(self, action: str, reason: str) -> None:
        if action == "kill":
            await self.kill(Actor.SYSTEM, reason)
        elif self.state.get() is EngineState.RUNNING:
            await self.pause(Actor.SYSTEM, reason)

    async def on_critical_task_failure(self, task: str, error: str) -> None:
        await self.kill(Actor.SYSTEM, f"critical task {task} failed: {error}")

    # ------------------------------------------------------------------ configuration

    async def set_autotrade(self, on: bool, actor: Actor, who: str = "") -> dict[str, Any]:
        self.autotrade = on
        n = 0
        if not on:
            n = await self.oms.cancel_where(lambda o: o.strategy not in ("manual", "flatten"))
        self._audit(f"{actor.value}:{who}", "autotrade", {"on": on, "cancelled": n})
        self.bus.publish(
            Alert(self.clock.now_ms(), "info", f"Autotrade {'ON' if on else 'OFF'}", f"by {actor.value}")
        )
        return {"ok": True, "autotrade": on}

    async def set_limits(self, changes: dict[str, Any], actor: Actor, who: str = "") -> dict[str, Any]:
        new, diff = self.risk.limits.patch(changes)
        self.risk.limits = new
        if self.repo is not None:
            self.repo.kv_set("risk_limits_override", new.model_dump())
        self._audit(f"{actor.value}:{who}", "set_limits", diff)
        if diff:
            self.bus.publish(RiskLimitsChanged(self.clock.now_ms(), diff, actor))
        return {"ok": True, "changed": diff, "limits": new.model_dump()}

    # ------------------------------------------------------------------ manual trading

    async def manual_order(
        self,
        ticker: str,
        side: BookSide,
        price: float,
        contracts: float,
        *,
        tif: TimeInForce = TimeInForce.GTC,
        post_only: bool = False,
        actor: Actor = Actor.WEB,
        who: str = "",
    ) -> dict[str, Any]:
        m = self.catalog.get(ticker) or await self.catalog.ensure_market(self.md.rest, ticker)
        if m is None:
            return {"ok": False, "reason": f"unknown market {ticker}"}
        p = (
            m.grid.snap_down(round(price * PRICE_SCALE))
            if side is BookSide.BID
            else m.grid.snap_up(round(price * PRICE_SCALE))
        )
        if p is None:
            return {"ok": False, "reason": "price off grid"}
        q = Qty(round(contracts * 100))
        await self._await_book(ticker)
        o = Order(
            new_client_order_id("manual"), ticker, side, p, q, tif=tif, post_only=post_only, strategy="manual"
        )
        res = await self.oms.submit(o, manual=True)
        self._audit(
            f"{actor.value}:{who}",
            "manual_order",
            {
                "ticker": ticker,
                "side": side.value,
                "price": p / PRICE_SCALE,
                "qty": contracts,
                "tif": tif.value,
                "ok": res.ok,
                "check": res.check,
            },
        )
        return {
            "ok": res.ok and o.status is not OrderStatus.REJECTED,
            "client_order_id": o.client_order_id,
            "status": o.status.value,
            "filled": o.filled / 100,
            "reason": o.reject_reason or res.detail,
        }

    async def cancel_order(self, client_order_id: str, actor: Actor, who: str = "") -> dict[str, Any]:
        o = self.oms.orders.get(client_order_id)
        if o is None or not o.is_live:
            return {"ok": False, "reason": "not live"}
        await self.oms.cancel(o)
        self._audit(f"{actor.value}:{who}", "cancel_order", {"coid": client_order_id})
        return {"ok": True, "status": o.status.value}

    async def cancel_all_orders(self, actor: Actor, who: str = "") -> dict[str, Any]:
        n = await self.oms.cancel_all()
        self._audit(f"{actor.value}:{who}", "cancel_all", {"n": n})
        return {"ok": True, "cancelled": n}

    # ------------------------------------------------------------------ status

    def status(self) -> dict[str, Any]:
        s = self.portfolio.summary()
        return {
            "mode": self.settings.app.mode.value,
            "state": self.state.get().value,
            "state_reason": self.state.reason,
            "state_since_ms": self.state.since_ms,
            "autotrade": self.autotrade,
            "live_armed": self.risk.live_armed,
            "equity": s.equity / 1e6,
            "cash": s.cash / 1e6,
            "pnl_today": s.pnl_today / 1e6,
            "pnl_today_fmt": fmt_usd(s.pnl_today),
            "positions": s.n_positions,
            "open_orders": len(self.oms.live_orders()),
            "markets_entered_today": len(self.risk.entered_today),
            "max_markets_per_day": self.risk.limits.max_markets_per_day,
            "md_connected": self.md.connected,
        }
