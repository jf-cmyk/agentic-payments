"""Free-tier offer text, licence, and attribution generated from configuration.

Every user-facing statement about the free allowance must come from here so the
number, period, licence, and attribution can change in one place
(``settings.free_tier`` in ``src/config.py``). See
docs/gtm/free_tier_dev_checkpoint_2026-09-23.md sections 3 and 4.6.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from src.config import settings

logger = logging.getLogger(__name__)

FREE_TIER_PERIOD = "calendar_month_utc"
FREE_TIER_RESET_RULE = "Resets on the first day of each calendar month (UTC)."
ATTRIBUTION_TEXT = "Data by Blocksize"
ATTRIBUTION_URL = (
    "https://blocksize.info/?utm_source=mcp&utm_medium=attribution&utm_campaign=free-tier"
)
LICENCE_ID = "blocksize-free-tier-evaluation-v1"
LICENCE_SUMMARY = (
    "Evaluation and prototyping licence: internal use only; no resale, "
    "redistribution, or public redisplay without attribution; production "
    "commercial use requires a Blocksize subscription."
)
LICENCE_TERMS = (
    "internal_use_only",
    "no_resale",
    "no_redistribution",
    "attribution_required",
    "production_use_requires_subscription",
)
ELIGIBILITY = "authenticated_connector_only"
ELIGIBILITY_SENTENCE = (
    "Available only to eligible authenticated connector users with a verified email."
)
# Legacy allowance switches from the 50-credit era. They are no longer read;
# the free tier is configured exclusively through FREE_TIER_* variables.
LEGACY_ALLOWANCE_ENV_VARS = (
    "STARTER_CREDIT_ALLOWANCE",
    "ANTHROPIC_DAILY_CREDITS",
    "CURSOR_DAILY_CREDITS",
    "OPENAI_DAILY_CREDITS",
)


def allowance_credits() -> int:
    """Return the configured monthly free-tier allowance in credits."""
    return int(settings.free_tier.monthly_credits)


def allowance_label(credits: int | None = None) -> str:
    """Return the allowance formatted for copy, e.g. ``15,000``."""
    value = allowance_credits() if credits is None else int(credits)
    return f"{value:,}"


def enabled() -> bool:
    return bool(settings.free_tier.enabled)


def positioning() -> str:
    """Return the one-line offer used across connectors, catalogs, and docs."""
    return f"Start with {allowance_label()} free live-data credits every month"


def allowance_sentence() -> str:
    """Return the full allowance sentence used in longer copy."""
    return (
        f"Eligible authenticated connector users receive {allowance_label()} free "
        "live-data credits every calendar month (UTC), then continue with signed "
        "x402 payment or a Blocksize subscription plan."
    )


def upgrade_path_text() -> str:
    """Return the upgrade sentence shown once the pool is exhausted or capped."""
    return (
        "When the monthly free allowance is exhausted or rate limited, use signed "
        "x402 for direct public HTTP or contact Blocksize sales about an "
        "authenticated account plan."
    )


def attribution_payload() -> dict[str, Any]:
    """Return the attribution block that every free-tier response must carry."""
    return {
        "required": True,
        "text": ATTRIBUTION_TEXT,
        "url": ATTRIBUTION_URL,
    }


def licence_payload() -> dict[str, Any]:
    return {
        "id": LICENCE_ID,
        "summary": LICENCE_SUMMARY,
        "terms": list(LICENCE_TERMS),
    }


def guard_summary() -> dict[str, Any]:
    """Return the non-secret guard configuration for operators and readers."""
    free_tier = settings.free_tier
    return {
        "per_minute_credits": int(free_tier.per_minute_credits),
        "daily_soft_cap_credits": int(free_tier.daily_soft_cap_credits),
        "max_batch_items": int(free_tier.max_batch_items),
        "global_daily_cap_credits": int(free_tier.global_daily_cap_credits),
        "allowed_services": sorted(free_tier.allowed_service_set),
    }


def offer_payload() -> dict[str, Any]:
    """Return the canonical machine-readable free-tier offer."""
    return {
        "positioning": positioning(),
        "allowance_credits": allowance_credits(),
        "period": FREE_TIER_PERIOD,
        "resets": FREE_TIER_RESET_RULE,
        "recurring": True,
        "enabled": enabled(),
        "eligibility": ELIGIBILITY,
        "eligibility_note": ELIGIBILITY_SENTENCE,
        "licence": licence_payload(),
        "attribution": attribution_payload(),
        "guards": guard_summary(),
        "upgrade_path": upgrade_path_text(),
    }


def legacy_allowance_env_warnings() -> list[str]:
    """Return warnings for legacy allowance variables that are set but ignored."""
    warnings: list[str] = []
    for name in LEGACY_ALLOWANCE_ENV_VARS:
        if os.environ.get(name, "").strip():
            warnings.append(
                f"{name} is set but ignored; the free allowance is "
                f"FREE_TIER_MONTHLY_CREDITS={allowance_credits()}"
            )
    return warnings


def log_legacy_allowance_env_warnings() -> None:
    for message in legacy_allowance_env_warnings():
        logger.warning("free_tier config: %s", message)
