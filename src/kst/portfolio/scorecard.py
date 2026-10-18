"""Lifetime paper execution scorecard. Money accumulates in integer micros.

FIFO lots attribute closing/flatten fills to the strategy that opened the exposure.
Execution context uses the existing audit journal, leaving historical schemas intact.
Missing context is never inferred from today's model or prices.
"""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kst.core.events import FillReceived, MarketSettled, OrderUpdated, PortfolioUpdated
from kst.core.types import BookSide, Mode
from kst.persistence.repo import Repository

if TYPE_CHECKING:
    from kst.app import Application


class ScorecardRecorder:
    """Record execution context and a fresh midpoint 60–90 seconds after each fill."""

    def __init__(self, app: Application) -> None:
        self.app = app
        self.decisions: dict[str, dict[str, Any]] = {}
        self.pending: dict[str, dict[str, Any]] = {}
        self.seen_fills: set[str] = set()
        self.enabled = app.mode in (Mode.PAPER, Mode.REPLAY)
        if self.enabled:
            app.bus.subscribe(OrderUpdated, self.on_order)
            app.bus.subscribe(FillReceived, self.on_fill)
            app.bus.subscribe(PortfolioUpdated, self.on_tick)
            app.bus.subscribe(MarketSettled, self.on_settled)

    def on_settled(self, e: MarketSettled) -> None:
        if e.settlement_price is not None:
            self.app.repo.audit(
                "scorecard",
                "scorecard_settlement",
                {
                    "ticker": e.ticker,
                    "ts_ms": e.ts_ms,
                    "price": e.settlement_price,
                },
            )

    def mid(self, ticker: str, now: int) -> int | None:
        b = self.app.md.book(ticker)
        if b is None or not self.app.md.is_fresh(ticker, now):
            return None
        if b.best_bid is None or b.best_ask is None:
            return None
        return (b.best_bid + b.best_ask) // 2

    def on_order(self, e: OrderUpdated) -> None:
        o = e.order
        previous = self.decisions.get(o.client_order_id)
        # Preserve the decision midpoint through broker acknowledgements and fills.
        if previous is None or (previous["price"], previous["qty"]) != (o.price, o.qty):
            self.decisions[o.client_order_id] = {
                "price": o.price,
                "qty": o.qty,
                "mid": self.mid(o.ticker, e.ts_ms),
                "edge": o.expected_edge,
            }
        if o.status.is_terminal and o.status.value != "filled":
            self.decisions.pop(o.client_order_id, None)

    def on_fill(self, e: FillReceived) -> None:
        f = e.fill
        if not f.fill_id or f.fill_id in self.seen_fills:
            return  # no stable identity for deduplication
        self.seen_fills.add(f.fill_id)
        d = self.decisions.get(f.client_order_id, {})
        edge = d.get("edge")
        # Only strategy entries have a hold-to-settlement expectation. Exit EV has different semantics.
        o = self.app.oms.orders.get(f.client_order_id)
        expected = None
        if o is not None and o.is_entry and edge is not None:
            expected = round(edge * 1_000_000) + f.side.sign * (d["price"] - f.price) * 100
        payload = {
            "fill_id": f.fill_id,
            "ticker": f.ticker,
            "ts_ms": f.ts_ms,
            "side": f.side.value,
            "price": f.price,
            "qty": f.qty,
            "decision_mid": d.get("mid"),
            "expected_per_contract_micros": expected,
        }
        self.app.repo.audit("scorecard", "scorecard_fill", payload)
        self.pending[f.fill_id] = payload
        if o is not None and o.status.is_terminal:
            self.decisions.pop(f.client_order_id, None)

    async def restore(self) -> None:
        if not self.enabled:
            return
        # Pending observations survive restarts; old context remains in the audit journal.
        _, _, _, context = await self.app.repo.scorecard_rows()
        self.seen_fills = {r.payload["fill_id"] for r in context if r.action == "scorecard_fill"}
        done = {r.payload["fill_id"] for r in context if r.action == "scorecard_markout"}
        now = self.app.clock.now_ms()
        self.pending = {
            r.payload["fill_id"]: r.payload
            for r in context
            if r.action == "scorecard_fill"
            and r.payload["fill_id"] not in done
            and now - r.payload["ts_ms"] <= 90_000
        }

    def on_tick(self, e: PortfolioUpdated) -> None:
        for key, f in list(self.pending.items()):
            age = e.ts_ms - f["ts_ms"]
            if age > 90_000:
                del self.pending[key]
            elif age >= 60_000 and (mid := self.mid(f["ticker"], e.ts_ms)) is not None:
                sign = 1 if f["side"] == "bid" else -1
                self.app.repo.audit(
                    "scorecard",
                    "scorecard_markout",
                    {
                        "fill_id": key,
                        "markout_price_units": sign * (mid - f["price"]),
                        "observed_ms": e.ts_ms,
                    },
                )
                del self.pending[key]


@dataclass
class _Lot:
    strategy: str
    sign: int
    qty: int
    price: int
    fee: int
    expected: int | None  # total micros for remaining lot, net of modeled fees


def _metric() -> dict[str, int]:
    return dict.fromkeys(
        (
            "orders",
            "rejected",
            "eligible_orders",
            "filled_orders",
            "fills",
            "fill_qty",
            "fees",
            "gross",
            "closed_qty",
            "winning_qty",
            "settled_qty",
            "settled_net",
            "expected_qty",
            "expected",
            "paired_settled_qty",
            "paired_expected",
            "paired_net",
            "slippage_qty",
            "slippage",
            "markout_qty",
            "markout",
        ),
        0,
    )


async def paper_scorecard(repo: Repository, now_ms: int, mode: str = "paper") -> dict[str, Any]:
    orders, fills, settlements, context = await repo.scorecard_rows()
    stats: dict[str, dict[str, int]] = defaultdict(_metric)
    details = {r.payload["fill_id"]: r.payload for r in context if r.action == "scorecard_fill"}
    markouts = {r.payload["fill_id"]: r.payload for r in context if r.action == "scorecard_markout"}
    payouts = {
        (r.payload["ticker"], r.payload["ts_ms"]): r.payload["price"]
        for r in context
        if r.action == "scorecard_settlement"
    }
    filled_ids = {f.client_order_id for f in fills}
    for o in orders:
        s = stats[o.strategy]
        s["orders"] += 1
        s["rejected"] += o.status == "rejected"
        s["eligible_orders"] += o.status != "rejected"
        s["filled_orders"] += o.status != "rejected" and o.client_order_id in filled_ids

    lots: dict[str, deque[_Lot]] = defaultdict(deque)
    seen: set[str] = set()
    # At equal timestamps fills precede settlements, matching portfolio event delivery.
    events: list[tuple[int, int, int, Any]] = sorted(
        [(f.ts_ms, 0, i, f) for i, f in enumerate(fills)]
        + [(r.ts_ms, 1, i, r) for i, r in enumerate(settlements)]
    )
    unsupported_settlements = 0
    for _, kind, _, row in events:
        queue = lots[row.ticker]
        if kind == 1:
            # Scalar payouts cannot be recovered precisely from the legacy settlement row.
            price = payouts.get((row.ticker, row.ts_ms))
            if price is None and row.result.lower() in ("yes", "no"):
                price = 10_000 if row.result.lower() == "yes" else 0
            if price is None:
                unsupported_settlements += 1
                queue.clear()
                continue
            while queue:
                lot = queue.popleft()
                s = stats[lot.strategy]
                gross = lot.sign * (price - lot.price) * lot.qty
                net = gross - lot.fee
                s["gross"] += gross
                s["closed_qty"] += lot.qty
                s["winning_qty"] += lot.qty if net > 0 else 0
                s["settled_qty"] += lot.qty
                s["settled_net"] += net
                if lot.expected is not None:
                    s["paired_settled_qty"] += lot.qty
                    s["paired_expected"] += lot.expected
                    s["paired_net"] += net
            continue
        f = row
        if f.fill_id in seen:
            continue
        seen.add(f.fill_id)
        sign = BookSide(f.side).sign
        s = stats[f.strategy]
        s["fills"] += 1
        s["fill_qty"] += f.qty
        d = details.get(f.fill_id, {})
        if d.get("decision_mid") is not None:
            s["slippage_qty"] += f.qty
            s["slippage"] += sign * (f.price - d["decision_mid"]) * f.qty
        if f.fill_id in markouts:
            s["markout_qty"] += f.qty
            s["markout"] += markouts[f.fill_id]["markout_price_units"] * f.qty
        remaining, fee_left = f.qty, f.fee
        while remaining and queue and queue[0].sign != sign:
            lot = queue[0]
            n = min(remaining, lot.qty)
            entry_fee = lot.fee * n // lot.qty
            exit_fee = fee_left * n // remaining
            gross = lot.sign * (f.price - lot.price) * n
            owner = stats[lot.strategy]
            owner["gross"] += gross
            owner["fees"] += exit_fee
            owner["closed_qty"] += n
            owner["winning_qty"] += n if gross - entry_fee - exit_fee > 0 else 0
            if lot.expected is not None:
                lot.expected -= lot.expected * n // lot.qty
            lot.fee -= entry_fee
            lot.qty -= n
            remaining -= n
            fee_left -= exit_fee
            if not lot.qty:
                queue.popleft()
        if remaining:
            expected_pc = d.get("expected_per_contract_micros")
            expected = None if expected_pc is None else expected_pc * remaining // 100
            lots[f.ticker].append(_Lot(f.strategy, sign, remaining, f.price, fee_left, expected))
            s["fees"] += fee_left
            if expected is not None:
                s["expected_qty"] += remaining
                s["expected"] += expected

    def output(strategy: str, s: dict[str, int]) -> dict[str, Any]:
        def ratio(n: str, d: str) -> float | None:
            return s[n] / s[d] if s[d] else None

        return {
            "strategy": strategy,
            "orders": s["orders"],
            "rejected": s["rejected"],
            "fills": s["fills"],
            "filled_contracts": s["fill_qty"] / 100,
            "order_fill_rate": ratio("filled_orders", "eligible_orders"),
            "gross_realized_usd": s["gross"] / 1e6,
            "fees_usd": s["fees"] / 1e6,
            "net_realized_usd": (s["gross"] - s["fees"]) / 1e6,
            "closed_contracts": s["closed_qty"] / 100,
            "win_rate": ratio("winning_qty", "closed_qty"),
            "expected_entry_usd": s["expected"] / 1e6 if s["expected_qty"] else None,
            "expected_contracts": s["expected_qty"] / 100,
            "settled_contracts": s["settled_qty"] / 100,
            "settled_net_usd": s["settled_net"] / 1e6 if s["settled_qty"] else None,
            "paired_settled_contracts": s["paired_settled_qty"] / 100,
            "settled_expected_usd": s["paired_expected"] / 1e6 if s["paired_settled_qty"] else None,
            "settled_actual_usd": s["paired_net"] / 1e6 if s["paired_settled_qty"] else None,
            "slippage_cents": None if not s["slippage_qty"] else s["slippage"] / s["slippage_qty"] / 100,
            "slippage_contracts": s["slippage_qty"] / 100,
            "markout_60s_cents": None if not s["markout_qty"] else s["markout"] / s["markout_qty"] / 100,
            "markout_contracts": s["markout_qty"] / 100,
        }

    total = _metric()
    for s in stats.values():
        for k, v in s.items():
            total[k] += v
    return {
        "mode": mode,
        "as_of_ms": now_ms,
        "scope": "lifetime",
        "first_fill_ms": fills[0].ts_ms if fills else None,
        "totals": output("all", total),
        "strategies": [output(k, v) for k, v in sorted(stats.items())],
        "unsupported_settlements": unsupported_settlements,
    }
