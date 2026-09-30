"""The Claude connector keeps received data for under 30 days (29-day cutoff)."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import sqlite3
import time

import pytest

from src import agent_auth
from src import claude_data_retention as retention
from src.config import settings
from key_value.aio.stores.filetree import FileTreeV1CollectionSanitizationStrategy

from src.connector_auth import oauth_client_storage_for
from src.entitlement_manager import EntitlementManager
from src.free_tier_ledger import FreeTierLedger
from src.observability import UsageEventStore, fingerprint

# A synthetic clock late in a month, so a current-month day is older than the cutoff.
NOW = datetime(2026, 10, 31, 12, 0, tzinfo=UTC)
CUTOFF = NOW - timedelta(days=retention.RETENTION_DAYS)  # 2026-10-02T12:00Z
RECENT = (NOW - timedelta(days=1)).isoformat()
OLD = (CUTOFF - timedelta(days=1)).isoformat()


# --- entitlement store (per-connector, Claude only) --------------------------------


def _seed_entitlements(db_path: Path) -> EntitlementManager:
    manager = EntitlementManager(db_path, default_daily_credits=100)
    with sqlite3.connect(db_path) as conn:
        users = [
            ("user-active", "active@example.org", RECENT),
            ("user-idle", "idle@example.org", OLD),
            ("user-idle-pending", "pending@example.org", OLD),
            ("user-subscriber", "sub@example.org", OLD),
        ]
        conn.executemany(
            "INSERT INTO users (user_id, email, daily_limit, status, created_at, updated_at) "
            "VALUES (?, ?, 100, 'active', ?, ?)",
            [(user_id, email, OLD, updated) for user_id, email, updated in users],
        )
        conn.execute(
            "INSERT INTO allowance_overrides (user_id, allowance, reason, created_at, updated_at) "
            "VALUES ('user-subscriber', 500, 'subscriber', ?, ?)",
            (OLD, OLD),
        )
        conn.executemany(
            "INSERT INTO identity_aliases (ledger_subject, user_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?)",
            [
                ("scoped-active", "user-active", OLD, RECENT),
                ("scoped-idle", "user-idle", OLD, OLD),
            ],
        )
        conn.executemany(
            "INSERT INTO daily_usage (user_id, usage_date, credits_spent, updated_at) "
            "VALUES (?, ?, ?, ?)",
            [
                ("user-active", "2026-09-15", 7, OLD),  # past month: dropped
                ("user-active", "2026-10-01", 4, OLD),  # this month, before cutoff: folded
                ("user-active", "2026-10-20", 2, RECENT),  # recent: kept
                ("user-idle", "2026-10-01", 9, OLD),
            ],
        )
        conn.executemany(
            "INSERT INTO usage_events (user_id, usage_date, tool_name, subject, credits_delta, "
            "credits_remaining, outcome, created_at) VALUES (?, ?, 'get_vwap', 'BTC', 1, 1, 'ok', ?)",
            [
                ("user-active", "2026-10-01", OLD),
                ("user-active", "2026-10-30", RECENT),
                ("user-idle", "2026-10-01", OLD),
            ],
        )
        conn.executemany(
            "INSERT INTO credit_charges (charge_id, user_id, usage_date, amount, state, created_at) "
            "VALUES (?, ?, '2026-10-01', 1, ?, ?)",
            [
                ("old-delivered", "user-active", "delivered", OLD),
                ("new-delivered", "user-active", "delivered", RECENT),
                ("idle-delivered", "user-idle", "delivered", OLD),
                ("idle-pending", "user-idle-pending", "pending", OLD),
            ],
        )
    return manager


def _rows(db_path: Path, sql: str, params: tuple = ()) -> list[tuple]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(sql, params).fetchall()


def test_entitlement_purge_drops_old_records_and_idle_accounts(tmp_path: Path) -> None:
    db_path = tmp_path / "anthropic_entitlements.db"
    manager = _seed_entitlements(db_path)

    report = manager.purge_expired_data(CUTOFF, usage_date=NOW.date().isoformat())

    assert _rows(db_path, "SELECT user_id FROM users ORDER BY user_id") == [
        ("user-active",),
        ("user-idle-pending",),
        ("user-subscriber",),
    ]
    assert report["users_deleted"] == 1
    assert report["deleted_subjects"] == ["scoped-idle", "user-idle"]
    assert _rows(db_path, "SELECT ledger_subject FROM identity_aliases") == [("scoped-active",)]
    assert _rows(db_path, "SELECT created_at FROM usage_events") == [(RECENT,)]
    assert _rows(db_path, "SELECT charge_id FROM credit_charges ORDER BY charge_id") == [
        ("idle-pending",),
        ("new-delivered",),
    ]
    # An idle subscriber keeps the allowance override but not the email.
    assert _rows(db_path, "SELECT email FROM users WHERE user_id = 'user-subscriber'") == [(None,)]
    assert _rows(db_path, "SELECT email FROM users WHERE user_id = 'user-active'") == [
        ("active@example.org",)
    ]


def test_entitlement_purge_keeps_the_monthly_allowance_intact(tmp_path: Path) -> None:
    db_path = tmp_path / "anthropic_entitlements.db"
    manager = _seed_entitlements(db_path)

    active_rows = (
        "SELECT usage_date, credits_spent FROM daily_usage WHERE user_id = 'user-active' "
        "ORDER BY usage_date"
    )
    manager.purge_expired_data(CUTOFF, usage_date=NOW.date().isoformat())
    assert _rows(db_path, active_rows) == [("2026-10-00", 4), ("2026-10-20", 2)]

    # A second pass leaves the carry row alone instead of double counting.
    manager.purge_expired_data(CUTOFF, usage_date=NOW.date().isoformat())
    assert _rows(db_path, active_rows) == [("2026-10-00", 4), ("2026-10-20", 2)]

    # The monthly allowance reads the carry row. (status() stamps the real clock,
    # so it is checked only after the synthetic-clock passes.)
    assert manager.status("user-active", usage_date=NOW.date().isoformat()).credits_spent == 6


def test_entitlement_carry_row_ages_out_the_next_month(tmp_path: Path) -> None:
    db_path = tmp_path / "anthropic_entitlements.db"
    manager = _seed_entitlements(db_path)
    manager.purge_expired_data(CUTOFF, usage_date=NOW.date().isoformat())

    next_month = datetime(2026, 11, 3, 12, 0, tzinfo=UTC)
    manager.purge_expired_data(next_month - timedelta(days=29), usage_date="2026-11-03")

    assert _rows(
        db_path,
        "SELECT usage_date FROM daily_usage WHERE user_id = 'user-active' ORDER BY usage_date",
    ) == [("2026-10-20",)]


# --- shared free-tier ledger -------------------------------------------------------


@pytest.fixture
def ledger(tmp_path: Path, monkeypatch) -> FreeTierLedger:
    monkeypatch.setattr(settings.free_tier, "monthly_credits", 100)
    ledger = FreeTierLedger(tmp_path / "free_tier.db")
    with sqlite3.connect(ledger.db_path) as conn:
        conn.executemany(
            "INSERT INTO grants (grant_key, status, status_reason, created_at, first_call_at, "
            "updated_at) VALUES (?, ?, ?, ?, ?, ?)",
            [
                ("grant-active", "active", "", OLD, OLD, RECENT),
                ("grant-idle", "active", "", OLD, OLD, OLD),
                ("grant-suspended", "suspended", "fast_drain", OLD, OLD, OLD),
            ],
        )
        conn.executemany(
            "INSERT INTO grant_usage (grant_key, usage_date, credits_spent, updated_at) "
            "VALUES (?, ?, ?, ?)",
            [
                ("grant-active", "2026-09-15", 7, OLD),
                ("grant-active", "2026-10-01", 4, OLD),
                ("grant-active", "2026-10-20", 2, RECENT),
                ("grant-idle", "2026-10-01", 9, OLD),
                ("grant-suspended", "2026-10-01", 80, OLD),
            ],
        )
        conn.executemany(
            "INSERT INTO grant_subjects (grant_key, ledger_subject, created_at) VALUES (?, ?, ?)",
            [
                ("grant-active", "connector:aud:old-sub", OLD),
                ("grant-active", "connector:aud:new-sub", RECENT),
                ("grant-suspended", "connector:aud:bad-sub", OLD),
            ],
        )
        old_ts = (CUTOFF - timedelta(days=1)).timestamp()
        conn.executemany(
            "INSERT INTO grant_reservations (charge_id, grant_key, usage_date, credits, state, "
            "reserved_at, updated_at) VALUES (?, ?, '2026-10-01', 1, ?, ?, ?)",
            [
                ("r-delivered", "grant-active", "delivered", old_ts, OLD),
                ("r-pending", "grant-active", "pending", old_ts, OLD),
            ],
        )
    return ledger


def test_ledger_purge_keeps_pool_and_only_the_suspended_hash(ledger: FreeTierLedger) -> None:
    report = ledger.purge_expired_data(CUTOFF, usage_date=NOW.date().isoformat())

    db = Path(ledger.db_path)
    assert _rows(db, "SELECT grant_key, status FROM grants ORDER BY grant_key") == [
        ("grant-active", "active"),
        ("grant-suspended", "suspended"),
    ]
    assert report["grants_deleted"] == 1
    # The idle grant is gone; the suspended one keeps only this month's total.
    assert _rows(
        db, "SELECT grant_key, usage_date FROM grant_usage WHERE grant_key != 'grant-active'"
    ) == [("grant-suspended", "2026-10-00")]
    assert _rows(db, "SELECT grant_key, ledger_subject FROM grant_subjects") == [
        ("grant-active", "connector:aud:new-sub")
    ]
    assert _rows(db, "SELECT charge_id FROM grant_reservations") == [("r-pending",)]
    assert ledger.status("grant-active", usage_date=NOW.date().isoformat()).credits_spent == 6
    assert ledger.status("grant-suspended", usage_date=NOW.date().isoformat()).status == "suspended"


# --- observability -----------------------------------------------------------------


def test_observability_purge_is_scoped_to_claude_surfaces(tmp_path: Path) -> None:
    store = UsageEventStore(tmp_path / "usage.db")
    with sqlite3.connect(store.db_path) as conn:
        conn.executemany(
            "INSERT INTO usage_events (timestamp, event, surface) VALUES (?, ?, ?)",
            [
                (OLD, "mcp_tool_call", "claude_mcp"),
                (OLD, "http_request", "anthropic_mcp"),
                (OLD, "user_email_sent", "email"),
                (OLD, "http_request", "http_api"),
                (RECENT, "mcp_tool_call", "claude_mcp"),
            ],
        )
        conn.executemany(
            "INSERT INTO event_milestones (event, identity_hash, timestamp) VALUES (?, ?, ?)",
            [
                ("upgrade_cta_shown", "cta-hash", OLD),
                ("user_email_threshold_80_2026-09", "grant-hash", OLD),
                ("first_live_price_delivered", "active-user", OLD),
                ("first_live_price_delivered", "purged-user", OLD),
                ("upgrade_cta_shown", "cta-recent", RECENT),
            ],
        )

    store.purge_expired(
        CUTOFF,
        surfaces=retention.CLAUDE_SURFACES,
        keep_milestone_events=retention.ACCOUNT_MILESTONES,
        milestone_identity_hashes=("purged-user",),
    )

    db = Path(store.db_path)
    assert _rows(db, "SELECT surface, timestamp FROM usage_events ORDER BY surface") == [
        ("claude_mcp", RECENT),
        ("http_api", OLD),
    ]
    assert _rows(db, "SELECT event, identity_hash FROM event_milestones ORDER BY identity_hash") == [
        ("first_live_price_delivered", "active-user"),
        ("upgrade_cta_shown", "cta-recent"),
    ]


# --- OAuth token files -------------------------------------------------------------


def _age(path: Path, when: datetime) -> None:
    os.utime(path, (when.timestamp(), when.timestamp()))


def test_oauth_purge_removes_old_user_tokens_but_keeps_client_registrations(tmp_path: Path) -> None:
    now = datetime.now(UTC)
    cutoff = retention.retention_cutoff(now)
    old, recent = cutoff - timedelta(days=1), now - timedelta(hours=1)
    sanitize = FileTreeV1CollectionSanitizationStrategy(tmp_path).sanitize
    files = {}
    for collection in (*retention.OAUTH_TOKEN_COLLECTIONS, "mcp-oauth-proxy-clients"):
        directory = tmp_path / sanitize(collection)
        directory.mkdir()
        for label, when in (("old", old), ("recent", recent)):
            path = directory / f"{label}.json"
            path.write_text("{}")
            _age(path, when)
            files[(collection, label)] = path
    info = tmp_path / sanitize("mcp-upstream-tokens") / "info.json"
    info.write_text("{}")
    _age(info, old)

    removed = retention.purge_oauth_token_files(tmp_path, cutoff)

    assert removed == len(retention.OAUTH_TOKEN_COLLECTIONS)
    for (collection, label), path in files.items():
        expected = label == "recent" or collection == "mcp-oauth-proxy-clients"
        assert path.exists() is expected, (collection, label)
    assert info.exists()
    assert retention.purge_oauth_token_files(None, cutoff) == 0
    assert retention.purge_oauth_token_files(tmp_path / "missing", cutoff) == 0


def test_oauth_token_collections_match_the_encrypted_store_layout(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("ANTHROPIC_OAUTH_STORAGE_DIR", str(tmp_path))
    monkeypatch.setenv("ANTHROPIC_OAUTH_STORAGE_ENCRYPTION_KEY", "k" * 64)
    store = oauth_client_storage_for("ANTHROPIC", jwt_signing_key=None, fallback_secret=None)
    assert store is not None

    async def write_all() -> None:
        for collection in retention.OAUTH_TOKEN_COLLECTIONS:
            await store.put(key="user-token", value={"token": "t"}, collection=collection)

    asyncio.run(write_all())

    assert retention.oauth_storage_dir() == tmp_path
    sanitize = FileTreeV1CollectionSanitizationStrategy(tmp_path).sanitize
    for collection in retention.OAUTH_TOKEN_COLLECTIONS:
        assert list((tmp_path / sanitize(collection)).glob("*.json")), collection
    future = datetime.now(UTC) + timedelta(days=1)
    assert retention.purge_oauth_token_files(tmp_path, future) == len(
        retention.OAUTH_TOKEN_COLLECTIONS
    )


# --- agent auth store --------------------------------------------------------------


def test_agent_auth_audit_rows_expire_before_thirty_days(tmp_path: Path) -> None:
    assert agent_auth.AUDIT_TTL_SECONDS < 30 * 86400
    store = agent_auth.Store(str(tmp_path / "auth.db"), "s" * 64)
    now = int(time.time())
    with store.transaction() as db:
        store.put(db, "audit", "expired", {"event": "x"}, now - 1)
        store.put(db, "audit", "live", {"event": "y"}, now + 3600)

    assert store.purge_expired() == 1
    with store.transaction() as db:
        assert store.get(db, "audit", "live") == {"event": "y"}
        assert db.execute("SELECT COUNT(*) FROM records").fetchone()[0] == 1


# --- the full pass -----------------------------------------------------------------


def test_purge_claude_data_runs_every_store(tmp_path: Path, ledger: FreeTierLedger) -> None:
    manager = _seed_entitlements(tmp_path / "anthropic_entitlements.db")
    store = UsageEventStore(tmp_path / "usage.db")
    purged_hash = fingerprint("user:scoped-idle")
    kept_hash = fingerprint("user:scoped-active")
    with sqlite3.connect(store.db_path) as conn:
        conn.executemany(
            "INSERT INTO event_milestones (event, identity_hash, timestamp) VALUES (?, ?, ?)",
            [
                ("first_live_price_delivered", purged_hash, OLD),
                ("first_live_price_delivered", kept_hash, OLD),
            ],
        )

    report = retention.purge_claude_data(
        entitlements=manager,
        ledger=ledger,
        observability=store,
        agent_auth_store=None,
        oauth_dir=None,
        now=NOW,
    )

    assert report["cutoff"] == CUTOFF.isoformat()
    assert report["entitlements"]["users_deleted"] == 1
    assert "deleted_subjects" not in report["entitlements"]
    assert report["free_tier"]["grants_deleted"] == 1
    assert report["oauth_token_files"] == 0
    assert _rows(Path(store.db_path), "SELECT identity_hash FROM event_milestones") == [
        (kept_hash,)
    ]
