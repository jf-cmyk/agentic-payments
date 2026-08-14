from __future__ import annotations

import importlib.util
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from src.entitlement_manager import (
    DEFAULT_PENDING_CHARGE_LEASE_SECONDS,
    ENTITLEMENT_SCHEMA_VERSION,
    MIN_PENDING_CHARGE_LEASE_SECONDS,
    PENDING_RECOVERY_BATCH_LIMIT,
    EntitlementManager,
    connector_entitlement_manager,
)


ROOT = Path(__file__).resolve().parents[1]
V062_FIXTURE = ROOT / "tests" / "fixtures" / "entitlement_manager_v062.py"


def _replace_identity_aliases_table(
    db_path: Path,
    create_sql: str,
    rows: list[tuple[str, str]] | None = None,
) -> None:
    with sqlite3.connect(db_path) as conn:
        conn.execute("DROP TABLE identity_aliases")
        conn.execute(create_sql)
        if rows:
            now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC).isoformat()
            conn.executemany(
                "INSERT INTO identity_aliases VALUES (?, ?, ?, ?)",
                [(ledger_subject, user_id, now, now) for ledger_subject, user_id in rows],
            )


def _load_v062_entitlement_manager():
    module_name = "_blocksize_v062_entitlement_manager"
    spec = importlib.util.spec_from_file_location(module_name, V062_FIXTURE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module.EntitlementManager


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
    assert status["identity_aliases"]["ready"] is True
    assert status["identity_aliases"]["primary_key_columns"] == ["ledger_subject"]
    assert status["identity_aliases"]["primary_key_valid"] is True
    assert len(status["identity_aliases"]["unique_user_id_indexes"]) == 1
    assert status["identity_aliases"]["unique_user_id_valid"] is True
    assert status["identity_aliases"]["foreign_key_valid"] is True
    assert status["identity_aliases"]["empty_bindings"] == 0
    assert status["identity_aliases"]["duplicate_ledger_subjects"] == 0
    assert status["identity_aliases"]["duplicate_user_ids"] == 0
    assert status["identity_aliases"]["orphan_bindings"] == 0
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


def test_scoped_identity_preserves_v062_balance_and_rollback_visibility(tmp_path):
    db_path = tmp_path / "legacy-v062.db"
    created_at = datetime(2026, 8, 13, 12, 0, tzinfo=UTC).isoformat()
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
            """
        )
        conn.execute(
            "INSERT INTO users VALUES (?, ?, ?, ?, ?, ?)",
            ("legacy-user", "legacy@example.com", 5, "active", created_at, created_at),
        )
        conn.execute(
            "INSERT INTO daily_usage VALUES (?, ?, ?, ?)",
            ("legacy-user", "2026-08-13", 2, created_at),
        )

    candidate = EntitlementManager(db_path, default_daily_credits=50)
    canonical = candidate.bind_identity(
        "anthropic:issuer-audience-scope:legacy-user",
        "legacy-user",
        email="legacy@example.com",
    )
    ok, charged = candidate.spend(
        canonical,
        1,
        tool_name="get_vwap",
        subject="BTCUSD",
        usage_date="2026-08-13",
        charge_id="candidate-charge",
    )
    delivered = candidate.finalize_delivery(
        canonical,
        1,
        charge_id="candidate-charge",
    )

    assert canonical == "legacy-user"
    assert ok is True
    assert charged.daily_limit == 5
    assert charged.credits_spent == 3
    assert delivered is not None
    assert delivered.credits_remaining == 2

    # v0.6.2 reads and writes the raw user_id directly. Its view must include
    # candidate charges, and its writes must remain visible after re-upgrade.
    with sqlite3.connect(db_path) as rollback_conn:
        rollback_balance = rollback_conn.execute(
            """
            SELECT u.daily_limit - COALESCE(SUM(du.credits_spent), 0)
            FROM users u
            LEFT JOIN daily_usage du ON du.user_id = u.user_id
            WHERE u.user_id = ?
            GROUP BY u.user_id, u.daily_limit
            """,
            ("legacy-user",),
        ).fetchone()
        assert rollback_balance == (2,)
        rollback_conn.execute(
            """
            UPDATE daily_usage
            SET credits_spent = credits_spent + 1, updated_at = ?
            WHERE user_id = ? AND usage_date = ?
            """,
            (created_at, "legacy-user", "2026-08-13"),
        )

    reupgraded = EntitlementManager(db_path, default_daily_credits=50)
    assert (
        reupgraded.bind_identity(
            "anthropic:issuer-audience-scope:legacy-user",
            "legacy-user",
        )
        == "legacy-user"
    )
    assert reupgraded.status("legacy-user").credits_remaining == 1
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT ledger_subject, user_id FROM identity_aliases"
        ).fetchall() == [
            ("anthropic:issuer-audience-scope:legacy-user", "legacy-user")
        ]
        assert conn.execute("SELECT user_id FROM users").fetchall() == [("legacy-user",)]
        assert conn.execute(
            "SELECT user_id, state FROM credit_charges WHERE charge_id = ?",
            ("candidate-charge",),
        ).fetchone() == ("legacy-user", "delivered")


def test_exact_v062_manager_reads_candidate_charges_and_writes_visible_usage(tmp_path):
    legacy_manager_class = _load_v062_entitlement_manager()
    db_path = tmp_path / "actual-v062.db"
    legacy_user_id = " legacy-user "
    scoped_subject = "\tanthropic:issuer-audience-scope: legacy-user \n"
    legacy = legacy_manager_class(db_path, default_daily_credits=5)
    legacy_ok, legacy_charge = legacy.spend(
        legacy_user_id,
        2,
        tool_name="get_vwap",
        subject="ETHUSD",
        usage_date="2026-08-13",
    )
    assert legacy_ok is True
    assert legacy_charge.credits_remaining == 3

    candidate = EntitlementManager(db_path, default_daily_credits=50)
    canonical = candidate.bind_identity(
        scoped_subject,
        legacy_user_id,
    )
    assert canonical == legacy_user_id
    candidate_ok, _ = candidate.spend(
        canonical,
        1,
        tool_name="get_vwap",
        subject="BTCUSD",
        usage_date="2026-08-13",
        charge_id="candidate-delivery",
    )
    delivered = candidate.finalize_delivery(
        canonical,
        1,
        charge_id="candidate-delivery",
    )
    assert candidate_ok is True
    assert delivered is not None
    assert delivered.credits_remaining == 2

    rolled_back = legacy_manager_class(db_path, default_daily_credits=50)
    assert rolled_back.status(legacy_user_id).credits_remaining == 2
    rollback_ok, rollback_charge = rolled_back.spend(
        legacy_user_id,
        1,
        tool_name="get_vwap",
        subject="SOLUSD",
        usage_date="2026-08-13",
    )
    assert rollback_ok is True
    assert rollback_charge.daily_limit == 5
    assert rollback_charge.credits_remaining == 1

    reupgraded = EntitlementManager(db_path, default_daily_credits=50)
    assert reupgraded.schema_status()["ready"] is True
    assert reupgraded.bind_identity(scoped_subject, legacy_user_id) == legacy_user_id
    assert reupgraded.status(legacy_user_id).credits_remaining == 1
    with sqlite3.connect(db_path) as conn:
        assert conn.execute(
            "SELECT user_id, daily_limit FROM users"
        ).fetchall() == [(legacy_user_id, 5)]
        assert conn.execute(
            "SELECT DISTINCT user_id FROM daily_usage"
        ).fetchall() == [(legacy_user_id,)]
        assert conn.execute(
            "SELECT ledger_subject, user_id FROM identity_aliases"
        ).fetchall() == [(scoped_subject, legacy_user_id)]


def test_identity_binding_is_idempotent_and_fails_closed_on_scope_collision(tmp_path):
    manager = EntitlementManager(tmp_path / "identity-collision.db")
    first_scope = "anthropic:issuer-audience-a:shared-user"
    second_scope = "anthropic:issuer-audience-b:shared-user"

    assert manager.bind_identity(first_scope, "shared-user") == "shared-user"
    assert manager.bind_identity(first_scope, "shared-user") == "shared-user"
    with pytest.raises(sqlite3.IntegrityError, match="binding collision"):
        manager.bind_identity(second_scope, "shared-user")
    with pytest.raises(sqlite3.IntegrityError, match="binding collision"):
        manager.bind_identity(first_scope, "different-user")

    with sqlite3.connect(manager.db_path) as conn:
        assert conn.execute(
            "SELECT ledger_subject, user_id FROM identity_aliases"
        ).fetchall() == [(first_scope, "shared-user")]
        assert conn.execute("SELECT user_id FROM users").fetchall() == [("shared-user",)]


def test_preexisting_scoped_allowance_requires_manual_reconciliation(tmp_path):
    manager = EntitlementManager(tmp_path / "preexisting-scoped.db")
    scoped_subject = "openai:issuer-audience-scope:legacy-user"
    manager.status(scoped_subject, usage_date="2026-08-13")

    with pytest.raises(sqlite3.IntegrityError, match="manual reconciliation"):
        manager.bind_identity(scoped_subject, "legacy-user")

    with sqlite3.connect(manager.db_path) as conn:
        assert conn.execute("SELECT user_id FROM users").fetchall() == [(scoped_subject,)]
        assert conn.execute("SELECT * FROM identity_aliases").fetchall() == []


@pytest.mark.parametrize(
    ("create_sql", "invalid_check"),
    [
        (
            """
            CREATE TABLE identity_aliases (
                ledger_subject TEXT NOT NULL,
                user_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
            """,
            "primary_key_valid",
        ),
        (
            """
            CREATE TABLE identity_aliases (
                ledger_subject TEXT PRIMARY KEY,
                user_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE (user_id, ledger_subject),
                FOREIGN KEY (user_id) REFERENCES users(user_id)
            )
            """,
            "unique_user_id_valid",
        ),
        (
            """
            CREATE TABLE identity_aliases (
                ledger_subject TEXT PRIMARY KEY,
                user_id TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """,
            "foreign_key_valid",
        ),
    ],
)
def test_schema_status_rejects_malformed_identity_alias_constraints(
    tmp_path,
    create_sql,
    invalid_check,
):
    db_path = tmp_path / f"malformed-{invalid_check}.db"
    EntitlementManager(db_path)
    _replace_identity_aliases_table(db_path, create_sql)

    reopened = EntitlementManager(db_path)
    status = reopened.schema_status()

    assert status["ready"] is False
    assert status["initialization_blocker"] == "incompatible_existing_schema"
    assert status["identity_aliases"]["ready"] is False
    assert status["identity_aliases"][invalid_check] is False
    with pytest.raises(sqlite3.IntegrityError, match="schema is not ready"):
        reopened.bind_identity("anthropic:scope:new-user", "new-user")


@pytest.mark.parametrize(
    "binding",
    [
        ("", "raw-user"),
        ("\t\n", "raw-user"),
        ("anthropic:scope:raw-user", ""),
    ],
)
def test_schema_status_rejects_empty_identity_alias_bindings(tmp_path, binding):
    db_path = tmp_path / "empty-alias.db"
    manager = EntitlementManager(db_path)
    manager.status("raw-user")
    with sqlite3.connect(db_path) as conn:
        now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC).isoformat()
        conn.execute(
            "INSERT INTO identity_aliases VALUES (?, ?, ?, ?)",
            (*binding, now, now),
        )

    status = EntitlementManager(db_path).schema_status()

    assert status["ready"] is False
    assert status["initialization_blocker"] == "incompatible_existing_schema"
    assert status["identity_aliases"]["empty_bindings"] == 1


def test_schema_status_rejects_duplicate_identity_alias_bindings(tmp_path):
    db_path = tmp_path / "duplicate-aliases.db"
    manager = EntitlementManager(db_path)
    manager.status("raw-user-1")
    manager.status("raw-user-2")
    _replace_identity_aliases_table(
        db_path,
        """
        CREATE TABLE identity_aliases (
            ledger_subject TEXT,
            user_id TEXT,
            created_at TEXT,
            updated_at TEXT
        )
        """,
        [
            ("anthropic:scope-a", "raw-user-1"),
            ("anthropic:scope-a", "raw-user-2"),
            ("anthropic:scope-b", "raw-user-1"),
        ],
    )

    status = EntitlementManager(db_path).schema_status()

    assert status["ready"] is False
    assert status["initialization_blocker"] == "incompatible_existing_schema"
    assert status["identity_aliases"]["duplicate_ledger_subjects"] == 1
    assert status["identity_aliases"]["duplicate_user_ids"] == 1


def test_schema_status_rejects_orphan_identity_alias_binding(tmp_path):
    db_path = tmp_path / "orphan-alias.db"
    EntitlementManager(db_path)
    with sqlite3.connect(db_path) as conn:
        now = datetime(2026, 8, 13, 12, 0, tzinfo=UTC).isoformat()
        conn.execute(
            "INSERT INTO identity_aliases VALUES (?, ?, ?, ?)",
            ("openai:scope:missing-user", "missing-user", now, now),
        )

    status = EntitlementManager(db_path).schema_status()

    assert status["ready"] is False
    assert status["initialization_blocker"] == "incompatible_existing_schema"
    assert status["identity_aliases"]["orphan_bindings"] == 1


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
    assert status["missing_tables"] == [
        "daily_usage",
        "identity_aliases",
        "usage_events",
        "users",
    ]
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
    assert status["missing_tables"] == [
        "daily_usage",
        "identity_aliases",
        "usage_events",
        "users",
    ]
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
