"""Integrity tests for the bounded RWA evidence store."""

from __future__ import annotations

from datetime import datetime, timezone
from copy import deepcopy
import os
import sqlite3

import pytest

from src.rwa_store import RWAObservationStore, RWA_STORE_SCHEMA_VERSION
from src.rwa_security import database_paths_collide


def _payload(*, value: float = 1.14, idempotency_key: str | None = None) -> dict:
    timestamp = datetime.now(timezone.utc).isoformat()
    observation = {
        "symbol": "EUR/USD",
        "venue": "gains",
        "asset_class": "fx",
        "source_type": "price_stream_no_book",
        "timestamp": timestamp,
        "value": value,
    }
    payload = {
        "raw_payload": dict(observation),
        "normalized_observation": dict(observation),
        "metadata": {"product": "store_test"},
    }
    if idempotency_key:
        payload["idempotency_key"] = idempotency_key
    return payload


def test_store_uses_schema_v2_and_server_owned_ingestion_time(tmp_path):
    store = RWAObservationStore(str(tmp_path / "rwa.db"))
    payload = _payload()
    payload["created_at"] = "1999-01-01T00:00:00+00:00"

    record = store.store_observation(payload)

    status = store.schema_status()
    assert status["ready"] is True
    assert status["schema_version"] == RWA_STORE_SCHEMA_VERSION
    assert status["integrity"] == "ok"
    assert status["migration_required"] is False
    assert status["invalid_or_legacy_rows"] == 0
    assert record["created_at"] != payload["created_at"]
    assert record["created_at"] == record["ingested_at"]
    assert record["observed_at"] == payload["normalized_observation"]["timestamp"]


def test_duplicate_evidence_is_idempotent_and_not_replaced(tmp_path):
    store = RWAObservationStore(str(tmp_path / "rwa.db"))
    payload = _payload(idempotency_key="same-source-record-0001")

    first = store.store_observation(payload)
    second = store.store_observation(payload)

    assert first["inserted"] is True
    assert second["inserted"] is False
    assert second["observation_id"] == first["observation_id"]
    assert second["created_at"] == first["created_at"]
    assert second["symbol"] == first["symbol"]
    assert store.summary()["total_observations"] == 1
    connection = sqlite3.connect(store.db_path)
    try:
        stored_key = connection.execute(
            "SELECT idempotency_key FROM rwa_observations"
        ).fetchone()[0]
    finally:
        connection.close()
    assert stored_key.startswith("sha256:")
    assert "same-source-record-0001" not in stored_key


def test_idempotency_conflict_is_rejected_without_overwrite(tmp_path):
    store = RWAObservationStore(str(tmp_path / "rwa.db"))
    store.store_observation(_payload(value=1.14, idempotency_key="vendor-record-0000001"))

    with pytest.raises(ValueError, match="idempotency key conflicts"):
        store.store_observation(
            _payload(value=9.99, idempotency_key="vendor-record-0000001")
        )

    assert store.summary()["total_observations"] == 1


def test_idempotency_conflict_never_returns_a_phantom_identity(tmp_path):
    store = RWAObservationStore(str(tmp_path / "rwa.db"))
    original = _payload(idempotency_key="fixed-key-00000001")
    first = store.store_observation(original)
    conflicting = deepcopy(original)
    conflicting["symbol"] = "GBP/USD"
    conflicting["venue"] = "different"
    conflicting["timestamp"] = "2026-01-01T00:00:00+00:00"

    with pytest.raises(ValueError, match="conflicts"):
        store.store_observation(conflicting)

    rows = store.list_observations()
    assert len(rows) == 1
    assert rows[0]["observation_id"] == first["observation_id"]
    assert rows[0]["symbol"] == "EUR/USD"
    assert rows[0]["venue"] == "gains"


def test_first_insert_rejects_contradictory_canonical_identity(tmp_path):
    store = RWAObservationStore(str(tmp_path / "rwa.db"))
    payload = _payload()
    payload["symbol"] = "GBP/USD"
    payload["venue"] = "different"

    with pytest.raises(ValueError, match="conflicts with normalized evidence"):
        store.store_observation(payload)

    assert store.summary()["total_observations"] == 0


def test_identity_fields_are_rejected_instead_of_silently_truncated(tmp_path):
    store = RWAObservationStore(str(tmp_path / "rwa.db"))
    payload = _payload()
    payload["normalized_observation"]["asset_class"] = "x" * 65

    with pytest.raises(ValueError, match="asset_class exceeds"):
        store.store_observation(payload)

    assert store.summary()["total_observations"] == 0


def test_ingestion_source_is_server_owned(tmp_path):
    store = RWAObservationStore(str(tmp_path / "rwa.db"))
    payload = _payload()
    payload["metadata"]["product"] = "caller_controlled_label"

    store.store_observation(payload, ingestion_source="operator_api")

    connection = sqlite3.connect(store.db_path)
    try:
        ingestion_source, metadata_json = connection.execute(
            "SELECT ingestion_source, metadata_json FROM rwa_observations"
        ).fetchone()
    finally:
        connection.close()
    assert ingestion_source == "operator_api"
    assert "caller_controlled_label" in metadata_json


def test_batch_validation_rolls_back_every_row(tmp_path):
    store = RWAObservationStore(str(tmp_path / "rwa.db"))
    valid = _payload(value=1.14)
    invalid = _payload(value=1.15)
    invalid["metadata"]["private_key"] = "never-store-this"

    with pytest.raises(ValueError, match="sensitive field"):
        store.store_observations_batch([valid, invalid])

    assert store.summary()["total_observations"] == 0


def test_store_rejects_oversized_evidence_component(tmp_path):
    store = RWAObservationStore(str(tmp_path / "rwa.db"))
    payload = _payload()
    payload["raw_payload"]["blob"] = ["x" * 8_192 for _ in range(9)]

    with pytest.raises(ValueError, match="raw_payload exceeds"):
        store.store_observation(payload)

    assert store.summary()["total_observations"] == 0


def test_store_upgrades_legacy_schema_without_destroying_rows(tmp_path):
    db_path = tmp_path / "legacy.db"
    connection = sqlite3.connect(db_path)
    try:
        connection.execute(
            """
            CREATE TABLE rwa_observations (
                observation_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                symbol TEXT NOT NULL,
                venue TEXT NOT NULL,
                asset_class TEXT DEFAULT '',
                source_type TEXT DEFAULT '',
                raw_payload_hash TEXT NOT NULL,
                normalized_hash TEXT NOT NULL,
                raw_payload_json TEXT NOT NULL,
                normalized_json TEXT NOT NULL,
                realtime_quality_json TEXT DEFAULT '{}',
                blocksize_benchmark_json TEXT DEFAULT '{}',
                promotion_json TEXT DEFAULT '{}',
                metadata_json TEXT DEFAULT '{}'
            )
            """
        )
        connection.execute(
            """
            INSERT INTO rwa_observations (
                observation_id, created_at, symbol, venue, raw_payload_hash,
                normalized_hash, raw_payload_json, normalized_json
            ) VALUES ('legacy-1', '2026-01-01T00:00:00Z', 'EUR/USD', 'gains',
                      'sha256:raw', 'sha256:normalized', ?, '{}')
            """
            ,
            ("{\"blob\":\"" + ("x" * 500_000) + "\"}",),
        )
        connection.execute(
            "UPDATE rwa_observations SET metadata_json = ? WHERE observation_id = 'legacy-1'",
            ('{"api_key":"plaintext"}',),
        )
        connection.commit()
    finally:
        connection.close()

    store = RWAObservationStore(str(db_path))

    status = store.schema_status()
    assert status["ready"] is False
    assert status["schema_version"] == 1
    assert status["migration_required"] is True
    assert status["invalid_or_legacy_rows"] == 1
    assert store.summary()["total_observations"] == 1
    with pytest.raises(ValueError, match="migration is required"):
        store.list_observations()


def test_database_collision_detects_hard_links(tmp_path):
    first = tmp_path / "usage.db"
    second = tmp_path / "rwa.db"
    first.write_bytes(b"sqlite-placeholder")
    os.link(first, second)

    assert database_paths_collide(first, second) is True


def test_summary_caps_high_cardinality_venue_output(tmp_path):
    store = RWAObservationStore(str(tmp_path / "rwa.db"))
    for index in range(51):
        payload = _payload(value=1.0 + index)
        venue = f"venue-{index:02d}"
        payload["raw_payload"]["venue"] = venue
        payload["normalized_observation"]["venue"] = venue
        store.store_observation(payload)

    summary = store.summary()

    assert len(summary["by_venue"]) == 50
    assert summary["venues_truncated"] is True
