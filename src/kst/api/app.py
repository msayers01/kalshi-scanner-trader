"""FastAPI app: REST (state, scanner, trading, control) + WebSocket (/ws) + static UI (web/dist).

Security: binds to 127.0.0.1 by default. If `web.api_token` is set, every control/trading endpoint and
the WebSocket require it (Authorization: Bearer <token> or ?token=). Non-local binds REQUIRE a token.
"""

from __future__ import annotations

import hmac
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal

from fastapi import Depends, FastAPI, HTTPException, Query, Request, WebSocket, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from kst.api import serialize as ser
from kst.config import REPO_ROOT
from kst.core.types import Actor, BookSide, TimeInForce

if TYPE_CHECKING:
    from kst.app import Application

WEB_DIST = REPO_ROOT / "web" / "dist"


class KillBody(BaseModel):
    reason: str = ""
    flatten: bool = False


class ReasonBody(BaseModel):
    reason: str = ""


class AutotradeBody(BaseModel):
    on: bool


class LimitsBody(BaseModel):
    changes: dict[str, Any]


class OrderBody(BaseModel):
    ticker: str
    side: Literal["yes", "no"]  # buy YES or buy NO
    price: float = Field(gt=0, lt=1, description="Price of the side being bought, in dollars")
    contracts: float = Field(gt=0)
    tif: Literal["gtc", "ioc"] = "gtc"
    post_only: bool = False


def create_app(app: Application) -> FastAPI:
    cfg = app.settings.web
    token = cfg.api_token.get_secret_value() if cfg.api_token else ""
    if cfg.host not in ("127.0.0.1", "localhost", "::1") and not token:
        raise RuntimeError("web.host is not local: set KST_WEB__API_TOKEN to protect the control endpoints")

    def _check(supplied: str | None) -> None:
        if token and not (supplied and hmac.compare_digest(supplied, token)):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid or missing API token")

    def auth(request: Request) -> str:
        h = request.headers.get("authorization", "")
        supplied = h[7:] if h.lower().startswith("bearer ") else request.query_params.get("token")
        _check(supplied)
        return request.client.host if request.client else "web"

    api = FastAPI(title="kst", version="0.1.0", docs_url="/api/docs", openapi_url="/api/openapi.json")
    api.add_middleware(
        CORSMiddleware, allow_origins=cfg.cors_origins, allow_methods=["*"], allow_headers=["*"]
    )
    hub = app.hub
    read = [Depends(auth)] if token else []  # with a token configured, reads are protected too

    # ------------------------------------------------------------------ read endpoints

    @api.get("/api/health")
    def health() -> dict[str, Any]:
        return app.health()

    @api.get("/api/status", dependencies=read)
    def status_() -> dict[str, Any]:
        return {**app.control.status(), "health": app.health()}

    @api.get("/api/scan", dependencies=read)
    def scan(limit: int = Query(100, le=500), category: str | None = None) -> dict[str, Any]:
        out = ser.scan(app, limit if not category else 500)
        if category:
            out["rows"] = [r for r in out["rows"] if r["category"] == category][:limit]
        return out

    @api.get("/api/markets/{ticker}", dependencies=read)
    def market(ticker: str) -> dict[str, Any]:
        app.md.add_focus("ui", {ticker})
        d = ser.market_detail(app, ticker)
        if d is None:
            raise HTTPException(404, "unknown market")
        return d

    @api.get("/api/positions", dependencies=read)
    def positions() -> list[dict[str, Any]]:
        return ser.positions(app)

    @api.get("/api/orders", dependencies=read)
    def orders(n: int = Query(100, le=1000)) -> list[dict[str, Any]]:
        return ser.orders(app, n)

    @api.get("/api/fills", dependencies=read)
    async def fills(n: int = Query(100, le=1000)) -> list[dict[str, Any]]:
        rows = await app.repo.recent_fills(n)
        return [
            {
                "id": r.fill_id,
                "ticker": r.ticker,
                "side": r.side,
                "price": r.price / 10_000,
                "qty": r.qty / 100,
                "fee": r.fee / 1e6,
                "taker": r.is_taker,
                "strategy": r.strategy,
                "ts_ms": r.ts_ms,
            }
            for r in rows
        ]

    @api.get("/api/pnl", dependencies=read)
    async def pnl(days: int = Query(30, le=365)) -> dict[str, Any]:
        hist = await app.repo.daily_pnl(days)
        eq = await app.repo.equity_history(app.clock.now_ms() - 3 * 86_400_000)
        return {
            "today": app.portfolio.daily_report().to_dict(),
            "summary": app.portfolio.summary().to_dict(),
            "history": [r.report for r in hist],
            "equity": [[r.ts_ms, r.equity] for r in eq],
        }

    @api.get("/api/limits", dependencies=read)
    def limits() -> dict[str, Any]:
        return {
            "limits": app.risk.limits.model_dump(),
            "describe": app.risk.limits.describe(),
            "rejections": app.risk.rejections,
        }

    @api.get("/api/scorecard", dependencies=read)
    async def scorecard() -> dict[str, Any]:
        from kst.portfolio.scorecard import paper_scorecard

        if app.mode.value not in ("paper", "replay"):
            raise HTTPException(409, "Scorecard requires paper or replay mode")
        return await paper_scorecard(app.repo, app.clock.now_ms(), app.mode.value)

    @api.get("/api/llm", dependencies=read)
    def llm() -> dict[str, Any]:
        return {"status": app.gate.status(), "verdicts": [v.to_dict() for v in app.gate.verdicts.values()]}

    @api.get("/api/llm/packet", dependencies=read, response_class=PlainTextResponse)
    def llm_packet(top: int = Query(20, le=60)) -> str:
        return app.export_llm_packet(top)

    @api.get("/api/audit", dependencies=read)
    async def audit(n: int = Query(100, le=1000)) -> list[dict[str, Any]]:
        return [
            {"ts_ms": r.ts_ms, "actor": r.actor, "action": r.action, "payload": r.payload}
            for r in await app.repo.audit_log(n)
        ]

    # ------------------------------------------------------------------ control (auth)

    @api.post("/api/control/pause")
    async def pause(body: ReasonBody, who: str = Depends(auth)) -> dict[str, Any]:
        return await app.control.pause(Actor.WEB, body.reason or "web", who=who)

    @api.post("/api/control/resume")
    async def resume(who: str = Depends(auth)) -> dict[str, Any]:
        return await app.control.resume(Actor.WEB, who=who)

    @api.post("/api/control/kill")
    async def kill(body: KillBody, who: str = Depends(auth)) -> dict[str, Any]:
        return await app.control.kill(
            Actor.WEB, body.reason or "web kill switch", flatten=body.flatten, who=who
        )

    @api.post("/api/control/flatten")
    async def flatten(who: str = Depends(auth)) -> dict[str, Any]:
        return await app.control.flatten(Actor.WEB, who=who)

    @api.post("/api/control/autotrade")
    async def autotrade(body: AutotradeBody, who: str = Depends(auth)) -> dict[str, Any]:
        return await app.control.set_autotrade(body.on, Actor.WEB, who=who)

    @api.post("/api/limits")
    async def set_limits(body: LimitsBody, who: str = Depends(auth)) -> dict[str, Any]:
        try:
            return await app.control.set_limits(body.changes, Actor.WEB, who=who)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @api.post("/api/orders")
    async def place(body: OrderBody, who: str = Depends(auth)) -> dict[str, Any]:
        # UI speaks "buy YES at p" / "buy NO at p"; the engine is YES-space: buy NO at p ≡ ASK at 1 − p.
        side = BookSide.BID if body.side == "yes" else BookSide.ASK
        yes_price = body.price if body.side == "yes" else 1.0 - body.price
        return await app.control.manual_order(
            body.ticker,
            side,
            yes_price,
            body.contracts,
            tif=TimeInForce.IOC if body.tif == "ioc" else TimeInForce.GTC,
            post_only=body.post_only,
            actor=Actor.WEB,
            who=who,
        )

    @api.delete("/api/orders/{coid}")
    async def cancel(coid: str, who: str = Depends(auth)) -> dict[str, Any]:
        return await app.control.cancel_order(coid, Actor.WEB, who=who)

    @api.post("/api/orders/cancel_all")
    async def cancel_all(who: str = Depends(auth)) -> dict[str, Any]:
        return await app.control.cancel_all_orders(Actor.WEB, who=who)

    # ------------------------------------------------------------------ websocket + UI

    @api.websocket("/ws")
    async def ws(websocket: WebSocket, token_q: str | None = Query(None, alias="token")) -> None:
        if token and not (token_q and hmac.compare_digest(token_q, token)):
            await websocket.close(code=4401)
            return
        await hub.handle(websocket)

    @api.get("/api/time")
    def server_time() -> dict[str, float]:
        return {"server_ms": time.time() * 1000}

    if cfg.serve_ui and WEB_DIST.is_dir():
        api.mount("/assets", StaticFiles(directory=WEB_DIST / "assets"), name="assets")

        @api.get("/{path:path}", include_in_schema=False)
        def spa(path: str) -> FileResponse:
            f = WEB_DIST / path
            if path and f.is_file() and Path(f).resolve().is_relative_to(WEB_DIST.resolve()):
                return FileResponse(f)
            return FileResponse(WEB_DIST / "index.html")

    return api
