from __future__ import annotations

import base64
import json
from pathlib import Path

import httpx
import pytest
import respx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, padding, rsa

from kst.core.types import BookSide, MarketStatus, OrderStatus, StrikeType, TimeInForce
from kst.kalshi.auth import KalshiSigner
from kst.kalshi.parse import (
    parse_event,
    parse_fill,
    parse_market,
    parse_order,
    parse_rest_orderbook,
    parse_trade,
    parse_ws_delta,
    parse_ws_snapshot,
)
from kst.kalshi.ratelimit import KalshiRateLimiter
from kst.kalshi.rest import KalshiApiError, KalshiRest

FIX = Path(__file__).parent / "fixtures"
BASE = "https://api.test/trade-api/v2"


def _rsa_signer() -> tuple[KalshiSigner, rsa.RSAPublicKey]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    return KalshiSigner.from_pem("kid", pem), key.public_key()


def test_rsa_pss_signature_verifies() -> None:
    signer, pub = _rsa_signer()
    h = signer.headers("GET", "/trade-api/v2/portfolio/orders?limit=5", timestamp_ms=1_700_000_000_000)
    assert h["KALSHI-ACCESS-KEY"] == "kid" and h["KALSHI-ACCESS-TIMESTAMP"] == "1700000000000"
    msg = b"1700000000000GET/trade-api/v2/portfolio/orders"  # query string NOT signed
    pub.verify(
        base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]),
        msg,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_ed25519_signature_verifies_and_full_url_path() -> None:
    key = ed25519.Ed25519PrivateKey.generate()
    signer = KalshiSigner("kid", key)
    assert signer.key_type == "ed25519"
    h = signer.headers(
        "post", "https://x.kalshi.com/trade-api/v2/portfolio/events/orders?a=1", timestamp_ms=5
    )
    key.public_key().verify(
        base64.b64decode(h["KALSHI-ACCESS-SIGNATURE"]), b"5POST/trade-api/v2/portfolio/events/orders"
    )


# ------------------------------------------------------------------ parsing real payloads


def test_parse_market_fixture() -> None:
    d = json.loads((FIX / "market_btcd.json").read_text())
    m = parse_market(d)
    assert m.ticker == "KXBTCD-26OCT1817-T91249.99"
    assert m.series_ticker == "KXBTCD"
    assert m.status is MarketStatus.ACTIVE and m.is_tradable
    assert m.strike_type is StrikeType.GREATER and m.floor_strike == pytest.approx(91249.99)
    assert m.yes_bid is None and m.yes_ask == 100  # "0.0000" bid = empty side
    assert m.grid.is_valid(100) and not m.grid.is_valid(150)
    assert m.volume_24h == 500 and "sixty seconds" in m.rules_primary


def test_parse_event_fixture() -> None:
    d = json.loads((FIX / "event_highny.json").read_text())
    ev, markets = parse_event(d)
    assert ev.series_ticker == "KXHIGHNY" and ev.mutually_exclusive
    assert len(markets) == len(ev.market_tickers) == 6
    assert {m.category for m in markets} == {ev.category}
    kinds = {m.strike_type for m in markets}
    assert {StrikeType.LESS, StrikeType.BETWEEN, StrikeType.GREATER} <= kinds


def test_parse_rest_orderbook_converts_no_leg() -> None:
    d = json.loads((FIX / "orderbook_rest.json").read_text())
    bids, asks = parse_rest_orderbook(d)
    assert bids == []
    # NO bid at 0.99 → YES ask at 0.01; best ask is the min
    assert min(p for p, _ in asks) == 100
    assert dict(asks)[200] == 32745  # NO 0.98 × 327.45


def test_parse_ws_book_messages() -> None:
    snap = {"yes_dollars_fp": [["0.0800", "300.00"]], "no_dollars_fp": [["0.5400", "20.00"]]}
    bids, asks = parse_ws_snapshot(snap, use_yes_price=True)
    assert bids == [(800, 30000)] and asks == [(5400, 2000)]
    _, asks_legacy = parse_ws_snapshot(snap, use_yes_price=False)
    assert asks_legacy == [(4600, 2000)]
    side, price, delta = parse_ws_delta({"price_dollars": "0.9600", "delta_fp": "-54.00", "side": "yes"})
    assert side is BookSide.BID and price == 9600 and delta == -5400
    side, price, _ = parse_ws_delta({"price_dollars": "0.7000", "delta_fp": "1.00", "side": "no"})
    assert side is BookSide.ASK and price == 7000


def test_parse_fill_order_trade() -> None:
    f = parse_fill(
        {
            "trade_id": "t1",
            "order_id": "o1",
            "client_order_id": "kst-et-abc",
            "market_ticker": "M",
            "is_taker": True,
            "yes_price_dollars": "0.7500",
            "count_fp": "278.00",
            "fee_cost": "0.010000",
            "book_side": "bid",
            "ts_ms": 1671899397000,
        }
    )
    assert f.side is BookSide.BID and f.price == 7500 and f.qty == 27800 and f.fee == 10_000
    assert f.strategy == "edge_taker"
    o = parse_order(
        {
            "order_id": "o1",
            "client_order_id": "c1",
            "ticker": "M",
            "status": "executed",
            "book_side": "ask",
            "yes_price_dollars": "0.3500",
            "fill_count_fp": "10.00",
            "remaining_count_fp": "0.00",
            "initial_count_fp": "10.00",
            "taker_fees_dollars": "0.0200",
            "maker_fees_dollars": "0",
            "created_time": "2024-12-01T10:00:00Z",
        }
    )
    assert o.status is OrderStatus.FILLED and o.side is BookSide.ASK and o.filled == 1000 and o.fees == 20_000
    t = parse_trade(
        {
            "market_ticker": "M",
            "yes_price_dollars": "0.36",
            "count_fp": "136.00",
            "taker_book_side": "ask",
            "ts_ms": 9,
        }
    )
    assert t == ("M", 3600, 13600, BookSide.ASK, 9)


# ------------------------------------------------------------------ REST client


def _client(signer: KalshiSigner | None = None) -> KalshiRest:
    return KalshiRest(BASE, signer=signer, limiter=KalshiRateLimiter(10_000, 10_000))


@respx.mock
async def test_rest_pagination_and_public_no_auth() -> None:
    route = respx.get(f"{BASE}/markets").mock(
        side_effect=[
            httpx.Response(200, json={"markets": [{"ticker": "A"}], "cursor": "c1"}),
            httpx.Response(200, json={"markets": [{"ticker": "B"}], "cursor": ""}),
        ]
    )
    rest = _client()
    got = [m["ticker"] async for m in rest.markets(status="open")]
    assert got == ["A", "B"]
    assert route.calls[1].request.url.params["cursor"] == "c1"
    assert "KALSHI-ACCESS-KEY" not in route.calls[0].request.headers
    await rest.aclose()


@respx.mock
async def test_rest_retry_on_429_and_error_mapping() -> None:
    respx.get(f"{BASE}/exchange/status").mock(
        side_effect=[
            httpx.Response(429, json={"error": "too many requests"}),
            httpx.Response(200, json={"trading_active": True}),
        ]
    )
    respx.get(f"{BASE}/markets/X").mock(
        return_value=httpx.Response(404, json={"error": {"code": "not_found", "message": "nope"}})
    )
    rest = _client()
    assert (await rest.exchange_status())["trading_active"] is True
    with pytest.raises(KalshiApiError) as ei:
        await rest.market("X")
    assert ei.value.status == 404 and ei.value.code == "not_found"
    await rest.aclose()


@respx.mock
async def test_create_order_v2_body_and_auth() -> None:
    signer, _ = _rsa_signer()
    route = respx.post(f"{BASE}/portfolio/events/orders").mock(
        return_value=httpx.Response(
            201, json={"order_id": "o1", "fill_count": "0.00", "remaining_count": "10.00", "ts_ms": 1}
        )
    )
    rest = _client(signer)
    await rest.create_order(
        ticker="M",
        side=BookSide.ASK,
        price=5600,
        qty=1000,
        client_order_id="kst-x-1",
        tif=TimeInForce.GTC,
        post_only=True,
        expiration_ts=1_800_000_000,
    )
    req = route.calls[0].request
    body = json.loads(req.content)
    assert body["side"] == "ask" and body["price"] == "0.5600" and body["count"] == "10.00"
    assert body["time_in_force"] == "good_till_canceled" and body["expiration_time"] == 1_800_000_000
    assert body["cancel_order_on_pause"] is True and body["self_trade_prevention_type"] == "taker_at_cross"
    assert req.headers["KALSHI-ACCESS-KEY"] == "kid"
    await rest.aclose()
