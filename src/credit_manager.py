import sqlite3
import os
import logging
import httpx
import json
from datetime import datetime
from decimal import Decimal

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
        
        # Trial History (for Sybil Protection)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS trial_history (
                ip_hash TEXT PRIMARY KEY,
                address TEXT,
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

    async def ensure_wallet_with_welcome_pack(self, address: str, ip: str) -> float:
        """
        Hardened Trial Grant Logic:
        1. Check if IP has already claimed a trial (Permanent blacklist).
        2. Check if wallet exists.
        3. If new, verify On-Chain Stake (0.1 SOL).
        4. Grant trial if all checks pass.
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            # 1. IP Check
            cursor.execute("SELECT address FROM trial_history WHERE ip_hash = ?", (ip,))
            if cursor.fetchone():
                logger.warning(f"🚫 SYBIL DETECTED: IP {ip} attempted duplicate trial claim.")
                return 0.0 # No trial for repeat IPs
            
            # 2. Wallet Check
            cursor.execute("SELECT balance_credits FROM wallets WHERE address = ?", (address,))
            row = cursor.fetchone()
            
            if row is None:
                # 3. Stake Check (Iron Dome)
                sol_balance = await self._get_solana_balance(address)
                threshold = 0.1 # 0.1 SOL
                
                if sol_balance < threshold:
                    logger.warning(f"⚠️ LOW STAKE: Wallet {address} has only {sol_balance} SOL (Needs {threshold}). Trial denied.")
                    return 0.0
                
                # 4. Success! Grant Welcome Pack.
                welcome_credits = 50.0
                cursor.execute('''
                    INSERT INTO wallets (address, balance_credits, last_updated)
                    VALUES (?, ?, ?)
                ''', (address, welcome_credits, datetime.utcnow()))
                
                # Record to Trial History
                cursor.execute('''
                    INSERT INTO trial_history (ip_hash, address, timestamp)
                    VALUES (?, ?, ?)
                ''', (ip, address, datetime.utcnow()))
                
                # Log it as a special internal transaction
                cursor.execute('''
                    INSERT INTO credit_purchases (tx_hash, address, amount_usdc, credits_added, timestamp)
                    VALUES (?, ?, ?, ?, ?)
                ''', (f"WELCOME_{address[:8]}", address, 0.0, welcome_credits, datetime.utcnow()))
                
                conn.commit()
                logger.info(f"🎁 SHIELDED TRIAL GRANTED: 50.0 credits to {address} (SOL: {sol_balance})")
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
            ''', (address, credits, datetime.utcnow()))
            
            # Log purchase
            cursor.execute('''
                INSERT INTO credit_purchases (tx_hash, address, amount_usdc, credits_added, timestamp)
                VALUES (?, ?, ?, ?, ?)
            ''', (tx_hash, address, amount_usdc, credits, datetime.utcnow()))
            
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
            cursor.execute("SELECT balance_credits FROM wallets WHERE address = ?", (address,))
            row = cursor.fetchone()
            if not row or row[0] < credits:
                return False
            
            cursor.execute('''
                UPDATE wallets SET balance_credits = balance_credits - ?, last_updated = ?
                WHERE address = ?
            ''', (credits, datetime.utcnow(), address))
            
            conn.commit()
            return True
        except Exception as e:
            logger.error(f"Error spending credits for {address}: {e}")
            conn.rollback()
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
