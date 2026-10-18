"""Point-in-time catalog snapshots for offline replay (integer price/quantity units)."""

from __future__ import annotations

from dataclasses import asdict, fields
from typing import Any

from kst.core.types import MarketStatus, StrikeType
from kst.domain.market import Event, Market, PriceBand, PriceGrid, Series
from kst.marketdata.catalog import MarketCatalog


def catalog_snapshot(catalog: MarketCatalog) -> dict[str, Any]:
    markets = []
    for _, m in sorted(catalog.markets.items()):
        row = {f.name: getattr(m, f.name) for f in fields(m) if f.name != "grid"}
        row["grid"] = [asdict(b) for b in m.grid.bands]
        markets.append(row)
    return {
        "markets": markets,
        "events": [asdict(v) for _, v in sorted(catalog.events.items())],
        "series": [asdict(v) for _, v in sorted(catalog.series.items())],
        "universe": sorted(catalog.universe),
    }


def restore_catalog(catalog: MarketCatalog, payload: dict[str, Any], now_ms: int) -> None:
    """Replace the observed universe, retaining existing positions' market metadata."""
    for raw in payload["markets"]:
        row = dict(raw)
        row["status"] = MarketStatus(row["status"])
        row["strike_type"] = StrikeType(row["strike_type"])
        row["grid"] = PriceGrid.of([PriceBand(**b) for b in row.pop("grid")])
        catalog.upsert_market(Market(**row))
    catalog.events = {e["event_ticker"]: Event(**e) for e in payload["events"]}
    catalog.series = {s["ticker"]: Series(**s) for s in payload["series"]}
    catalog.universe = set(payload["universe"])
    if not catalog.universe <= catalog.markets.keys():
        raise ValueError("Catalog universe contains markets without metadata")
    catalog.last_refresh_ms = now_ms
