"""Async Kalshi REST client (Trade API v2, V2 order endpoints).

* Public market-data endpoints work unauthenticated; portfolio/order endpoints need a signer.
* Every request passes the read/write token bucket first. 429 → drain bucket + exponential backoff.
* Order creation is idempotent via `client_order_id`: a retried create that conflicts (409) is not an error.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import urlsplit

import httpx
import orjson

from kst.core.fixedpoint import price_to_str, qty_to_str
from kst.core.logging import get_logger
from kst.core.types import BookSide, TimeInForce
from kst.kalshi.auth import KalshiSigner
from kst.kalshi.ratelimit import KalshiRateLimiter, TokenBucket

log = get_logger(__name__)


class KalshiApiError(RuntimeError):
    def __init__(self, status: int, code: str, message: str, path: str) -> None:
        super().__init__(f"{status} {code}: {message} ({path})")
        self.status = status
        self.code = code
        self.message = message
        self.path = path

    @property
    def is_conflict(self) -> bool:
        return self.status == 409


class KalshiAuthRequiredError(RuntimeError):
    pass


class KalshiRest:
    def __init__(
        self,
        base_url: str,
        signer: KalshiSigner | None = None,
        limiter: KalshiRateLimiter | None = None,
        timeout_s: float = 10.0,
        default_cost: int = 10,
        cancel_cost: int = 2,
        subaccount: int = 0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._base_path = urlsplit(self.base_url).path  # "/trade-api/v2"
        self.signer = signer
        self.limiter = limiter or KalshiRateLimiter()
        self.default_cost = default_cost
        self.cancel_cost = cancel_cost
        self.subaccount = subaccount
        self.throttled = 0  # 429 count (exposed in /api/health)
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(timeout_s, connect=5.0),
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
            headers={"User-Agent": "kst/0.1", "Accept": "application/json"},
        )

    @property
    def authenticated(self) -> bool:
        return self.signer is not None

    async def aclose(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ core request

    async def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | list[tuple[str, Any]] | None = None,
        body: dict[str, Any] | None = None,
        auth: bool = False,
        bucket: TokenBucket | None = None,
        cost: int | None = None,
        retries: int = 3,
    ) -> dict[str, Any]:
        method = method.upper()
        if auth and self.signer is None:
            raise KalshiAuthRequiredError(f"{method} {path} needs API credentials")
        bucket = bucket or (self.limiter.read if method == "GET" else self.limiter.write)
        url = self.base_url + path
        content = orjson.dumps(body) if body is not None else None
        attempt = 0
        while True:
            await bucket.acquire(cost or self.default_cost)
            headers = {"Content-Type": "application/json"} if content is not None else {}
            if self.signer is not None and (auth or path.startswith("/portfolio")):
                headers.update(self.signer.headers(method, self._base_path + path))
            try:
                resp = await self._client.request(
                    method, url, params=params, content=content, headers=headers
                )
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                attempt += 1
                # Writes are only retried because client_order_id makes creates idempotent (409 on dup).
                if attempt > retries:
                    raise
                log.warning("kalshi_transport_retry", path=path, attempt=attempt, error=str(exc))
                await asyncio.sleep(min(0.25 * 2**attempt, 4.0))
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                attempt += 1
                if resp.status_code == 429:
                    bucket.drain()
                    self.throttled += 1
                if attempt > max(retries, 5 if method == "GET" else retries):
                    self._raise(resp, path)
                await asyncio.sleep(min(0.5 * 2**attempt, 16.0))
                continue
            if resp.status_code >= 400:
                self._raise(resp, path)
            if not resp.content:
                return {}
            data = orjson.loads(resp.content)
            return data if isinstance(data, dict) else {"data": data}

    @staticmethod
    def _raise(resp: httpx.Response, path: str) -> None:
        code, message = "", resp.text[:300]
        try:
            j = resp.json()
            err = j.get("error", j) if isinstance(j, dict) else {}
            if isinstance(err, dict):
                code = str(err.get("code", ""))
                message = str(err.get("message", err.get("details", message)))
            elif isinstance(err, str):
                message = err
        except ValueError:
            pass
        raise KalshiApiError(resp.status_code, code, message, path)

    async def paginate(
        self,
        path: str,
        key: str,
        params: dict[str, Any] | None = None,
        *,
        auth: bool = False,
        max_pages: int = 500,
        page_delay_s: float = 0.0,
    ) -> AsyncIterator[dict[str, Any]]:
        """Cursor pagination. `page_delay_s` spaces heavy pages (large /markets pages are throttled harder)."""
        p = dict(params or {})
        for page in range(max_pages):
            if page and page_delay_s:
                await asyncio.sleep(page_delay_s)
            data = await self.request("GET", path, params=p, auth=auth)
            for item in data.get(key) or []:
                yield item
            cursor = data.get("cursor")
            if not cursor:
                return
            p["cursor"] = cursor

    # ------------------------------------------------------------------ public market data

    async def exchange_status(self) -> dict[str, Any]:
        return await self.request("GET", "/exchange/status")

    async def series_list(self) -> list[dict[str, Any]]:
        return (await self.request("GET", "/series")).get("series") or []

    async def series(self, series_ticker: str) -> dict[str, Any]:
        return (await self.request("GET", f"/series/{series_ticker}")).get("series") or {}

    def events(
        self, *, status: str = "open", nested_markets: bool = True, **params: Any
    ) -> AsyncIterator[dict[str, Any]]:
        q = {"status": status, "with_nested_markets": str(nested_markets).lower(), "limit": 200, **params}
        return self.paginate("/events", "events", q)

    def markets(self, *, page_delay_s: float = 0.0, **params: Any) -> AsyncIterator[dict[str, Any]]:
        q = {"limit": 500, **params}
        return self.paginate("/markets", "markets", q, page_delay_s=page_delay_s)

    async def events_by_ticker(self, event_tickers: list[str], chunk: int = 50) -> list[dict[str, Any]]:
        """Event metadata (no nested markets) for specific events, in chunks."""
        out: list[dict[str, Any]] = []
        for i in range(0, len(event_tickers), chunk):
            part = event_tickers[i : i + chunk]
            data = await self.request("GET", "/events", params={"tickers": ",".join(part), "limit": 200})
            out.extend(data.get("events") or [])
        return out

    async def market(self, ticker: str) -> dict[str, Any]:
        return (await self.request("GET", f"/markets/{ticker}")).get("market") or {}

    async def orderbook(self, ticker: str, depth: int = 0) -> dict[str, Any]:
        return await self.request(
            "GET", f"/markets/{ticker}/orderbook", params={"depth": depth} if depth else None
        )

    async def orderbooks(self, tickers: list[str]) -> list[dict[str, Any]]:
        """Up to 100 books per call. Returns [{"ticker", "orderbook_fp"}]."""
        if not tickers:
            return []
        out: list[dict[str, Any]] = []
        for i in range(0, len(tickers), 100):
            chunk = tickers[i : i + 100]
            data = await self.request("GET", "/markets/orderbooks", params=[("tickers", t) for t in chunk])
            out.extend(data.get("orderbooks") or [])
        return out

    def trades(self, **params: Any) -> AsyncIterator[dict[str, Any]]:
        return self.paginate("/markets/trades", "trades", {"limit": 1000, **params})

    async def candlesticks(
        self, series_ticker: str, ticker: str, start_ts: int, end_ts: int, period_interval: int = 1
    ) -> list[dict[str, Any]]:
        data = await self.request(
            "GET",
            f"/series/{series_ticker}/markets/{ticker}/candlesticks",
            params={"start_ts": start_ts, "end_ts": end_ts, "period_interval": period_interval},
        )
        return data.get("candlesticks") or []

    def historical_markets(self, **params: Any) -> AsyncIterator[dict[str, Any]]:
        """Archived markets; series/event/ticker filters are mutually exclusive in this API."""
        return self.paginate("/historical/markets", "markets", {"limit": 500, **params})

    async def historical_candlesticks(
        self, ticker: str, start_ts: int, end_ts: int, period_interval: int = 1
    ) -> list[dict[str, Any]]:
        data = await self.request(
            "GET",
            f"/historical/markets/{ticker}/candlesticks",
            params={
                "start_ts": start_ts,
                "end_ts": end_ts,
                "period_interval": period_interval,
            },
        )
        return data.get("candlesticks") or []

    # ------------------------------------------------------------------ portfolio (auth)

    async def balance(self) -> dict[str, Any]:
        return await self.request("GET", "/portfolio/balance", auth=True, bucket=self.limiter.balance, cost=1)

    async def account_limits(self) -> dict[str, Any]:
        return await self.request("GET", "/account/limits", auth=True)

    async def positions(self) -> list[dict[str, Any]]:
        return [
            p
            async for p in self.paginate(
                "/portfolio/positions",
                "market_positions",
                {"limit": 1000, "count_filter": "position"},
                auth=True,
            )
        ]

    async def open_orders(self) -> list[dict[str, Any]]:
        return [
            o
            async for o in self.paginate(
                "/portfolio/orders", "orders", {"status": "resting", "limit": 1000}, auth=True
            )
        ]

    async def fills(self, min_ts: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": 1000}
        if min_ts:
            params["min_ts"] = min_ts
        return [f async for f in self.paginate("/portfolio/fills", "fills", params, auth=True)]

    async def settlements(self, min_ts: int | None = None) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"limit": 1000}
        if min_ts:
            params["min_ts"] = min_ts
        return [s async for s in self.paginate("/portfolio/settlements", "settlements", params, auth=True)]

    # ------------------------------------------------------------------ orders (auth, V2)

    async def create_order(
        self,
        *,
        ticker: str,
        side: BookSide,
        price: int,
        qty: int,
        client_order_id: str,
        tif: TimeInForce = TimeInForce.GTC,
        post_only: bool = False,
        reduce_only: bool = False,
        expiration_ts: int | None = None,
        cancel_on_pause: bool = True,
        order_group_id: str | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "ticker": ticker,
            "client_order_id": client_order_id,
            "side": side.value,
            "count": qty_to_str(qty),
            "price": price_to_str(price),
            "time_in_force": tif.value,
            "self_trade_prevention_type": "taker_at_cross",
            "post_only": post_only,
            "reduce_only": reduce_only,
            "cancel_order_on_pause": cancel_on_pause,
        }
        if self.subaccount:
            body["subaccount"] = self.subaccount
        if expiration_ts and tif is TimeInForce.GTC:
            body["expiration_time"] = int(expiration_ts)
        if order_group_id:
            body["order_group_id"] = order_group_id
        return await self.request("POST", "/portfolio/events/orders", body=body, auth=True)

    async def cancel_order(self, order_id: str, ticker: str) -> dict[str, Any]:
        return await self.request(
            "DELETE",
            f"/portfolio/events/orders/{order_id}",
            params={"market_ticker": ticker},
            auth=True,
            cost=self.cancel_cost,
        )

    async def cancel_all(self) -> dict[str, Any]:
        params = {"subaccount": self.subaccount} if self.subaccount else None
        return await self.request("DELETE", "/portfolio/events/orders", params=params, auth=True, retries=5)

    async def amend_order(
        self,
        order_id: str,
        *,
        ticker: str,
        side: BookSide,
        price: int,
        total_qty: int,
        expiration_ts: int | None = None,
    ) -> dict[str, Any]:
        """`total_qty` = already-filled + desired remaining (Kalshi semantics)."""
        body: dict[str, Any] = {
            "ticker": ticker,
            "side": side.value,
            "price": price_to_str(price),
            "count": qty_to_str(total_qty),
        }
        if expiration_ts:
            body["expiration_time"] = int(expiration_ts)
        return await self.request("POST", f"/portfolio/events/orders/{order_id}/amend", body=body, auth=True)

    async def create_order_group(self, contracts_limit: int) -> dict[str, Any]:
        return await self.request(
            "POST", "/portfolio/order_groups/create", body={"contracts_limit": contracts_limit}, auth=True
        )
