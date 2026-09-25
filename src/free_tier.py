"""Free-tier offer text, licence, and attribution generated from configuration.

Every user-facing statement about the free allowance must come from here so the
number, period, licence, and attribution can change in one place
(``settings.free_tier`` in ``src/config.py``). See
docs/gtm/free_tier_dev_checkpoint_2026-09-23.md sections 3 and 4.6.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from functools import lru_cache
import hashlib
import logging
import os
from pathlib import Path
import re
from typing import Any

from src.commercial_plans import conversion_ctas
from src.config import KNOWN_CRYPTO_X_BASES, TOP_250_CRYPTO, settings

logger = logging.getLogger(__name__)

FREE_TIER_PERIOD = "calendar_month_utc"
FREE_TIER_RESET_RULE = "Resets on the first day of each calendar month (UTC)."
ATTRIBUTION_TEXT = "Data by Blocksize"
ATTRIBUTION_URL = (
    "https://blocksize.info/?utm_source=mcp&utm_medium=attribution&utm_campaign=free-tier"
)
LICENCE_ID = "blocksize-free-tier-evaluation-v1"
# The free tier is governed by the published Blocksize data terms (the Crypto
# Data License Agreement); /terms on this server redirects there.
TERMS_URL = "https://blocksize.info/terms-conditions-data/"
LICENCE_SUMMARY = (
    "Evaluation and prototyping licence under the Blocksize data terms: internal "
    "use only; no resale, redistribution, or public redisplay without attribution; "
    "production commercial use requires a Blocksize subscription."
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
        "When the monthly free allowance is exhausted or rate limited, start a free "
        "trial at /go/free-trial, compare plans at /go/pricing, or use signed x402 "
        "for direct public HTTP. For Enterprise terms, contact Blocksize sales about "
        "an authenticated account plan."
    )


def upgrade_fields(*, source: str, trigger: str = "surface", plan_id: str = "developer") -> dict[str, Any]:
    """Return the standard ``upgrade`` block for catalogs, 402s, and denials."""
    return {
        "upgrade_path": upgrade_path_text(),
        "upgrade": {
            "plan_id": plan_id,
            "trigger": trigger,
            **conversion_ctas(plan_id, source=source, trigger=trigger),
        },
    }


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
        "terms_url": TERMS_URL,
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


def operator_status() -> dict[str, Any]:
    """Non-secret deploy verification block for /health (checkpoint section 6.A)."""
    ledger_path = str(settings.free_tier.ledger_db_path)
    return {
        "enabled": enabled(),
        "monthly_credits": allowance_credits(),
        "period": FREE_TIER_PERIOD,
        "allowed_services": sorted(settings.free_tier.allowed_service_set),
        "guards": guard_summary(),
        # Railway keeps durable state under /data; a relative path would be lost on redeploy.
        "ledger_on_persistent_volume": ledger_path.startswith("/data/"),
        "email_hash_salt_configured": bool((settings.free_tier.email_hash_salt or "").strip()),
        "require_verified_email": bool(settings.free_tier.require_verified_email),
        "legacy_env_warnings": legacy_allowance_env_warnings(),
        "terms_url": TERMS_URL,
    }


# ---------------------------------------------------------------------------
# Identity: one grant per person across connectors (checkpoint D1 / 4.1)
# ---------------------------------------------------------------------------

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_GMAIL_DOMAINS = frozenset({"gmail.com", "googlemail.com"})
_EQUITY_LIKE_RE = re.compile(r"^[A-Z]{1,5}$")


def normalize_email(email: str | None) -> str | None:
    """Return a canonical lowercase email, folding sub-address tags and Gmail dots."""
    if not email:
        return None
    candidate = email.strip().lower()
    if not _EMAIL_RE.fullmatch(candidate):
        return None
    local, _, domain = candidate.rpartition("@")
    local = local.split("+", 1)[0]
    if domain in _GMAIL_DOMAINS:
        local = local.replace(".", "")
        domain = "gmail.com"
    if not local:
        return None
    return f"{local}@{domain}"


def email_domain(email: str | None) -> str | None:
    normalized = normalize_email(email)
    return normalized.rpartition("@")[2] if normalized else None


def _grant_salt() -> str:
    configured = (settings.free_tier.email_hash_salt or "").strip()
    if configured:
        return configured
    # Fall back to the production observability salt so grant keys stay
    # unpredictable without adding a new readiness requirement.
    from src.security_config import hash_salt

    return hash_salt("OBSERVABILITY_HASH_SALT")


def grant_key_for_email(email: str | None) -> str | None:
    """Return the salted hash used as the shared free-tier grant key."""
    normalized = normalize_email(email)
    if normalized is None:
        return None
    digest = hashlib.sha256(f"{_grant_salt()}\0{normalized}".encode("utf-8")).hexdigest()
    return f"ft_{digest[:40]}"


@lru_cache(maxsize=4)
def _load_domain_list(path_text: str, mtime: float) -> frozenset[str]:
    del mtime  # cache key only
    path = Path(path_text)
    if not path.is_file():
        return frozenset()
    domains: set[str] = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip().lower().lstrip("@")
        if line:
            domains.add(line)
    return frozenset(domains)


PACKAGED_DISPOSABLE_DOMAINS_PATH = Path(__file__).parent / "data" / "disposable_email_domains.txt"


def disposable_email_blocklist_path() -> Path:
    """Return the configured blocklist path, or the list shipped in the package."""
    configured = (settings.free_tier.disposable_email_blocklist_path or "").strip()
    return Path(configured) if configured else PACKAGED_DISPOSABLE_DOMAINS_PATH


def disposable_email_domains() -> frozenset[str]:
    """Return the maintained disposable-domain blocklist (empty when missing)."""
    path = disposable_email_blocklist_path()
    try:
        mtime = path.stat().st_mtime
    except OSError:
        return frozenset()
    return _load_domain_list(str(path), mtime)


def is_disposable_email(email: str | None) -> bool:
    domain = email_domain(email)
    if domain is None:
        return False
    if domain in settings.free_tier.email_domain_allowlist_set:
        return False
    blocked = disposable_email_domains()
    return domain in blocked or any(domain.endswith(f".{item}") for item in blocked)


@dataclass(frozen=True)
class Eligibility:
    eligible: bool
    reason: str
    grant_key: str | None = None
    normalized_email: str | None = None


def eligibility_for(identity: Any) -> Eligibility:
    """Decide whether an authenticated identity may draw from the free tier."""
    email = getattr(identity, "email", None)
    normalized = normalize_email(email)
    if normalized is None:
        return Eligibility(False, "missing_verified_email")
    verified = getattr(identity, "email_verified", None)
    if settings.free_tier.require_verified_email:
        if verified is False:
            return Eligibility(False, "email_not_verified", normalized_email=normalized)
        # OAuth logins must state verification explicitly; a login that says
        # nothing fails closed. Operator-issued beta tokens are vetted out of band.
        if verified is None and getattr(identity, "source", "oauth") == "oauth":
            return Eligibility(False, "email_verification_unknown", normalized_email=normalized)
    if is_disposable_email(normalized):
        return Eligibility(False, "disposable_email_domain", normalized_email=normalized)
    return Eligibility(True, "ok", grant_key_for_email(normalized), normalized)


# ---------------------------------------------------------------------------
# Free scope (data-rights gate, checkpoint section 5)
# ---------------------------------------------------------------------------

TOOL_SERVICES = {
    "get_vwap": "crypto_vwap",
    "get_bid_ask": "crypto_bidask",
    "get_fx_rate": "fx",
    "get_metal_price": "metals",
}


def looks_like_equity_symbol(symbol: str) -> bool:
    """Conservatively classify shared bid/ask symbols that are equity-like.

    Mirrors ``BlocksizeClient._is_equity_like_entry``: tokenized tickers such as
    ``AAPLXUSD`` (base ending in ``X`` quoted in USD stablecoins) and bare 1-5
    letter tickers that are not top-250 crypto assets. Unknown symbols lean
    toward "equity" so nothing outside the cleared free scope leaks for free.
    """
    clean = symbol.strip().upper().replace("-", "").replace("/", "").replace("_", "")
    for quote in ("USDT", "USDC", "USD"):
        if clean.endswith(quote) and len(clean) > len(quote):
            base = clean[: -len(quote)]
            return (
                base.endswith("X")
                and len(base) >= 2
                and base not in TOP_250_CRYPTO
                and base not in KNOWN_CRYPTO_X_BASES
            )
    return bool(_EQUITY_LIKE_RE.fullmatch(clean)) and clean not in TOP_250_CRYPTO


def service_for_tool(tool_name: str, symbol: str = "") -> str:
    """Map a connector tool call to the free-scope service key it consumes."""
    service = TOOL_SERVICES.get(tool_name)
    if service is None:
        raise KeyError(f"No free-scope service is defined for tool {tool_name!r}")
    if service == "crypto_bidask" and symbol and looks_like_equity_symbol(symbol):
        return "equity_bidask"
    return service


async def service_for_tool_async(tool_name: str, symbol: str, client: Any) -> str:
    """Resolve the free-scope service using the live instrument catalog.

    The catalog is authoritative (``BlocksizeClient.classify_symbol``). The
    naming heuristic is used only when the catalog cannot be read, so an
    upstream outage never turns cleared crypto data into a scope refusal.
    """
    service = TOOL_SERVICES.get(tool_name)
    if service is None:
        raise KeyError(f"No free-scope service is defined for tool {tool_name!r}")
    if service != "crypto_bidask" or not symbol:
        return service
    classify = getattr(client, "classify_symbol", None)
    if classify is None:
        return service_for_tool(tool_name, symbol)
    try:
        asset_class = await classify(symbol)
    except Exception:  # catalog unavailable: fall back, never fail closed on outage
        logger.warning("free_tier: instrument catalog unavailable, using symbol heuristic")
        return service_for_tool(tool_name, symbol)
    if not isinstance(asset_class, str):
        return service_for_tool(tool_name, symbol)
    return {
        "crypto": "crypto_bidask",
        "equity": "equity_bidask",
        "fx": "fx",
        "metal": "metals",
    }.get(asset_class, service_for_tool(tool_name, symbol))


def service_in_free_scope(service: str) -> bool:
    return service in settings.free_tier.allowed_service_set


def batch_items_allowed(item_count: int) -> bool:
    """Free-tier multi-item calls are capped below the paid batch maximum."""
    return 0 < int(item_count) <= int(settings.free_tier.max_batch_items)


# ---------------------------------------------------------------------------
# Payload helpers shared by connectors and HTTP surfaces
# ---------------------------------------------------------------------------

def today_utc() -> str:
    return datetime.now(UTC).date().isoformat()


def next_utc_day(usage_date: str | None = None) -> str:
    from datetime import date, timedelta

    current = date.fromisoformat(usage_date or today_utc())
    return (current + timedelta(days=1)).isoformat()


def scope_excluded_payload(service: str) -> dict[str, Any]:
    return {
        "service": service,
        "free_scope": sorted(settings.free_tier.allowed_service_set),
        "reason": (
            "This data package is not yet cleared for the free tier. Use signed x402 "
            "for direct public HTTP or contact Blocksize sales about an account plan."
        ),
        "upgrade_path": upgrade_path_text(),
        "attribution": attribution_payload(),
    }
