from __future__ import annotations

from src.entitlement_manager import EntitlementManager, connector_entitlement_manager


def test_daily_status_creates_default_allowance(tmp_path):
    manager = EntitlementManager(tmp_path / "entitlements.db", default_daily_credits=50)

    status = manager.status("user-1", "user@example.com", usage_date="2026-04-29")

    assert status.daily_limit == 50
    assert status.credits_spent == 0
    assert status.credits_remaining == 50
    assert status.email == "user@example.com"


def test_spend_is_atomic_and_caps_daily_usage(tmp_path):
    manager = EntitlementManager(tmp_path / "entitlements.db", default_daily_credits=3)

    ok, status = manager.spend(
        "user-1",
        2,
        email="user@example.com",
        tool_name="get_vwap",
        subject="BTC-USD",
        usage_date="2026-04-29",
    )
    assert ok is True
    assert status.credits_remaining == 1

    ok, status = manager.spend(
        "user-1",
        2,
        tool_name="get_vwap",
        subject="ETH-USD",
        usage_date="2026-04-29",
    )
    assert ok is False
    assert status.credits_remaining == 1


def test_usage_resets_by_day(tmp_path):
    manager = EntitlementManager(tmp_path / "entitlements.db", default_daily_credits=3)

    manager.spend(
        "user-1",
        3,
        tool_name="get_vwap",
        subject="BTC-USD",
        usage_date="2026-04-29",
    )

    same_day = manager.status("user-1", usage_date="2026-04-29")
    next_day = manager.status("user-1", usage_date="2026-04-30")

    assert same_day.credits_remaining == 0
    assert next_day.credits_remaining == 3


def test_refund_restores_credits(tmp_path):
    manager = EntitlementManager(tmp_path / "entitlements.db", default_daily_credits=5)

    manager.spend(
        "user-1",
        2,
        tool_name="get_fx_rate",
        subject="EURUSD",
        usage_date="2026-04-29",
    )
    status = manager.refund(
        "user-1",
        2,
        tool_name="get_fx_rate",
        subject="EURUSD",
        usage_date="2026-04-29",
    )

    assert status.credits_spent == 0
    assert status.credits_remaining == 5


def test_set_daily_limit_supports_subscriber_overrides(tmp_path):
    manager = EntitlementManager(tmp_path / "entitlements.db", default_daily_credits=50)

    status = manager.set_daily_limit("user-1", 250, email="subscriber@example.com")

    assert status.daily_limit == 250
    assert status.credits_remaining == 250
    assert status.email == "subscriber@example.com"


def test_connector_entitlement_manager_uses_prefix_specific_env(tmp_path, monkeypatch):
    anthropic_db = tmp_path / "anthropic.db"
    cursor_db = tmp_path / "cursor.db"
    monkeypatch.setenv("ANTHROPIC_ENTITLEMENT_DB_PATH", str(anthropic_db))
    monkeypatch.setenv("ANTHROPIC_DAILY_CREDITS", "75")
    monkeypatch.setenv("CURSOR_ENTITLEMENT_DB_PATH", str(cursor_db))
    monkeypatch.setenv("CURSOR_DAILY_CREDITS", "25")

    anthropic = connector_entitlement_manager("ANTHROPIC")
    cursor = connector_entitlement_manager("CURSOR")

    assert anthropic.db_path == str(anthropic_db)
    assert anthropic.default_daily_credits == 75
    assert cursor.db_path == str(cursor_db)
    assert cursor.default_daily_credits == 25


def test_cursor_fallback_does_not_follow_anthropic_db_path(tmp_path, monkeypatch):
    anthropic_db = tmp_path / "anthropic.db"
    monkeypatch.setenv("ANTHROPIC_ENTITLEMENT_DB_PATH", str(anthropic_db))
    monkeypatch.setenv("ANTHROPIC_DAILY_CREDITS", "75")
    monkeypatch.delenv("CURSOR_ENTITLEMENT_DB_PATH", raising=False)
    monkeypatch.delenv("CURSOR_DAILY_CREDITS", raising=False)

    cursor = connector_entitlement_manager(
        "CURSOR",
        fallback_db_path="anthropic_entitlements.db",
        fallback_daily_credits=50,
    )

    assert cursor.db_path == "anthropic_entitlements.db"
    assert cursor.default_daily_credits == 50
