"""
Blocksize Capital MCP Server — Agent-Facing Tool Interface.

This is the primary entry point for AI agents. It exposes Blocksize Capital's
full multi-asset data platform as MCP tools that any LLM client (Claude Desktop,
Cursor, LangChain, etc.) can discover and call.

Asset Classes:
  - Crypto:     Real-time VWAP and shared bid/ask snapshots
  - FX:         Shared bid/ask snapshots where supported by the upstream key
  - Metals:     Gold, Silver, Platinum, Palladium, Copper

Payment Model:
  Tiered pricing in USDC — settled on Solana (primary) or Base L2 (fallback).
  Discovery tools are FREE. Data tools: $0.002–$0.005 per call.
"""

from __future__ import annotations

import json
import logging
import sys

from fastmcp import FastMCP

from src.blocksize_client import BlocksizeClient, BlocksizeAPIError
from src.config import settings
from src.models import (
    BidAskResponse,
    ErrorResponse,
    InstrumentListResponse,
    PairSearchResponse,
    VWAPResponse,
)
from src.public_metadata import (
    AGENT_MANUAL_URL,
    APP_VERSION,
    DATA_CATALOG_URL,
    DISCOVERABLE_SYMBOL_COUNT,
    INSTRUMENT_COUNTS,
    MCP_MANIFEST_URL,
    OPENAPI_URL,
    PRIVACY_POLICY_URL,
    PROMPT_EXAMPLES_URL,
    PUBLIC_BASE_URL,
    QUICKSTART_URL,
    REPOSITORY_URL,
    REMOTE_MCP_URL,
    SERVER_JSON_URL,
    SUPPORT_URL,
    SWAGGER_URL,
    get_static_document,
    search_api_url,
    search_static_documents,
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
        "Access real-time VWAP, bid/ask spreads, FX, and metals across "
        "thousands of discoverable symbols. "
        "Outlier-filtered, decision-ready output. "
        "Pay per call via x402 (USDC on Solana or Base L2). "
        "No subscription required."
    ),
)

_client: BlocksizeClient | None = None
READ_ONLY_TOOL_ANNOTATIONS = {
    "readOnlyHint": True,
    "idempotentHint": True,
    "openWorldHint": True,
}


async def _get_client() -> BlocksizeClient:
    """Get or initialize the Blocksize API client."""
    global _client
    if _client is None:
        _client = BlocksizeClient()
    return _client


def _error_payload(error_code: str, message: str, details: str | None = None) -> str:
    """Build a consistent JSON error string for MCP tools."""
    return json.dumps(
        ErrorResponse(
            error_code=error_code,
            message=message,
            details=details,
        ).model_dump()
    )


def _instrument_search_result(pair_info) -> dict[str, object]:
    """Convert a pair search match into an OpenAI-style search result."""
    pair = pair_info.pair.upper()
    title = f"{pair} market data"
    return {
        "id": f"instrument:{pair_info.asset_class}:{pair_info.pair}",
        "title": title,
        "url": search_api_url(pair_info.pair),
        "metadata": {
            "type": "instrument",
            "asset_class": pair_info.asset_class,
            "services": pair_info.services,
            "pricing_tier": pair_info.tier,
        },
    }


def _instrument_fetch_payload(pair_info) -> dict[str, object]:
    """Convert a pair search match into a document-style payload."""
    pair = pair_info.pair.upper()
    services = ", ".join(pair_info.services)
    endpoints = []
    if "vwap" in pair_info.services:
        endpoints.append(f"GET {PUBLIC_BASE_URL}/v1/vwap/{pair}")
    if pair_info.asset_class == "fx":
        endpoints.append(f"GET {PUBLIC_BASE_URL}/v1/fx/{pair}")
    elif pair_info.asset_class == "metal":
        endpoints.append(f"GET {PUBLIC_BASE_URL}/v1/metal/{pair}")
    elif "bidask" in pair_info.services:
        endpoints.append(f"GET {PUBLIC_BASE_URL}/v1/bidask/{pair}")

    text = (
        f"{pair} is available through Blocksize Capital as a {pair_info.asset_class} "
        f"instrument. Available services: {services}. Pricing tier: {pair_info.tier}. "
        "Live paid HTTP API access is pay-per-call through x402 or wallet credits. "
        f"Use the remote MCP discovery tools to find instruments, then call the "
        f"paid HTTP API endpoints documented in {SWAGGER_URL}. "
        f"Suggested endpoints: {'; '.join(endpoints) if endpoints else SWAGGER_URL}."
    )
    return {
        "id": f"instrument:{pair_info.asset_class}:{pair_info.pair}",
        "title": f"{pair} market data",
        "text": text,
        "url": search_api_url(pair_info.pair),
        "metadata": {
            "type": "instrument",
            "asset_class": pair_info.asset_class,
            "services": pair_info.services,
            "pricing_tier": pair_info.tier,
        },
    }


# ===========================================================================
# CRYPTO TOOLS
# ===========================================================================

@mcp.tool(
    title="Crypto VWAP Snapshot",
    description=(
        "Use this when you need the latest institutional crypto VWAP for one "
        "trading pair. Returns a single paid snapshot only; use search_pairs "
        "first if you do not know the exact symbol."
    ),
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
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


@mcp.tool(
    title="Crypto Bid Ask Snapshot",
    description=(
        "Use this when you need the current bid, ask, and spread for one crypto "
        "pair. Returns a single paid snapshot only; it does not stream quotes."
    ),
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
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


# ===========================================================================
# FX & METALS TOOLS
# ===========================================================================

@mcp.tool(
    title="FX Snapshot",
    description=(
        "Use this when you need the latest bid, ask, and mid rate for one FX pair. "
        "Returns a single paid snapshot only."
    ),
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
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


@mcp.tool(
    title="Metal Snapshot",
    description=(
        "Use this when you need the latest spot price for one supported metal ticker. "
        "Returns a single paid snapshot only."
    ),
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
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

@mcp.tool(
    title="Instrument Search",
    description=(
        "Use this to discover valid crypto, FX, or metal symbols before "
        "calling paid market data tools. This is free and does not return live prices."
    ),
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
async def search_pairs(query: str, asset_class: str = "all") -> str:
    """
    Search through the currently enabled upstream symbols. FREE.

    Searches crypto, FX, and metals. Returns matching pairs
    with their available data services and pricing tier.

    Cost: FREE — no payment required.

    Args:
        query: Search term (e.g., 'btc', 'eurusd', 'xauusd').
        asset_class: Filter by class. Options: 'all', 'crypto', 'fx', 'metal'.
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


@mcp.tool(
    title="Instrument List",
    description=(
        "Use this to list all supported instruments for one Blocksize service. "
        "This is free and is best for capability discovery, not live market data."
    ),
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
async def list_instruments(service: str = "vwap") -> str:
    """
    List all available instruments for a Blocksize data service. FREE.

    Args:
        service: Service to list. Options:
                 'vwap' — Real-time VWAP crypto pairs
                 'bidask' — Shared upstream bid/ask instrument namespace
                 'fx' — FX currency pairs derived from the shared bid/ask namespace
                 'metal' — Supported metal tickers exposed by this gateway
                 Default: 'vwap'.

    Cost: FREE — no payment required.
    """
    try:
        client = await _get_client()

        if service == "vwap":
            instruments = await client.list_vwap_instruments()
        elif service == "bidask":
            instruments = await client.list_bidask_instruments()
        elif service == "fx":
            instruments = await client.list_fx_instruments()
        elif service == "metal":
            instruments = await client.list_metal_instruments()
        else:
            return json.dumps(ErrorResponse(
                error_code="INVALID_SERVICE",
                message=f"Unknown service '{service}'. Use 'vwap', 'bidask', 'fx', or 'metal'.",
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


@mcp.tool(
    title="Pricing Information",
    description=(
        "Use this to inspect the current free and paid tiers, settlement networks, "
        "and pricing guidance before using Blocksize data tools."
    ),
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
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
            "rt_vwap_crypto": "6,362 enabled crypto pairs",
            "shared_bidask_namespace": "2,365 enabled upstream symbols",
            "fx": "3 enabled FX pairs",
            "metals": "Gold, Silver, Platinum, Palladium, Copper",
            "dexs": "56 DEXs across 11 chains",
        },
        "competitive_note": (
            "An agent making 10,000 VWAP calls/month pays ~$20 vs. Pyth Pro $2,000+/month. "
            "Pure pay-per-use, or explore traditional flat-rate subscriptions for high consumption agents at https://blocksize.info/crypto-market-data/#pricing."
        ),
    }

    summary = (
        "Blocksize Capital Pricing (USDC per call):\n"
        f"  🆓 Discovery:      FREE (search, list, pricing)\n"
        f"  📊 Core Crypto:    ${settings.pricing.core_crypto} (high-liquidity VWAP pairs)\n"
        f"  📊 Extended Crypto: ${settings.pricing.extended_crypto} (shared bid/ask crypto pairs)\n"
        f"  🏦 TradFi:         ${settings.pricing.tradfi} (FX, metals)\n"
        f"\nPayment: Solana (primary) or Base L2 (fallback)\n"
        f"High Consumption Agents: Subscriptions available at https://blocksize.info/crypto-market-data/#pricing"
    )

    details = json.dumps(pricing, indent=2, default=str)
    return f"{summary}\n\n<details>\n{details}\n</details>"


# ---------------------------------------------------------------------------
# OPENAI-STYLE DISCOVERY TOOLS
# ---------------------------------------------------------------------------

@mcp.tool(
    title="Catalog Search",
    description=(
        "Use this when a remote MCP client needs OpenAI-style search across "
        "Blocksize documentation and free instrument metadata. This does not "
        "return paid live market data."
    ),
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
async def search(query: str) -> str:
    """
    Search Blocksize documents and instrument metadata using an OpenAI-compatible shape.

    Returns a JSON document with a single top-level `results` array containing
    documentation pages and supported instrument matches.
    """
    try:
        results = search_static_documents(query)
        if not results:
            for fallback_id in ("doc:quickstart", "doc:pricing"):
                doc = get_static_document(fallback_id)
                if doc is None:
                    continue
                results.append(
                    {
                        "id": doc["id"],
                        "title": doc["title"],
                        "url": doc["url"],
                        "metadata": doc["metadata"],
                    }
                )

        client = await _get_client()
        instrument_matches = await client.search_pairs(query, "all")
        for match in instrument_matches[:10]:
            results.append(_instrument_search_result(match))

        deduped: list[dict[str, object]] = []
        seen_ids: set[str] = set()
        for item in results:
            item_id = str(item["id"])
            if item_id in seen_ids:
                continue
            seen_ids.add(item_id)
            deduped.append(item)

        return json.dumps({"results": deduped[:12]}, indent=2)
    except Exception as e:
        logger.error("Error in search(%s): %s", query, e, exc_info=True)
        return _error_payload(
            "INTERNAL_ERROR",
            f"Error searching the Blocksize catalog for '{query}'",
            str(e),
        )


@mcp.tool(
    title="Catalog Fetch",
    description=(
        "Use this when a remote MCP client needs the full contents of a document "
        "or instrument guide returned by search. This does not execute paid "
        "market data requests."
    ),
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
async def fetch(id: str) -> str:
    """
    Fetch one document-style payload for a known documentation or instrument id.

    Supported ids:
    - doc:<slug>
    - instrument:<asset_class>:<pair>
    """
    try:
        static_doc = get_static_document(id)
        if static_doc is not None:
            return json.dumps(static_doc, indent=2)

        if id.startswith("instrument:"):
            _, asset_class, pair = id.split(":", 2)
            client = await _get_client()
            matches = await client.search_pairs(pair, asset_class)
            exact = next(
                (
                    match for match in matches
                    if match.pair.lower() == pair.lower()
                    and match.asset_class.lower() == asset_class.lower()
                ),
                None,
            )
            if exact is None:
                return _error_payload(
                    "NOT_FOUND",
                    f"No instrument metadata found for '{id}'",
                )
            return json.dumps(_instrument_fetch_payload(exact), indent=2)

        return _error_payload(
            "INVALID_ID",
            "Unsupported fetch id. Use an id returned by the search tool.",
        )
    except ValueError:
        return _error_payload(
            "INVALID_ID",
            "Unsupported fetch id. Use an id returned by the search tool.",
        )
    except Exception as e:
        logger.error("Error in fetch(%s): %s", id, e, exc_info=True)
        return _error_payload(
            "INTERNAL_ERROR",
            f"Error fetching '{id}'",
            str(e),
        )


# ---------------------------------------------------------------------------
# MCP Resources (Read-Only Context)
# ---------------------------------------------------------------------------

@mcp.resource("blocksize://info")
async def server_info() -> str:
    """Blocksize Capital MCP server information and capabilities."""
    return json.dumps({
        "name": "Blocksize Capital MCP Server",
        "version": APP_VERSION,
        "description": (
            "Institutional-grade multi-asset market data for AI agents. "
            "Discovery across crypto, FX, and metals plus "
            "paid HTTP market data access via x402. "
            "Pay per call via x402."
        ),
        "data_source": "Blocksize Capital (Tier 1 Pyth Publisher)",
        "asset_classes": ["crypto", "fx", "metals"],
        "tools": [
            "get_vwap", "get_bid_ask", "get_fx_rate", "get_metal_price",
            "search_pairs", "list_instruments", "get_pricing_info", "search", "fetch",
        ],
        "payment": {
            "protocol": "x402",
            "networks": ["Solana (primary)", "Base L2 (fallback)"],
            "currency": "USDC",
            "model": "tiered pay-per-call (no subscription)",
        },
        "coverage": {
            "discoverable_symbols": DISCOVERABLE_SYMBOL_COUNT,
            **INSTRUMENT_COUNTS,
        },
        "links": {
            "remote_mcp": REMOTE_MCP_URL,
            "manifest": MCP_MANIFEST_URL,
            "openapi": OPENAPI_URL,
            "swagger": SWAGGER_URL,
            "quickstart": QUICKSTART_URL,
            "prompt_examples": PROMPT_EXAMPLES_URL,
            "privacy_policy": PRIVACY_POLICY_URL,
            "support": SUPPORT_URL,
            "server_json": SERVER_JSON_URL,
            "agent_manual": AGENT_MANUAL_URL,
            "data_catalog": DATA_CATALOG_URL,
            "repository": REPOSITORY_URL,
        },
    }, indent=2)


# ---------------------------------------------------------------------------
# Entry Point
# ---------------------------------------------------------------------------

def main() -> None:
    """Run the MCP server."""
    transport = settings.server.mcp_transport

    logger.info("Starting Blocksize Capital MCP Server v%s", APP_VERSION)
    logger.info("Transport: %s", transport)
    logger.info("Tools: 9 tools across 3 asset classes")

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
