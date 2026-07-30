from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from src.entitlement_manager import (
    DEFAULT_PENDING_CHARGE_LEASE_SECONDS,
    ENTITLEMENT_SCHEMA_VERSION,
    MIN_PENDING_CHARGE_LEASE_SECONDS,
    PENDING_RECOVERY_BATCH_LIMIT,
    EntitlementManager,
    connector_entitlement_manager,
)


def test_starter_status_creates_default_allowance(tmp_path):
    manager = EntitlementManager(tmp_path / "entitlements.db", default_daily_credits=50)

    status = manager.status("user-1", "user@example.com", usage_date="2026-04-29")

    assert status.daily_limit == 50
    assert status.credits_spent == 0
    assert status.credits_remaining == 50
    assert status.email == "user@example.com"


def test_spend_is_atomic_and_caps_starter_usage(tmp_path):
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


def test_starter_usage_does_not_reset_by_day(tmp_path):
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
    assert next_day.credits_remaining == 0
    assert next_day.credits_spent == 3


def test_refund_restores_credits(tmp_path):
    manager = EntitlementManager(tmp_path / "entitlements.db", default_daily_credits=5)

    manager.spend(
        "user-1",
        2,
        tool_name="get_fx_rate",
        subject="EURUSD",
        usage_date="2026-04-29",
        charge_id="charge-1",
    )
    status = manager.refund(
        "user-1",
        2,
        tool_name="get_fx_rate",
        subject="EURUSD",
        usage_date="2026-04-29",
        charge_id="charge-1",
    )

    duplicate = manager.refund(
        "user-1",
        2,
        tool_name="get_fx_rate",
        subject="EURUSD",
        usage_date="2026-04-29",
        charge_id="charge-1",
    )

    assert status.credits_spent == 0
    assert status.credits_remaining == 5
    assert duplicate.credits_spent == 0
    assert duplicate.credits_remaining == 5


def test_refund_resolves_original_charge_date_across_midnight(tmp_path):
    manager = EntitlementManager(tmp_path / "entitlements.db", default_daily_credits=5)
    ok, charged = manager.spend(
        "user-1",
        2,
        tool_name="get_fx_rate",
        subject="EURUSD",
        usage_date="2026-07-29",
        charge_id="cross-midnight-charge",
    )

    refunded = manager.refund(
        "user-1",
        2,
        tool_name="get_fx_rate",
        subject="EURUSD",
        usage_date="2026-07-30",
        charge_id="cross-midnight-charge",
    )

    assert ok is True
    assert charged.credits_remaining == 3
    assert refunded.date == "2026-07-29"
    assert refunded.credits_spent == 0
    assert refunded.credits_remaining == 5
    assert manager.status("user-1", usage_date="2026-07-30").credits_remaining == 5


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
    monkeypatch.setenv("ANTHROPIC_ENTITLEMENT_PENDING_LEASE_SECONDS", "600")
    monkeypatch.setenv("CURSOR_ENTITLEMENT_DB_PATH", str(cursor_db))
    monkeypatch.setenv("CURSOR_DAILY_CREDITS", "25")

    anthropic = connector_entitlement_manager("ANTHROPIC")
    cursor = connector_entitlement_manager("CURSOR")

    assert anthropic.db_path == str(anthropic_db)
    assert anthropic.default_daily_credits == 75
    assert anthropic.pending_charge_lease_seconds == 600
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


def test_schema_status_accepts_entitlement_schema(tmp_path):
    manager = EntitlementManager(tmp_path / "entitlements.db")

    status = manager.schema_status()

    assert status["ready"] is True
    assert status["schema_version"] == ENTITLEMENT_SCHEMA_VERSION
    assert status["integrity"] == "ok"
    assert status["missing_tables"] == []
    assert status["missing_columns"] == {}
    assert status["incompatible_column_types"] == {}
    assert status["initialization_blocker"] is None
    assert status["invalid_charge_states"] == []
    assert status["charge_states"] == {}
    assert status["pending_recovery"] == {
        "lease_seconds": DEFAULT_PENDING_CHARGE_LEASE_SECONDS,
        "batch_limit": PENDING_RECOVERY_BATCH_LIMIT,
        "pending_charges": 0,
        "stale_pending_charges": 0,
        "last_recovery": {
            "recovered_charges": 0,
            "recovered_credits": 0,
            "remaining_stale_charges": 0,
        },
    }


def test_schema_status_rejects_credit_manager_charge_table(tmp_path):
    db_path = tmp_path / "credits.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE credit_charges (
                charge_id TEXT PRIMARY KEY,
                address TEXT NOT NULL,
                credits REAL NOT NULL,
                purpose TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL,
                refunded_at TIMESTAMP
            )
            """
        )

    status = EntitlementManager(db_path).schema_status()

    assert status["ready"] is False
    assert status["integrity"] == "ok"
    assert status["initialization_blocker"] == "incompatible_existing_schema"
    assert status["missing_tables"] == ["daily_usage", "usage_events", "users"]
    assert status["missing_columns"] == {
        "credit_charges": ["amount", "delivered_at", "usage_date", "user_id"]
    }
    assert status["incompatible_column_types"] == {
        "credit_charges": {
            "created_at": {"expected": "TEXT", "actual": "TIMESTAMP"},
            "refunded_at": {"expected": "TEXT", "actual": "TIMESTAMP"},
        }
    }
    with sqlite3.connect(db_path) as conn:
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert tables == {"credit_charges"}


def test_schema_status_rejects_wrong_required_column_type(tmp_path):
    db_path = tmp_path / "wrong-type.db"
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE credit_charges (
                charge_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                usage_date TEXT NOT NULL,
                amount REAL NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                delivered_at TEXT,
                refunded_at TEXT
            )
            """
        )

    status = EntitlementManager(db_path).schema_status()

    assert status["ready"] is False
    assert status["initialization_blocker"] == "incompatible_existing_schema"
    assert status["missing_tables"] == ["daily_usage", "usage_events", "users"]
    assert status["missing_columns"] == {}
    assert status["incompatible_column_types"] == {
        "credit_charges": {
            "amount": {"expected": "INTEGER", "actual": "REAL"},
        }
    }


def test_spend_is_pending_until_delivery_is_finalized(tmp_path):
    manager = EntitlementManager(tmp_path / "entitlements.db", default_daily_credits=5)

    ok, pending = manager.spend(
        "user-1",
        2,
        tool_name="get_vwap",
        subject="BTCUSD",
        usage_date="2026-07-29",
        charge_id="delivery-1",
    )
    with sqlite3.connect(manager.db_path) as conn:
        pending_state = conn.execute(
            "SELECT state, delivered_at FROM credit_charges WHERE charge_id = ?",
            ("delivery-1",),
        ).fetchone()

    delivered = manager.finalize_delivery("user-1", 2, charge_id="delivery-1")
    duplicate = manager.finalize_delivery("user-1", 2, charge_id="delivery-1")
    after_refund_attempt = manager.refund(
        "user-1",
        2,
        tool_name="get_vwap",
        subject="BTCUSD",
        charge_id="delivery-1",
    )
    with sqlite3.connect(manager.db_path) as conn:
        delivered_state = conn.execute(
            "SELECT state, delivered_at FROM credit_charges WHERE charge_id = ?",
            ("delivery-1",),
        ).fetchone()

    assert ok is True
    assert pending.credits_remaining == 3
    assert pending_state == ("pending", None)
    assert delivered is not None
    assert delivered.credits_remaining == 3
    assert duplicate is None
    assert after_refund_attempt.credits_remaining == 3
    assert delivered_state[0] == "delivered"
    assert delivered_state[1]


def test_legacy_spent_charge_migrates_to_delivered(tmp_path):
    db_path = tmp_path / "legacy.db"
    created_at = datetime(2026, 7, 29, 12, 0, tzinfo=UTC).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE users (
                user_id TEXT PRIMARY KEY,
                email TEXT,
                daily_limit INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE daily_usage (
                user_id TEXT NOT NULL,
                usage_date TEXT NOT NULL,
                credits_spent INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (user_id, usage_date),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );
            CREATE TABLE usage_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                usage_date TEXT NOT NULL,
                tool_name TEXT NOT NULL,
                subject TEXT NOT NULL DEFAULT '',
                credits_delta INTEGER NOT NULL,
                credits_remaining INTEGER NOT NULL,
                outcome TEXT NOT NULL,
                created_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );
            CREATE TABLE credit_charges (
                charge_id TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                usage_date TEXT NOT NULL,
                amount INTEGER NOT NULL,
                state TEXT NOT NULL,
                created_at TEXT NOT NULL,
                refunded_at TEXT,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            );
            """
        )
        conn.execute(
            "INSERT INTO users VALUES (?, ?, ?, ?, ?, ?)",
            ("user-1", None, 5, "active", created_at, created_at),
        )
        conn.execute(
            "INSERT INTO daily_usage VALUES (?, ?, ?, ?)",
            ("user-1", "2026-07-29", 2, created_at),
        )
        conn.execute(
            "INSERT INTO credit_charges VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "legacy-spent",
                "user-1",
                "2026-07-29",
                2,
                "spent",
                created_at,
                None,
            ),
        )

    reopened = EntitlementManager(db_path, default_daily_credits=5)
    with sqlite3.connect(db_path) as conn:
        state = conn.execute(
            "SELECT state, delivered_at FROM credit_charges WHERE charge_id = ?",
            ("legacy-spent",),
        ).fetchone()
    refund_attempt = reopened.refund(
        "user-1",
        2,
        tool_name="get_vwap",
        subject="BTCUSD",
        charge_id="legacy-spent",
    )

    assert state[0] == "delivered"
    assert state[1]
    assert refund_attempt.credits_remaining == 3


def test_stale_pending_recovery_is_bounded_and_preserves_active_charge(tmp_path):
    db_path = tmp_path / "crash-recovery.db"
    now = datetime(2026, 7, 30, 0, 5, tzinfo=UTC)
    manager = EntitlementManager(
        db_path,
        default_daily_credits=3,
        pending_charge_lease_seconds=MIN_PENDING_CHARGE_LEASE_SECONDS,
    )
    for charge_id in ("crashed-1", "crashed-2", "active-1"):
        ok, _ = manager.spend(
            "user-1",
            1,
            tool_name="get_vwap",
            subject="BTCUSD",
            usage_date="2026-07-29",
            charge_id=charge_id,
        )
        assert ok is True
    with sqlite3.connect(db_path) as conn:
        conn.executemany(
            "UPDATE credit_charges SET created_at = ? WHERE charge_id = ?",
            [
                ((now - timedelta(minutes=20)).isoformat(), "crashed-1"),
                ((now - timedelta(minutes=15)).isoformat(), "crashed-2"),
                ((now - timedelta(minutes=1)).isoformat(), "active-1"),
            ],
        )

    del manager  # Simulate process loss after the pending reservations committed.
    recovered = EntitlementManager(
        db_path,
        default_daily_credits=3,
        pending_charge_lease_seconds=MIN_PENDING_CHARGE_LEASE_SECONDS,
    )
    first = recovered.recover_stale_pending(now=now, limit=1)
    second = recovered.recover_stale_pending(now=now, limit=1)
    duplicate = recovered.recover_stale_pending(now=now, limit=1)
    with sqlite3.connect(db_path) as conn:
        states = dict(conn.execute("SELECT charge_id, state FROM credit_charges").fetchall())
        prior_day_spent = conn.execute(
            """
            SELECT credits_spent FROM daily_usage
            WHERE user_id = ? AND usage_date = ?
            """,
            ("user-1", "2026-07-29"),
        ).fetchone()[0]

    assert first == {
        "recovered_charges": 1,
        "recovered_credits": 1,
        "remaining_stale_charges": 1,
    }
    assert second == {
        "recovered_charges": 1,
        "recovered_credits": 1,
        "remaining_stale_charges": 0,
    }
    assert duplicate == {
        "recovered_charges": 0,
        "recovered_credits": 0,
        "remaining_stale_charges": 0,
    }
    assert states == {
        "crashed-1": "refunded",
        "crashed-2": "refunded",
        "active-1": "pending",
    }
    assert prior_day_spent == 1


def test_pending_recovery_lease_rejects_unsafe_short_configuration(tmp_path):
    with pytest.raises(ValueError, match="at least"):
        EntitlementManager(
            tmp_path / "unsafe.db",
            pending_charge_lease_seconds=MIN_PENDING_CHARGE_LEASE_SECONDS - 1,
        )
