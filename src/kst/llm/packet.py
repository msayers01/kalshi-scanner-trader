"""Compact, cache-friendly decision packets for the LLM entry gate (docs/LLM_GATE.md).

The system prompt is STATIC (byte-identical every call) so provider-side prompt caching works; all
volatile data goes in the user message. Rows use short numeric ids and cents to save tokens.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

from kst.core.clock import ms_to_dt
from kst.core.types import BookSide

SYSTEM_PROMPT = """You are the entry gate for a systematic trading bot on Kalshi (a regulated US prediction-market exchange).
Each row is a binary market the bot's quantitative strategy wants to ENTER right now. The bot already did
the math: it priced the contract, checked fees, liquidity and strict risk limits. It will size, price and
exit the position itself. Your only job is to approve or veto each entry using judgment the math lacks.

Decide E (enter) or S (skip) per row.
Approve (E) when:
- the market's resolution is clear and the bot's "fair" estimate is plausible for this question;
- nothing you know suggests the crowd price reflects information a statistical model would miss
  (scheduled announcements in the next hours, breaking news, injuries, insider-prone topics);
- the proposed side makes sense given the title and current price.
Skip (S) when:
- rules or resolution look ambiguous, subjective, or easy to misread;
- the outcome is likely already decided or about to be revealed (the price may be stale, not wrong);
- the question depends on fast-moving news you cannot verify, or you simply lack knowledge;
- the edge looks too good to be true for a liquid, heavily traded market.
When unsure, choose S. A skipped good trade costs little; an approved bad trade costs money.

Optional side view s: Y if you think YES is underpriced, N if NO is underpriced, - for no view.
Confidence c: 0-100, how sure you are of your verdict.
Reason r: at most 12 words, no filler.

Columns: i=id | mkt=market title (YES outcome) | cat=category | close=time to close | bid,ask=YES prices in
cents | vol24=contracts traded 24h | oi=open interest | chg24=24h price change in cents | fair=bot's YES
probability in cents (- if none) | want=bot's proposed entry (BUY_YES or BUY_NO) and its edge in cents
after fees | note=model details or a rules excerpt.

Reply with JSON only, no prose, exactly this shape:
{"d":[{"i":1,"v":"E","c":70,"s":"-","r":"clear rules, model plausible"}]}
Include every id exactly once."""

CATEGORY_ABBR = {
    "Crypto": "CRY",
    "Climate and Weather": "WX",
    "Economics": "ECO",
    "Financials": "FIN",
    "Sports": "SPT",
    "Politics": "POL",
    "Elections": "ELE",
    "Entertainment": "ENT",
    "Science and Technology": "SCI",
    "Companies": "CO",
    "World": "WLD",
    "Health": "HLT",
    "Commodities": "CMD",
    "Mentions": "MEN",
    "Social": "SOC",
    "Transportation": "TRN",
}


@dataclass(frozen=True, slots=True)
class Candidate:
    ticker: str
    title: str
    category: str
    close_s: float | None
    bid: int | None  # Price units
    ask: int | None
    vol24: float
    oi: float
    chg24: float | None  # dollars
    fair: float | None  # probability
    want_side: BookSide | None
    want_edge: float | None  # $/contract
    note: str = ""


@dataclass(frozen=True, slots=True)
class ParsedVerdict:
    ticker: str
    enter: bool
    confidence: int
    side: BookSide | None
    reason: str


def _ttc(s: float | None) -> str:
    if s is None:
        return "?"
    if s < 3600:
        return f"{s / 60:.0f}m"
    if s < 86400 * 2:
        return f"{s / 3600:.1f}h"
    return f"{s / 86400:.0f}d"


def _c(p: int | None) -> str:
    return "-" if p is None else f"{p / 100:g}"


def _k(x: float) -> str:
    if x >= 1e6:
        return f"{x / 1e6:.1f}M"
    if x >= 1e3:
        return f"{x / 1e3:.0f}k"
    return f"{x:.0f}"


def _clean(s: str, n: int) -> str:
    s = " ".join(s.replace("|", "/").split())
    return s if len(s) <= n else s[: n - 1] + "…"


def build_packet(cands: list[Candidate], now_ms: int) -> tuple[str, dict[int, str]]:
    """Return (user_message, id→ticker)."""
    ids: dict[int, str] = {}
    lines = [
        f"T={ms_to_dt(now_ms).strftime('%Y-%m-%dT%H:%MZ')} cols: i|mkt|cat|close|bid|ask|vol24|oi|chg24|fair|want|note"
    ]
    for i, c in enumerate(cands, 1):
        ids[i] = c.ticker
        want = "-"
        if c.want_side is not None:
            want = ("BUY_YES" if c.want_side is BookSide.BID else "BUY_NO") + (
                f" +{c.want_edge * 100:.1f}" if c.want_edge is not None else ""
            )
        lines.append(
            "|".join(
                [
                    str(i),
                    _clean(c.title, 70),
                    CATEGORY_ABBR.get(c.category, c.category[:3].upper() or "?"),
                    _ttc(c.close_s),
                    _c(c.bid),
                    _c(c.ask),
                    _k(c.vol24),
                    _k(c.oi),
                    "-" if c.chg24 is None else f"{c.chg24 * 100:+.0f}",
                    "-" if c.fair is None else f"{c.fair * 100:.1f}",
                    want,
                    _clean(c.note, 160),
                ]
            )
        )
    lines.append("Reply JSON only.")
    return "\n".join(lines), ids


def packet_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


_THINK = re.compile(r"<think>.*?</think>", re.S)


def extract_json(text: str) -> dict[str, Any] | None:
    """Lenient: strip <think> blocks / code fences, then take the outermost {...}."""
    t = _THINK.sub("", text or "").strip()
    t = t.replace("```json", "```")
    if "```" in t:
        parts = [p for p in t.split("```") if "{" in p]
        t = parts[0] if parts else t
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        obj = json.loads(t[start : end + 1])
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def parse_reply(text: str, ids: dict[int, str]) -> dict[str, ParsedVerdict]:
    obj = extract_json(text)
    out: dict[str, ParsedVerdict] = {}
    if obj is None:
        return out
    rows = obj.get("d") or obj.get("decisions") or []
    if not isinstance(rows, list):
        return out
    for r in rows:
        if not isinstance(r, dict):
            continue
        try:
            tid = int(r["i"])
        except (KeyError, TypeError, ValueError):
            continue
        ticker = ids.get(tid)
        if ticker is None:
            continue
        v = str(r.get("v", "S")).upper()[:1]
        s = str(r.get("s", "-")).upper()[:1]
        try:
            conf = max(0, min(100, int(r.get("c", 0))))
        except (TypeError, ValueError):
            conf = 0
        out[ticker] = ParsedVerdict(
            ticker=ticker,
            enter=v == "E",
            confidence=conf,
            side=BookSide.BID if s == "Y" else BookSide.ASK if s == "N" else None,
            reason=_clean(str(r.get("r", "")), 120),
        )
    return out


# JSON schema for providers that support constrained decoding (structured outputs).
REPLY_SCHEMA = {
    "type": "object",
    "properties": {
        "d": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "i": {"type": "integer"},
                    "v": {"type": "string", "enum": ["E", "S"]},
                    "c": {"type": "integer"},
                    "s": {"type": "string", "enum": ["Y", "N", "-"]},
                    "r": {"type": "string"},
                },
                "required": ["i", "v", "c", "s", "r"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["d"],
    "additionalProperties": False,
}
