import os
import unittest
from src.credit_manager import CreditManager

class TestCreditSystem(unittest.TestCase):
    def setUp(self):
        self.db_path = "test_credits.db"
        if os.path.exists(self.db_path):
            os.remove(self.db_path)
        self.mgr = CreditManager(self.db_path)

    def tearDown(self):
        if os.path.exists(self.db_path):
            os.remove(self.db_path)

    def test_initial_balance(self):
        self.assertEqual(self.mgr.get_balance("wallet_a"), 0.0)

    def test_add_and_spend_credits(self):
        wallet = "wallet_a"
        self.mgr.add_credits(wallet, 1000.0, "tx_1", 0.90)
        self.assertEqual(self.mgr.get_balance(wallet), 1000.0)
        
        # Spend valid
        success = self.mgr.spend_credits(wallet, 2.0)
        self.assertTrue(success)
        self.assertEqual(self.mgr.get_balance(wallet), 998.0)
        
        # Spend more than balance
        success = self.mgr.spend_credits(wallet, 1000.0)
        self.assertFalse(success)
        self.assertEqual(self.mgr.get_balance(wallet), 998.0)

    def test_replay_protection(self):
        wallet = "wallet_a"
        self.mgr.add_credits(wallet, 1000.0, "tx_duplicate", 0.90)
        self.assertEqual(self.mgr.get_balance(wallet), 1000.0)
        
        # Re-add same TX hash
        self.mgr.add_credits(wallet, 1000.0, "tx_duplicate", 0.90)
        self.assertEqual(self.mgr.get_balance(wallet), 1000.0) # Should NOT have increased

if __name__ == "__main__":
    unittest.main()
