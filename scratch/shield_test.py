import os
import unittest
import asyncio
from unittest.mock import patch, AsyncMock
from src.credit_manager import CreditManager

class TestIronDomeShield(unittest.TestCase):
    def setUp(self):
        self.db_path = "shield_test_credits.db"
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        self.mgr = CreditManager(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def run_async(self, coro):
        return asyncio.get_event_loop().run_until_complete(coro)

    def test_rejection_low_stake(self):
        wallet = "low_stake_wallet"
        ip = "1.2.3.4"
        
        # Mocking balance to 0.05 SOL (Threshold is 0.1)
        with patch.object(self.mgr, '_get_solana_balance', new_callable=AsyncMock) as mock_balance:
            mock_balance.return_value = 0.05
            balance = self.run_async(self.mgr.ensure_wallet_with_welcome_pack(wallet, ip))
            
            self.assertEqual(balance, 0.0)
            self.assertEqual(self.mgr.get_balance(wallet), 0.0)

    def test_success_funded_wallet(self):
        wallet = "funded_wallet"
        ip = "5.6.7.8"
        
        # Mocking balance to 0.15 SOL
        with patch.object(self.mgr, '_get_solana_balance', new_callable=AsyncMock) as mock_balance:
            mock_balance.return_value = 0.15
            balance = self.run_async(self.mgr.ensure_wallet_with_welcome_pack(wallet, ip))
            
            self.assertEqual(balance, 50.0)
            self.assertEqual(self.mgr.get_balance(wallet), 50.0)

    def test_rejection_duplicate_ip(self):
        wallet_1 = "wallet_one"
        wallet_2 = "wallet_two"
        ip = "9.9.9.9"
        
        # First wallet succeeds
        with patch.object(self.mgr, '_get_solana_balance', new_callable=AsyncMock) as mock_balance:
            mock_balance.return_value = 0.2
            self.run_async(self.mgr.ensure_wallet_with_welcome_pack(wallet_1, ip))
            
            # Second wallet with SAME IP fails regardless of stake
            mock_balance.return_value = 0.5
            balance_2 = self.run_async(self.mgr.ensure_wallet_with_welcome_pack(wallet_2, ip))
            
            self.assertEqual(balance_2, 0.0)
            self.assertEqual(self.mgr.get_balance(wallet_2), 0.0)

if __name__ == "__main__":
    unittest.main()
