import sqlite3
import os
import logging
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

    def ensure_wallet_with_welcome_pack(self, address: str) -> float:
        """
        Check if a wallet exists. If not, create it and grant the 50-credit Welcome Pack.
        Returns the current balance.
        """
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        try:
            cursor.execute("SELECT balance_credits FROM wallets WHERE address = ?", (address,))
            row = cursor.fetchone()
            
            if row is None:
                # First time seeing this agent! Grant Welcome Pack.
                welcome_credits = 50.0
                cursor.execute('''
                    INSERT INTO wallets (address, balance_credits, last_updated)
                    VALUES (?, ?, ?)
                ''', (address, welcome_credits, datetime.utcnow()))
                
                # Log it as a special internal transaction
                cursor.execute('''
                    INSERT INTO credit_purchases (tx_hash, address, amount_usdc, credits_added, timestamp)
                    VALUES (?, ?, ?, ?, ?)
                ''', (f"WELCOME_{address[:8]}", address, 0.0, welcome_credits, datetime.utcnow()))
                
                conn.commit()
                logger.info(f"🎁 WELCOME PACK: Granted 50.0 credits to new agent {address}")
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
