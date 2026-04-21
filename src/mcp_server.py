"""
Blocksize Capital MCP Server — Agent-Facing Tool Interface.

This is the primary entry point for AI agents. It exposes Blocksize Capital's
full multi-asset data platform as MCP tools that any LLM client (Claude Desktop,
Cursor, LangChain, etc.) can discover and call.

Asset Classes:
  - Crypto:     9,410 RT VWAP pairs, 1,962 Bid/Ask pairs, 251 30-Min VWAP, State Price
  - Equities:   18,071 US + Chinese stocks
  - FX:         129 currency pairs
  - Metals:     Gold, Silver, Platinum, Palladium, Copper

Payment Model:
  Tiered pricing in USDC — settled on Solana (primary) or Base L2 (fallback).
  Discovery tools are FREE. Data tools: $0.002–$0.008 per call.
"""

from __future__ import annotations

import json
import logging
import sys

from fastmcp import FastMCP

from src.blocksize_client import BlocksizeClient, BlocksizeAPIError
from src.config import settings, TOP_250_CRYPTO
from src.models import (
    BidAskResponse,
    EquityResponse,
    ErrorResponse,
    InstrumentListResponse,
    PairSearchResponse,
    VWAPResponse,
)

# ---------------------------------------------------------------------------
# Logging — stderr in STDIO mode (stdout is the MCP JSON-RPC pipe)
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=getattr(logging, settings.server.log_level.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    stream=sys.stderr,
)
logger = logging.getLogger("blocksize-mcp")


# ---------------------------------------------------------------------------
# MCP Server Initialization
# ---------------------------------------------------------------------------

mcp = FastMCP(
    "Blocksize Capital",
    instructions=(
        "Institutional-grade multi-asset market data for AI agents. "
        "Access real-time VWAP, bid/ask spreads, equities, FX, metals, "
        "and analytics across 30,000+ instruments. "
        "Outlier-filtered, decision-ready output. "
        "Pay per call via x402 (USDC on Solana or Base L2). "
        "No subscription required."
    ),
)

_client: BlocksizeClient | None = None


async def _get_client() -> BlocksizeClient:
    """Get or initialize the Blocksize API client."""
    global _client
    if _client is None:
        _client = BlocksizeClient()
    return _client


# ===========================================================================
# CRYPTO TOOLS
# ===========================================================================

@mcp.tool
async def get_vwap(pair: str) -> str:
    """
    Get the real-time Volume Weighted Average Price (VWAP) for a crypto trading pair.

    Returns institutional-grade, outlier-filtered VWAP data from Blocksize Capital.
    Sub-second pricing used by Tier 1 Pyth Network publishers.

    Cost: $0.002 USDC (top 250 crypto) or $0.004 USDC (niche crypto).
    Payment: x402 on Solana (USDC) or Base L2.

    Args:
        pair: Trading pair identifier. Examples: 'btc-usd', 'eth-eur', 'sol-usdt'.
              Use search_pairs to discover available pairs.

    Returns:
        Decision-ready VWAP summary including price, timestamp, and data quality.
    """
    try:
        client = await _get_client()
        vwap_data = await client.get_vwap_latest(pair)
        response = VWAPResponse(data=vwap_data)

        summary = vwap_data.to_decision_summary()
        details = json.dumps(response.model_dump(), default=str, indent=2)
        return f"{summary}\n\n<details>\n{details}\n</details>"

    except BlocksizeAPIError as e:
        return json.dumps(ErrorResponse(
            error_code="BLOCKSIZE_API_ERROR",
            message=f"Failed to retrieve VWAP for '{pair}'",
            details=str(e),
        ).model_dump())
    except Exception as e:
        logger.error("Unexpected error in get_vwap(%s): %s", pair, e, exc_info=True)
        return json.dumps(ErrorResponse(
            error_code="INTERNAL_ERROR",
            message=f"Error retrieving VWAP for '{pair}'",
            details=str(e),
        ).model_dump())


@mcp.tool
async def get_bid_ask(pair: str) -> str:
    """
    Get the current best bid/ask prices and spread for a crypto trading pair.

    Returns real-time best bid, best ask, absolute spread, and spread percentage.

    Cost: $0.002 USDC (top 250 crypto) or $0.004 USDC (niche crypto).

    Args:
        pair: Trading pair identifier. Examples: 'btc-usd', 'eth-eur'.
    """
    try:
        client = await _get_client()
        bidask_data = await client.get_bidask_snapshot(pair)
        response = BidAskResponse(data=bidask_data)

        summary = bidask_data.to_decision_summary()
        details = json.dumps(response.model_dump(), default=str, indent=2)
        return f"{summary}\n\n<details>\n{details}\n</details>"

    except BlocksizeAPIError as e:
        return json.dumps(ErrorResponse(
            error_code="BLOCKSIZE_API_ERROR",
            message=f"Failed to retrieve bid/ask for '{pair}'",
            details=str(e),
        ).model_dump())
    except Exception as e:
        logger.error("Unexpected error in get_bid_ask(%s): %s", pair, e, exc_info=True)
        return json.dumps(ErrorResponse(
            error_code="INTERNAL_ERROR",
            message=f"Error retrieving bid/ask for '{pair}'",
            details=str(e),
        ).model_dump())


@mcp.tool
async def get_vwap_30min(ticker: str) -> str:
    """
    Get the 30-minute VWAP snapshot for a crypto asset.

    Provides periodic VWAP aggregated over 30-minute windows. Available for
    251 major crypto tickers. Ideal for time-series analysis and backtesting.
    Cost: $0.001 USDC per call (Analytics tier).

    Args:
        ticker: Base currency ticker (e.g., 'BTC', 'ETH', 'SOL').
                NOT a trading pair — just the base asset symbol.
    """
    try:
        client = await _get_client()
        data = await client.get_vwap_30min(ticker)
        summary = data.to_decision_summary()
        details = json.dumps(data.model_dump(), default=str, indent=2)
        return f"{summary}\n\n<details>\n{details}\n</details>"

    except BlocksizeAPIError as e:
        return json.dumps(ErrorResponse(
            error_code="BLOCKSIZE_API_ERROR",
            message=f"Failed to retrieve 30-min VWAP for '{ticker}'",
            details=str(e),
        ).model_dump())
    except Exception as e:
        logger.error("Error in get_vwap_30min(%s): %s", ticker, e, exc_info=True)
        return json.dumps(ErrorResponse(
            error_code="INTERNAL_ERROR",
            message=f"Error retrieving 30-min VWAP for '{ticker}'",
            details=str(e),
        ).model_dump())


@mcp.tool
async def get_vwap_24hr(pair: str) -> str:
    """
    Get the 24-hour VWAP (closing price) for a crypto pair.

    Provides the volume-weighted average price over the past 24 hours,
    essentially the institutional closing price. Includes 24h volume.
    Cost: $0.001 USDC per call (Analytics tier).

    Args:
        pair: Trading pair identifier (e.g., 'BTCUSD', 'ETHUSDT').
    """
    try:
        client = await _get_client()
        data = await client.get_vwap_24hr(pair)
        summary = data.to_decision_summary()
        details = json.dumps(data.model_dump(), default=str, indent=2)
        return f"{summary}\n\n<details>\n{details}\n</details>"

    except BlocksizeAPIError as e:
        return json.dumps(ErrorResponse(
            error_code="BLOCKSIZE_API_ERROR",
            message=f"Failed to retrieve 24h VWAP for '{pair}'",
            details=str(e),
        ).model_dump())
    except Exception as e:
        logger.error("Error in get_vwap_24hr(%s): %s", pair, e, exc_info=True)
        return json.dumps(ErrorResponse(
            error_code="INTERNAL_ERROR",
            message=f"Error retrieving 24h VWAP for '{pair}'",
            details=str(e),
        ).model_dump())


@mcp.tool
async def get_state_price(pair: str) -> str:
    """
    Get the state/reference price for a crypto pair.

    The state price is a settlement-grade reference price used for
    mark-to-market and accounting purposes.
    Cost: $0.002 USDC (top 250 crypto) or $0.004 USDC (niche crypto).

    Args:
        pair: Trading pair identifier (e.g., 'btc-usd').
    """
    try:
        client = await _get_client()
        data = await client.get_state_price(pair)
        summary = data.to_decision_summary()
        details = json.dumps(data.model_dump(), default=str, indent=2)
        return f"{summary}\n\n<details>\n{details}\n</details>"

    except BlocksizeAPIError as e:
        return json.dumps(ErrorResponse(
            error_code="BLOCKSIZE_API_ERROR",
            message=f"Failed to retrieve state price for '{pair}'",
            details=str(e),
        ).model_dump())
    except Exception as e:
        logger.error("Error in get_state_price(%s): %s", pair, e, exc_info=True)
        return json.dumps(ErrorResponse(
            error_code="INTERNAL_ERROR",
            message=f"Error retrieving state price for '{pair}'",
            details=str(e),
        ).model_dump())


# ===========================================================================
# EQUITIES TOOLS
# ===========================================================================

@mcp.tool
async def get_equity(ticker: str) -> str:
    """
    Get the latest snapshot for a US or Chinese equity (stock).

    Returns OHLCV, bid/ask, and previous close for 18,071 tickers.
    No other crypto MCP server offers equity data.

    Cost: $0.008 USDC per call (Equities tier).

    Args:
        ticker: Stock ticker symbol (e.g., 'AAPL', 'TSLA', 'NVDA', 'BABA').
    """
    try:
        client = await _get_client()
        data = await client.get_equity_snapshot(ticker)
        response = EquityResponse(data=data)

        summary = data.to_decision_summary()
        details = json.dumps(response.model_dump(), default=str, indent=2)
        return f"{summary}\n\n<details>\n{details}\n</details>"

    except BlocksizeAPIError as e:
        return json.dumps(ErrorResponse(
            error_code="BLOCKSIZE_API_ERROR",
            message=f"Failed to retrieve equity data for '{ticker}'",
            details=str(e),
        ).model_dump())
    except Exception as e:
        logger.error("Error in get_equity(%s): %s", ticker, e, exc_info=True)
        return json.dumps(ErrorResponse(
            error_code="INTERNAL_ERROR",
            message=f"Error retrieving equity data for '{ticker}'",
            details=str(e),
        ).model_dump())


# ===========================================================================
# FX & METALS TOOLS
# ===========================================================================

@mcp.tool
async def get_fx_rate(pair: str) -> str:
    """
    Get the current exchange rate for an FX (foreign exchange) pair.

    Covers 129 FX pairs including major, minor, and exotic currency pairs.
    Returns bid/ask/mid rates.

    Cost: $0.005 USDC per call (TradFi tier).

    Args:
        pair: FX pair identifier (e.g., 'eurusd', 'gbpjpy', 'usdchf').
    """
    try:
        client = await _get_client()
        data = await client.get_fx_rate(pair)

        summary = data.to_decision_summary()
        details = json.dumps(data.model_dump(), default=str, indent=2)
        return f"{summary}\n\n<details>\n{details}\n</details>"

    except BlocksizeAPIError as e:
        return json.dumps(ErrorResponse(
            error_code="BLOCKSIZE_API_ERROR",
            message=f"Failed to retrieve FX rate for '{pair}'",
            details=str(e),
        ).model_dump())
    except Exception as e:
        logger.error("Error in get_fx_rate(%s): %s", pair, e, exc_info=True)
        return json.dumps(ErrorResponse(
            error_code="INTERNAL_ERROR",
            message=f"Error retrieving FX rate for '{pair}'",
            details=str(e),
        ).model_dump())


@mcp.tool
async def get_metal_price(ticker: str) -> str:
    """
    Get the spot price for a precious or industrial metal.

    Available metals: Gold (xauusd), Silver (xagusd), Platinum (xptusd),
    Palladium (xpdusd), Copper (copperusd).

    Cost: $0.005 USDC per call (TradFi tier).

    Args:
        ticker: Metal ticker (e.g., 'xauusd' for gold, 'xagusd' for silver).
    """
    try:
        client = await _get_client()
        data = await client.get_metal_price(ticker)

        summary = data.to_decision_summary()
        details = json.dumps(data.model_dump(), default=str, indent=2)
        return f"{summary}\n\n<details>\n{details}\n</details>"

    except BlocksizeAPIError as e:
        return json.dumps(ErrorResponse(
            error_code="BLOCKSIZE_API_ERROR",
            message=f"Failed to retrieve metal price for '{ticker}'",
            details=str(e),
        ).model_dump())
    except Exception as e:
        logger.error("Error in get_metal_price(%s): %s", ticker, e, exc_info=True)
        return json.dumps(ErrorResponse(
            error_code="INTERNAL_ERROR",
            message=f"Error retrieving metal price for '{ticker}'",
            details=str(e),
        ).model_dump())


# ===========================================================================
# DISCOVERY TOOLS (FREE)
# ===========================================================================

@mcp.tool
async def search_pairs(query: str, asset_class: str = "all") -> str:
    """
    Search through 30,000+ instruments across all asset classes. FREE.

    Searches crypto, equities, FX, and metals. Returns matching pairs
    with their available data services and pricing tier.

    Cost: FREE — no payment required.

    Args:
        query: Search term (e.g., 'btc', 'AAPL', 'eurusd', 'gold').
        asset_class: Filter by class. Options: 'all', 'crypto', 'equity', 'fx', 'metal'.
                     Default: 'all'.
    """
    try:
        client = await _get_client()
        pairs = await client.search_pairs(query, asset_class)

        response = PairSearchResponse(
            query=query,
            total_matches=len(pairs),
            pairs=pairs,
        )

        if not pairs:
            return f"No instruments found matching '{query}' (class: {asset_class})."

        pair_list = ", ".join(f"{p.pair} ({p.tier})" for p in pairs[:10])
        summary = (
            f"Found {len(pairs)} instruments matching '{query}': {pair_list}"
            + (f" ... and {len(pairs) - 10} more" if len(pairs) > 10 else "")
        )

        details = json.dumps(response.model_dump(), default=str, indent=2)
        return f"{summary}\n\n<details>\n{details}\n</details>"

    except Exception as e:
        logger.error("Error in search_pairs(%s): %s", query, e, exc_info=True)
        return json.dumps(ErrorResponse(
            error_code="INTERNAL_ERROR",
            message=f"Error searching for '{query}'",
            details=str(e),
        ).model_dump())


@mcp.tool
async def list_instruments(service: str = "vwap") -> str:
    """
    List all available instruments for a Blocksize data service. FREE.

    Args:
        service: Service to list. Options:
                 'vwap' — Real-time VWAP crypto (9,410 pairs)
                 'bidask' — Bid/Ask crypto (1,962 pairs)
                 'equity' — US + Chinese equities (18,071)
                 'fx' — FX currency pairs (129)
                 Default: 'vwap'.

    Cost: FREE — no payment required.
    """
    try:
        client = await _get_client()

        if service == "vwap":
            instruments = await client.list_vwap_instruments()
        elif service == "bidask":
            instruments = await client.list_bidask_instruments()
        elif service == "equity":
            instruments = await client.list_equity_instruments()
        elif service == "fx":
            instruments = await client.list_fx_instruments()
        else:
            return json.dumps(ErrorResponse(
                error_code="INVALID_SERVICE",
                message=f"Unknown service '{service}'. Use 'vwap', 'bidask', 'equity', or 'fx'.",
            ).model_dump())

        response = InstrumentListResponse(
            service=service,
            total_instruments=len(instruments),
            instruments=instruments,
        )

        sample = ", ".join(instruments[:10])
        summary = (
            f"{len(instruments)} instruments for {service}: {sample}"
            + (f" ... and {len(instruments) - 10} more" if len(instruments) > 10 else "")
        )

        details = json.dumps(response.model_dump(), default=str, indent=2)
        return f"{summary}\n\n<details>\n{details}\n</details>"

    except BlocksizeAPIError as e:
        return json.dumps(ErrorResponse(
            error_code="BLOCKSIZE_API_ERROR",
            message=f"Failed to list instruments for '{service}'",
            details=str(e),
        ).model_dump())
    except Exception as e:
        logger.error("Error in list_instruments(%s): %s", service, e, exc_info=True)
        return json.dumps(ErrorResponse(
            error_code="INTERNAL_ERROR",
            message=f"Error listing instruments for '{service}'",
            details=str(e),
        ).model_dump())


@mcp.tool
async def get_pricing_info() -> str:
    """
    Get complete pricing information for all Blocksize Capital data tools. FREE.

    Returns the cost per call for each tool, tiered by asset class,
    supported payment networks (Solana + Base), and wallet address.

    Cost: FREE — no payment required.
    """
    pricing = {
        "provider": "Blocksize Capital",
        "payment_protocol": "x402 (HTTP 402)",
        "settlement_networks": {
            "primary": {
                "network": "Solana",
                "caip2": settings.x402.solana_network,
                "wallet": settings.x402.solana_wallet_address or "(not configured)",
                "currency": "USDC (SPL)",
                "settlement_time": "~400ms",
            },
            "fallback": {
                "network": "Base L2",
                "caip2": settings.x402.base_network,
                "wallet": settings.x402.evm_wallet_address or "(not configured)",
                "currency": "USDC (ERC-20)",
                "settlement_time": "~2s",
            },
        },
        "tiers": settings.pricing_summary,
        "coverage": {
            "rt_vwap_crypto": "9,410 pairs (800 base currencies)",
            "bidask_crypto": "1,962 pairs (629 base currencies)",
            "vwap_30min_crypto": "251 tickers",
            "vwap_24hr_crypto": "42 tickers",
            "equities": "18,071 US + Chinese stocks",
            "fx": "129 currency pairs",
            "metals": "Gold, Silver, Platinum, Palladium, Copper",
            "state_price": "Available",
            "dexs": "56 DEXs across 11 chains",
        },
        "competitive_note": (
            "An agent making 10,000 VWAP calls/month pays ~$20 vs. Pyth Pro $2,000+/month. "
            "No subscription required — pure pay-per-use."
        ),
    }

    summary = (
        "Blocksize Capital Pricing (USDC per call):\n"
        f"  🆓 Discovery:      FREE (search, list, pricing)\n"
        f"  📊 Core Crypto:    ${settings.pricing.core_crypto} (top 250 by market cap)\n"
        f"  📊 Extended Crypto: ${settings.pricing.extended_crypto} (550+ niche assets)\n"
        f"  🏦 TradFi:         ${settings.pricing.tradfi} (FX, metals)\n"
        f"  📈 Equities:       ${settings.pricing.equities} (18k US + China stocks)\n"
        f"  ⏱️  Analytics:      ${settings.pricing.analytics} (30-min + 24-hr VWAP)\n"
        f"\nPayment: Solana (primary) or Base L2 (fallback)"
    )

    details = json.dumps(pricing, indent=2, default=str)
    return f"{summary}\n\n<details>\n{details}\n</details>"


# ---------------------------------------------------------------------------
# MCP Resources (Read-Only Context)
# ---------------------------------------------------------------------------

@mcp.resource("blocksize://info")
async def server_info() -> str:
    """Blocksize Capital MCP server information and capabilities."""
    return json.dumps({
        "name": "Blocksize Capital MCP Server",
        "version": "0.2.0",
        "description": (
            "Institutional-grade multi-asset market data for AI agents. "
            "30,000+ instruments across crypto, equities, FX, metals, and analytics. "
            "Pay per call via x402."
        ),
        "data_source": "Blocksize Capital (Tier 1 Pyth Publisher)",
        "asset_classes": ["crypto", "equities", "fx", "metals", "commodities", "analytics"],
        "tools": [
            "get_vwap", "get_bid_ask", "get_vwap_30min", "get_vwap_24hr",
            "get_state_price", "get_equity", "get_fx_rate", "get_metal_price",
            "search_pairs", "list_instruments", "get_pricing_info",
        ],
        "payment": {
            "protocol": "x402",
            "networks": ["Solana (primary)", "Base L2 (fallback)"],
            "currency": "USDC",
            "model": "tiered pay-per-call (no subscription)",
        },
    }, indent=2)


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the MCP server."""
    transport = settings.server.mcp_transport

    logger.info("Starting Blocksize Capital MCP Server v0.2.0")
    logger.info("Transport: %s", transport)
    logger.info("Tools: 13 tools across 6 asset classes")

    if transport == "streamable-http":
        mcp.run(
            transport="streamable-http",
            host="0.0.0.0",
            port=settings.server.mcp_server_port,
        )
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
