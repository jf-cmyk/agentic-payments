from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from src.cex_stream_cache import CEXBookCache, CEXBookUnavailable, KrakenV2BookStream
from src.rwa_adapters import KrakenSpotAdapter, KrakenXStocksAdapter, RWAAdapterBlockedError, RevolutXAdapter


def test_cex_cache_rejects_sequence_gaps_and_crossed_books() -> None:
    cache = CEXBookCache(ttl_seconds=60)
    cache.apply_snapshot(
        "venue",
        "BTC/USD",
        bids=[{"price": 100, "size": 2}],
        asks=[{"price": 101, "size": 3}],
        sequence=5,
    )
    with pytest.raises(CEXBookUnavailable, match="sequence gap"):
        cache.apply_update(
            "venue",
            "BTC/USD",
            bids=[{"price": 100, "size": 1}],
            asks=[],
            sequence=7,
        )
    with pytest.raises(CEXBookUnavailable, match="no valid"):
        cache.get("venue", "BTC/USD")


def test_cex_cache_rejects_stale_state() -> None:
    cache = CEXBookCache(ttl_seconds=1)
    cache.apply_snapshot(
        "venue",
        "BTC/USD",
        bids=[{"price": 100, "size": 2}],
        asks=[{"price": 101, "size": 3}],
    )
    cache._books[("venue", "BTC/USD")].received_at = datetime.now(UTC) - timedelta(seconds=2)
    with pytest.raises(CEXBookUnavailable, match="stale"):
        cache.get("venue", "BTC/USD")


def test_kraken_v2_decoder_applies_snapshot_and_update() -> None:
    cache = CEXBookCache(ttl_seconds=60)
    stream = KrakenV2BookStream(cache, symbols=["AAPLx/USD"])
    stream.handle_message({
        "channel": "book",
        "type": "snapshot",
        "data": [{
            "symbol": "AAPLx/USD",
            "bids": [{"price": 199.0, "qty": 5.0}],
            "asks": [{"price": 201.0, "qty": 4.0}],
            "sequence": 10,
        }],
    })
    stream.handle_message({
        "channel": "book",
        "type": "update",
        "data": [{
            "symbol": "AAPLx/USD",
            "bids": [{"price": 200.0, "qty": 2.0}],
            "asks": [],
            "sequence": 11,
        }],
    })
    assert cache.get("kraken_xstocks", "AAPLx/USD")["bids"][0] == {"price": 200.0, "size": 2.0}


def test_kraken_v2_decoder_builds_fresh_trade_vwap() -> None:
    cache = CEXBookCache(ttl_seconds=60)
    stream = KrakenV2BookStream(cache, symbols=["BTC/USD"], venue_id="kraken_spot")
    now = datetime.now(UTC).isoformat()
    stream.handle_message({
        "channel": "trade",
        "type": "update",
        "data": [
            {"symbol": "BTC/USD", "price": 100, "qty": 2, "timestamp": now, "trade_id": 1},
            {"symbol": "BTC/USD", "price": 110, "qty": 1, "timestamp": now, "trade_id": 2},
        ],
    })
    row = cache.trade_vwap("kraken_spot", "BTC/USD")
    assert row["trade_count"] == 2
    assert row["vwap"] == pytest.approx(103.333333333)


def test_kraken_spot_does_not_append_xstocks_suffix() -> None:
    assert KrakenSpotAdapter.normalize_symbol("BTCUSD") == "BTC/USD"
    assert KrakenSpotAdapter.normalize_symbol("ETH/EUR") == "ETH/EUR"


@pytest.mark.asyncio
async def test_kraken_adapter_prefers_fresh_streamed_book() -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/0/public/AssetPairs":
            return httpx.Response(200, json={"error": [], "result": {"AAPLXUSD": {"wsname": "AAPLx/USD"}}})
        return httpx.Response(500)

    cache = CEXBookCache(ttl_seconds=60)
    cache.apply_snapshot(
        "kraken_xstocks",
        "AAPLx/USD",
        bids=[{"price": 199, "size": 2}],
        asks=[{"price": 201, "size": 3}],
        sequence=12,
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = KrakenXStocksAdapter(client=client, stream_cache=cache)
        quote = await adapter.fetch_bidask("AAPL/USD")
        book = await adapter.fetch_order_book("AAPL/USD", side="buy")

    assert quote["bid"] == 199
    assert book["levels"] == [{"price": 201.0, "size": 3.0}]
    assert quote["metadata"]["transport"] == "websocket"
    assert calls == ["/0/public/AssetPairs", "/0/public/AssetPairs"]


def _private_key_pem() -> str:
    return Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()


def test_revolut_x_without_credentials_is_explicitly_blocked() -> None:
    adapter = RevolutXAdapter(api_key="", private_key_pem="")
    assert adapter.metadata()["status"] == "implemented_blocked_on_credentials"
    with pytest.raises(RWAAdapterBlockedError, match="REVOLUT_X_API_KEY"):
        adapter._headers("GET", "/api/1.0/order-book/BTC-USD")


@pytest.mark.asyncio
async def test_revolut_x_normalizes_signed_order_book() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Revx-API-Key"] == "test-key"
        assert request.headers["X-Revx-Signature"]
        return httpx.Response(200, json={
            "data": {
                "bids": [{"p": "100", "q": "2"}],
                "asks": [{"p": "101", "q": "3"}],
            },
            "metadata": {"timestamp": 123456789},
        })

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = RevolutXAdapter(api_key="test-key", private_key_pem=_private_key_pem(), client=client)
        quote = await adapter.fetch_bidask("BTC/USD")
        book = await adapter.fetch_order_book("BTC/USD", side="buy", depth=20)

    assert quote["bid"] == 100
    assert quote["ask"] == 101
    assert book["levels"] == [{"price": 101.0, "size": 3.0}]
    assert book["metadata"]["transport"] == "signed_rest"


@pytest.mark.asyncio
async def test_revolut_x_public_pair_discovery_uses_bearer_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Bearer test-key"
        assert request.url.path == "/api/1.0/public/configuration/pairs"
        return httpx.Response(200, json={"BTC/USD": {"status": "active"}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        adapter = RevolutXAdapter(api_key="test-key", client=client)
        pairs = await adapter.discover_pairs()

    assert pairs["BTC/USD"]["status"] == "active"
