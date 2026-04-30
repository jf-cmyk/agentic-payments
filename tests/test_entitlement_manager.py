from __future__ import annotations

from src.entitlement_manager import EntitlementManager


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
