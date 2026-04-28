"""Public remote MCP server for discovery, docs, and listing surfaces."""

from __future__ import annotations

import json

from fastmcp import FastMCP

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
    PRIVACY_POLICY_URL,
    PROMPT_EXAMPLES_URL,
    PUBLIC_BASE_URL,
    QUICKSTART_URL,
    REMOTE_MCP_URL,
    SUPPORT_URL,
)

public_mcp = FastMCP(
    "Blocksize Capital Remote Discovery",
    version=APP_VERSION,
    instructions=(
        "Public discovery MCP for Blocksize Capital. Use these tools to find "
        "supported instruments, inspect pricing, read integration docs, and fetch "
        "catalog metadata. This public remote server does not execute paid live "
        "market data calls directly; paid HTTP data access remains available through "
        "Blocksize's x402-protected REST API."
    ),
)


@public_mcp.tool(
    name="search_pairs",
    title="Instrument Search",
    description=(
        "Use this to discover supported crypto, FX, or metal symbols before "
        "using Blocksize's paid HTTP API. This is free and read-only."
    ),
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
async def public_search_pairs(query: str, asset_class: str = "all") -> str:
    """Search supported instruments on the public remote MCP surface."""
    return await search_local_pairs(query, asset_class)


@public_mcp.tool(
    name="list_instruments",
    title="Instrument List",
    description=(
        "Use this to list supported instruments for one service such as vwap, bidask, "
        "fx, or metal. This is free and read-only."
    ),
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
async def public_list_instruments(service: str = "vwap") -> str:
    """List supported instruments on the public remote MCP surface."""
    return await list_local_instruments(service)


@public_mcp.tool(
    name="get_pricing_info",
    title="Pricing Information",
    description=(
        "Use this to inspect the current free and paid tiers, supported settlement "
        "networks, and credit options before using Blocksize's paid HTTP API."
    ),
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
async def public_get_pricing_info() -> str:
    """Return pricing guidance for public discovery clients."""
    return await get_local_pricing_info()


@public_mcp.tool(
    name="search",
    title="Catalog Search",
    description=(
        "Use this when a remote MCP client needs OpenAI-style search across "
        "Blocksize docs and free catalog metadata. This does not return live "
        "market prices."
    ),
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
async def public_search(query: str) -> str:
    """Search docs and catalog entries in a document-oriented shape."""
    return await search_catalog(query)


@public_mcp.tool(
    name="fetch",
    title="Catalog Fetch",
    description=(
        "Use this to fetch the full contents of a document or instrument guide "
        "returned by search. This is read-only."
    ),
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
async def public_fetch(id: str) -> str:
    """Fetch one documentation or instrument payload."""
    return await fetch_catalog(id)


@public_mcp.resource("blocksize://public-info")
async def public_info() -> str:
    """Provide remote-discovery server metadata to MCP clients."""
    return json.dumps(
        {
            "name": "Blocksize Capital Remote Discovery",
            "version": APP_VERSION,
            "purpose": (
                "Public, read-only discovery server for listing directories, "
                "remote MCP clients, and installation workflows."
            ),
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
