"""Starter credit entitlements for authenticated MCP connector surfaces."""

from __future__ import annotations

import os
import sqlite3
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path


DEFAULT_DAILY_CREDITS = int(os.environ.get("ANTHROPIC_DAILY_CREDITS", "50"))
DEFAULT_ENTITLEMENT_DB_PATH = "anthropic_entitlements.db"
DEFAULT_PENDING_CHARGE_LEASE_SECONDS = 15 * 60
MIN_PENDING_CHARGE_LEASE_SECONDS = 5 * 60
PENDING_RECOVERY_BATCH_LIMIT = 100
ENTITLEMENT_SCHEMA_VERSION = 3
VALID_CHARGE_STATES = frozenset({"pending", "delivered", "refunded"})
ENTITLEMENT_SCHEMA_COLUMNS = {
    "users": {
        "user_id": "TEXT",
        "email": "TEXT",
        "daily_limit": "INTEGER",
        "status": "TEXT",
        "created_at": "TEXT",
        "updated_at": "TEXT",
    },
    "daily_usage": {
        "user_id": "TEXT",
        "usage_date": "TEXT",
        "credits_spent": "INTEGER",
        "updated_at": "TEXT",
    },
    "usage_events": {
        "id": "INTEGER",
        "user_id": "TEXT",
        "usage_date": "TEXT",
        "tool_name": "TEXT",
        "subject": "TEXT",
        "credits_delta": "INTEGER",
        "credits_remaining": "INTEGER",
        "outcome": "TEXT",
        "created_at": "TEXT",
    },
    "credit_charges": {
        "charge_id": "TEXT",
        "user_id": "TEXT",
        "usage_date": "TEXT",
        "amount": "INTEGER",
        "state": "TEXT",
        "created_at": "TEXT",
        "delivered_at": "TEXT",
        "refunded_at": "TEXT",
    },
    "identity_aliases": {
        "ledger_subject": "TEXT",
        "user_id": "TEXT",
        "created_at": "TEXT",
        "updated_at": "TEXT",
    },
}


def connector_entitlement_db_path(
    prefix: str,
    fallback_db_path: str | Path | None = None,
) -> str:
    """Resolve a connector-specific ledger path without sharing defaults."""
    normalized = prefix.strip().upper()
    fallback = fallback_db_path or f"{normalized.lower()}_entitlements.db"
    return os.environ.get(f"{normalized}_ENTITLEMENT_DB_PATH", str(fallback))


def connector_entitlement_manager(
    prefix: str,
    *,
    fallback_db_path: str | Path | None = None,
    fallback_daily_credits: int | None = None,
) -> "EntitlementManager":
    """Build a connector-specific entitlement manager from environment variables."""
    prefix = prefix.upper()
    db_path = connector_entitlement_db_path(prefix, fallback_db_path)
    daily_credits = int(
        os.environ.get(
            f"{prefix}_DAILY_CREDITS",
            str(fallback_daily_credits or DEFAULT_DAILY_CREDITS),
        )
    )
    pending_lease_seconds = int(
        os.environ.get(
            f"{prefix}_ENTITLEMENT_PENDING_LEASE_SECONDS",
            os.environ.get(
                "ENTITLEMENT_PENDING_CHARGE_LEASE_SECONDS",
                str(DEFAULT_PENDING_CHARGE_LEASE_SECONDS),
            ),
        )
    )
    return EntitlementManager(
        db_path,
        default_daily_credits=daily_credits,
        pending_charge_lease_seconds=pending_lease_seconds,
    )


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
    """Server-side starter credit ledger keyed by authenticated user identity."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        *,
        default_daily_credits: int = DEFAULT_DAILY_CREDITS,
        pending_charge_lease_seconds: int | None = None,
    ) -> None:
        self.db_path = str(db_path or os.environ.get(
            "ANTHROPIC_ENTITLEMENT_DB_PATH",
            DEFAULT_ENTITLEMENT_DB_PATH,
        ))
        self.default_daily_credits = default_daily_credits
        configured_lease = (
            int(os.environ.get(
                "ENTITLEMENT_PENDING_CHARGE_LEASE_SECONDS",
                str(DEFAULT_PENDING_CHARGE_LEASE_SECONDS),
            ))
            if pending_charge_lease_seconds is None
            else int(pending_charge_lease_seconds)
        )
        if configured_lease < MIN_PENDING_CHARGE_LEASE_SECONDS:
            raise ValueError(
                "pending_charge_lease_seconds must be at least "
                f"{MIN_PENDING_CHARGE_LEASE_SECONDS}"
            )
        self.pending_charge_lease_seconds = configured_lease
        self._initialization_blocker: str | None = None
        self._last_recovery: dict[str, object] = {
            "recovered_charges": 0,
            "recovered_credits": 0,
            "remaining_stale_charges": 0,
        }
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

    @staticmethod
    def _table_columns(conn: sqlite3.Connection, table_name: str) -> dict[str, str]:
        return {
            str(row[1]): " ".join(str(row[2] or "").upper().split())
            for row in conn.execute(f'PRAGMA table_info("{table_name}")').fetchall()
        }

    @staticmethod
    def _identity_alias_schema_status(conn: sqlite3.Connection) -> dict[str, object]:
        table_info = conn.execute('PRAGMA table_info("identity_aliases")').fetchall()
        primary_key_columns = [
            str(row[1])
            for row in sorted(table_info, key=lambda row: int(row[5] or 0))
            if int(row[5] or 0) > 0
        ]

        exact_unique_user_id_indexes: list[str] = []
        for index_row in conn.execute('PRAGMA index_list("identity_aliases")').fetchall():
            index_name = str(index_row[1])
            unique = int(index_row[2] or 0) == 1
            partial = len(index_row) > 4 and int(index_row[4] or 0) == 1
            quoted_index = index_name.replace('"', '""')
            index_columns = [
                str(row[2])
                for row in conn.execute(
                    f'PRAGMA index_info("{quoted_index}")'
                ).fetchall()
                if row[2] is not None
            ]
            if unique and not partial and index_columns == ["user_id"]:
                exact_unique_user_id_indexes.append(index_name)

        foreign_key_rows = conn.execute(
            'PRAGMA foreign_key_list("identity_aliases")'
        ).fetchall()
        foreign_keys = [
            {
                "table": str(row[2]),
                "from": str(row[3]),
                "to": str(row[4]),
                "on_update": str(row[5]).upper(),
                "on_delete": str(row[6]).upper(),
                "match": str(row[7]).upper(),
            }
            for row in foreign_key_rows
        ]
        expected_foreign_key = {
            "table": "users",
            "from": "user_id",
            "to": "user_id",
            "on_update": "NO ACTION",
            "on_delete": "NO ACTION",
            "match": "NONE",
        }
        return {
            "primary_key_columns": primary_key_columns,
            "primary_key_valid": primary_key_columns == ["ledger_subject"],
            "unique_user_id_indexes": sorted(exact_unique_user_id_indexes),
            "unique_user_id_valid": len(exact_unique_user_id_indexes) == 1,
            "foreign_keys": foreign_keys,
            "foreign_key_valid": foreign_keys == [expected_foreign_key],
        }

    @staticmethod
    def _identity_alias_data_status(conn: sqlite3.Connection) -> dict[str, int]:
        empty_bindings = sum(
            1
            for ledger_subject, user_id in conn.execute(
                "SELECT ledger_subject, user_id FROM identity_aliases"
            )
            if ledger_subject is None
            or user_id is None
            or not str(ledger_subject).strip()
            or not str(user_id).strip()
        )
        duplicate_subject_row = conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT ledger_subject FROM identity_aliases
                GROUP BY ledger_subject HAVING COUNT(*) > 1
            )
            """
        ).fetchone()
        duplicate_user_row = conn.execute(
            """
            SELECT COUNT(*) FROM (
                SELECT user_id FROM identity_aliases
                GROUP BY user_id HAVING COUNT(*) > 1
            )
            """
        ).fetchone()
        orphan_row = conn.execute(
            """
            SELECT COUNT(*)
            FROM identity_aliases aliases
            LEFT JOIN users ON users.user_id = aliases.user_id
            WHERE users.user_id IS NULL
            """
        ).fetchone()
        return {
            "empty_bindings": empty_bindings,
            "duplicate_ledger_subjects": (
                int(duplicate_subject_row[0]) if duplicate_subject_row else 0
            ),
            "duplicate_user_ids": int(duplicate_user_row[0]) if duplicate_user_row else 0,
            "orphan_bindings": int(orphan_row[0]) if orphan_row else 0,
        }

    @classmethod
    def _identity_alias_status(cls, conn: sqlite3.Connection) -> dict[str, object]:
        schema = cls._identity_alias_schema_status(conn)
        data = cls._identity_alias_data_status(conn)
        return {
            **schema,
            **data,
            "ready": (
                bool(schema["primary_key_valid"])
                and bool(schema["unique_user_id_valid"])
                and bool(schema["foreign_key_valid"])
                and all(count == 0 for count in data.values())
            ),
        }

    @classmethod
    def _schema_can_be_initialized(cls, conn: sqlite3.Connection) -> bool:
        """Reject foreign ledgers before creating or migrating entitlement tables."""
        tables = {
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type = 'table' AND name NOT LIKE 'sqlite_%'"
            ).fetchall()
        }
        owned_tables = tables.intersection(ENTITLEMENT_SCHEMA_COLUMNS)
        if tables and not owned_tables:
            return False

        for table_name in owned_tables:
            expected = ENTITLEMENT_SCHEMA_COLUMNS[table_name]
            columns = cls._table_columns(conn, table_name)
            allowed_missing = {"delivered_at"} if table_name == "credit_charges" else set()
            if set(expected) - set(columns) - allowed_missing:
                return False
            if any(
                columns[column_name] != expected_type
                for column_name, expected_type in expected.items()
                if column_name in columns
            ):
                return False
        if "identity_aliases" in tables:
            if "users" not in tables:
                return False
            if not cls._identity_alias_status(conn)["ready"]:
                return False
        return True

    def _init_db(self) -> None:
        with self._connection() as conn:
            if not self._schema_can_be_initialized(conn):
                self._initialization_blocker = "incompatible_existing_schema"
                return
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
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS credit_charges (
                    charge_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL,
                    usage_date TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    delivered_at TEXT,
                    refunded_at TEXT,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS identity_aliases (
                    ledger_subject TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    FOREIGN KEY (user_id) REFERENCES users(user_id)
                )
                """
            )
            charge_columns = self._table_columns(conn, "credit_charges")
            if "delivered_at" not in charge_columns:
                conn.execute("ALTER TABLE credit_charges ADD COLUMN delivered_at TEXT")
            conn.execute(
                """
                UPDATE credit_charges
                SET state = 'delivered', delivered_at = COALESCE(delivered_at, created_at)
                WHERE state = 'spent'
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_entitlement_charges_recovery
                ON credit_charges(state, created_at)
                """
            )

    def bind_identity(
        self,
        ledger_subject: str,
        legacy_user_id: str,
        *,
        email: str | None = None,
    ) -> str:
        """Bind one scoped principal to the raw v0.6.2-compatible ledger owner.

        The raw ``user_id`` remains authoritative so an application rollback can
        still see every balance and charge. The scoped subject is retained as a
        durable one-to-one authentication binding; ambiguous claims fail closed.
        """
        scoped_subject = str(ledger_subject)
        raw_user_id = str(legacy_user_id)
        if not scoped_subject.strip() or not raw_user_id.strip():
            raise sqlite3.IntegrityError("entitlement identity subjects must be non-empty")
        if self._initialization_blocker:
            raise sqlite3.IntegrityError("entitlement identity schema is not ready")

        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if scoped_subject == raw_user_id:
                self._ensure_user(conn, raw_user_id, email)
                return raw_user_id

            scoped_binding = conn.execute(
                """
                SELECT user_id FROM identity_aliases
                WHERE ledger_subject = ?
                """,
                (scoped_subject,),
            ).fetchone()
            raw_binding = conn.execute(
                """
                SELECT ledger_subject FROM identity_aliases
                WHERE user_id = ?
                """,
                (raw_user_id,),
            ).fetchone()
            if (
                scoped_binding is not None
                and str(scoped_binding[0]) != raw_user_id
            ) or (
                raw_binding is not None
                and str(raw_binding[0]) != scoped_subject
            ):
                raise sqlite3.IntegrityError(
                    "entitlement identity binding collision"
                )

            scoped_user = conn.execute(
                "SELECT 1 FROM users WHERE user_id = ?",
                (scoped_subject,),
            ).fetchone()
            if scoped_user is not None:
                raise sqlite3.IntegrityError(
                    "scoped entitlement row requires manual reconciliation"
                )

            self._ensure_user(conn, raw_user_id, email)
            now = _utc_now()
            if scoped_binding is None:
                conn.execute(
                    """
                    INSERT INTO identity_aliases (
                        ledger_subject, user_id, created_at, updated_at
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (scoped_subject, raw_user_id, now, now),
                )
            else:
                conn.execute(
                    """
                    UPDATE identity_aliases
                    SET updated_at = ?
                    WHERE ledger_subject = ? AND user_id = ?
                    """,
                    (now, scoped_subject, raw_user_id),
                )
        return raw_user_id

    def schema_status(self) -> dict[str, object]:
        """Return a read-only integrity and required-schema status."""
        required_tables = set(ENTITLEMENT_SCHEMA_COLUMNS)
        missing_columns: dict[str, list[str]] = {}
        incompatible_column_types: dict[str, dict[str, dict[str, str]]] = {}
        charge_states: dict[str, int] = {}
        stale_pending_charges: int | None = None
        invalid_charge_states: list[str] = []
        credit_schema_ready = False
        identity_alias_status: dict[str, object] = {
            "ready": False,
            "primary_key_columns": [],
            "primary_key_valid": False,
            "unique_user_id_indexes": [],
            "unique_user_id_valid": False,
            "foreign_keys": [],
            "foreign_key_valid": False,
            "empty_bindings": None,
            "duplicate_ledger_subjects": None,
            "duplicate_user_ids": None,
            "orphan_bindings": None,
        }
        try:
            with self._connect() as conn:
                integrity_row = conn.execute("PRAGMA integrity_check").fetchone()
                tables = {
                    str(row[0])
                    for row in conn.execute(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    ).fetchall()
                }
                for table_name, required_columns in ENTITLEMENT_SCHEMA_COLUMNS.items():
                    if table_name not in tables:
                        continue
                    columns = self._table_columns(conn, table_name)
                    missing_for_table = sorted(set(required_columns) - set(columns))
                    if missing_for_table:
                        missing_columns[table_name] = missing_for_table
                    mismatches = {
                        column_name: {
                            "expected": expected_type,
                            "actual": columns[column_name] or "UNDECLARED",
                        }
                        for column_name, expected_type in required_columns.items()
                        if column_name in columns and columns[column_name] != expected_type
                    }
                    if mismatches:
                        incompatible_column_types[table_name] = mismatches
                identity_columns_ready = (
                    "identity_aliases" in tables
                    and "identity_aliases" not in missing_columns
                    and "identity_aliases" not in incompatible_column_types
                    and "users" in tables
                )
                if identity_columns_ready:
                    identity_alias_status = self._identity_alias_status(conn)
                credit_schema_ready = (
                    "credit_charges" in tables
                    and "credit_charges" not in missing_columns
                    and "credit_charges" not in incompatible_column_types
                )
                if credit_schema_ready:
                    charge_states = {
                        str(row[0]): int(row[1])
                        for row in conn.execute(
                            "SELECT state, COUNT(*) FROM credit_charges GROUP BY state"
                        ).fetchall()
                    }
                    invalid_charge_states = sorted(set(charge_states) - VALID_CHARGE_STATES)
                    stale_row = conn.execute(
                        """
                        SELECT COUNT(*) FROM credit_charges
                        WHERE state = 'pending' AND created_at <= ?
                        """,
                        (self._recovery_cutoff(),),
                    ).fetchone()
                    stale_pending_charges = int(stale_row[0]) if stale_row else 0
        except sqlite3.Error:
            return {
                "ready": False,
                "schema_version": ENTITLEMENT_SCHEMA_VERSION,
                "integrity": "unavailable",
                "missing_tables": sorted(required_tables),
                "missing_columns": {},
                "incompatible_column_types": {},
                "initialization_blocker": self._initialization_blocker,
                "identity_aliases": identity_alias_status,
                "invalid_charge_states": [],
                "charge_states": {},
                "pending_recovery": {
                    "lease_seconds": self.pending_charge_lease_seconds,
                    "batch_limit": PENDING_RECOVERY_BATCH_LIMIT,
                    "pending_charges": None,
                    "stale_pending_charges": None,
                    "last_recovery": dict(self._last_recovery),
                },
            }
        integrity = str(integrity_row[0]) if integrity_row else "unavailable"
        missing = sorted(required_tables - tables)
        return {
            "ready": (
                integrity == "ok"
                and not missing
                and not missing_columns
                and not incompatible_column_types
                and not self._initialization_blocker
                and not invalid_charge_states
                and identity_alias_status["ready"] is True
            ),
            "schema_version": ENTITLEMENT_SCHEMA_VERSION,
            "integrity": integrity,
            "missing_tables": missing,
            "missing_columns": missing_columns,
            "incompatible_column_types": incompatible_column_types,
            "initialization_blocker": self._initialization_blocker,
            "identity_aliases": identity_alias_status,
            "invalid_charge_states": invalid_charge_states,
            "charge_states": charge_states,
            "pending_recovery": {
                "lease_seconds": self.pending_charge_lease_seconds,
                "batch_limit": PENDING_RECOVERY_BATCH_LIMIT,
                "pending_charges": (
                    charge_states.get("pending", 0) if credit_schema_ready else None
                ),
                "stale_pending_charges": stale_pending_charges,
                "last_recovery": dict(self._last_recovery),
            },
        }

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
        self.recover_stale_pending()
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
        charge_id: str | None = None,
    ) -> tuple[bool, CreditStatus]:
        """Atomically reserve starter credits in a durable pending charge."""
        if amount < 0:
            raise ValueError("amount must be non-negative")
        self.recover_stale_pending()
        usage_date = usage_date or _today()
        effective_charge_id = charge_id or f"legacy:{uuid.uuid4().hex}"

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

            existing_charge = conn.execute(
                "SELECT state FROM credit_charges WHERE charge_id = ?",
                (effective_charge_id,),
            ).fetchone()
            if existing_charge is not None:
                return False, CreditStatus(
                    user_id=user_id,
                    email=stored_email,
                    date=usage_date,
                    daily_limit=int(daily_limit),
                    credits_spent=lifetime_spent,
                    credits_remaining=max(0, int(daily_limit) - lifetime_spent),
                    status=status,
                )

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
            conn.execute(
                """
                INSERT INTO credit_charges (
                    charge_id, user_id, usage_date, amount, state, created_at
                ) VALUES (?, ?, ?, ?, 'pending', ?)
                """,
                (effective_charge_id, user_id, usage_date, amount, _utc_now()),
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
                "pending",
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

    def finalize_delivery(
        self,
        user_id: str,
        amount: int,
        *,
        charge_id: str,
    ) -> CreditStatus | None:
        """Atomically mark one pending charge delivered before returning data."""
        if amount < 0:
            raise ValueError("amount must be non-negative")
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            charge = conn.execute(
                """
                SELECT usage_date, amount, state FROM credit_charges
                WHERE charge_id = ? AND user_id = ?
                """,
                (charge_id, user_id),
            ).fetchone()
            if (
                charge is None
                or int(charge[1]) != amount
                or str(charge[2]) != "pending"
            ):
                return None
            usage_date = str(charge[0])
            cursor = conn.execute(
                """
                UPDATE credit_charges
                SET state = 'delivered', delivered_at = ?
                WHERE charge_id = ? AND user_id = ? AND usage_date = ?
                  AND amount = ? AND state = 'pending'
                """,
                (_utc_now(), charge_id, user_id, usage_date, amount),
            )
            if cursor.rowcount != 1:
                return None
            user_row = conn.execute(
                "SELECT email, daily_limit, status FROM users WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            spent_row = conn.execute(
                """
                SELECT COALESCE(SUM(credits_spent), 0)
                FROM daily_usage WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            if user_row is None or spent_row is None:
                raise sqlite3.IntegrityError("Delivered charge has no entitlement balance")
            email, daily_limit, status = user_row
            lifetime_spent = int(spent_row[0])
            return CreditStatus(
                user_id=user_id,
                email=email,
                date=usage_date,
                daily_limit=int(daily_limit),
                credits_spent=lifetime_spent,
                credits_remaining=max(0, int(daily_limit) - lifetime_spent),
                status=str(status),
            )

    def _recovery_cutoff(self, now: datetime | None = None) -> str:
        current = now or datetime.now(UTC)
        if current.tzinfo is None:
            raise ValueError("recovery time must be timezone-aware")
        return (
            current.astimezone(UTC)
            - timedelta(seconds=self.pending_charge_lease_seconds)
        ).isoformat()

    def recover_stale_pending(
        self,
        *,
        now: datetime | None = None,
        limit: int = PENDING_RECOVERY_BATCH_LIMIT,
    ) -> dict[str, int]:
        """Refund a bounded batch of expired pending charges atomically."""
        if limit <= 0:
            raise ValueError("limit must be positive")
        bounded_limit = min(int(limit), PENDING_RECOVERY_BATCH_LIMIT)
        cutoff = self._recovery_cutoff(now)
        recovered_charges = 0
        recovered_credits = 0
        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            charges = conn.execute(
                """
                SELECT charge_id, user_id, usage_date, amount
                FROM credit_charges
                WHERE state = 'pending' AND created_at <= ?
                ORDER BY created_at, charge_id
                LIMIT ?
                """,
                (cutoff, bounded_limit),
            ).fetchall()
            for charge_id, user_id, usage_date, raw_amount in charges:
                amount = int(raw_amount)
                usage_row = conn.execute(
                    """
                    SELECT credits_spent FROM daily_usage
                    WHERE user_id = ? AND usage_date = ?
                    """,
                    (user_id, usage_date),
                ).fetchone()
                if usage_row is None or int(usage_row[0]) < amount:
                    raise sqlite3.IntegrityError(
                        "Pending charge exceeds its authoritative entitlement balance"
                    )
                cursor = conn.execute(
                    """
                    UPDATE credit_charges
                    SET state = 'refunded', refunded_at = ?
                    WHERE charge_id = ? AND state = 'pending' AND created_at <= ?
                    """,
                    (_utc_now(), charge_id, cutoff),
                )
                if cursor.rowcount != 1:
                    continue
                conn.execute(
                    """
                    UPDATE daily_usage
                    SET credits_spent = credits_spent - ?, updated_at = ?
                    WHERE user_id = ? AND usage_date = ?
                    """,
                    (amount, _utc_now(), user_id, usage_date),
                )
                user_row = conn.execute(
                    "SELECT daily_limit FROM users WHERE user_id = ?",
                    (user_id,),
                ).fetchone()
                spent_row = conn.execute(
                    """
                    SELECT COALESCE(SUM(credits_spent), 0)
                    FROM daily_usage WHERE user_id = ?
                    """,
                    (user_id,),
                ).fetchone()
                if user_row is None or spent_row is None:
                    raise sqlite3.IntegrityError(
                        "Recovered charge has no entitlement balance"
                    )
                remaining = max(0, int(user_row[0]) - int(spent_row[0]))
                self._record_event(
                    conn,
                    str(user_id),
                    str(usage_date),
                    "system_recovery",
                    "",
                    -amount,
                    remaining,
                    "stale_pending_refunded",
                )
                recovered_charges += 1
                recovered_credits += amount
            remaining_row = conn.execute(
                """
                SELECT COUNT(*) FROM credit_charges
                WHERE state = 'pending' AND created_at <= ?
                """,
                (cutoff,),
            ).fetchone()
            remaining_stale_charges = int(remaining_row[0]) if remaining_row else 0
        result = {
            "recovered_charges": recovered_charges,
            "recovered_credits": recovered_credits,
            "remaining_stale_charges": remaining_stale_charges,
        }
        self._last_recovery = dict(result)
        return result

    def refund(
        self,
        user_id: str,
        amount: int,
        *,
        tool_name: str,
        subject: str = "",
        usage_date: str | None = None,
        charge_id: str,
    ) -> CreditStatus:
        """Refund one failed upstream charge exactly once."""
        if amount < 0:
            raise ValueError("amount must be non-negative")
        requested_date = usage_date or _today()

        with self._connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_user(conn, user_id, None)
            charge = conn.execute(
                """
                SELECT usage_date, amount, state FROM credit_charges
                WHERE charge_id = ? AND user_id = ?
                """,
                (charge_id, user_id),
            ).fetchone()
            authoritative_date = str(charge[0]) if charge is not None else requested_date
            self._ensure_daily_usage(conn, user_id, authoritative_date)

            row = conn.execute(
                """
                SELECT u.email, u.daily_limit, u.status, du.credits_spent
                FROM users u
                JOIN daily_usage du ON du.user_id = u.user_id
                WHERE u.user_id = ? AND du.usage_date = ?
                """,
                (user_id, authoritative_date),
            ).fetchone()
            stored_email, daily_limit, status, credits_spent = row
            refundable = (
                charge is not None
                and int(charge[1]) == amount
                and str(charge[2]) == "pending"
            )
            new_spent = int(credits_spent)
            if refundable:
                if int(credits_spent) < amount:
                    raise sqlite3.IntegrityError(
                        "Pending charge exceeds its authoritative entitlement balance"
                    )
                charge_cursor = conn.execute(
                    """
                    UPDATE credit_charges
                    SET state = 'refunded', refunded_at = ?
                    WHERE charge_id = ? AND user_id = ? AND usage_date = ?
                      AND amount = ? AND state = 'pending'
                    """,
                    (_utc_now(), charge_id, user_id, authoritative_date, amount),
                )
                if charge_cursor.rowcount == 1:
                    new_spent = int(credits_spent) - amount
            conn.execute(
                """
                UPDATE daily_usage
                SET credits_spent = ?, updated_at = ?
                WHERE user_id = ? AND usage_date = ?
                """,
                (new_spent, _utc_now(), user_id, authoritative_date),
            )
            spent_row = conn.execute(
                """
                SELECT COALESCE(SUM(credits_spent), 0)
                FROM daily_usage
                WHERE user_id = ?
                """,
                (user_id,),
            ).fetchone()
            lifetime_spent = int(spent_row[0])
            remaining = max(0, int(daily_limit) - lifetime_spent)
            if new_spent != int(credits_spent):
                self._record_event(
                    conn,
                    user_id,
                    authoritative_date,
                    tool_name,
                    subject,
                    -amount,
                    remaining,
                    "refunded",
                )

        return CreditStatus(
            user_id=user_id,
            email=stored_email,
            date=authoritative_date,
            daily_limit=int(daily_limit),
            credits_spent=lifetime_spent,
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
