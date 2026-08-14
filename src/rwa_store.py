"""Persistence for bounded, replayable RWA sourcing observations."""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from src.rwa_security import (
    configured_rwa_observation_db_path,
    stable_json_bytes,
    validate_component_size,
    validate_json_shape,
)


RWA_STORE_SCHEMA_VERSION = 3
RWA_PILOT_OUTCOME_BATCH_MAX = 20
RWA_PILOT_OUTCOME_HISTORY_MAX = 20_000
RWA_PILOT_MAX_FUTURE_SKEW_SECONDS = 5.0

_RWA_V1_COLUMN_SIGNATURES = {
    "observation_id": ("TEXT", 0, None, 1),
    "created_at": ("TEXT", 1, None, 0),
    "symbol": ("TEXT", 1, None, 0),
    "venue": ("TEXT", 1, None, 0),
    "asset_class": ("TEXT", 0, "''", 0),
    "source_type": ("TEXT", 0, "''", 0),
    "raw_payload_hash": ("TEXT", 1, None, 0),
    "normalized_hash": ("TEXT", 1, None, 0),
    "raw_payload_json": ("TEXT", 1, None, 0),
    "normalized_json": ("TEXT", 1, None, 0),
    "realtime_quality_json": ("TEXT", 0, "'{}'", 0),
    "blocksize_benchmark_json": ("TEXT", 0, "'{}'", 0),
    "promotion_json": ("TEXT", 0, "'{}'", 0),
    "metadata_json": ("TEXT", 0, "'{}'", 0),
}
_RWA_ADDITIVE_COLUMN_DDL = (
    ("observed_at", "TEXT"),
    ("ingested_at", "TEXT"),
    ("raw_payload_bytes", "INTEGER DEFAULT 0"),
    ("normalized_bytes", "INTEGER DEFAULT 0"),
    ("realtime_quality_bytes", "INTEGER DEFAULT 0"),
    ("blocksize_benchmark_bytes", "INTEGER DEFAULT 0"),
    ("promotion_bytes", "INTEGER DEFAULT 0"),
    ("metadata_bytes", "INTEGER DEFAULT 0"),
    ("ingestion_source", "TEXT DEFAULT 'legacy'"),
    ("idempotency_key", "TEXT"),
)
_RWA_ADDITIVE_COLUMN_SIGNATURES = {
    "observed_at": ("TEXT", 0, None, 0),
    "ingested_at": ("TEXT", 0, None, 0),
    "raw_payload_bytes": ("INTEGER", 0, "0", 0),
    "normalized_bytes": ("INTEGER", 0, "0", 0),
    "realtime_quality_bytes": ("INTEGER", 0, "0", 0),
    "blocksize_benchmark_bytes": ("INTEGER", 0, "0", 0),
    "promotion_bytes": ("INTEGER", 0, "0", 0),
    "metadata_bytes": ("INTEGER", 0, "0", 0),
    "ingestion_source": ("TEXT", 0, "'legacy'", 0),
    "idempotency_key": ("TEXT", 0, None, 0),
}
_RWA_METADATA_COLUMN_SIGNATURES = {
    "key": ("TEXT", 0, None, 1),
    "value": ("TEXT", 1, None, 0),
}
_RWA_PILOT_COLUMN_SIGNATURES = {
    "outcome_id": ("TEXT", 0, None, 1),
    "created_at": ("TEXT", 1, None, 0),
    "started_at": ("TEXT", 1, None, 0),
    "checked_at": ("TEXT", 1, None, 0),
    "pilot_id": ("TEXT", 1, None, 0),
    "status": ("TEXT", 1, None, 0),
    "symbol": ("TEXT", 1, None, 0),
    "venue": ("TEXT", 1, None, 0),
    "source_lane": ("TEXT", 1, None, 0),
    "freshness_limit_seconds": ("REAL", 1, None, 0),
    "checks_json": ("TEXT", 1, None, 0),
    "evidence_hash": ("TEXT", 1, None, 0),
    "evidence_json": ("TEXT", 1, None, 0),
    "evidence_bytes": ("INTEGER", 1, None, 0),
    "error_type": ("TEXT", 0, None, 0),
    "error_message": ("TEXT", 0, None, 0),
    "production_promoted": ("INTEGER", 1, "0", 0),
    "ingestion_source": ("TEXT", 1, "'growth_pilot'", 0),
}
_RWA_REQUIRED_INDEX_SQL = {
    "idx_rwa_observations_symbol": (
        "create index idx_rwa_observations_symbol on rwa_observations(symbol)"
    ),
    "idx_rwa_observations_venue": (
        "create index idx_rwa_observations_venue on rwa_observations(venue)"
    ),
    "idx_rwa_observations_created_at": (
        "create index idx_rwa_observations_created_at on rwa_observations(created_at)"
    ),
    "idx_rwa_observations_idempotency": (
        "create unique index idx_rwa_observations_idempotency "
        "on rwa_observations(idempotency_key) where idempotency_key is not null"
    ),
    "idx_rwa_pilot_outcomes_pilot_checked": (
        "create index idx_rwa_pilot_outcomes_pilot_checked "
        "on rwa_pilot_outcomes(pilot_id, checked_at desc)"
    ),
    "idx_rwa_pilot_outcomes_status_checked": (
        "create index idx_rwa_pilot_outcomes_status_checked "
        "on rwa_pilot_outcomes(status, checked_at desc)"
    ),
}
_RWA_OBSERVATION_PK_INDEX = (
    "sqlite_autoindex_rwa_observations_1",
    1,
    "pk",
    0,
    (
        (0, 0, "observation_id", 0, "BINARY", 1),
        (1, -1, None, 0, "BINARY", 0),
    ),
)
_RWA_OBSERVATION_SYMBOL_INDEXES = {
    (
        "idx_rwa_observations_symbol",
        0,
        "c",
        0,
        (
            (0, column_id, "symbol", 0, "BINARY", 1),
            (1, -1, None, 0, "BINARY", 0),
        ),
    )
    for column_id in (2, 4)
}
_RWA_OBSERVATION_VENUE_INDEXES = {
    (
        "idx_rwa_observations_venue",
        0,
        "c",
        0,
        (
            (0, column_id, "venue", 0, "BINARY", 1),
            (1, -1, None, 0, "BINARY", 0),
        ),
    )
    for column_id in (3, 5)
}
_RWA_OBSERVATION_CREATED_AT_INDEX = (
    "idx_rwa_observations_created_at",
    0,
    "c",
    0,
    (
        (0, 1, "created_at", 0, "BINARY", 1),
        (1, -1, None, 0, "BINARY", 0),
    ),
)
_RWA_OBSERVATION_IDEMPOTENCY_INDEX = (
    "idx_rwa_observations_idempotency",
    1,
    "c",
    1,
    (
        (0, 23, "idempotency_key", 0, "BINARY", 1),
        (1, -1, None, 0, "BINARY", 0),
    ),
)
_RWA_METADATA_PK_INDEX = (
    "sqlite_autoindex_rwa_store_metadata_1",
    1,
    "pk",
    0,
    (
        (0, 0, "key", 0, "BINARY", 1),
        (1, -1, None, 0, "BINARY", 0),
    ),
)
_RWA_PILOT_PK_INDEX = (
    "sqlite_autoindex_rwa_pilot_outcomes_1",
    1,
    "pk",
    0,
    (
        (0, 0, "outcome_id", 0, "BINARY", 1),
        (1, -1, None, 0, "BINARY", 0),
    ),
)
_RWA_PILOT_TIME_UNIQUE_INDEX = (
    "sqlite_autoindex_rwa_pilot_outcomes_2",
    1,
    "u",
    0,
    (
        (0, 4, "pilot_id", 0, "BINARY", 1),
        (1, 3, "checked_at", 0, "BINARY", 1),
        (2, -1, None, 0, "BINARY", 0),
    ),
)
_RWA_PILOT_CHECKED_INDEX = (
    "idx_rwa_pilot_outcomes_pilot_checked",
    0,
    "c",
    0,
    (
        (0, 4, "pilot_id", 0, "BINARY", 1),
        (1, 3, "checked_at", 1, "BINARY", 1),
        (2, -1, None, 0, "BINARY", 0),
    ),
)
_RWA_PILOT_STATUS_CHECKED_INDEX = (
    "idx_rwa_pilot_outcomes_status_checked",
    0,
    "c",
    0,
    (
        (0, 5, "status", 0, "BINARY", 1),
        (1, 3, "checked_at", 1, "BINARY", 1),
        (2, -1, None, 0, "BINARY", 0),
    ),
)
_RWA_V1_COLUMN_DDL = (
    ("observation_id", "TEXT PRIMARY KEY"),
    ("created_at", "TEXT NOT NULL"),
    ("symbol", "TEXT NOT NULL"),
    ("venue", "TEXT NOT NULL"),
    ("asset_class", "TEXT DEFAULT ''"),
    ("source_type", "TEXT DEFAULT ''"),
    ("raw_payload_hash", "TEXT NOT NULL"),
    ("normalized_hash", "TEXT NOT NULL"),
    ("raw_payload_json", "TEXT NOT NULL"),
    ("normalized_json", "TEXT NOT NULL"),
    ("realtime_quality_json", "TEXT DEFAULT '{}'"),
    ("blocksize_benchmark_json", "TEXT DEFAULT '{}'"),
    ("promotion_json", "TEXT DEFAULT '{}'"),
    ("metadata_json", "TEXT DEFAULT '{}'"),
)
_RWA_FRESH_OBSERVATION_COLUMN_DDL = (
    ("observation_id", "TEXT PRIMARY KEY"),
    ("created_at", "TEXT NOT NULL"),
    ("observed_at", "TEXT"),
    ("ingested_at", "TEXT"),
    ("symbol", "TEXT NOT NULL"),
    ("venue", "TEXT NOT NULL"),
    ("asset_class", "TEXT DEFAULT ''"),
    ("source_type", "TEXT DEFAULT ''"),
    ("raw_payload_hash", "TEXT NOT NULL"),
    ("normalized_hash", "TEXT NOT NULL"),
    ("raw_payload_json", "TEXT NOT NULL"),
    ("normalized_json", "TEXT NOT NULL"),
    ("realtime_quality_json", "TEXT DEFAULT '{}'"),
    ("blocksize_benchmark_json", "TEXT DEFAULT '{}'"),
    ("promotion_json", "TEXT DEFAULT '{}'"),
    ("metadata_json", "TEXT DEFAULT '{}'"),
    ("raw_payload_bytes", "INTEGER DEFAULT 0"),
    ("normalized_bytes", "INTEGER DEFAULT 0"),
    ("realtime_quality_bytes", "INTEGER DEFAULT 0"),
    ("blocksize_benchmark_bytes", "INTEGER DEFAULT 0"),
    ("promotion_bytes", "INTEGER DEFAULT 0"),
    ("metadata_bytes", "INTEGER DEFAULT 0"),
    ("ingestion_source", "TEXT DEFAULT 'operator'"),
    ("idempotency_key", "TEXT"),
)
_RWA_METADATA_COLUMN_DDL = (
    ("key", "TEXT PRIMARY KEY"),
    ("value", "TEXT NOT NULL"),
)
_RWA_PILOT_COLUMN_DDL = (
    ("outcome_id", "TEXT PRIMARY KEY"),
    ("created_at", "TEXT NOT NULL"),
    ("started_at", "TEXT NOT NULL"),
    ("checked_at", "TEXT NOT NULL"),
    ("pilot_id", "TEXT NOT NULL"),
    ("status", "TEXT NOT NULL CHECK(status IN ('ok', 'error'))"),
    ("symbol", "TEXT NOT NULL"),
    ("venue", "TEXT NOT NULL"),
    ("source_lane", "TEXT NOT NULL"),
    ("freshness_limit_seconds", "REAL NOT NULL"),
    ("checks_json", "TEXT NOT NULL"),
    ("evidence_hash", "TEXT NOT NULL"),
    ("evidence_json", "TEXT NOT NULL"),
    ("evidence_bytes", "INTEGER NOT NULL"),
    ("error_type", "TEXT"),
    ("error_message", "TEXT"),
    (
        "production_promoted",
        "INTEGER NOT NULL DEFAULT 0 CHECK(production_promoted = 0)",
    ),
    ("ingestion_source", "TEXT NOT NULL DEFAULT 'growth_pilot'"),
)
_RWA_LEGACY_HASH_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _stable_json(payload: Any) -> str:
    return stable_json_bytes(payload).decode("utf-8")


def _hash_payload(payload: Any) -> str:
    return f"sha256:{hashlib.sha256(stable_json_bytes(payload)).hexdigest()}"


def _finite_number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _derive_pilot_checks(
    evidence: dict[str, Any],
    *,
    checked_at: datetime,
    freshness_limit_seconds: float,
) -> dict[str, Any]:
    """Derive security-relevant pilot checks from immutable raw evidence."""
    if not evidence:
        return {
            "source_timestamp_pass": False,
            "future_skew_seconds": None,
            "freshness_seconds": None,
            "freshness_limit_seconds": freshness_limit_seconds,
            "freshness_pass": False,
            "bidask_sanity_pass": False,
        }
    observed_at = _parse_source_timestamp(evidence.get("timestamp"))
    future_skew_seconds = (observed_at - checked_at).total_seconds()
    if future_skew_seconds > RWA_PILOT_MAX_FUTURE_SKEW_SECONDS:
        raise ValueError("pilot source timestamp is after the checked_at allowance")
    freshness_seconds = max(0.0, (checked_at - observed_at).total_seconds())
    bid = _finite_number(evidence.get("bid"))
    ask = _finite_number(evidence.get("ask"))
    return {
        "source_timestamp_pass": True,
        "future_skew_seconds": round(max(0.0, future_skew_seconds), 6),
        "freshness_seconds": round(freshness_seconds, 6),
        "freshness_limit_seconds": freshness_limit_seconds,
        "freshness_pass": freshness_seconds <= freshness_limit_seconds,
        "bidask_sanity_pass": (
            bid is not None and ask is not None and 0 < bid <= ask
        ),
    }


def _parse_source_timestamp(value: Any) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str) and value.strip():
        try:
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("source timestamp must be ISO 8601") from exc
    else:
        raise ValueError("source timestamp is required")
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    parsed = parsed.astimezone(UTC)
    if parsed > datetime.now(UTC) + timedelta(minutes=5):
        raise ValueError("source timestamp is too far in the future")
    return parsed


def _source_timestamp(payload: dict[str, Any], normalized: dict[str, Any], raw: dict[str, Any]) -> str:
    normalized_value = normalized.get("timestamp")
    raw_value = raw.get("timestamp")
    top_level_value = payload.get("timestamp")
    canonical_value = normalized_value or raw_value or top_level_value
    canonical = _parse_source_timestamp(canonical_value)
    if top_level_value is not None and _parse_source_timestamp(top_level_value) != canonical:
        raise ValueError("top-level timestamp conflicts with normalized evidence")
    return canonical.isoformat()


class RWAObservationStore:
    """SQLite-backed store with bounded evidence and idempotent inserts."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or configured_rwa_observation_db_path()
        self._schema_status_cache: dict[str, Any] | None = None
        self._schema_status_checked_at = 0.0
        parent = Path(self.db_path).expanduser().parent
        if parent != Path("."):
            parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        self.schema_status(force=True)

    def _connect(self, *, lock_timeout_seconds: float = 30.0) -> sqlite3.Connection:
        timeout = max(0.01, min(float(lock_timeout_seconds), 30.0))
        connection = sqlite3.connect(self.db_path, timeout=timeout)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {int(timeout * 1_000)}")
        return connection

    @staticmethod
    def _integrity_check(connection: sqlite3.Connection) -> None:
        results = [str(row[0]) for row in connection.execute("PRAGMA integrity_check")]
        if results != ["ok"]:
            raise ValueError("RWA evidence database failed PRAGMA integrity_check")

    @staticmethod
    def _table_columns(
        connection: sqlite3.Connection,
        table_name: str,
    ) -> dict[str, tuple[str, int, str | None, int]]:
        quoted_table_name = table_name.replace('"', '""')
        columns: dict[str, tuple[str, int, str | None, int]] = {}
        for row in connection.execute(f'PRAGMA table_xinfo("{quoted_table_name}")'):
            if len(row) < 7 or int(row[6] or 0) != 0:
                raise ValueError(f"RWA {table_name} contains hidden or generated columns")
            columns[str(row[1])] = (
                " ".join(str(row[2] or "").upper().split()),
                int(row[3]),
                None if row[4] is None else str(row[4]),
                int(row[5]),
            )
        return columns

    @staticmethod
    def _canonical_sql(sql: str) -> str:
        tokens: list[str] = []
        index = 0
        while index < len(sql):
            character = sql[index]
            if character.isspace():
                index += 1
                continue
            if character == "'":
                start = index
                index += 1
                while index < len(sql):
                    if sql[index] == "'":
                        index += 1
                        if index < len(sql) and sql[index] == "'":
                            index += 1
                            continue
                        break
                    index += 1
                tokens.append(sql[start:index])
                continue
            if character in {'"', '`', '['}:
                closing = ']' if character == '[' else character
                index += 1
                value: list[str] = []
                while index < len(sql):
                    if sql[index] == closing:
                        index += 1
                        if closing != ']' and index < len(sql) and sql[index] == closing:
                            value.append(closing)
                            index += 1
                            continue
                        break
                    value.append(sql[index])
                    index += 1
                tokens.append(''.join(value).lower())
                continue
            if character in "(),":
                tokens.append(character)
                index += 1
                continue
            start = index
            while index < len(sql) and not sql[index].isspace() and sql[index] not in "(),":
                index += 1
            tokens.append(sql[start:index].lower())
        return " ".join(tokens)

    @classmethod
    def _expected_table_sql(
        cls,
        table_name: str,
        columns: tuple[tuple[str, str], ...],
        *,
        table_constraints: tuple[str, ...] = (),
    ) -> str:
        declarations = [f"{name} {definition}" for name, definition in columns]
        declarations.extend(table_constraints)
        return cls._canonical_sql(
            f"CREATE TABLE {table_name} ({', '.join(declarations)})"
        )

    @classmethod
    def _validate_table_sql(
        cls,
        connection: sqlite3.Connection,
        table_name: str,
        expected: set[str],
    ) -> None:
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        if row is None or row[0] is None or cls._canonical_sql(str(row[0])) not in expected:
            raise ValueError(f"RWA {table_name} table definition is incompatible")
        quoted_table_name = table_name.replace('"', '""')
        if connection.execute(f'PRAGMA foreign_key_list("{quoted_table_name}")').fetchone():
            raise ValueError(f"RWA {table_name} table definition is incompatible")
        table_list_row = connection.execute(
            "SELECT wr, strict FROM pragma_table_list WHERE schema = 'main' AND name = ?",
            (table_name,),
        ).fetchone()
        if table_list_row is None or int(table_list_row[0] or 0) or int(table_list_row[1] or 0):
            raise ValueError(f"RWA {table_name} table definition is incompatible")

    @classmethod
    def _validate_observation_table_sql(
        cls,
        connection: sqlite3.Connection,
        actual_names: set[str],
    ) -> None:
        additive_names = [name for name, _definition in _RWA_ADDITIVE_COLUMN_DDL]
        present_additive = [name for name in additive_names if name in actual_names]
        if present_additive != additive_names[: len(present_additive)]:
            raise ValueError("RWA observation additive columns are out of migration order")
        allowed = {
            cls._expected_table_sql("rwa_observations", _RWA_FRESH_OBSERVATION_COLUMN_DDL)
        }
        for count in range(len(_RWA_ADDITIVE_COLUMN_DDL) + 1):
            allowed.add(
                cls._expected_table_sql(
                    "rwa_observations",
                    _RWA_V1_COLUMN_DDL + _RWA_ADDITIVE_COLUMN_DDL[:count],
                )
            )
        cls._validate_table_sql(connection, "rwa_observations", allowed)

    @staticmethod
    def _index_signatures(
        connection: sqlite3.Connection,
        table_name: str,
    ) -> dict[str, tuple[Any, ...]]:
        quoted_table_name = table_name.replace('"', '""')
        signatures: dict[str, tuple[Any, ...]] = {}
        for index_row in connection.execute(f'PRAGMA index_list("{quoted_table_name}")'):
            if len(index_row) < 5:
                raise ValueError(f"RWA {table_name} index inventory is incomplete")
            unique = int(index_row[2] or 0)
            index_name = str(index_row[1])
            quoted_index_name = index_name.replace('"', '""')
            column_layout: list[tuple[int, int, str | None, int, str, int]] = []
            for column_row in connection.execute(
                f'PRAGMA index_xinfo("{quoted_index_name}")'
            ):
                if len(column_row) < 6:
                    raise ValueError(f"RWA {table_name} index inventory is incomplete")
                column_layout.append(
                    (
                        int(column_row[0]),
                        int(column_row[1]),
                        None if column_row[2] is None else str(column_row[2]),
                        int(column_row[3] or 0),
                        str(column_row[4] or ""),
                        int(column_row[5] or 0),
                    )
                )
            signatures[index_name] = (
                index_name,
                unique,
                str(index_row[3]),
                int(index_row[4] or 0),
                tuple(column_layout),
            )
        return signatures

    @classmethod
    def _validate_index_allowlist(
        cls,
        connection: sqlite3.Connection,
        table_name: str,
        *,
        expected: dict[str, set[tuple[Any, ...]]],
        required_names: set[str],
    ) -> None:
        actual = cls._index_signatures(connection, table_name)
        if set(actual) - set(expected):
            raise ValueError(
                f"RWA {table_name} contains an unknown or incompatible index"
            )
        if required_names - set(actual):
            raise ValueError(f"RWA {table_name} required index is missing")
        for index_name, signature in actual.items():
            if signature not in expected[index_name]:
                raise ValueError(
                    f"RWA {table_name} contains an unknown or incompatible index"
                )
            sql_row = connection.execute(
                "SELECT sql FROM sqlite_master WHERE type = 'index' AND name = ?",
                (index_name,),
            ).fetchone()
            actual_sql = None if sql_row is None or sql_row[0] is None else str(sql_row[0])
            expected_sql = _RWA_REQUIRED_INDEX_SQL.get(index_name)
            if expected_sql is None:
                if actual_sql is not None:
                    raise ValueError(
                        f"RWA {table_name} contains an unknown or incompatible index"
                    )
            elif actual_sql is None or cls._canonical_sql(actual_sql) != cls._canonical_sql(
                expected_sql
            ):
                raise ValueError(
                    f"RWA {table_name} contains an unknown or incompatible index"
                )

    @classmethod
    def _validate_exact_table(
        cls,
        connection: sqlite3.Connection,
        table_name: str,
        expected: dict[str, tuple[str, int, str | None, int]],
    ) -> None:
        actual = cls._table_columns(connection, table_name)
        if set(actual) != set(expected):
            raise ValueError(f"RWA {table_name} schema is missing or ambiguous")
        mismatches = {name for name, signature in expected.items() if actual.get(name) != signature}
        if mismatches:
            raise ValueError(f"RWA {table_name} column declarations are incompatible")

    @classmethod
    def _validate_observation_table(
        cls,
        connection: sqlite3.Connection,
        *,
        require_current: bool,
    ) -> set[str]:
        actual = cls._table_columns(connection, "rwa_observations")
        actual_names = set(actual)
        base_names = set(_RWA_V1_COLUMN_SIGNATURES)
        additive_names = set(_RWA_ADDITIVE_COLUMN_SIGNATURES)
        if base_names - actual_names or actual_names - base_names - additive_names:
            raise ValueError("RWA observation schema is missing or ambiguous")
        cls._validate_observation_table_sql(connection, actual_names)
        if require_current and additive_names - actual_names:
            raise ValueError("RWA observation schema is incomplete")
        for name, signature in _RWA_V1_COLUMN_SIGNATURES.items():
            if actual.get(name) != signature:
                raise ValueError("RWA v1 observation column declarations are incompatible")
        for name in actual_names & additive_names:
            signature = actual[name]
            expected = _RWA_ADDITIVE_COLUMN_SIGNATURES[name]
            if name == "ingestion_source":
                allowed = {expected, ("TEXT", 0, "'operator'", 0)}
                if signature not in allowed:
                    raise ValueError("RWA ingestion_source declaration is incompatible")
            elif signature != expected:
                raise ValueError(f"RWA {name} declaration is incompatible")
        expected_indexes = {
            "sqlite_autoindex_rwa_observations_1": {_RWA_OBSERVATION_PK_INDEX},
            "idx_rwa_observations_symbol": _RWA_OBSERVATION_SYMBOL_INDEXES,
            "idx_rwa_observations_venue": _RWA_OBSERVATION_VENUE_INDEXES,
            "idx_rwa_observations_created_at": {_RWA_OBSERVATION_CREATED_AT_INDEX},
            "idx_rwa_observations_idempotency": {
                _RWA_OBSERVATION_IDEMPOTENCY_INDEX
            },
        }
        required_index_names = {"sqlite_autoindex_rwa_observations_1"}
        if require_current:
            required_index_names = set(expected_indexes)
        cls._validate_index_allowlist(
            connection,
            "rwa_observations",
            expected=expected_indexes,
            required_names=required_index_names,
        )
        return actual_names

    @classmethod
    def _validate_metadata_table(cls, connection: sqlite3.Connection) -> None:
        cls._validate_exact_table(
            connection,
            "rwa_store_metadata",
            _RWA_METADATA_COLUMN_SIGNATURES,
        )
        cls._validate_table_sql(
            connection,
            "rwa_store_metadata",
            {cls._expected_table_sql("rwa_store_metadata", _RWA_METADATA_COLUMN_DDL)},
        )
        cls._validate_index_allowlist(
            connection,
            "rwa_store_metadata",
            expected={
                "sqlite_autoindex_rwa_store_metadata_1": {_RWA_METADATA_PK_INDEX},
            },
            required_names={"sqlite_autoindex_rwa_store_metadata_1"},
        )

    @classmethod
    def _validate_pilot_table_indexes(
        cls,
        connection: sqlite3.Connection,
        *,
        require_current: bool,
    ) -> None:
        cls._validate_exact_table(
            connection,
            "rwa_pilot_outcomes",
            _RWA_PILOT_COLUMN_SIGNATURES,
        )
        cls._validate_table_sql(
            connection,
            "rwa_pilot_outcomes",
            {
                cls._expected_table_sql(
                    "rwa_pilot_outcomes",
                    _RWA_PILOT_COLUMN_DDL,
                    table_constraints=("UNIQUE(pilot_id, checked_at)",),
                )
            },
        )
        expected_indexes = {
            "sqlite_autoindex_rwa_pilot_outcomes_1": {_RWA_PILOT_PK_INDEX},
            "sqlite_autoindex_rwa_pilot_outcomes_2": {_RWA_PILOT_TIME_UNIQUE_INDEX},
            "idx_rwa_pilot_outcomes_pilot_checked": {_RWA_PILOT_CHECKED_INDEX},
            "idx_rwa_pilot_outcomes_status_checked": {
                _RWA_PILOT_STATUS_CHECKED_INDEX
            },
        }
        required_index_names = {
            "sqlite_autoindex_rwa_pilot_outcomes_1",
            "sqlite_autoindex_rwa_pilot_outcomes_2",
        }
        if require_current:
            required_index_names = set(expected_indexes)
        cls._validate_index_allowlist(
            connection,
            "rwa_pilot_outcomes",
            expected=expected_indexes,
            required_names=required_index_names,
        )

    @classmethod
    def _validate_pilot_table(
        cls,
        connection: sqlite3.Connection,
        *,
        require_current_indexes: bool,
    ) -> None:
        cls._validate_pilot_table_indexes(
            connection,
            require_current=require_current_indexes,
        )

        probe_prefix = f"__rwa_schema_probe_{time.time_ns()}_"
        while connection.execute(
            "SELECT 1 FROM rwa_pilot_outcomes "
            "WHERE substr(outcome_id, 1, ?) = ? OR substr(pilot_id, 1, ?) = ? LIMIT 1",
            (len(probe_prefix), probe_prefix, len(probe_prefix), probe_prefix),
        ).fetchone():
            probe_prefix += "_"

        def insert_probe(
            label: str,
            *,
            status: str = "ok",
            production_promoted: int = 0,
            pilot_id: str | None = None,
            checked_at: str | None = None,
        ) -> None:
            timestamp = checked_at or "1970-01-01T00:00:00+00:00"
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
                    f"{probe_prefix}{label}",
                    "1970-01-01T00:00:00+00:00",
                    "1970-01-01T00:00:00+00:00",
                    timestamp,
                    pilot_id or f"{probe_prefix}{label}",
                    status,
                    "PROBE",
                    "schema_validation",
                    "schema_validation",
                    1.0,
                    "{}",
                    f"sha256:{'0' * 64}",
                    "{}",
                    2,
                    None,
                    None,
                    production_promoted,
                    "growth_pilot",
                ),
            )

        def require_rejection(
            label: str,
            expected_error_code: int,
            error_message: str,
            **overrides: Any,
        ) -> None:
            try:
                insert_probe(label, **overrides)
            except sqlite3.IntegrityError as exc:
                if getattr(exc, "sqlite_errorcode", None) == expected_error_code:
                    return
                raise ValueError(error_message) from exc
            raise ValueError(error_message)

        connection.execute("SAVEPOINT rwa_pilot_constraint_validation")
        try:
            # Both documented statuses must remain writable, while any other
            # status and all production-promotion claims must be rejected by
            # database-level CHECK constraints.
            insert_probe("valid-ok")
            insert_probe("valid-error", status="error")
            require_rejection(
                "invalid-status",
                sqlite3.SQLITE_CONSTRAINT_CHECK,
                "RWA pilot outcome status constraint is missing or incompatible",
                status="unexpected",
            )
            require_rejection(
                "invalid-promotion",
                sqlite3.SQLITE_CONSTRAINT_CHECK,
                "RWA pilot outcome promotion constraint is missing or incompatible",
                production_promoted=1,
            )

            duplicate_pilot_id = f"{probe_prefix}duplicate-pilot"
            duplicate_checked_at = "1970-01-02T00:00:00+00:00"
            insert_probe(
                "duplicate-first",
                pilot_id=duplicate_pilot_id,
                checked_at=duplicate_checked_at,
            )
            require_rejection(
                "duplicate-second",
                sqlite3.SQLITE_CONSTRAINT_UNIQUE,
                "RWA pilot outcome uniqueness constraint is missing or incompatible",
                pilot_id=duplicate_pilot_id,
                checked_at=duplicate_checked_at,
            )
        except sqlite3.IntegrityError as exc:
            raise ValueError("RWA pilot outcome constraints reject valid records") from exc
        finally:
            connection.execute("ROLLBACK TO rwa_pilot_constraint_validation")
            connection.execute("RELEASE rwa_pilot_constraint_validation")

    @staticmethod
    def _metadata_values(connection: sqlite3.Connection) -> dict[str, str] | None:
        table_exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'rwa_store_metadata'"
        ).fetchone()
        if table_exists is None:
            return None
        RWAObservationStore._validate_metadata_table(connection)
        values = {
            str(row[0]): str(row[1])
            for row in connection.execute("SELECT key, value FROM rwa_store_metadata")
        }
        if set(values) - {"schema_version", "migration_required"}:
            raise ValueError("RWA metadata contains unsupported migration state")
        return values

    @classmethod
    def _schema_state(cls, connection: sqlite3.Connection) -> str:
        unexpected_objects = [
            (str(row[0]), str(row[1]))
            for row in connection.execute(
                "SELECT type, name FROM sqlite_master "
                "WHERE type IN ('trigger', 'view') "
                "ORDER BY type, name"
            )
        ]
        if unexpected_objects:
            raise ValueError("RWA database contains unexpected triggers or views")
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' "
                "AND lower(substr(name, 1, 7)) != 'sqlite_'"
            )
        }
        if not tables:
            return "new"
        allowed_tables = {
            "rwa_observations",
            "rwa_store_metadata",
            "rwa_pilot_outcomes",
        }
        if "rwa_observations" not in tables or tables - allowed_tables:
            raise ValueError("RWA database contains an ambiguous table layout")
        cls._validate_observation_table(connection, require_current=False)
        if "rwa_pilot_outcomes" in tables:
            cls._validate_pilot_table(
                connection,
                require_current_indexes=False,
            )
        metadata = cls._metadata_values(connection)
        if not metadata:
            return "v1"
        if set(metadata) != {"schema_version", "migration_required"}:
            raise ValueError("RWA metadata migration state is incomplete")
        try:
            version = int(metadata["schema_version"])
        except ValueError as exc:
            raise ValueError("RWA schema version is malformed") from exc
        migration_required = metadata["migration_required"].strip().lower()
        if migration_required not in {"true", "false"}:
            raise ValueError("RWA migration flag is malformed")
        if version == 1 and migration_required == "true":
            return "v1"
        if version == 2 and migration_required == "false":
            cls._validate_observation_table(connection, require_current=True)
            return "v2"
        if version == RWA_STORE_SCHEMA_VERSION and migration_required == "false":
            cls._validate_observation_table(connection, require_current=True)
            if "rwa_pilot_outcomes" not in tables:
                raise ValueError("RWA v3 pilot outcome table is missing")
            cls._validate_pilot_table_indexes(
                connection,
                require_current=True,
            )
            return "v3"
        raise ValueError("RWA metadata describes an unsupported or ambiguous migration state")

    @staticmethod
    def _create_observation_table(connection: sqlite3.Connection) -> None:
        connection.execute(
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
                idempotency_key TEXT
            )
            """
        )

    @staticmethod
    def _create_metadata_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rwa_store_metadata (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )

    @staticmethod
    def _create_pilot_table(connection: sqlite3.Connection) -> None:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS rwa_pilot_outcomes (
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
                UNIQUE(pilot_id, checked_at)
            )
            """
        )

    @staticmethod
    def _create_required_indexes(connection: sqlite3.Connection) -> None:
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_rwa_observations_symbol ON rwa_observations(symbol)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_rwa_observations_venue ON rwa_observations(venue)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_rwa_observations_created_at "
            "ON rwa_observations(created_at)"
        )
        connection.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_rwa_observations_idempotency "
            "ON rwa_observations(idempotency_key) WHERE idempotency_key IS NOT NULL"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_rwa_pilot_outcomes_pilot_checked "
            "ON rwa_pilot_outcomes(pilot_id, checked_at DESC)"
        )
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_rwa_pilot_outcomes_status_checked "
            "ON rwa_pilot_outcomes(status, checked_at DESC)"
        )

    @staticmethod
    def _validate_required_indexes(connection: sqlite3.Connection) -> None:
        rows = {
            str(row[0]): " ".join(str(row[1] or "").lower().split())
            for row in connection.execute(
                "SELECT name, sql FROM sqlite_master "
                "WHERE type = 'index' AND lower(substr(name, 1, 7)) != 'sqlite_'"
            )
        }
        for name, expected_sql in _RWA_REQUIRED_INDEX_SQL.items():
            if rows.get(name) != expected_sql:
                raise ValueError(f"RWA required index {name} is missing or incompatible")

    @staticmethod
    def _require_unpopulated_v1_additions(
        connection: sqlite3.Connection,
        existing_columns: set[str],
    ) -> None:
        predicates: list[str] = []
        for name in ("observed_at", "ingested_at", "idempotency_key"):
            if name in existing_columns:
                predicates.append(f'"{name}" IS NOT NULL')
        for name in (
            "raw_payload_bytes",
            "normalized_bytes",
            "realtime_quality_bytes",
            "blocksize_benchmark_bytes",
            "promotion_bytes",
            "metadata_bytes",
        ):
            if name in existing_columns:
                predicates.append(f'COALESCE("{name}", 0) != 0')
        if "ingestion_source" in existing_columns:
            predicates.append("COALESCE(ingestion_source, 'legacy') NOT IN ('legacy', 'legacy_v1')")
        if (
            predicates
            and connection.execute(
                f"SELECT 1 FROM rwa_observations WHERE {' OR '.join(predicates)} LIMIT 1"
            ).fetchone()
        ):
            raise ValueError("RWA v1 additive fields contain ambiguous data")

    @staticmethod
    def _decode_legacy_component(raw_value: Any, component_name: str) -> dict[str, Any]:
        if not isinstance(raw_value, str):
            raise ValueError(f"RWA legacy {component_name} is not JSON text")
        try:
            decoded = json.loads(raw_value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"RWA legacy {component_name} is malformed") from exc
        if not isinstance(decoded, dict):
            raise ValueError(f"RWA legacy {component_name} must be an object")
        validate_json_shape(decoded, path=component_name)
        validate_component_size(component_name, decoded)
        return decoded

    @classmethod
    def _legacy_backfill_rows(
        cls,
        connection: sqlite3.Connection,
        *,
        all_rows: bool,
    ) -> list[tuple[Any, ...]]:
        columns = list(_RWA_V1_COLUMN_SIGNATURES)
        where = ""
        if not all_rows:
            where = (
                " WHERE observed_at IS NULL OR ingested_at IS NULL"
                " OR raw_payload_bytes <= 0 OR normalized_bytes <= 0"
                " OR realtime_quality_bytes <= 0 OR blocksize_benchmark_bytes <= 0"
                " OR promotion_bytes <= 0 OR metadata_bytes <= 0"
                " OR ingestion_source IS NULL OR TRIM(ingestion_source) = ''"
            )
        cursor = connection.execute(
            f"SELECT {', '.join(columns)} FROM rwa_observations{where} ORDER BY observation_id"
        )
        prepared: list[tuple[Any, ...]] = []
        for values in cursor:
            row = dict(zip(columns, values))
            observation_id = row["observation_id"]
            if (
                not isinstance(observation_id, str)
                or not observation_id.strip()
                or len(observation_id) > 128
            ):
                raise ValueError("RWA legacy observation_id is malformed")
            created_at_raw = row["created_at"]
            if not isinstance(created_at_raw, str):
                raise ValueError("RWA legacy created_at is malformed")
            created_at = _parse_source_timestamp(created_at_raw)
            components = {
                "raw_payload": cls._decode_legacy_component(row["raw_payload_json"], "raw_payload"),
                "normalized_observation": cls._decode_legacy_component(
                    row["normalized_json"], "normalized_observation"
                ),
                "realtime_quality": cls._decode_legacy_component(
                    row["realtime_quality_json"], "realtime_quality"
                ),
                "blocksize_benchmark": cls._decode_legacy_component(
                    row["blocksize_benchmark_json"], "blocksize_benchmark"
                ),
                "promotion": cls._decode_legacy_component(row["promotion_json"], "promotion"),
                "metadata": cls._decode_legacy_component(row["metadata_json"], "metadata"),
            }
            raw_payload = components["raw_payload"]
            normalized = components["normalized_observation"]
            for stored_name, json_name, uppercase in (
                ("symbol", "symbol", True),
                ("venue", "venue", False),
                ("asset_class", "asset_class", False),
                ("source_type", "source_type", False),
            ):
                stored_value = row[stored_name]
                if not isinstance(stored_value, str) or len(stored_value) > 64:
                    raise ValueError(f"RWA legacy {stored_name} is malformed")
                normalize = str.upper if uppercase else str.lower
                if normalize(stored_value) != stored_value:
                    raise ValueError(f"RWA legacy {stored_name} is not canonical")
                evidence_value = normalized.get(json_name) or raw_payload.get(json_name)
                if evidence_value and normalize(str(evidence_value).strip()) != stored_value:
                    raise ValueError(f"RWA legacy {stored_name} conflicts with evidence")
            if not row["symbol"] or not row["venue"]:
                raise ValueError("RWA legacy identity is incomplete")
            for hash_column, component in (
                ("raw_payload_hash", raw_payload),
                ("normalized_hash", normalized),
            ):
                stored_hash = row[hash_column]
                if (
                    not isinstance(stored_hash, str)
                    or _RWA_LEGACY_HASH_RE.fullmatch(stored_hash) is None
                    or stored_hash != _hash_payload(component)
                ):
                    raise ValueError(f"RWA legacy {hash_column} is invalid")
            observed_source = (
                normalized.get("timestamp") or raw_payload.get("timestamp") or created_at_raw
            )
            observed_at = _parse_source_timestamp(observed_source).isoformat()
            byte_counts = {
                name: validate_component_size(name, component)
                for name, component in components.items()
            }
            prepared.append(
                (
                    observed_at,
                    created_at.isoformat(),
                    byte_counts["raw_payload"],
                    byte_counts["normalized_observation"],
                    byte_counts["realtime_quality"],
                    byte_counts["blocksize_benchmark"],
                    byte_counts["promotion"],
                    byte_counts["metadata"],
                    "legacy_v1",
                    observation_id,
                )
            )
        return prepared

    @staticmethod
    def _apply_legacy_backfills(
        connection: sqlite3.Connection,
        rows: list[tuple[Any, ...]],
    ) -> None:
        connection.executemany(
            """
            UPDATE rwa_observations
            SET observed_at = ?, ingested_at = ?, raw_payload_bytes = ?,
                normalized_bytes = ?, realtime_quality_bytes = ?,
                blocksize_benchmark_bytes = ?, promotion_bytes = ?, metadata_bytes = ?,
                ingestion_source = ?
            WHERE observation_id = ?
            """,
            rows,
        )

    @staticmethod
    def _stamp_current_schema(connection: sqlite3.Connection) -> None:
        connection.execute(
            "INSERT INTO rwa_store_metadata(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (str(RWA_STORE_SCHEMA_VERSION),),
        )
        connection.execute(
            "INSERT INTO rwa_store_metadata(key, value) "
            "VALUES('migration_required', 'false') "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value"
        )

    def _init_db(self) -> None:
        connection = self._connect()
        try:
            # Refuse malformed, foreign, or corrupt databases before any schema
            # mutation. The same checks are repeated under the write lock.
            self._integrity_check(connection)
            preflight_state = self._schema_state(connection)
            if preflight_state == "v1":
                existing = self._validate_observation_table(
                    connection,
                    require_current=False,
                )
                self._require_unpopulated_v1_additions(connection, existing)
                self._legacy_backfill_rows(connection, all_rows=True)
            elif preflight_state in {"v2", "v3"}:
                self._legacy_backfill_rows(connection, all_rows=False)

            connection.execute("BEGIN IMMEDIATE")
            self._integrity_check(connection)
            state = self._schema_state(connection)
            if state != preflight_state:
                raise ValueError("RWA schema changed during migration preflight")
            if state == "new":
                self._create_observation_table(connection)
            elif state == "v1":
                existing = self._validate_observation_table(
                    connection,
                    require_current=False,
                )
                self._require_unpopulated_v1_additions(connection, existing)
                for name, definition in _RWA_ADDITIVE_COLUMN_DDL:
                    if name not in existing:
                        connection.execute(
                            f"ALTER TABLE rwa_observations ADD COLUMN {name} {definition}"
                        )

            self._create_metadata_table(connection)
            self._create_pilot_table(connection)
            rows_before = int(
                connection.execute("SELECT COUNT(*) FROM rwa_observations").fetchone()[0]
            )
            backfills = self._legacy_backfill_rows(
                connection,
                all_rows=state == "v1",
            )
            self._apply_legacy_backfills(connection, backfills)
            rows_after = int(
                connection.execute("SELECT COUNT(*) FROM rwa_observations").fetchone()[0]
            )
            if rows_after != rows_before:
                raise ValueError("RWA migration changed the legacy observation count")
            self._create_required_indexes(connection)
            self._validate_observation_table(connection, require_current=True)
            self._validate_metadata_table(connection)
            self._validate_pilot_table(
                connection,
                require_current_indexes=True,
            )
            self._validate_required_indexes(connection)
            self._integrity_check(connection)

            # Metadata is the final migration write. Any later validation or
            # commit failure rolls the stamp and every additive DDL statement back.
            if state != "v3":
                self._stamp_current_schema(connection)
            if self._schema_state(connection) != "v3":
                raise ValueError("RWA schema did not reach the current version")
            self._integrity_check(connection)
            connection.commit()
            connection.execute("PRAGMA journal_mode = WAL")
        except Exception:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _observation_identity(payload: dict[str, Any]) -> dict[str, str]:
        normalized = payload.get("normalized_observation") or payload.get("observation") or {}
        raw = payload.get("raw_payload") or normalized

        def canonical_value(field: str, *, uppercase: bool = False) -> str:
            normalized_value = str(normalized.get(field) or "").strip()
            raw_value = str(raw.get(field) or "").strip()
            top_level_value = str(payload.get(field) or "").strip()
            value = normalized_value or raw_value or top_level_value
            normalize = str.upper if uppercase else str.lower
            canonical = normalize(value)
            if top_level_value and normalize(top_level_value) != canonical:
                raise ValueError(f"top-level {field} conflicts with normalized evidence")
            if len(canonical) > 64:
                raise ValueError(f"{field} exceeds the 64-character limit")
            return canonical

        symbol = canonical_value("symbol", uppercase=True)
        venue = canonical_value("venue")
        asset_class = canonical_value("asset_class")
        source_type = canonical_value("source_type")
        if not symbol:
            raise ValueError("symbol is required")
        if not venue:
            raise ValueError("venue is required")
        return {
            "symbol": symbol,
            "venue": venue,
            "asset_class": asset_class,
            "source_type": source_type,
        }

    def _prepare_observation(
        self,
        payload: dict[str, Any],
        *,
        ingestion_source: str,
    ) -> dict[str, Any]:
        if not isinstance(payload, dict):
            raise ValueError("observation payload must be an object")
        normalized = payload.get("normalized_observation") or payload.get("observation")
        if not isinstance(normalized, dict) or not normalized:
            raise ValueError("normalized_observation or observation is required")
        raw_payload = payload.get("raw_payload")
        if not isinstance(raw_payload, dict) or not raw_payload:
            raise ValueError("raw_payload is required")
        components = {
            "raw_payload": raw_payload,
            "normalized_observation": normalized,
            "realtime_quality": payload.get("realtime_quality") or {},
            "blocksize_benchmark": payload.get("blocksize_benchmark") or {},
            "promotion": payload.get("promotion") or {},
            "metadata": payload.get("metadata") or {},
        }
        for name, value in components.items():
            if not isinstance(value, dict):
                raise ValueError(f"{name} must be an object")
            validate_json_shape(value, path=name)
        byte_counts = {
            name: validate_component_size(name, value) for name, value in components.items()
        }
        identity = self._observation_identity(payload)
        observed_at = _source_timestamp(payload, normalized, raw_payload)
        ingested_at = _utc_now_iso()
        raw_hash = _hash_payload(raw_payload)
        normalized_hash = _hash_payload(normalized)
        raw_idempotency_key = str(payload.get("idempotency_key") or "").strip() or None
        if raw_idempotency_key is not None and (
            not 8 <= len(raw_idempotency_key) <= 128
            or re.fullmatch(r"[A-Za-z0-9._:-]+", raw_idempotency_key) is None
        ):
            raise ValueError("idempotency_key must be an opaque 8 to 128 character identifier")
        idempotency_key = (
            f"sha256:{hashlib.sha256(raw_idempotency_key.encode()).hexdigest()}"
            if raw_idempotency_key is not None
            else None
        )
        if re.fullmatch(r"[a-z0-9_:-]{1,64}", ingestion_source) is None:
            raise ValueError("invalid server-owned ingestion source")
        identity_basis = {
            **identity,
            "observed_at": observed_at,
            "raw_hash": raw_hash,
            "normalized_hash": normalized_hash,
            "idempotency_key": idempotency_key,
        }
        observation_id = (
            "rwaobs_"
            f"{hashlib.sha256(stable_json_bytes(identity_basis)).hexdigest()[:24]}"
        )
        return {
            "observation_id": observation_id,
            "created_at": ingested_at,
            "observed_at": observed_at,
            "ingested_at": ingested_at,
            **identity,
            "raw_payload_hash": raw_hash,
            "normalized_hash": normalized_hash,
            "raw_payload_json": _stable_json(raw_payload),
            "normalized_json": _stable_json(normalized),
            "realtime_quality_json": _stable_json(components["realtime_quality"]),
            "blocksize_benchmark_json": _stable_json(components["blocksize_benchmark"]),
            "promotion_json": _stable_json(components["promotion"]),
            "metadata_json": _stable_json(components["metadata"]),
            "raw_payload_bytes": byte_counts["raw_payload"],
            "normalized_bytes": byte_counts["normalized_observation"],
            "realtime_quality_bytes": byte_counts["realtime_quality"],
            "blocksize_benchmark_bytes": byte_counts["blocksize_benchmark"],
            "promotion_bytes": byte_counts["promotion"],
            "metadata_bytes": byte_counts["metadata"],
            "ingestion_source": ingestion_source,
            "idempotency_key": idempotency_key,
        }

    @staticmethod
    def _insert_prepared(connection: sqlite3.Connection, record: dict[str, Any]) -> bool:
        columns = tuple(record)
        placeholders = ", ".join("?" for _ in columns)
        cursor = connection.execute(
            f"INSERT INTO rwa_observations ({', '.join(columns)}) "
            f"VALUES ({placeholders}) ON CONFLICT DO NOTHING",
            tuple(record[column] for column in columns),
        )
        if cursor.rowcount:
            return True
        immutable_columns = (
            "observed_at",
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
            "ingestion_source",
        )
        select_columns = (
            "observation_id",
            "created_at",
            "ingested_at",
            *immutable_columns,
        )
        if record["idempotency_key"] is None:
            conflict = connection.execute(
                f"SELECT {', '.join(select_columns)} FROM rwa_observations "
                "WHERE observation_id = ? LIMIT 1",
                (record["observation_id"],),
            ).fetchone()
        else:
            conflict = connection.execute(
                f"SELECT {', '.join(select_columns)} FROM rwa_observations "
                "WHERE observation_id = ? OR idempotency_key = ? LIMIT 1",
                (record["observation_id"], record["idempotency_key"]),
            ).fetchone()
        if conflict is None:
            raise ValueError("evidence conflict could not be resolved")
        stored = dict(zip(select_columns, conflict))
        if any(stored[column] != record[column] for column in immutable_columns):
            raise ValueError("idempotency key conflicts with different evidence")
        record.update(
            {
                "observation_id": stored["observation_id"],
                "created_at": stored["created_at"],
                "ingested_at": stored["ingested_at"],
                "observed_at": stored["observed_at"],
                "symbol": stored["symbol"],
                "venue": stored["venue"],
                "asset_class": stored["asset_class"],
                "source_type": stored["source_type"],
            }
        )
        return False

    def _require_current_schema(self) -> None:
        status = self.schema_status()
        if not status["ready"]:
            raise ValueError("RWA evidence migration is required before writes or detailed reads")

    @staticmethod
    def _public_record(record: dict[str, Any], *, inserted: bool) -> dict[str, Any]:
        return {
            "observation_id": record["observation_id"],
            "created_at": record["created_at"],
            "observed_at": record["observed_at"],
            "ingested_at": record["ingested_at"],
            "symbol": record["symbol"],
            "venue": record["venue"],
            "asset_class": record["asset_class"],
            "source_type": record["source_type"],
            "raw_payload_hash": record["raw_payload_hash"],
            "normalized_hash": record["normalized_hash"],
            "inserted": inserted,
            "replayable": True,
        }

    def store_observation(
        self,
        payload: dict[str, Any],
        *,
        lock_timeout_seconds: float = 30.0,
        deadline_monotonic: float | None = None,
        ingestion_source: str = "operator_api",
    ) -> dict[str, Any]:
        """Validate and idempotently store one observation."""
        self._require_current_schema()
        record = self._prepare_observation(payload, ingestion_source=ingestion_source)
        connection = self._connect(lock_timeout_seconds=lock_timeout_seconds)
        try:
            connection.execute("BEGIN IMMEDIATE")
            inserted = self._insert_prepared(connection, record)
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                raise TimeoutError("RWA evidence write exceeded its deadline")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return self._public_record(record, inserted=inserted)

    def store_observations_batch(
        self,
        payloads: list[dict[str, Any]],
        *,
        lock_timeout_seconds: float = 30.0,
        deadline_monotonic: float | None = None,
        ingestion_source: str = "sourcing_probe",
    ) -> list[dict[str, Any]]:
        """Validate a bounded batch and commit it atomically."""
        self._require_current_schema()
        if not payloads or len(payloads) > 20:
            raise ValueError("observation batch must contain 1 to 20 rows")
        prepared = [
            self._prepare_observation(payload, ingestion_source=ingestion_source)
            for payload in payloads
        ]
        connection = self._connect(lock_timeout_seconds=lock_timeout_seconds)
        inserted_flags: list[bool] = []
        try:
            connection.execute("BEGIN IMMEDIATE")
            for record in prepared:
                inserted_flags.append(self._insert_prepared(connection, record))
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                raise TimeoutError("RWA evidence batch exceeded its deadline")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return [
            self._public_record(record, inserted=inserted)
            for record, inserted in zip(prepared, inserted_flags)
        ]

    @staticmethod
    def _pilot_text(
        capture: dict[str, Any],
        field: str,
        *,
        uppercase: bool = False,
    ) -> str:
        value = str(capture.get(field) or "").strip()
        if not value or len(value) > 64:
            raise ValueError(f"pilot {field} must contain 1 to 64 characters")
        canonical = value.upper() if uppercase else value.lower()
        if field in {"pilot_id", "venue", "source_lane"} and re.fullmatch(
            r"[a-z0-9_:-]+", canonical
        ) is None:
            raise ValueError(f"pilot {field} contains unsupported characters")
        return canonical

    def _prepare_pilot_outcome(self, capture: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(capture, dict):
            raise ValueError("pilot outcome must be an object")
        if capture.get("production_promoted") not in (None, False, 0):
            raise ValueError("growth pilot outcomes cannot mark a feed as production promoted")

        pilot_id = self._pilot_text(capture, "pilot_id")
        symbol = self._pilot_text(capture, "symbol", uppercase=True)
        venue = self._pilot_text(capture, "venue")
        source_lane = self._pilot_text(capture, "source_lane")
        status = str(capture.get("status") or "").strip().lower()
        if status not in {"ok", "error"}:
            raise ValueError("pilot status must be ok or error")

        checked = _parse_source_timestamp(capture.get("checked_at"))
        started = _parse_source_timestamp(capture.get("started_at") or checked)
        if checked > datetime.now(UTC) + timedelta(
            seconds=RWA_PILOT_MAX_FUTURE_SKEW_SECONDS
        ):
            raise ValueError("pilot checked_at is too far in the future")
        if started > checked:
            raise ValueError("pilot started_at cannot be after checked_at")

        try:
            freshness_limit = float(capture.get("freshness_limit_seconds"))
        except (TypeError, ValueError) as exc:
            raise ValueError("pilot freshness_limit_seconds must be numeric") from exc
        if not math.isfinite(freshness_limit) or not 1 <= freshness_limit <= 86_400:
            raise ValueError("pilot freshness_limit_seconds must be between 1 and 86400")

        submitted_checks = capture.get("checks") or {}
        if not isinstance(submitted_checks, dict):
            raise ValueError("pilot checks must be an object")
        validate_json_shape(submitted_checks, path="pilot.checks")

        evidence = capture.get("raw_observation") or {}
        if not isinstance(evidence, dict):
            raise ValueError("pilot raw_observation must be an object")
        if status == "ok" and not evidence:
            raise ValueError("successful pilot outcomes require raw_observation evidence")
        validate_json_shape(evidence, path="pilot.raw_observation")
        evidence_bytes = validate_component_size("raw_payload", evidence)
        if evidence:
            evidence_symbol = str(evidence.get("symbol") or "").strip().upper()
            evidence_venue = str(evidence.get("venue") or "").strip().lower()
            if evidence_symbol and evidence_symbol != symbol:
                raise ValueError("pilot symbol conflicts with raw observation evidence")
            if evidence_venue and evidence_venue != venue:
                raise ValueError("pilot venue conflicts with raw observation evidence")

        derived_checks = _derive_pilot_checks(
            evidence,
            checked_at=checked,
            freshness_limit_seconds=freshness_limit,
        )
        for field in (
            "source_timestamp_pass",
            "freshness_pass",
            "bidask_sanity_pass",
        ):
            if field in submitted_checks and submitted_checks[field] is not derived_checks[field]:
                raise ValueError(
                    f"pilot {field} conflicts with raw observation evidence"
                )
        if "freshness_limit_seconds" in submitted_checks:
            submitted_limit = _finite_number(
                submitted_checks["freshness_limit_seconds"]
            )
            if submitted_limit != freshness_limit:
                raise ValueError(
                    "pilot freshness_limit_seconds conflicts with outcome metadata"
                )
        checks = {**submitted_checks, **derived_checks}
        validate_component_size("realtime_quality", checks)

        error_type = str(capture.get("error_type") or "").strip() or None
        error_message = str(capture.get("message") or "").strip() or None
        if status == "error" and (error_type is None or error_message is None):
            raise ValueError("failed pilot outcomes require error_type and message")
        if status == "ok" and (error_type is not None or error_message is not None):
            raise ValueError("successful pilot outcomes cannot contain error metadata")
        if error_type is not None and (
            len(error_type) > 128 or re.fullmatch(r"[A-Za-z0-9_.:-]+", error_type) is None
        ):
            raise ValueError("pilot error_type contains unsupported characters")
        if error_message is not None and len(error_message) > 1_000:
            raise ValueError("pilot error message exceeds the 1000-character limit")
        validate_json_shape(
            {"error_type": error_type, "message": error_message},
            path="pilot.error",
        )

        checked_at = checked.isoformat()
        started_at = started.isoformat()
        evidence_hash = _hash_payload(evidence)
        identity_basis = {
            "pilot_id": pilot_id,
            "checked_at": checked_at,
        }
        outcome_id = (
            "rwapilot_"
            f"{hashlib.sha256(stable_json_bytes(identity_basis)).hexdigest()[:24]}"
        )
        return {
            "outcome_id": outcome_id,
            "created_at": _utc_now_iso(),
            "started_at": started_at,
            "checked_at": checked_at,
            "pilot_id": pilot_id,
            "status": status,
            "symbol": symbol,
            "venue": venue,
            "source_lane": source_lane,
            "freshness_limit_seconds": freshness_limit,
            "checks_json": _stable_json(checks),
            "evidence_hash": evidence_hash,
            "evidence_json": _stable_json(evidence),
            "evidence_bytes": evidence_bytes,
            "error_type": error_type,
            "error_message": error_message,
            "production_promoted": 0,
            "ingestion_source": "growth_pilot",
        }

    @staticmethod
    def _insert_prepared_pilot_outcome(
        connection: sqlite3.Connection,
        record: dict[str, Any],
    ) -> bool:
        columns = tuple(record)
        placeholders = ", ".join("?" for _ in columns)
        cursor = connection.execute(
            f"INSERT INTO rwa_pilot_outcomes ({', '.join(columns)}) "
            f"VALUES ({placeholders}) ON CONFLICT DO NOTHING",
            tuple(record[column] for column in columns),
        )
        if cursor.rowcount:
            return True
        immutable_columns = tuple(column for column in columns if column != "created_at")
        stored_row = connection.execute(
            f"SELECT created_at, {', '.join(immutable_columns)} "
            "FROM rwa_pilot_outcomes "
            "WHERE outcome_id = ? OR (pilot_id = ? AND checked_at = ?) LIMIT 1",
            (record["outcome_id"], record["pilot_id"], record["checked_at"]),
        ).fetchone()
        if stored_row is None:
            raise ValueError("pilot outcome conflict could not be resolved")
        stored = dict(zip(("created_at", *immutable_columns), stored_row))
        if any(stored[column] != record[column] for column in immutable_columns):
            raise ValueError("pilot outcome conflicts with existing ledger evidence")
        record["created_at"] = stored["created_at"]
        return False

    @staticmethod
    def _public_pilot_outcome(
        record: dict[str, Any],
        *,
        inserted: bool,
        include_evidence: bool = False,
    ) -> dict[str, Any]:
        public = {
            "outcome_id": record["outcome_id"],
            "created_at": record["created_at"],
            "started_at": record["started_at"],
            "checked_at": record["checked_at"],
            "pilot_id": record["pilot_id"],
            "status": record["status"],
            "symbol": record["symbol"],
            "venue": record["venue"],
            "source_lane": record["source_lane"],
            "freshness_limit_seconds": record["freshness_limit_seconds"],
            "checks": json.loads(record["checks_json"] or "{}"),
            "evidence_hash": record["evidence_hash"],
            "evidence_bytes": record["evidence_bytes"],
            "error_type": record["error_type"],
            "message": record["error_message"],
            "production_promoted": False,
            "ingestion_source": record["ingestion_source"],
            "inserted": inserted,
            "replayable": True,
        }
        if include_evidence:
            public["raw_observation"] = json.loads(record["evidence_json"] or "{}")
        return public

    def store_pilot_outcomes(
        self,
        captures: list[dict[str, Any]],
        *,
        lock_timeout_seconds: float = 30.0,
        deadline_monotonic: float | None = None,
    ) -> list[dict[str, Any]]:
        """Atomically record successful and failed growth-pilot attempts."""
        self._require_current_schema()
        if not captures or len(captures) > RWA_PILOT_OUTCOME_BATCH_MAX:
            raise ValueError(
                "pilot outcome batch must contain "
                f"1 to {RWA_PILOT_OUTCOME_BATCH_MAX} rows"
            )
        prepared = [self._prepare_pilot_outcome(capture) for capture in captures]
        inserted_flags: list[bool] = []
        connection = self._connect(lock_timeout_seconds=lock_timeout_seconds)
        try:
            connection.execute("BEGIN IMMEDIATE")
            for record in prepared:
                inserted_flags.append(
                    self._insert_prepared_pilot_outcome(connection, record)
                )
            connection.execute(
                "DELETE FROM rwa_pilot_outcomes WHERE rowid IN ("
                "SELECT rowid FROM rwa_pilot_outcomes "
                "ORDER BY checked_at DESC, outcome_id DESC LIMIT -1 OFFSET ?"
                ")",
                (RWA_PILOT_OUTCOME_HISTORY_MAX,),
            )
            if deadline_monotonic is not None and time.monotonic() >= deadline_monotonic:
                raise TimeoutError("RWA pilot outcome batch exceeded its deadline")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        return [
            self._public_pilot_outcome(record, inserted=inserted)
            for record, inserted in zip(prepared, inserted_flags)
        ]

    def list_pilot_outcomes(
        self,
        *,
        pilot_ids: list[str] | tuple[str, ...] | None = None,
        limit: int = 5_000,
        include_evidence: bool = False,
    ) -> list[dict[str, Any]]:
        """List bounded pilot history from the authoritative SQLite ledger."""
        self._require_current_schema()
        clean_ids = [
            str(pilot_id).strip().lower()
            for pilot_id in (pilot_ids or ())
            if str(pilot_id).strip()
        ]
        if len(clean_ids) > 50 or any(
            re.fullmatch(r"[a-z0-9_:-]{1,64}", pilot_id) is None
            for pilot_id in clean_ids
        ):
            raise ValueError("pilot_ids must contain at most 50 valid identifiers")
        columns = (
            "outcome_id", "created_at", "started_at", "checked_at", "pilot_id",
            "status", "symbol", "venue", "source_lane", "freshness_limit_seconds",
            "checks_json", "evidence_hash", "evidence_json", "evidence_bytes",
            "error_type", "error_message", "production_promoted", "ingestion_source",
        )
        selected_columns = [
            column
            if column != "evidence_json" or include_evidence
            else "'{}' AS evidence_json"
            for column in columns
        ]
        params: list[Any] = []
        where = ""
        if clean_ids:
            where = f"WHERE pilot_id IN ({', '.join('?' for _ in clean_ids)})"
            params.extend(clean_ids)
        maximum = 200 if include_evidence else RWA_PILOT_OUTCOME_HISTORY_MAX
        params.append(max(1, min(int(limit), maximum)))
        connection = self._connect()
        try:
            rows = connection.execute(
                f"SELECT {', '.join(selected_columns)} FROM rwa_pilot_outcomes {where} "
                "ORDER BY checked_at DESC LIMIT ?",
                params,
            ).fetchall()
        finally:
            connection.close()
        return [
            self._public_pilot_outcome(
                dict(zip(columns, row)),
                inserted=False,
                include_evidence=include_evidence,
            )
            for row in rows
        ]

    def pilot_freshness(
        self,
        pilot_ids: list[str] | tuple[str, ...],
        *,
        stale_after_seconds: float,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        """Return fail-closed per-feed freshness from the latest ledger outcomes."""
        if not math.isfinite(float(stale_after_seconds)) or not 1 <= float(
            stale_after_seconds
        ) <= 604_800:
            raise ValueError("stale_after_seconds must be between 1 and 604800")
        requested = [str(pilot_id).strip().lower() for pilot_id in pilot_ids]
        if not requested or len(requested) > 50 or len(set(requested)) != len(requested):
            raise ValueError("pilot_ids must contain 1 to 50 unique identifiers")
        if any(
            re.fullmatch(r"[a-z0-9_:-]{1,64}", pilot_id) is None
            for pilot_id in requested
        ):
            raise ValueError("pilot_ids contains an invalid identifier")

        checked_now = (now or datetime.now(UTC)).astimezone(UTC)
        rows = self.list_pilot_outcomes(
            pilot_ids=requested,
            limit=RWA_PILOT_OUTCOME_HISTORY_MAX,
        )
        latest_by_id: dict[str, dict[str, Any]] = {}
        for row in rows:
            latest_by_id.setdefault(row["pilot_id"], row)

        feeds: list[dict[str, Any]] = []
        for pilot_id in requested:
            latest = latest_by_id.get(pilot_id)
            checked_at = (
                _parse_source_timestamp(latest["checked_at"])
                if latest is not None
                else None
            )
            age_seconds = (
                max(0.0, (checked_now - checked_at).total_seconds())
                if checked_at is not None
                else None
            )
            stale = age_seconds is None or age_seconds > float(stale_after_seconds)
            checks = latest.get("checks", {}) if latest is not None else {}
            healthy = bool(
                latest is not None
                and latest["status"] == "ok"
                and not stale
                and checks.get("freshness_pass") is True
                and checks.get("bidask_sanity_pass") is True
            )
            feeds.append(
                {
                    "pilot_id": pilot_id,
                    "last_checked_at": checked_at.isoformat() if checked_at else None,
                    "age_seconds": round(age_seconds, 3) if age_seconds is not None else None,
                    "stale": stale,
                    "last_status": latest["status"] if latest is not None else "missing",
                    "healthy": healthy,
                    "last_outcome_id": latest["outcome_id"] if latest is not None else None,
                }
            )
        stale_ids = [row["pilot_id"] for row in feeds if row["stale"]]
        unhealthy_ids = [row["pilot_id"] for row in feeds if not row["healthy"]]
        alert_status = (
            "not_started"
            if all(row["last_status"] == "missing" for row in feeds)
            else "stale"
            if stale_ids
            else "degraded"
            if unhealthy_ids
            else "healthy"
        )
        return {
            "source_of_truth": "rwa_observation_store",
            "generated_at": checked_now.isoformat(),
            "stale_after_seconds": float(stale_after_seconds),
            "status": alert_status,
            "ready": not unhealthy_ids,
            "stale_pilot_ids": stale_ids,
            "unhealthy_pilot_ids": unhealthy_ids,
            "feeds": feeds,
        }

    def schema_status(
        self,
        *,
        force: bool = False,
        deadline_seconds: float = 2.0,
    ) -> dict[str, Any]:
        """Return bounded structural integrity metadata for readiness checks."""
        if (
            not force
            and self._schema_status_cache is not None
            and time.monotonic() - self._schema_status_checked_at < 60.0
        ):
            return dict(self._schema_status_cache)
        deadline = time.monotonic() + max(0.1, min(float(deadline_seconds), 10.0))
        connection = self._connect(lock_timeout_seconds=min(deadline_seconds, 2.0))
        connection.set_progress_handler(
            lambda: 1 if time.monotonic() >= deadline else 0,
            1_000,
        )
        try:
            observation_columns = self._validate_observation_table(
                connection,
                require_current=True,
            )
            self._validate_metadata_table(connection)
            self._validate_pilot_table_indexes(
                connection,
                require_current=True,
            )
            self._validate_required_indexes(connection)
            pilot_columns = set(_RWA_PILOT_COLUMN_SIGNATURES)
            row = connection.execute(
                "SELECT value FROM rwa_store_metadata WHERE key = 'schema_version'"
            ).fetchone()
            migration_row = connection.execute(
                "SELECT value FROM rwa_store_metadata WHERE key = 'migration_required'"
            ).fetchone()
            required_columns = {
                "observation_id",
                "observed_at",
                "ingested_at",
                "raw_payload_bytes",
                "normalized_bytes",
                "metadata_bytes",
                "idempotency_key",
            }
            required_pilot_columns = {
                "outcome_id",
                "started_at",
                "checked_at",
                "pilot_id",
                "status",
                "checks_json",
                "evidence_hash",
                "evidence_json",
                "error_type",
                "error_message",
                "production_promoted",
            }
            missing_columns = sorted(
                [
                    f"rwa_observations.{column}"
                    for column in required_columns - observation_columns
                ]
                + [
                    f"rwa_pilot_outcomes.{column}"
                    for column in required_pilot_columns - pilot_columns
                ]
            )
            # Table-scoped quick_check validates the small metadata b-tree. Deep
            # whole-database verification belongs in an offline maintenance job.
            quick_check = connection.execute(
                "PRAGMA quick_check('rwa_store_metadata')"
            ).fetchone()
        except (sqlite3.Error, ValueError):
            result = {
                "ready": False,
                "schema_version": 0,
                "integrity": (
                    "timeout" if time.monotonic() >= deadline else "unavailable"
                ),
                "migration_required": True,
                "invalid_or_legacy_rows": 1,
                "missing_columns": [],
            }
            self._schema_status_cache = result
            self._schema_status_checked_at = time.monotonic()
            return dict(result)
        finally:
            connection.set_progress_handler(None, 0)
            connection.close()
        version = int(row[0]) if row else 0
        integrity = str(quick_check[0]) if quick_check else "unknown"
        migration_required = not migration_row or str(migration_row[0]).lower() == "true"
        invalid_rows = 1 if migration_required else 0
        result = {
            "ready": version == RWA_STORE_SCHEMA_VERSION
            and integrity == "ok"
            and not migration_required
            and not missing_columns,
            "schema_version": version,
            "integrity": integrity,
            "migration_required": migration_required,
            "invalid_or_legacy_rows": invalid_rows,
            "missing_columns": missing_columns,
        }
        self._schema_status_cache = result
        self._schema_status_checked_at = time.monotonic()
        return dict(result)

    def list_observations(
        self,
        *,
        symbol: str | None = None,
        venue: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List recent observation summaries without raw stored evidence."""
        self._require_current_schema()
        clauses: list[str] = []
        params: list[Any] = []
        if symbol:
            clauses.append("symbol = ?")
            params.append(symbol.upper())
        if venue:
            clauses.append("venue = ?")
            params.append(venue.lower())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = (
            "SELECT observation_id, created_at, observed_at, ingested_at, symbol, venue, "
            "asset_class, source_type, raw_payload_hash, normalized_hash, "
            "realtime_quality_json, blocksize_benchmark_json, promotion_json, metadata_json "
            f"FROM rwa_observations {where} ORDER BY created_at DESC LIMIT ?"
        )
        params.append(max(1, min(limit, 200)))
        connection = self._connect()
        try:
            rows = connection.execute(query, params).fetchall()
        finally:
            connection.close()
        return [
            {
                "observation_id": row[0],
                "created_at": row[1],
                "observed_at": row[2],
                "ingested_at": row[3],
                "symbol": row[4],
                "venue": row[5],
                "asset_class": row[6],
                "source_type": row[7],
                "raw_payload_hash": row[8],
                "normalized_hash": row[9],
                "realtime_quality": json.loads(row[10] or "{}"),
                "blocksize_benchmark": json.loads(row[11] or "{}"),
                "promotion": json.loads(row[12] or "{}"),
                "metadata": json.loads(row[13] or "{}"),
            }
            for row in rows
        ]

    def summary(self) -> dict[str, Any]:
        """Return compact persistence stats."""
        connection = self._connect()
        try:
            total = connection.execute("SELECT COUNT(*) FROM rwa_observations").fetchone()[0]
            venues = connection.execute(
                "SELECT venue, COUNT(*) FROM rwa_observations GROUP BY venue "
                "ORDER BY COUNT(*) DESC LIMIT 50"
            ).fetchall()
            venue_count = connection.execute(
                "SELECT COUNT(DISTINCT venue) FROM rwa_observations"
            ).fetchone()[0]
            symbols = connection.execute(
                "SELECT symbol, COUNT(*) FROM rwa_observations GROUP BY symbol "
                "ORDER BY COUNT(*) DESC LIMIT 20"
            ).fetchall()
            latest = connection.execute(
                "SELECT MAX(created_at) FROM rwa_observations"
            ).fetchone()[0]
        finally:
            connection.close()
        return {
            "total_observations": total,
            "latest_observation_at": latest,
            "by_venue": {row[0]: row[1] for row in venues},
            "venues_truncated": venue_count > len(venues),
            "top_symbols": {row[0]: row[1] for row in symbols},
        }
