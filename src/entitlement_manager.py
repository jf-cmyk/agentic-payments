"""Daily credit entitlements for the Anthropic-safe MCP surface."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path


DEFAULT_DAILY_CREDITS = int(os.environ.get("ANTHROPIC_DAILY_CREDITS", "50"))


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
    """Server-side daily quota ledger keyed by authenticated user identity."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        default_daily_credits: int = DEFAULT_DAILY_CREDITS,
    ) -> None:
        self.db_path = str(db_path or os.environ.get(
            "ANTHROPIC_ENTITLEMENT_DB_PATH",
            "anthropic_entitlements.db",
        ))
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
        """Return the user's current daily quota state, creating rows as needed."""
        usage_date = usage_date or _today()
        with self._connection() as conn:
            self._ensure_user(conn, user_id, email)
            self._ensure_daily_usage(conn, user_id, usage_date)
            row = conn.execute(
                """
                SELECT u.email, u.daily_limit, u.status, du.credits_spent
                FROM users u
                JOIN daily_usage du ON du.user_id = u.user_id
                WHERE u.user_id = ? AND du.usage_date = ?
                """,
                (user_id, usage_date),
            ).fetchone()

        stored_email, daily_limit, status, credits_spent = row
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
        """Atomically spend daily credits if the user has enough remaining."""
        if amount < 0:
            raise ValueError("amount must be non-negative")
        usage_date = usage_date or _today()

        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_user(conn, user_id, email)
            self._ensure_daily_usage(conn, user_id, usage_date)

            row = conn.execute(
                """
                SELECT u.email, u.daily_limit, u.status, du.credits_spent
                FROM users u
                JOIN daily_usage du ON du.user_id = u.user_id
                WHERE u.user_id = ? AND du.usage_date = ?
                """,
                (user_id, usage_date),
            ).fetchone()
            stored_email, daily_limit, status, credits_spent = row

            if status != "active":
                remaining = max(0, int(daily_limit) - int(credits_spent))
                credit_status = CreditStatus(
                    user_id=user_id,
                    email=stored_email,
                    date=usage_date,
                    daily_limit=int(daily_limit),
                    credits_spent=int(credits_spent),
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

            remaining_before = int(daily_limit) - int(credits_spent)
            if remaining_before < amount:
                credit_status = CreditStatus(
                    user_id=user_id,
                    email=stored_email,
                    date=usage_date,
                    daily_limit=int(daily_limit),
                    credits_spent=int(credits_spent),
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

            new_spent = int(credits_spent) + amount
            conn.execute(
                """
                UPDATE daily_usage
                SET credits_spent = ?, updated_at = ?
                WHERE user_id = ? AND usage_date = ?
                """,
                (new_spent, _utc_now(), user_id, usage_date),
            )
            remaining_after = int(daily_limit) - new_spent
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
            credits_spent=new_spent,
            credits_remaining=remaining_after,
            status=status,
        )

    def refund(
        self,
        user_id: str,
        amount: int,
        *,
        tool_name: str,
        subject: str = "",
        usage_date: str | None = None,
    ) -> CreditStatus:
        """Refund credits for a failed upstream call."""
        if amount < 0:
            raise ValueError("amount must be non-negative")
        usage_date = usage_date or _today()

        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_user(conn, user_id, None)
            self._ensure_daily_usage(conn, user_id, usage_date)

            row = conn.execute(
                """
                SELECT u.email, u.daily_limit, u.status, du.credits_spent
                FROM users u
                JOIN daily_usage du ON du.user_id = u.user_id
                WHERE u.user_id = ? AND du.usage_date = ?
                """,
                (user_id, usage_date),
            ).fetchone()
            stored_email, daily_limit, status, credits_spent = row
            new_spent = max(0, int(credits_spent) - amount)
            conn.execute(
                """
                UPDATE daily_usage
                SET credits_spent = ?, updated_at = ?
                WHERE user_id = ? AND usage_date = ?
                """,
                (new_spent, _utc_now(), user_id, usage_date),
            )
            remaining = int(daily_limit) - new_spent
            self._record_event(
                conn,
                user_id,
                usage_date,
                tool_name,
                subject,
                -amount,
                remaining,
                "refunded",
            )

        return CreditStatus(
            user_id=user_id,
            email=stored_email,
            date=usage_date,
            daily_limit=int(daily_limit),
            credits_spent=new_spent,
            credits_remaining=remaining,
            status=status,
        )

    def set_daily_limit(
        self,
        user_id: str,
        daily_limit: int,
        *,
        email: str | None = None,
    ) -> CreditStatus:
        """Set a user's daily allowance for subscriptions or manual beta grants."""
        if daily_limit < 0:
            raise ValueError("daily_limit must be non-negative")
        with self._connection() as conn:
            self._ensure_user(conn, user_id, email)
            conn.execute(
                """
                UPDATE users
                SET daily_limit = ?, email = COALESCE(?, email), updated_at = ?
                WHERE user_id = ?
                """,
                (daily_limit, email, _utc_now(), user_id),
            )
        return self.status(user_id, email)

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
