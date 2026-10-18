"""AutoTrader: the orchestration loop  Scanner → LLM gate → Strategy → Risk → OMS.

Conflation design (ARCHITECTURE.md §3): book events only mark markets dirty; every `tick_s` the trader
re-evaluates dirty managed/candidate markets on their *latest* state (and every market at least every
`refresh_s`). Strategies are pure; all side effects go through the OMS (which runs risk checks).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from kst.config import Settings
from kst.core.bus import EventBus
from kst.core.clock import Clock
from kst.core.events import BookUpdated, TradePrinted
from kst.core.fixedpoint import MICROS_PER_DOLLAR, PRICE_SCALE
from kst.core.logging import get_logger
from kst.core.types import EngineState, Mode
from kst.domain.orders import Decision
from kst.engine.control import ControlService
from kst.engine.state import EngineStateStore
from kst.execution.oms import OrderManager
from kst.llm.gate import LLMGate
from kst.llm.packet import Candidate
from kst.marketdata.catalog import MarketCatalog
from kst.marketdata.service import MarketDataService
from kst.portfolio.service import PortfolioService
from kst.pricing.base import Posterior, combine_with_market
from kst.pricing.resolver import ModelResolver
from kst.risk.checks import OrderProposal
from kst.risk.engine import RiskEngine
from kst.scanner.service import ScannerService
from kst.strategies.base import Strategy, StrategyContext
from kst.strategies.router import StrategyRouter
from kst.strategies.structural_arb import StructuralArb

log = get_logger(__name__)

MANAGED_EXEMPT = frozenset({"manual", "external", "flatten"})


@dataclass(slots=True)
class EvalRecord:
    ticker: str
    strategy: str
    ts_ms: int
    note: str
    takes: int
    quotes: int
    gated: str = ""


class AutoTrader:
    def __init__(
        self,
        settings: Settings,
        scanner: ScannerService,
        router: StrategyRouter,
        arb: StructuralArb | None,
        gate: LLMGate,
        oms: OrderManager,
        risk: RiskEngine,
        portfolio: PortfolioService,
        md: MarketDataService,
        catalog: MarketCatalog,
        resolver: ModelResolver,
        state: EngineStateStore,
        control: ControlService,
        bus: EventBus,
        clock: Clock,
    ) -> None:
        self.s = settings
        self.scanner = scanner
        self.router = router
        self.arb = arb
        self.gate = gate
        self.oms = oms
        self.risk = risk
        self.portfolio = portfolio
        self.md = md
        self.catalog = catalog
        self.resolver = resolver
        self.state = state
        self.control = control
        self.bus = bus
        self.clock = clock
        self._dirty: set[str] = set()
        self._last_eval: dict[str, int] = {}
        self._last_take: dict[str, int] = {}
        self.take_cooldown_ms = 2000
        self.refresh_ms = 2000
        self.max_candidates = 30
        self.last_evals: dict[str, EvalRecord] = {}
        self.ticks = 0
        bus.subscribe(BookUpdated, lambda e: self._dirty.add(e.ticker))
        bus.subscribe(TradePrinted, self._on_trade)

    def _on_trade(self, e: TradePrinted) -> None:
        for strat in self.router.by_name.values():
            strat.on_trade(e.ticker, e.price, e.qty, e.taker_side, e.ts_ms)

    # ------------------------------------------------------------------ helpers

    def managed_tickers(self) -> set[str]:
        out = {o.ticker for o in self.oms.live_orders() if o.strategy not in MANAGED_EXEMPT}
        for p in self.portfolio.positions.values():
            if p.qty and (p.strategy not in MANAGED_EXEMPT or self.s.engine.manage_manual_positions):
                out.add(p.ticker)
        return out

    def posterior(self, ticker: str) -> Posterior | None:
        m = self.catalog.get(ticker)
        b = self.md.book(ticker)
        if m is None or b is None:
            return None
        fv = self.resolver.fair_value(m, self.clock.now_ms())
        if fv is None:
            return None
        bb, ba = b.best_bid, b.best_ask
        return combine_with_market(
            fv,
            None if bb is None else bb / PRICE_SCALE,
            None if ba is None else ba / PRICE_SCALE,
            s_floor=self.s.pricing.s_floor,
            s_p_base=self.s.pricing.s_p_base,
        )

    def _strategy_for(self, ticker: str) -> Strategy | None:
        pos = self.portfolio.positions.get(ticker)
        if pos is not None and pos.qty and (s := self.router.get(pos.strategy)) is not None:
            return s  # positions stay with the strategy that opened them
        m = self.catalog.get(ticker)
        return None if m is None else self.router.route(m)

    def _candidate(self, ticker: str, d: Decision, post: Posterior | None) -> Candidate:
        m = self.catalog.get(ticker)
        row = self.scanner.latest.by_ticker.get(ticker) if self.scanner.latest else None
        entries = [t for t in d.takes if not t.reduce_only] + [
            q for q in d.quotes if not q.reduce_only_intent
        ]
        best = max(entries, key=lambda x: x.expected_edge) if entries else None
        b = self.md.book(ticker)
        note = ""
        if post is not None and post.fv is not None:
            note = " ".join(f"{k}={v}" for k, v in list(post.fv.detail.items())[:5])
        elif m is not None:
            note = m.rules_primary[:160]
        assert m is not None
        return Candidate(
            ticker=ticker,
            title=m.short_title(70),
            category=m.category,
            close_s=m.seconds_to_close(self.clock.now_ms()),
            bid=None if b is None else b.best_bid,
            ask=None if b is None else b.best_ask,
            vol24=row.volume_24h if row else m.volume_24h / 100,
            oi=row.open_interest if row else m.open_interest / 100,
            chg24=row.chg_24h if row else None,
            fair=None if post is None else post.q,
            want_side=None if best is None else best.side,
            want_edge=None if best is None else best.expected_edge,
            note=note,
        )

    # ------------------------------------------------------------------ main loop

    async def run(self) -> None:
        while True:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("trader_tick_failed")
            await asyncio.sleep(self.s.engine.tick_s)

    async def tick(self) -> None:
        self.ticks += 1
        now = self.clock.now_ms()
        managed = self.managed_tickers()
        # Positions (incl. manual) always get fresh books for marking/exits.
        self.md.request_focus(
            "positions", managed | {p.ticker for p in self.portfolio.positions.values() if p.qty}
        )
        state = self.state.get()
        if state is EngineState.KILLED or not self.control.autotrade:
            return
        snap = self.scanner.latest
        candidates: list[str] = []
        if snap is not None and state is EngineState.RUNNING:
            for r in snap.rows:
                if r.q is not None and (r.best_edge or -1) > 0:
                    candidates.append(r.ticker)
                if len(candidates) >= self.max_candidates:
                    break
        todo = []
        for t in dict.fromkeys([*managed, *candidates]):
            if t in self._dirty or now - self._last_eval.get(t, 0) >= self.refresh_ms:
                todo.append(t)
        self._dirty.difference_update(todo)
        if todo:
            await asyncio.gather(*(self.evaluate(t, state) for t in todo))
        if snap is not None and state is EngineState.RUNNING and self.arb is not None:
            await self.run_arbs(snap.arbs)

    async def evaluate(self, ticker: str, state: EngineState) -> None:
        now = self.clock.now_ms()
        self._last_eval[ticker] = now
        strat = self._strategy_for(ticker)
        m = self.catalog.get(ticker)
        book = self.md.book(ticker)
        if strat is None or m is None or book is None:
            return
        fresh = self.md.is_fresh(ticker, now)
        if not fresh:
            # Never leave quotes resting on stale data.
            await self.oms.reconcile(strat.name, ticker, [], cancel_others=True)
            return
        post = self.posterior(ticker)
        pos = self.portfolio.positions.get(ticker)
        summary = self.portfolio.summary()
        allow = state is EngineState.RUNNING and self.risk.can_enter(ticker)
        ctx = StrategyContext(
            now_ms=now,
            market=m,
            book=book,
            fees=self.catalog.fees(m),
            posterior=post,
            position=pos if pos is not None and pos.qty else None,
            open_orders=self.oms.live_orders(ticker, strat.name),
            bankroll_usd=summary.equity / MICROS_PER_DOLLAR * self.s.risk.bankroll_fraction,
            allow_entries=allow,
            side_bias=self.gate.side_bias(ticker) if self.gate.cfg.mode == "gate" else None,
            rounding=self.s.fees.rounding_allowance,
            max_order_qty=int(self.risk.limits.max_order_contracts * 100),
            max_order_usd=self.risk.limits.max_order_usd,
            max_position_qty=int(self.risk.limits.max_position_contracts * 100),
            own_in_book=self.s.app.mode in (Mode.LIVE, Mode.DEMO),
        )
        d = strat.evaluate(ctx)
        if now - self._last_take.get(ticker, 0) < self.take_cooldown_ms:
            # Throttle repeat entry takes on the same (possibly not-yet-refreshed) book. Resting quotes are
            # left alone: toggling entries off here would cancel and re-place them every cycle.
            d.takes = [t for t in d.takes if t.reduce_only]
        gated = ""
        has_entry = any(not t.reduce_only for t in d.takes) or any(not q.reduce_only_intent for q in d.quotes)
        if has_entry and strat.name not in self.s.llm.bypass_strategies:
            ok, v = self.gate.decide(self._candidate(ticker, d, post), self.resolver.has_quant_model(m))
            if not ok:
                gated = "awaiting_llm" if v is None else f"llm_skip: {v.reason}"
                d.takes = [t for t in d.takes if t.reduce_only]
                d.quotes = [q for q in d.quotes if q.reduce_only_intent]
        ref = post.q if post is not None else book.mid()
        for t in d.takes:
            self._last_take[ticker] = now
            await self.oms.take(t, strat.name, reference_price=ref)
        if d.manage_quotes:
            await self.oms.reconcile(strat.name, ticker, d.quotes, reference_price=ref)
        self.last_evals[ticker] = EvalRecord(
            ticker, strat.name, now, d.note, len(d.takes), len(d.quotes), gated
        )

    async def run_arbs(self, opps: list[Any]) -> None:
        assert self.arb is not None
        now = self.clock.now_ms()
        for opp in opps[:5]:
            key = f"arb:{opp.event_ticker}:{opp.kind}"
            if now - self._last_take.get(key, 0) < self.take_cooldown_ms * 5:
                continue
            if any(not self.md.is_fresh(leg.ticker, now) for leg in opp.legs):
                continue
            lim = self.risk.limits
            takes = self.arb.plan(
                opp, self.md.book, max_contracts=lim.max_order_contracts, max_leg_usd=lim.max_order_usd
            )
            if not takes:
                continue
            # Pre-check every leg; send nothing unless all legs pass (avoid legging into naked risk).
            checks = [
                self.risk.check(
                    OrderProposal(
                        t.ticker, t.side, t.limit, t.qty, strategy="structural_arb", basket_id=t.basket_id
                    )
                )
                for t in takes
            ]
            self._last_take[key] = now  # cooldown applies to rejected baskets too (no log spam)
            if not all(c.ok for c in checks):
                continue
            log.info(
                "arb_execute",
                event_ticker=opp.event_ticker,
                kind=opp.kind,
                legs=len(takes),
                edge=round(opp.edge, 4),
                qty=takes[0].qty / 100,
            )
            await asyncio.gather(*(self.oms.take(t, "structural_arb") for t in takes))
