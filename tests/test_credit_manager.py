from __future__ import annotations

import pytest

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


@pytest.mark.asyncio
async def test_starter_allowance_grants_50_credits_for_user_subject(tmp_path):
    db_path = tmp_path / "credits.db"
    manager = CreditManager(str(db_path))

    result = await manager.ensure_starter_allowance(
        subject="user-12345678",
        subject_type="user",
        ip="203.0.113.10",
        device_id="device-12345678",
        session_id="session-12345678",
    )

    assert result.eligible is True
    assert result.granted_credits == 50.0
    assert manager.get_balance("user-12345678") == 50.0


@pytest.mark.asyncio
async def test_starter_allowance_blocks_duplicate_device(tmp_path):
    db_path = tmp_path / "credits.db"
    manager = CreditManager(str(db_path))

    first = await manager.ensure_starter_allowance(
        subject="agent-12345678",
        subject_type="agent",
        ip="203.0.113.10",
        device_id="device-12345678",
    )
    second = await manager.ensure_starter_allowance(
        subject="agent-87654321",
        subject_type="agent",
        ip="203.0.113.11",
        device_id="device-12345678",
    )

    assert first.eligible is True
    assert second.eligible is False
    assert second.reason == "duplicate_trial_fingerprint"
    assert manager.get_balance("agent-87654321") == 0.0
