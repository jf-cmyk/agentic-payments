"""Anthropic-safe remote MCP server with daily user credits."""

from __future__ import annotations

import json
import logging
import re
from typing import Annotated, Awaitable, Callable, Literal, TypeVar

from fastmcp import FastMCP
from pydantic import Field

from src import anthropic_auth
from src.blocksize_client import BlocksizeAPIError, BlocksizeClient
from src.entitlement_manager import CreditStatus, EntitlementManager
from src.mcp_server import READ_ONLY_TOOL_ANNOTATIONS
from src.models import (
    BidAskResponse,
    ErrorResponse,
    InstrumentListResponse,
    PairSearchResponse,
    VWAPResponse,
)
from src.public_metadata import APP_VERSION, PUBLIC_BASE_URL

logger = logging.getLogger(__name__)

InstrumentSearchQuery = Annotated[
    str,
    Field(
        description="Symbol, ticker, asset, or pair to search for.",
        min_length=1,
        max_length=80,
    ),
]
AssetClassFilter = Annotated[
    Literal["all", "crypto", "fx", "metal"],
    Field(description="Optional asset class filter."),
]
InstrumentService = Annotated[
    Literal["vwap", "bidask", "fx", "metal"],
    Field(description="Blocksize service namespace to list."),
]
PairValue = Annotated[
    str,
    Field(description="Trading pair or ticker, such as BTC-USD, EURUSD, or XAUUSD."),
]

T = TypeVar("T")

TOOL_COSTS = {
    "search_pairs": 0,
    "list_instruments": 0,
    "get_credit_balance": 0,
    "get_vwap": 1,
    "get_bid_ask": 1,
    "get_fx_rate": 2,
    "get_metal_price": 2,
}
SYMBOL_RE = re.compile(r"^[A-Z0-9.]{2,32}$")

anthropic_mcp = FastMCP(
    "Blocksize Market Data",
    version=APP_VERSION,
    instructions=(
        "Read-only Blocksize Capital market data for Claude. The connector uses "
        "server-side daily credits for live market data access. It exposes only "
        "safe, read-only data and metadata tools."
    ),
    auth=anthropic_auth.build_anthropic_auth_provider(),
)

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


def _error_payload(error_code: str, message: str, details: str | None = None) -> str:
    return json.dumps(
        ErrorResponse(
            error_code=error_code,
            message=message,
            details=details,
        ).model_dump()
    )


def _credit_payload(status: CreditStatus) -> dict[str, object]:
    return {
        "user_id": status.user_id,
        "email": status.email,
        "date": status.date,
        "daily_limit": status.daily_limit,
        "credits_spent": status.credits_spent,
        "credits_remaining": status.credits_remaining,
        "status": status.status,
    }


def _normalise_symbol(value: str, field_name: str = "symbol") -> str:
    raw = value.strip()
    if len(raw) > 64:
        raise ValueError(f"{field_name} is too long")
    clean = raw.replace("-", "").replace("/", "").replace("_", "").upper()
    if (
        not SYMBOL_RE.fullmatch(clean)
        or clean.startswith(".")
        or clean.endswith(".")
        or ".." in clean
    ):
        raise ValueError(f"Invalid {field_name}; use 2-32 letters, digits, or dots")
    return clean


async def _with_credits(
    tool_name: str,
    subject: str,
    call: Callable[[], Awaitable[T]],
    render: Callable[[T], str],
) -> str:
    identity = anthropic_auth.resolve_anthropic_identity()
    if identity is None:
        return _error_payload(
            "AUTH_REQUIRED",
            "Connect with an authenticated Blocksize account to use live market data.",
        )

    cost = TOOL_COSTS[tool_name]
    entitlements = _get_entitlements()
    ok, status = entitlements.spend(
        identity.user_id,
        cost,
        email=identity.email,
        tool_name=tool_name,
        subject=subject,
    )
    if not ok:
        return _error_payload(
            "DAILY_CREDIT_LIMIT_REACHED",
            (
                "Daily Blocksize credit limit reached. Credits reset daily. "
                "Upgrade your Blocksize account outside Claude to increase this allowance."
            ),
            json.dumps(_credit_payload(status)),
        )

    try:
        result = await call()
        rendered = render(result)
    except BlocksizeAPIError as e:
        entitlements.refund(identity.user_id, cost, tool_name=tool_name, subject=subject)
        return _error_payload(
            "BLOCKSIZE_API_ERROR",
            f"Failed to retrieve data for '{subject}'",
            str(e),
        )
    except Exception as e:
        entitlements.refund(identity.user_id, cost, tool_name=tool_name, subject=subject)
        logger.error("Unexpected error in %s(%s): %s", tool_name, subject, e, exc_info=True)
        return _error_payload(
            "INTERNAL_ERROR",
            f"Error retrieving data for '{subject}'",
            str(e),
        )

    current = entitlements.status(identity.user_id, identity.email)
    return (
        f"{rendered}\n\n"
        f"Credits remaining today: {current.credits_remaining}/{current.daily_limit}"
    )


@anthropic_mcp.tool(
    name="search_pairs",
    title="Instrument Search",
    description=(
        "Search supported Blocksize crypto, FX, and metal instruments by symbol "
        "or asset name. This returns metadata only, not live prices."
    ),
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
async def anthropic_search_pairs(
    query: InstrumentSearchQuery,
    asset_class: AssetClassFilter = "all",
) -> str:
    try:
        client = await _get_client()
        pairs = await client.search_pairs(query, asset_class)
        response = PairSearchResponse(query=query, total_matches=len(pairs), pairs=pairs)
        if not pairs:
            return f"No instruments found matching '{query}' (class: {asset_class})."
        pair_list = ", ".join(f"{p.pair} ({p.tier})" for p in pairs[:10])
        summary = (
            f"Found {len(pairs)} instruments matching '{query}': {pair_list}"
            + (f" ... and {len(pairs) - 10} more" if len(pairs) > 10 else "")
        )
        return f"{summary}\n\n<details>\n{json.dumps(response.model_dump(), default=str, indent=2)}\n</details>"
    except Exception as e:
        logger.error("Error in anthropic_search_pairs(%s): %s", query, e, exc_info=True)
        return _error_payload("INTERNAL_ERROR", f"Error searching for '{query}'", str(e))


@anthropic_mcp.tool(
    name="list_instruments",
    title="Instrument List",
    description=(
        "List supported instruments for one Blocksize service. This returns "
        "metadata only, not live prices."
    ),
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
async def anthropic_list_instruments(service: InstrumentService = "vwap") -> str:
    try:
        client = await _get_client()
        if service == "vwap":
            instruments = await client.list_vwap_instruments()
        elif service == "bidask":
            instruments = await client.list_bidask_instruments()
        elif service == "fx":
            instruments = await client.list_fx_instruments()
        else:
            instruments = await client.list_metal_instruments()

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
        return f"{summary}\n\n<details>\n{json.dumps(response.model_dump(), default=str, indent=2)}\n</details>"
    except Exception as e:
        logger.error("Error in anthropic_list_instruments(%s): %s", service, e, exc_info=True)
        return _error_payload("INTERNAL_ERROR", f"Error listing instruments for '{service}'", str(e))


@anthropic_mcp.tool(
    name="get_credit_balance",
    title="Credit Balance",
    description="Show the authenticated user's remaining Blocksize daily data credits.",
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
async def anthropic_get_credit_balance() -> str:
    identity = anthropic_auth.resolve_anthropic_identity()
    if identity is None:
        return _error_payload(
            "AUTH_REQUIRED",
            "Connect with an authenticated Blocksize account to view daily credits.",
        )
    status = _get_entitlements().status(identity.user_id, identity.email)
    return json.dumps({"status": "ok", "credits": _credit_payload(status)}, indent=2)


@anthropic_mcp.tool(
    name="get_vwap",
    title="Crypto VWAP Snapshot",
    description=(
        "Get the latest institutional crypto VWAP for one trading pair. "
        "This read-only live data call uses daily Blocksize credits."
    ),
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
async def anthropic_get_vwap(pair: PairValue) -> str:
    try:
        clean_pair = _normalise_symbol(pair, "pair")
    except ValueError as e:
        return _error_payload("INVALID_SYMBOL", str(e))

    async def call():
        return await (await _get_client()).get_vwap_latest(clean_pair)

    def render(data) -> str:
        response = VWAPResponse(data=data)
        return (
            f"{data.to_decision_summary()}\n\n<details>\n"
            f"{json.dumps(response.model_dump(), default=str, indent=2)}\n</details>"
        )

    return await _with_credits("get_vwap", clean_pair, call, render)


@anthropic_mcp.tool(
    name="get_bid_ask",
    title="Crypto Bid Ask Snapshot",
    description=(
        "Get the latest bid, ask, and spread for one crypto pair. "
        "This read-only live data call uses daily Blocksize credits."
    ),
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
async def anthropic_get_bid_ask(pair: PairValue) -> str:
    try:
        clean_pair = _normalise_symbol(pair, "pair")
    except ValueError as e:
        return _error_payload("INVALID_SYMBOL", str(e))

    async def call():
        return await (await _get_client()).get_bidask_snapshot(clean_pair)

    def render(data) -> str:
        response = BidAskResponse(data=data)
        return (
            f"{data.to_decision_summary()}\n\n<details>\n"
            f"{json.dumps(response.model_dump(), default=str, indent=2)}\n</details>"
        )

    return await _with_credits("get_bid_ask", clean_pair, call, render)


@anthropic_mcp.tool(
    name="get_fx_rate",
    title="FX Snapshot",
    description=(
        "Get the latest bid, ask, and mid rate for one FX pair. "
        "This read-only live data call uses daily Blocksize credits."
    ),
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
async def anthropic_get_fx_rate(pair: PairValue) -> str:
    try:
        clean_pair = _normalise_symbol(pair, "pair")
    except ValueError as e:
        return _error_payload("INVALID_SYMBOL", str(e))

    async def call():
        return await (await _get_client()).get_fx_rate(clean_pair)

    def render(data) -> str:
        return (
            f"{data.to_decision_summary()}\n\n<details>\n"
            f"{json.dumps(data.model_dump(), default=str, indent=2)}\n</details>"
        )

    return await _with_credits("get_fx_rate", clean_pair, call, render)


@anthropic_mcp.tool(
    name="get_metal_price",
    title="Metal Snapshot",
    description=(
        "Get the latest spot price for one supported metal ticker. "
        "This read-only live data call uses daily Blocksize credits."
    ),
    annotations=READ_ONLY_TOOL_ANNOTATIONS,
)
async def anthropic_get_metal_price(ticker: PairValue) -> str:
    try:
        clean_ticker = _normalise_symbol(ticker, "ticker")
    except ValueError as e:
        return _error_payload("INVALID_SYMBOL", str(e))

    async def call():
        return await (await _get_client()).get_metal_price(clean_ticker)

    def render(data) -> str:
        return (
            f"{data.to_decision_summary()}\n\n<details>\n"
            f"{json.dumps(data.model_dump(), default=str, indent=2)}\n</details>"
        )

    return await _with_credits("get_metal_price", clean_ticker, call, render)


@anthropic_mcp.resource("blocksize://anthropic-info")
async def anthropic_info() -> str:
    return json.dumps(
        {
            "name": "Blocksize Market Data",
            "version": APP_VERSION,
            "purpose": "Read-only market data connector with daily user credits.",
            "daily_default_credits": _get_entitlements().default_daily_credits,
            "tool_costs": TOOL_COSTS,
            "subscription_note": (
                "Higher daily allowances are managed by Blocksize account "
                "entitlements outside this MCP connector."
            ),
            "links": {
                "homepage": PUBLIC_BASE_URL,
                "subscription": "https://blocksize.info/crypto-market-data/#pricing",
            },
        },
        indent=2,
    )
