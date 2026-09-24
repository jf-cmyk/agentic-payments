"""Shared read-only authenticated market-data MCP surface."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated, Awaitable, Callable, Literal, TypeVar

from fastmcp import FastMCP
from pydantic import Field

from src.blocksize_client import BlocksizeAPIError, BlocksizeClient
from src.commercial_plans import upgrade_recommendation
from src.connector_auth import ConnectorIdentity
from src.entitlement_manager import CreditStatus, EntitlementManager
from src import free_tier
from src.free_tier_ledger import FreeTierDecision, FreeTierSnapshot, get_free_tier_ledger
from src.mcp_server import (
    DISCOVERY_INSTRUMENT_DEFAULT_LIMIT,
    DISCOVERY_SEARCH_DEFAULT_LIMIT,
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
from src.config import settings
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

# Denial reasons from the shared ledger mapped to stable client-facing codes.
# DAILY_CREDIT_LIMIT_REACHED is kept for the exhausted pool because published
# agent skills already handle it; the message explains the monthly semantics.
FREE_TIER_DENIAL_CODES = {
    "monthly_pool_exhausted": (
        "DAILY_CREDIT_LIMIT_REACHED",
        "Your free-tier live-data credits for this month are exhausted. No credit was used.",
    ),
    "rate_limited_minute": (
        "FREE_TIER_RATE_LIMITED",
        "Free-tier calls are rate limited per minute. Retry after the given delay. No credit was used.",
    ),
    "daily_soft_cap": (
        "FREE_TIER_DAILY_CAP_REACHED",
        "You reached today's free-tier soft cap. It resets at the next UTC day. No credit was used.",
    ),
    "global_daily_cap": (
        "FREE_TIER_AT_CAPACITY",
        "The free tier is at capacity for today. Signed x402 payment still works. No credit was used.",
    ),
    "suspended": (
        "FREE_TIER_SUSPENDED",
        "This free-tier grant is suspended pending review. No credit was used.",
    ),
    "free_tier_disabled": (
        "FREE_TIER_DISABLED",
        "The Blocksize free tier is temporarily unavailable. No credit was used.",
    ),
}
FREE_TIER_INELIGIBLE_MESSAGES = {
    "missing_verified_email": (
        "The free tier requires an account with a verified email address."
    ),
    "email_not_verified": "Verify the email on your Blocksize account to use the free tier.",
    "disposable_email_domain": (
        "Disposable email domains are not eligible for the free tier. Sign in with a "
        "permanent address."
    ),
}


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

    def credit_payload(
        status: CreditStatus,
        shared: FreeTierSnapshot | None = None,
        *,
        breadth: int | None = None,
    ) -> dict[str, object]:
        """Return account-scoped credit state without exposing direct identifiers.

        ``credits_remaining`` is the effective figure: the lower of this
        connector's ledger and the shared cross-connector pool.
        """
        remaining = status.credits_remaining
        if shared is not None:
            remaining = min(remaining, shared.credits_remaining)
        payload: dict[str, object] = {
            "date": status.date,
            "period": status.period,
            "daily_limit": status.daily_limit,
            "monthly_limit": status.monthly_limit,
            "credits_spent": status.credits_spent,
            "credits_spent_today": status.credits_spent_today,
            "credits_remaining": remaining,
            "resets_at": status.resets_at or free_tier.FREE_TIER_RESET_RULE,
            "status": status.status,
            "free_tier": {
                "enabled": free_tier.enabled(),
                "free_scope": sorted(settings.free_tier.allowed_service_set),
                "daily_soft_cap": int(settings.free_tier.daily_soft_cap_credits),
                "per_minute_credits": int(settings.free_tier.per_minute_credits),
                "licence": free_tier.LICENCE_ID,
            },
            "attribution": free_tier.attribution_payload(),
        }
        if shared is not None:
            payload["shared_pool"] = shared.as_payload()
        recommendation = upgrade_recommendation(
            remaining,
            monthly_limit=status.monthly_limit,
            distinct_instruments_30d=breadth,
            source=observability_surface,
        )
        if recommendation is not None:
            payload["upgrade_recommendation"] = recommendation
            payload["upgrade"] = {
                "plan_id": recommendation["recommended_plan_id"],
                "trigger": recommendation["trigger"],
                **recommendation["ctas"],
            }
        return payload

    def instrument_breadth(entitlements: EntitlementManager, user_id: str) -> int | None:
        try:
            return entitlements.distinct_subjects(user_id, days=30)
        except sqlite3.Error:
            return None

    def denial_payload(
        decision: FreeTierDecision,
        *,
        identity: ConnectorIdentity,
        tool_name: str,
        breadth: int | None = None,
    ) -> dict[str, object]:
        snapshot = decision.snapshot
        recommendation = upgrade_recommendation(
            snapshot.credits_remaining,
            monthly_limit=snapshot.monthly_limit,
            distinct_instruments_30d=breadth,
            source=observability_surface,
        ) or upgrade_recommendation(0, monthly_limit=None, source=observability_surface)
        payload: dict[str, object] = {
            "reason": decision.reason,
            **snapshot.as_payload(),
            "upgrade_path": free_tier.upgrade_path_text(),
            "upgrade": {
                "plan_id": recommendation["recommended_plan_id"],
                "trigger": decision.reason,
                **recommendation["ctas"],
            },
            "attribution": free_tier.attribution_payload(),
        }
        if decision.retry_after_seconds is not None:
            payload["retry_after_seconds"] = decision.retry_after_seconds
        record_cta_shown(
            identity,
            tool_name=tool_name,
            plan_id=recommendation["recommended_plan_id"],
            trigger=decision.reason,
        )
        return payload

    def record_cta_shown(
        identity: ConnectorIdentity,
        *,
        tool_name: str,
        plan_id: str,
        trigger: str,
    ) -> None:
        """Count one CTA impression per identity, trigger, and UTC day."""
        record_usage_event_once(
            "upgrade_cta_shown",
            fingerprint(f"cta:{identity.ledger_subject}:{free_tier.today_utc()}:{trigger}"),
            surface=observability_surface,
            tool_name=tool_name,
            metadata={
                **telemetry_identity_payload(identity),
                "plan_id": plan_id,
                "trigger": trigger,
                "primary_destination": "free-trial",
                "secondary_destination": "pricing",
            },
        )

    def free_tier_event(
        event: str,
        *,
        tool_name: str,
        subject: str,
        grant_key: str,
        identity: ConnectorIdentity,
        reason: str | None = None,
        **metadata: object,
    ) -> None:
        record_usage_event(
            event,
            surface=observability_surface,
            tool_name=tool_name,
            subject=subject,
            reason=reason,
            metadata={
                "grant_hash": fingerprint(grant_key),
                **telemetry_identity_payload(identity),
                **metadata,
            },
        )

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

    def record_upgrade_trigger(
        status: CreditStatus,
        identity: ConnectorIdentity,
        *,
        tool_name: str,
        remaining: int | None = None,
        breadth: int | None = None,
    ) -> dict[str, object] | None:
        recommendation = upgrade_recommendation(
            status.credits_remaining if remaining is None else remaining,
            monthly_limit=status.monthly_limit,
            distinct_instruments_30d=breadth,
            source=observability_surface,
        )
        if recommendation is None:
            return None
        record_usage_event_once(
            "account_plan_upgrade_triggered",
            fingerprint(
                f"{identity.ledger_subject}:{status.date}:{recommendation['status']}"
            ),
            surface=observability_surface,
            tool_name=tool_name,
            metadata={
                **telemetry_credit_payload(status),
                **telemetry_identity_payload(identity),
                "trigger_status": recommendation["status"],
                "trigger": recommendation["trigger"],
                "recommended_plan_id": recommendation["recommended_plan_id"],
            },
        )
        record_cta_shown(
            identity,
            tool_name=tool_name,
            plan_id=recommendation["recommended_plan_id"],
            trigger=recommendation["trigger"],
        )
        return recommendation

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

        def ledger_unavailable() -> str:
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

        entitlements = get_entitlements()
        ledger = get_free_tier_ledger()
        try:
            canonical_user_id = entitlements.bind_identity(
                identity.ledger_subject,
                identity.legacy_ledger_subject,
                email=identity.email,
            )
            override_allowance = entitlements.allowance_override(canonical_user_id)
        except sqlite3.Error:
            return ledger_unavailable()

        # --- Free-tier gate (kill switch, eligibility, data-rights scope) ----
        # Users with an explicit allowance override (subscribers, beta grants)
        # are metered only by their own entitlement row, never by the free tier.
        def gate_failure(code: str, message: str, reason: str, details: dict[str, object]) -> str:
            record_usage_event(
                "mcp_credit_drawdown_failed",
                surface=observability_surface,
                tool_name=tool_name,
                subject=subject,
                reason=reason,
                metadata={"attempt_id": attempt_id, **identity_metadata},
            )
            return error_payload(
                code,
                f"{message} {free_tier.upgrade_path_text()}",
                json.dumps({**details, "attribution": free_tier.attribution_payload()}),
            )

        grant_key: str | None = None
        decision: FreeTierDecision | None = None
        # Pin the shared-pool day so a refund after midnight UTC credits the
        # day the reservation was made.
        usage_date = datetime.now(UTC).date().isoformat()
        if override_allowance is None:
            if not free_tier.enabled():
                code, message = FREE_TIER_DENIAL_CODES["free_tier_disabled"]
                return gate_failure(
                    code,
                    message,
                    "free_tier_disabled",
                    {"reason": "free_tier_disabled", "upgrade_path": free_tier.upgrade_path_text()},
                )
            eligibility = free_tier.eligibility_for(identity)
            if not eligibility.eligible or eligibility.grant_key is None:
                return gate_failure(
                    "FREE_TIER_INELIGIBLE",
                    FREE_TIER_INELIGIBLE_MESSAGES.get(
                        eligibility.reason, "This account is not eligible for the free tier."
                    ),
                    f"ineligible_{eligibility.reason}",
                    {"reason": eligibility.reason, "upgrade_path": free_tier.upgrade_path_text()},
                )
            grant_key = eligibility.grant_key
            try:
                catalog_client = await get_client()
            except Exception:  # pragma: no cover - client construction failures surface later
                catalog_client = None
            service = await free_tier.service_for_tool_async(tool_name, subject, catalog_client)
            if not free_tier.service_in_free_scope(service):
                return gate_failure(
                    "FREE_TIER_SCOPE_EXCLUDED",
                    f"'{subject}' ({service}) is outside the current free-tier scope. No credit was used.",
                    "free_scope_excluded",
                    free_tier.scope_excluded_payload(service),
                )

        shared_reserved = False

        def release_shared() -> None:
            nonlocal shared_reserved
            if shared_reserved and grant_key is not None:
                ledger.release(grant_key, cost, usage_date=usage_date)
                shared_reserved = False

        try:
            if grant_key is not None:
                decision = ledger.reserve(
                    grant_key, cost, symbol=subject, usage_date=usage_date
                )
                shared_reserved = decision.allowed
                ledger.bind_subject(grant_key, identity.ledger_subject)
            if decision is not None and decision.grant_created:
                free_tier_event(
                    "free_tier_grant_created",
                    tool_name=tool_name,
                    subject=subject,
                    grant_key=grant_key,
                    identity=identity,
                    monthly_limit=decision.snapshot.monthly_limit,
                )
            if decision is not None and not decision.allowed:
                code, message = FREE_TIER_DENIAL_CODES.get(
                    decision.reason,
                    ("FREE_TIER_UNAVAILABLE", "The free tier declined this call. No credit was used."),
                )
                event = (
                    "free_tier_exhausted"
                    if decision.reason == "monthly_pool_exhausted"
                    else "free_tier_rate_limited"
                    if decision.reason in {"rate_limited_minute", "daily_soft_cap", "global_daily_cap"}
                    else "mcp_credit_drawdown_failed"
                )
                free_tier_event(
                    event,
                    tool_name=tool_name,
                    subject=subject,
                    grant_key=grant_key,
                    identity=identity,
                    reason=decision.reason,
                    attempt_id=attempt_id,
                    retry_after_seconds=decision.retry_after_seconds,
                    **decision.snapshot.as_payload(),
                )
                return error_payload(
                    code,
                    f"{message} {free_tier.upgrade_path_text()}",
                    json.dumps(
                        denial_payload(
                            decision,
                            identity=identity,
                            tool_name=tool_name,
                            breadth=instrument_breadth(entitlements, canonical_user_id),
                        )
                    ),
                )
            for level in decision.thresholds_crossed if decision is not None else ():
                free_tier_event(
                    "free_tier_threshold_crossed",
                    tool_name=tool_name,
                    subject=subject,
                    grant_key=grant_key,
                    identity=identity,
                    threshold_pct=level,
                    **decision.snapshot.as_payload(),
                )
                if level == 100:
                    free_tier_event(
                        "free_tier_exhausted",
                        tool_name=tool_name,
                        subject=subject,
                        grant_key=grant_key,
                        identity=identity,
                        reason="monthly_pool_consumed",
                        **decision.snapshot.as_payload(),
                    )
            ok, status = entitlements.spend(
                canonical_user_id,
                cost,
                email=identity.email,
                tool_name=tool_name,
                subject=subject,
                charge_id=charge_id,
            )
            if not ok:
                release_shared()
            if decision is not None and decision.abuse_flags:
                # The call that tripped the detector still completes (the shared
                # ledger already admitted it); every later call is blocked in
                # both ledgers until an operator reinstates the grant.
                free_tier_event(
                    "free_tier_abuse_flagged",
                    tool_name=tool_name,
                    subject=subject,
                    grant_key=grant_key,
                    identity=identity,
                    reason=",".join(decision.abuse_flags),
                    flags=list(decision.abuse_flags),
                    action="suspended",
                )
                entitlements.set_status(
                    canonical_user_id,
                    "suspended",
                    reason=",".join(decision.abuse_flags),
                    email=identity.email,
                )
        except sqlite3.Error:
            try:
                release_shared()
            except sqlite3.Error:
                logger.error("Shared free-tier release is pending recovery for %s", tool_name)
            return ledger_unavailable()
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
            code, message = FREE_TIER_DENIAL_CODES["monthly_pool_exhausted"]
            if status.status != "active":
                code, message = FREE_TIER_DENIAL_CODES["suspended"]
            else:
                record_cta_shown(
                    identity,
                    tool_name=tool_name,
                    plan_id="developer",
                    trigger="monthly_pool_exhausted",
                )
            return error_payload(
                code,
                f"{message} {free_tier.upgrade_path_text()}",
                json.dumps(
                    credit_payload(
                        status,
                        decision.snapshot if decision is not None else None,
                        breadth=instrument_breadth(entitlements, canonical_user_id),
                    )
                ),
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
                release_shared()
            except sqlite3.Error:
                logger.error("Shared free-tier release is pending recovery for %s", tool_name)
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
        remaining = (
            min(current.credits_remaining, decision.snapshot.credits_remaining)
            if decision is not None
            else current.credits_remaining
        )
        recommendation = record_upgrade_trigger(
            current,
            identity,
            tool_name=tool_name,
            remaining=remaining,
            breadth=instrument_breadth(entitlements, canonical_user_id),
        )
        upgrade_suffix = (
            f"\nUpgrade: {recommendation['ctas']['primary']['label']} "
            f"{PUBLIC_BASE_URL}{recommendation['ctas']['primary']['path']} | "
            f"{recommendation['ctas']['secondary']['label']} "
            f"{PUBLIC_BASE_URL}{recommendation['ctas']['secondary']['path']}"
            if recommendation is not None
            else ""
        )
        return (
            f"{rendered}\n\n"
            f"Starter credits remaining: {remaining}/{current.daily_limit} "
            f"(Credits remaining this month: {remaining}/{current.daily_limit}, "
            f"resets {current.resets_at})\n"
            f"{free_tier.ATTRIBUTION_TEXT}: {free_tier.ATTRIBUTION_URL}"
            f"{upgrade_suffix}"
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
        limit: InstrumentPageLimit = DISCOVERY_SEARCH_DEFAULT_LIMIT,
        offset: InstrumentPageOffset = 0,
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
            pairs, total = await client.search_pairs_page(
                query,
                asset_class,
                limit=limit,
                offset=offset,
            )
            next_offset = offset + len(pairs)
            has_more = next_offset < total
            response = PairSearchResponse(
                query=query,
                total_matches=total,
                returned_matches=len(pairs),
                offset=offset,
                limit=limit,
                has_more=has_more,
                next_offset=next_offset if has_more else None,
                pairs=pairs,
                meta={
                    **build_catalog_snapshot_metadata(
                        source="Blocksize instrument search result set",
                        records=list(pairs),
                        grain="instrument_search_match",
                        snapshot_scope="returned_search_page",
                    ),
                    "pagination": {
                        "limit": limit,
                        "offset": offset,
                        "returned": len(pairs),
                        "total": total,
                        "has_more": has_more,
                        "next_offset": next_offset if has_more else None,
                    },
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
            summary = f"Found {total} instruments matching '{query}'; returned {len(pairs)} from offset {offset}: {pair_list}" + (
                f" ... and {len(pairs) - 10} more on this page" if len(pairs) > 10 else ""
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
        description=(
            "Show the authenticated user's remaining Blocksize free-tier live-data "
            "credits for the current month."
        ),
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
            has_override = entitlements.allowance_override(canonical_user_id) is not None
            eligibility = free_tier.eligibility_for(identity)
            shared = (
                get_free_tier_ledger().status(eligibility.grant_key)
                if not has_override and eligibility.eligible and eligibility.grant_key
                else None
            )
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
            metadata={
                **telemetry_credit_payload(status),
                **telemetry_identity_payload(identity),
            },
        )
        breadth = instrument_breadth(entitlements, canonical_user_id)
        effective_remaining = (
            min(status.credits_remaining, shared.credits_remaining)
            if shared is not None
            else status.credits_remaining
        )
        record_upgrade_trigger(
            status,
            identity,
            tool_name="get_credit_balance",
            remaining=effective_remaining,
            breadth=breadth,
        )
        balance = credit_payload(status, shared, breadth=breadth)
        if not has_override and not eligibility.eligible:
            balance["free_tier_eligibility"] = eligibility.reason
        return json.dumps({"status": "ok", "credits": balance}, indent=2)

    @mcp.tool(
        name="get_vwap",
        title="Crypto VWAP Snapshot",
        description=(
            "Get the latest institutional crypto VWAP for one trading pair. "
            "This read-only live data call uses the monthly Blocksize free-tier credits."
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
            "catalog-confirmed equity symbol such as AAPLXUSD for Apple/USD. This read-only live data call uses "
            "the monthly Blocksize free-tier credits."
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
            "This read-only live data call uses the monthly Blocksize free-tier credits."
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
            "This read-only live data call uses the monthly Blocksize free-tier credits."
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
                    **free_tier.offer_payload(),
                    "allowance_credits": get_entitlements().default_daily_credits,
                },
                "tool_costs": TOOL_COSTS,
                "subscription_note": (
                    "After the monthly free allowance is exhausted, production usage "
                    "should move to x402 payment, an authenticated account plan, or "
                    "Blocksize account entitlements outside this MCP connector."
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
