"""
Unit tests for the Blocksize Capital API client.
"""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest

from src.blocksize_client import BlocksizeClient, BlocksizeAPIError, _parse_timestamp, _split_pair


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def client():
    """Create a test client with a dummy API key."""
    return BlocksizeClient(api_key="test-api-key", base_url="https://test.blocksize.capital/api")


# ---------------------------------------------------------------------------
# JSON-RPC Request Building
# ---------------------------------------------------------------------------

class TestRPCRequestBuilding:
    def test_build_rpc_request_basic(self, client):
        req = client._build_rpc_request("vwap_latest", {"ticker": "btc-usd"})
        assert req["jsonrpc"] == "2.0"
        assert req["method"] == "vwap_latest"
        assert req["params"] == {"ticker": "btc-usd"}
        assert "id" in req

    def test_build_rpc_request_no_params(self, client):
        req = client._build_rpc_request("vwap_instruments")
        assert req["params"] == {}

    def test_build_rpc_request_unique_ids(self, client):
        req1 = client._build_rpc_request("method1")
        req2 = client._build_rpc_request("method2")
        assert req1["id"] != req2["id"]


# ---------------------------------------------------------------------------
# VWAP Parsing
# ---------------------------------------------------------------------------

class TestVWAPParsing:
    @pytest.mark.asyncio
    async def test_get_vwap_latest_dict_response(self, client):
        mock_result = {"ticker": "btc-usd", "price": 95432.50, "timestamp": 1713567600000, "currency": "USD"}
        with patch.object(client, "_rpc_call", new_callable=AsyncMock, return_value=mock_result):
            vwap = await client.get_vwap_latest("btc-usd")
        assert vwap.pair == "btc-usd"
        assert vwap.vwap == 95432.50
        assert vwap.currency == "USD"

    @pytest.mark.asyncio
    async def test_get_vwap_latest_list_response(self, client):
        mock_result = [{"ticker": "eth-usd", "vwap": 3200.75, "timestamp": "2026-04-19T12:00:00Z"}]
        with patch.object(client, "_rpc_call", new_callable=AsyncMock, return_value=mock_result):
            vwap = await client.get_vwap_latest("eth-usd")
        assert vwap.pair == "eth-usd"
        assert vwap.vwap == 3200.75

    @pytest.mark.asyncio
    async def test_get_vwap_latest_error(self, client):
        with patch.object(client, "_rpc_call", new_callable=AsyncMock, side_effect=BlocksizeAPIError(-1, "Not found")):
            with pytest.raises(BlocksizeAPIError, match="Not found"):
                await client.get_vwap_latest("nonexistent-pair")


# ---------------------------------------------------------------------------
# Bid/Ask Parsing
# ---------------------------------------------------------------------------

class TestBidAskParsing:
    @pytest.mark.asyncio
    async def test_get_bidask_snapshot(self, client):
        mock_result = {"ticker": "btc-usd", "bid": 95400.00, "ask": 95450.00, "timestamp": 1713567600}
        with patch.object(client, "_rpc_call", new_callable=AsyncMock, return_value=mock_result):
            bidask = await client.get_bidask_snapshot("btc-usd")
        assert bidask.bid == 95400.00
        assert bidask.ask == 95450.00
        assert bidask.spread == 50.00
        assert bidask.spread_pct == pytest.approx(0.0524, rel=0.01)

    @pytest.mark.asyncio
    async def test_get_bidask_zero_ask(self, client):
        mock_result = {"ticker": "test", "bid": 0, "ask": 0, "timestamp": None}
        with patch.object(client, "_rpc_call", new_callable=AsyncMock, return_value=mock_result):
            bidask = await client.get_bidask_snapshot("test")
        assert bidask.spread == 0.0
        assert bidask.spread_pct == 0.0


# ---------------------------------------------------------------------------
# Equity Parsing
# ---------------------------------------------------------------------------

class TestEquityParsing:
    @pytest.mark.asyncio
    async def test_get_equity_snapshot(self, client):
        mock_result = {
            "ticker": "AAPL", "open": 180.5, "high": 182.0, "low": 179.0,
            "last": 181.5, "bidPrice": 181.4, "askPrice": 181.6,
            "volume": 50000000, "prevClose": 179.8,
            "timestamp": "2026-04-19T20:00:00+00:00",
        }
        with patch.object(client, "_rpc_call", new_callable=AsyncMock, return_value=mock_result):
            equity = await client.get_equity_snapshot("AAPL")
        assert equity.ticker == "AAPL"
        assert equity.last == 181.5
        assert equity.bid == 181.4
        assert equity.ask == 181.6
        assert equity.volume == 50000000


# ---------------------------------------------------------------------------
# FX Parsing
# ---------------------------------------------------------------------------

class TestFXParsing:
    @pytest.mark.asyncio
    async def test_get_fx_rate(self, client):
        mock_result = {
            "baseCurrency": "EUR", "quoteCurrency": "USD",
            "bid": 1.0850, "ask": 1.0855,
            "timestamp": "2026-04-19T12:00:00Z",
        }
        with patch.object(client, "_rpc_call", new_callable=AsyncMock, return_value=mock_result):
            fx = await client.get_fx_rate("eurusd")
        assert fx.base_currency == "EUR"
        assert fx.quote_currency == "USD"
        assert fx.bid == 1.0850
        assert fx.ask == 1.0855
        assert fx.mid == pytest.approx(1.08525, rel=0.0001)


# ---------------------------------------------------------------------------
# Treasury Rates
# ---------------------------------------------------------------------------

class TestTreasuryRates:
    @pytest.mark.asyncio
    async def test_get_treasury_rate(self, client):
        mock_result = {"yield": 4.25, "timestamp": "2026-04-19T20:00:00Z"}
        with patch.object(client, "_rpc_call", new_callable=AsyncMock, return_value=mock_result):
            rate = await client.get_treasury_rate("10Y")
        assert rate.maturity == "10Y"
        assert rate.yield_pct == 4.25
        assert rate.ticker == "Rates.US10Y"


# ---------------------------------------------------------------------------
# Instrument Listing
# ---------------------------------------------------------------------------

class TestInstrumentListing:
    @pytest.mark.asyncio
    async def test_list_vwap_instruments_list(self, client):
        mock_result = ["btc-usd", "eth-usd", "sol-usd"]
        with patch.object(client, "_rpc_call", new_callable=AsyncMock, return_value=mock_result):
            instruments = await client.list_vwap_instruments()
        assert instruments == ["btc-usd", "eth-usd", "sol-usd"]

    @pytest.mark.asyncio
    async def test_list_vwap_instruments_dict(self, client):
        mock_result = {"instruments": ["btc-eur", "eth-eur"]}
        with patch.object(client, "_rpc_call", new_callable=AsyncMock, return_value=mock_result):
            instruments = await client.list_vwap_instruments()
        assert instruments == ["btc-eur", "eth-eur"]


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

class TestPairSearch:
    @pytest.mark.asyncio
    async def test_search_pairs(self, client):
        with patch.object(client, "list_vwap_instruments", new_callable=AsyncMock, return_value=["btc-usd", "btc-eur", "eth-usd"]), \
             patch.object(client, "list_bidask_instruments", new_callable=AsyncMock, return_value=["btc-usd", "sol-usd"]), \
             patch.object(client, "list_fx_instruments", new_callable=AsyncMock, return_value=[]):
            results = await client.search_pairs("btc")
        assert len(results) == 2
        pair_names = [r.pair for r in results]
        assert "btc-eur" in pair_names
        assert "btc-usd" in pair_names

    @pytest.mark.asyncio
    async def test_search_pairs_no_match(self, client):
        with patch.object(client, "list_vwap_instruments", new_callable=AsyncMock, return_value=["btc-usd"]), \
             patch.object(client, "list_bidask_instruments", new_callable=AsyncMock, return_value=[]), \
             patch.object(client, "list_fx_instruments", new_callable=AsyncMock, return_value=[]):
            results = await client.search_pairs("nonexistent")
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_search_pairs_max_50(self, client):
        instruments = [f"test-{i}" for i in range(100)]
        with patch.object(client, "list_vwap_instruments", new_callable=AsyncMock, return_value=instruments), \
             patch.object(client, "list_bidask_instruments", new_callable=AsyncMock, return_value=[]), \
             patch.object(client, "list_fx_instruments", new_callable=AsyncMock, return_value=[]):
            results = await client.search_pairs("test")
        assert len(results) == 50

    @pytest.mark.asyncio
    async def test_search_assigns_tier(self, client):
        with patch.object(client, "list_vwap_instruments", new_callable=AsyncMock, return_value=["BTCUSD", "NICHETOKEN123"]), \
             patch.object(client, "list_bidask_instruments", new_callable=AsyncMock, return_value=[]), \
             patch.object(client, "list_fx_instruments", new_callable=AsyncMock, return_value=[]):
            results = await client.search_pairs("", asset_class="crypto")

        btc_result = [r for r in results if "BTC" in r.base_currency]
        niche_result = [r for r in results if "NICHETOKEN" in r.pair.upper()]

        if btc_result:
            assert btc_result[0].tier == "core"
        if niche_result:
            assert niche_result[0].tier == "extended"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

class TestHelpers:
    def test_parse_timestamp_unix_seconds(self):
        assert isinstance(_parse_timestamp(1713567600), datetime)

    def test_parse_timestamp_unix_millis(self):
        assert isinstance(_parse_timestamp(1713567600000), datetime)

    def test_parse_timestamp_iso_string(self):
        assert isinstance(_parse_timestamp("2026-04-19T12:00:00Z"), datetime)

    def test_parse_timestamp_none(self):
        assert isinstance(_parse_timestamp(None), datetime)

    def test_split_pair_dash(self):
        assert _split_pair("btc-usd") == ("BTC", "USD")

    def test_split_pair_slash(self):
        assert _split_pair("ETH/EUR") == ("ETH", "EUR")

    def test_split_pair_no_separator(self):
        base, quote = _split_pair("btcusd")
        assert base == "BTC"
        assert quote == "USD"
