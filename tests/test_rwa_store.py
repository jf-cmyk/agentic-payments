"""Integrity tests for the bounded RWA evidence store."""

from __future__ import annotations

from datetime import datetime, timezone
from copy import deepcopy
import hashlib
import json
import os
import sqlite3

import pytest

from src.rwa_store import RWAObservationStore, RWA_STORE_SCHEMA_VERSION
from src.rwa_security import database_paths_collide


_LEGACY_COLUMNS = (
    "observation_id",
    "created_at",
    "symbol",
    "venue",
    "asset_class",
    "source_type",
    "raw_payload_hash",
    "normalized_hash",
    "raw_payload_json",
    "normalized_json",
    "realtime_quality_json",
    "blocksize_benchmark_json",
    "promotion_json",
    "metadata_json",
)


def _legacy_json(value: dict) -> str:
    return json.dumps(value, default=str, sort_keys=True, separators=(",", ":"))


def _legacy_hash(value: dict) -> str:
    return f"sha256:{hashlib.sha256(_legacy_json(value).encode()).hexdigest()}"


def _create_legacy_v1_db(db_path, rows: list[tuple]) -> None:
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
        connection.execute("CREATE INDEX idx_rwa_observations_symbol ON rwa_observations(symbol)")
        connection.execute("CREATE INDEX idx_rwa_observations_venue ON rwa_observations(venue)")
        connection.execute(
            "CREATE INDEX idx_rwa_observations_created_at ON rwa_observations(created_at)"
        )
        connection.executemany(
            f"INSERT INTO rwa_observations ({', '.join(_LEGACY_COLUMNS)}) "
            f"VALUES ({', '.join('?' for _ in _LEGACY_COLUMNS)})",
            rows,
        )
        connection.commit()
    finally:
        connection.close()


def _create_pilot_schema_fixture(
    connection: sqlite3.Connection,
    *,
    status_check: bool,
    promotion_check: bool,
    uniqueness: str,
    spoof_check_comments: bool = False,
) -> None:
    constraints = []
    if status_check:
        constraints.append("CHECK(status IN ('ok', 'error'))")
    if promotion_check:
        constraints.append("CHECK(production_promoted = 0)")
    if uniqueness == "full":
        constraints.append("UNIQUE(pilot_id, checked_at)")
    constraint_sql = "".join(f", {constraint}" for constraint in constraints)
    spoofed_sql = ""
    if spoof_check_comments:
        spoofed_sql = (
            "/* CHECK(status IN ('ok', 'error')) "
            "CHECK(production_promoted = 0) */"
        )
    connection.execute(
        f"""
        CREATE TABLE rwa_pilot_outcomes (
            outcome_id TEXT PRIMARY KEY,
            created_at TEXT NOT NULL,
            started_at TEXT NOT NULL,
            checked_at TEXT NOT NULL,
            pilot_id TEXT NOT NULL,
            status TEXT NOT NULL,
            symbol TEXT NOT NULL,
            venue TEXT NOT NULL,
            source_lane TEXT NOT NULL,
            freshness_limit_seconds REAL NOT NULL,
            checks_json TEXT NOT NULL,
            evidence_hash TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            evidence_bytes INTEGER NOT NULL,
            error_type TEXT,
            error_message TEXT,
            production_promoted INTEGER NOT NULL DEFAULT 0,
            ingestion_source TEXT NOT NULL DEFAULT 'growth_pilot'
            {constraint_sql}
            {spoofed_sql}
        )
        """
    )
    if uniqueness == "partial":
        connection.execute(
            "CREATE UNIQUE INDEX spoofed_pilot_time_unique "
            "ON rwa_pilot_outcomes(pilot_id, checked_at) WHERE 0"
        )


def _insert_pilot_schema_fixture_row(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        INSERT INTO rwa_pilot_outcomes (
            outcome_id, created_at, started_at, checked_at, pilot_id,
            status, symbol, venue, source_lane, freshness_limit_seconds,
            checks_json, evidence_hash, evidence_json, evidence_bytes,
            error_type, error_message, production_promoted, ingestion_source
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "existing-pilot-outcome",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:01+00:00",
            "existing-pilot",
            "ok",
            "EUR/USD",
            "gains",
            "price_stream_no_book",
            60.0,
            "{}",
            f"sha256:{'1' * 64}",
            "{}",
            2,
            None,
            None,
            0,
            "growth_pilot",
        ),
    )


def _legacy_row(
    observation_id: str,
    *,
    timestamp: str,
    symbol: str = "EUR/USD",
    venue: str = "gains",
) -> tuple:
    raw = {
        "symbol": symbol,
        "venue": venue,
        "asset_class": "fx",
        "source_type": "price_stream_no_book",
        "timestamp": timestamp,
        "value": 1.14,
    }
    normalized = dict(raw)
    return (
        observation_id,
        timestamp,
        symbol,
        venue,
        "fx",
        "price_stream_no_book",
        _legacy_hash(raw),
        _legacy_hash(normalized),
        _legacy_json(raw),
        _legacy_json(normalized),
        "{}",
        "{}",
        "{}",
        _legacy_json({"product": "legacy_store_test"}),
    )


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


def test_store_uses_current_schema_and_server_owned_ingestion_time(tmp_path):
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


def test_store_migrates_v1_transactionally_and_legacy_reopen_still_works(tmp_path):
    db_path = tmp_path / "legacy.db"
    first = _legacy_row("legacy-1", timestamp="2026-01-01T00:00:00+00:00")
    second = _legacy_row(
        "legacy-2",
        timestamp="2026-01-02T00:00:00+00:00",
        symbol="GBP/USD",
        venue="ostium",
    )
    _create_legacy_v1_db(db_path, [first, second])
    connection = sqlite3.connect(db_path)
    before = connection.execute(
        f"SELECT {', '.join(_LEGACY_COLUMNS)} FROM rwa_observations ORDER BY observation_id"
    ).fetchall()
    connection.close()

    store = RWAObservationStore(str(db_path))

    status = store.schema_status()
    assert status["ready"] is True
    assert status["schema_version"] == RWA_STORE_SCHEMA_VERSION == 3
    assert status["migration_required"] is False
    assert status["integrity"] == "ok"
    assert store.summary()["total_observations"] == 2

    connection = sqlite3.connect(db_path)
    after = connection.execute(
        f"SELECT {', '.join(_LEGACY_COLUMNS)} FROM rwa_observations ORDER BY observation_id"
    ).fetchall()
    migrated = connection.execute(
        "SELECT observed_at, ingested_at, raw_payload_bytes, normalized_bytes, "
        "ingestion_source, idempotency_key FROM rwa_observations ORDER BY observation_id"
    ).fetchall()
    metadata = dict(connection.execute("SELECT key, value FROM rwa_store_metadata"))
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()
    assert after == before
    assert all(row[0] and row[1] and row[2] > 0 and row[3] > 0 for row in migrated)
    assert {row[4] for row in migrated} == {"legacy_v1"}
    assert {row[5] for row in migrated} == {None}
    assert metadata == {"schema_version": "3", "migration_required": "false"}

    # Reopening the candidate is idempotent and does not rewrite legacy columns.
    reopened = RWAObservationStore(str(db_path))
    assert reopened.schema_status()["ready"] is True
    connection = sqlite3.connect(db_path)
    assert (
        connection.execute(
            f"SELECT {', '.join(_LEGACY_COLUMNS)} FROM rwa_observations ORDER BY observation_id"
        ).fetchall()
        == before
    )

    # The v0.6.2 named-column INSERT remains valid after migration. A subsequent
    # candidate start safely backfills that rollback-era row without row loss.
    rollback_row = _legacy_row(
        "legacy-after-rollback",
        timestamp="2026-01-03T00:00:00+00:00",
        symbol="USD/JPY",
        venue="gains",
    )
    connection.execute(
        f"INSERT OR REPLACE INTO rwa_observations ({', '.join(_LEGACY_COLUMNS)}) "
        f"VALUES ({', '.join('?' for _ in _LEGACY_COLUMNS)})",
        rollback_row,
    )
    connection.commit()
    assert connection.execute("SELECT COUNT(*) FROM rwa_observations").fetchone()[0] == 3
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()

    upgraded_again = RWAObservationStore(str(db_path))
    assert upgraded_again.schema_status()["ready"] is True
    assert upgraded_again.summary()["total_observations"] == 3
    connection = sqlite3.connect(db_path)
    rollback_backfill = connection.execute(
        "SELECT observed_at, ingested_at, ingestion_source FROM rwa_observations "
        "WHERE observation_id = 'legacy-after-rollback'"
    ).fetchone()
    assert rollback_backfill[0] and rollback_backfill[1]
    assert rollback_backfill[2] == "legacy_v1"
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()


def test_v1_migration_rolls_back_ddl_rows_and_metadata_on_integrity_failure(
    tmp_path,
    monkeypatch,
):
    db_path = tmp_path / "legacy.db"
    legacy = _legacy_row("legacy-1", timestamp="2026-01-01T00:00:00+00:00")
    _create_legacy_v1_db(db_path, [legacy])
    original_integrity_check = RWAObservationStore._integrity_check
    calls = 0

    def fail_after_additive_migration(connection):
        nonlocal calls
        calls += 1
        if calls == 4:
            raise ValueError("injected post-migration integrity failure")
        original_integrity_check(connection)

    monkeypatch.setattr(
        RWAObservationStore,
        "_integrity_check",
        staticmethod(fail_after_additive_migration),
    )
    with pytest.raises(ValueError, match="injected post-migration integrity failure"):
        RWAObservationStore(str(db_path))

    connection = sqlite3.connect(db_path)
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    columns = {row[1] for row in connection.execute("PRAGMA table_info(rwa_observations)")}
    stored = connection.execute(
        f"SELECT {', '.join(_LEGACY_COLUMNS)} FROM rwa_observations"
    ).fetchone()
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()
    assert tables == {"rwa_observations"}
    assert columns == set(_LEGACY_COLUMNS)
    assert stored == legacy


def test_v1_migration_fails_closed_without_mutation_for_ambiguous_schema(tmp_path):
    db_path = tmp_path / "ambiguous.db"
    legacy = _legacy_row("legacy-1", timestamp="2026-01-01T00:00:00+00:00")
    _create_legacy_v1_db(db_path, [legacy])
    connection = sqlite3.connect(db_path)
    connection.execute("ALTER TABLE rwa_observations ADD COLUMN unexplained_state TEXT")
    connection.commit()
    connection.close()

    with pytest.raises(ValueError, match="missing or ambiguous"):
        RWAObservationStore(str(db_path))

    connection = sqlite3.connect(db_path)
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    columns = {row[1] for row in connection.execute("PRAGMA table_info(rwa_observations)")}
    assert connection.execute("SELECT COUNT(*) FROM rwa_observations").fetchone()[0] == 1
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()
    assert tables == {"rwa_observations"}
    assert columns == set(_LEGACY_COLUMNS) | {"unexplained_state"}


def test_zero_row_v1_migration_rejects_stealth_unique_index_without_stamp_or_mutation(
    tmp_path,
):
    db_path = tmp_path / "stealth-unique-observation.db"
    _create_legacy_v1_db(db_path, [])
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE UNIQUE INDEX stealth_unique_observation_symbol "
        "ON rwa_observations(symbol)"
    )
    connection.commit()
    before_schema = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    connection.close()

    with pytest.raises(ValueError, match="unknown or incompatible index"):
        RWAObservationStore(str(db_path))

    connection = sqlite3.connect(db_path)
    after_schema = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    assert connection.execute("SELECT COUNT(*) FROM rwa_observations").fetchone()[0] == 0
    assert connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'rwa_store_metadata'"
    ).fetchone() is None
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()
    assert after_schema == before_schema


def test_zero_row_v1_migration_rejects_nonunique_expression_index_without_mutation(
    tmp_path,
):
    db_path = tmp_path / "stealth-expression-observation.db"
    _create_legacy_v1_db(db_path, [])
    connection = sqlite3.connect(db_path)
    connection.execute(
        "CREATE INDEX stealth_observation_json_symbol "
        "ON rwa_observations(json_extract(raw_payload_json, '$.symbol'))"
    )
    connection.commit()
    before_schema = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    expression_columns = connection.execute(
        "PRAGMA index_xinfo(stealth_observation_json_symbol)"
    ).fetchall()
    connection.close()
    assert any(row[1] == -2 for row in expression_columns)

    with pytest.raises(ValueError, match="unknown or incompatible index"):
        RWAObservationStore(str(db_path))

    connection = sqlite3.connect(db_path)
    after_schema = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    assert connection.execute("SELECT COUNT(*) FROM rwa_observations").fetchone()[0] == 0
    assert connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'rwa_store_metadata'"
    ).fetchone() is None
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()
    assert after_schema == before_schema


def test_zero_row_v1_migration_rejects_table_check_without_stamp_or_mutation(tmp_path):
    db_path = tmp_path / "stealth-check-observation.db"
    connection = sqlite3.connect(db_path)
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
            metadata_json TEXT DEFAULT '{}',
            CHECK(symbol = 'EUR/USD')
        )
        """
    )
    connection.execute("CREATE INDEX idx_rwa_observations_symbol ON rwa_observations(symbol)")
    connection.execute("CREATE INDEX idx_rwa_observations_venue ON rwa_observations(venue)")
    connection.execute(
        "CREATE INDEX idx_rwa_observations_created_at ON rwa_observations(created_at)"
    )
    connection.commit()
    before_schema = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    connection.close()

    with pytest.raises(ValueError, match="table definition is incompatible"):
        RWAObservationStore(str(db_path))

    connection = sqlite3.connect(db_path)
    after_schema = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    assert connection.execute("SELECT COUNT(*) FROM rwa_observations").fetchone()[0] == 0
    assert connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'rwa_store_metadata'"
    ).fetchone() is None
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()
    assert after_schema == before_schema


def test_partially_additive_v1_without_current_unique_index_migrates_safely(tmp_path):
    db_path = tmp_path / "partial-v1.db"
    legacy = _legacy_row("legacy-1", timestamp="2026-01-01T00:00:00+00:00")
    _create_legacy_v1_db(db_path, [legacy])
    connection = sqlite3.connect(db_path)
    for name, definition in (
        ("observed_at", "TEXT"),
        ("ingested_at", "TEXT"),
        ("raw_payload_bytes", "INTEGER DEFAULT 0"),
    ):
        connection.execute(f"ALTER TABLE rwa_observations ADD COLUMN {name} {definition}")
    connection.execute("DROP INDEX idx_rwa_observations_venue")
    connection.commit()
    assert connection.execute(
        "SELECT 1 FROM sqlite_master "
        "WHERE type = 'index' AND name = 'idx_rwa_observations_idempotency'"
    ).fetchone() is None
    connection.close()

    store = RWAObservationStore(str(db_path))

    assert store.schema_status()["ready"] is True
    connection = sqlite3.connect(db_path)
    unique_indexes = {
        row[1]
        for row in connection.execute("PRAGMA index_list(rwa_observations)")
        if int(row[2]) == 1
    }
    all_indexes = {
        row[1] for row in connection.execute("PRAGMA index_list(rwa_observations)")
    }
    stored = connection.execute(
        f"SELECT {', '.join(_LEGACY_COLUMNS)} FROM rwa_observations"
    ).fetchone()
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()
    assert unique_indexes == {
        "sqlite_autoindex_rwa_observations_1",
        "idx_rwa_observations_idempotency",
    }
    assert all_indexes == {
        "sqlite_autoindex_rwa_observations_1",
        "idx_rwa_observations_symbol",
        "idx_rwa_observations_venue",
        "idx_rwa_observations_created_at",
        "idx_rwa_observations_idempotency",
    }
    assert stored == legacy


def test_v1_migration_rejects_generated_observation_column_without_stamp_or_mutation(
    tmp_path,
):
    db_path = tmp_path / "generated-observation.db"
    legacy = _legacy_row("legacy-1", timestamp="2026-01-01T00:00:00+00:00")
    _create_legacy_v1_db(db_path, [legacy])
    connection = sqlite3.connect(db_path)
    connection.execute(
        "ALTER TABLE rwa_observations ADD COLUMN stealth_evidence_length INTEGER "
        "GENERATED ALWAYS AS (length(metadata_json)) VIRTUAL"
    )
    connection.commit()
    before_schema = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    before_rows = connection.execute(
        "SELECT * FROM rwa_observations ORDER BY observation_id"
    ).fetchall()
    hidden = {
        row[1]: row[6]
        for row in connection.execute("PRAGMA table_xinfo(rwa_observations)")
    }
    connection.close()
    assert hidden["stealth_evidence_length"] != 0

    with pytest.raises(ValueError, match="hidden or generated columns"):
        RWAObservationStore(str(db_path))

    connection = sqlite3.connect(db_path)
    after_schema = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    after_rows = connection.execute(
        "SELECT * FROM rwa_observations ORDER BY observation_id"
    ).fetchall()
    assert connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'rwa_store_metadata'"
    ).fetchone() is None
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()
    assert after_schema == before_schema
    assert after_rows == before_rows


@pytest.mark.parametrize(
    ("table_name", "generated_column_ddl"),
    [
        (
            "rwa_store_metadata",
            "ALTER TABLE rwa_store_metadata ADD COLUMN stealth_value_length INTEGER "
            "GENERATED ALWAYS AS (length(value)) VIRTUAL",
        ),
        (
            "rwa_pilot_outcomes",
            "ALTER TABLE rwa_pilot_outcomes ADD COLUMN stealth_status TEXT "
            "GENERATED ALWAYS AS (status) VIRTUAL",
        ),
    ],
)
def test_v3_readiness_rejects_generated_metadata_or_pilot_column_without_mutation(
    tmp_path,
    table_name,
    generated_column_ddl,
):
    db_path = tmp_path / f"generated-{table_name}.db"
    store = RWAObservationStore(str(db_path))
    store.store_observation(_payload())
    connection = sqlite3.connect(db_path)
    _insert_pilot_schema_fixture_row(connection)
    connection.execute(generated_column_ddl)
    connection.commit()
    before_schema = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    before_observations = connection.execute(
        "SELECT * FROM rwa_observations ORDER BY observation_id"
    ).fetchall()
    before_metadata = connection.execute(
        "SELECT * FROM rwa_store_metadata ORDER BY key"
    ).fetchall()
    before_pilot_rows = connection.execute(
        "SELECT * FROM rwa_pilot_outcomes ORDER BY outcome_id"
    ).fetchall()
    hidden = [
        row[6]
        for row in connection.execute(f'PRAGMA table_xinfo("{table_name}")')
        if str(row[1]).startswith("stealth_")
    ]
    connection.close()
    assert hidden and all(value != 0 for value in hidden)

    status = store.schema_status(force=True)
    assert status["ready"] is False
    with pytest.raises(ValueError, match=f"RWA {table_name} contains hidden or generated"):
        RWAObservationStore(str(db_path))

    connection = sqlite3.connect(db_path)
    after_schema = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    after_observations = connection.execute(
        "SELECT * FROM rwa_observations ORDER BY observation_id"
    ).fetchall()
    after_metadata = connection.execute(
        "SELECT * FROM rwa_store_metadata ORDER BY key"
    ).fetchall()
    after_pilot_rows = connection.execute(
        "SELECT * FROM rwa_pilot_outcomes ORDER BY outcome_id"
    ).fetchall()
    assert dict(connection.execute("SELECT key, value FROM rwa_store_metadata")) == {
        "schema_version": "3",
        "migration_required": "false",
    }
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()
    assert after_schema == before_schema
    assert after_observations == before_observations
    assert after_metadata == before_metadata
    assert after_pilot_rows == before_pilot_rows


@pytest.mark.parametrize(
    ("table_name", "unique_index_ddl"),
    [
        (
            "rwa_observations",
            "CREATE UNIQUE INDEX stealth_runtime_observation_symbol "
            "ON rwa_observations(symbol)",
        ),
        (
            "rwa_store_metadata",
            "CREATE UNIQUE INDEX stealth_runtime_metadata_value "
            "ON rwa_store_metadata(value)",
        ),
        (
            "rwa_pilot_outcomes",
            "CREATE UNIQUE INDEX stealth_runtime_pilot_status "
            "ON rwa_pilot_outcomes(status)",
        ),
    ],
)
def test_current_unique_index_allowlist_rejects_runtime_tampering_in_readiness(
    tmp_path,
    table_name,
    unique_index_ddl,
):
    db_path = tmp_path / f"runtime-unique-{table_name}.db"
    store = RWAObservationStore(str(db_path))
    assert store.schema_status(force=True)["ready"] is True
    connection = sqlite3.connect(db_path)
    connection.execute(unique_index_ddl)
    connection.commit()
    before_schema = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    before_metadata = connection.execute(
        "SELECT key, value FROM rwa_store_metadata ORDER BY key"
    ).fetchall()
    connection.close()

    assert store.schema_status(force=True)["ready"] is False
    with pytest.raises(ValueError, match=f"RWA {table_name} contains an unknown"):
        RWAObservationStore(str(db_path))

    connection = sqlite3.connect(db_path)
    after_schema = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    after_metadata = connection.execute(
        "SELECT key, value FROM rwa_store_metadata ORDER BY key"
    ).fetchall()
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()
    assert after_schema == before_schema
    assert after_metadata == before_metadata == [
        ("migration_required", "false"),
        ("schema_version", "3"),
    ]


@pytest.mark.parametrize(
    ("table_name", "expression_index_ddl"),
    [
        (
            "rwa_observations",
            "CREATE INDEX stealth_runtime_observation_expression "
            "ON rwa_observations(json_extract(raw_payload_json, '$.symbol'))",
        ),
        (
            "rwa_store_metadata",
            "CREATE INDEX stealth_runtime_metadata_expression "
            "ON rwa_store_metadata(length(value))",
        ),
        (
            "rwa_pilot_outcomes",
            "CREATE INDEX stealth_runtime_pilot_expression "
            "ON rwa_pilot_outcomes(lower(status))",
        ),
    ],
)
def test_current_index_allowlist_rejects_runtime_nonunique_expression_tampering(
    tmp_path,
    table_name,
    expression_index_ddl,
):
    db_path = tmp_path / f"runtime-expression-{table_name}.db"
    store = RWAObservationStore(str(db_path))
    assert store.schema_status(force=True)["ready"] is True
    connection = sqlite3.connect(db_path)
    connection.execute(expression_index_ddl)
    connection.commit()
    before_schema = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    before_metadata = connection.execute(
        "SELECT key, value FROM rwa_store_metadata ORDER BY key"
    ).fetchall()
    expression_index_name = connection.execute(
        "SELECT name FROM sqlite_master WHERE type = 'index' AND name LIKE 'stealth_runtime_%'"
    ).fetchone()[0]
    expression_columns = connection.execute(
        f'PRAGMA index_xinfo("{expression_index_name}")'
    ).fetchall()
    connection.close()
    assert any(row[1] == -2 for row in expression_columns)

    assert store.schema_status(force=True)["ready"] is False
    with pytest.raises(ValueError, match=f"RWA {table_name} contains an unknown"):
        RWAObservationStore(str(db_path))

    connection = sqlite3.connect(db_path)
    after_schema = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    after_metadata = connection.execute(
        "SELECT key, value FROM rwa_store_metadata ORDER BY key"
    ).fetchall()
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()
    assert after_schema == before_schema
    assert after_metadata == before_metadata


@pytest.mark.parametrize(
    ("table_name", "replacement_sql"),
    [
        (
            "rwa_observations",
            """
            CREATE TABLE rwa_observations (
                observation_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                observed_at TEXT,
                ingested_at TEXT,
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
                metadata_json TEXT DEFAULT '{}',
                raw_payload_bytes INTEGER DEFAULT 0,
                normalized_bytes INTEGER DEFAULT 0,
                realtime_quality_bytes INTEGER DEFAULT 0,
                blocksize_benchmark_bytes INTEGER DEFAULT 0,
                promotion_bytes INTEGER DEFAULT 0,
                metadata_bytes INTEGER DEFAULT 0,
                ingestion_source TEXT DEFAULT 'operator',
                idempotency_key TEXT,
                CHECK(symbol = 'EUR/USD')
            )
            """,
        ),
        (
            "rwa_store_metadata",
            """
            CREATE TABLE rwa_store_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                CHECK(key IN ('schema_version', 'migration_required'))
            )
            """,
        ),
        (
            "rwa_pilot_outcomes",
            """
            CREATE TABLE rwa_pilot_outcomes (
                outcome_id TEXT PRIMARY KEY,
                created_at TEXT NOT NULL,
                started_at TEXT NOT NULL,
                checked_at TEXT NOT NULL,
                pilot_id TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('ok', 'error')),
                symbol TEXT NOT NULL,
                venue TEXT NOT NULL,
                source_lane TEXT NOT NULL,
                freshness_limit_seconds REAL NOT NULL,
                checks_json TEXT NOT NULL,
                evidence_hash TEXT NOT NULL,
                evidence_json TEXT NOT NULL,
                evidence_bytes INTEGER NOT NULL,
                error_type TEXT,
                error_message TEXT,
                production_promoted INTEGER NOT NULL DEFAULT 0
                    CHECK(production_promoted = 0),
                ingestion_source TEXT NOT NULL DEFAULT 'growth_pilot',
                UNIQUE(pilot_id, checked_at),
                CHECK(symbol = 'EUR/USD')
            )
            """,
        ),
    ],
)
def test_current_readiness_rejects_runtime_table_semantic_tampering(
    tmp_path,
    table_name,
    replacement_sql,
):
    db_path = tmp_path / f"runtime-table-{table_name}.db"
    store = RWAObservationStore(str(db_path))
    assert store.schema_status(force=True)["ready"] is True
    connection = sqlite3.connect(db_path)
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(f'ALTER TABLE "{table_name}" RENAME TO "{table_name}_old"')
    connection.execute(replacement_sql)
    connection.execute(
        f'INSERT INTO "{table_name}" SELECT * FROM "{table_name}_old"'
    )
    connection.execute(f'DROP TABLE "{table_name}_old"')
    if table_name == "rwa_observations":
        connection.execute(
            "CREATE INDEX idx_rwa_observations_symbol ON rwa_observations(symbol)"
        )
        connection.execute(
            "CREATE INDEX idx_rwa_observations_venue ON rwa_observations(venue)"
        )
        connection.execute(
            "CREATE INDEX idx_rwa_observations_created_at ON rwa_observations(created_at)"
        )
        connection.execute(
            "CREATE UNIQUE INDEX idx_rwa_observations_idempotency "
            "ON rwa_observations(idempotency_key) WHERE idempotency_key IS NOT NULL"
        )
    connection.commit()
    before_schema = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    before_metadata = connection.execute(
        "SELECT key, value FROM rwa_store_metadata ORDER BY key"
    ).fetchall()
    connection.close()

    assert store.schema_status(force=True)["ready"] is False
    with pytest.raises(ValueError, match="table definition is incompatible"):
        RWAObservationStore(str(db_path))

    connection = sqlite3.connect(db_path)
    after_schema = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    after_metadata = connection.execute(
        "SELECT key, value FROM rwa_store_metadata ORDER BY key"
    ).fetchall()
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()
    assert after_schema == before_schema
    assert after_metadata == before_metadata


@pytest.mark.parametrize(
    ("object_type", "object_ddl"),
    [
        pytest.param(
            "trigger",
            """
            CREATE TRIGGER mutate_legacy_evidence
            AFTER UPDATE ON rwa_observations
            BEGIN
                UPDATE rwa_observations
                SET metadata_json = '{"tampered":true}'
                WHERE observation_id = NEW.observation_id;
            END
            """,
            id="trigger",
        ),
        pytest.param(
            "trigger",
            """
            CREATE TRIGGER sqliteXmutate
            AFTER UPDATE ON rwa_observations
            BEGIN
                UPDATE rwa_observations
                SET metadata_json = NULL
                WHERE observation_id = NEW.observation_id;
            END
            """,
            id="sqliteX-trigger-prefix-bypass",
        ),
        pytest.param(
            "view",
            """
            CREATE VIEW rwa_observation_export AS
            SELECT observation_id, metadata_json FROM rwa_observations
            """,
            id="view",
        ),
    ],
)
def test_v1_migration_rejects_unexpected_schema_objects_before_mutation(
    tmp_path,
    object_type,
    object_ddl,
):
    db_path = tmp_path / f"unexpected-{object_type}.db"
    legacy = _legacy_row("legacy-1", timestamp="2026-01-01T00:00:00+00:00")
    _create_legacy_v1_db(db_path, [legacy])
    connection = sqlite3.connect(db_path)
    connection.execute(object_ddl)
    connection.commit()
    before = connection.execute(
        f"SELECT {', '.join(_LEGACY_COLUMNS)} FROM rwa_observations"
    ).fetchone()
    connection.close()

    with pytest.raises(ValueError, match="unexpected triggers or views"):
        RWAObservationStore(str(db_path))

    connection = sqlite3.connect(db_path)
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    objects = {
        (row[0], row[1])
        for row in connection.execute(
            "SELECT type, name FROM sqlite_master "
            "WHERE type IN ('trigger', 'view')"
        )
    }
    columns = {row[1] for row in connection.execute("PRAGMA table_info(rwa_observations)")}
    after = connection.execute(
        f"SELECT {', '.join(_LEGACY_COLUMNS)} FROM rwa_observations"
    ).fetchone()
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()
    assert tables == {"rwa_observations"}
    assert columns == set(_LEGACY_COLUMNS)
    assert before == after == legacy
    assert len(objects) == 1
    assert next(iter(objects))[0] == object_type


def test_v1_migration_rejects_sqlitex_named_user_table_before_mutation(tmp_path):
    db_path = tmp_path / "sqlitex-user-table.db"
    legacy = _legacy_row("legacy-1", timestamp="2026-01-01T00:00:00+00:00")
    _create_legacy_v1_db(db_path, [legacy])
    connection = sqlite3.connect(db_path)
    connection.execute("CREATE TABLE sqliteXshadow (payload TEXT NOT NULL)")
    connection.execute("INSERT INTO sqliteXshadow VALUES ('must-remain')")
    connection.commit()
    before = connection.execute(
        f"SELECT {', '.join(_LEGACY_COLUMNS)} FROM rwa_observations"
    ).fetchone()
    connection.close()

    with pytest.raises(ValueError, match="ambiguous table layout"):
        RWAObservationStore(str(db_path))

    connection = sqlite3.connect(db_path)
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' "
            "AND lower(substr(name, 1, 7)) != 'sqlite_'"
        )
    }
    after = connection.execute(
        f"SELECT {', '.join(_LEGACY_COLUMNS)} FROM rwa_observations"
    ).fetchone()
    shadow_payload = connection.execute("SELECT payload FROM sqliteXshadow").fetchone()[0]
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()
    assert tables == {"rwa_observations", "sqliteXshadow"}
    assert before == after == legacy
    assert shadow_payload == "must-remain"


@pytest.mark.parametrize(
    ("status_check", "promotion_check"),
    [
        (False, True),
        (True, False),
    ],
)
def test_v1_migration_behaviorally_rejects_comment_spoofed_pilot_checks(
    tmp_path,
    status_check,
    promotion_check,
):
    db_path = tmp_path / "spoofed-pilot-check.db"
    legacy = _legacy_row("legacy-1", timestamp="2026-01-01T00:00:00+00:00")
    _create_legacy_v1_db(db_path, [legacy])
    connection = sqlite3.connect(db_path)
    _create_pilot_schema_fixture(
        connection,
        status_check=status_check,
        promotion_check=promotion_check,
        uniqueness="full",
        spoof_check_comments=True,
    )
    _insert_pilot_schema_fixture_row(connection)
    connection.commit()
    before_schema = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    before_observations = connection.execute(
        f"SELECT {', '.join(_LEGACY_COLUMNS)} FROM rwa_observations"
    ).fetchall()
    before_pilot_rows = connection.execute(
        "SELECT * FROM rwa_pilot_outcomes ORDER BY outcome_id"
    ).fetchall()
    connection.close()

    with pytest.raises(ValueError, match="table definition is incompatible"):
        RWAObservationStore(str(db_path))

    connection = sqlite3.connect(db_path)
    after_schema = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    after_observations = connection.execute(
        f"SELECT {', '.join(_LEGACY_COLUMNS)} FROM rwa_observations"
    ).fetchall()
    after_pilot_rows = connection.execute(
        "SELECT * FROM rwa_pilot_outcomes ORDER BY outcome_id"
    ).fetchall()
    assert connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'rwa_store_metadata'"
    ).fetchone() is None
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()
    assert after_schema == before_schema
    assert after_observations == before_observations == [legacy]
    assert after_pilot_rows == before_pilot_rows


def test_v1_migration_rejects_spoofed_checks_and_partial_unique_without_mutation(
    tmp_path,
):
    db_path = tmp_path / "spoofed-partial-pilot.db"
    legacy = _legacy_row("legacy-1", timestamp="2026-01-01T00:00:00+00:00")
    _create_legacy_v1_db(db_path, [legacy])
    connection = sqlite3.connect(db_path)
    _create_pilot_schema_fixture(
        connection,
        status_check=False,
        promotion_check=False,
        uniqueness="partial",
        spoof_check_comments=True,
    )
    _insert_pilot_schema_fixture_row(connection)
    connection.commit()
    before_schema = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    before_observations = connection.execute(
        f"SELECT {', '.join(_LEGACY_COLUMNS)} FROM rwa_observations"
    ).fetchall()
    before_pilot_rows = connection.execute(
        "SELECT * FROM rwa_pilot_outcomes ORDER BY outcome_id"
    ).fetchall()
    connection.close()

    with pytest.raises(ValueError, match="table definition is incompatible"):
        RWAObservationStore(str(db_path))

    connection = sqlite3.connect(db_path)
    after_schema = connection.execute(
        "SELECT type, name, tbl_name, sql FROM sqlite_master ORDER BY type, name"
    ).fetchall()
    after_observations = connection.execute(
        f"SELECT {', '.join(_LEGACY_COLUMNS)} FROM rwa_observations"
    ).fetchall()
    after_pilot_rows = connection.execute(
        "SELECT * FROM rwa_pilot_outcomes ORDER BY outcome_id"
    ).fetchall()
    assert connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'rwa_store_metadata'"
    ).fetchone() is None
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()
    assert after_schema == before_schema
    assert after_observations == before_observations == [legacy]
    assert after_pilot_rows == before_pilot_rows


def test_pilot_constraint_probes_preserve_rows_inside_and_outside_transaction(tmp_path):
    store = RWAObservationStore(str(tmp_path / "rwa.db"))
    connection = sqlite3.connect(store.db_path)
    _insert_pilot_schema_fixture_row(connection)
    connection.commit()
    before = connection.execute(
        "SELECT * FROM rwa_pilot_outcomes ORDER BY outcome_id"
    ).fetchall()

    RWAObservationStore._validate_pilot_table(
        connection,
        require_current_indexes=True,
    )
    assert connection.in_transaction is False
    assert connection.execute(
        "SELECT * FROM rwa_pilot_outcomes ORDER BY outcome_id"
    ).fetchall() == before

    connection.execute("BEGIN IMMEDIATE")
    RWAObservationStore._validate_pilot_table(
        connection,
        require_current_indexes=True,
    )
    assert connection.in_transaction is True
    assert connection.execute(
        "SELECT * FROM rwa_pilot_outcomes ORDER BY outcome_id"
    ).fetchall() == before
    connection.rollback()
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()


def test_v1_migration_fails_closed_without_mutation_for_invalid_evidence(tmp_path):
    db_path = tmp_path / "invalid-row.db"
    legacy = list(_legacy_row("legacy-1", timestamp="2026-01-01T00:00:00+00:00"))
    legacy[_LEGACY_COLUMNS.index("raw_payload_hash")] = "sha256:" + ("0" * 64)
    _create_legacy_v1_db(db_path, [tuple(legacy)])

    with pytest.raises(ValueError, match="raw_payload_hash is invalid"):
        RWAObservationStore(str(db_path))

    connection = sqlite3.connect(db_path)
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
        )
    }
    columns = {row[1] for row in connection.execute("PRAGMA table_info(rwa_observations)")}
    stored_hash = connection.execute("SELECT raw_payload_hash FROM rwa_observations").fetchone()[0]
    assert connection.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    connection.close()
    assert tables == {"rwa_observations"}
    assert columns == set(_LEGACY_COLUMNS)
    assert stored_hash == "sha256:" + ("0" * 64)


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
