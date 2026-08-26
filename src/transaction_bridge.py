"""One-release economic write lock for the v0.6.2 to v0.6.5 bridge."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping


LEGACY_TRANSACTION_BRIDGE_ENV = "LEGACY_TRANSACTION_BRIDGE_LOCK"
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", ""})
_PAYMENT_PROOF_STATES = (
    "pending",
    "settled",
    "settlement_unknown",
    "released",
    "finalized",
)
_CREDIT_LEGACY_PROJECTIONS = {
    "wallets": ("address", "balance_credits", "last_updated"),
    "credit_purchases": (
        "tx_hash",
        "address",
        "amount_usdc",
        "credits_added",
        "timestamp",
    ),
    "trial_history": (
        "ip_hash",
        "address",
        "funding_address",
        "timestamp",
        "subject_hash",
        "subject_type",
        "device_hash",
        "session_hash",
        "user_agent_hash",
    ),
    "payment_proofs": (
        "tx_hash",
        "network",
        "amount_atomic",
        "recipient",
        "purpose",
        "timestamp",
    ),
    "price_receipts": (
        "receipt_id",
        "product",
        "subject",
        "payload_json",
        "created_at",
    ),
}
_CONNECTOR_LEGACY_PROJECTIONS = {
    "users": (
        "user_id",
        "email",
        "daily_limit",
        "status",
        "created_at",
        "updated_at",
    ),
    "daily_usage": ("user_id", "usage_date", "credits_spent", "updated_at"),
    "usage_events": (
        "id",
        "user_id",
        "usage_date",
        "tool_name",
        "subject",
        "credits_delta",
        "credits_remaining",
        "outcome",
        "created_at",
    ),
}
_INSPECTION_TIMEOUT_SECONDS = 5.0


def legacy_transaction_bridge_lock_status() -> dict[str, Any]:
    """Return a fail-closed interpretation of the bridge-lock environment value."""
    raw = os.environ.get(LEGACY_TRANSACTION_BRIDGE_ENV, "")
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return {
            "configured": bool(normalized),
            "configuration_valid": True,
            "economic_writes_locked": True,
            "mode": "locked",
        }
    if normalized in _FALSE_VALUES:
        return {
            "configured": bool(normalized),
            "configuration_valid": True,
            "economic_writes_locked": False,
            "mode": "unlocked",
        }
    return {
        "configured": True,
        "configuration_valid": False,
        "economic_writes_locked": True,
        "mode": "invalid_locked",
        "reason": "invalid_legacy_transaction_bridge_lock",
    }


def economic_writes_locked() -> bool:
    """Fail closed for malformed bridge configuration."""
    return bool(legacy_transaction_bridge_lock_status()["economic_writes_locked"])


def _read_only_connection(path: str | Path) -> sqlite3.Connection:
    resolved = Path(path).expanduser().resolve(strict=True)
    conn = sqlite3.connect(f"file:{resolved.as_posix()}?mode=ro", uri=True, timeout=5)
    deadline = time.monotonic() + _INSPECTION_TIMEOUT_SECONDS

    def progress() -> int:
        return int(time.monotonic() >= deadline)

    conn.set_progress_handler(progress, 1_000)
    return conn


def _legacy_business_fingerprint(
    conn: sqlite3.Connection,
    projections: Mapping[str, tuple[str, ...]],
) -> str:
    """Hash every v0.6.2 economic column, excluding additive candidate DDL."""
    digest = hashlib.sha256(b"blocksize-legacy-business-v1\n")
    deadline = time.monotonic() + _INSPECTION_TIMEOUT_SECONDS
    for table_name, columns in projections.items():
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table_name,),
        ).fetchone()
        available = {
            str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table_name}")')
        }
        if table is None or not set(columns).issubset(available):
            raise sqlite3.DatabaseError(
                f"legacy business projection {table_name} is missing"
            )
        projection = ", ".join(f'"{column}"' for column in columns)
        order = ", ".join(f'"{column}"' for column in columns)
        digest.update(
            json.dumps(
                {"table": table_name, "columns": list(columns)},
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        )
        digest.update(b"\n")
        rows = conn.execute(
            f'SELECT {projection} FROM "{table_name}" ORDER BY {order}'
        )
        for row in rows:
            if time.monotonic() >= deadline:
                raise sqlite3.OperationalError("legacy business fingerprint timed out")
            digest.update(
                json.dumps(
                    list(row),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    allow_nan=False,
                ).encode("utf-8")
            )
            digest.update(b"\n")
    return digest.hexdigest()


def _payment_proof_evidence(path: str | Path) -> tuple[dict[str, int], str]:
    with _read_only_connection(path) as conn:
        conn.execute("BEGIN")
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'payment_proofs'"
        ).fetchone()
        if table is None:
            raise sqlite3.DatabaseError("payment_proofs table is missing")
        counts = {
            state: int(
                conn.execute(
                    "SELECT COUNT(*) FROM payment_proofs WHERE state = ?",
                    (state,),
                ).fetchone()[0]
            )
            for state in _PAYMENT_PROOF_STATES
        }
        total = int(conn.execute("SELECT COUNT(*) FROM payment_proofs").fetchone()[0])
        known = sum(counts.values())
        finalized_cached = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM payment_proofs
                WHERE state = 'finalized'
                  AND response_status IS NOT NULL
                  AND response_headers_json IS NOT NULL
                  AND response_body IS NOT NULL
                """
            ).fetchone()[0]
        )
        recent_finalized_cached = int(
            conn.execute(
                """
                SELECT COUNT(*) FROM payment_proofs
                WHERE state = 'finalized'
                  AND response_status IS NOT NULL
                  AND response_headers_json IS NOT NULL
                  AND response_body IS NOT NULL
                  AND finalized_at IS NOT NULL
                  AND julianday(finalized_at) >= julianday('now', '-24 hours')
                """
            ).fetchone()[0]
        )
        fingerprint = _legacy_business_fingerprint(conn, _CREDIT_LEGACY_PROJECTIONS)
    return (
        {
            "total": total,
            **counts,
            "unknown": total - known,
            "finalized_cached_responses": finalized_cached,
            "recent_finalized_cached_responses": recent_finalized_cached,
        },
        fingerprint,
    )


def _connector_direct_evidence(path: str | Path) -> dict[str, int | str]:
    with _read_only_connection(path) as conn:
        conn.execute("BEGIN")
        table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'credit_charges'"
        ).fetchone()
        if table is None:
            raise sqlite3.DatabaseError("credit_charges table is missing")
        usage_table = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'daily_usage'"
        ).fetchone()
        if usage_table is None:
            raise sqlite3.DatabaseError("daily_usage table is missing")
        pending = int(
            conn.execute(
                "SELECT COUNT(*) FROM credit_charges WHERE state = 'pending'"
            ).fetchone()[0]
        )
        usage_row = conn.execute(
            "SELECT COUNT(*), COALESCE(SUM(credits_spent), 0) FROM daily_usage"
        ).fetchone()
        fingerprint = _legacy_business_fingerprint(
            conn,
            _CONNECTOR_LEGACY_PROJECTIONS,
        )
    return {
        "pending_charges": pending,
        "daily_usage_row_count": int(usage_row[0]),
        "daily_usage_credits_spent_total": int(usage_row[1]),
        "legacy_business_fingerprint": fingerprint,
    }


def transaction_bridge_readiness(
    credit_db_path: str | Path,
    connector_db_paths: Mapping[str, str | Path],
) -> dict[str, Any]:
    """Directly count transient economic rows for the locked bridge release.

    This performs read-only SQLite inspection. The result is intended to be cached
    by the regular readiness probe loop rather than recomputed on every HTTP probe.
    """
    lock = legacy_transaction_bridge_lock_status()
    connector_counts: dict[str, int] = {}
    connector_usage: dict[str, dict[str, int]] = {}
    try:
        payment_counts, credit_db_fingerprint = _payment_proof_evidence(credit_db_path)
        connector_fingerprints: dict[str, str] = {}
        for name, path in sorted(connector_db_paths.items()):
            connector = _connector_direct_evidence(path)
            connector_counts[name] = int(connector["pending_charges"])
            connector_usage[name] = {
                "row_count": int(connector["daily_usage_row_count"]),
                "credits_spent_total": int(connector[
                    "daily_usage_credits_spent_total"
                ]),
            }
            connector_fingerprints[name] = str(
                connector["legacy_business_fingerprint"]
            )
    except (OSError, sqlite3.Error, ValueError) as exc:
        return {
            **lock,
            "ready": False,
            "checked": True,
            "direct_counts": None,
            "blockers": ["economic_ledger_direct_inspection_failed"],
            "reason": type(exc).__name__,
        }

    connector_pending = sum(connector_counts.values())
    blockers: list[str] = []
    if not lock["configuration_valid"]:
        blockers.append("bridge_lock_configuration_invalid")
    if lock["economic_writes_locked"]:
        if connector_pending:
            blockers.append("connector_pending_charges_not_drained")
        if any(
            payment_counts[state]
            for state in ("pending", "settled", "settlement_unknown")
        ):
            blockers.append("payment_proof_transient_states_not_drained")
        if payment_counts["unknown"]:
            blockers.append("payment_proof_unknown_states_present")

    direct_counts = {
        "connector_pending_charges": connector_pending,
        "connector_pending_charges_by_connector": connector_counts,
        "connector_daily_usage": connector_usage,
        "database_fingerprints": {
            "creditDb": credit_db_fingerprint,
            "connectors": connector_fingerprints,
        },
        "payment_proofs": payment_counts,
    }
    return {
        **lock,
        "ready": not blockers,
        "checked": True,
        "direct_counts": direct_counts,
        "blockers": blockers,
        "reason": None if not blockers else blockers[0],
    }
