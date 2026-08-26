"""Frozen v0.6.2 entitlement compatibility fixture.

The methods exercised by the rollback regression below are copied without
behavior changes from ``src/entitlement_manager.py`` at commit
1791c5c9c46163cdcc1c9b69613f2855bee4d7a1. Keeping the fixture in-tree makes
the compatibility proof independent of shallow CI checkout history.
"""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


DEFAULT_DAILY_CREDITS = int(os.environ.get("ANTHROPIC_DAILY_CREDITS", "50"))
DEFAULT_ENTITLEMENT_DB_PATH = "anthropic_entitlements.db"


@dataclass(frozen=True)
class CreditStatus:
    user_id: str
    email: str | None
    date: str
    daily_limit: int
    credits_spent: int
    credits_remaining: int
    status: str


class EntitlementManager:
    """The v0.6.2 ledger behavior needed for upgrade/rollback verification."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        default_daily_credits: int = DEFAULT_DAILY_CREDITS,
    ) -> None:
        self.db_path = str(
            db_path
            or os.environ.get(
                "ANTHROPIC_ENTITLEMENT_DB_PATH",
                DEFAULT_ENTITLEMENT_DB_PATH,
            )
        )
        self.default_daily_credits = default_daily_credits
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.execute("PRAGMA foreign_keys = ON")
        return conn

    @contextmanager
    def _connection(self) -> Iterator[sqlite3.Connection]:
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._connection() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    email TEXT,
                    daily_limit INTEGER NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_usage (
                    user_id TEXT NOT NULL,
                    usage_date TEXT NOT NULL,
                    credits_spent INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, usage_date),
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS usage_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT NOT NULL,
                    usage_date TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    subject TEXT NOT NULL DEFAULT '',
                    credits_delta INTEGER NOT NULL,
                    credits_remaining INTEGER NOT NULL,
                    outcome TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
                """
            )

    def _ensure_user(
        self,
        conn: sqlite3.Connection,
        user_id: str,
        email: str | None,
    ) -> None:
        now = _utc_now()
        conn.execute(
            """
            INSERT INTO users (user_id, email, daily_limit, status, created_at, updated_at)
            VALUES (?, ?, ?, 'active', ?, ?)
            ON CONFLICT(user_id) DO UPDATE SET
                email = COALESCE(excluded.email, users.email),
                updated_at = excluded.updated_at
            """,
            (user_id, email, self.default_daily_credits, now, now),
        )

    def _ensure_daily_usage(
        self,
        conn: sqlite3.Connection,
        user_id: str,
        usage_date: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO daily_usage (user_id, usage_date, credits_spent, updated_at)
            VALUES (?, ?, 0, ?)
            ON CONFLICT(user_id, usage_date) DO NOTHING
            """,
            (user_id, usage_date, _utc_now()),
        )

    def status(
        self,
        user_id: str,
        email: str | None = None,
        *,
        usage_date: str | None = None,
    ) -> CreditStatus:
        """Return the user's current starter credit state, creating rows as needed."""
        usage_date = usage_date or _today()
        with self._connection() as conn:
            self._ensure_user(conn, user_id, email)
            self._ensure_daily_usage(conn, user_id, usage_date)
            user_row = conn.execute(
                """
                SELECT email, daily_limit, status
                FROM users u
                WHERE u.user_id = ?
                """,
                (user_id,),
            ).fetchone()
            spent_row = conn.execute(
                """
                SELECT COALESCE(SUM(credits_spent), 0)
                FROM daily_usage
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()

        stored_email, daily_limit, status = user_row
        credits_spent = spent_row[0]
        remaining = max(0, int(daily_limit) - int(credits_spent))
        return CreditStatus(
            user_id=user_id,
            email=stored_email,
            date=usage_date,
            daily_limit=int(daily_limit),
            credits_spent=int(credits_spent),
            credits_remaining=remaining,
            status=status,
        )

    def spend(
        self,
        user_id: str,
        amount: int,
        *,
        email: str | None = None,
        tool_name: str,
        subject: str = "",
        usage_date: str | None = None,
    ) -> tuple[bool, CreditStatus]:
        """Atomically spend starter credits if the user has enough remaining."""
        if amount < 0:
            raise ValueError("amount must be non-negative")
        usage_date = usage_date or _today()

        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_user(conn, user_id, email)
            self._ensure_daily_usage(conn, user_id, usage_date)

            user_row = conn.execute(
                """
                SELECT email, daily_limit, status
                FROM users u
                WHERE u.user_id = ?
                """,
                (user_id,),
            ).fetchone()
            spent_row = conn.execute(
                """
                SELECT COALESCE(SUM(credits_spent), 0)
                FROM daily_usage
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            date_spent_row = conn.execute(
                """
                SELECT credits_spent
                FROM daily_usage
                WHERE user_id = ? AND usage_date = ?
                """,
                (user_id, usage_date),
            ).fetchone()
            stored_email, daily_limit, status = user_row
            lifetime_spent = int(spent_row[0])
            date_spent = int(date_spent_row[0])

            if status != "active":
                remaining = max(0, int(daily_limit) - lifetime_spent)
                credit_status = CreditStatus(
                    user_id=user_id,
                    email=stored_email,
                    date=usage_date,
                    daily_limit=int(daily_limit),
                    credits_spent=lifetime_spent,
                    credits_remaining=remaining,
                    status=status,
                )
                self._record_event(
                    conn,
                    user_id,
                    usage_date,
                    tool_name,
                    subject,
                    0,
                    remaining,
                    "blocked",
                )
                return False, credit_status

            remaining_before = int(daily_limit) - lifetime_spent
            if remaining_before < amount:
                credit_status = CreditStatus(
                    user_id=user_id,
                    email=stored_email,
                    date=usage_date,
                    daily_limit=int(daily_limit),
                    credits_spent=lifetime_spent,
                    credits_remaining=max(0, remaining_before),
                    status=status,
                )
                self._record_event(
                    conn,
                    user_id,
                    usage_date,
                    tool_name,
                    subject,
                    0,
                    max(0, remaining_before),
                    "insufficient_credits",
                )
                return False, credit_status

            new_lifetime_spent = lifetime_spent + amount
            new_date_spent = date_spent + amount
            conn.execute(
                """
                UPDATE daily_usage
                SET credits_spent = ?, updated_at = ?
                WHERE user_id = ? AND usage_date = ?
                """,
                (new_date_spent, _utc_now(), user_id, usage_date),
            )
            remaining_after = int(daily_limit) - new_lifetime_spent
            self._record_event(
                conn,
                user_id,
                usage_date,
                tool_name,
                subject,
                amount,
                remaining_after,
                "spent",
            )

        return True, CreditStatus(
            user_id=user_id,
            email=stored_email,
            date=usage_date,
            daily_limit=int(daily_limit),
            credits_spent=new_lifetime_spent,
            credits_remaining=remaining_after,
            status=status,
        )

    @staticmethod
    def _record_event(
        conn: sqlite3.Connection,
        user_id: str,
        usage_date: str,
        tool_name: str,
        subject: str,
        credits_delta: int,
        credits_remaining: int,
        outcome: str,
    ) -> None:
        conn.execute(
            """
            INSERT INTO usage_events (
                user_id, usage_date, tool_name, subject, credits_delta,
                credits_remaining, outcome, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                user_id,
                usage_date,
                tool_name,
                subject,
                credits_delta,
                credits_remaining,
                outcome,
                _utc_now(),
            ),
        )


def _today() -> str:
    return datetime.now(UTC).date().isoformat()


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()
