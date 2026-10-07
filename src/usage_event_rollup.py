"""Keep the usage-event log bounded.

``usage_events`` is the raw telemetry table behind the usage dashboard. It had no
retention at all; by October 2026 it held 1.2 million rows (0.9 GB) and grew by
about 450 MB a month on the Railway volume. This module folds rows older than a
retention window into a compact daily table and deletes the raw rows, so disk
use stays flat while long-term history remains available.

The window defaults to 185 days: the dashboard's longest view is 90 days and it
compares each window with the previous one, so 180 days of raw rows must stay.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
import shutil
import sqlite3
from typing import Any

from src.observability import UsageEventStore

RETENTION_DAYS_ENV = "USAGE_EVENT_RETENTION_DAYS"
DEFAULT_RETENTION_DAYS = 185
MIN_RETENTION_DAYS = 180  # two 90-day dashboard windows
BATCH_ROWS = 20_000
MAX_BATCHES_PER_PASS = 50  # bounds one pass to about a million rows


def retention_days() -> int:
    raw = os.environ.get(RETENTION_DAYS_ENV, "").strip()
    try:
        value = int(raw) if raw else DEFAULT_RETENTION_DAYS
    except ValueError:
        value = DEFAULT_RETENTION_DAYS
    return max(MIN_RETENTION_DAYS, value)


def retention_cutoff(now: datetime | None = None, *, days: int | None = None) -> datetime:
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return current.astimezone(UTC) - timedelta(days=days or retention_days())


def ensure_rollup_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS usage_daily_rollup (
            day TEXT NOT NULL,
            event TEXT NOT NULL,
            surface TEXT NOT NULL DEFAULT '',
            status_code INTEGER NOT NULL DEFAULT 0,
            count INTEGER NOT NULL,
            price_usdc_sum REAL NOT NULL DEFAULT 0,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (day, event, surface, status_code)
        )
        """
    )


def rollup_and_prune(
    store: UsageEventStore,
    *,
    now: datetime | None = None,
    days: int | None = None,
    batch_rows: int = BATCH_ROWS,
    max_batches: int = MAX_BATCHES_PER_PASS,
) -> dict[str, Any]:
    """Fold raw events older than the cutoff into ``usage_daily_rollup`` and delete them.

    Each batch aggregates and deletes the same set of rows inside one transaction,
    so a crash between passes can neither double count nor lose a day.
    """
    cutoff = retention_cutoff(now, days=days)
    cutoff_iso = cutoff.isoformat()
    rolled = deleted = batches = 0
    with store._connect() as conn:  # noqa: SLF001 - same store, same file
        ensure_rollup_table(conn)
        for _ in range(max_batches):
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT id FROM usage_events WHERE timestamp < ?
                ORDER BY id LIMIT 1 OFFSET ?
                """,
                (cutoff_iso, max(batch_rows - 1, 0)),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    "SELECT MAX(id) AS id FROM usage_events WHERE timestamp < ?",
                    (cutoff_iso,),
                ).fetchone()
            max_id = row["id"] if row is not None else None
            if max_id is None:
                conn.execute("COMMIT")
                break
            stamp = datetime.now(UTC).isoformat()
            conn.execute(
                """
                INSERT INTO usage_daily_rollup
                    (day, event, surface, status_code, count, price_usdc_sum, updated_at)
                SELECT substr(timestamp, 1, 10), event, COALESCE(surface, ''),
                       COALESCE(status_code, 0), COUNT(*), COALESCE(SUM(price_usdc), 0), ?
                FROM usage_events
                WHERE timestamp < ? AND id <= ?
                GROUP BY 1, 2, 3, 4
                ON CONFLICT(day, event, surface, status_code) DO UPDATE SET
                    count = count + excluded.count,
                    price_usdc_sum = price_usdc_sum + excluded.price_usdc_sum,
                    updated_at = excluded.updated_at
                """,
                (stamp, cutoff_iso, max_id),
            )
            removed = conn.execute(
                "DELETE FROM usage_events WHERE timestamp < ? AND id <= ?",
                (cutoff_iso, max_id),
            ).rowcount
            conn.execute("COMMIT")
            rolled += removed
            deleted += removed
            batches += 1
            if removed < batch_rows:
                break
        remaining = conn.execute(
            "SELECT COUNT(*) AS n FROM usage_events WHERE timestamp < ?", (cutoff_iso,)
        ).fetchone()["n"]
    return {
        "cutoff": cutoff_iso,
        "retention_days": days or retention_days(),
        "rows_rolled_up": rolled,
        "rows_deleted": deleted,
        "batches": batches,
        "remaining_rows_past_cutoff": int(remaining),
        "completed_at": datetime.now(UTC).isoformat(),
    }


def storage_status(db_path: str | Path, *, volume_path: str | None = None) -> dict[str, Any]:
    """Disk use of the volume that holds the usage log, for /health and alerts."""
    path = Path(db_path)
    mount = Path(volume_path or os.environ.get("RAILWAY_VOLUME_MOUNT_PATH") or path.parent or ".")
    payload: dict[str, Any] = {
        "usage_events_db_bytes": path.stat().st_size if path.exists() else 0,
        "volume_path": str(mount),
    }
    try:
        usage = shutil.disk_usage(mount)
    except OSError:
        payload["volume_available"] = False
        return payload
    used_percent = round(100.0 * usage.used / usage.total, 1) if usage.total else None
    payload.update(
        {
            "volume_available": True,
            "volume_total_bytes": usage.total,
            "volume_used_bytes": usage.used,
            "volume_free_bytes": usage.free,
            "volume_used_percent": used_percent,
            "volume_pressure": bool(used_percent is not None and used_percent >= 80.0),
        }
    )
    return payload
