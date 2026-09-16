"""Sales-assisted commercial plans and deterministic recommendations."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode


ACCOUNT_PLANS: tuple[dict[str, Any], ...] = (
    {
        "id": "developer",
        "name": "Developer",
        "indicative_monthly_price_usd": 49,
        "included_live_data_credits": 10_000,
        "recommended_for": "Individual builders validating a recurring agent workflow.",
        "features": [
            "Authenticated account access",
            "Spend caps and usage visibility",
            "Signed provenance receipts",
            "Email support",
        ],
    },
    {
        "id": "production",
        "name": "Production",
        "indicative_monthly_price_usd": 249,
        "included_live_data_credits": 100_000,
        "recommended_for": "Teams operating recurring production agents.",
        "features": [
            "Everything in Developer",
            "Shared team access",
            "Higher concurrency and spend controls",
            "Priority support and integration review",
        ],
    },
    {
        "id": "institutional",
        "name": "Institutional",
        "indicative_monthly_price_usd": 999,
        "included_live_data_credits": 500_000,
        "recommended_for": "Institutions requiring governance, support, and negotiated service terms.",
        "features": [
            "Everything in Production",
            "Named service owner",
            "Custom retention and redistribution review",
            "SLA and volume terms subject to agreement",
        ],
    },
)


def account_plan_catalog() -> dict[str, Any]:
    """Return truthful, non-activating sales-assisted plan metadata."""

    return {
        "status": "ok",
        "currency": "USD",
        "sales_model": "sales_assisted",
        "self_serve_purchase_available": False,
        "commercial_terms": (
            "Prices and included credits are indicative starting points. "
            "Availability, data rights, service levels, and final terms are set in a signed order form."
        ),
        "plans": [dict(plan) for plan in ACCOUNT_PLANS],
    }


def recommend_account_plan(
    *,
    expected_monthly_live_calls: int,
    team_seats: int = 1,
    needs_sla: bool = False,
    recurring_days_per_month: int = 0,
) -> dict[str, Any]:
    """Choose a plan deterministically from non-sensitive operating inputs."""

    if not 0 <= expected_monthly_live_calls <= 100_000_000:
        raise ValueError("expected_monthly_live_calls must be between 0 and 100000000")
    if not 1 <= team_seats <= 10_000:
        raise ValueError("team_seats must be between 1 and 10000")
    if not 0 <= recurring_days_per_month <= 31:
        raise ValueError("recurring_days_per_month must be between 0 and 31")

    if needs_sla or team_seats > 20 or expected_monthly_live_calls > 100_000:
        selected_id = "institutional"
        reason = "SLA, team, or volume requirements need negotiated institutional terms."
    elif team_seats > 1 or expected_monthly_live_calls > 10_000 or recurring_days_per_month >= 8:
        selected_id = "production"
        reason = "Recurring production usage or shared access fits the Production plan."
    else:
        selected_id = "developer"
        reason = "The expected usage fits a developer-scale recurring workflow."

    plan = next(plan for plan in ACCOUNT_PLANS if plan["id"] == selected_id)
    return {
        "recommended_plan": dict(plan),
        "reason": reason,
        "inputs": {
            "expected_monthly_live_calls": expected_monthly_live_calls,
            "team_seats": team_seats,
            "needs_sla": bool(needs_sla),
            "recurring_days_per_month": recurring_days_per_month,
        },
        "next_step": "Review the recommendation with Blocksize and finalize a signed order form.",
    }


def tracked_plan_contact_path(plan_id: str, *, source: str) -> str:
    """Build a first-party tracked contact path without collecting PII."""

    if plan_id not in {str(plan["id"]) for plan in ACCOUNT_PLANS}:
        raise ValueError("Unknown account plan")
    query = urlencode(
        {
            "utm_source": source,
            "utm_medium": "product",
            "utm_campaign": "account-plan-conversion",
            "utm_content": plan_id,
        }
    )
    return f"/go/contact?{query}"


def upgrade_recommendation(credits_remaining: int) -> dict[str, Any] | None:
    """Return a plan CTA when an authenticated starter allowance is low."""

    if credits_remaining > 10:
        return None
    return {
        "status": "exhausted" if credits_remaining <= 0 else "low_balance",
        "recommended_plan_id": "developer",
        "account_plans_path": "/v1/account-plans",
        "recommendation_path": "/v1/account-plans/recommend",
        "contact_path": tracked_plan_contact_path(
            "developer",
            source="authenticated-connector",
        ),
    }
