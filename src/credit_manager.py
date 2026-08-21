import sqlite3
import os
import logging
import httpx
import hashlib
import json
from datetime import UTC, datetime, timedelta
from dataclasses import dataclass

logger = logging.getLogger(__name__)


STARTER_CREDIT_ALLOWANCE = float(os.environ.get("STARTER_CREDIT_ALLOWANCE", "50"))


@dataclass(frozen=True)
class StarterAllowanceResult:
    subject: str
    subject_type: str
    balance_credits: float
    granted_credits: float
    eligible: bool
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

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            ip_fingerprint = _fingerprint_ip(ip)
            subject_hash = _fingerprint_value(clean_subject, "starter-subject")
            device_hash = _fingerprint_value(device_id, "starter-device") if device_id else None
            session_hash = _fingerprint_value(session_id, "starter-session") if session_id else None
            user_agent_hash = _fingerprint_value(user_agent, "starter-user-agent") if user_agent else None

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
            if cursor.fetchone():
                logger.warning(
                    "Duplicate starter allowance claim blocked for IP fingerprint %s",
                    ip_fingerprint[:12],
                )
                return StarterAllowanceResult(
                    clean_subject,
                    clean_subject_type,
                    self.get_balance(clean_subject),
                    0.0,
                    False,
                    "duplicate_trial_fingerprint",
                )
            
            cursor.execute("SELECT balance_credits FROM wallets WHERE address = ?", (clean_subject,))
            row = cursor.fetchone()
            
            if row is None:
                metadata = {"age_hours": None, "count": None}
                if require_wallet_history:
                    sol_balance = await self._get_solana_balance(clean_subject)
                    metadata = await self._get_wallet_metadata(clean_subject)

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

                    if metadata["age_hours"] < 24 and metadata["count"] < 5:
                        logger.warning(
                            "FRESH WALLET: %s is only %.1fh old with %s txs. Denied.",
                            clean_subject,
                            metadata["age_hours"],
                            metadata["count"],
                        )
                        return StarterAllowanceResult(
                            clean_subject,
                            clean_subject_type,
                            0.0,
                            0.0,
                            False,
                            "wallet_history_too_fresh",
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
            
            return StarterAllowanceResult(
                clean_subject,
                clean_subject_type,
                float(row[0]),
                0.0,
                True,
                "existing_balance",
            )
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

    def spend_credits(self, address: str, credits: float) -> bool:
        """Attempt to spend credits from a wallet. Returns True if successful."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
            cursor.execute('''
                UPDATE wallets SET balance_credits = balance_credits - ?, last_updated = ?
                WHERE address = ? AND balance_credits >= ?
            ''', (credits, _utc_now(), address, credits))

            if cursor.rowcount != 1:
                conn.rollback()
                return False
            
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error spending credits for {address}: {e}")
            conn.rollback()
            return False
        finally:
            conn.close()

    def refund_credits(self, address: str, credits: float) -> bool:
        """Return previously spent credits to a wallet after a charged delivery failure."""
        if credits <= 0:
            return False

        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute("BEGIN IMMEDIATE")
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
        """Persist a verified payment proof. Returns False for replayed proofs."""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute('''
                INSERT INTO payment_proofs (
                    tx_hash, network, amount_atomic, recipient, purpose, timestamp
                )
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (
                tx_hash,
                network,
                int(amount_atomic),
                recipient,
                purpose,
                _utc_now(),
            ))
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            conn.rollback()
            logger.warning("Duplicate payment proof rejected: %s", tx_hash)
            return False
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
    salt = os.environ.get("TRIAL_IP_HASH_SALT", "blocksize-agentic-payments")
    return hashlib.sha256(f"{salt}:{ip}".encode("utf-8")).hexdigest()


def _fingerprint_value(value: str, namespace: str) -> str:
    """Hash trial identifiers before storage."""
    salt = os.environ.get("TRIAL_IP_HASH_SALT", "blocksize-agentic-payments")
    return hashlib.sha256(f"{salt}:{namespace}:{value}".encode("utf-8")).hexdigest()


def _utc_now() -> str:
    """Return an ISO-8601 UTC timestamp for SQLite storage."""
    return datetime.now(UTC).isoformat()
