"""Cursor remote MCP server with Clerk signup and daily user credits."""

from __future__ import annotations

from src import cursor_auth
from src.authenticated_mcp_server import (
    TOOL_COSTS as SHARED_TOOL_COSTS,
    create_authenticated_market_data_mcp,
)
from src.blocksize_client import BlocksizeClient
from src.entitlement_manager import EntitlementManager

TOOL_COSTS = SHARED_TOOL_COSTS

__all__ = [
    "TOOL_COSTS",
    "cursor_get_bid_ask",
    "cursor_get_credit_balance",
    "cursor_get_fx_rate",
    "cursor_get_metal_price",
    "cursor_get_vwap",
    "cursor_info",
    "cursor_list_instruments",
    "cursor_mcp",
    "cursor_search_pairs",
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
    return cursor_auth.resolve_cursor_identity()


_bundle = create_authenticated_market_data_mcp(
    mcp_name="Blocksize Market Data for Cursor",
    instructions=(
        "Read-only Blocksize Capital market data for Cursor. Sign in with "
        "Blocksize through Clerk to use server-side daily credits for live "
        "market data. This connector never executes wallet transactions or "
        "submits x402 payment proofs from Cursor."
    ),
    auth_provider=cursor_auth.build_cursor_auth_provider(),
    resolve_identity=_resolve_identity,
    get_client=_get_client,
    get_entitlements=_get_entitlements,
    client_label="Cursor",
    resource_uri="blocksize://cursor-info",
)

cursor_mcp = _bundle.mcp
cursor_search_pairs = _bundle.search_pairs
cursor_list_instruments = _bundle.list_instruments
cursor_get_credit_balance = _bundle.get_credit_balance
cursor_get_vwap = _bundle.get_vwap
cursor_get_bid_ask = _bundle.get_bid_ask
cursor_get_fx_rate = _bundle.get_fx_rate
cursor_get_metal_price = _bundle.get_metal_price
cursor_info = _bundle.info
