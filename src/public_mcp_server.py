"""Public remote MCP server for discovery, docs, and listing surfaces."""

from __future__ import annotations

import json
from urllib.parse import quote
from typing import Annotated, Literal

from fastmcp import FastMCP
from pydantic import Field

from src.mcp_server import (
    READ_ONLY_TOOL_ANNOTATIONS,
    fetch as fetch_catalog,
    get_pricing_info as get_local_pricing_info,
    list_instruments as list_local_instruments,
    search as search_catalog,
    search_pairs as search_local_pairs,
)
from src.public_metadata import (
    AGENT_MANUAL_URL,
    APP_VERSION,
    MCP_MANIFEST_URL,
    OPENAPI_URL,
    PRICING_GUIDE_URL,
    PRIVACY_POLICY_URL,
    PROMPT_EXAMPLES_URL,
    PUBLIC_BASE_URL,
    PUBLIC_DESCRIPTION,
    PUBLIC_DISPLAY_NAME,
    QUICKSTART_URL,
    REMOTE_MCP_URL,
    SUPPORT_URL,
    SWAGGER_URL,
)

InstrumentSearchQuery = Annotated[
    str,
    Field(
        description=(
            "Symbol, ticker, asset, or pair to search for, such as BTC, BTC-USD, "
            "ETH, AAPL, EURUSD, or XAUUSD."
        ),
        min_length=1,
        max_length=80,
    ),
]
LiveMarketDataService = Annotated[
    Literal["vwap", "bidask", "fx", "metal"],
    Field(
        description=(
            "Live HTTP data service to prepare: vwap for crypto VWAP, bidask for "
            "crypto pairs or supported equity tickers, fx for currency pairs, or "
            "metal for metals."
        ),
    ),
]
LiveMarketDataSymbol = Annotated[
    str,
    Field(
        description=(
            "Exact pair or ticker to use in the paid HTTP URL, such as BTC-USD, "
            "AAPL, EURUSD, or XAUUSD. Use search_pairs first if unsure."
        ),
        min_length=2,
        max_length=80,
    ),
]
AssetClassFilter = Annotated[
    Literal["all", "crypto", "equity", "equities", "fx", "metal"],
    Field(
        description=(
            "Optional asset-class filter. Use all for the full catalog, crypto "
            "for digital assets, equity/equities for supported stock tickers, "
            "fx for currency pairs, or metal for metals."
        ),
    ),
]
InstrumentService = Annotated[
    Literal["vwap", "bidask", "fx", "metal"],
    Field(
        description=(
            "Blocksize service namespace to list: vwap for crypto VWAP pairs, "
            "bidask for shared bid/ask symbols, fx for FX pairs, or metal for metals."
        ),
    ),
]
CatalogSearchQuery = Annotated[
    str,
    Field(
        description=(
            "Documentation or catalog search query, such as pricing, quickstart, "
            "credits, x402, Solana, Base, BTC, or VWAP."
        ),
        min_length=1,
        max_length=120,
    ),
]
CatalogFetchId = Annotated[
    str,
    Field(
        description=(
            "Result id returned by the search tool, for example doc:pricing, "
            "doc:quickstart, or instrument:crypto:BTCUSD."
        ),
        min_length=1,
        max_length=160,
    ),
]

public_mcp = FastMCP(
    PUBLIC_DISPLAY_NAME,
    version=APP_VERSION,
    instructions=(
        f"{PUBLIC_DESCRIPTION} This connector is read-only and free to inspect. "
        "It never starts blockchain payments, stores credentials, or fetches paid "
        "live prices directly; agents use the returned HTTPS URLs with the "
        "x402-protected HTTP API when they are ready to purchase live data."
    ),
)


@public_mcp.tool(
    name="search_pairs",
    title="Instrument Search",
    description=(
        "Discover supported symbols before using the paid HTTP API. Returns up to "
        "50 catalog matches with asset class, available services, and pricing tier; "
        "it is free, read-only, and never returns live prices or starts payment."
    ),
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
async def public_search_pairs(
    query: InstrumentSearchQuery,
    asset_class: AssetClassFilter = "all",
) -> str:
    """Search supported instruments on the public remote MCP surface."""
    return await search_local_pairs(query, asset_class)


@public_mcp.tool(
    name="list_instruments",
    title="Instrument List",
    description=(
        "List the supported instruments for one Blocksize service. This is free, "
        "read-only catalog metadata; it does not fetch live prices, create accounts, "
        "or start x402 payment."
    ),
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
async def public_list_instruments(service: InstrumentService = "vwap") -> str:
    """List supported instruments on the public remote MCP surface."""
    return await list_local_instruments(service)


@public_mcp.tool(
    name="get_pricing_info",
    title="Pricing Information",
    description=(
        "Inspect current per-call prices, bulk credit tiers, and supported Solana "
        "and Base USDC settlement networks. This is free and read-only planning "
        "metadata; it does not initiate payment."
    ),
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
async def public_get_pricing_info() -> str:
    """Return pricing guidance for public discovery clients."""
    return await get_local_pricing_info()


@public_mcp.tool(
    name="get_market_data_endpoint",
    title="Live Data Endpoint Builder",
    description=(
        "Build the exact x402-protected HTTP URL for one live market-data request. "
        "Returns method, URL, service notes, pricing docs, and next steps; it is "
        "read-only and does not fetch prices, charge a wallet, or submit payment."
    ),
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
async def public_get_market_data_endpoint(
    service: LiveMarketDataService,
    symbol: LiveMarketDataSymbol,
) -> str:
    """Return the paid HTTP endpoint an agent should call for live data."""
    clean_symbol = symbol.strip().upper()
    encoded_symbol = quote(clean_symbol, safe="-_")
    path = {
        "vwap": f"/v1/vwap/{encoded_symbol}",
        "bidask": f"/v1/bidask/{encoded_symbol}",
        "fx": f"/v1/fx/{encoded_symbol}",
        "metal": f"/v1/metal/{encoded_symbol}",
    }[service]
    notes = {
        "vwap": "Crypto VWAP endpoint. Use search_pairs/list_instruments to confirm pair support.",
        "bidask": "Shared bid/ask endpoint for crypto pairs and supported equity tickers.",
        "fx": "FX spot endpoint for supported currency pairs.",
        "metal": "Metals endpoint for supported precious/base metal tickers.",
    }
    return json.dumps(
        {
            "status": "ok",
            "request": {
                "method": "GET",
                "url": f"{PUBLIC_BASE_URL}{path}",
                "service": service,
                "symbol": clean_symbol,
            },
            "behavior": {
                "returns_live_data": False,
                "starts_payment": False,
                "side_effects": "none",
                "next_step": (
                    "Call the returned URL directly. Without payment it returns an HTTP 402 "
                    "x402 challenge; after valid USDC settlement it returns JSON market data."
                ),
            },
            "links": {
                "pricing": PRICING_GUIDE_URL,
                "openapi": OPENAPI_URL,
                "swagger": SWAGGER_URL,
                "quickstart": QUICKSTART_URL,
            },
            "notes": notes[service],
        },
        indent=2,
    )


@public_mcp.tool(
    name="search",
    title="Catalog Search",
    description=(
        "Search Blocksize documentation and catalog metadata by keyword. This "
        "free read-only search returns document and instrument ids for fetch; it "
        "does not return live prices or start payment."
    ),
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
async def public_search(query: CatalogSearchQuery) -> str:
    """Search docs and catalog entries in a document-oriented shape."""
    return await search_catalog(query)


@public_mcp.tool(
    name="fetch",
    title="Catalog Fetch",
    description=(
        "Fetch one document or instrument guide returned by search. This is "
        "free, read-only content retrieval with no account, credential, payment, "
        "or live-price side effects."
    ),
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
async def public_fetch(id: CatalogFetchId) -> str:
    """Fetch one documentation or instrument payload."""
    return await fetch_catalog(id)


@public_mcp.resource("blocksize://public-info")
async def public_info() -> str:
    """Provide remote-discovery server metadata to MCP clients."""
    return json.dumps(
        {
            "name": PUBLIC_DISPLAY_NAME,
            "version": APP_VERSION,
            "purpose": PUBLIC_DESCRIPTION,
            "links": {
                "homepage": PUBLIC_BASE_URL,
                "remote_mcp": REMOTE_MCP_URL,
                "manifest": MCP_MANIFEST_URL,
                "openapi": OPENAPI_URL,
                "quickstart": QUICKSTART_URL,
                "prompt_examples": PROMPT_EXAMPLES_URL,
                "privacy_policy": PRIVACY_POLICY_URL,
                "support": SUPPORT_URL,
                "agent_manual": AGENT_MANUAL_URL,
            },
            "paid_data_access": {
                "mode": "direct-http",
                "openapi": OPENAPI_URL,
                "notes": (
                    "Live paid market data is exposed through the x402-protected HTTP "
                    "API and advanced local MCP setup, not this public remote server."
                ),
            },
        },
        indent=2,
    )
