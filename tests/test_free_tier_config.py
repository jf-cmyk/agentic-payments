"""Single-source-of-truth checks for the free-tier allowance configuration."""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from src import free_tier, public_metadata
from src.config import FreeTierSettings, settings
from src.entitlement_manager import EntitlementManager, connector_entitlement_manager


ROOT = Path(__file__).resolve().parents[1]

# Copy that used to hard-code the 50-credit allowance or the wrong "per UTC day"
# reset. Superseded GTM plans and generated PDFs are excluded on purpose.
ALLOWANCE_COPY_FILES = (
    Path("README.md"),
    Path(".env.example"),
    Path("src/public_metadata.py"),
    Path("src/mcp_server.py"),
    Path("src/public_mcp_server.py"),
    Path("src/resource_server.py"),
    Path("src/authenticated_mcp_server.py"),
    Path("src/anthropic_mcp_server.py"),
    Path("src/cursor_mcp_server.py"),
    Path("src/openai_mcp_server.py"),
    Path("docs/README_EXTERNAL.md"),
    Path("docs/developer_portal.html"),
    Path("docs/anthropic_beta_connector_runbook.md"),
    Path("docs/openai_mcp_deployment_runbook.md"),
    Path("docs/gtm/claude_connector_launch_status.md"),
    Path("docs/gtm/claude_connector_submission.md"),
    Path("docs/gtm/pay_skills_submission/providers/blocksize/market-data/PAY.md"),
    Path("pay-skills/providers/blocksize/market-data/PAY.md"),
)

STALE_ALLOWANCE_CLAIMS = (
    re.compile(r"\b50-credit\b", re.IGNORECASE),
    re.compile(r"\b50 (?:free )?live[- ]data credits\b", re.IGNORECASE),
    re.compile(r"\bstart with 50\b", re.IGNORECASE),
    re.compile(r"\bup to 50 (?:live|test) (?:data )?credits\b", re.IGNORECASE),
    re.compile(r"\b50 (?:daily )?credits per user\b", re.IGNORECASE),
    re.compile(r"\bper UTC day\b", re.IGNORECASE),
    re.compile(r"\b(?:ANTHROPIC|CURSOR|OPENAI)_DAILY_CREDITS=\d+"),
    re.compile(r"\bnot[- _]free[- _]forever\b", re.IGNORECASE),
)


def test_free_tier_defaults_match_the_checkpoint_decisions() -> None:
    defaults = FreeTierSettings(_env_file=None)

    assert defaults.enabled is True
    assert defaults.monthly_credits == 15_000
    assert defaults.per_minute_credits == 30
    assert defaults.daily_soft_cap_credits == 2_000
    assert defaults.max_batch_items == 5
    assert defaults.global_daily_cap_credits == 200_000
    # Data-rights gate: every production package was cleared on 2026-09-23.
    assert defaults.allowed_service_set == frozenset(FreeTierSettings.KNOWN_SERVICES)
    assert defaults.require_verified_email is True
    assert defaults.max_batch_items < settings.server.max_batch_size


def test_free_tier_settings_read_environment_overrides(monkeypatch) -> None:
    monkeypatch.setenv("FREE_TIER_ENABLED", "false")
    monkeypatch.setenv("FREE_TIER_MONTHLY_CREDITS", "20000")
    monkeypatch.setenv("FREE_TIER_ALLOWED_SERVICES", " crypto_vwap, FX ,metals ")
    monkeypatch.setenv("FREE_TIER_EMAIL_DOMAIN_ALLOWLIST", "@Blocksize.info, partner.example")

    configured = FreeTierSettings(_env_file=None)

    assert configured.enabled is False
    assert configured.monthly_credits == 20_000
    assert configured.allowed_service_set == frozenset({"crypto_vwap", "fx", "metals"})
    assert configured.email_domain_allowlist_set == frozenset(
        {"blocksize.info", "partner.example"}
    )


def test_unknown_free_scope_service_is_rejected(monkeypatch) -> None:
    monkeypatch.setenv("FREE_TIER_ALLOWED_SERVICES", "crypto_vwap,history_export")

    with pytest.raises(ValueError, match="history_export"):
        FreeTierSettings(_env_file=None).allowed_service_set


def test_free_tier_settings_reject_negative_or_zero_batch(monkeypatch) -> None:
    monkeypatch.setenv("FREE_TIER_MONTHLY_CREDITS", "-1")
    with pytest.raises(ValueError):
        FreeTierSettings(_env_file=None)

    monkeypatch.delenv("FREE_TIER_MONTHLY_CREDITS")
    monkeypatch.setenv("FREE_TIER_MAX_BATCH_ITEMS", "0")
    with pytest.raises(ValueError):
        FreeTierSettings(_env_file=None)


def test_offer_payload_is_generated_from_settings(monkeypatch) -> None:
    monkeypatch.setattr(settings.free_tier, "monthly_credits", 12_500)

    offer = free_tier.offer_payload()

    assert offer["allowance_credits"] == 12_500
    assert offer["positioning"] == "Start with 12,500 free live-data credits every month"
    assert offer["period"] == "calendar_month_utc"
    assert offer["recurring"] is True
    assert offer["eligibility"] == "authenticated_connector_only"
    assert offer["attribution"] == {
        "required": True,
        "text": "Data by Blocksize",
        "url": free_tier.ATTRIBUTION_URL,
    }
    assert offer["licence"]["id"] == free_tier.LICENCE_ID
    assert "production_use_requires_subscription" in offer["licence"]["terms"]
    assert offer["guards"]["allowed_services"] == sorted(settings.free_tier.allowed_service_set)
    assert "signed x402" in offer["upgrade_path"]
    json.dumps(offer)


def test_allowance_flows_into_every_ledger_default(monkeypatch) -> None:
    monkeypatch.setattr(settings.free_tier, "monthly_credits", 9_000)

    assert free_tier.allowance_credits() == 9_000
    assert EntitlementManager(":memory:").default_daily_credits == 9_000
    assert (
        connector_entitlement_manager("CURSOR", fallback_db_path=":memory:").default_daily_credits
        == 9_000
    )


def test_legacy_allowance_variables_are_reported_as_ignored(monkeypatch) -> None:
    for name in free_tier.LEGACY_ALLOWANCE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    assert free_tier.legacy_allowance_env_warnings() == []

    monkeypatch.setenv("ANTHROPIC_DAILY_CREDITS", "50")
    monkeypatch.setenv("STARTER_CREDIT_ALLOWANCE", "50")

    warnings = free_tier.legacy_allowance_env_warnings()

    assert len(warnings) == 2
    assert all("ignored" in message for message in warnings)
    assert all(f"FREE_TIER_MONTHLY_CREDITS={free_tier.allowance_credits()}" in m for m in warnings)


def test_public_description_states_the_configured_allowance() -> None:
    label = free_tier.allowance_label()

    assert f"{label} live-data credits every calendar month" in public_metadata.PUBLIC_DESCRIPTION
    assert "starter allowance" in public_metadata.PUBLIC_DESCRIPTION
    assert "only to eligible authenticated connector users" in public_metadata.PUBLIC_DESCRIPTION


def test_no_repo_copy_still_advertises_the_retired_50_credit_allowance() -> None:
    label = free_tier.allowance_label()
    stale: dict[str, list[str]] = {}
    missing_current_number: list[str] = []
    for relative_path in ALLOWANCE_COPY_FILES:
        path = ROOT / relative_path
        assert path.is_file(), path
        text = path.read_text(encoding="utf-8")
        matches = [pattern.pattern for pattern in STALE_ALLOWANCE_CLAIMS if pattern.search(text)]
        if matches:
            stale[str(relative_path)] = matches
        if relative_path.suffix in {".md", ".html"} or relative_path.name == ".env.example":
            if label not in text and "FREE_TIER_MONTHLY_CREDITS" not in text:
                missing_current_number.append(str(relative_path))

    assert stale == {}
    assert missing_current_number == []
