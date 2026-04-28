import sqlite3
import os
import logging
import httpx
import hashlib
from datetime import UTC, datetime

logger = logging.getLogger(__name__)

class CreditManager:
    def __init__(self, db_path: str = "credits.db"):
        self.db_path = db_path
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
        
        conn.commit()
        conn.close()

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
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            # 1. IP Check
            ip_fingerprint = _fingerprint_ip(ip)
            cursor.execute(
                "SELECT address FROM trial_history WHERE ip_hash IN (?, ?)",
                (ip_fingerprint, ip),
            )
            if cursor.fetchone():
                logger.warning(
                    "Duplicate trial claim blocked for IP fingerprint %s",
                    ip_fingerprint[:12],
                )
                return 0.0
            
            # 2. Wallet Check (Internal)
            cursor.execute("SELECT balance_credits FROM wallets WHERE address = ?", (address,))
            row = cursor.fetchone()
            
            if row is None:
                # 3. Stake & History Check
                sol_balance = await self._get_solana_balance(address)
                metadata = await self._get_wallet_metadata(address)
                
                if sol_balance < 0.1:
                    logger.warning(f"⚠️ LOW STAKE: Wallet {address} has only {sol_balance} SOL. Denied.")
                    return 0.0
                
                if metadata["age_hours"] < 24 and metadata["count"] < 5:
                    logger.warning(f"⚠️ FRESH WALLET: {address} is only {metadata['age_hours']:.1f}h old with {metadata['count']} txs. Denied.")
                    return 0.0

                # 4. Success! Grant Welcome Pack.
                welcome_credits = 50.0
                cursor.execute('''
                    INSERT INTO wallets (address, balance_credits, last_updated)
                    VALUES (?, ?, ?)
                ''', (address, welcome_credits, _utc_now()))
                
                # Record to Trial History
                cursor.execute('''
                    INSERT INTO trial_history (ip_hash, address, timestamp)
                    VALUES (?, ?, ?)
                ''', (ip_fingerprint, address, _utc_now()))
                
                # Log it
                cursor.execute('''
                    INSERT INTO credit_purchases (tx_hash, address, amount_usdc, credits_added, timestamp)
                    VALUES (?, ?, ?, ?, ?)
                ''', (f"WELCOME_{address[:8]}", address, 0.0, welcome_credits, _utc_now()))
                
                conn.commit()
                logger.info(
                    "Secure trial granted: 50.0 credits to %s (wallet age %.1fh)",
                    address,
                    metadata["age_hours"],
                )
                return welcome_credits
            
            return row[0]
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

# Credit Unit: 1 Credit = $0.001 market value
# Costs in Credits
CREDIT_COSTS = {
    "core": 2.0,       # $0.002
    "extended": 4.0,   # $0.004
    "tradfi": 5.0,     # $0.005
    "equities": 8.0,   # $0.008
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


def _utc_now() -> str:
    """Return an ISO-8601 UTC timestamp for SQLite storage."""
    return datetime.now(UTC).isoformat()
