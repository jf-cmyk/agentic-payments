"""Persistence for replayable RWA sourcing observations."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import UTC, datetime
from typing import Any


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _stable_json(payload: Any) -> str:
    return json.dumps(payload, default=str, sort_keys=True, separators=(",", ":"))


def _hash_payload(payload: Any) -> str:
    return f"sha256:{hashlib.sha256(_stable_json(payload).encode()).hexdigest()}"


class RWAObservationStore:
    """SQLite-backed store for RWA observation audit and replay records."""

    def __init__(self, db_path: str | None = None) -> None:
        self.db_path = db_path or os.environ.get("RWA_OBSERVATION_DB_PATH", "rwa_observations.db")
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _init_db(self) -> None:
        conn = self._connect()
        try:
            cursor = conn.cursor()
            cursor.execute(
                """
                CREATE TABLE IF NOT EXISTS rwa_observations (
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
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_rwa_observations_symbol ON rwa_observations(symbol)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_rwa_observations_venue ON rwa_observations(venue)"
            )
            cursor.execute(
                "CREATE INDEX IF NOT EXISTS idx_rwa_observations_created_at ON rwa_observations(created_at)"
            )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _observation_identity(payload: dict[str, Any]) -> dict[str, str]:
        normalized = payload.get("normalized_observation")
        if not isinstance(normalized, dict):
            normalized = payload.get("observation")
        if not isinstance(normalized, dict):
            normalized = {}
        raw = payload.get("raw_payload")
        if not isinstance(raw, dict):
            raw = normalized
        symbol = str(
            payload.get("symbol")
            or normalized.get("symbol")
            or raw.get("symbol")
            or ""
        ).upper()
        venue = str(
            payload.get("venue")
            or normalized.get("venue")
            or raw.get("venue")
            or "unknown"
        ).lower()
        asset_class = str(
            payload.get("asset_class")
            or normalized.get("asset_class")
            or raw.get("asset_class")
            or ""
        ).lower()
        source_type = str(
            payload.get("source_type")
            or normalized.get("source_type")
            or raw.get("source_type")
            or ""
        ).lower()
        if not symbol:
            raise ValueError("symbol is required")
        return {
            "symbol": symbol,
            "venue": venue,
            "asset_class": asset_class,
            "source_type": source_type,
        }

    def store_observation(self, payload: dict[str, Any]) -> dict[str, Any]:
        """Store one replayable RWA observation record."""
        normalized = payload.get("normalized_observation")
        if not isinstance(normalized, dict):
            normalized = payload.get("observation")
        if not isinstance(normalized, dict):
            raise ValueError("normalized_observation or observation is required")
        raw_payload = payload.get("raw_payload")
        if not isinstance(raw_payload, dict):
            raw_payload = normalized
        realtime_quality = payload.get("realtime_quality") if isinstance(payload.get("realtime_quality"), dict) else {}
        blocksize_benchmark = (
            payload.get("blocksize_benchmark")
            if isinstance(payload.get("blocksize_benchmark"), dict)
            else {}
        )
        promotion = payload.get("promotion") if isinstance(payload.get("promotion"), dict) else {}
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        identity = self._observation_identity(payload)
        raw_hash = _hash_payload(raw_payload)
        normalized_hash = _hash_payload(normalized)
        basis = {
            **identity,
            "raw_hash": raw_hash,
            "normalized_hash": normalized_hash,
            "created_at_hint": payload.get("created_at") or "",
        }
        observation_id = f"rwaobs_{hashlib.sha256(_stable_json(basis).encode()).hexdigest()[:24]}"
        created_at = str(payload.get("created_at") or _utc_now_iso())

        conn = self._connect()
        try:
            conn.execute(
                """
                INSERT OR REPLACE INTO rwa_observations (
                    observation_id, created_at, symbol, venue, asset_class, source_type,
                    raw_payload_hash, normalized_hash, raw_payload_json, normalized_json,
                    realtime_quality_json, blocksize_benchmark_json, promotion_json, metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    observation_id,
                    created_at,
                    identity["symbol"],
                    identity["venue"],
                    identity["asset_class"],
                    identity["source_type"],
                    raw_hash,
                    normalized_hash,
                    _stable_json(raw_payload),
                    _stable_json(normalized),
                    _stable_json(realtime_quality),
                    _stable_json(blocksize_benchmark),
                    _stable_json(promotion),
                    _stable_json(metadata),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        return {
            "observation_id": observation_id,
            "created_at": created_at,
            **identity,
            "raw_payload_hash": raw_hash,
            "normalized_hash": normalized_hash,
            "replayable": True,
        }

    def list_observations(
        self,
        *,
        symbol: str | None = None,
        venue: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        """List recent observation records with bounded detail."""
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
            "SELECT observation_id, created_at, symbol, venue, asset_class, source_type, "
            "raw_payload_hash, normalized_hash, realtime_quality_json, blocksize_benchmark_json, "
            "promotion_json, metadata_json FROM rwa_observations "
            f"{where} ORDER BY created_at DESC LIMIT ?"
        )
        params.append(max(1, min(limit, 200)))
        conn = self._connect()
        try:
            rows = conn.execute(query, params).fetchall()
        finally:
            conn.close()
        return [
            {
                "observation_id": row[0],
                "created_at": row[1],
                "symbol": row[2],
                "venue": row[3],
                "asset_class": row[4],
                "source_type": row[5],
                "raw_payload_hash": row[6],
                "normalized_hash": row[7],
                "realtime_quality": json.loads(row[8] or "{}"),
                "blocksize_benchmark": json.loads(row[9] or "{}"),
                "promotion": json.loads(row[10] or "{}"),
                "metadata": json.loads(row[11] or "{}"),
            }
            for row in rows
        ]

    def summary(self) -> dict[str, Any]:
        """Return compact persistence stats."""
        conn = self._connect()
        try:
            total = conn.execute("SELECT COUNT(*) FROM rwa_observations").fetchone()[0]
            venues = conn.execute(
                "SELECT venue, COUNT(*) FROM rwa_observations GROUP BY venue ORDER BY COUNT(*) DESC"
            ).fetchall()
            symbols = conn.execute(
                "SELECT symbol, COUNT(*) FROM rwa_observations GROUP BY symbol ORDER BY COUNT(*) DESC LIMIT 20"
            ).fetchall()
            latest = conn.execute("SELECT MAX(created_at) FROM rwa_observations").fetchone()[0]
        finally:
            conn.close()
        return {
            "total_observations": total,
            "latest_observation_at": latest,
            "by_venue": {row[0]: row[1] for row in venues},
            "top_symbols": {row[0]: row[1] for row in symbols},
        }
