"""Subscription plan ladder, upgrade triggers, and tracked conversion CTAs.

The ladder mirrors https://blocksize.info/crypto-market-data/pricing/ (EUR,
annual billing 15% off). Plan boundaries are feed counts, seats, and history,
not call counts, so recommendations are driven by the number of distinct
instruments an identity actually uses and by free-tier consumption.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import urlencode

CURRENCY = "EUR"
ANNUAL_DISCOUNT_PCT = 15
PRICING_URL = "https://blocksize.info/crypto-market-data/pricing/"

ACCOUNT_PLANS: tuple[dict[str, Any], ...] = (
    {
        "id": "developer",
        "name": "Developer",
        "indicative_monthly_price_eur": 49,
        "feeds": 5,
        "history": "7 days",
        "seats": 1,
        "recommended_for": "Individual builders running a small recurring agent workflow.",
        "features": [
            "Up to 5 live feeds",
            "7-day history",
            "1 seat",
            "Authenticated account access and usage visibility",
        ],
    },
    {
        "id": "startup",
        "name": "Start-Up",
        "indicative_monthly_price_eur": 299,
        "feeds": 50,
        "history": "1 month",
        "seats": 5,
        "recommended_for": "Teams operating recurring production agents across a watchlist.",
        "features": [
            "Up to 50 live feeds",
            "1-month history",
            "5 seats",
            "Everything in Developer",
        ],
    },
    {
        "id": "business",
        "name": "Business",
        "indicative_monthly_price_eur": 799,
        "feeds": 350,
        "history": "3 months",
        "seats": 10,
        "recommended_for": "Products and desks that need broad coverage, State Prices, and a support SLA.",
        "features": [
            "Up to 350 live feeds",
            "3-month history",
            "10 seats",
            "State Prices",
            "Slack support SLA",
        ],
    },
    {
        "id": "enterprise",
        "name": "Enterprise",
        "indicative_monthly_price_eur": None,
        "feeds": None,
        "history": "custom",
        "seats": None,
        "recommended_for": "Institutions requiring unlimited feeds, commodities, governance, and negotiated terms.",
        "features": [
            "Unlimited feeds",
            "Commodities",
            "Custom history and retention",
            "Contractual SLA and named service owner",
        ],
    },
)
PLAN_IDS = tuple(str(plan["id"]) for plan in ACCOUNT_PLANS)
# Ids used before the ladder was aligned with the website; kept so stored
# links and older clients keep resolving.
LEGACY_PLAN_ALIASES = {"production": "startup", "institutional": "business"}
# Consumption thresholds (percent of the monthly free pool) that trigger a CTA.
UPGRADE_THRESHOLDS_PCT = (50, 80, 95, 100)


def plan_by_id(plan_id: str) -> dict[str, Any]:
    resolved = LEGACY_PLAN_ALIASES.get(plan_id, plan_id)
    for plan in ACCOUNT_PLANS:
        if plan["id"] == resolved:
            return dict(plan)
    raise ValueError("Unknown account plan")


def account_plan_catalog() -> dict[str, Any]:
    """Return truthful, non-activating plan metadata aligned with the website."""

    return {
        "status": "ok",
        "currency": CURRENCY,
        "annual_discount_pct": ANNUAL_DISCOUNT_PCT,
        "pricing_url": PRICING_URL,
        "free_trial_path": "/go/free-trial",
        "pricing_path": "/go/pricing",
        "sales_model": "sales_assisted",
        "self_serve_purchase_available": False,
        "commercial_terms": (
            "Prices are the published monthly list prices in EUR; annual billing is "
            f"{ANNUAL_DISCOUNT_PCT}% off. Data rights, service levels, and final terms "
            "are set in a signed order form."
        ),
        "plans": [dict(plan) for plan in ACCOUNT_PLANS],
    }


def plan_for_instruments(distinct_instruments: int, *, seats: int = 1, needs_sla: bool = False) -> str:
    """Map usage breadth onto the plan whose feed limit covers it."""
    if needs_sla or distinct_instruments > 350 or seats > 10:
        return "enterprise"
    if distinct_instruments > 50 or seats > 5:
        return "business"
    if distinct_instruments > 5 or seats > 1:
        return "startup"
    return "developer"


def recommend_account_plan(
    *,
    expected_monthly_live_calls: int = 0,
    team_seats: int = 1,
    needs_sla: bool = False,
    recurring_days_per_month: int = 0,
    distinct_instruments: int | None = None,
) -> dict[str, Any]:
    """Choose a plan deterministically from non-sensitive operating inputs.

    Feed count (``distinct_instruments``) is the primary signal because it is
    the plan boundary. When it is unknown, call volume stands in: roughly one
    feed polled every five minutes is 8,640 calls a month.
    """

    if not 0 <= expected_monthly_live_calls <= 100_000_000:
        raise ValueError("expected_monthly_live_calls must be between 0 and 100000000")
    if not 1 <= team_seats <= 10_000:
        raise ValueError("team_seats must be between 1 and 10000")
    if not 0 <= recurring_days_per_month <= 31:
        raise ValueError("recurring_days_per_month must be between 0 and 31")
    if distinct_instruments is not None and not 0 <= distinct_instruments <= 1_000_000:
        raise ValueError("distinct_instruments must be between 0 and 1000000")

    if distinct_instruments is None:
        estimated_feeds = max(1, expected_monthly_live_calls // 8_640) if expected_monthly_live_calls else 0
        feeds_basis = "estimated_from_calls"
    else:
        estimated_feeds = int(distinct_instruments)
        feeds_basis = "distinct_instruments"

    selected_id = plan_for_instruments(estimated_feeds, seats=team_seats, needs_sla=needs_sla)
    if selected_id == "developer" and recurring_days_per_month >= 8 and estimated_feeds > 5:
        selected_id = "startup"
    reasons = {
        "enterprise": "SLA, seat, or feed requirements need negotiated enterprise terms.",
        "business": "More than 50 feeds or more than 5 seats fits the Business plan.",
        "startup": "More than 5 feeds or shared access fits the Start-Up plan.",
        "developer": "The expected usage fits within 5 feeds and one seat.",
    }
    plan = plan_by_id(selected_id)
    return {
        "recommended_plan": plan,
        "reason": reasons[selected_id],
        "inputs": {
            "expected_monthly_live_calls": expected_monthly_live_calls,
            "team_seats": team_seats,
            "needs_sla": bool(needs_sla),
            "recurring_days_per_month": recurring_days_per_month,
            "distinct_instruments": distinct_instruments,
            "feeds_basis": feeds_basis,
            "estimated_feeds": estimated_feeds,
        },
        "ctas": conversion_ctas(selected_id, source="plan-recommender", trigger="recommendation"),
        "next_step": (
            "Start the free trial to evaluate the plan, or contact Blocksize sales for "
            "Enterprise terms."
        ),
    }


def _tracked_path(destination: str, plan_id: str, *, source: str, trigger: str) -> str:
    query = urlencode(
        {
            "utm_source": source,
            "utm_medium": "product",
            "utm_campaign": "free-tier-upgrade",
            "utm_content": plan_id,
            "utm_term": trigger,
        }
    )
    return f"/go/{destination}?{query}"


def tracked_plan_contact_path(plan_id: str, *, source: str) -> str:
    """Build a first-party tracked contact path without collecting PII."""

    resolved = LEGACY_PLAN_ALIASES.get(plan_id, plan_id)
    if resolved not in PLAN_IDS:
        raise ValueError("Unknown account plan")
    query = urlencode(
        {
            "utm_source": source,
            "utm_medium": "product",
            "utm_campaign": "account-plan-conversion",
            "utm_content": resolved,
        }
    )
    return f"/go/contact?{query}"


def conversion_ctas(plan_id: str, *, source: str, trigger: str) -> dict[str, Any]:
    """Return the primary (trial), secondary (pricing), and sales CTAs."""
    resolved = LEGACY_PLAN_ALIASES.get(plan_id, plan_id)
    if resolved not in PLAN_IDS:
        raise ValueError("Unknown account plan")
    plan = plan_by_id(resolved)
    price = plan["indicative_monthly_price_eur"]
    ctas: dict[str, Any] = {
        "primary": {
            "label": "Start a free trial",
            "path": _tracked_path("free-trial", resolved, source=source, trigger=trigger),
        },
        "secondary": {
            "label": (
                f"See plans from {CURRENCY} {price}/month"
                if price is not None
                else "See plans"
            ),
            "path": _tracked_path("pricing", resolved, source=source, trigger=trigger),
        },
    }
    if resolved == "enterprise":
        ctas["sales"] = {
            "label": "Contact sales for Enterprise terms",
            "path": tracked_plan_contact_path(resolved, source=source),
        }
    return ctas


def upgrade_recommendation(
    credits_remaining: int,
    *,
    monthly_limit: int | None = None,
    distinct_instruments_30d: int | None = None,
    source: str = "authenticated-connector",
) -> dict[str, Any] | None:
    """Return a plan CTA when free-tier consumption or breadth warrants one.

    Triggers: 50, 80, 95, and 100 percent of the monthly pool consumed, or more
    distinct instruments in the trailing 30 days than the Developer plan
    covers. Without ``monthly_limit`` the legacy rule (10 credits or fewer)
    applies so older callers keep working.
    """

    remaining = max(0, int(credits_remaining))
    consumed_pct: float | None = None
    if monthly_limit is not None and monthly_limit > 0:
        consumed = monthly_limit - remaining
        consumed_pct = round(consumed * 100 / monthly_limit, 1)
        # Compare exactly (not on the rounded display value) so 49.99% is not 50%.
        crossed = [level for level in UPGRADE_THRESHOLDS_PCT if consumed * 100 >= level * monthly_limit]
        threshold = crossed[-1] if crossed else None
    else:
        threshold = 100 if remaining <= 0 else 50 if remaining <= 10 else None

    breadth_plan = (
        plan_for_instruments(int(distinct_instruments_30d))
        if distinct_instruments_30d is not None
        else "developer"
    )
    breadth_trigger = breadth_plan != "developer"
    if threshold is None and not breadth_trigger:
        return None

    if remaining <= 0 or threshold == 100:
        status = "exhausted"
    elif threshold == 95:
        status = "critical"
    elif threshold == 80:
        status = "high"
    elif threshold == 50:
        status = "half"
    else:
        status = "breadth"
    trigger = f"consumption_{threshold}" if threshold is not None else f"instruments_{breadth_plan}"
    plan_id = breadth_plan
    plan = plan_by_id(plan_id)
    return {
        "status": status,
        "trigger": trigger,
        "consumed_pct": consumed_pct,
        "distinct_instruments_30d": distinct_instruments_30d,
        "recommended_plan_id": plan_id,
        "recommended_plan": {
            "id": plan["id"],
            "name": plan["name"],
            "indicative_monthly_price_eur": plan["indicative_monthly_price_eur"],
            "feeds": plan["feeds"],
        },
        "ctas": conversion_ctas(plan_id, source=source, trigger=trigger),
        "account_plans_path": "/v1/account-plans",
        "recommendation_path": "/v1/account-plans/recommend",
        "contact_path": tracked_plan_contact_path(plan_id, source=source),
    }
