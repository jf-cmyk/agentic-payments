"""OpenAI and ChatGPT remote MCP server with authenticated live data."""

from __future__ import annotations

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

TOOL_COSTS = SHARED_TOOL_COSTS

__all__ = [
    "TOOL_COSTS",
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
    instructions=(
        "Read-only Blocksize Capital live market data for ChatGPT and OpenAI "
        "Responses API clients across crypto VWAP, supported equity bid/ask, FX, "
        "and metals. OAuth users receive a 50-credit starter allowance; production "
        "usage can continue through direct x402 or a Blocksize authenticated account "
        "plan. All tools are read-only: they never place trades, move funds, sign "
        "wallet messages, or submit x402 payment proofs."
    ),
    auth_provider=openai_auth.build_openai_auth_provider(),
    resolve_identity=_resolve_identity,
    get_client=_get_client,
    get_entitlements=_get_entitlements,
    client_label="OpenAI",
    resource_uri="blocksize://openai-info",
)

openai_mcp = _bundle.mcp
openai_search_pairs = _bundle.search_pairs
openai_list_instruments = _bundle.list_instruments
openai_get_credit_balance = _bundle.get_credit_balance
openai_get_vwap = _bundle.get_vwap
openai_get_bid_ask = _bundle.get_bid_ask
openai_get_fx_rate = _bundle.get_fx_rate
openai_get_metal_price = _bundle.get_metal_price
openai_info = _bundle.info
