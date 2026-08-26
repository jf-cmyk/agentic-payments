"""Public remote MCP server for discovery, docs, and listing surfaces."""

from __future__ import annotations

import json
from urllib.parse import quote
from typing import Annotated, Literal

from fastmcp import FastMCP
from fastmcp.server.dependencies import get_http_headers
from pydantic import Field

from src.mcp_server import (
    DISCOVERY_INSTRUMENT_DEFAULT_LIMIT,
    InstrumentPageLimit,
    InstrumentPageOffset,
    READ_ONLY_TOOL_ANNOTATIONS,
    fetch as fetch_catalog,
    get_pricing_info as get_local_pricing_info,
    list_instruments as list_local_instruments,
    search as search_catalog,
    search_pairs as search_local_pairs,
)
from src.observability import record_usage_event
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
    build_data_packages_json,
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
    Literal["vwap", "bidask", "state", "vwap30m", "vwap24h", "fx", "metal"],
    Field(
        description=(
            "Live HTTP data service to prepare: vwap for crypto VWAP, bidask for "
            "crypto pairs or supported equity/stock tickers such as AAPL, state for AMM state price, "
            "vwap30m for latest completed 30-minute close, vwap24h for fixed "
            "24-hour VWAP from the stream cache, fx for currency pairs, or metal for metals."
        ),
    ),
]
LiveMarketDataSymbol = Annotated[
    str,
    Field(
        description=(
            "Exact pair or ticker to use in the paid HTTP URL, such as BTC-USD, "
            "AAPL, MSFT, NVDA, EURUSD, or XAUUSD. Use search_pairs first if unsure."
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
            "for digital assets, equity/equities for supported stock tickers such as AAPL, "
            "fx for currency pairs, or metal for metals."
        ),
    ),
]
InstrumentService = Annotated[
    Literal["vwap", "bidask", "fx", "metal"],
    Field(
        description=(
            "Blocksize service namespace to list: vwap for crypto VWAP pairs, "
            "bidask for shared bid/ask symbols including supported equities, fx for FX pairs, or metal for metals."
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
PremiumWorkflowProduct = Annotated[
    Literal[
        "agent_market_brief",
        "pre_trade_sanity_check",
        "audit_grade_price_receipt",
        "multi_asset_macro_snapshot",
        "spend_controlled_market_monitor",
        "token_market_quality_indicator",
        "state_divergence_indicator",
        "solana_token_brief",
        "trader_alpha_pack",
    ],
    Field(description="Premium Blocksize workflow product to prepare."),
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


def _record_public_mcp_usage(tool_name: str, **fields: object) -> None:
    """Record public MCP usage with only the request user-agent for test tagging."""
    user_agent = get_http_headers().get("user-agent", "").strip()
    if user_agent:
        fields["user_agent"] = user_agent
    record_usage_event(
        "mcp_tool_call",
        surface="public_mcp",
        tool_name=tool_name,
        **fields,
    )


@public_mcp.tool(
    name="search_pairs",
    title="Instrument Search",
    description=(
            "Discover supported crypto, equity, FX, and metal symbols before using "
            "the paid HTTP API. Returns up to 50 catalog matches with asset class, "
            "available services, and pricing tier; "
        "it is free, read-only, and never returns live prices or starts payment."
    ),
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
async def public_search_pairs(
    query: InstrumentSearchQuery,
    asset_class: AssetClassFilter = "all",
) -> str:
    """Search supported instruments on the public remote MCP surface."""
    _record_public_mcp_usage(
        "search_pairs",
        subject=query,
        asset_class=asset_class,
    )
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
async def public_list_instruments(
    service: InstrumentService = "vwap",
    limit: InstrumentPageLimit = DISCOVERY_INSTRUMENT_DEFAULT_LIMIT,
    offset: InstrumentPageOffset = 0,
) -> str:
    """List supported instruments on the public remote MCP surface."""
    _record_public_mcp_usage(
        "list_instruments",
        subject=service,
    )
    return await list_local_instruments(service, limit, offset)


@public_mcp.tool(
    name="get_pricing_info",
    title="Pricing Information",
    description=(
        "Inspect current direct x402 per-call prices, authenticated connector "
        "starter-credit costs, supported USDC settlement networks, and the account-plan "
        "contact path. This is free read-only metadata; it does not initiate payment."
    ),
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
async def public_get_pricing_info() -> str:
    """Return pricing guidance for public discovery clients."""
    _record_public_mcp_usage("get_pricing_info")
    return await get_local_pricing_info()


@public_mcp.tool(
    name="get_product_catalog",
    title="Product Catalog",
    description=(
        "Inspect Blocksize raw data, supported equity ticker bid/ask, and premium "
        "agent-native workflow products, including starter-credit positioning, credit costs, suggested paid "
        "prices, endpoint templates, and upgrade path. This is free and read-only."
    ),
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
async def public_get_product_catalog() -> str:
    """Return product catalog guidance for agents and listing surfaces."""
    _record_public_mcp_usage("get_product_catalog")
    return json.dumps(build_data_packages_json(), indent=2)


@public_mcp.tool(
    name="get_workflow_endpoint",
    title="Premium Workflow Endpoint Builder",
    description=(
        "Build the exact paid HTTP endpoint, method, starter-credit cost, and "
        "example body for a premium Blocksize workflow. This is free and "
        "read-only; it does not fetch live data, charge credits, or start x402."
    ),
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
async def public_get_workflow_endpoint(product: PremiumWorkflowProduct) -> str:
    """Return the paid HTTP endpoint and example body for a premium workflow."""
    _record_public_mcp_usage(
        "get_workflow_endpoint",
        subject=product,
    )
    catalog: dict[str, dict[str, object]] = {
        "agent_market_brief": {
            "path": "/v1/briefs/market",
            "credit_cost": 10,
            "paid_price_usdc": "0.25",
            "example_body": {"symbols": ["BTCUSD", "ETHUSD"], "intent": "portfolio_update"},
        },
        "pre_trade_sanity_check": {
            "path": "/v1/checks/pre-trade",
            "credit_cost": 5,
            "paid_price_usdc": "0.10",
            "example_body": {
                "symbol": "BTCUSD",
                "side": "buy",
                "notional_usd": 2500,
                "reference_price": 67250.12,
                "max_spread_bps": 25,
            },
        },
        "audit_grade_price_receipt": {
            "path": "/v1/receipts/price",
            "credit_cost": 10,
            "paid_price_usdc": "0.25",
            "example_body": {
                "service": "vwap",
                "symbol": "BTCUSD",
                "purpose": "treasury_rebalance_reference",
            },
        },
        "multi_asset_macro_snapshot": {
            "path": "/v1/snapshots/macro",
            "credit_cost": 25,
            "paid_price_usdc": "1.00",
            "example_body": {"universe": ["BTCUSD", "ETHUSD", "EURUSD", "XAUUSD"]},
        },
        "spend_controlled_market_monitor": {
            "path": "/v1/monitors/evaluate",
            "credit_cost": 10,
            "paid_price_usdc": "0.25",
            "example_body": {
                "symbols": ["BTCUSD", "ETHUSD"],
                "rules": [{"metric": "spread_bps", "operator": ">", "value": 50}],
                "max_credits": 20,
            },
        },
        "token_market_quality_indicator": {
            "path": "/v1/indicators/token-quality",
            "credit_cost": 15,
            "paid_price_usdc": "0.50",
            "example_body": {
                "symbol": "SOLUSD",
                "include_state_coverage": True,
                "include_state_price": False,
                "include_windows": False,
                "max_spread_bps": 50,
                "max_state_divergence_bps": 75,
            },
        },
        "state_divergence_indicator": {
            "path": "/v1/indicators/state-divergence",
            "credit_cost": 15,
            "paid_price_usdc": "0.50",
            "example_body": {
                "symbol": "MSOLUSD",
                "max_divergence_bps": 75,
            },
        },
        "solana_token_brief": {
            "path": "/v1/signals/solana-token-brief",
            "credit_cost": 25,
            "paid_price_usdc": "1.00",
            "example_body": {
                "symbols": ["SOLUSD", "JUPUSD", "PYTHUSD", "MSOLUSD"],
                "include_state_coverage": True,
                "include_state_price": False,
                "include_windows": False,
            },
        },
        "trader_alpha_pack": {
            "path": "/v1/signals/trader-alpha-pack",
            "credit_cost": 50,
            "paid_price_usdc": "2.50",
            "example_body": {
                "watchlist": ["BTCUSD", "ETHUSD", "SOLUSD"],
                "include_state_coverage": True,
                "include_state_price": False,
                "include_windows": False,
            },
        },
    }
    item = catalog[product]
    return json.dumps(
        {
            "status": "ok",
            "product": product,
            "request": {
                "method": "POST",
                "url": f"{PUBLIC_BASE_URL}{item['path']}",
                "example_body": item["example_body"],
            },
            "readiness_check": {
                "method": "POST",
                "url": f"{PUBLIC_BASE_URL}/v1/capabilities/check",
                "example_body": {
                    "product": product,
                    **(
                        {"symbols": item["example_body"].get("symbols")}
                        if isinstance(item["example_body"], dict)
                        and item["example_body"].get("symbols")
                        else {"symbol": item["example_body"].get("symbol", "SOLUSD")}
                        if isinstance(item["example_body"], dict)
                        else {"symbol": "SOLUSD"}
                    ),
                    "optional_feeds": {
                        "state_coverage": False,
                        "state_price": False,
                        "vwap_windows": False,
                    },
                },
                "cost": "free",
                "purpose": "Verify required and optional feed coverage before spending credits on the paid workflow.",
            },
            "pricing": {
                "starter_credit_cost": item["credit_cost"],
                "paid_price_usdc": item["paid_price_usdc"],
                "starter_positioning": "Start with 50 live data credits",
                "upgrade_path": "x402 payment or an authenticated account plan",
            },
            "behavior": {
                "returns_live_data": False,
                "starts_payment": False,
                "side_effects": "none",
                "next_step": (
                    "Call the returned HTTP endpoint to receive a direct x402 payment "
                    "challenge. Starter credits are available only through an "
                    "authenticated connector, not caller-selected HTTP identity headers."
                ),
            },
            "links": {
                "openapi": OPENAPI_URL,
                "swagger": SWAGGER_URL,
                "quickstart": QUICKSTART_URL,
            },
        },
        indent=2,
    )


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
    _record_public_mcp_usage(
        "get_market_data_endpoint",
        subject=symbol.strip().upper(),
        asset_class=service,
    )
    clean_symbol = symbol.strip().upper()
    encoded_symbol = quote(clean_symbol, safe="-_")
    path = {
        "vwap": f"/v1/vwap/{encoded_symbol}",
        "bidask": f"/v1/bidask/{encoded_symbol}",
        "state": f"/v1/state/{encoded_symbol}",
        "vwap30m": f"/v1/vwap30m/{encoded_symbol}",
        "vwap24h": f"/v1/vwap24h/{encoded_symbol}",
        "fx": f"/v1/fx/{encoded_symbol}",
        "metal": f"/v1/metal/{encoded_symbol}",
    }[service]
    notes = {
        "vwap": "Crypto VWAP endpoint. Use search_pairs/list_instruments to confirm pair support.",
        "bidask": "Shared bid/ask endpoint for crypto pairs and supported equity tickers.",
        "state": "Pool-derived AMM state price endpoint. Use state-covered symbols such as MSOLUSD, JUPSOLUSD, or WSTETHUSD.",
        "vwap30m": "Latest completed 30-minute close endpoint backed by Blocksize closingprice_list; include_trades=true adds closingprice_trades evidence.",
        "vwap24h": "24-hour fixed VWAP endpoint backed by the Blocksize fixedvwap_subscribe websocket cache.",
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
    _record_public_mcp_usage(
        "search",
        subject=query,
    )
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
    _record_public_mcp_usage(
        "fetch",
        subject=id,
    )
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
                "starter_allowance": "Start with authenticated connector credits, then upgrade through x402 payment or an authenticated account plan.",
                "notes": (
                    "Live paid market data is exposed through the x402-protected HTTP "
                    "API and advanced local MCP setup, not this public remote server."
                ),
            },
        },
        indent=2,
    )
