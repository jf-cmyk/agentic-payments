"""Connector-level behaviour of the free tier through the shared factory.

Uses the Claude and Cursor servers with mocked upstream data so the gate,
shared pool, denial payloads, attribution, and events are exercised end to end.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src import anthropic_mcp_server as claude
from src import cursor_mcp_server as cursor
from src import free_tier
from src.config import settings
from src.connector_auth import ConnectorIdentity
from src.entitlement_manager import EntitlementManager
from src.free_tier_ledger import get_free_tier_ledger
from src.models import VWAPData


def _event_counts(store) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in store.recent_events(limit=500):
        counts[event["event"]] = counts.get(event["event"], 0) + 1
    return counts


def _vwap_client() -> AsyncMock:
    client = AsyncMock()
    client.get_vwap_latest = AsyncMock(
        return_value=VWAPData(
            pair="btc-usd",
            vwap=95432.5,
            timestamp=datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc),
            currency="USD",
        )
    )
    client.get_fx_rate = AsyncMock(side_effect=AssertionError("must not be called"))
    client.get_bidask_snapshot = AsyncMock(side_effect=AssertionError("must not be called"))
    return client


@pytest.fixture(autouse=True)
def connector_state(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.free_tier, "enabled", True)
    monkeypatch.setattr(settings.free_tier, "monthly_credits", 3)
    monkeypatch.setattr(settings.free_tier, "per_minute_credits", 0)
    monkeypatch.setattr(settings.free_tier, "daily_soft_cap_credits", 0)
    monkeypatch.setattr(settings.free_tier, "global_daily_cap_credits", 0)
    monkeypatch.setattr(settings.free_tier, "allowed_services", "crypto_vwap,crypto_bidask")
    monkeypatch.setattr(settings.free_tier, "abuse_fast_drain_ratio", 1.01)
    monkeypatch.setattr(settings.free_tier, "email_hash_salt", "connector-test-salt-" * 2)
    claude._client = _vwap_client()
    cursor._client = _vwap_client()
    claude._entitlements = EntitlementManager(tmp_path / "claude.db", default_daily_credits=3)
    cursor._entitlements = EntitlementManager(tmp_path / "cursor.db", default_daily_credits=3)
    monkeypatch.setattr(claude.anthropic_auth, "resolve_anthropic_identity", lambda: None)
    monkeypatch.setattr(cursor.cursor_auth, "resolve_cursor_identity", lambda: None)
    yield
    claude._client = cursor._client = None
    claude._entitlements = cursor._entitlements = None


def _use(monkeypatch, server_module, identity: ConnectorIdentity) -> None:
    if server_module is claude:
        monkeypatch.setattr(claude.anthropic_auth, "resolve_anthropic_identity", lambda: identity)
    else:
        monkeypatch.setattr(cursor.cursor_auth, "resolve_cursor_identity", lambda: identity)


def _identity(user_id: str, email: str | None, **extra) -> ConnectorIdentity:
    return ConnectorIdentity(
        user_id=user_id,
        email=email,
        source="test",
        principal_id=f"test:{user_id}",
        **extra,
    )


@pytest.mark.asyncio
async def test_live_call_returns_attribution_and_monthly_balance(monkeypatch):
    _use(monkeypatch, claude, _identity("u1", "one@example.org"))

    result = await claude.anthropic_get_vwap("btc-usd")

    assert "VWAP [btc-usd]" in result
    assert "Credits remaining this month: 2/3" in result
    assert "resets " in result
    assert f"{free_tier.ATTRIBUTION_TEXT}: {free_tier.ATTRIBUTION_URL}" in result


@pytest.mark.asyncio
async def test_one_person_shares_one_pool_across_connectors(monkeypatch, isolate_usage_event_store):
    _use(monkeypatch, claude, _identity("claude-u", "Same.Person+claude@gmail.com"))
    _use(monkeypatch, cursor, _identity("cursor-u", "samepers.on+cursor@GMAIL.com"))

    first = await claude.anthropic_get_vwap("btc-usd")
    second = await cursor.cursor_get_vwap("btc-usd")
    third = await claude.anthropic_get_vwap("eth-usd")
    fourth = json.loads(await cursor.cursor_get_vwap("sol-usd"))

    assert "Credits remaining this month: 2/3" in first
    assert "Credits remaining this month: 1/3" in second
    assert "Credits remaining this month: 0/3" in third
    assert fourth["error_code"] == "DAILY_CREDIT_LIMIT_REACHED"
    details = json.loads(fourth["details"])
    assert details["reason"] == "monthly_pool_exhausted"
    assert details["credits_remaining"] == 0 and details["monthly_limit"] == 3
    assert details["attribution"]["required"] is True
    # Each connector ledger only saw its own charges; the shared pool saw all three.
    assert claude._entitlements.status("claude-u").credits_spent == 2
    assert cursor._entitlements.status("cursor-u").credits_spent == 1
    grant_key = free_tier.grant_key_for_email("sameperson@gmail.com")
    assert get_free_tier_ledger().status(grant_key).credits_spent == 3

    counts = _event_counts(isolate_usage_event_store)
    assert counts["free_tier_grant_created"] == 1
    assert counts["free_tier_threshold_crossed"] == 4  # 50, 80, 95, 100
    assert counts["free_tier_exhausted"] == 2  # 100% crossing + the denied call


@pytest.mark.asyncio
async def test_balance_tool_reports_effective_shared_remaining(monkeypatch):
    _use(monkeypatch, claude, _identity("claude-u", "person@example.org"))
    _use(monkeypatch, cursor, _identity("cursor-u", "person@example.org"))
    await cursor.cursor_get_vwap("btc-usd")

    balance = json.loads(await claude.anthropic_get_credit_balance())["credits"]

    assert balance["monthly_limit"] == 3
    assert balance["credits_spent"] == 0  # this connector's ledger
    assert balance["credits_remaining"] == 2  # effective, shared pool
    assert balance["shared_pool"]["credits_spent"] == 1
    assert balance["resets_at"].endswith("-01")
    assert balance["attribution"]["text"] == "Data by Blocksize"
    assert balance["free_tier"]["free_scope"] == ["crypto_bidask", "crypto_vwap"]
    assert "user_id" not in balance and "email" not in balance


@pytest.mark.asyncio
async def test_kill_switch_returns_clear_payload_without_spending(monkeypatch):
    monkeypatch.setattr(settings.free_tier, "enabled", False)
    _use(monkeypatch, claude, _identity("u1", "one@example.org"))

    parsed = json.loads(await claude.anthropic_get_vwap("btc-usd"))

    assert parsed["error_code"] == "FREE_TIER_DISABLED"
    assert "signed x402" in parsed["message"]
    assert json.loads(parsed["details"])["attribution"]["text"] == "Data by Blocksize"
    claude._client.get_vwap_latest.assert_not_called()
    assert claude._entitlements.status("u1").credits_spent == 0


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("identity", "reason"),
    [
        (_identity("u1", None), "missing_verified_email"),
        (_identity("u1", "one@example.org", email_verified=False), "email_not_verified"),
        (_identity("u1", "one@mailinator.com"), "disposable_email_domain"),
    ],
)
async def test_ineligible_identities_are_refused_before_any_spend(monkeypatch, identity, reason):
    _use(monkeypatch, claude, identity)

    parsed = json.loads(await claude.anthropic_get_vwap("btc-usd"))

    assert parsed["error_code"] == "FREE_TIER_INELIGIBLE"
    assert json.loads(parsed["details"])["reason"] == reason
    claude._client.get_vwap_latest.assert_not_called()


@pytest.mark.asyncio
async def test_services_outside_the_data_rights_scope_are_refused(monkeypatch):
    _use(monkeypatch, claude, _identity("u1", "one@example.org"))

    fx = json.loads(await claude.anthropic_get_fx_rate("EURUSD"))
    equity = json.loads(await claude.anthropic_get_bid_ask("AAPLXUSD"))

    assert fx["error_code"] == "FREE_TIER_SCOPE_EXCLUDED"
    assert json.loads(fx["details"])["service"] == "fx"
    assert equity["error_code"] == "FREE_TIER_SCOPE_EXCLUDED"
    assert json.loads(equity["details"])["service"] == "equity_bidask"
    assert json.loads(equity["details"])["free_scope"] == ["crypto_bidask", "crypto_vwap"]
    assert claude._entitlements.status("u1").credits_spent == 0

    monkeypatch.setattr(settings.free_tier, "allowed_services", "crypto_vwap,fx")
    claude._client.get_fx_rate = AsyncMock(side_effect=RuntimeError("upstream down"))
    parsed = json.loads(await claude.anthropic_get_fx_rate("EURUSD"))
    assert parsed["error_code"] == "INTERNAL_ERROR"  # scope passed; upstream failed and refunded
    assert claude._entitlements.status("u1").credits_spent == 0
    grant_key = free_tier.grant_key_for_email("one@example.org")
    assert get_free_tier_ledger().status(grant_key).credits_spent == 0


@pytest.mark.asyncio
async def test_rate_limit_payload_includes_retry_after(monkeypatch, isolate_usage_event_store):
    monkeypatch.setattr(settings.free_tier, "per_minute_credits", 1)
    _use(monkeypatch, claude, _identity("u1", "one@example.org"))

    await claude.anthropic_get_vwap("btc-usd")
    parsed = json.loads(await claude.anthropic_get_vwap("eth-usd"))

    assert parsed["error_code"] == "FREE_TIER_RATE_LIMITED"
    details = json.loads(parsed["details"])
    assert details["reason"] == "rate_limited_minute"
    assert 1 <= details["retry_after_seconds"] <= 60
    assert claude._entitlements.status("u1").credits_spent == 1
    counts = _event_counts(isolate_usage_event_store)
    assert counts["free_tier_rate_limited"] == 1


@pytest.mark.asyncio
async def test_abuse_flag_suspends_both_ledgers(monkeypatch, isolate_usage_event_store):
    monkeypatch.setattr(settings.free_tier, "abuse_sweep_distinct_symbols", 1)
    _use(monkeypatch, claude, _identity("u1", "one@example.org"))

    await claude.anthropic_get_vwap("btc-usd")
    flagged = await claude.anthropic_get_vwap("eth-usd")
    blocked = json.loads(await claude.anthropic_get_vwap("sol-usd"))

    assert "VWAP" in flagged
    assert blocked["error_code"] == "FREE_TIER_SUSPENDED"
    assert claude._entitlements.status("u1").status == "suspended"
    grant_key = free_tier.grant_key_for_email("one@example.org")
    assert get_free_tier_ledger().status(grant_key).status == "suspended"
    counts = _event_counts(isolate_usage_event_store)
    assert counts["free_tier_abuse_flagged"] == 1
