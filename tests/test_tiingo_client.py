"""Unit tests for the Tiingo real-time equity client."""

from __future__ import annotations

import httpx
import pytest

from src.tiingo_client import TiingoAPIError, TiingoClient


@pytest.mark.asyncio
async def test_get_equity_snapshot_parses_top_of_book() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["Authorization"] == "Token test-tiingo-key"
        assert request.url.path == "/iex/LCID"
        return httpx.Response(
            200,
            json=[
                {
                    "ticker": "LCID",
                    "timestamp": "2026-07-21T17:00:01Z",
                    "quoteTimestamp": "2026-07-21T17:00:00.900Z",
                    "lastSaleTimestamp": "2026-07-21T17:00:00.500Z",
                    "bidPrice": 7.14,
                    "askPrice": 7.16,
                    "mid": 7.15,
                    "last": 7.15,
                    "tngoLast": 7.15,
                    "volume": 12_345_678,
                    "bidSize": 100,
                    "askSize": 200,
                }
            ],
        )

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.tiingo.com",
    ) as http_client:
        client = TiingoClient(
            api_key="test-tiingo-key",
            base_url="https://api.tiingo.com/iex",
            client=http_client,
        )
        row = await client.get_equity_snapshot("lcid")

    assert row["ticker"] == "LCID"
    assert row["bid"] == 7.14
    assert row["ask"] == 7.16
    assert row["reference_price"] == 7.15
    assert row["reference_price_field"] == "mid"
    assert row["timestamp"] == "2026-07-21T17:00:00.900000+00:00"


@pytest.mark.asyncio
async def test_get_equity_snapshot_falls_back_to_tngo_last() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=[
                {
                    "ticker": "LCID",
                    "timestamp": "2026-07-21T17:00:01Z",
                    "lastSaleTimestamp": "2026-07-21T17:00:00Z",
                    "tngoLast": 7.15,
                }
            ],
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = TiingoClient(api_key="test-key", client=http_client)
        row = await client.get_equity_snapshot("LCID")

    assert row["reference_price"] == 7.15
    assert row["reference_price_field"] == "tngoLast"
    assert row["timestamp"] == "2026-07-21T17:00:00+00:00"


@pytest.mark.asyncio
async def test_get_consolidated_equity_snapshot_preserves_liquidity_semantics() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/tiingo/equity/intraday/LCID"
        return httpx.Response(
            200,
            json={
                "ticker": "LCID",
                "timestamp": "2026-07-21T17:00:01Z",
                "tngoLast": 7.15,
                "lqRefPrice": 7.15,
                "lqBidPrice": 7.14,
                "lqBidSize": 100,
                "lqAskPrice": 7.16,
                "lqAskSize": 200,
                "lqSpread": 0.0028,
                "volume": 500_000,
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
        client = TiingoClient(
            api_key="test-key",
            equity_base_url="https://api.tiingo.com/tiingo/equity/intraday",
            client=http_client,
        )
        row = await client.get_consolidated_equity_snapshot("LCID")

    assert row["bid"] == 7.14
    assert row["ask"] == 7.16
    assert row["reference_price"] == 7.15
    assert row["quote_semantics"] == "consolidated_liquidity_reference_not_verified_nbbo"


@pytest.mark.asyncio
async def test_get_equity_snapshot_requires_api_key() -> None:
    client = TiingoClient(api_key="")
    with pytest.raises(TiingoAPIError, match="TIINGO_API_KEY"):
        await client.get_equity_snapshot("LCID")
