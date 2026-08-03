from __future__ import annotations

import sqlite3

import pytest

from src.credit_manager import MAX_CACHED_PAYMENT_RESPONSE_BYTES, CreditManager


def test_credit_manager_uses_env_db_path(tmp_path, monkeypatch):
    db_path = tmp_path / "env-credits.db"
    monkeypatch.setenv("CREDIT_DB_PATH", str(db_path))

    manager = CreditManager()

    assert manager.db_path == str(db_path)
    assert db_path.exists()


def test_spend_credits_is_atomic(tmp_path):
    db_path = tmp_path / "credits.db"
    manager = CreditManager(str(db_path))
    manager.add_credits("wallet-12345678901234567890", 5.0, "tx-1", 0.005)

    assert manager.spend_credits("wallet-12345678901234567890", 3.0) is True
    assert manager.spend_credits("wallet-12345678901234567890", 3.0) is False
    assert manager.get_balance("wallet-12345678901234567890") == 2.0


def test_refund_credits_restores_spent_balance(tmp_path):
    db_path = tmp_path / "credits.db"
    manager = CreditManager(str(db_path))
    manager.add_credits("wallet-12345678901234567890", 5.0, "tx-1", 0.005)

    assert manager.spend_credits(
        "wallet-12345678901234567890",
        3.0,
        charge_id="charge-1",
    ) is True
    assert manager.refund_credits(
        "wallet-12345678901234567890",
        3.0,
        charge_id="charge-1",
    ) is True
    assert manager.refund_credits(
        "wallet-12345678901234567890",
        3.0,
        charge_id="charge-1",
    ) is False
    assert manager.get_balance("wallet-12345678901234567890") == 5.0


def test_payment_proof_replay_is_persistent(tmp_path):
    db_path = tmp_path / "credits.db"
    manager = CreditManager(str(db_path))

    assert manager.record_payment_proof("tx-proof", "solana", 2000, "recipient", "data") is True
    assert manager.record_payment_proof("tx-proof", "solana", 2000, "recipient", "data") is False


def test_payment_reservation_release_retry_and_finalize_lifecycle(tmp_path):
    manager = CreditManager(str(tmp_path / "credits.db"))
    values = {
        "payment_id": "proof-1",
        "network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
        "amount_atomic": 2000,
        "recipient": "merchant",
        "purpose": "GET /v1/vwap/BTCUSD",
        "request_binding": "binding-a",
        "attempt_id": "attempt-1",
        "lease_seconds": 60,
        "now": 1000.0,
    }

    first = manager.reserve_payment_proof(**values)
    concurrent = manager.reserve_payment_proof(**{**values, "attempt_id": "attempt-2"})
    wrong_binding = manager.reserve_payment_proof(
        **{**values, "request_binding": "binding-b", "attempt_id": "attempt-3"}
    )

    assert first.acquired is True
    assert concurrent.acquired is False
    assert concurrent.reason == "payment_reservation_in_progress"
    assert wrong_binding.acquired is False
    assert wrong_binding.reason == "payment_bound_to_different_request"
    assert manager.release_payment_proof(
        payment_id=first.payment_id,
        reservation_id=first.reservation_id or "",
    ) is True

    retry = manager.reserve_payment_proof(
        **{**values, "attempt_id": "attempt-4", "now": 1001.0}
    )
    assert retry.acquired is True
    assert retry.reason == "released_payment_retried"
    assert manager.finalize_payment_proof(
        payment_id=retry.payment_id,
        reservation_id=retry.reservation_id or "",
        settlement={"success": True},
    ) is True

    replay = manager.reserve_payment_proof(
        **{**values, "attempt_id": "attempt-5", "now": 1002.0}
    )
    assert replay.acquired is False
    assert replay.reason == "payment_already_finalized"
    assert manager.payment_proof_state("proof-1")["state"] == "finalized"


def test_finalized_payment_response_replays_only_for_exact_request_binding(tmp_path):
    manager = CreditManager(str(tmp_path / "credits.db"))
    reservation = manager.reserve_payment_proof(
        payment_id="proof-with-response",
        network="eip155:8453",
        amount_atomic=2000,
        recipient="0x1111111111111111111111111111111111111111",
        purpose="GET /v1/vwap/BTCUSD",
        request_binding="binding-a",
        attempt_id="attempt-a",
        lease_seconds=60,
    )

    assert manager.finalize_payment_proof(
        payment_id=reservation.payment_id,
        reservation_id=reservation.reservation_id or "",
        settlement={"success": True, "network": "eip155:8453"},
        response_status=200,
        response_headers={
            "Content-Type": "application/json",
            "Set-Cookie": "must-not-be-cached=true",
        },
        response_body=b'{"price":95000}',
    ) is True

    replay = manager.finalized_payment_response(
        payment_id=reservation.payment_id,
        request_binding="binding-a",
    )

    assert replay == {
        "status_code": 200,
        "headers": {"content-type": "application/json"},
        "body": b'{"price":95000}',
        "settlement": {"network": "eip155:8453", "success": True},
    }
    assert manager.finalized_payment_response(
        payment_id=reservation.payment_id,
        request_binding="binding-b",
    ) is None
    state = manager.payment_proof_state(reservation.payment_id)
    assert state["has_cached_response"] == 1

    with sqlite3.connect(manager.db_path) as conn:
        conn.execute(
            "UPDATE payment_proofs SET finalized_at = ? WHERE tx_hash = ?",
            ("2000-01-01T00:00:00+00:00", reservation.payment_id),
        )
    assert manager.finalized_payment_response(
        payment_id=reservation.payment_id,
        request_binding="binding-a",
        max_age_seconds=1,
    ) is None


def test_settled_checkpoint_recovers_exact_response_without_new_reservation(tmp_path):
    manager = CreditManager(str(tmp_path / "credits.db"))
    reservation = manager.reserve_payment_proof(
        payment_id="proof-settled-before-finalize",
        network="eip155:8453",
        amount_atomic=2000,
        recipient="0x1111111111111111111111111111111111111111",
        purpose="GET /v1/vwap/BTCUSD",
        request_binding="binding-a",
        attempt_id="attempt-a",
        lease_seconds=60,
    )
    settlement = {
        "success": True,
        "network": "eip155:8453",
        "transaction": "0x" + "34" * 32,
    }

    assert manager.checkpoint_settled_payment(
        payment_id=reservation.payment_id,
        reservation_id=reservation.reservation_id or "",
        settlement=settlement,
        response_status=200,
        response_headers={"Content-Type": "application/json"},
        response_body=b'{"price":95000}',
    ) is True
    assert manager.payment_proof_state(reservation.payment_id)["state"] == "settled"
    assert manager.finalized_payment_response(
        payment_id=reservation.payment_id,
        request_binding="binding-b",
    ) is None
    assert manager.payment_proof_state(reservation.payment_id)["state"] == "settled"

    replay = manager.finalized_payment_response(
        payment_id=reservation.payment_id,
        request_binding="binding-a",
    )

    assert replay == {
        "status_code": 200,
        "headers": {"content-type": "application/json"},
        "body": b'{"price":95000}',
        "settlement": settlement,
    }
    assert manager.payment_proof_state(reservation.payment_id)["state"] == "finalized"
    duplicate = manager.reserve_payment_proof(
        payment_id=reservation.payment_id,
        network="eip155:8453",
        amount_atomic=2000,
        recipient="0x1111111111111111111111111111111111111111",
        purpose="GET /v1/vwap/BTCUSD",
        request_binding="binding-a",
        attempt_id="attempt-b",
        lease_seconds=60,
    )
    assert duplicate.acquired is False
    assert duplicate.reason == "payment_already_finalized"


def test_settled_checkpoint_prunes_cached_bodies_to_hard_entry_bound(tmp_path):
    manager = CreditManager(str(tmp_path / "credits.db"))
    for index in range(2):
        reservation = manager.reserve_payment_proof(
            payment_id=f"settled-proof-{index}",
            network="eip155:8453",
            amount_atomic=2000,
            recipient="0x1111111111111111111111111111111111111111",
            purpose="GET /v1/vwap/BTCUSD",
            request_binding=f"binding-{index}",
            attempt_id=f"attempt-{index}",
            lease_seconds=60,
        )
        assert manager.checkpoint_settled_payment(
            payment_id=reservation.payment_id,
            reservation_id=reservation.reservation_id or "",
            settlement={"success": True, "transaction": f"tx-{index}"},
            response_status=200,
            response_headers={"Content-Type": "application/json"},
            response_body=f'{{"index":{index}}}'.encode(),
            replay_max_entries=1,
        )

    first_state = manager.payment_proof_state("settled-proof-0")
    second_state = manager.payment_proof_state("settled-proof-1")
    assert first_state["state"] == "settled"
    assert first_state["has_cached_response"] == 0
    assert second_state["state"] == "settled"
    assert second_state["has_cached_response"] == 1
    with sqlite3.connect(manager.db_path) as conn:
        first_settlement = conn.execute(
            "SELECT settlement_json FROM payment_proofs WHERE tx_hash = ?",
            ("settled-proof-0",),
        ).fetchone()
    assert first_settlement is not None
    assert first_settlement[0]


def test_payment_response_cache_rejects_oversize_body_without_finalizing(tmp_path):
    manager = CreditManager(str(tmp_path / "credits.db"))
    reservation = manager.reserve_payment_proof(
        payment_id="proof-too-large",
        network="eip155:8453",
        amount_atomic=2000,
        recipient="0x1111111111111111111111111111111111111111",
        purpose="GET /v1/vwap/BTCUSD",
        request_binding="binding-a",
        attempt_id="attempt-a",
        lease_seconds=60,
    )

    with pytest.raises(ValueError, match="replay-cache limit"):
        manager.finalize_payment_proof(
            payment_id=reservation.payment_id,
            reservation_id=reservation.reservation_id or "",
            settlement={"success": True},
            response_status=200,
            response_body=b"x" * (MAX_CACHED_PAYMENT_RESPONSE_BYTES + 1),
        )

    assert manager.payment_proof_state(reservation.payment_id)["state"] == "pending"


@pytest.mark.parametrize(
    "overrides",
    [
        {"replay_ttl_seconds": 3_601},
        {"replay_max_entries": 501},
    ],
)
def test_payment_response_cache_rejects_retention_above_hard_limits(tmp_path, overrides):
    manager = CreditManager(str(tmp_path / "credits.db"))
    reservation = manager.reserve_payment_proof(
        payment_id=f"proof-retention-{next(iter(overrides.values()))}",
        network="eip155:8453",
        amount_atomic=2000,
        recipient="0x1111111111111111111111111111111111111111",
        purpose="GET /v1/vwap/BTCUSD",
        request_binding="binding-a",
        attempt_id="attempt-a",
        lease_seconds=60,
    )

    with pytest.raises(ValueError, match="hard retention limit"):
        manager.finalize_payment_proof(
            payment_id=reservation.payment_id,
            reservation_id=reservation.reservation_id or "",
            settlement={"success": True},
            response_status=200,
            response_body=b"ok",
            **overrides,
        )

    assert manager.payment_proof_state(reservation.payment_id)["state"] == "pending"


def test_payment_response_cache_prunes_oldest_body_but_keeps_finalized_proof(tmp_path):
    manager = CreditManager(str(tmp_path / "credits.db"))
    for index in range(2):
        reservation = manager.reserve_payment_proof(
            payment_id=f"proof-{index}",
            network="eip155:8453",
            amount_atomic=2000,
            recipient="0x1111111111111111111111111111111111111111",
            purpose="GET /v1/vwap/BTCUSD",
            request_binding=f"binding-{index}",
            attempt_id=f"attempt-{index}",
            lease_seconds=60,
        )
        assert manager.finalize_payment_proof(
            payment_id=reservation.payment_id,
            reservation_id=reservation.reservation_id or "",
            settlement={"success": True},
            response_status=200,
            response_body=f'{{"index":{index}}}'.encode(),
            replay_max_entries=1,
        )

    assert manager.finalized_payment_response(
        payment_id="proof-0",
        request_binding="binding-0",
    ) is None
    first_state = manager.payment_proof_state("proof-0")
    assert first_state["state"] == "finalized"
    assert first_state["request_binding"] == "binding-0"
    assert first_state["attempt_id"] == "attempt-0"
    assert first_state["has_cached_response"] == 0
    assert manager.finalized_payment_response(
        payment_id="proof-1",
        request_binding="binding-1",
    )["body"] == b'{"index":1}'


def test_payment_reservation_reclaims_only_stale_exact_binding(tmp_path):
    manager = CreditManager(str(tmp_path / "credits.db"))
    values = {
        "payment_id": "proof-stale",
        "network": "eip155:8453",
        "amount_atomic": 4000,
        "recipient": "0x1111111111111111111111111111111111111111",
        "purpose": "GET /v1/vwap/ETHUSD",
        "request_binding": "binding-a",
        "attempt_id": "attempt-1",
        "lease_seconds": 30,
        "now": 1000.0,
    }
    first = manager.reserve_payment_proof(**values)
    fresh = manager.reserve_payment_proof(
        **{**values, "attempt_id": "attempt-2", "now": 1029.0}
    )
    stale = manager.reserve_payment_proof(
        **{**values, "attempt_id": "attempt-3", "now": 1030.0}
    )

    assert first.acquired is True
    assert fresh.acquired is False
    assert stale.acquired is True
    assert stale.reason == "stale_lease_reclaimed"


def test_unknown_settlement_is_quarantined_from_stale_retries(tmp_path):
    manager = CreditManager(str(tmp_path / "credits.db"))
    values = {
        "payment_id": "proof-unknown-settlement",
        "network": "eip155:8453",
        "amount_atomic": 2000,
        "recipient": "0x1111111111111111111111111111111111111111",
        "purpose": "GET /v1/vwap/BTCUSD",
        "request_binding": "binding-a",
        "attempt_id": "attempt-a",
        "lease_seconds": 30,
        "now": 1000.0,
    }
    reservation = manager.reserve_payment_proof(**values)

    assert manager.mark_payment_settlement_unknown(
        payment_id=reservation.payment_id,
        reservation_id=reservation.reservation_id or "",
    )
    retried = manager.reserve_payment_proof(
        **{**values, "attempt_id": "attempt-b", "now": 9999.0}
    )

    assert retried.acquired is False
    assert retried.state == "settlement_unknown"
    assert retried.reason == "payment_settlement_reconciliation_required"
    state = manager.payment_proof_state(reservation.payment_id)
    assert state["state"] == "settlement_unknown"
    assert state["settlement_unknown_at"]


def test_existing_only_reservation_never_creates_an_unverified_proof(tmp_path):
    manager = CreditManager(str(tmp_path / "credits.db"))

    result = manager.reserve_payment_proof(
        payment_id="unverified-proof",
        network="eip155:8453",
        amount_atomic=2000,
        recipient="0x1111111111111111111111111111111111111111",
        purpose="GET /v1/vwap/BTCUSD",
        request_binding="binding-a",
        attempt_id="attempt-a",
        lease_seconds=60,
        existing_only=True,
    )

    assert result.acquired is False
    assert result.reason == "payment_reservation_missing"
    assert manager.payment_proof_state("unverified-proof") is None


def test_evm_payment_ids_are_case_canonicalized(tmp_path):
    manager = CreditManager(str(tmp_path / "credits.db"))
    assert manager.record_payment_proof(
        "0xABCDEF",
        "eip155:8453",
        2000,
        "merchant",
        "data",
    ) is True
    assert manager.record_payment_proof(
        "0xabcdef",
        "eip155:8453",
        2000,
        "merchant",
        "data",
    ) is False


def test_bulk_credit_grant_and_payment_finalization_are_atomic(tmp_path):
    manager = CreditManager(str(tmp_path / "credits.db"))
    reservation = manager.reserve_payment_proof(
        payment_id="bulk-proof",
        network="solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
        amount_atomic=900000,
        recipient="merchant",
        purpose="credits:starter",
        request_binding="binding",
        attempt_id="attempt",
        lease_seconds=60,
    )

    assert manager.finalize_payment_and_add_credits(
        payment_id=reservation.payment_id,
        reservation_id=reservation.reservation_id or "",
        address="wallet-12345678901234567890",
        credits=1000,
        amount_usdc=0.9,
        settlement={"success": True},
    ) is True
    assert manager.get_balance("wallet-12345678901234567890") == 1000
    assert manager.payment_proof_state("bulk-proof")["state"] == "finalized"
    assert manager.finalize_payment_and_add_credits(
        payment_id=reservation.payment_id,
        reservation_id=reservation.reservation_id or "",
        address="wallet-12345678901234567890",
        credits=1000,
        amount_usdc=0.9,
    ) is False
    assert manager.get_balance("wallet-12345678901234567890") == 1000


def test_rate_limit_is_shared_across_manager_instances(tmp_path):
    db_path = tmp_path / "credits.db"
    first = CreditManager(str(db_path))
    second = CreditManager(str(db_path))

    assert first.check_rate_limit(
        scope="discovery",
        key="203.0.113.10",
        per_minute=1,
        per_day=10,
        now=1000,
    )[0] is True
    blocked = second.check_rate_limit(
        scope="discovery",
        key="203.0.113.10",
        per_minute=1,
        per_day=10,
        now=1001,
    )

    assert blocked[0] is False
    assert blocked[2] == "minute"


def test_wallet_inflow_summary_combines_direct_payments_and_credit_topups(tmp_path):
    db_path = tmp_path / "credits.db"
    manager = CreditManager(str(db_path))

    assert manager.record_payment_proof(
        "direct-tx",
        "solana",
        2000,
        "merchant-recipient",
        "GET /v1/vwap/BTC-USD",
    ) is True
    assert manager.record_payment_proof(
        "topup-tx",
        "solana",
        900000,
        "merchant-recipient",
        "credits:starter",
    ) is True
    manager.add_credits("wallet-12345678901234567890", 1000, "topup-tx", 0.9)

    summary = manager.wallet_inflow_summary(days=1)

    assert summary["total_inflows"] == 2
    assert summary["direct_x402_count"] == 1
    assert summary["credit_topup_count"] == 1
    assert summary["total_usdc"] == 0.902
    assert {row["tx_hash"] for row in summary["rows"]} == {"direct-tx", "topup-tx"}
    topup = next(row for row in summary["rows"] if row["kind"] == "credit_topup")
    assert topup["wallet"] == "wallet-12345678901234567890"
    assert topup["credits_added"] == 1000.0


def test_wallet_inflow_summary_excludes_zero_value_proofs_and_promotional_credits(tmp_path):
    db_path = tmp_path / "credits.db"
    manager = CreditManager(str(db_path))

    assert manager.record_payment_proof(
        "zero-proof",
        "solana",
        0,
        "merchant-recipient",
        "GET /v1/vwap/BTC-USD",
    ) is True
    manager.add_credits("wallet-promotional-12345678", 50, "promo-tx", 0)

    summary = manager.wallet_inflow_summary(days=1)

    assert summary["total_inflows"] == 0
    assert summary["total_usdc"] == 0


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
