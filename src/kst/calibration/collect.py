"""Bounded, read-only collection of pre-close quotes and final binary outcomes."""

from __future__ import annotations

import math
from collections import Counter
from typing import Any

from kst.calibration.data import MarketSample
from kst.config import CalibrationToolsCfg
from kst.core.clock import parse_iso_ms
from kst.kalshi.rest import KalshiApiError, KalshiRest


def _price(obj: dict[str, Any], field: str) -> float | None:
    item = obj.get(field) or {}
    raw = item.get("close_dollars", item.get("close"))
    try:
        if raw is None:
            return None
        value = float(raw)
        return value if type(raw) is not bool and math.isfinite(value) and 0 < value < 1 else None
    except (ValueError, TypeError):
        return None


def sample_market(
    raw: dict[str, Any], candles: list[dict[str, Any]], category: str, cfg: CalibrationToolsCfg, as_of_ms: int
) -> MarketSample | None:
    close = parse_iso_ms(raw.get("close_time"))
    settled = parse_iso_ms(raw.get("settlement_ts"))
    if close is None or settled is None or settled > as_of_ms or close > settled:
        return None
    if (
        raw.get("market_type") != "binary"
        or raw.get("result") not in ("yes", "no")
        or raw.get("is_provisional")
        or raw.get("mve_collection_ticker")
    ):
        return None
    outcome = int(raw["result"] == "yes")
    payout = raw.get("settlement_value_dollars")
    if payout is not None and float(payout) != outcome:
        return None
    target = close - cfg.horizon_h * 3_600_000
    eligible = []
    for candle in candles:
        end = int(candle.get("end_period_ts", 0)) * 1000
        if not target - cfg.max_quote_age_s * 1000 <= end <= target:
            continue
        bid, ask = _price(candle, "yes_bid"), _price(candle, "yes_ask")
        volume = float(candle.get("volume_fp", candle.get("volume", 0)) or 0)
        if (
            bid is None
            or ask is None
            or bid > ask
            or ask - bid > cfg.max_spread
            or not math.isfinite(volume)
            or volume <= 0
            or _price(candle, "price") is None
        ):
            continue
        eligible.append((end, (bid + ask) / 2))
    if not eligible:
        return None
    observed, price = max(eligible)
    return MarketSample(
        ticker=raw["ticker"],
        event_ticker=raw["event_ticker"],
        category=category or "Unknown",
        price=price,
        outcome=outcome,
        observed_ms=observed,
        close_ms=close,
        settled_ms=settled,
    )


async def collect_markets(
    rest: KalshiRest, series: list[str], cfg: CalibrationToolsCfg, as_of_ms: int
) -> tuple[list[MarketSample], dict[str, Any], list[dict[str, Any]]]:
    if not series:
        raise ValueError("Specify at least one series for bounded historical acquisition")
    candidates: dict[str, tuple[dict[str, Any], str, str, str]] = {}
    counts: Counter[str] = Counter()
    truncated: list[str] = []
    for ticker in sorted(set(series)):
        category = (await rest.series(ticker)).get("category", "")
        # Read both partitions. Neither a settlement cutoff nor a current series category is a quote.
        for tier in ("recent", "historical"):
            stream = (
                rest.markets(status="settled", series_ticker=ticker, limit=min(cfg.max_markets, 1000))
                if tier == "recent"
                else rest.historical_markets(series_ticker=ticker, limit=min(cfg.max_markets, 1000))
            )
            visited = 0
            async for raw in stream:
                visited += 1
                counts[f"{tier}_visited"] += 1
                if raw.get("ticker"):
                    candidates.setdefault(raw["ticker"], (raw, tier, ticker, category))
                if visited >= cfg.max_markets:
                    truncated.append(f"{ticker}:{tier}")
                    break
    ordered = sorted(
        candidates.values(),
        key=lambda item: (item[0].get("settlement_ts", ""), item[0]["ticker"]),
        reverse=True,
    )
    chosen = ordered[: cfg.max_markets]
    samples, evidence = [], []
    for raw, tier, series_ticker, category in chosen:
        close = parse_iso_ms(raw.get("close_time"))
        settled = parse_iso_ms(raw.get("settlement_ts"))
        if (
            close is None
            or settled is None
            or settled > as_of_ms
            or raw.get("result") not in ("yes", "no")
            or raw.get("status") not in ("settled", "finalized")
        ):
            counts["excluded_not_final_as_of"] += 1
            continue
        target = int((close - cfg.horizon_h * 3_600_000) // 1000)
        start = max(0, target - math.ceil(cfg.max_quote_age_s))

        async def get_candles(
            use_history: bool,
            raw: dict[str, Any] = raw,
            start: int = start,
            target: int = target,
            series_ticker: str = series_ticker,
        ) -> list[dict[str, Any]]:
            return (
                await rest.historical_candlesticks(raw["ticker"], start, target)
                if use_history
                else await rest.candlesticks(series_ticker, raw["ticker"], start, target)
            )

        try:
            candles = await get_candles(tier == "historical")
        except KalshiApiError as exc:
            if exc.status != 404:
                raise
            candles = await get_candles(tier != "historical")  # handle a market crossing the archive boundary
            counts["archive_boundary_fallback"] += 1
        sample = sample_market(raw, candles, category, cfg, as_of_ms)
        evidence.append({"tier": tier, "market": raw, "candlesticks": candles})
        if sample is None:
            counts["excluded_nonbinary_or_no_valid_quote"] += 1
        else:
            samples.append(sample)
    return (
        samples,
        {
            "counts": dict(counts),
            "series": sorted(set(series)),
            "bounded_partitions": truncated,
            "candidate_markets": len(candidates),
            "selected_markets": len(chosen),
            "accepted_samples": len(samples),
            "warnings": [
                "A bounded recent-settlement cohort; not a census of all historical markets.",
                "Category labels come from current series metadata; quote prices come only from pre-horizon candles.",
            ],
        },
        evidence,
    )
