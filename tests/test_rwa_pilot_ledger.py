"""Acceptance tests for the authoritative RWA growth-pilot ledger."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
import sqlite3
import sys

import pytest

from scripts import run_rwa_growth_pilot
from scripts.run_rwa_growth_pilot import (
    PILOT_FEEDS,
    evaluate_store,
    import_legacy_history,
    persist_capture,
)
from src import rwa_store as rwa_store_module
from src.rwa_store import RWAObservationStore, RWA_STORE_SCHEMA_VERSION


def _successful_capture(feed: dict, checked_at: datetime) -> dict:
    return {
        **feed,
        "started_at": (checked_at - timedelta(seconds=2)).isoformat(),
        "checked_at": checked_at.isoformat(),
        "status": "ok",
        "checks": {
            "freshness_seconds": 1.0,
            "freshness_limit_seconds": feed["freshness_limit_seconds"],
            "freshness_pass": True,
            "bidask_sanity_pass": True,
        },
        "raw_observation": {
            "symbol": feed["symbol"],
            "venue": feed["venue"],
            "source_type": "venue_api_order_book",
            "timestamp": checked_at.isoformat(),
            "bid": 99.0,
            "ask": 101.0,
        },
        "production_promoted": False,
    }


def _failed_capture(feed: dict, checked_at: datetime) -> dict:
    return {
        **feed,
        "started_at": (checked_at - timedelta(seconds=3)).isoformat(),
        "checked_at": checked_at.isoformat(),
        "status": "error",
        "error_type": "TimeoutError",
        "message": "Upstream adapter timed out.",
        "checks": {"freshness_pass": False, "bidask_sanity_pass": False},
        "production_promoted": False,
    }


def test_pilot_ledger_atomically_records_success_and_failure(tmp_path):
    store = RWAObservationStore(str(tmp_path / "rwa.db"))
    checked_at = datetime.now(UTC) - timedelta(seconds=10)

    stored = store.store_pilot_outcomes(
        [
            _successful_capture(PILOT_FEEDS[0], checked_at),
            _failed_capture(PILOT_FEEDS[1], checked_at + timedelta(seconds=1)),
        ]
    )
    history = store.list_pilot_outcomes(
        pilot_ids=[PILOT_FEEDS[0]["pilot_id"], PILOT_FEEDS[1]["pilot_id"]],
        include_evidence=True,
    )

    assert [row["inserted"] for row in stored] == [True, True]
    assert {row["status"] for row in history} == {"ok", "error"}
    successful = next(row for row in history if row["status"] == "ok")
    failed = next(row for row in history if row["status"] == "error")
    assert successful["raw_observation"]["bid"] == 99.0
    assert successful["evidence_hash"].startswith("sha256:")
    assert failed["error_type"] == "TimeoutError"
    assert failed["message"] == "Upstream adapter timed out."
    assert failed["raw_observation"] == {}
    assert all(row["production_promoted"] is False for row in history)
    assert all(row["ingestion_source"] == "growth_pilot" for row in history)


def test_pilot_ledger_is_idempotent_and_rejects_conflicting_evidence(tmp_path):
    store = RWAObservationStore(str(tmp_path / "rwa.db"))
    checked_at = datetime.now(UTC) - timedelta(seconds=5)
    capture = _successful_capture(PILOT_FEEDS[0], checked_at)

    first = store.store_pilot_outcomes([capture])[0]
    duplicate = store.store_pilot_outcomes([capture])[0]
    conflicting = deepcopy(capture)
    conflicting["raw_observation"]["ask"] = 999.0

    assert first["inserted"] is True
    assert duplicate["inserted"] is False
    assert duplicate["outcome_id"] == first["outcome_id"]
    with pytest.raises(ValueError, match="conflicts with existing ledger"):
        store.store_pilot_outcomes([conflicting])
    assert len(store.list_pilot_outcomes()) == 1


def test_pilot_ledger_rejects_future_source_timestamp(tmp_path):
    store = RWAObservationStore(str(tmp_path / "rwa.db"))
    checked_at = datetime.now(UTC) - timedelta(seconds=1)
    capture = _successful_capture(PILOT_FEEDS[0], checked_at)
    capture["raw_observation"]["timestamp"] = (
        checked_at + timedelta(hours=24)
    ).isoformat()

    with pytest.raises(ValueError, match="source timestamp"):
        store.store_pilot_outcomes([capture])

    assert store.list_pilot_outcomes() == []


def test_pilot_ledger_derives_bidask_sanity_from_raw_evidence(tmp_path):
    store = RWAObservationStore(str(tmp_path / "rwa.db"))
    capture = _successful_capture(
        PILOT_FEEDS[0],
        datetime.now(UTC) - timedelta(seconds=1),
    )
    capture["raw_observation"].update({"bid": 200.0, "ask": 100.0})
    capture["checks"]["bidask_sanity_pass"] = True

    with pytest.raises(ValueError, match="bidask_sanity_pass conflicts"):
        store.store_pilot_outcomes([capture])

    assert store.list_pilot_outcomes() == []


def test_pilot_ledger_rejects_any_automatic_promotion_claim(tmp_path):
    store = RWAObservationStore(str(tmp_path / "rwa.db"))
    capture = _successful_capture(
        PILOT_FEEDS[0],
        datetime.now(UTC) - timedelta(seconds=5),
    )
    capture["production_promoted"] = True

    with pytest.raises(ValueError, match="cannot mark a feed as production promoted"):
        store.store_pilot_outcomes([capture])

    connection = sqlite3.connect(store.db_path)
    try:
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'rwa_pilot_outcomes'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert "CHECK(production_promoted = 0)" in sql


def test_pilot_freshness_fails_closed_for_stale_failed_and_missing_feeds(tmp_path):
    store = RWAObservationStore(str(tmp_path / "rwa.db"))
    now = datetime.now(UTC)
    store.store_pilot_outcomes(
        [
            _successful_capture(PILOT_FEEDS[0], now - timedelta(minutes=90)),
            _failed_capture(PILOT_FEEDS[1], now - timedelta(minutes=5)),
        ]
    )

    freshness = store.pilot_freshness(
        [feed["pilot_id"] for feed in PILOT_FEEDS],
        stale_after_seconds=3_900,
        now=now,
    )
    by_id = {row["pilot_id"]: row for row in freshness["feeds"]}

    assert freshness["status"] == "stale"
    assert freshness["ready"] is False
    assert by_id[PILOT_FEEDS[0]["pilot_id"]]["stale"] is True
    assert by_id[PILOT_FEEDS[1]["pilot_id"]]["last_status"] == "error"
    assert by_id[PILOT_FEEDS[1]["pilot_id"]]["healthy"] is False
    assert by_id[PILOT_FEEDS[2]["pilot_id"]]["last_status"] == "missing"
    assert by_id[PILOT_FEEDS[2]["pilot_id"]]["stale"] is True


def test_persist_capture_and_status_are_derived_from_same_ledger(tmp_path):
    store = RWAObservationStore(str(tmp_path / "rwa.db"))
    now = datetime.now(UTC)
    captures = [
        _successful_capture(PILOT_FEEDS[0], now - timedelta(seconds=10)),
        _failed_capture(PILOT_FEEDS[1], now - timedelta(seconds=9)),
        _successful_capture(PILOT_FEEDS[2], now - timedelta(seconds=8)),
    ]

    report = persist_capture(store, captures, now=now)
    independently_derived = evaluate_store(store, now=now)

    assert report["ledger"]["authoritative"] is True
    assert report["ledger"]["history_rows_evaluated"] == 3
    assert report["current_capture"] == {
        "attempted": 3,
        "succeeded": 2,
        "failed": 1,
        "inserted": 3,
        "rows": report["current_capture"]["rows"],
    }
    assert independently_derived["freshness"] == report["freshness"]
    assert report["promotion_ready"] is False
    assert report["production_promoted_feed_count"] == 0
    assert report["policy"]["automatic_promotion"] is False


def test_healthy_v2_store_migrates_additively_to_v3_without_losing_observations(tmp_path):
    db_path = tmp_path / "rwa.db"
    original = RWAObservationStore(str(db_path))
    payload_time = datetime.now(UTC) - timedelta(minutes=1)
    original.store_observation(
        {
            "raw_payload": {
                "symbol": "EUR/USD",
                "venue": "gains",
                "timestamp": payload_time.isoformat(),
            },
            "normalized_observation": {
                "symbol": "EUR/USD",
                "venue": "gains",
                "timestamp": payload_time.isoformat(),
            },
        }
    )
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            "UPDATE rwa_store_metadata SET value = '2' WHERE key = 'schema_version'"
        )
        connection.execute("DROP TABLE rwa_pilot_outcomes")
        connection.commit()
    finally:
        connection.close()

    migrated = RWAObservationStore(str(db_path))

    assert migrated.schema_status()["ready"] is True
    assert migrated.schema_status()["schema_version"] == RWA_STORE_SCHEMA_VERSION == 3
    assert migrated.summary()["total_observations"] == 1
    assert migrated.list_pilot_outcomes() == []


def test_legacy_jsonl_migration_is_idempotent_counted_and_redacted(tmp_path):
    store = RWAObservationStore(str(tmp_path / "rwa.db"))
    checked_at = datetime.now(UTC) - timedelta(minutes=1)
    success = _successful_capture(PILOT_FEEDS[0], checked_at)
    failure = _failed_capture(PILOT_FEEDS[1], checked_at + timedelta(seconds=1))
    failure["message"] = "request failed at https://upstream.test/?api_key=do-not-store"
    promoted = _successful_capture(PILOT_FEEDS[2], checked_at + timedelta(seconds=2))
    promoted["production_promoted"] = True
    history_path = tmp_path / "legacy.jsonl"
    history_path.write_text(
        "\n".join(
            [
                json.dumps(success),
                json.dumps(failure),
                json.dumps(success),
                "{malformed-json",
                json.dumps(promoted),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    first = import_legacy_history(store, history_path)
    second = import_legacy_history(store, history_path)

    assert first["attempted"] == 5
    assert first["imported"] == 2
    assert first["duplicates"] == 1
    assert first["rejected"] == 2
    assert first["complete"] is False
    assert first["automatic_promotion"] is False
    assert first["production_promoted_feed_count"] == 0
    assert second["imported"] == 0
    assert second["duplicates"] == 3
    assert second["rejected"] == 2
    stored_failure = next(
        row for row in store.list_pilot_outcomes() if row["status"] == "error"
    )
    assert stored_failure["message"] == "Upstream adapter timed out."
    assert "api_key" not in json.dumps(store.list_pilot_outcomes())


def test_import_only_cli_never_runs_a_live_capture(tmp_path, monkeypatch, capsys):
    checked_at = datetime.now(UTC) - timedelta(minutes=1)
    history_path = tmp_path / "legacy.jsonl"
    history_path.write_text(
        json.dumps(_successful_capture(PILOT_FEEDS[0], checked_at)) + "\n",
        encoding="utf-8",
    )

    async def forbidden_live_capture(_timeout_seconds):
        raise AssertionError("import-only must not perform a live capture")

    monkeypatch.setattr(run_rwa_growth_pilot, "_run", forbidden_live_capture)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_rwa_growth_pilot.py",
            "--db-path",
            str(tmp_path / "rwa.db"),
            "--legacy-history",
            str(history_path),
            "--import-only",
        ],
    )

    run_rwa_growth_pilot.main()

    output = json.loads(capsys.readouterr().out)
    assert output["imported"] == 1
    assert output["rejected"] == 0
    assert output["automatic_promotion"] is False


def test_pilot_ledger_retention_is_bounded_in_the_write_transaction(
    tmp_path, monkeypatch
):
    store = RWAObservationStore(str(tmp_path / "rwa.db"))
    monkeypatch.setattr(rwa_store_module, "RWA_PILOT_OUTCOME_HISTORY_MAX", 3)
    feed = PILOT_FEEDS[0]

    for offset in range(4):
        checked_at = datetime.now(UTC) - timedelta(minutes=offset)
        store.store_pilot_outcomes([_successful_capture(feed, checked_at)])

    rows = store.list_pilot_outcomes(pilot_ids=[feed["pilot_id"]], limit=10)

    assert len(rows) == 3
    assert rows[0]["checked_at"] > rows[-1]["checked_at"]
