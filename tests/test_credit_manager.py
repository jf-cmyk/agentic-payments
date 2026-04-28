from __future__ import annotations

from src.credit_manager import CreditManager


def test_spend_credits_is_atomic(tmp_path):
    db_path = tmp_path / "credits.db"
    manager = CreditManager(str(db_path))
    manager.add_credits("wallet-12345678901234567890", 5.0, "tx-1", 0.005)

    assert manager.spend_credits("wallet-12345678901234567890", 3.0) is True
    assert manager.spend_credits("wallet-12345678901234567890", 3.0) is False
    assert manager.get_balance("wallet-12345678901234567890") == 2.0


def test_payment_proof_replay_is_persistent(tmp_path):
    db_path = tmp_path / "credits.db"
    manager = CreditManager(str(db_path))

    assert manager.record_payment_proof("tx-proof", "solana", 2000, "recipient", "data") is True
    assert manager.record_payment_proof("tx-proof", "solana", 2000, "recipient", "data") is False
