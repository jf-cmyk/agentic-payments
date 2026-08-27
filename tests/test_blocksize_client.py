"""
Unit tests for the Blocksize Capital API client.
"""

from __future__ import annotations

from datetime import datetime
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

    @pytest.mark.asyncio
    async def test_get_vwap_30min_uses_closingprice_list(self, client):
        mock_result = {
            "prices": [
                {"base": "BTC", "quote": "USD", "price": "66800.0", "ts": 1713567600000},
                {"base": "SOL", "quote": "USD", "price": "75.25", "ts": 1713567600000},
            ]
        }
        with patch.object(client, "_rpc_call", new_callable=AsyncMock, return_value=mock_result) as rpc:
            vwap = await client.get_vwap_30min("SOL")

        assert rpc.await_args.args[0] == "closingprice_list"
        assert rpc.await_args.args[1]["quote"] == "USD"
        assert vwap.ticker == "SOL"
        assert vwap.vwap == 75.25

    @pytest.mark.asyncio
    async def test_get_vwap_24hr_requires_stream_cache_when_http_method_missing(self, client):
        with patch.object(
            client,
            "_rpc_call",
            new_callable=AsyncMock,
            side_effect=BlocksizeAPIError(-32601, "method not found"),
        ):
            with pytest.raises(BlocksizeAPIError, match="stream cache"):
                await client.get_vwap_24hr("BTCUSD")

    @pytest.mark.asyncio
    async def test_get_vwap_30min_trades_uses_closingprice_trades(self, client):
        mock_result = {
            "prices": [
                {"base": "SOL", "quote": "USD", "exchange": "COINBASE", "price": "75", "size": "10", "ts": 1},
            ]
        }
        with patch.object(client, "_rpc_call", new_callable=AsyncMock, return_value=mock_result) as rpc:
            trades = await client.get_vwap_30min_trades("SOLUSD")

        assert rpc.await_args.args[0] == "closingprice_trades"
        assert rpc.await_args.args[1]["base"] == "SOL"
        assert trades[0]["exchange"] == "COINBASE"


# ---------------------------------------------------------------------------
# State Pool Parsing
# ---------------------------------------------------------------------------

class TestStatePoolParsing:
    @pytest.mark.asyncio
    async def test_get_state_price_derives_weighted_price_from_state_pools(self, client):
        async def rpc_side_effect(method, params=None):
            if method == "state_instruments":
                return {
                    "instruments": [
                        {
                            "symbol": "MSOLUSD",
                            "pools": [
                                {"network": "solana", "address": "pool-1"},
                                {"network": "solana", "address": "pool-2"},
                            ],
                        }
                    ]
                }
            if method == "state_pool" and params["pool"] == "pool-1":
                return {
                    "state": {
                        "state_price_usd": "200.0",
                        "weight": "0.25",
                        "block_time": 1713567600,
                    }
                }
            if method == "state_pool" and params["pool"] == "pool-2":
                return {
                    "state": {
                        "state_price_usd": "220.0",
                        "weight": "0.75",
                        "block_time": 1713567601,
                    }
                }
            raise AssertionError(f"unexpected RPC call {method} {params}")

        with patch.object(client, "_rpc_call", new_callable=AsyncMock, side_effect=rpc_side_effect):
            state_price = await client.get_state_price("MSOLUSD")

        assert state_price.pair == "MSOLUSD"
        assert state_price.price == pytest.approx(215.0)

    @pytest.mark.asyncio
    async def test_get_state_price_reports_missing_pool_coverage(self, client):
        async def rpc_side_effect(method, params=None):
            if method == "state_instruments":
                return {"instruments": [{"symbol": "MSOLUSD", "pools": []}]}
            raise BlocksizeAPIError(-32601, "method not found")

        with patch.object(client, "_rpc_call", new_callable=AsyncMock, side_effect=rpc_side_effect):
            with pytest.raises(BlocksizeAPIError, match="No state_instruments pool coverage"):
                await client.get_state_price("SOLUSD")


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

    @pytest.mark.asyncio
    async def test_get_bidask_snapshot_parses_equity_fields(self, client):
        mock_result = {
            "ticker": "AAPL",
            "bidPrice": 181.4,
            "askPrice": 181.6,
            "last": 181.5,
            "timestamp": "2026-04-19T20:00:00+00:00",
        }
        with patch.object(client, "_rpc_call", new_callable=AsyncMock, return_value=mock_result):
            bidask = await client.get_bidask_snapshot("AAPL")
        assert bidask.pair == "AAPL"
        assert bidask.bid == 181.4
        assert bidask.ask == 181.6
        assert bidask.spread == pytest.approx(0.2)

    @pytest.mark.asyncio
    async def test_get_bidask_snapshot_resolves_bare_equity_to_usd_suffix(self, client):
        mock_result = {
            "snapshot": [
                {
                    "ticker": "LCIDUSD",
                    "agg_bid_price": "7.14",
                    "agg_ask_price": "7.16",
                    "agg_mid_price": "7.15",
                    "ts": 1784652304843551,
                }
            ]
        }
        with patch.object(client, "_rpc_call", new_callable=AsyncMock, return_value=mock_result):
            bidask = await client.get_bidask_snapshot("LCID")
        assert bidask.pair == "LCIDUSD"
        assert bidask.bid == 7.14
        assert bidask.ask == 7.16

    @pytest.mark.asyncio
    async def test_get_bidask_snapshot_rejects_missing_master_stream_symbol(self, client):
        with patch.object(
            client,
            "_rpc_call",
            new_callable=AsyncMock,
            return_value={"snapshot": []},
        ):
            with pytest.raises(BlocksizeAPIError, match="not found"):
                await client.get_bidask_snapshot("MISSING")


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
# Instrument Listing
# ---------------------------------------------------------------------------

class TestInstrumentListing:
    @pytest.mark.asyncio
    async def test_list_vwap_instruments_list(self, client):
        mock_result = ["btc-usd", "eth-usd", "sol-usd"]
        with patch.object(client, "_rpc_call", new_callable=AsyncMock, return_value=mock_result):
            instruments = await client.list_vwap_instruments()
        assert instruments == ["BTC-USD", "ETH-USD", "SOL-USD"]

    @pytest.mark.asyncio
    async def test_list_vwap_instruments_dict(self, client):
        mock_result = {"instruments": [
            {"ticker": "btc-eur", "base_currency": "BTC", "quote_currency": "EUR"},
            {"ticker": "eth-eur", "base_currency": "ETH", "quote_currency": "EUR"},
        ]}
        with patch.object(client, "_rpc_call", new_callable=AsyncMock, return_value=mock_result):
            instruments = await client.list_vwap_instruments()
        assert instruments == ["BTC-EUR", "ETH-EUR"]

    @pytest.mark.asyncio
    async def test_list_fx_instruments_derived_from_bidask_catalog(self, client):
        mock_result = {"instruments": [
            {"ticker": "EURUSD", "base_currency": "EUR", "quote_currency": "USD"},
            {"ticker": "BTCUSD", "base_currency": "BTC", "quote_currency": "USD"},
        ]}
        with patch.object(client, "_rpc_call", new_callable=AsyncMock, return_value=mock_result):
            instruments = await client.list_fx_instruments()
        assert instruments == ["EURUSD"]


# ---------------------------------------------------------------------------
# Search
# ---------------------------------------------------------------------------

class TestPairSearch:
    @pytest.mark.asyncio
    async def test_search_pairs(self, client):
        with patch.object(client, "list_vwap_instruments", new_callable=AsyncMock, return_value=["BTC-USD", "BTC-EUR", "ETH-USD"]), \
             patch.object(client, "_list_bidask_entries", new_callable=AsyncMock, return_value=[
                 {"ticker": "BTC-USD", "base_currency": "BTC", "quote_currency": "USD"},
                 {"ticker": "SOL-USD", "base_currency": "SOL", "quote_currency": "USD"},
             ]), \
             patch.object(client, "list_fx_instruments", new_callable=AsyncMock, return_value=[]), \
             patch.object(client, "list_metal_instruments", new_callable=AsyncMock, return_value=[]):
            results = await client.search_pairs("btc")
        assert len(results) == 2
        pair_names = [r.pair for r in results]
        assert "BTC-EUR" in pair_names
        assert "BTC-USD" in pair_names

    @pytest.mark.asyncio
    async def test_search_pairs_includes_equities_from_bidask_namespace(self, client):
        with patch.object(client, "list_vwap_instruments", new_callable=AsyncMock, return_value=[]), \
             patch.object(client, "_list_bidask_entries", new_callable=AsyncMock, return_value=[
                 {"ticker": "AAPL", "base_currency": "AAPL", "quote_currency": "", "asset_class": "equity"},
                 {"ticker": "BTCUSD", "base_currency": "BTC", "quote_currency": "USD", "asset_class": ""},
             ]), \
             patch.object(client, "list_metal_instruments", new_callable=AsyncMock, return_value=[]):
            results = await client.search_pairs("aapl", asset_class="equity")

        assert len(results) == 1
        assert results[0].pair == "AAPL"
        assert results[0].asset_class == "equity"
        assert results[0].services == ["bidask"]
        assert results[0].tier == "equities"

    @pytest.mark.asyncio
    async def test_search_normalizes_separators_and_ranks_exact_then_base_matches(self, client):
        with patch.object(
            client,
            "list_vwap_instruments",
            new_callable=AsyncMock,
            return_value=[
                "AAVESOL",
                "SOLBTC",
                "SOLETH",
                "SOLUSD",
                "SOLUSDC",
                "SOLUSDT",
                "XSOLUSD",
            ],
        ), patch.object(
            client,
            "_list_bidask_entries",
            new_callable=AsyncMock,
            return_value=[],
        ), patch.object(
            client,
            "list_metal_instruments",
            new_callable=AsyncMock,
            return_value=[],
        ):
            exact, _ = await client.search_pairs_page(
                "SOL-USD", asset_class="crypto", limit=50, offset=0
            )
            base, _ = await client.search_pairs_page(
                "SOL", asset_class="crypto", limit=50, offset=0
            )

        assert exact[0].pair == "SOLUSD"
        assert {item.pair for item in exact} == {
            "SOLUSD",
            "SOLUSDC",
            "SOLUSDT",
            "XSOLUSD",
        }
        assert [item.pair for item in base[:5]] == [
            "SOLUSD",
            "SOLUSDC",
            "SOLUSDT",
            "SOLBTC",
            "SOLETH",
        ]
        assert [item.pair for item in base].index("AAVESOL") >= 5

    @pytest.mark.asyncio
    async def test_fx_search_reports_customer_facing_fx_service(self, client):
        with patch.object(
            client,
            "list_vwap_instruments",
            new_callable=AsyncMock,
            return_value=[],
        ), patch.object(
            client,
            "_list_bidask_entries",
            new_callable=AsyncMock,
            return_value=[
                {
                    "ticker": "EURUSD",
                    "base_currency": "EUR",
                    "quote_currency": "USD",
                    "asset_class": "fx",
                }
            ],
        ), patch.object(
            client,
            "list_metal_instruments",
            new_callable=AsyncMock,
            return_value=[],
        ):
            results = await client.search_pairs("EUR/USD", asset_class="fx")

        assert len(results) == 1
        assert results[0].pair == "EURUSD"
        assert results[0].services == ["fx"]

    @pytest.mark.asyncio
    async def test_search_pairs_no_match(self, client):
        with patch.object(client, "list_vwap_instruments", new_callable=AsyncMock, return_value=["btc-usd"]), \
             patch.object(client, "_list_bidask_entries", new_callable=AsyncMock, return_value=[]), \
             patch.object(client, "list_fx_instruments", new_callable=AsyncMock, return_value=[]), \
             patch.object(client, "list_metal_instruments", new_callable=AsyncMock, return_value=[]):
            results = await client.search_pairs("nonexistent")
        assert len(results) == 0

    @pytest.mark.asyncio
    async def test_search_pairs_max_50(self, client):
        instruments = [f"test-{i}" for i in range(100)]
        with patch.object(client, "list_vwap_instruments", new_callable=AsyncMock, return_value=instruments), \
             patch.object(client, "_list_bidask_entries", new_callable=AsyncMock, return_value=[]), \
             patch.object(client, "list_fx_instruments", new_callable=AsyncMock, return_value=[]), \
             patch.object(client, "list_metal_instruments", new_callable=AsyncMock, return_value=[]):
            results = await client.search_pairs("test")
        assert len(results) == 50

    @pytest.mark.asyncio
    async def test_search_pairs_page_preserves_total_beyond_legacy_cap(self, client):
        instruments = [f"test-{i:03d}" for i in range(100)]
        with patch.object(client, "list_vwap_instruments", new_callable=AsyncMock, return_value=instruments), \
             patch.object(client, "_list_bidask_entries", new_callable=AsyncMock, return_value=[]), \
             patch.object(client, "list_metal_instruments", new_callable=AsyncMock, return_value=[]):
            page, total = await client.search_pairs_page(
                "test", asset_class="crypto", limit=20, offset=40
            )

        assert total == 100
        assert len(page) == 20
        assert page[0].pair == "test-040"
        assert page[-1].pair == "test-059"

    @pytest.mark.asyncio
    async def test_search_pairs_page_does_not_claim_complete_total_on_catalog_failure(
        self,
        client,
    ):
        with patch.object(
            client,
            "list_vwap_instruments",
            new_callable=AsyncMock,
            side_effect=BlocksizeAPIError(503, "upstream unavailable"),
        ):
            with pytest.raises(BlocksizeAPIError):
                await client.search_pairs_page("btc", asset_class="crypto")

    @pytest.mark.asyncio
    async def test_search_assigns_tier(self, client):
        with patch.object(client, "list_vwap_instruments", new_callable=AsyncMock, return_value=["BTCUSD", "NICHETOKEN123"]), \
             patch.object(client, "_list_bidask_entries", new_callable=AsyncMock, return_value=[]), \
             patch.object(client, "list_fx_instruments", new_callable=AsyncMock, return_value=[]), \
             patch.object(client, "list_metal_instruments", new_callable=AsyncMock, return_value=[]):
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

    def test_split_pair_bare_equity_ticker(self):
        assert _split_pair("AAPL") == ("AAPL", "")
