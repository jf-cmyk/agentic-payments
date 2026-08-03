import sqlite3
import os
import logging
import httpx
import hashlib
import json
import time
import uuid
from datetime import UTC, datetime, timedelta
from dataclasses import dataclass

from src.payment_limits import (
    MAX_CACHED_PAYMENT_RESPONSE_BYTES,
    MAX_PAYMENT_REPLAY_ENTRIES,
    MAX_PAYMENT_REPLAY_TTL_SECONDS,
)

logger = logging.getLogger(__name__)


STARTER_CREDIT_ALLOWANCE = float(os.environ.get("STARTER_CREDIT_ALLOWANCE", "50"))
_PAYMENT_REPLAY_HEADER_ALLOWLIST = frozenset(
    {
        "cache-control",
        "content-disposition",
        "content-language",
        "content-type",
        "etag",
        "last-modified",
        "vary",
    }
)


@dataclass(frozen=True)
class StarterAllowanceResult:
    subject: str
    subject_type: str
    balance_credits: float
    granted_credits: float
    eligible: bool
    reason: str


@dataclass(frozen=True)
class PaymentReservationResult:
    """Outcome of an atomic payment-proof lease attempt."""

    acquired: bool
    payment_id: str
    reservation_id: str | None
    state: str
    reason: str


class CreditManager:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or os.environ.get("CREDIT_DB_PATH", "credits.db")
        self._init_db()

    def _init_db(self):
        """Initialize the SQLite database with wallets and transactions tables."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Wallet balances
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS wallets (
                address TEXT PRIMARY KEY,
                balance_credits REAL DEFAULT 0.0,
                last_updated TIMESTAMP
            )
        ''')
        
        # Credit purchase log
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS credit_purchases (
                tx_hash TEXT PRIMARY KEY,
                address TEXT,
                amount_usdc REAL,
                credits_added REAL,
                timestamp TIMESTAMP
            )
        ''')
        
        # Trial History (with Ancestry Tracking)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trial_history (
                ip_hash TEXT PRIMARY KEY,
                address TEXT,
                funding_address TEXT,
                timestamp TIMESTAMP
            )
        ''')

        self._ensure_trial_column(cursor, "subject_hash", "TEXT")
        self._ensure_trial_column(cursor, "subject_type", "TEXT DEFAULT 'wallet'")
        self._ensure_trial_column(cursor, "device_hash", "TEXT")
        self._ensure_trial_column(cursor, "session_hash", "TEXT")
        self._ensure_trial_column(cursor, "user_agent_hash", "TEXT")

        # Persistent replay protection for paid data and credit-purchase proofs.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS payment_proofs (
                tx_hash TEXT PRIMARY KEY,
                network TEXT NOT NULL,
                amount_atomic INTEGER DEFAULT 0,
                recipient TEXT DEFAULT '',
                purpose TEXT DEFAULT '',
                timestamp TIMESTAMP
            )
        ''')

        # Existing rows predate the delivery lifecycle and represent already
        # consumed proofs, so migrations intentionally mark them finalized.
        self._ensure_table_column(
            cursor,
            "payment_proofs",
            "state",
            "TEXT NOT NULL DEFAULT 'finalized'",
        )
        self._ensure_table_column(cursor, "payment_proofs", "request_binding", "TEXT DEFAULT ''")
        self._ensure_table_column(cursor, "payment_proofs", "reservation_id", "TEXT")
        self._ensure_table_column(cursor, "payment_proofs", "attempt_id", "TEXT")
        self._ensure_table_column(cursor, "payment_proofs", "reserved_at", "REAL")
        self._ensure_table_column(cursor, "payment_proofs", "settled_at", "TIMESTAMP")
        self._ensure_table_column(
            cursor,
            "payment_proofs",
            "settlement_unknown_at",
            "TIMESTAMP",
        )
        self._ensure_table_column(cursor, "payment_proofs", "finalized_at", "TIMESTAMP")
        self._ensure_table_column(cursor, "payment_proofs", "released_at", "TIMESTAMP")
        self._ensure_table_column(cursor, "payment_proofs", "settlement_json", "TEXT")
        self._ensure_table_column(cursor, "payment_proofs", "response_status", "INTEGER")
        self._ensure_table_column(cursor, "payment_proofs", "response_headers_json", "TEXT")
        self._ensure_table_column(cursor, "payment_proofs", "response_body", "BLOB")
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_payment_proofs_state ON payment_proofs(state)"
        )

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS credit_charges (
                charge_id TEXT PRIMARY KEY,
                address TEXT NOT NULL,
                credits REAL NOT NULL,
                purpose TEXT NOT NULL DEFAULT '',
                state TEXT NOT NULL,
                created_at TIMESTAMP NOT NULL,
                refunded_at TIMESTAMP
            )
        ''')

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS rate_limit_events (
                scope TEXT NOT NULL,
                key_hash TEXT NOT NULL,
                occurred_at REAL NOT NULL
            )
        ''')
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_rate_limit_events_lookup "
            "ON rate_limit_events(scope, key_hash, occurred_at)"
        )

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS price_receipts (
                receipt_id TEXT PRIMARY KEY,
                product TEXT NOT NULL,
                subject TEXT DEFAULT '',
                payload_json TEXT NOT NULL,
                created_at TIMESTAMP
            )
        ''')
        
        conn.commit()
        conn.close()

    @staticmethod
    def _ensure_trial_column(cursor: sqlite3.Cursor, column: str, ddl_type: str) -> None:
        cursor.execute("PRAGMA table_info(trial_history)")
        columns = {row[1] for row in cursor.fetchall()}
        if column not in columns:
            cursor.execute(f"ALTER TABLE trial_history ADD COLUMN {column} {ddl_type}")

    @staticmethod
    def _ensure_table_column(
        cursor: sqlite3.Cursor,
        table: str,
        column: str,
        ddl_type: str,
    ) -> None:
        """Add one known migration column without interpolating caller input."""
        supported_tables = {"payment_proofs"}
        if table not in supported_tables:
            raise ValueError(f"Unsupported migration table: {table}")
        cursor.execute(f"PRAGMA table_info({table})")
        columns = {row[1] for row in cursor.fetchall()}
        if column not in columns:
            cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl_type}")

    def get_balance(self, address: str) -> float:
        """Get the current credit balance for a wallet."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        cursor.execute("SELECT balance_credits FROM wallets WHERE address = ?", (address,))
        row = cursor.fetchone()
        conn.close()
        return row[0] if row else 0.0

    async def _get_solana_balance(self, address: str) -> float:
        """Fetch Solana balance from on-chain RPC."""
        # Use a reliable public RPC if not configured in env
        rpc_url = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(rpc_url, json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getBalance",
                    "params": [address]
                })
                res.raise_for_status()
                data = res.json()
                
                # lamports to SOL (1 SOL = 10^9 lamports)
                lamports = data.get("result", {}).get("value", 0)
                return float(lamports) / 1000000000.0
        except Exception as e:
            logger.error(f"Failed to check SOL balance for {address}: {e}")
            return 0.0

    async def _get_wallet_metadata(self, address: str) -> dict:
        """Fetch transaction count and age from on-chain RPC signatures."""
        rpc_url = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(rpc_url, json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "getSignaturesForAddress",
                    "params": [address, {"limit": 10}]
                })
                res.raise_for_status()
                data = res.json()
                sigs = data.get("result", [])
                
                count = len(sigs)
                first_seen = datetime.now(UTC)
                if count > 0:
                    # Time of oldest signature in the batch
                    oldest_ts = sigs[-1].get("blockTime")
                    if oldest_ts:
                        first_seen = datetime.fromtimestamp(oldest_ts, UTC)
                    
                    # Attempt to find funding source (very basic: last sig's sender)
                    # Note: Full trace is complex, using count/age as primary signals
                
                return {
                    "count": count,
                    "age_hours": (datetime.now(UTC) - first_seen).total_seconds() / 3600.0,
                    "signatures": sigs
                }
        except Exception as e:
            logger.error(f"Failed to check history for {address}: {e}")
            return {"count": 0, "age_hours": 0}

    async def ensure_wallet_with_welcome_pack(self, address: str, ip: str) -> float:
        """
        Anti-Hopping Trial Grant Logic:
        1. Permanent IP Blacklist check.
        2. Verify Wallet Balance (>0.1 SOL).
        3. Verify Wallet History (>24h age OR >5 transactions).
        4. Verify Funding Ancestry (Ensure source isn't an existing trial claimer).
        """
        result = await self.ensure_starter_allowance(
            subject=address,
            subject_type="wallet",
            ip=ip,
            require_wallet_history=True,
        )
        return result.balance_credits if result.eligible else 0.0

    async def ensure_starter_allowance(
        self,
        *,
        subject: str,
        subject_type: str,
        ip: str,
        device_id: str | None = None,
        session_id: str | None = None,
        user_agent: str | None = None,
        require_wallet_history: bool = False,
    ) -> StarterAllowanceResult:
        """
        Grant the universal starter allowance once per eligible subject.

        The ledger still stores balances in the wallets table for compatibility,
        but subject may be a wallet, authenticated user id, agent id, device id,
        or session id. Trial history stores salted fingerprints for abuse checks.
        """
        clean_subject = subject.strip()
        clean_subject_type = subject_type.strip().lower() or "subject"
        if not clean_subject:
            return StarterAllowanceResult("", clean_subject_type, 0.0, 0.0, False, "missing_subject")

        # Do network I/O outside the SQLite write transaction. The authoritative
        # duplicate and balance checks are repeated under BEGIN IMMEDIATE below.
        wallet_metadata = {"age_hours": None, "count": None}
        if require_wallet_history and self.get_balance(clean_subject) <= 0:
            sol_balance = await self._get_solana_balance(clean_subject)
            wallet_metadata = await self._get_wallet_metadata(clean_subject)
            if sol_balance < 0.1:
                logger.warning(
                    "LOW STAKE: Wallet %s has only %.4f SOL. Denied.",
                    clean_subject,
                    sol_balance,
                )
                return StarterAllowanceResult(
                    clean_subject,
                    clean_subject_type,
                    0.0,
                    0.0,
                    False,
                    "wallet_stake_too_low",
                )
            if wallet_metadata["age_hours"] < 24 and wallet_metadata["count"] < 5:
                logger.warning(
                    "FRESH WALLET: %s is only %.1fh old with %s txs. Denied.",
                    clean_subject,
                    wallet_metadata["age_hours"],
                    wallet_metadata["count"],
                )
                return StarterAllowanceResult(
                    clean_subject,
                    clean_subject_type,
                    0.0,
                    0.0,
                    False,
                    "wallet_history_too_fresh",
                )

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            ip_fingerprint = _fingerprint_ip(ip)
            subject_hash = _fingerprint_value(clean_subject, "starter-subject")
            device_hash = _fingerprint_value(device_id, "starter-device") if device_id else None
            session_hash = _fingerprint_value(session_id, "starter-session") if session_id else None
            user_agent_hash = _fingerprint_value(user_agent, "starter-user-agent") if user_agent else None

            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute("SELECT balance_credits FROM wallets WHERE address = ?", (clean_subject,))
            row = cursor.fetchone()
            if row is not None:
                conn.commit()
                return StarterAllowanceResult(
                    clean_subject,
                    clean_subject_type,
                    float(row[0]),
                    0.0,
                    True,
                    "existing_balance",
                )

            cursor.execute(
                """
                SELECT address FROM trial_history
                WHERE ip_hash IN (?, ?)
                   OR subject_hash = ?
                   OR (? IS NOT NULL AND device_hash = ?)
                   OR (? IS NOT NULL AND session_hash = ?)
                """,
                (
                    ip_fingerprint,
                    ip,
                    subject_hash,
                    device_hash,
                    device_hash,
                    session_hash,
                    session_hash,
                ),
            )
            duplicate = cursor.fetchone()
            if duplicate:
                logger.warning(
                    "Duplicate starter allowance claim blocked for IP fingerprint %s",
                    ip_fingerprint[:12],
                )
                conn.rollback()
                return StarterAllowanceResult(
                    clean_subject,
                    clean_subject_type,
                    0.0,
                    0.0,
                    False,
                    "duplicate_trial_fingerprint",
                )

            cursor.execute('''
                INSERT INTO wallets (address, balance_credits, last_updated)
                VALUES (?, ?, ?)
            ''', (clean_subject, STARTER_CREDIT_ALLOWANCE, _utc_now()))
                
            cursor.execute('''
                INSERT INTO trial_history (
                    ip_hash, address, subject_hash, subject_type, device_hash,
                    session_hash, user_agent_hash, timestamp
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                ip_fingerprint,
                clean_subject,
                subject_hash,
                clean_subject_type,
                device_hash,
                session_hash,
                user_agent_hash,
                _utc_now(),
            ))
                
            cursor.execute('''
                INSERT INTO credit_purchases (tx_hash, address, amount_usdc, credits_added, timestamp)
                VALUES (?, ?, ?, ?, ?)
            ''', (
                f"STARTER_{subject_hash[:16]}",
                clean_subject,
                0.0,
                STARTER_CREDIT_ALLOWANCE,
                _utc_now(),
            ))
                
            conn.commit()
            logger.info(
                "Starter allowance granted: %.1f credits to %s:%s",
                STARTER_CREDIT_ALLOWANCE,
                clean_subject_type,
                clean_subject,
            )
            return StarterAllowanceResult(
                clean_subject,
                clean_subject_type,
                STARTER_CREDIT_ALLOWANCE,
                STARTER_CREDIT_ALLOWANCE,
                True,
                "starter_allowance_granted",
            )
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def add_credits(self, address: str, credits: float, tx_hash: str, amount_usdc: float):
        """Add credits to a wallet and log the transaction."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            # Upsert wallet
            cursor.execute('''
                INSERT INTO wallets (address, balance_credits, last_updated)
                VALUES (?, ?, ?)
                ON CONFLICT(address) DO UPDATE SET
                    balance_credits = balance_credits + excluded.balance_credits,
                    last_updated = excluded.last_updated
            ''', (address, credits, _utc_now()))
            
            # Log purchase
            cursor.execute('''
                INSERT INTO credit_purchases (tx_hash, address, amount_usdc, credits_added, timestamp)
                VALUES (?, ?, ?, ?, ?)
            ''', (tx_hash, address, amount_usdc, credits, _utc_now()))
            
            conn.commit()
            logger.info(f"Credited {credits} to {address} (TX: {tx_hash})")
        except sqlite3.IntegrityError:
            # Replay protection at the DB level
            logger.warning(f"Duplicate credit attempt for TX: {tx_hash}")
            conn.rollback()
        finally:
            conn.close()

    def spend_credits(
        self,
        address: str,
        credits: float,
        *,
        charge_id: str | None = None,
        purpose: str = "",
    ) -> bool:
        """Atomically spend credits and create an idempotency record."""
        if credits <= 0:
            return False
        effective_charge_id = charge_id or f"legacy:{uuid.uuid4().hex}"
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(
                "SELECT address, credits, state FROM credit_charges WHERE charge_id = ?",
                (effective_charge_id,),
            )
            if cursor.fetchone() is not None:
                conn.rollback()
                return False
            cursor.execute('''
                UPDATE wallets SET balance_credits = balance_credits - ?, last_updated = ?
                WHERE address = ? AND balance_credits >= ?
            ''', (credits, _utc_now(), address, credits))

            if cursor.rowcount != 1:
                conn.rollback()
                return False
            cursor.execute(
                """
                INSERT INTO credit_charges (
                    charge_id, address, credits, purpose, state, created_at
                ) VALUES (?, ?, ?, ?, 'spent', ?)
                """,
                (effective_charge_id, address, credits, purpose, _utc_now()),
            )
            
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error spending credits for {address}: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()

    def refund_credits(self, address: str, credits: float, *, charge_id: str) -> bool:
        """Refund one recorded charge exactly once using compare-and-set."""
        if credits <= 0 or not charge_id:
            return False

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute(
                """
                UPDATE credit_charges
                SET state = 'refunded', refunded_at = ?
                WHERE charge_id = ? AND address = ? AND credits = ? AND state = 'spent'
                """,
                (_utc_now(), charge_id, address, credits),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return False
            cursor.execute(
                """
                UPDATE wallets
                SET balance_credits = balance_credits + ?, last_updated = ?
                WHERE address = ?
                """,
                (credits, _utc_now(), address),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return False
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error refunding credits for {address}: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()

    def record_payment_proof(
        self,
        tx_hash: str,
        network: str,
        amount_atomic: int,
        recipient: str,
        purpose: str,
    ) -> bool:
        """Persist an already-consumed legacy proof as finalized."""
        canonical_id = _canonical_payment_id(tx_hash, network)
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO payment_proofs (
                    tx_hash, network, amount_atomic, recipient, purpose, timestamp,
                    state, finalized_at
                )
                VALUES (?, ?, ?, ?, ?, ?, 'finalized', ?)
            ''', (
                canonical_id,
                network,
                int(amount_atomic),
                recipient,
                purpose,
                _utc_now(),
                _utc_now(),
            ))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            conn.rollback()
            logger.warning("Duplicate payment proof rejected: %s", canonical_id)
            return False
        finally:
            conn.close()

    def reserve_payment_proof(
        self,
        *,
        payment_id: str,
        network: str,
        amount_atomic: int,
        recipient: str,
        purpose: str,
        request_binding: str,
        attempt_id: str,
        lease_seconds: int,
        now: float | None = None,
        existing_only: bool = False,
    ) -> PaymentReservationResult:
        """Acquire an exclusive proof lease, allowing only exact-bound retries."""
        canonical_id = _canonical_payment_id(payment_id, network)
        if not canonical_id or not request_binding or lease_seconds <= 0:
            return PaymentReservationResult(
                False,
                canonical_id,
                None,
                "invalid",
                "invalid_payment_reservation",
            )
        reserved_at = float(now if now is not None else time.time())
        reservation_id = uuid.uuid4().hex
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                """
                SELECT state, request_binding, reservation_id, reserved_at
                FROM payment_proofs WHERE tx_hash = ?
                """,
                (canonical_id,),
            ).fetchone()
            if row is None:
                if existing_only:
                    conn.rollback()
                    return PaymentReservationResult(
                        False,
                        canonical_id,
                        None,
                        "missing",
                        "payment_reservation_missing",
                    )
                conn.execute(
                    """
                    INSERT INTO payment_proofs (
                        tx_hash, network, amount_atomic, recipient, purpose, timestamp,
                        state, request_binding, reservation_id, attempt_id, reserved_at
                    ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)
                    """,
                    (
                        canonical_id,
                        network,
                        int(amount_atomic),
                        recipient,
                        purpose,
                        _utc_now(),
                        request_binding,
                        reservation_id,
                        attempt_id,
                        reserved_at,
                    ),
                )
                conn.commit()
                return PaymentReservationResult(
                    True,
                    canonical_id,
                    reservation_id,
                    "pending",
                    "reserved",
                )

            state = str(row["state"] or "finalized")
            existing_binding = str(row["request_binding"] or "")
            if state == "finalized":
                conn.rollback()
                return PaymentReservationResult(
                    False,
                    canonical_id,
                    None,
                    state,
                    "payment_already_finalized",
                )
            if existing_binding != request_binding:
                conn.rollback()
                return PaymentReservationResult(
                    False,
                    canonical_id,
                    None,
                    state,
                    "payment_bound_to_different_request",
                )
            if state == "settlement_unknown":
                conn.rollback()
                return PaymentReservationResult(
                    False,
                    canonical_id,
                    None,
                    state,
                    "payment_settlement_reconciliation_required",
                )

            existing_reserved_at = float(row["reserved_at"] or 0)
            stale = state == "pending" and reserved_at - existing_reserved_at >= lease_seconds
            retryable = state == "released" or stale
            if not retryable:
                conn.rollback()
                return PaymentReservationResult(
                    False,
                    canonical_id,
                    None,
                    state,
                    "payment_reservation_in_progress",
                )

            cursor = conn.execute(
                """
                UPDATE payment_proofs
                SET state = 'pending', reservation_id = ?, attempt_id = ?, reserved_at = ?,
                    released_at = NULL
                WHERE tx_hash = ? AND state = ? AND request_binding = ?
                """,
                (
                    reservation_id,
                    attempt_id,
                    reserved_at,
                    canonical_id,
                    state,
                    request_binding,
                ),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return PaymentReservationResult(
                    False,
                    canonical_id,
                    None,
                    state,
                    "payment_reservation_race",
                )
            conn.commit()
            return PaymentReservationResult(
                True,
                canonical_id,
                reservation_id,
                "pending",
                "stale_lease_reclaimed" if stale else "released_payment_retried",
            )
        except sqlite3.Error:
            conn.rollback()
            raise
        finally:
            conn.close()

    def finalize_payment_proof(
        self,
        *,
        payment_id: str,
        reservation_id: str,
        settlement: dict | None = None,
        response_status: int | None = None,
        response_headers: dict[str, str] | None = None,
        response_body: bytes | None = None,
        replay_ttl_seconds: int = MAX_PAYMENT_REPLAY_TTL_SECONDS,
        replay_max_entries: int = MAX_PAYMENT_REPLAY_ENTRIES,
    ) -> bool:
        """Finalize an active reservation with an optional bounded replay response."""
        cached_body, safe_headers = self._prepare_payment_response_cache(
            response_status=response_status,
            response_headers=response_headers,
            response_body=response_body,
        )
        self._validate_payment_replay_controls(
            replay_ttl_seconds=replay_ttl_seconds,
            replay_max_entries=replay_max_entries,
        )
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE payment_proofs
                SET state = 'finalized', finalized_at = ?, settlement_json = ?,
                    response_status = ?, response_headers_json = ?, response_body = ?
                WHERE tx_hash = ? AND reservation_id = ?
                  AND state IN ('pending', 'settled')
                """,
                (
                    _utc_now(),
                    json.dumps(settlement, sort_keys=True, default=str) if settlement else None,
                    response_status,
                    json.dumps(safe_headers, sort_keys=True) if response_status is not None else None,
                    cached_body,
                    payment_id,
                    reservation_id,
                ),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return False
            self._prune_payment_response_cache(
                conn,
                replay_ttl_seconds=replay_ttl_seconds,
                replay_max_entries=replay_max_entries,
            )
            conn.commit()
            return True
        except sqlite3.Error:
            conn.rollback()
            raise
        finally:
            conn.close()

    @staticmethod
    def _prepare_payment_response_cache(
        *,
        response_status: int | None,
        response_headers: dict[str, str] | None,
        response_body: bytes | None,
    ) -> tuple[bytes | None, dict[str, str]]:
        """Validate and sanitize a bounded replay response before persistence."""
        cached_body = bytes(response_body) if response_body is not None else None
        if cached_body is not None and len(cached_body) > MAX_CACHED_PAYMENT_RESPONSE_BYTES:
            raise ValueError("Payment response exceeds the replay-cache limit")
        if response_status is not None and not 100 <= int(response_status) <= 399:
            raise ValueError("Only successful payment responses may be cached")
        if (response_status is None) != (cached_body is None):
            raise ValueError("Cached payment status and body must be supplied together")
        safe_headers = {
            str(key).lower(): str(value)
            for key, value in (response_headers or {}).items()
            if str(key).lower() in _PAYMENT_REPLAY_HEADER_ALLOWLIST
            and "\r" not in str(value)
            and "\n" not in str(value)
        }
        return cached_body, safe_headers

    @staticmethod
    def _validate_payment_replay_controls(
        *,
        replay_ttl_seconds: int,
        replay_max_entries: int,
    ) -> None:
        if replay_ttl_seconds <= 0 or replay_max_entries <= 0:
            raise ValueError("Payment replay retention controls must be positive")
        if replay_ttl_seconds > MAX_PAYMENT_REPLAY_TTL_SECONDS:
            raise ValueError("Payment replay TTL exceeds the hard retention limit")
        if replay_max_entries > MAX_PAYMENT_REPLAY_ENTRIES:
            raise ValueError("Payment replay entries exceed the hard retention limit")

    @staticmethod
    def _prune_payment_response_cache(
        conn: sqlite3.Connection,
        *,
        replay_ttl_seconds: int,
        replay_max_entries: int,
    ) -> None:
        """Bound cached bodies for both settled checkpoints and final responses."""
        replay_cutoff = (
            datetime.now(UTC) - timedelta(seconds=replay_ttl_seconds)
        ).isoformat()
        conn.execute(
            """
            UPDATE payment_proofs
            SET response_status = NULL, response_headers_json = NULL, response_body = NULL
            WHERE response_body IS NOT NULL
              AND COALESCE(settled_at, finalized_at) < ?
            """,
            (replay_cutoff,),
        )
        conn.execute(
            """
            UPDATE payment_proofs
            SET response_status = NULL, response_headers_json = NULL, response_body = NULL
            WHERE tx_hash IN (
                SELECT tx_hash FROM payment_proofs
                WHERE response_body IS NOT NULL
                ORDER BY COALESCE(settled_at, finalized_at) DESC
                LIMIT -1 OFFSET ?
            )
            """,
            (replay_max_entries,),
        )

    def checkpoint_settled_payment(
        self,
        *,
        payment_id: str,
        reservation_id: str,
        settlement: dict,
        response_status: int,
        response_headers: dict[str, str] | None,
        response_body: bytes,
        replay_ttl_seconds: int = MAX_PAYMENT_REPLAY_TTL_SECONDS,
        replay_max_entries: int = MAX_PAYMENT_REPLAY_ENTRIES,
    ) -> bool:
        """Durably checkpoint remote settlement before local finalization.

        A retry can promote this exact-bound response to ``finalized`` without
        asking the facilitator to consume the payment authorization again.
        """
        if not isinstance(settlement, dict) or settlement.get("success") is not True:
            raise ValueError("Only successful settlements may be checkpointed")
        cached_body, safe_headers = self._prepare_payment_response_cache(
            response_status=response_status,
            response_headers=response_headers,
            response_body=response_body,
        )
        if cached_body is None:
            raise ValueError("A settled payment checkpoint requires a replay response")
        self._validate_payment_replay_controls(
            replay_ttl_seconds=replay_ttl_seconds,
            replay_max_entries=replay_max_entries,
        )

        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE payment_proofs
                SET state = 'settled', settled_at = ?, settlement_json = ?,
                    response_status = ?, response_headers_json = ?, response_body = ?
                WHERE tx_hash = ? AND reservation_id = ? AND state = 'pending'
                """,
                (
                    _utc_now(),
                    json.dumps(settlement, sort_keys=True, default=str),
                    int(response_status),
                    json.dumps(safe_headers, sort_keys=True),
                    cached_body,
                    payment_id,
                    reservation_id,
                ),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return False
            self._prune_payment_response_cache(
                conn,
                replay_ttl_seconds=replay_ttl_seconds,
                replay_max_entries=replay_max_entries,
            )
            conn.commit()
            return True
        except sqlite3.Error:
            conn.rollback()
            raise
        finally:
            conn.close()

    def finalized_payment_response(
        self,
        *,
        payment_id: str,
        request_binding: str,
        max_age_seconds: int = MAX_PAYMENT_REPLAY_TTL_SECONDS,
    ) -> dict | None:
        """Return an exact-bound replay, recovering a settled checkpoint once."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """
                SELECT state, response_status, response_headers_json, response_body,
                       settlement_json, settled_at, finalized_at
                FROM payment_proofs
                WHERE tx_hash = ? AND state IN ('settled', 'finalized')
                  AND request_binding = ?
                """,
                (payment_id, request_binding),
            ).fetchone()
            if row is None or row["response_status"] is None or row["response_body"] is None:
                return None
            if max_age_seconds <= 0:
                return None
            max_age_seconds = min(max_age_seconds, MAX_PAYMENT_REPLAY_TTL_SECONDS)
            try:
                completed_at = datetime.fromisoformat(
                    str(row["settled_at"] or row["finalized_at"])
                )
            except (TypeError, ValueError):
                return None
            if datetime.now(UTC) - completed_at > timedelta(seconds=max_age_seconds):
                return None
            body = bytes(row["response_body"])
            if len(body) > MAX_CACHED_PAYMENT_RESPONSE_BYTES:
                return None
            try:
                headers = json.loads(row["response_headers_json"] or "{}")
                settlement = json.loads(row["settlement_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                return None
            if not isinstance(headers, dict) or not isinstance(settlement, dict):
                return None
            if row["state"] == "settled":
                conn.execute("BEGIN IMMEDIATE")
                cursor = conn.execute(
                    """
                    UPDATE payment_proofs
                    SET state = 'finalized', finalized_at = ?
                    WHERE tx_hash = ? AND request_binding = ? AND state = 'settled'
                    """,
                    (_utc_now(), payment_id, request_binding),
                )
                if cursor.rowcount != 1:
                    conn.rollback()
                    return None
                conn.commit()
            return {
                "status_code": int(row["response_status"]),
                "headers": {
                    str(key): str(value)
                    for key, value in headers.items()
                    if str(key).lower() in _PAYMENT_REPLAY_HEADER_ALLOWLIST
                },
                "body": body,
                "settlement": settlement,
            }
        finally:
            conn.close()

    def release_payment_proof(self, *, payment_id: str, reservation_id: str) -> bool:
        """Release a failed delivery lease for an exact-bound retry."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE payment_proofs
                SET state = 'released', released_at = ?
                WHERE tx_hash = ? AND reservation_id = ? AND state = 'pending'
                """,
                (_utc_now(), payment_id, reservation_id),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return False
            conn.commit()
            return True
        except sqlite3.Error:
            conn.rollback()
            raise
        finally:
            conn.close()

    def mark_payment_settlement_unknown(
        self,
        *,
        payment_id: str,
        reservation_id: str,
    ) -> bool:
        """Quarantine a proof whose remote settlement outcome is ambiguous."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(
                """
                UPDATE payment_proofs
                SET state = 'settlement_unknown', settlement_unknown_at = ?
                WHERE tx_hash = ? AND reservation_id = ? AND state = 'pending'
                """,
                (_utc_now(), payment_id, reservation_id),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return False
            conn.commit()
            return True
        except sqlite3.Error:
            conn.rollback()
            raise
        finally:
            conn.close()

    def finalize_payment_and_add_credits(
        self,
        *,
        payment_id: str,
        reservation_id: str,
        address: str,
        credits: float,
        amount_usdc: float,
        settlement: dict | None = None,
    ) -> bool:
        """Atomically finalize a bulk payment and grant its purchased credits."""
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            proof = conn.execute(
                """
                SELECT state FROM payment_proofs
                WHERE tx_hash = ? AND reservation_id = ?
                """,
                (payment_id, reservation_id),
            ).fetchone()
            if proof is None or proof[0] != "pending":
                conn.rollback()
                return False
            conn.execute(
                """
                INSERT INTO credit_purchases (
                    tx_hash, address, amount_usdc, credits_added, timestamp
                ) VALUES (?, ?, ?, ?, ?)
                """,
                (payment_id, address, amount_usdc, credits, _utc_now()),
            )
            conn.execute(
                """
                INSERT INTO wallets (address, balance_credits, last_updated)
                VALUES (?, ?, ?)
                ON CONFLICT(address) DO UPDATE SET
                    balance_credits = balance_credits + excluded.balance_credits,
                    last_updated = excluded.last_updated
                """,
                (address, credits, _utc_now()),
            )
            cursor = conn.execute(
                """
                UPDATE payment_proofs
                SET state = 'finalized', finalized_at = ?, settlement_json = ?
                WHERE tx_hash = ? AND reservation_id = ? AND state = 'pending'
                """,
                (
                    _utc_now(),
                    json.dumps(settlement, sort_keys=True, default=str) if settlement else None,
                    payment_id,
                    reservation_id,
                ),
            )
            if cursor.rowcount != 1:
                conn.rollback()
                return False
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            conn.rollback()
            return False
        except sqlite3.Error:
            conn.rollback()
            raise
        finally:
            conn.close()

    def payment_proof_state(self, payment_id: str) -> dict | None:
        """Return a non-secret proof state for tests and operational checks."""
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            row = conn.execute(
                """
                SELECT tx_hash, network, purpose, state, request_binding,
                       reservation_id, attempt_id, reserved_at, settled_at,
                       settlement_unknown_at, finalized_at, released_at,
                       response_status, response_body IS NOT NULL AS has_cached_response
                FROM payment_proofs WHERE tx_hash = ?
                """,
                (payment_id,),
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            conn.close()

    def check_rate_limit(
        self,
        *,
        scope: str,
        key: str,
        per_minute: int,
        per_day: int,
        now: float | None = None,
    ) -> tuple[bool, int | None, str | None]:
        """Persist fixed-window rate-limit events across process restarts."""
        if per_minute <= 0 and per_day <= 0:
            return True, None, None
        current = float(now if now is not None else time.time())
        key_hash = _fingerprint_value(key, "rate-limit")
        conn = sqlite3.connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "DELETE FROM rate_limit_events WHERE occurred_at <= ?",
                (current - 86_400,),
            )
            rows = conn.execute(
                """
                SELECT occurred_at FROM rate_limit_events
                WHERE scope = ? AND key_hash = ?
                ORDER BY occurred_at ASC
                """,
                (scope, key_hash),
            ).fetchall()
            day_hits = [float(row[0]) for row in rows]
            minute_hits = [item for item in day_hits if item > current - 60]
            if per_minute > 0 and len(minute_hits) >= per_minute:
                retry_after = max(1, int(minute_hits[0] + 60 - current) + 1)
                conn.commit()
                return False, retry_after, "minute"
            if per_day > 0 and len(day_hits) >= per_day:
                retry_after = max(1, int(day_hits[0] + 86_400 - current) + 1)
                conn.commit()
                return False, retry_after, "day"
            conn.execute(
                "INSERT INTO rate_limit_events (scope, key_hash, occurred_at) VALUES (?, ?, ?)",
                (scope, key_hash, current),
            )
            conn.commit()
            return True, None, None
        except sqlite3.Error:
            conn.rollback()
            raise
        finally:
            conn.close()

    def wallet_inflow_summary(self, *, days: int = 30, limit: int = 100) -> dict:
        """Return read-only wallet inflow rows for the internal dashboard."""
        cutoff = datetime.now(UTC) - timedelta(days=days)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        try:
            direct_rows = conn.execute(
                '''
                SELECT tx_hash, network, amount_atomic, recipient, purpose, timestamp
                FROM payment_proofs
                WHERE timestamp >= ?
                  AND state = 'finalized'
                  AND amount_atomic > 0
                  AND (purpose IS NULL OR purpose NOT LIKE 'credits:%')
                ORDER BY timestamp DESC
                LIMIT ?
                ''',
                (cutoff.isoformat(), limit),
            ).fetchall()
            purchase_rows = conn.execute(
                '''
                SELECT cp.tx_hash, cp.address, cp.amount_usdc, cp.credits_added,
                       cp.timestamp, pp.network, pp.recipient, pp.purpose
                FROM credit_purchases cp
                LEFT JOIN payment_proofs pp ON pp.tx_hash = cp.tx_hash
                WHERE cp.timestamp >= ?
                  AND cp.amount_usdc > 0
                ORDER BY cp.timestamp DESC
                LIMIT ?
                ''',
                (cutoff.isoformat(), limit),
            ).fetchall()
        finally:
            conn.close()

        rows = []
        for row in direct_rows:
            amount_usdc = round(float(row["amount_atomic"] or 0) / 1_000_000, 6)
            rows.append(
                {
                    "timestamp": row["timestamp"],
                    "kind": "direct_x402",
                    "network": row["network"],
                    "amount_usdc": amount_usdc,
                    "credits_added": None,
                    "wallet": None,
                    "recipient": row["recipient"],
                    "purpose": row["purpose"],
                    "tx_hash": row["tx_hash"],
                }
            )
        for row in purchase_rows:
            rows.append(
                {
                    "timestamp": row["timestamp"],
                    "kind": "credit_topup",
                    "network": row["network"] or "",
                    "amount_usdc": float(row["amount_usdc"] or 0.0),
                    "credits_added": float(row["credits_added"] or 0.0),
                    "wallet": row["address"],
                    "recipient": row["recipient"] or "",
                    "purpose": row["purpose"] or "credits",
                    "tx_hash": row["tx_hash"],
                }
            )

        rows.sort(key=lambda item: str(item.get("timestamp") or ""), reverse=True)
        rows = rows[:limit]
        direct_count = sum(1 for row in rows if row["kind"] == "direct_x402")
        topup_count = sum(1 for row in rows if row["kind"] == "credit_topup")
        total_usdc = round(sum(float(row.get("amount_usdc") or 0.0) for row in rows), 6)
        return {
            "window_days": days,
            "total_inflows": len(rows),
            "direct_x402_count": direct_count,
            "credit_topup_count": topup_count,
            "total_usdc": total_usdc,
            "latest_timestamp": rows[0]["timestamp"] if rows else None,
            "rows": rows,
        }

    def store_price_receipt(
        self,
        *,
        receipt_id: str,
        product: str,
        subject: str,
        payload: dict,
    ) -> None:
        """Persist an audit/provenance receipt payload for later lookup."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute(
                '''
                INSERT OR REPLACE INTO price_receipts (
                    receipt_id, product, subject, payload_json, created_at
                )
                VALUES (?, ?, ?, ?, ?)
                ''',
                (
                    receipt_id,
                    product,
                    subject,
                    json.dumps(payload, default=str, sort_keys=True),
                    _utc_now(),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_price_receipt(self, receipt_id: str) -> dict | None:
        """Return a stored receipt payload by id."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute(
                "SELECT payload_json FROM price_receipts WHERE receipt_id = ?",
                (receipt_id,),
            )
            row = cursor.fetchone()
            if not row:
                return None
            return json.loads(row[0])
        finally:
            conn.close()

# Starter-credit product costs. x402 prices stay in USDC separately.
CREDIT_COSTS = {
    "raw_vwap": 1.0,
    "raw_bidask": 1.0,
    "raw_state": 1.0,
    "raw_vwap_30m": 1.0,
    "raw_vwap_24h": 1.0,
    "fx": 2.0,
    "metals": 2.0,
    "market_brief": 10.0,
    "pre_trade_check": 5.0,
    "audit_receipt": 10.0,
    "macro_snapshot": 25.0,
    "token_quality_indicator": 15.0,
    "state_divergence_indicator": 15.0,
    "solana_token_brief": 25.0,
    "trader_alpha_pack": 50.0,
    "rwa_blocksize_benchmark": 10.0,
    "provenance_lookup": 0.0,
}

# Bulk Tier Credits
BULK_TIERS = {
    "starter": {"price": 0.90, "credits": 1000.0},      # 10% discount
    "pro": {"price": 8.00, "credits": 10000.0},         # 20% discount
    "institutional": {"price": 60.00, "credits": 100000.0} # 40% discount
}


def _fingerprint_ip(ip: str) -> str:
    """Hash client IPs before storing trial history."""
    from src.security_config import hash_salt

    salt = hash_salt("TRIAL_IP_HASH_SALT")
    return hashlib.sha256(f"{salt}:{ip}".encode("utf-8")).hexdigest()


def _fingerprint_value(value: str, namespace: str) -> str:
    """Hash trial identifiers before storage."""
    from src.security_config import hash_salt

    salt = hash_salt("TRIAL_IP_HASH_SALT")
    return hashlib.sha256(f"{salt}:{namespace}:{value}".encode("utf-8")).hexdigest()


def _canonical_payment_id(payment_id: str, network: str) -> str:
    """Canonicalize case-insensitive EVM transaction identifiers."""
    clean = payment_id.strip()
    return clean.lower() if network.strip().lower().startswith("eip155:") else clean


def _utc_now() -> str:
    """Return an ISO-8601 UTC timestamp for SQLite storage."""
    return datetime.now(UTC).isoformat()
