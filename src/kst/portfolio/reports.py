"""Daily P&L report: built at day rollover / on demand; persisted to DB, data/pnl/YYYY-MM-DD.json, logs,
and pushed to Discord."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from kst.core.fixedpoint import MICROS_PER_DOLLAR, fmt_usd


@dataclass(slots=True)
class DailyPnL:
    day: str
    mode: str
    start_equity: int  # micros
    end_equity: int
    realized: int  # gross realized P&L (excl. fees)
    fees: int
    unrealized_end: int
    trades: int
    round_trips: int
    wins: int
    losses: int
    markets_entered: int
    peak_equity: int
    by_strategy: dict[str, dict[str, float]] = field(
        default_factory=dict
    )  # name → {net_usd, fees_usd, trades}
    generated_ms: int = 0

    @property
    def net(self) -> int:
        return self.end_equity - self.start_equity

    @property
    def win_rate(self) -> float | None:
        n = self.wins + self.losses
        return None if n == 0 else self.wins / n

    @property
    def max_drawdown(self) -> int:
        return self.peak_equity - self.end_equity

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.update(
            net=self.net,
            win_rate=self.win_rate,
            net_usd=self.net / MICROS_PER_DOLLAR,
            realized_usd=self.realized / MICROS_PER_DOLLAR,
            fees_usd=self.fees / MICROS_PER_DOLLAR,
            unrealized_usd=self.unrealized_end / MICROS_PER_DOLLAR,
            end_equity_usd=self.end_equity / MICROS_PER_DOLLAR,
        )
        return d

    def text(self) -> str:
        wr = "—" if self.win_rate is None else f"{self.win_rate:.0%}"
        lines = [
            f"P&L {self.day} [{self.mode.upper()}]",
            f"Net {fmt_usd(self.net)}  (realized {fmt_usd(self.realized)}, fees {fmt_usd(-self.fees)}, unrealized {fmt_usd(self.unrealized_end)})",
            f"Equity {fmt_usd(self.start_equity)} → {fmt_usd(self.end_equity)}  peak {fmt_usd(self.peak_equity)}",
            f"Trades {self.trades} · round trips {self.round_trips} · win rate {wr} · markets entered {self.markets_entered}",
        ]
        for name, s in sorted(self.by_strategy.items()):
            lines.append(
                f"  {name}: net ${s.get('net_usd', 0):.2f}, fees ${s.get('fees_usd', 0):.2f}, trades {int(s.get('trades', 0))}"
            )
        return "\n".join(lines)


def write_ledger(directory: Path, report: DailyPnL) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{report.day}_{report.mode}.json"
    path.write_text(json.dumps(report.to_dict(), indent=2, default=str), encoding="utf-8")
    return path
