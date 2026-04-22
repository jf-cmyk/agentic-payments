"""
Tests for the MCP server tool definitions.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from src.mcp_server import (
    get_vwap, get_bid_ask,
    get_fx_rate, get_metal_price,
    search_pairs, get_pricing_info, search, fetch,
)
from src.blocksize_client import BlocksizeAPIError
from src.models import (
    VWAPData, BidAskData, FXData,
    MetalData, PairInfo,
)
from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Crypto Tools
# ---------------------------------------------------------------------------

class TestGetVWAPTool:
    @pytest.mark.asyncio
    async def test_get_vwap_success(self):
        mock_data = VWAPData(pair="btc-usd", vwap=95432.50, timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc), currency="USD")
        with patch("src.mcp_server._get_client", new_callable=AsyncMock) as mock_client:
            mock_client.return_value.get_vwap_latest = AsyncMock(return_value=mock_data)
            result = await get_vwap("btc-usd")
        assert "VWAP [btc-usd]" in result
        assert "95432.5" in result

    @pytest.mark.asyncio
    async def test_get_vwap_api_error(self):
        with patch("src.mcp_server._get_client", new_callable=AsyncMock) as mock_client:
            mock_client.return_value.get_vwap_latest = AsyncMock(side_effect=BlocksizeAPIError(-1, "Not found"))
            result = await get_vwap("nonexistent")
        parsed = json.loads(result)
        assert parsed["status"] == "error"


class TestGetBidAskTool:
    @pytest.mark.asyncio
    async def test_get_bid_ask_success(self):
        mock_data = BidAskData(pair="eth-usd", bid=3200.0, ask=3201.5, spread=1.5, spread_pct=0.047, timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc))
        with patch("src.mcp_server._get_client", new_callable=AsyncMock) as mock_client:
            mock_client.return_value.get_bidask_snapshot = AsyncMock(return_value=mock_data)
            result = await get_bid_ask("eth-usd")
        assert "BID/ASK [eth-usd]" in result
        assert "3200" in result


# ---------------------------------------------------------------------------
# TradFi Tools
# ---------------------------------------------------------------------------

class TestFXTool:
    @pytest.mark.asyncio
    async def test_get_fx_rate_success(self):
        mock_data = FXData(pair="eurusd", base_currency="EUR", quote_currency="USD", bid=1.085, ask=1.0855, mid=1.08525, timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc))
        with patch("src.mcp_server._get_client", new_callable=AsyncMock) as mock_client:
            mock_client.return_value.get_fx_rate = AsyncMock(return_value=mock_data)
            result = await get_fx_rate("eurusd")
        assert "FX [EURUSD]" in result


class TestMetalTool:
    @pytest.mark.asyncio
    async def test_get_metal_price_success(self):
        mock_data = MetalData(ticker="xauusd", name="Gold", price=2350.50, currency="USD", timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc))
        with patch("src.mcp_server._get_client", new_callable=AsyncMock) as mock_client:
            mock_client.return_value.get_metal_price = AsyncMock(return_value=mock_data)
            result = await get_metal_price("xauusd")
        assert "METAL [Gold]" in result
        assert "2350.5" in result


# ---------------------------------------------------------------------------
# Discovery Tools (FREE)
# ---------------------------------------------------------------------------

class TestSearchPairsTool:
    @pytest.mark.asyncio
    async def test_search_pairs_with_results(self):
        mock_pairs = [
            PairInfo(pair="btc-usd", base_currency="BTC", quote_currency="USD", asset_class="crypto", services=["vwap"], tier="core"),
            PairInfo(pair="btc-eur", base_currency="BTC", quote_currency="EUR", asset_class="crypto", services=["vwap", "bidask"], tier="core"),
        ]
        with patch("src.mcp_server._get_client", new_callable=AsyncMock) as mock_client:
            mock_client.return_value.search_pairs = AsyncMock(return_value=mock_pairs)
            result = await search_pairs("btc")
        assert "2 instruments" in result
        assert "btc-usd" in result

    @pytest.mark.asyncio
    async def test_search_pairs_no_results(self):
        with patch("src.mcp_server._get_client", new_callable=AsyncMock) as mock_client:
            mock_client.return_value.search_pairs = AsyncMock(return_value=[])
            result = await search_pairs("zzzzz")
        assert "No instruments found" in result


class TestGetPricingInfoTool:
    @pytest.mark.asyncio
    async def test_pricing_info_returns_all_tiers(self):
        result = await get_pricing_info()
        assert "Core Crypto" in result
        assert "Extended Crypto" in result
        assert "TradFi" in result
        assert "rates" not in result.lower()
        assert "Equities" not in result
        assert "Analytics" not in result
        assert "FREE" in result
        assert "Solana" in result


class TestOpenAIStyleDiscoveryTools:
    @pytest.mark.asyncio
    async def test_search_returns_results_payload(self):
        mock_pairs = [
            PairInfo(
                pair="btc-usd",
                base_currency="BTC",
                quote_currency="USD",
                asset_class="crypto",
                services=["vwap", "bidask"],
                tier="core",
            )
        ]
        with patch("src.mcp_server._get_client", new_callable=AsyncMock) as mock_client:
            mock_client.return_value.search_pairs = AsyncMock(return_value=mock_pairs)
            result = await search("btc")

        parsed = json.loads(result)
        assert "results" in parsed
        assert any(item["id"].startswith("instrument:") for item in parsed["results"])
        assert any(item["id"] == "doc:pricing" or item["id"] == "doc:quickstart" for item in parsed["results"])

    @pytest.mark.asyncio
    async def test_fetch_returns_static_doc(self):
        result = await fetch("doc:quickstart")
        parsed = json.loads(result)
        assert parsed["id"] == "doc:quickstart"
        assert "remote mcp" in parsed["text"].lower()

    @pytest.mark.asyncio
    async def test_fetch_returns_instrument_doc(self):
        mock_pairs = [
            PairInfo(
                pair="btc-usd",
                base_currency="BTC",
                quote_currency="USD",
                asset_class="crypto",
                services=["vwap", "bidask"],
                tier="core",
            )
        ]
        with patch("src.mcp_server._get_client", new_callable=AsyncMock) as mock_client:
            mock_client.return_value.search_pairs = AsyncMock(return_value=mock_pairs)
            result = await fetch("instrument:crypto:btc-usd")

        parsed = json.loads(result)
        assert parsed["id"] == "instrument:crypto:btc-usd"
        assert "paid http api" in parsed["text"].lower()
