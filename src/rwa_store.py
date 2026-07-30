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
    def _ensure_column(
        connection: sqlite3.Connection,
        existing: set[str],
        name: str,
        definition: str,
    ) -> None:
        if name not in existing:
            connection.execute(f"ALTER TABLE rwa_observations ADD COLUMN {name} {definition}")

    def _init_db(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode = WAL")
            table_preexisting = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'rwa_observations'"
            ).fetchone() is not None
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS rwa_store_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS rwa_observations (
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
            existing = {
                str(row[1])
                for row in connection.execute("PRAGMA table_info(rwa_observations)").fetchall()
            }
            for name, definition in (
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
            ):
                self._ensure_column(connection, existing, name, definition)
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
            current_version_row = connection.execute(
                "SELECT value FROM rwa_store_metadata WHERE key = 'schema_version'"
            ).fetchone()
            migration_row = connection.execute(
                "SELECT value FROM rwa_store_metadata WHERE key = 'migration_required'"
            ).fetchone()
            if current_version_row:
                try:
                    schema_version = int(current_version_row[0])
                except (TypeError, ValueError):
                    schema_version = 1
            elif table_preexisting:
                # A pre-v2 table has no metadata row. Adding nullable columns
                # does not make its historical evidence replay-safe, so retain
                # the legacy version and require the explicit migration.
                schema_version = 1
            else:
                schema_version = RWA_STORE_SCHEMA_VERSION
            if (
                schema_version == 2
                and migration_row is not None
                and str(migration_row[0]).lower() == "false"
            ):
                # v3 is an additive pilot-outcome table. A healthy v2 evidence
                # store can be upgraded in place without rewriting observations.
                schema_version = RWA_STORE_SCHEMA_VERSION
            if not table_preexisting:
                schema_version = RWA_STORE_SCHEMA_VERSION
            migration_required = (
                table_preexisting
                and (
                    schema_version != RWA_STORE_SCHEMA_VERSION
                    or migration_row is None
                    or str(migration_row[0]).lower() == "true"
                )
            )
            connection.execute(
                "INSERT INTO rwa_store_metadata(key, value) VALUES('schema_version', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                (str(schema_version),),
            )
            connection.execute(
                "INSERT INTO rwa_store_metadata(key, value) VALUES('migration_required', ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                ("true" if migration_required else "false",),
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_rwa_observations_symbol "
                "ON rwa_observations(symbol)"
            )
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_rwa_observations_venue "
                "ON rwa_observations(venue)"
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
            connection.commit()
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
            row = connection.execute(
                "SELECT value FROM rwa_store_metadata WHERE key = 'schema_version'"
            ).fetchone()
            migration_row = connection.execute(
                "SELECT value FROM rwa_store_metadata WHERE key = 'migration_required'"
            ).fetchone()
            observation_columns = {
                str(item[1])
                for item in connection.execute(
                    "PRAGMA table_info(rwa_observations)"
                ).fetchall()
            }
            required_columns = {
                "observation_id",
                "observed_at",
                "ingested_at",
                "raw_payload_bytes",
                "normalized_bytes",
                "metadata_bytes",
                "idempotency_key",
            }
            pilot_columns = {
                str(item[1])
                for item in connection.execute(
                    "PRAGMA table_info(rwa_pilot_outcomes)"
                ).fetchall()
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
        except sqlite3.Error:
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
