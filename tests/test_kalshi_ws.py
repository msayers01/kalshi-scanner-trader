"""Real loopback WebSocket server: subscriptions, gaps, reconnect and shutdown. No external traffic."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import itertools
import json
from collections.abc import Callable

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from websockets.asyncio.server import ServerConnection, serve

from kst.kalshi.auth import KalshiSigner
from kst.kalshi.ws import KalshiWs
from tests.conftest import Harness


async def eventually(check: Callable[[], bool]) -> None:
    async with asyncio.timeout(5):
        while not check():  # noqa: ASYNC110 - bounded assertion across the real socket/client tasks
            await asyncio.sleep(0.01)


class Peer:
    def __init__(self) -> None:
        self.key = Ed25519PrivateKey.generate()
        self.commands: asyncio.Queue[tuple[ServerConnection, dict]] = asyncio.Queue()
        self.connections: asyncio.Queue[ServerConnection] = asyncio.Queue()
        self.sids = itertools.count(10)
        self.auto_ack = True
        self.messages: list[dict] = []
        self.states: list[bool] = []
        self.gaps: list[str] = []
        self.client: KalshiWs
        self.task: asyncio.Task | None = None

    async def handle(self, conn: ServerConnection) -> None:
        await self.connections.put(conn)
        async for raw in conn:
            cmd = json.loads(raw)
            await self.commands.put((conn, cmd))
            if cmd["cmd"] == "subscribe" and self.auto_ack:
                await self.ack(conn, cmd)

    async def ack(self, conn: ServerConnection, cmd: dict) -> int:
        sid = next(self.sids)
        await conn.send(
            json.dumps(
                {
                    "type": "subscribed",
                    "id": cmd["id"],
                    "msg": {"channel": cmd["params"]["channels"][0], "sid": sid},
                }
            )
        )
        return sid

    def start(self) -> None:
        self.task = asyncio.create_task(self.client.run())

    async def command(self) -> tuple[ServerConnection, dict]:
        return await asyncio.wait_for(self.commands.get(), 5)


@pytest.fixture
async def peer():
    p = Peer()
    async with serve(p.handle, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        p.client = KalshiWs(
            f"ws://127.0.0.1:{port}/trade-api/ws/v2",
            KalshiSigner("local-test-key", p.key),
            {kind: p.messages.append for kind in ("fill", "orderbook_snapshot", "orderbook_delta")},
            on_state=lambda connected, _: p.states.append(connected),
            on_gap=p.gaps.append,
        )
        try:
            yield p
        finally:
            if p.task:
                p.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await p.task


async def test_handshake_subscriptions_focus_updates_and_shutdown(peer: Peer) -> None:
    p = peer
    p.client.want_channel("fill")
    p.client.want_channel("user_orders")
    p.client.set_focus({"B", "A"})
    p.start()
    conn = await asyncio.wait_for(p.connections.get(), 5)
    headers = conn.request.headers
    signed = (headers["KALSHI-ACCESS-TIMESTAMP"] + "GET/trade-api/ws/v2").encode()
    p.key.public_key().verify(base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"]), signed)
    assert headers["KALSHI-ACCESS-KEY"] == "local-test-key"
    commands = [(await p.command())[1] for _ in range(4)]
    assert {c["params"]["channels"][0] for c in commands} == {
        "fill",
        "user_orders",
        "orderbook_delta",
        "trade",
    }
    book = next(c for c in commands if c["params"]["channels"] == ["orderbook_delta"])
    assert book["params"]["use_yes_price"] and book["params"]["market_tickers"] == ["A", "B"]
    await eventually(lambda: len(p.client._channel_sid) == 4)
    p.client.set_focus({"B", "C"})
    updates = [(await p.command())[1] for _ in range(4)]
    assert all(c["cmd"] == "update_subscription" for c in updates)
    assert [(c["params"]["action"], c["params"]["market_tickers"]) for c in updates] == [
        ("add_markets", ["C"]),
        ("delete_markets", ["A"]),
        ("add_markets", ["C"]),
        ("delete_markets", ["A"]),
    ]
    p.client.set_focus(set())
    cancels = [(await p.command())[1] for _ in range(2)]
    assert all(c["cmd"] == "unsubscribe" for c in cancels)
    p.task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await p.task
    assert not p.client.connected and p.states == [True, False]
    assert not p.client._tasks


async def test_gap_quarantines_old_subscription_until_fresh_snapshot(peer: Peer) -> None:
    p = peer
    p.client.set_focus({"M"})
    p.start()
    conn = await asyncio.wait_for(p.connections.get(), 5)
    await eventually(lambda: len(p.client._channel_sid) == 2)
    for _ in range(2):
        await p.command()
    sid = p.client._channel_sid["orderbook_delta"]
    for seq in (1, 3, 4):
        await conn.send(
            json.dumps(
                {
                    "type": "orderbook_snapshot" if seq == 1 else "orderbook_delta",
                    "sid": sid,
                    "seq": seq,
                    "msg": {"ticker": "M", "marker": seq},
                }
            )
        )
    await eventually(lambda: p.client._channel_sid.get("orderbook_delta", sid) != sid)
    fresh_sid = p.client._channel_sid["orderbook_delta"]
    await conn.send(json.dumps({"type": "orderbook_delta", "sid": sid, "seq": 5, "msg": {}}))
    await conn.send(json.dumps({"type": "orderbook_snapshot", "sid": fresh_sid, "seq": 1, "msg": {}}))
    await eventually(lambda: len(p.messages) == 2)
    assert p.gaps == ["orderbook_delta"]
    assert [(m["sid"], m["seq"]) for m in p.messages] == [(sid, 1), (fresh_sid, 1)]
    commands = [(await p.command())[1] for _ in range(2)]
    assert [c["cmd"] for c in commands] == ["unsubscribe", "subscribe"]


async def test_reconnect_restores_desired_subscriptions_and_resets_sequence(peer: Peer) -> None:
    p = peer
    p.client.want_channel("fill")
    p.client.set_focus({"M"})
    p.start()
    first = await asyncio.wait_for(p.connections.get(), 5)
    await eventually(lambda: len(p.client._channel_sid) == 3)
    for _ in range(3):
        await p.command()
    sid = p.client._channel_sid["fill"]
    await first.send(json.dumps({"type": "fill", "sid": sid, "seq": 200, "msg": {"trade_id": "a"}}))
    await eventually(lambda: len(p.messages) == 1)
    await first.close()
    second = await asyncio.wait_for(p.connections.get(), 5)
    commands = [(await p.command())[1] for _ in range(3)]
    assert {c["params"]["channels"][0] for c in commands} == {"fill", "orderbook_delta", "trade"}
    await eventually(lambda: len(p.client._channel_sid) == 3)
    sid = p.client._channel_sid["fill"]
    await second.send(json.dumps({"type": "fill", "sid": sid, "seq": 1, "msg": {"trade_id": "b"}}))
    await eventually(lambda: len(p.messages) == 2)
    assert p.states == [True, False, True] and not p.gaps


async def test_focus_changes_during_subscribe_are_applied_after_ack(peer: Peer) -> None:
    p = peer
    p.auto_ack = False
    p.client.set_focus({"A"})
    p.start()
    initial = [await p.command() for _ in range(2)]
    p.client.set_focus({"B"})  # initial subscription has been sent, but not acknowledged
    for conn, cmd in initial:
        await p.ack(conn, cmd)
    updates = [(await p.command())[1] for _ in range(4)]
    assert all(c["cmd"] == "update_subscription" for c in updates)
    assert [c["params"]["market_tickers"] for c in updates] == [["B"], ["A"], ["B"], ["A"]]


async def test_market_data_gap_blocks_risk_until_fresh_snapshot(peer: Peer, h: Harness) -> None:
    p = peer
    h.add_market("M")
    h.md.ws, h.md.mode = p.client, "ws"
    p.client.handlers = h.md.ws_handlers()
    p.client.on_state, p.client.on_gap = h.md.on_ws_state, h.md.on_ws_gap
    p.client.set_focus({"M"})
    p.start()
    conn = await asyncio.wait_for(p.connections.get(), 5)
    await eventually(lambda: len(p.client._channel_sid) == 2)
    sid = p.client._channel_sid["orderbook_delta"]
    snapshot = {
        "market_ticker": "M",
        "yes_dollars_fp": [["0.38", "10.00"]],
        "no_dollars_fp": [["0.42", "10.00"]],
    }
    await conn.send(json.dumps({"type": "orderbook_snapshot", "sid": sid, "seq": 1, "msg": snapshot}))
    await eventually(lambda: h.md.is_fresh("M", h.clock.now_ms()))
    await conn.send(json.dumps({"type": "orderbook_delta", "sid": sid, "seq": 3, "msg": {}}))
    await eventually(lambda: not h.md.is_fresh("M", h.clock.now_ms()))
    await eventually(lambda: p.client._channel_sid.get("orderbook_delta", sid) != sid)
    from kst.core.fixedpoint import Price, Qty
    from kst.core.types import BookSide
    from kst.domain.orders import Order

    rejected = Order("stale", "M", BookSide.BID, Price(4000), Qty(100))
    assert not (await h.oms.submit(rejected)).ok
    fresh_sid = p.client._channel_sid["orderbook_delta"]
    await conn.send(json.dumps({"type": "orderbook_snapshot", "sid": fresh_sid, "seq": 1, "msg": snapshot}))
    await eventually(lambda: h.md.is_fresh("M", h.clock.now_ms()))
    await h.md.rest.aclose()
