"""Discord embed builders (pure formatting; no I/O)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import discord

from kst.core.fixedpoint import MICROS_PER_DOLLAR

if TYPE_CHECKING:
    from kst.portfolio.reports import DailyPnL

GREEN, RED, AMBER, BLUE, GREY = 0x2ECC71, 0xE74C3C, 0xF39C12, 0x3498DB, 0x95A5A6
STATE_COLOR = {"running": GREEN, "paused": AMBER, "killed": RED}


def usd(x: float) -> str:
    return f"{'-' if x < 0 else ''}${abs(x):,.2f}"


def status_embed(st: dict[str, Any], llm: dict[str, Any] | None = None) -> discord.Embed:
    e = discord.Embed(
        title=f"kst · {st['mode'].upper()} · {st['state'].upper()}",
        description=st.get("state_reason") or "",
        color=STATE_COLOR.get(st["state"], GREY),
    )
    e.add_field(name="Equity", value=usd(st["equity"]))
    e.add_field(name="Today", value=usd(st["pnl_today"]))
    e.add_field(name="Cash", value=usd(st["cash"]))
    e.add_field(name="Positions", value=str(st["positions"]))
    e.add_field(name="Open orders", value=str(st["open_orders"]))
    e.add_field(name="Markets today", value=f"{st['markets_entered_today']}/{st['max_markets_per_day']}")
    e.add_field(name="Autotrade", value="ON" if st["autotrade"] else "off")
    e.add_field(name="Market data", value="connected" if st["md_connected"] else "⚠️ DOWN")
    if llm:
        e.add_field(
            name="LLM",
            value=f"{llm['mode']} · {llm.get('model') or '-'} · {llm['calls_today']}/{llm['max_calls']} calls",
        )
    return e


def pnl_embed(s: dict[str, Any]) -> discord.Embed:
    color = GREEN if s["pnl_today"] >= 0 else RED
    e = discord.Embed(title=f"P&L {s['day']} · {usd(s['pnl_today'])}", color=color)
    e.add_field(name="Realized", value=usd(s["realized_today"]))
    e.add_field(name="Unrealized", value=usd(s["unrealized"]))
    e.add_field(name="Fees", value=usd(-s["fees_today"]))
    e.add_field(name="Equity", value=usd(s["equity"]))
    e.add_field(name="Peak", value=usd(s["peak_equity"]))
    e.add_field(name="Exposure", value=usd(s["exposure"]))
    rt = s["round_trips_today"]
    wr = f"{s['wins_today'] / rt:.0%}" if rt else "—"
    e.add_field(name="Trades / RT / Win%", value=f"{s['trades_today']} / {rt} / {wr}")
    for name, v in sorted((s.get("by_strategy") or {}).items()):
        e.add_field(
            name=name,
            value=f"net {usd(v.get('net_usd', 0))} · fees {usd(v.get('fees_usd', 0))} · {int(v.get('trades', 0))} tr",
            inline=False,
        )
    return e


def daily_report_embed(r: DailyPnL) -> discord.Embed:
    net = r.net / MICROS_PER_DOLLAR
    e = discord.Embed(
        title=f"📒 Daily report {r.day} [{r.mode}] · {usd(net)}", color=GREEN if net >= 0 else RED
    )
    e.description = "```\n" + r.text() + "\n```"
    return e


def positions_embed(rows: list[dict[str, Any]]) -> discord.Embed:
    e = discord.Embed(title=f"Positions ({len(rows)})", color=BLUE)
    if not rows:
        e.description = "Flat."
    for p in rows[:20]:
        u = p.get("unrealized")
        e.add_field(
            name=f"{p['ticker']} · {p['outcome'].upper()} {abs(p['qty']):g}",
            value=f"avg {p['avg_price']:.3f} · mark {p['mark'] if p['mark'] is not None else '—'} · uPnL {usd(u) if u is not None else '—'} · {p['strategy']}",
            inline=False,
        )
    return e


def fills_embed(fills: list[dict[str, Any]]) -> discord.Embed:
    e = discord.Embed(title=f"Fills ({len(fills)})", color=BLUE)
    lines = []
    for f in fills[:15]:
        verb = "BUY YES" if f["side"] == "bid" else "BUY NO"
        price = f["price"] if f["side"] == "bid" else 1 - f["price"]
        lines.append(
            f"`{f['ticker']}` {verb} {f['qty']:g} @ {price:.3f}  fee ${f['fee']:.3f}  [{f['strategy']}]"
        )
    e.description = "\n".join(lines) + ("\n…" if len(fills) > 15 else "")
    return e


def alert_embed(level: str, title: str, message: str) -> discord.Embed:
    color = {"critical": RED, "warning": AMBER}.get(level, BLUE)
    return discord.Embed(
        title=("🚨 " if level == "critical" else "⚠️ " if level == "warning" else "ℹ️ ") + title,
        description=message[:3900],
        color=color,
    )


def limits_embed(limits: dict[str, Any]) -> discord.Embed:
    e = discord.Embed(title="Risk limits", color=GREY)
    e.description = "```\n" + "\n".join(f"{k:<32} {v}" for k, v in limits.items()) + "\n```"
    return e


def scan_embed(rows: list[dict[str, Any]]) -> discord.Embed:
    e = discord.Embed(title="Top scanner rows", color=BLUE)
    lines = []
    for r in rows:
        edge = r.get("edge")
        lines.append(
            f"`{r['ticker'][:34]}` {r['bid'] / 100 if r['bid'] else '—'}/{r['ask'] / 100 if r['ask'] else '—'}¢"
            f" fair {('%.1f' % (r['q'] * 100)) if r.get('q') is not None else '—'}"
            f" edge {('%+.1f¢' % (edge * 100)) if edge is not None else '—'} {' '.join(r['signals'][:3])}"
        )
    e.description = "\n".join(lines) or "No data yet."
    return e
