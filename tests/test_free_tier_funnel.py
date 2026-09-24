"""Step D: subscription funnel from the free tier (CTAs, events, dashboard)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from src import anthropic_mcp_server as claude
from src.config import settings
from src.connector_auth import ConnectorIdentity
from src.entitlement_manager import EntitlementManager
from src.models import VWAPData
from src.observability import UsageEventStore
from src.resource_server import app


def _events(store) -> list[dict]:
    return store.recent_events(limit=500)


@pytest.fixture
def connector(tmp_path, monkeypatch):
    monkeypatch.setattr(settings.free_tier, "enabled", True)
    monkeypatch.setattr(settings.free_tier, "monthly_credits", 10)
    monkeypatch.setattr(settings.free_tier, "per_minute_credits", 0)
    monkeypatch.setattr(settings.free_tier, "daily_soft_cap_credits", 0)
    monkeypatch.setattr(settings.free_tier, "global_daily_cap_credits", 0)
    monkeypatch.setattr(settings.free_tier, "abuse_fast_drain_ratio", 1.01)
    monkeypatch.setattr(settings.free_tier, "abuse_sweep_distinct_symbols", 1_000)
    client = AsyncMock()
    client.get_vwap_latest = AsyncMock(
        side_effect=lambda pair: VWAPData(
            pair=pair,
            vwap=1.0,
            timestamp=datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc),
            currency="USD",
        )
    )
    claude._client = client
    claude._entitlements = EntitlementManager(tmp_path / "claude.db", default_daily_credits=10)
    identity = ConnectorIdentity(
        user_id="u1", email="one@example.org", source="test", principal_id="test:u1"
    )
    monkeypatch.setattr(claude.anthropic_auth, "resolve_anthropic_identity", lambda: identity)
    yield claude
    claude._client = None
    claude._entitlements = None


@pytest.mark.asyncio
async def test_live_calls_surface_trial_cta_from_half_of_the_pool(connector, isolate_usage_event_store):
    results = [await connector.anthropic_get_vwap(f"S{index}USD") for index in range(6)]

    assert "Upgrade:" not in results[3]  # 4/10 consumed
    assert "Upgrade: Start a free trial" in results[4]  # 5/10 consumed = 50%
    assert "/go/free-trial?utm_source=claude_mcp" in results[4]
    assert "/go/pricing?" in results[4]
    balance = json.loads(await connector.anthropic_get_credit_balance())["credits"]
    assert balance["upgrade"]["plan_id"] == "startup"  # 6 distinct instruments in 30 days
    assert balance["upgrade"]["primary"]["path"].startswith("/go/free-trial?")
    assert balance["upgrade_recommendation"]["trigger"] == "consumption_50"
    assert balance["upgrade_recommendation"]["distinct_instruments_30d"] == 6

    events = [event for event in _events(isolate_usage_event_store) if event["event"] == "upgrade_cta_shown"]
    # One impression per identity, trigger, and day: consumption_50 only (breadth is folded in).
    # It fired on the fifth call, when only five instruments had been used.
    assert len(events) == 1
    assert events[0]["metadata"]["plan_id"] == "developer"
    assert events[0]["metadata"]["trigger"] == "consumption_50"


@pytest.mark.asyncio
async def test_exhausted_and_rate_limited_payloads_carry_ctas(connector, isolate_usage_event_store, monkeypatch):
    for index in range(10):
        await connector.anthropic_get_vwap(f"S{index}USD")
    exhausted = json.loads(await connector.anthropic_get_vwap("BTCUSD"))

    assert exhausted["error_code"] == "DAILY_CREDIT_LIMIT_REACHED"
    details = json.loads(exhausted["details"])
    assert details["upgrade"]["trigger"] == "monthly_pool_exhausted"
    assert details["upgrade"]["plan_id"] == "startup"
    assert details["upgrade"]["primary"]["path"].startswith("/go/free-trial?")
    assert "/go/free-trial" in exhausted["message"]

    monkeypatch.setattr(settings.free_tier, "monthly_credits", 100)
    monkeypatch.setattr(settings.free_tier, "per_minute_credits", 1)
    await connector.anthropic_get_vwap("BTCUSD")
    limited = json.loads(await connector.anthropic_get_vwap("ETHUSD"))
    assert limited["error_code"] == "FREE_TIER_RATE_LIMITED"
    assert json.loads(limited["details"])["upgrade"]["secondary"]["path"].startswith("/go/pricing?")

    triggers = {
        event["metadata"]["trigger"]
        for event in _events(isolate_usage_event_store)
        if event["event"] == "upgrade_cta_shown"
    }
    assert {"consumption_50", "monthly_pool_exhausted", "rate_limited_minute"} <= triggers


def test_observability_free_tier_panel_summarizes_the_funnel(tmp_path):
    store = UsageEventStore(tmp_path / "events.db")
    trusted = {"identity_trust": "verified_oauth", "identity_hash": "id-1"}
    store.record("free_tier_grant_created", surface="claude_mcp", metadata={"grant_hash": "g1", **trusted})
    store.record("free_tier_grant_created", surface="cursor_mcp", metadata={"grant_hash": "g2", **trusted})
    store.record("free_tier_threshold_crossed", surface="claude_mcp", metadata={"grant_hash": "g1", "threshold_pct": 50})
    store.record("free_tier_threshold_crossed", surface="claude_mcp", metadata={"grant_hash": "g1", "threshold_pct": 100})
    store.record("free_tier_exhausted", surface="claude_mcp", reason="monthly_pool_consumed", metadata={"grant_hash": "g1", **trusted})
    store.record("free_tier_rate_limited", surface="claude_mcp", reason="rate_limited_minute", metadata={"grant_hash": "g2"})
    store.record("free_tier_abuse_flagged", surface="claude_mcp", reason="symbol_sweep", metadata={"grant_hash": "g2", "flags": ["symbol_sweep"]})
    store.record("mcp_credit_drawdown_failed", surface="claude_mcp", reason="free_scope_excluded", metadata={})
    store.record("upgrade_cta_shown", surface="claude_mcp", metadata={"plan_id": "developer", "trigger": "consumption_50"})
    store.record("upgrade_cta_shown", surface="claude_mcp", metadata={"plan_id": "startup", "trigger": "monthly_pool_exhausted"})
    store.record("outbound_conversion_click", surface="http", subject="free-trial", metadata={"destination": "free-trial"})
    store.record("outbound_conversion_click", surface="http", subject="pricing", metadata={"destination": "pricing"})
    store.record("outbound_conversion_click", surface="http", subject="free-trial", metadata={"destination": "free-trial"})

    panel = store.summarize(days=1, include_synthetic=True)["free_tier"]["summary"]

    assert panel["grants_created"] == 2
    assert panel["exhausted_grants"] == 1
    assert panel["exhaustion_rate"] == 0.5
    assert panel["threshold_crossings"] == {"100%": 1, "50%": 1}
    assert panel["denials_by_reason"] == {"rate_limited_minute": 1, "free_scope_excluded": 1}
    assert panel["abuse_flags_by_reason"] == {"symbol_sweep": 1}
    assert panel["cta_impressions"] == 2
    assert panel["cta_impressions_by_trigger"] == {"consumption_50": 1, "monthly_pool_exhausted": 1}
    assert panel["go_clicks"] == 3
    assert panel["trial_starts"] == 2
    assert panel["cta_click_through_rate"] == 1.5
    growth = store.summarize(days=1, include_synthetic=True)["growth_funnel"]["summary"]
    assert growth["credits_exhausted_identities"] == 1


@pytest.fixture
def dashboard_client(monkeypatch, tmp_path, isolate_usage_event_store):
    from src import resource_server

    # HTTP surfaces write through the global store; the stats endpoint reads
    # the module-level store. Point both at the per-test database.
    monkeypatch.setattr(resource_server, "OBSERVABILITY", isolate_usage_event_store)
    monkeypatch.setenv("RWA_MUTATIONS_ENABLED", "true")
    monkeypatch.setenv("RWA_OPERATOR_TOKEN", "rwa-test-operator-token-0123456789abcdef")
    monkeypatch.setenv("RWA_OBSERVATION_DB_PATH", str(tmp_path / "rwa_observations.sqlite3"))
    monkeypatch.setenv("CREDIT_DB_PATH", str(tmp_path / "credits.sqlite3"))
    monkeypatch.setattr(settings.x402, "solana_wallet_address", "11111111111111111111111111111111")
    monkeypatch.setattr(settings.x402, "solana_fee_payer", "SysvarRent111111111111111111111111111111111")
    monkeypatch.setattr(settings.x402, "evm_wallet_address", "0x1111111111111111111111111111111111111111")
    monkeypatch.setattr(settings.server, "observability_dashboard_token", "obs-dashboard-token-0123456789abcdefABCDEF")
    with TestClient(
        app,
        base_url="https://testserver",
        headers={"Authorization": "Bearer obs-dashboard-token-0123456789abcdefABCDEF"},
    ) as client:
        # Seed the instrument catalog so paid preflight never touches upstream.
        from src import resource_server

        seeded_at = resource_server.time.monotonic()
        app.state.instrument_catalog_cache = {
            "vwap": (seeded_at, frozenset({"BTCUSD", "ETHUSD"})),
            "bidask": (seeded_at, frozenset({"BTCUSD", "ETHUSD"})),
            "fx": (seeded_at, frozenset({"EURUSD"})),
            "metal": (seeded_at, frozenset({"XAUUSD"})),
            "state": (seeded_at, frozenset({"SOLUSD"})),
        }
        yield client


def test_stats_and_dashboard_expose_the_free_tier_panel(dashboard_client):
    stats = dashboard_client.get("/internal/observability/stats?days=1")
    assert stats.status_code == 200
    panel = stats.json()["free_tier"]
    assert panel["config"]["monthly_credits"] == settings.free_tier.monthly_credits
    assert panel["config"]["allowed_services"] == sorted(settings.free_tier.allowed_service_set)
    assert "worst_case_exposure_credits" in panel["ledger"]
    assert "global_cap_remaining_today" in panel["ledger"]
    assert "cta_impressions" in panel["summary"]

    page = dashboard_client.get("/internal/observability")
    assert page.status_code == 200, page.text[:300]
    for element_id in ("free-tier-kpis", "free-tier-thresholds", "free-tier-cta", "free-tier-clicks", "free-tier-boundary"):
        assert f'id="{element_id}"' in page.text
    assert "renderFreeTier(data)" in page.text


def test_402_and_go_redirect_record_cta_impression_and_click(dashboard_client):
    challenge = dashboard_client.get("/v1/vwap/btc-usd")
    assert challenge.status_code == 402, challenge.text
    upgrade = challenge.json()["starter_credits"]["upgrade"]
    assert upgrade["primary"]["path"].startswith("/go/free-trial?utm_source=http-402")
    assert upgrade["secondary"]["path"].startswith("/go/pricing?")

    redirect = dashboard_client.get(upgrade["primary"]["path"], follow_redirects=False)
    assert redirect.status_code == 307
    assert redirect.headers["location"].startswith("https://matrix.blocksize.capital/?")
    assert "utm_term=surface" in redirect.headers["location"]

    stats = dashboard_client.get("/internal/observability/stats?days=1&include_synthetic=true").json()
    summary = stats["free_tier"]["summary"]
    assert summary["cta_impressions_by_trigger"].get("payment_required", 0) >= 1
    assert summary["trial_starts"] >= 1


def test_licence_points_at_the_published_blocksize_data_terms():
    from src import free_tier

    licence = free_tier.licence_payload()
    assert licence["terms_url"] == "https://blocksize.info/terms-conditions-data/"
    assert "Blocksize data terms" in licence["summary"]
    assert free_tier.offer_payload()["licence"]["terms_url"] == licence["terms_url"]


def test_operational_alerts_cover_the_free_tier(dashboard_client, monkeypatch):
    from src.resource_server import _build_operational_alerts

    baseline = dashboard_client.get("/internal/observability/stats?days=1&include_synthetic=true").json()
    ids = {alert["id"] for alert in baseline["operational_alerts"]["alerts"]}
    assert not {alert_id for alert_id in ids if alert_id.startswith("free-tier-")}

    summary = dict(baseline)
    summary["free_tier"] = {
        "summary": {
            "grants_created": 20,
            "exhaustion_rate": 0.5,
            "abuse_flags_by_reason": {"symbol_sweep": 2},
            "cta_impressions": 80,
            "go_clicks": 0,
        },
        "ledger": {"global_daily_cap_credits": 1000, "global_credits_today": 850},
        "config": {"enabled": False},
    }
    alerts = _build_operational_alerts(summary)["alerts"]
    by_id = {alert["id"]: alert for alert in alerts}
    assert by_id["free-tier-disabled"]["severity"] == "P1"
    assert by_id["free-tier-global-cap-pressure"]["value"] == "85.0%"
    assert by_id["free-tier-abuse-flags"]["value"] == "2"
    assert by_id["free-tier-exhaustion-high"]["severity"] == "P2"
    assert by_id["free-tier-cta-not-converting"]["severity"] == "P2"


def test_health_reports_free_tier_deploy_status_and_legacy_env(dashboard_client, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_DAILY_CREDITS", "50")
    monkeypatch.setattr(settings.free_tier, "ledger_db_path", "/data/free_tier_ledger.db")

    health = dashboard_client.get("/health").json()["free_tier"]

    assert health["enabled"] is True
    assert health["monthly_credits"] == settings.free_tier.monthly_credits
    assert health["allowed_services"] == sorted(settings.free_tier.allowed_service_set)
    assert health["ledger_on_persistent_volume"] is True
    assert health["terms_url"] == "https://blocksize.info/terms-conditions-data/"
    assert health["legacy_env_warnings"] == [
        f"ANTHROPIC_DAILY_CREDITS is set but ignored; the free allowance is FREE_TIER_MONTHLY_CREDITS={settings.free_tier.monthly_credits}"
    ]
    assert "salt" not in str(health).lower().replace("email_hash_salt_configured", "")
