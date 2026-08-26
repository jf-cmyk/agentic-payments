"""Shared read-only authenticated market-data MCP surface."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Annotated, Awaitable, Callable, Literal, TypeVar

from fastmcp import FastMCP
from pydantic import Field

from src.blocksize_client import BlocksizeAPIError, BlocksizeClient
from src.connector_auth import ConnectorIdentity
from src.entitlement_manager import CreditStatus, EntitlementManager
from src.mcp_server import (
    DISCOVERY_INSTRUMENT_DEFAULT_LIMIT,
    InstrumentPageLimit,
    InstrumentPageOffset,
    READ_ONLY_TOOL_ANNOTATIONS,
    build_catalog_snapshot_metadata,
)
from src.models import (
    BidAskResponse,
    ErrorResponse,
    InstrumentListResponse,
    PairSearchResponse,
    VWAPResponse,
)
from src.observability import (
    fingerprint,
    normalize_symbol_opportunity,
    record_usage_event,
    record_usage_event_once,
)
from src.public_metadata import APP_VERSION, MAIN_WEBSITE_PRICING_URL, PUBLIC_BASE_URL
from src.transaction_bridge import economic_writes_locked

logger = logging.getLogger(__name__)

InstrumentSearchQuery = Annotated[
    str,
    Field(
        description="Symbol, ticker, asset, or pair to search for, such as BTC, AAPL, EURUSD, or XAUUSD.",
        min_length=1,
        max_length=80,
    ),
]
AssetClassFilter = Annotated[
    Literal["all", "crypto", "equity", "equities", "fx", "metal"],
    Field(
        description="Optional asset class filter. Use equity/equities for supported stock tickers."
    ),
]
InstrumentService = Annotated[
    Literal["vwap", "bidask", "fx", "metal"],
    Field(description="Blocksize service namespace to list. Use bidask for supported equities."),
]
PairValue = Annotated[
    str,
    Field(description="Trading pair or ticker, such as BTC-USD, AAPL, EURUSD, or XAUUSD."),
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


@dataclass(frozen=True)
class AuthenticatedMCPBundle:
    mcp: FastMCP
    search_pairs: Callable[..., Awaitable[str]]
    list_instruments: Callable[..., Awaitable[str]]
    get_credit_balance: Callable[..., Awaitable[str]]
    get_vwap: Callable[..., Awaitable[str]]
    get_bid_ask: Callable[..., Awaitable[str]]
    get_fx_rate: Callable[..., Awaitable[str]]
    get_metal_price: Callable[..., Awaitable[str]]
    info: Callable[..., Awaitable[str]]


ClientGetter = Callable[[], Awaitable[BlocksizeClient]]
EntitlementGetter = Callable[[], EntitlementManager]
IdentityResolver = Callable[[], ConnectorIdentity | None]


def create_authenticated_market_data_mcp(
    *,
    mcp_name: str,
    instructions: str,
    auth_provider,
    resolve_identity: IdentityResolver,
    get_client: ClientGetter,
    get_entitlements: EntitlementGetter,
    client_label: str,
    resource_uri: str,
) -> AuthenticatedMCPBundle:
    """Create a read-only authenticated market-data MCP server."""
    mcp = FastMCP(
        mcp_name,
        version=APP_VERSION,
        instructions=instructions,
        auth=auth_provider,
    )
    observability_surface = f"{client_label.lower()}_mcp"

    def error_payload(error_code: str, message: str, details: str | None = None) -> str:
        return json.dumps(
            ErrorResponse(
                error_code=error_code,
                message=message,
                details=details,
            ).model_dump()
        )

    def credit_payload(status: CreditStatus) -> dict[str, object]:
        """Return account-scoped credit state without exposing direct identifiers."""
        return {
            "date": status.date,
            "daily_limit": status.daily_limit,
            "credits_spent": status.credits_spent,
            "credits_remaining": status.credits_remaining,
            "status": status.status,
        }

    def telemetry_credit_payload(status: CreditStatus) -> dict[str, object]:
        return {
            "date": status.date,
            "daily_limit": status.daily_limit,
            "credits_spent": status.credits_spent,
            "credits_remaining": status.credits_remaining,
            "status": status.status,
        }

    def telemetry_identity_payload(
        identity: ConnectorIdentity | None,
    ) -> dict[str, object]:
        """Return privacy-safe identity attribution and an explicit test marker."""
        if identity is None:
            return {}
        payload: dict[str, object] = {
            "identity_hash": fingerprint(identity.ledger_subject),
            "identity_type": "user",
            "identity_trust": (
                "verified_oauth"
                if identity.source == "oauth"
                else "verified_beta"
                if identity.source == "beta-token"
                else "synthetic_test"
            ),
        }
        if identity.source not in {"oauth", "beta-token"}:
            payload["synthetic"] = True
        return payload

    def normalise_symbol(value: str, field_name: str = "symbol") -> str:
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

    async def with_credits(
        tool_name: str,
        subject: str,
        call: Callable[[], Awaitable[T]],
        render: Callable[[T], str],
    ) -> str:
        if economic_writes_locked():
            return error_payload(
                "ECONOMIC_WRITES_LOCKED",
                (
                    "Live-data credit consumption is temporarily disabled during "
                    "a transaction-continuity maintenance release. No credit was used."
                ),
            )
        attempt_id = uuid.uuid4().hex
        identity = resolve_identity()
        identity_metadata = telemetry_identity_payload(identity)
        record_usage_event(
            "mcp_tool_call",
            surface=observability_surface,
            tool_name=tool_name,
            subject=subject,
            metadata={
                "attempt_id": attempt_id,
                "credit_cost": TOOL_COSTS[tool_name],
                **identity_metadata,
            },
        )
        if identity is None:
            record_usage_event(
                "mcp_auth_failed",
                surface=observability_surface,
                tool_name=tool_name,
                subject=subject,
                reason="missing_identity",
                metadata={"attempt_id": attempt_id},
            )
            return error_payload(
                "AUTH_REQUIRED",
                "Connect with an authenticated Blocksize account to use live market data.",
            )

        cost = TOOL_COSTS[tool_name]
        charge_id = uuid.uuid4().hex
        entitlements = get_entitlements()
        try:
            canonical_user_id = entitlements.bind_identity(
                identity.ledger_subject,
                identity.legacy_ledger_subject,
                email=identity.email,
            )
            ok, status = entitlements.spend(
                canonical_user_id,
                cost,
                email=identity.email,
                tool_name=tool_name,
                subject=subject,
                charge_id=charge_id,
            )
        except sqlite3.Error:
            logger.error("Connector credit ledger is unavailable for %s", tool_name)
            record_usage_event(
                "mcp_credit_drawdown_failed",
                surface=observability_surface,
                tool_name=tool_name,
                subject=subject,
                reason="credit_ledger_unavailable",
                metadata={
                    "attempt_id": attempt_id,
                    "charge_id": charge_id,
                    **identity_metadata,
                },
            )
            return error_payload(
                "CREDIT_LEDGER_UNAVAILABLE",
                "Blocksize could not safely reserve a live-data credit. No data was returned.",
            )
        if not ok:
            record_usage_event(
                "mcp_credit_drawdown_failed",
                surface=observability_surface,
                tool_name=tool_name,
                subject=subject,
                reason="daily_credit_limit_reached",
                metadata={
                    "attempt_id": attempt_id,
                    "charge_id": charge_id,
                    **telemetry_credit_payload(status),
                    **identity_metadata,
                },
            )
            return error_payload(
                "DAILY_CREDIT_LIMIT_REACHED",
                (
                    "Blocksize starter live-data credits are exhausted for this "
                    "allowance window. Upgrade outside the connector with x402 "
                    "payment or an authenticated account plan to continue production usage."
                ),
                json.dumps(credit_payload(status)),
            )
        record_usage_event(
            "mcp_credit_drawdown_success",
            surface=observability_surface,
            tool_name=tool_name,
            subject=subject,
            metadata={
                "attempt_id": attempt_id,
                "charge_id": charge_id,
                "charge_state": "pending",
                **telemetry_credit_payload(status),
                **identity_metadata,
            },
        )

        def refund_pending_charge() -> dict[str, object]:
            try:
                refunded = entitlements.refund(
                    canonical_user_id,
                    cost,
                    tool_name=tool_name,
                    subject=subject,
                    charge_id=charge_id,
                )
            except sqlite3.Error:
                logger.error("Connector credit refund is pending recovery for %s", tool_name)
                return {
                    "refund_status": "pending_recovery",
                    "credits_remaining_after_refund": status.credits_remaining,
                }
            return {
                "refund_status": "refunded",
                "credits_remaining_after_refund": refunded.credits_remaining,
            }

        try:
            result = await call()
            rendered = render(result)
        except asyncio.CancelledError:
            refund_metadata = refund_pending_charge()
            record_usage_event(
                "mcp_tool_error",
                surface=observability_surface,
                tool_name=tool_name,
                subject=subject,
                reason="request_cancelled",
                metadata={
                    "attempt_id": attempt_id,
                    "charge_id": charge_id,
                    **refund_metadata,
                    **identity_metadata,
                },
            )
            raise
        except BlocksizeAPIError as e:
            refund_metadata = refund_pending_charge()
            record_usage_event(
                "mcp_tool_error",
                surface=observability_surface,
                tool_name=tool_name,
                subject=subject,
                reason="blocksize_api_error",
                metadata={
                    "attempt_id": attempt_id,
                    "charge_id": charge_id,
                    **refund_metadata,
                    **identity_metadata,
                },
            )
            return error_payload(
                "BLOCKSIZE_API_ERROR",
                f"Failed to retrieve data for '{subject}'",
                str(e),
            )
        except Exception as e:
            refund_metadata = refund_pending_charge()
            logger.error(
                "Unexpected %s in %s(%s)",
                type(e).__name__,
                tool_name,
                subject,
            )
            record_usage_event(
                "mcp_tool_error",
                surface=observability_surface,
                tool_name=tool_name,
                subject=subject,
                reason="internal_error",
                metadata={
                    "attempt_id": attempt_id,
                    "charge_id": charge_id,
                    **refund_metadata,
                    **identity_metadata,
                },
            )
            return error_payload(
                "INTERNAL_ERROR",
                f"Error retrieving data for '{subject}'",
            )

        try:
            current = entitlements.finalize_delivery(
                canonical_user_id,
                cost,
                charge_id=charge_id,
            )
        except sqlite3.Error:
            logger.error("Connector credit delivery finalization failed for %s", tool_name)
            current = None
        if current is None:
            record_usage_event(
                "mcp_tool_error",
                surface=observability_surface,
                tool_name=tool_name,
                subject=subject,
                reason="credit_finalization_failed",
                metadata={
                    "attempt_id": attempt_id,
                    "charge_id": charge_id,
                    "charge_state": "pending_recovery",
                    **identity_metadata,
                },
            )
            return error_payload(
                "CREDIT_FINALIZATION_FAILED",
                "Blocksize could not safely finalize delivery. No live data was returned.",
            )
        record_usage_event(
            "mcp_data_delivered",
            surface=observability_surface,
            tool_name=tool_name,
            subject=subject,
            metadata={
                "attempt_id": attempt_id,
                "charge_id": charge_id,
                "charge_state": "delivered",
                **telemetry_credit_payload(current),
                **identity_metadata,
                "payment_mode": "starter_credit",
            },
        )
        record_usage_event_once(
            "first_live_price_delivered",
            fingerprint(f"user:{identity.ledger_subject}"),
            surface=observability_surface,
            tool_name=tool_name,
            subject=subject,
            metadata={
                "attempt_id": attempt_id,
                "charge_id": charge_id,
                **identity_metadata,
                "payment_mode": "starter_credit",
            },
        )
        return (
            f"{rendered}\n\n"
            f"Starter credits remaining: {current.credits_remaining}/{current.daily_limit} "
            f"(Credits remaining today: {current.credits_remaining}/{current.daily_limit})"
        )

    @mcp.tool(
        name="search_pairs",
        title="Instrument Search",
        description=(
            "Search supported Blocksize crypto, equity/stock ticker, FX, and metal "
            "instruments by symbol or asset name. This returns metadata only, not live prices."
        ),
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    async def search_pairs(
        query: InstrumentSearchQuery,
        asset_class: AssetClassFilter = "all",
    ) -> str:
        record_usage_event(
            "mcp_tool_call",
            surface=observability_surface,
            tool_name="search_pairs",
            subject=query,
            asset_class=asset_class,
        )
        try:
            client = await get_client()
            pairs = await client.search_pairs(query, asset_class)
            response = PairSearchResponse(
                query=query,
                total_matches=len(pairs),
                pairs=pairs,
                meta={
                    **build_catalog_snapshot_metadata(
                        source="Blocksize instrument search result set",
                        records=list(pairs),
                        grain="instrument_search_match",
                        snapshot_scope="returned_result_set_max_50",
                    ),
                    "result_limit": 50,
                    "total_coverage": (
                        "Enabled symbols across crypto, equities, FX, and metals"
                    ),
                },
            )
            if not pairs:
                if (opportunity := normalize_symbol_opportunity(query)) is not None:
                    record_usage_event(
                        "unsupported_symbol_request",
                        surface=observability_surface,
                        tool_name="search_pairs",
                        subject=opportunity,
                        asset_class=asset_class,
                        metadata={"result_count": 0},
                    )
                return (
                    f"No instruments found matching '{query}' (class: {asset_class})."
                    f"\n\n<details>\n"
                    f"{json.dumps(response.model_dump(), default=str, indent=2)}"
                    "\n</details>"
                )
            pair_list = ", ".join(f"{p.pair} ({p.tier})" for p in pairs[:10])
            summary = f"Found {len(pairs)} instruments matching '{query}': {pair_list}" + (
                f" ... and {len(pairs) - 10} more" if len(pairs) > 10 else ""
            )
            return (
                f"{summary}\n\n<details>\n"
                f"{json.dumps(response.model_dump(), default=str, indent=2)}\n</details>"
            )
        except Exception as e:
            logger.error("Error in %s search_pairs(%s): %s", client_label, query, e, exc_info=True)
            return error_payload("INTERNAL_ERROR", f"Error searching for '{query}'", str(e))

    @mcp.tool(
        name="list_instruments",
        title="Instrument List",
        description=(
            "List supported instruments for one Blocksize service. Use bidask for "
            "shared bid/ask coverage, including supported equity tickers. This "
            "returns metadata only, not live prices."
        ),
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    async def list_instruments(
        service: InstrumentService = "vwap",
        limit: InstrumentPageLimit = DISCOVERY_INSTRUMENT_DEFAULT_LIMIT,
        offset: InstrumentPageOffset = 0,
    ) -> str:
        record_usage_event(
            "mcp_tool_call",
            surface=observability_surface,
            tool_name="list_instruments",
            subject=service,
        )
        try:
            client = await get_client()
            if service == "vwap":
                instruments = await client.list_vwap_instruments()
            elif service == "bidask":
                instruments = await client.list_bidask_instruments()
            elif service == "fx":
                instruments = await client.list_fx_instruments()
            else:
                instruments = await client.list_metal_instruments()

            instruments = sorted(str(instrument) for instrument in instruments)
            total = len(instruments)
            page = instruments[offset : offset + limit]
            next_offset = offset + len(page)
            has_more = next_offset < total
            response = InstrumentListResponse(
                service=service,
                total_instruments=total,
                returned_instruments=len(page),
                offset=offset,
                limit=limit,
                has_more=has_more,
                next_offset=next_offset if has_more else None,
                instruments=page,
                meta={
                    **build_catalog_snapshot_metadata(
                        source=f"Blocksize {service} instrument catalog",
                        records=instruments,
                        grain="instrument",
                        snapshot_scope="full_upstream_catalog",
                    ),
                    "ordering": "lexicographic_ascending",
                },
            )
            sample = ", ".join(page[:10])
            summary = (
                f"Returned {len(page)} of {total} instruments for {service} "
                f"at offset {offset}: {sample}"
                + (" ... use next_offset for the next page" if has_more else "")
            )
            return (
                f"{summary}\n\n<details>\n"
                f"{json.dumps(response.model_dump(), default=str, indent=2)}\n</details>"
            )
        except Exception as e:
            logger.error(
                "Error in %s list_instruments(%s): %s",
                client_label,
                service,
                e,
                exc_info=True,
            )
            return error_payload(
                "INTERNAL_ERROR",
                f"Error listing instruments for '{service}'",
                str(e),
            )

    @mcp.tool(
        name="get_credit_balance",
        title="Credit Balance",
        description="Show the authenticated user's remaining Blocksize starter live-data credits.",
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    async def get_credit_balance() -> str:
        if economic_writes_locked():
            return error_payload(
                "ECONOMIC_WRITES_LOCKED",
                (
                    "Credit-ledger access is temporarily disabled during a "
                    "transaction-continuity maintenance release."
                ),
            )
        record_usage_event(
            "mcp_tool_call",
            surface=observability_surface,
            tool_name="get_credit_balance",
        )
        identity = resolve_identity()
        if identity is None:
            record_usage_event(
                "mcp_auth_failed",
                surface=observability_surface,
                tool_name="get_credit_balance",
                reason="missing_identity",
            )
            return error_payload(
                "AUTH_REQUIRED",
                "Connect with an authenticated Blocksize account to view starter credits.",
            )
        try:
            entitlements = get_entitlements()
            canonical_user_id = entitlements.bind_identity(
                identity.ledger_subject,
                identity.legacy_ledger_subject,
                email=identity.email,
            )
            status = entitlements.status(canonical_user_id, identity.email)
        except sqlite3.Error:
            logger.error("Connector credit ledger is unavailable for get_credit_balance")
            return error_payload(
                "CREDIT_LEDGER_UNAVAILABLE",
                "Blocksize could not safely read the live-data credit balance.",
            )
        record_usage_event(
            "mcp_credit_balance_viewed",
            surface=observability_surface,
            tool_name="get_credit_balance",
            metadata=telemetry_credit_payload(status),
        )
        return json.dumps({"status": "ok", "credits": credit_payload(status)}, indent=2)

    @mcp.tool(
        name="get_vwap",
        title="Crypto VWAP Snapshot",
        description=(
            "Get the latest institutional crypto VWAP for one trading pair. "
            "This read-only live data call uses daily Blocksize credits."
        ),
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    async def get_vwap(pair: PairValue) -> str:
        try:
            clean_pair = normalise_symbol(pair, "pair")
        except ValueError as e:
            return error_payload("INVALID_SYMBOL", str(e))

        async def call():
            return await (await get_client()).get_vwap_latest(clean_pair)

        def render(data) -> str:
            response = VWAPResponse(data=data)
            return (
                f"{data.to_decision_summary()}\n\n<details>\n"
                f"{json.dumps(response.model_dump(), default=str, indent=2)}\n</details>"
            )

        return await with_credits("get_vwap", clean_pair, call, render)

    @mcp.tool(
        name="get_bid_ask",
        title="Bid Ask Snapshot",
        description=(
            "Get the latest bid, ask, and spread for one crypto pair or supported "
            "equity/stock ticker such as AAPL. This read-only live data call uses "
            "daily Blocksize credits."
        ),
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    async def get_bid_ask(pair: PairValue) -> str:
        try:
            clean_pair = normalise_symbol(pair, "pair")
        except ValueError as e:
            return error_payload("INVALID_SYMBOL", str(e))

        async def call():
            return await (await get_client()).get_bidask_snapshot(clean_pair)

        def render(data) -> str:
            response = BidAskResponse(data=data)
            return (
                f"{data.to_decision_summary()}\n\n<details>\n"
                f"{json.dumps(response.model_dump(), default=str, indent=2)}\n</details>"
            )

        return await with_credits("get_bid_ask", clean_pair, call, render)

    @mcp.tool(
        name="get_fx_rate",
        title="FX Snapshot",
        description=(
            "Get the latest bid, ask, and mid rate for one FX pair. "
            "This read-only live data call uses daily Blocksize credits."
        ),
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    async def get_fx_rate(pair: PairValue) -> str:
        try:
            clean_pair = normalise_symbol(pair, "pair")
        except ValueError as e:
            return error_payload("INVALID_SYMBOL", str(e))

        async def call():
            return await (await get_client()).get_fx_rate(clean_pair)

        def render(data) -> str:
            return (
                f"{data.to_decision_summary()}\n\n<details>\n"
                f"{json.dumps(data.model_dump(), default=str, indent=2)}\n</details>"
            )

        return await with_credits("get_fx_rate", clean_pair, call, render)

    @mcp.tool(
        name="get_metal_price",
        title="Metal Snapshot",
        description=(
            "Get the latest spot price for one supported metal ticker. "
            "This read-only live data call uses daily Blocksize credits."
        ),
        annotations=READ_ONLY_TOOL_ANNOTATIONS,
    )
    async def get_metal_price(ticker: PairValue) -> str:
        try:
            clean_ticker = normalise_symbol(ticker, "ticker")
        except ValueError as e:
            return error_payload("INVALID_SYMBOL", str(e))

        async def call():
            return await (await get_client()).get_metal_price(clean_ticker)

        def render(data) -> str:
            return (
                f"{data.to_decision_summary()}\n\n<details>\n"
                f"{json.dumps(data.model_dump(), default=str, indent=2)}\n</details>"
            )

        return await with_credits("get_metal_price", clean_ticker, call, render)

    @mcp.resource(resource_uri)
    async def info() -> str:
        return json.dumps(
            {
                "name": mcp_name,
                "version": APP_VERSION,
                "purpose": f"Read-only market data connector for {client_label}.",
                "equities": {
                    "positioning": "Supported equity tickers are first-class live-data symbols.",
                    "discovery": "Use search_pairs with asset_class=equity before paid calls.",
                    "live_tool": "get_bid_ask",
                    "example_symbols": ["AAPL", "MSFT", "NVDA"],
                },
                "starter_allowance": {
                    "positioning": "Start with 50 live data credits",
                    "allowance_credits": get_entitlements().default_daily_credits,
                    "not_free_forever": True,
                },
                "daily_default_credits": get_entitlements().default_daily_credits,
                "tool_costs": TOOL_COSTS,
                "subscription_note": (
                    "After starter credits are exhausted, production usage should "
                    "move to x402 payment, an authenticated account plan, or Blocksize "
                    "account entitlements outside this MCP connector."
                ),
                "links": {
                    "homepage": PUBLIC_BASE_URL,
                    "subscription": MAIN_WEBSITE_PRICING_URL,
                },
            },
            indent=2,
        )

    return AuthenticatedMCPBundle(
        mcp=mcp,
        search_pairs=search_pairs,
        list_instruments=list_instruments,
        get_credit_balance=get_credit_balance,
        get_vwap=get_vwap,
        get_bid_ask=get_bid_ask,
        get_fx_rate=get_fx_rate,
        get_metal_price=get_metal_price,
        info=info,
    )
