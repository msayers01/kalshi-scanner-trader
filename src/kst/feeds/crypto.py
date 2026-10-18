"""Crypto reference prices for the digital model.

Primary: Kalshi `cfbenchmarks_value` WS channel — the actual settlement index (needs API keys).
Fallback: Coinbase Exchange public WS ticker (proxy; basis risk is priced in by the model).
Vol seeding: Coinbase 1-minute candles (public REST) at startup.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import orjson
import websockets

from kst.config import CryptoFeedCfg
from kst.core.bus import EventBus
from kst.core.clock import Clock
from kst.core.events import FeedHealth, ReferencePriceUpdated
from kst.core.logging import get_logger
from kst.kalshi.ws import KalshiWs
from kst.marketdata.recorder import Recorder
from kst.pricing.crypto import ReferenceStore

log = get_logger(__name__)

COINBASE_WS = "wss://ws-feed.exchange.coinbase.com"
COINBASE_REST = "https://api.exchange.coinbase.com"


class CryptoFeed:
    def __init__(
        self,
        cfg: CryptoFeedCfg,
        store: ReferenceStore,
        bus: EventBus,
        clock: Clock,
        *,
        kalshi_ws: KalshiWs | None = None,
        recorder: Recorder | None = None,
        http: httpx.AsyncClient | None = None,
    ) -> None:
        self.cfg = cfg
        self.store = store
        self.bus = bus
        self.clock = clock
        self.kalshi_ws = kalshi_ws
        self.recorder = recorder
        self.http = http or httpx.AsyncClient(timeout=10.0)
        self.index_ids = sorted(set(cfg.series_index.values()))
        src = cfg.source
        if src == "auto":
            src = "kalshi_cf" if kalshi_ws is not None else "coinbase"
        self.source = src
        self._last_pub: dict[str, int] = {}

    # ------------------------------------------------------------------ wiring

    def attach(self) -> None:
        """Register on the Kalshi WS (if used). Call before the WS task starts."""
        if self.source == "kalshi_cf" and self.kalshi_ws is not None:
            self.kalshi_ws.handlers["cfbenchmarks_value"] = self._on_cf
            self.kalshi_ws.want_indices(set(self.index_ids))

    def _emit(self, index_id: str, value: float, ts_ms: int, source: str) -> None:
        self.store.on_price(index_id, ts_ms, value, source)
        if self.recorder:
            self.recorder.ref(ts_ms, index_id, value, source)
        last = self._last_pub.get(index_id, 0)
        if ts_ms - last >= 1000:  # bus/UI at ≤1 Hz per index
            self._last_pub[index_id] = ts_ms
            self.bus.publish(ReferencePriceUpdated(ts_ms, index_id, value, source))

    def _on_cf(self, msg: dict[str, Any]) -> None:
        body = msg.get("msg") or {}
        idx = body.get("index_id", "")
        data = body.get("data")
        try:
            frame = orjson.loads(data) if isinstance(data, str | bytes) else (data or {})
            value = float(frame.get("value"))
            ts = int(frame.get("time") or body.get("received_at") or self.clock.now_ms())
        except (TypeError, ValueError, orjson.JSONDecodeError):
            return
        self._emit(idx, value, ts, "kalshi_cf")

    # ------------------------------------------------------------------ seeding

    async def seed_vol(self) -> None:
        for idx in self.index_ids:
            product = self.cfg.coinbase_products.get(idx)
            if not product:
                continue
            try:
                r = await self.http.get(f"{COINBASE_REST}/products/{product}/candles", params={"granularity": 60})
                r.raise_for_status()
                rows = sorted(r.json(), key=lambda x: x[0])  # [time, low, high, open, close, volume]
                closes = [float(x[4]) for x in rows]
                st = self.store.get(idx)
                st.vol.seed_from_closes(closes, 60.0)
                if self.source == "coinbase" and rows:
                    self._emit(idx, closes[-1], int(rows[-1][0]) * 1000, "coinbase")
                log.info("crypto_vol_seeded", index=idx, n=len(closes), sigma=getattr(st.vol.estimate(), "sigma_annual", None))
            except Exception as exc:
                log.warning("crypto_vol_seed_failed", index=idx, error=str(exc))

    # ------------------------------------------------------------------ coinbase fallback

    async def run_coinbase(self) -> None:
        prod_to_idx = {p: i for i, p in self.cfg.coinbase_products.items() if i in self.index_ids}
        if not prod_to_idx:
            return
        backoff = 1.0
        while True:
            try:
                async with websockets.connect(COINBASE_WS, ping_interval=20, open_timeout=15) as ws:
                    await ws.send(orjson.dumps({"type": "subscribe", "product_ids": sorted(prod_to_idx), "channels": ["ticker"]}).decode())
                    self.bus.publish(FeedHealth(self.clock.now_ms(), "crypto:coinbase", True))
                    backoff = 1.0
                    async for raw in ws:
                        m = orjson.loads(raw)
                        if m.get("type") != "ticker":
                            continue
                        idx = prod_to_idx.get(m.get("product_id", ""))
                        if idx is None or "price" not in m:
                            continue
                        self._emit(idx, float(m["price"]), self.clock.now_ms(), "coinbase")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("coinbase_ws_disconnected", error=str(exc))
                self.bus.publish(FeedHealth(self.clock.now_ms(), "crypto:coinbase", False, str(exc)))
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)
