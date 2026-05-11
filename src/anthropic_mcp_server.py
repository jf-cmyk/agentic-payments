"""Anthropic-safe remote MCP server with daily user credits."""

from __future__ import annotations

from src import anthropic_auth
from src.authenticated_mcp_server import (
    TOOL_COSTS as SHARED_TOOL_COSTS,
    create_authenticated_market_data_mcp,
)
from src.blocksize_client import BlocksizeClient
from src.entitlement_manager import EntitlementManager
from src.mcp_server import READ_ONLY_TOOL_ANNOTATIONS

TOOL_COSTS = SHARED_TOOL_COSTS

__all__ = [
    "READ_ONLY_TOOL_ANNOTATIONS",
    "TOOL_COSTS",
    "anthropic_get_bid_ask",
    "anthropic_get_credit_balance",
    "anthropic_get_fx_rate",
    "anthropic_get_metal_price",
    "anthropic_get_vwap",
    "anthropic_info",
    "anthropic_list_instruments",
    "anthropic_mcp",
    "anthropic_search_pairs",
]

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
        _entitlements = EntitlementManager()
    return _entitlements


def _resolve_identity():
    return anthropic_auth.resolve_anthropic_identity()


_bundle = create_authenticated_market_data_mcp(
    mcp_name="Blocksize Market Data",
    instructions=(
        "Read-only Blocksize Capital market data for Claude. The connector uses "
        "server-side daily credits for live market data access. It exposes only "
        "safe, read-only data and metadata tools."
    ),
    auth_provider=anthropic_auth.build_anthropic_auth_provider(),
    resolve_identity=_resolve_identity,
    get_client=_get_client,
    get_entitlements=_get_entitlements,
    client_label="Claude",
    resource_uri="blocksize://anthropic-info",
)

anthropic_mcp = _bundle.mcp
anthropic_search_pairs = _bundle.search_pairs
anthropic_list_instruments = _bundle.list_instruments
anthropic_get_credit_balance = _bundle.get_credit_balance
anthropic_get_vwap = _bundle.get_vwap
anthropic_get_bid_ask = _bundle.get_bid_ask
anthropic_get_fx_rate = _bundle.get_fx_rate
anthropic_get_metal_price = _bundle.get_metal_price
anthropic_info = _bundle.info
