"""OpenAI and ChatGPT remote MCP server with authenticated live data."""

from __future__ import annotations

import os

from src import openai_auth
from src.authenticated_mcp_server import (
    TOOL_COSTS as SHARED_TOOL_COSTS,
    create_authenticated_market_data_mcp,
)
from src.blocksize_client import BlocksizeClient
from src.entitlement_manager import (
    DEFAULT_DAILY_CREDITS,
    EntitlementManager,
    connector_entitlement_manager,
)
from src.free_tier import allowance_label

TOOL_COSTS = SHARED_TOOL_COSTS

__all__ = [
    "OPENAI_REVIEW_TOOLS",
    "TOOL_COSTS",
    "openai_tool_allowlist",
    "openai_get_bid_ask",
    "openai_get_credit_balance",
    "openai_get_fx_rate",
    "openai_get_metal_price",
    "openai_get_vwap",
    "openai_info",
    "openai_list_instruments",
    "openai_mcp",
    "openai_search_pairs",
]

# OpenAI's plugin dashboard stores only the first ten tools of a scan and
# never marks discovery complete (4 scans on 8 and 9 October 2026, OpenAI
# support confirms a platform-side persistence bug). Their workaround is to
# expose at most ten tools until review completes. This is that set; the
# Anthropic and Cursor connectors keep all tools. OPENAI_CONNECTOR_TOOLS=all
# restores every tool, a comma-separated list selects others.
OPENAI_REVIEW_TOOLS: tuple[str, ...] = (
    "search_pairs",
    "list_instruments",
    "get_credit_balance",
    "get_vwap",
    "get_bid_ask",
    "get_fx_rate",
    "get_metal_price",
    "get_state_price",
    "get_vwap_30m",
    "get_vwap_24h",
)


def openai_tool_allowlist(raw: str | None) -> frozenset[str] | None:
    """Return the tools the OpenAI connector exposes, or None for all of them."""
    value = (raw or "").strip()
    if not value:
        return frozenset(OPENAI_REVIEW_TOOLS)
    if value.lower() == "all":
        return None
    return frozenset(part.strip() for part in value.split(",") if part.strip())


_client: BlocksizeClient | None = None
_entitlements: EntitlementManager | None = None


async def _get_client() -> BlocksizeClient:
    global _client
    if _client is None:
        _client = BlocksizeClient()
    return _client


def _get_entitlements() -> EntitlementManager:
    global _entitlements
    if _entitlements is None:
        _entitlements = connector_entitlement_manager(
            "OPENAI",
            fallback_daily_credits=DEFAULT_DAILY_CREDITS,
        )
    return _entitlements


def _resolve_identity():
    return openai_auth.resolve_openai_identity()


_bundle = create_authenticated_market_data_mcp(
    mcp_name="Blocksize Market Data for OpenAI",
    # OpenAI's scanner may only use the first 512 characters; keep the whole
    # string within that so no length validator can hold the connector.
    instructions=(
        "Read-only Blocksize Capital live market data: crypto VWAP, equity "
        "bid/ask, FX, metals, AMM state prices, VWAP windows, market briefs, "
        "pre-trade checks, price receipts, macro snapshots and trader indicators. "
        f"Signed-in users get {allowance_label()} live-data credits a month; each "
        "tool reports its credit cost and the remaining balance, and "
        "get_credit_balance shows the allowance and reset date. Nothing here "
        "trades, moves funds or signs wallet messages. Cite the provider "
        "timestamp with every value."
    ),
    auth_provider=openai_auth.build_openai_auth_provider(),
    resolve_identity=_resolve_identity,
    get_client=_get_client,
    get_entitlements=_get_entitlements,
    client_label="OpenAI",
    resource_uri="blocksize://openai-info",
)

openai_mcp = _bundle.mcp

_allowed = openai_tool_allowlist(os.environ.get("OPENAI_CONNECTOR_TOOLS"))
if _allowed is not None:
    for _name in (
        "search_pairs", "list_instruments", "get_credit_balance", "get_vwap",
        "get_bid_ask", "get_fx_rate", "get_metal_price", *_bundle.products,
    ):
        if _name not in _allowed:
            openai_mcp.remove_tool(_name)
openai_search_pairs = _bundle.search_pairs
openai_list_instruments = _bundle.list_instruments
openai_get_credit_balance = _bundle.get_credit_balance
openai_get_vwap = _bundle.get_vwap
openai_get_bid_ask = _bundle.get_bid_ask
openai_get_fx_rate = _bundle.get_fx_rate
openai_get_metal_price = _bundle.get_metal_price
openai_info = _bundle.info
