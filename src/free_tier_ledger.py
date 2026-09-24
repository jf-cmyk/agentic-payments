"""Shared free-tier grant ledger: one monthly pool per person across connectors.

The per-connector ``EntitlementManager`` databases keep the charge lifecycle
(pending, delivered, refunded) and rollback compatibility. This ledger sits in
front of them and is keyed by the salted hash of the normalized email, so the
Claude, Cursor, and OpenAI connectors all draw from the same 15,000-credit
monthly pool and share the per-identity guards from checkpoint section 4:

- monthly pool (``FREE_TIER_MONTHLY_CREDITS``)
- sustained per-minute limit and soft daily cap
- global daily cap across all identities (circuit breaker)
- detect-and-suspend: fast drain and symbol sweeps

Every decision is atomic (``BEGIN IMMEDIATE``) and never raises for a policy
denial; callers translate ``FreeTierDecision`` into a clear non-5xx payload.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
import sqlite3
import time
from typing import Any

from src.config import settings
from src.entitlement_manager import month_prefix, month_reset_date

THRESHOLDS = (50, 80, 95, 100)
VALID_GRANT_STATUSES = frozenset({"active", "suspended"})
_MINUTE_WINDOW_SECONDS = 60.0
_MINUTE_RETENTION_SECONDS = 120.0


@dataclass(frozen=True)
class FreeTierSnapshot:
    grant_key: str
    usage_date: str
    monthly_limit: int
    credits_spent: int
    credits_remaining: int
    credits_spent_today: int
    daily_soft_cap: int
    resets_at: str
    status: str
    status_reason: str = ""

    def as_payload(self) -> dict[str, Any]:
        return {
            "period": month_prefix(self.usage_date),
            "monthly_limit": self.monthly_limit,
            "credits_spent": self.credits_spent,
            "credits_remaining": self.credits_remaining,
            "credits_spent_today": self.credits_spent_today,
            "daily_soft_cap": self.daily_soft_cap,
            "resets_at": self.resets_at,
            "status": self.status,
        }


@dataclass(frozen=True)
class FreeTierDecision:
    allowed: bool
    reason: str
    snapshot: FreeTierSnapshot
    retry_after_seconds: int | None = None
    grant_created: bool = False
    thresholds_crossed: tuple[int, ...] = ()
    abuse_flags: tuple[str, ...] = field(default_factory=tuple)


class FreeTierLedger:
    """SQLite-backed shared grant ledger (see module docstring)."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self.db_path = str(db_path or settings.free_tier.ledger_db_path)
        self._init_db()

    # -- infrastructure ---------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS grants (
                    grant_key TEXT PRIMARY KEY,
                    status TEXT NOT NULL DEFAULT 'active',
                    status_reason TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    first_call_at TEXT,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS grant_usage (
                    grant_key TEXT NOT NULL,
                    usage_date TEXT NOT NULL,
                    credits_spent INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (grant_key, usage_date),
                    FOREIGN KEY (grant_key) REFERENCES grants(grant_key)
                );
                CREATE TABLE IF NOT EXISTS grant_minute_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    grant_key TEXT NOT NULL,
                    occurred_at REAL NOT NULL,
                    credits INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_grant_minute_events
                    ON grant_minute_events(grant_key, occurred_at);
                CREATE TABLE IF NOT EXISTS grant_symbol_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    grant_key TEXT NOT NULL,
                    occurred_at REAL NOT NULL,
                    symbol TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_grant_symbol_events
                    ON grant_symbol_events(grant_key, occurred_at);
                CREATE TABLE IF NOT EXISTS global_usage (
                    usage_date TEXT PRIMARY KEY,
                    credits_spent INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS grant_subjects (
                    grant_key TEXT NOT NULL,
                    ledger_subject TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    PRIMARY KEY (grant_key, ledger_subject)
                );
                """
            )

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(UTC).isoformat()

    def _ensure_grant(self, conn: sqlite3.Connection, grant_key: str) -> bool:
        now = self._now_iso()
        cursor = conn.execute(
            """
            INSERT INTO grants (grant_key, status, status_reason, created_at, updated_at)
            VALUES (?, 'active', '', ?, ?)
            ON CONFLICT(grant_key) DO NOTHING
            """,
            (grant_key, now, now),
        )
        return cursor.rowcount == 1

    def _month_spent(self, conn: sqlite3.Connection, grant_key: str, usage_date: str) -> int:
        row = conn.execute(
            "SELECT COALESCE(SUM(credits_spent), 0) FROM grant_usage "
            "WHERE grant_key = ? AND usage_date LIKE ?",
            (grant_key, f"{month_prefix(usage_date)}-%"),
        ).fetchone()
        return int(row[0]) if row else 0

    def _date_spent(self, conn: sqlite3.Connection, grant_key: str, usage_date: str) -> int:
        row = conn.execute(
            "SELECT credits_spent FROM grant_usage WHERE grant_key = ? AND usage_date = ?",
            (grant_key, usage_date),
        ).fetchone()
        return int(row[0]) if row else 0

    def _global_spent(self, conn: sqlite3.Connection, usage_date: str) -> int:
        row = conn.execute(
            "SELECT credits_spent FROM global_usage WHERE usage_date = ?",
            (usage_date,),
        ).fetchone()
        return int(row[0]) if row else 0

    def _snapshot(self, conn: sqlite3.Connection, grant_key: str, usage_date: str) -> FreeTierSnapshot:
        row = conn.execute(
            "SELECT status, status_reason FROM grants WHERE grant_key = ?",
            (grant_key,),
        ).fetchone()
        status, status_reason = (str(row[0]), str(row[1])) if row else ("active", "")
        limit = int(settings.free_tier.monthly_credits)
        spent = self._month_spent(conn, grant_key, usage_date)
        return FreeTierSnapshot(
            grant_key=grant_key,
            usage_date=usage_date,
            monthly_limit=limit,
            credits_spent=spent,
            credits_remaining=max(0, limit - spent),
            credits_spent_today=self._date_spent(conn, grant_key, usage_date),
            daily_soft_cap=int(settings.free_tier.daily_soft_cap_credits),
            resets_at=month_reset_date(usage_date),
            status=status,
            status_reason=status_reason,
        )

    # -- public API -------------------------------------------------------
    def status(self, grant_key: str, *, usage_date: str | None = None) -> FreeTierSnapshot:
        usage_date = usage_date or _today()
        with self._connect() as conn:
            self._ensure_grant(conn, grant_key)
            return self._snapshot(conn, grant_key, usage_date)

    def bind_subject(self, grant_key: str, ledger_subject: str) -> None:
        """Remember which connector principals draw from a grant (no PII)."""
        with self._connect() as conn:
            self._ensure_grant(conn, grant_key)
            conn.execute(
                "INSERT OR IGNORE INTO grant_subjects (grant_key, ledger_subject, created_at) "
                "VALUES (?, ?, ?)",
                (grant_key, ledger_subject, self._now_iso()),
            )

    def reserve(
        self,
        grant_key: str,
        credits: int,
        *,
        symbol: str = "",
        usage_date: str | None = None,
        now: float | None = None,
    ) -> FreeTierDecision:
        """Apply every guard and, if allowed, record the spend atomically."""
        if credits < 0:
            raise ValueError("credits must be non-negative")
        usage_date = usage_date or _today()
        current = float(now if now is not None else time.time())
        free_tier = settings.free_tier
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            created = self._ensure_grant(conn, grant_key)
            self._prune(conn, current)
            before = self._snapshot(conn, grant_key, usage_date)

            if not free_tier.enabled:
                return FreeTierDecision(False, "free_tier_disabled", before, grant_created=created)
            if before.status != "active":
                return FreeTierDecision(False, "suspended", before, grant_created=created)

            # Per-minute sustained limit (fixed 60 s window, credit-weighted).
            if free_tier.per_minute_credits > 0:
                rows = conn.execute(
                    "SELECT occurred_at, credits FROM grant_minute_events "
                    "WHERE grant_key = ? AND occurred_at > ? ORDER BY occurred_at",
                    (grant_key, current - _MINUTE_WINDOW_SECONDS),
                ).fetchall()
                minute_credits = sum(int(row[1]) for row in rows)
                if minute_credits + credits > free_tier.per_minute_credits:
                    retry_after = (
                        max(1, int(float(rows[0][0]) + _MINUTE_WINDOW_SECONDS - current) + 1)
                        if rows
                        else 60
                    )
                    return FreeTierDecision(
                        False,
                        "rate_limited_minute",
                        before,
                        retry_after_seconds=retry_after,
                        grant_created=created,
                    )

            # Soft daily cap per identity.
            if (
                free_tier.daily_soft_cap_credits > 0
                and before.credits_spent_today + credits > free_tier.daily_soft_cap_credits
            ):
                return FreeTierDecision(
                    False,
                    "daily_soft_cap",
                    before,
                    retry_after_seconds=_seconds_until_next_utc_day(current),
                    grant_created=created,
                )

            # Monthly pool.
            if before.credits_spent + credits > before.monthly_limit:
                return FreeTierDecision(False, "monthly_pool_exhausted", before, grant_created=created)

            # Global circuit breaker.
            if (
                free_tier.global_daily_cap_credits > 0
                and self._global_spent(conn, usage_date) + credits > free_tier.global_daily_cap_credits
            ):
                return FreeTierDecision(
                    False,
                    "global_daily_cap",
                    before,
                    retry_after_seconds=_seconds_until_next_utc_day(current),
                    grant_created=created,
                )

            # Record the spend.
            now_iso = self._now_iso()
            conn.execute(
                """
                INSERT INTO grant_usage (grant_key, usage_date, credits_spent, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(grant_key, usage_date) DO UPDATE SET
                    credits_spent = grant_usage.credits_spent + excluded.credits_spent,
                    updated_at = excluded.updated_at
                """,
                (grant_key, usage_date, credits, now_iso),
            )
            conn.execute(
                """
                INSERT INTO global_usage (usage_date, credits_spent, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(usage_date) DO UPDATE SET
                    credits_spent = global_usage.credits_spent + excluded.credits_spent,
                    updated_at = excluded.updated_at
                """,
                (usage_date, credits, now_iso),
            )
            if credits > 0:
                conn.execute(
                    "INSERT INTO grant_minute_events (grant_key, occurred_at, credits) VALUES (?, ?, ?)",
                    (grant_key, current, credits),
                )
            if symbol:
                conn.execute(
                    "INSERT INTO grant_symbol_events (grant_key, occurred_at, symbol) VALUES (?, ?, ?)",
                    (grant_key, current, symbol.strip().upper()),
                )
            conn.execute(
                "UPDATE grants SET first_call_at = COALESCE(first_call_at, ?), updated_at = ? "
                "WHERE grant_key = ?",
                (datetime.fromtimestamp(current, UTC).isoformat(), now_iso, grant_key),
            )

            after = self._snapshot(conn, grant_key, usage_date)
            thresholds = _thresholds_crossed(before, after)
            abuse_flags = self._detect_abuse(conn, grant_key, after, current)
            if abuse_flags:
                conn.execute(
                    "UPDATE grants SET status = 'suspended', status_reason = ?, updated_at = ? "
                    "WHERE grant_key = ?",
                    (",".join(abuse_flags), now_iso, grant_key),
                )
                after = self._snapshot(conn, grant_key, usage_date)
            return FreeTierDecision(
                True,
                "ok",
                after,
                grant_created=created,
                thresholds_crossed=thresholds,
                abuse_flags=tuple(abuse_flags),
            )

    def release(
        self,
        grant_key: str,
        credits: int,
        *,
        usage_date: str | None = None,
    ) -> FreeTierSnapshot:
        """Give credits back after a failed delivery (mirrors an entitlement refund)."""
        if credits < 0:
            raise ValueError("credits must be non-negative")
        usage_date = usage_date or _today()
        now_iso = self._now_iso()
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_grant(conn, grant_key)
            conn.execute(
                "UPDATE grant_usage SET credits_spent = MAX(0, credits_spent - ?), updated_at = ? "
                "WHERE grant_key = ? AND usage_date = ?",
                (credits, now_iso, grant_key, usage_date),
            )
            conn.execute(
                "UPDATE global_usage SET credits_spent = MAX(0, credits_spent - ?), updated_at = ? "
                "WHERE usage_date = ?",
                (credits, now_iso, usage_date),
            )
            return self._snapshot(conn, grant_key, usage_date)

    def set_status(self, grant_key: str, status: str, *, reason: str = "") -> FreeTierSnapshot:
        if status not in VALID_GRANT_STATUSES:
            raise ValueError(f"status must be one of {sorted(VALID_GRANT_STATUSES)}")
        with self._connect() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_grant(conn, grant_key)
            conn.execute(
                "UPDATE grants SET status = ?, status_reason = ?, updated_at = ? WHERE grant_key = ?",
                (status, reason, self._now_iso(), grant_key),
            )
            return self._snapshot(conn, grant_key, _today())

    def summary(self, *, usage_date: str | None = None) -> dict[str, Any]:
        """Operator view for /health and the command center (no identifiers)."""
        usage_date = usage_date or _today()
        with self._connect() as conn:
            grants = conn.execute("SELECT COUNT(*) FROM grants").fetchone()[0]
            suspended = conn.execute(
                "SELECT COUNT(*) FROM grants WHERE status = 'suspended'"
            ).fetchone()[0]
            month_credits = conn.execute(
                "SELECT COALESCE(SUM(credits_spent), 0) FROM grant_usage WHERE usage_date LIKE ?",
                (f"{month_prefix(usage_date)}-%",),
            ).fetchone()[0]
            active_this_month = conn.execute(
                "SELECT COUNT(DISTINCT grant_key) FROM grant_usage "
                "WHERE usage_date LIKE ? AND credits_spent > 0",
                (f"{month_prefix(usage_date)}-%",),
            ).fetchone()[0]
            global_today = self._global_spent(conn, usage_date)
        cap = int(settings.free_tier.global_daily_cap_credits)
        return {
            "grants": int(grants),
            "suspended_grants": int(suspended),
            "active_grants_this_month": int(active_this_month),
            "credits_consumed_this_month": int(month_credits),
            "global_credits_today": int(global_today),
            "global_daily_cap_credits": cap,
            "global_cap_remaining_today": max(0, cap - int(global_today)) if cap > 0 else None,
            "worst_case_exposure_credits": int(grants) * int(settings.free_tier.monthly_credits),
        }

    # -- internals ---------------------------------------------------------
    def _prune(self, conn: sqlite3.Connection, current: float) -> None:
        conn.execute(
            "DELETE FROM grant_minute_events WHERE occurred_at <= ?",
            (current - _MINUTE_RETENTION_SECONDS,),
        )
        window = float(settings.free_tier.abuse_sweep_window_minutes) * 60.0
        conn.execute(
            "DELETE FROM grant_symbol_events WHERE occurred_at <= ?",
            (current - window * 2,),
        )

    def _detect_abuse(
        self,
        conn: sqlite3.Connection,
        grant_key: str,
        after: FreeTierSnapshot,
        current: float,
    ) -> list[str]:
        free_tier = settings.free_tier
        flags: list[str] = []
        if after.monthly_limit > 0:
            row = conn.execute(
                "SELECT first_call_at FROM grants WHERE grant_key = ?",
                (grant_key,),
            ).fetchone()
            first_call = _parse_iso(row[0]) if row and row[0] else None
            ratio = after.credits_spent / after.monthly_limit
            if (
                first_call is not None
                and ratio >= free_tier.abuse_fast_drain_ratio
                and current - first_call <= free_tier.abuse_fast_drain_window_hours * 3600
            ):
                flags.append("fast_drain")
        window = float(free_tier.abuse_sweep_window_minutes) * 60.0
        distinct = conn.execute(
            "SELECT COUNT(DISTINCT symbol) FROM grant_symbol_events "
            "WHERE grant_key = ? AND occurred_at > ?",
            (grant_key, current - window),
        ).fetchone()[0]
        if int(distinct) > free_tier.abuse_sweep_distinct_symbols:
            flags.append("symbol_sweep")
        return flags


def _thresholds_crossed(before: FreeTierSnapshot, after: FreeTierSnapshot) -> tuple[int, ...]:
    if after.monthly_limit <= 0:
        return ()
    before_pct = before.credits_spent * 100 / after.monthly_limit
    after_pct = after.credits_spent * 100 / after.monthly_limit
    return tuple(level for level in THRESHOLDS if before_pct < level <= after_pct)


def _seconds_until_next_utc_day(current: float) -> int:
    seconds_into_day = int(current) % 86_400
    return max(1, 86_400 - seconds_into_day)


def _parse_iso(value: str) -> float | None:
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


_LEDGER: FreeTierLedger | None = None


def get_free_tier_ledger() -> FreeTierLedger:
    """Return the process-wide shared ledger, honouring FREE_TIER_LEDGER_DB_PATH."""
    global _LEDGER
    configured = str(settings.free_tier.ledger_db_path)
    if _LEDGER is None or _LEDGER.db_path != configured:
        parent = Path(configured).expanduser().parent
        if str(parent) not in {"", "."}:
            parent.mkdir(parents=True, exist_ok=True)
        _LEDGER = FreeTierLedger(configured)
    return _LEDGER


def reset_free_tier_ledger() -> None:
    global _LEDGER
    _LEDGER = None
