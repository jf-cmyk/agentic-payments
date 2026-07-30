#!/usr/bin/env python3
"""Build a read-only migration plan for historical synthetic telemetry."""

from __future__ import annotations

import argparse
import json
import sqlite3
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.observability import fingerprint


def _metadata(raw: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def audit_history(
    db_path: str | Path,
    *,
    known_test_identities: list[str],
) -> dict[str, Any]:
    known_hashes = {fingerprint(identity) for identity in known_test_identities}
    with sqlite3.connect(str(db_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            """
            SELECT id, timestamp, event, surface, wallet_hash, metadata_json
            FROM usage_events
            ORDER BY id ASC
            """
        ).fetchall()

    candidates: list[dict[str, Any]] = []
    already_tagged = 0
    for raw in rows:
        row = dict(raw)
        metadata = _metadata(row.pop("metadata_json"))
        tagged = bool(metadata.get("synthetic") or metadata.get("test"))
        if tagged:
            already_tagged += 1
        identity_hash = str(metadata.get("identity_hash") or "")
        wallet_hash = str(row.get("wallet_hash") or "")
        matches_known_fixture = bool(
            known_hashes and ({identity_hash, wallet_hash} & known_hashes)
        )
        if not matches_known_fixture or tagged:
            continue
        candidates.append(
            {
                "id": row["id"],
                "timestamp": row["timestamp"],
                "event": row["event"],
                "surface": row["surface"],
                "reason": "known_test_identity_hash",
                "proposed_metadata_patch": {
                    "synthetic": True,
                    "synthetic_reason": "known_test_identity_hash",
                },
            }
        )

    return {
        "mode": "read_only_migration_plan",
        "database": str(db_path),
        "total_events": len(rows),
        "already_tagged_events": already_tagged,
        "known_test_identity_count": len(known_test_identities),
        "untagged_candidate_events": len(candidates),
        "candidate_event_mix": dict(Counter(row["event"] for row in candidates)),
        "candidate_surface_mix": dict(Counter(row["surface"] for row in candidates)),
        "candidates": candidates,
        "next_step": (
            "No untagged fixture events remain."
            if not candidates
            else "Review this plan, back up the database, then apply a separately approved reversible migration."
        ),
    }


def apply_history_tags(
    db_path: str | Path,
    *,
    known_test_identities: list[str],
    backup_dir: str | Path,
) -> dict[str, Any]:
    """Back up the database and tag only reviewed fixture-identity rows."""
    db_path = Path(db_path)
    backup_dir = Path(backup_dir)
    backup_dir.mkdir(parents=True, exist_ok=True)
    migration_id = datetime.now(UTC).strftime("synthetic-fixtures-%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"{db_path.stem}.{migration_id}.sqlite"
    manifest_path = backup_dir / f"{migration_id}.manifest.json"
    known_hashes = {fingerprint(identity) for identity in known_test_identities}

    plan = audit_history(db_path, known_test_identities=known_test_identities)
    candidate_ids = [int(row["id"]) for row in plan["candidates"]]

    previous_rows: list[dict[str, Any]] = []
    with sqlite3.connect(str(db_path)) as conn:
        with sqlite3.connect(str(backup_path)) as backup_conn:
            conn.backup(backup_conn)

        conn.execute("BEGIN IMMEDIATE")
        for event_id in candidate_ids:
            row = conn.execute(
                "SELECT wallet_hash, metadata_json FROM usage_events WHERE id = ?",
                (event_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"Candidate event {event_id} disappeared before migration")
            raw_metadata = row[1] or "{}"
            metadata = _metadata(raw_metadata)
            identity_hash = str(metadata.get("identity_hash") or "")
            wallet_hash = str(row[0] or "")
            if not ({identity_hash, wallet_hash} & known_hashes):
                raise RuntimeError(f"Candidate event {event_id} no longer matches a fixture")
            if metadata.get("synthetic") or metadata.get("test"):
                continue
            previous_rows.append({"id": event_id, "metadata_json": raw_metadata})
            metadata.update(
                {
                    "synthetic": True,
                    "synthetic_reason": "known_test_identity_hash",
                    "synthetic_migration_id": migration_id,
                }
            )
            conn.execute(
                "UPDATE usage_events SET metadata_json = ? WHERE id = ?",
                (json.dumps(metadata, separators=(",", ":"), sort_keys=True), event_id),
            )
        conn.commit()

    manifest = {
        "migration_id": migration_id,
        "database": str(db_path),
        "backup": str(backup_path),
        "updated_events": len(previous_rows),
        "previous_rows": previous_rows,
    }
    manifest_path.write_text(f"{json.dumps(manifest, indent=2)}\n", encoding="utf-8")
    verification = audit_history(db_path, known_test_identities=known_test_identities)
    if verification["untagged_candidate_events"] != 0:
        raise RuntimeError("Migration verification found remaining untagged fixture events")
    return {
        "mode": "applied_reversible_migration",
        "migration_id": migration_id,
        "database": str(db_path),
        "backup": str(backup_path),
        "manifest": str(manifest_path),
        "updated_events": len(previous_rows),
        "remaining_candidates": verification["untagged_candidate_events"],
    }


def revert_history_tags(db_path: str | Path, *, manifest_path: str | Path) -> dict[str, Any]:
    """Restore metadata changed by one migration without replacing newer events."""
    manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    migration_id = str(manifest["migration_id"])
    previous_rows = manifest.get("previous_rows", [])
    restored = 0
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for previous in previous_rows:
            event_id = int(previous["id"])
            row = conn.execute(
                "SELECT metadata_json FROM usage_events WHERE id = ?",
                (event_id,),
            ).fetchone()
            if row is None:
                raise RuntimeError(f"Migrated event {event_id} is missing")
            current = _metadata(row[0])
            if current.get("synthetic_migration_id") != migration_id:
                raise RuntimeError(f"Migrated event {event_id} changed after migration")
            conn.execute(
                "UPDATE usage_events SET metadata_json = ? WHERE id = ?",
                (previous["metadata_json"], event_id),
            )
            restored += 1
        conn.commit()
    return {
        "mode": "reverted_migration",
        "migration_id": migration_id,
        "database": str(db_path),
        "restored_events": restored,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="usage_events.db")
    parser.add_argument(
        "--known-test-identity",
        action="append",
        default=[],
        help="Known fixture identity; may be repeated. Values are hashed before matching.",
    )
    parser.add_argument("--output", help="Optional JSON plan output path")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Back up the database and apply the reviewed fixture tags.",
    )
    mode.add_argument(
        "--revert-manifest",
        help="Revert one applied migration using its generated manifest.",
    )
    parser.add_argument(
        "--backup-dir",
        default="backups/observability",
        help="Private directory for database backups and rollback manifests.",
    )
    args = parser.parse_args()

    if args.revert_manifest:
        result = revert_history_tags(args.db, manifest_path=args.revert_manifest)
    elif args.apply:
        if not args.known_test_identity:
            parser.error("--apply requires at least one --known-test-identity")
        result = apply_history_tags(
            args.db,
            known_test_identities=args.known_test_identity,
            backup_dir=args.backup_dir,
        )
    else:
        result = audit_history(
            args.db,
            known_test_identities=args.known_test_identity,
        )
    rendered = json.dumps(result, indent=2)
    if args.output:
        Path(args.output).write_text(f"{rendered}\n", encoding="utf-8")
    print(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
