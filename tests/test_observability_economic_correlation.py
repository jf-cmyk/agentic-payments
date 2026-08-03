"""Decision-grade correlation tests for product-usage economics."""

from __future__ import annotations

from src.observability import UsageEventStore


def _payment_event_metadata(
    attempt_id: str,
    payment_id: str,
    **extra,
) -> dict[str, object]:
    return {
        "attempt_id": attempt_id,
        "payment_id": payment_id,
        "identity_hash": "verified-principal",
        "identity_type": "wallet",
        "identity_trust": "verified_x402",
        **extra,
    }


def _record_successful_payment(
    store: UsageEventStore,
    *,
    attempt_id: str = "attempt-1",
    payment_id: str = "payment-1",
    price: float = 0.002,
) -> None:
    common = {
        "surface": "http_api",
        "endpoint": "/v1/vwap/{pair}",
        "subject": "BTC-USD",
        "price_usdc": price,
        "wallet_hash": "verified-wallet",
    }
    store.record(
        "payment_proof_submitted",
        **common,
        metadata={"attempt_id": attempt_id},
    )
    store.record(
        "payment_authorization_verified",
        **common,
        metadata=_payment_event_metadata(attempt_id, payment_id),
    )
    store.record(
        "payment_settled",
        **common,
        metadata=_payment_event_metadata(
            attempt_id,
            payment_id,
            payment_state="finalized",
        ),
    )
    store.record(
        "data_delivered",
        **common,
        metadata=_payment_event_metadata(
            attempt_id,
            payment_id,
            payment_mode="x402",
            payment_state="finalized",
        ),
    )


def test_verified_then_settlement_failed_is_not_revenue_delivery_or_conversion(tmp_path):
    store = UsageEventStore(tmp_path / "usage.db")
    metadata = _payment_event_metadata("attempt-1", "payment-1")
    store.record("payment_proof_submitted", metadata={"attempt_id": "attempt-1"})
    store.record(
        "payment_authorization_verified",
        wallet_hash="spoof-resistant-wallet",
        price_usdc=0.002,
        metadata=metadata,
    )
    store.record(
        "payment_failed",
        price_usdc=0.002,
        reason="settlement_failed",
        metadata=metadata,
    )

    stats = store.summarize(days=1)

    assert stats["overview"]["paid_calls"] == 0
    assert stats["overview"]["estimated_revenue_usdc"] == 0
    assert stats["overview"]["payment_success_rate"] == 0
    assert stats["overview"]["active_paying_wallets"] == 0
    assert stats["growth_funnel"]["summary"]["starter_to_paid_identities"] == 0


def test_unmatched_mcp_drawdown_is_unresolved_not_delivery(tmp_path):
    store = UsageEventStore(tmp_path / "usage.db")
    store.record(
        "mcp_credit_drawdown_success",
        surface="anthropic_mcp",
        tool_name="get_vwap",
        subject="BTCUSD",
        metadata={"attempt_id": "attempt-1", "charge_id": "charge-1"},
    )

    stats = store.summarize(days=1)

    assert stats["overview"]["paid_calls"] == 0
    assert stats["reliability"]["charged_delivery_successes"] == 0
    assert stats["economic_correlation"]["unresolved_drawdowns"] == 1
    assert stats["popularity"]["total_delivered"] == 0


def test_explicit_mcp_delivery_is_joined_by_attempt_and_charge(tmp_path):
    store = UsageEventStore(tmp_path / "usage.db")
    trusted = {
        "identity_hash": "oauth-principal",
        "identity_type": "user",
        "identity_trust": "verified_oauth",
    }
    store.record(
        "mcp_credit_drawdown_success",
        surface="anthropic_mcp",
        tool_name="get_vwap",
        subject="BTCUSD",
        metadata={
            "attempt_id": "attempt-1",
            "charge_id": "charge-1",
            "credits_spent": 1,
            **trusted,
        },
    )
    store.record(
        "mcp_data_delivered",
        surface="anthropic_mcp",
        tool_name="get_vwap",
        subject="BTCUSD",
        metadata={
            "attempt_id": "attempt-1",
            "charge_id": "charge-1",
            "credits_spent": 1,
            **trusted,
        },
    )
    store.record(
        "mcp_tool_error",
        surface="anthropic_mcp",
        tool_name="get_vwap",
        subject="ETHUSD",
        metadata={"attempt_id": "unrelated", "charge_id": "unrelated"},
    )

    stats = store.summarize(days=1)

    assert stats["overview"]["paid_calls"] == 1
    assert stats["overview"]["active_paying_wallets"] == 0
    assert stats["overview"]["active_verified_principals"] == 1
    assert stats["popularity"]["total_delivered"] == 1
    assert stats["popularity"]["total_credits_spent"] == 1


def test_duplicate_settlement_same_payment_id_recognizes_revenue_once(tmp_path):
    store = UsageEventStore(tmp_path / "usage.db")
    _record_successful_payment(store)
    store.record(
        "payment_proof_submitted",
        price_usdc=0.002,
        metadata={"attempt_id": "attempt-2"},
    )
    store.record(
        "payment_authorization_verified",
        price_usdc=0.002,
        metadata=_payment_event_metadata("attempt-2", "payment-1"),
    )
    store.record(
        "payment_settled",
        price_usdc=0.002,
        metadata=_payment_event_metadata(
            "attempt-2",
            "payment-1",
            payment_state="finalized",
        ),
    )

    stats = store.summarize(days=1)

    assert stats["overview"]["estimated_revenue_usdc"] == 0.002
    assert stats["overview"]["paid_calls"] == 1
    assert stats["economic_correlation"]["duplicate_settlements_deduplicated"] == 1


def test_untrusted_legacy_identity_does_not_enter_verified_kpis(tmp_path):
    store = UsageEventStore(tmp_path / "usage.db")
    store.record(
        "first_live_price_delivered",
        wallet_hash="caller-selected-wallet",
        metadata={"identity_hash": "caller-selected", "identity_type": "wallet"},
    )

    stats = store.summarize(days=1)

    assert stats["overview"]["active_paying_wallets"] == 0
    assert stats["overview"]["active_verified_principals"] == 0
    assert stats["growth_funnel"]["summary"]["activated_identities"] == 0
    assert stats["growth_funnel"]["summary"]["unattributed_activation_events"] == 1


def test_fully_correlated_finalized_payment_counts_once(tmp_path):
    store = UsageEventStore(tmp_path / "usage.db")
    _record_successful_payment(store)

    stats = store.summarize(days=1)

    assert stats["overview"]["paid_calls"] == 1
    assert stats["overview"]["estimated_revenue_usdc"] == 0.002
    assert stats["overview"]["payment_success_rate"] == 1
    assert stats["overview"]["active_paying_wallets"] == 1
    assert stats["overview"]["active_verified_principals"] == 1
    assert stats["reliability"]["charged_delivery_successes"] == 1
