"""
Tests for the MCP server tool definitions.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

import pytest

from src.mcp_server import (
    get_vwap, get_bid_ask, get_vwap_30min, get_equity,
    get_fx_rate, get_metal_price,
    search_pairs, list_instruments, get_pricing_info,
)
from src.blocksize_client import BlocksizeAPIError
from src.models import (
    VWAPData, BidAskData, VWAP30MinData, EquityData, FXData,
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
# Analytics Tools
# ---------------------------------------------------------------------------

class TestVWAP30MinTool:
    @pytest.mark.asyncio
    async def test_get_vwap_30min_success(self):
        mock_data = VWAP30MinData(ticker="BTC", vwap=95000.0, quote_currency="EUR", timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc))
        with patch("src.mcp_server._get_client", new_callable=AsyncMock) as mock_client:
            mock_client.return_value.get_vwap_30min = AsyncMock(return_value=mock_data)
            result = await get_vwap_30min("BTC")
        assert "30-MIN VWAP [BTC]" in result
        assert "95000" in result


# ---------------------------------------------------------------------------
# Equities Tools
# ---------------------------------------------------------------------------

class TestEquityTool:
    @pytest.mark.asyncio
    async def test_get_equity_success(self):
        mock_data = EquityData(
            ticker="AAPL", open=180.5, high=182.0, low=179.0, last=181.5,
            bid=181.4, ask=181.6, volume=50000000, prev_close=179.8,
            timestamp=datetime(2026, 4, 19, 20, 0, tzinfo=timezone.utc),
        )
        with patch("src.mcp_server._get_client", new_callable=AsyncMock) as mock_client:
            mock_client.return_value.get_equity_snapshot = AsyncMock(return_value=mock_data)
            result = await get_equity("AAPL")
        assert "EQUITY [AAPL]" in result
        assert "181.5" in result


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
        assert "Equities" in result
        assert "Analytics" in result
        assert "FREE" in result
        assert "Solana" in result
