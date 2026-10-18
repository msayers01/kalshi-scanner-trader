"""Web API: status, control endpoints, limits validation, manual orders, token auth."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from kst.api.app import create_app
from kst.app import Application
from kst.config import Settings
from kst.core.types import EngineState, MarketStatus, StrikeType
from kst.domain.market import Event, Market
from kst.domain.orderbook import OrderBook


def _app(tmp_path: Path, token: str | None = None) -> Application:
    s = Settings()
    s.app.data_dir = tmp_path
    s.llm.mode = "off"
    s.web.serve_ui = False
    if token:
        from pydantic import SecretStr

        s.web.api_token = SecretStr(token)
    return Application(s)


def _client(app: Application) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(app)), base_url="http://test")


async def test_status_and_controls(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with _client(app) as c:
        st = (await c.get("/api/status")).json()
        assert st["mode"] == "paper" and st["state"] == "running"
        assert (await c.post("/api/control/pause", json={"reason": "t"})).json()["state"] == "paused"
        assert app.state.get() is EngineState.PAUSED
        await c.post("/api/control/resume")
        r = (await c.post("/api/control/kill", json={"reason": "t"})).json()
        assert r["state"] == "killed" and app.state.get() is EngineState.KILLED
        assert (await c.get("/api/health")).status_code == 200


async def test_limits_validation(tmp_path: Path) -> None:
    app = _app(tmp_path)
    async with _client(app) as c:
        ok = (await c.post("/api/limits", json={"changes": {"max_markets_per_day": 3}})).json()
        assert ok["changed"] == {"max_markets_per_day": 3} and app.risk.limits.max_markets_per_day == 3
        bad = await c.post("/api/limits", json={"changes": {"not_a_limit": 1}})
        assert bad.status_code == 422
        neg = await c.post("/api/limits", json={"changes": {"max_order_usd": -5}})
        assert neg.status_code == 422


async def test_manual_order_goes_through_risk(tmp_path: Path) -> None:
    app = _app(tmp_path)
    now = app.clock.now_ms()
    m = Market(
        ticker="M",
        event_ticker="E-1",
        series_ticker="E",
        title="Test",
        status=MarketStatus.ACTIVE,
        strike_type=StrikeType.GREATER,
        close_time_ms=now + 3_600_000,
    )
    app.catalog.markets["M"] = m
    app.catalog.events["E-1"] = Event("E-1", "E", market_tickers=["M"])
    book = OrderBook("M")
    book.apply_snapshot([(4000, 5000)], [(4200, 5000)], now)
    app.md.books["M"] = book
    await app.broker.start()
    async with _client(app) as c:
        r = (
            await c.post(
                "/api/orders",
                json={"ticker": "M", "side": "yes", "price": 0.42, "contracts": 5, "tif": "ioc"},
            )
        ).json()
        assert r["ok"] and r["filled"] == 5
        # Buy NO at 0.60 ≡ sell YES at 0.40 (closes the YES position)
        r2 = (
            await c.post(
                "/api/orders", json={"ticker": "M", "side": "no", "price": 0.60, "contracts": 5, "tif": "ioc"}
            )
        ).json()
        assert r2["ok"] and app.portfolio.positions["M"].qty == 0
        # Fat-finger: buying YES 30¢ above the mid is refused by risk
        r3 = (
            await c.post(
                "/api/orders",
                json={"ticker": "M", "side": "yes", "price": 0.71, "contracts": 1, "tif": "ioc"},
            )
        ).json()
        assert not r3["ok"] and "FatFinger" in r3["reason"]


async def test_token_required_when_configured(tmp_path: Path) -> None:
    app = _app(tmp_path, token="s3cret")
    async with _client(app) as c:
        assert (await c.get("/api/status")).status_code == 401
        assert (await c.post("/api/control/kill", json={})).status_code == 401
        ok = await c.get("/api/status", headers={"Authorization": "Bearer s3cret"})
        assert ok.status_code == 200
        assert (await c.get("/api/health")).status_code == 200  # liveness stays open


def test_non_local_bind_requires_token(tmp_path: Path) -> None:
    app = _app(tmp_path)
    app.settings.web.host = "0.0.0.0"
    with pytest.raises(RuntimeError, match="API_TOKEN"):
        create_app(app)


async def test_cross_site_requests_cannot_drive_controls_without_a_token(tmp_path: Path) -> None:
    app = _app(tmp_path)
    transport = httpx.ASGITransport(app=create_app(app))
    async with httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8787") as c:
        # Any web page can POST to localhost, and a bodiless POST needs no CORS preflight.
        evil = await c.post("/api/control/flatten", headers={"Origin": "https://evil.example"})
        assert evil.status_code == 403
        # DNS rebinding: Origin and Host both carry the attacker's name.
        rebind = await c.post(
            "/api/control/kill",
            json={},
            headers={"Origin": "http://evil.example:8787", "Host": "evil.example:8787"},
        )
        assert rebind.status_code == 403 and app.state.get() is EngineState.RUNNING
        same = await c.post("/api/control/pause", json={}, headers={"Origin": "http://127.0.0.1:8787"})
        dev = await c.post("/api/control/resume", headers={"Origin": "http://localhost:5173"})
        cli = await c.post("/api/control/pause", json={})  # curl / scripts send no Origin
        assert same.status_code == dev.status_code == cli.status_code == 200
