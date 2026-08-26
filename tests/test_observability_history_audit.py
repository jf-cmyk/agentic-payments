from __future__ import annotations

from scripts.audit_observability_history import (
    apply_history_tags,
    audit_history,
    revert_history_tags,
)
from src.observability import UsageEventStore, fingerprint


def test_history_audit_finds_only_untagged_known_fixture_events(tmp_path):
    store = UsageEventStore(tmp_path / "usage.db")
    known_hash = fingerprint("fixture-user")
    store.record(
        "first_live_price_delivered",
        surface="openai_mcp",
        wallet_hash=known_hash,
        metadata={"identity_hash": known_hash},
    )
    store.record(
        "mcp_data_delivered",
        surface="openai_mcp",
        wallet_hash=known_hash,
        metadata={"identity_hash": known_hash, "synthetic": True},
    )
    store.record(
        "mcp_data_delivered",
        surface="openai_mcp",
        wallet_hash=fingerprint("real-user"),
        metadata={"identity_hash": fingerprint("real-user")},
    )

    result = audit_history(
        tmp_path / "usage.db",
        known_test_identities=["fixture-user"],
    )

    assert result["total_events"] == 3
    assert result["already_tagged_events"] == 1
    assert result["untagged_candidate_events"] == 1
    assert result["candidates"][0]["event"] == "first_live_price_delivered"


def test_history_migration_is_backed_up_verified_and_reversible(tmp_path):
    db_path = tmp_path / "usage.db"
    store = UsageEventStore(db_path)
    known_hash = fingerprint("fixture-user")
    store.record(
        "first_live_price_delivered",
        surface="openai_mcp",
        wallet_hash=known_hash,
        metadata={"identity_hash": known_hash, "original": "value"},
    )

    applied = apply_history_tags(
        db_path,
        known_test_identities=["fixture-user"],
        backup_dir=tmp_path / "backups",
    )

    assert applied["updated_events"] == 1
    assert applied["remaining_candidates"] == 0
    assert list((tmp_path / "backups").glob("*.sqlite"))
    after = audit_history(db_path, known_test_identities=["fixture-user"])
    assert after["already_tagged_events"] == 1

    reverted = revert_history_tags(db_path, manifest_path=applied["manifest"])
    assert reverted["restored_events"] == 1
    restored = audit_history(db_path, known_test_identities=["fixture-user"])
    assert restored["untagged_candidate_events"] == 1
