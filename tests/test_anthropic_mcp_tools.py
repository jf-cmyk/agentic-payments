from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest

from src import anthropic_mcp_server as server
from src.anthropic_auth import AnthropicIdentity
from src.blocksize_client import BlocksizeAPIError
from src.entitlement_manager import EntitlementManager
from src.models import PairInfo, VWAPData


@pytest.fixture(autouse=True)
def isolated_anthropic_state(tmp_path, monkeypatch):
    server._client = None
    server._entitlements = EntitlementManager(
        tmp_path / "anthropic_entitlements.db",
        default_daily_credits=2,
    )
    monkeypatch.setattr(server.anthropic_auth, "resolve_anthropic_identity", lambda: None)
    yield
    server._client = None
    server._entitlements = None


def _identity():
    return AnthropicIdentity(user_id="user-1", email="user@example.com", source="test")


def test_anthropic_tools_use_directory_safe_annotations():
    annotations = server.READ_ONLY_TOOL_ANNOTATIONS

    assert annotations["readOnlyHint"] is True
    assert annotations["destructiveHint"] is False
    assert annotations["idempotentHint"] is True


@pytest.mark.asyncio
async def test_live_tool_requires_identity():
    result = await server.anthropic_get_vwap("btc-usd")
    parsed = json.loads(result)

    assert parsed["status"] == "error"
    assert parsed["error_code"] == "AUTH_REQUIRED"


@pytest.mark.asyncio
async def test_credit_balance_returns_daily_status(monkeypatch):
    monkeypatch.setattr(server.anthropic_auth, "resolve_anthropic_identity", _identity)

    result = await server.anthropic_get_credit_balance()
    parsed = json.loads(result)

    assert parsed["status"] == "ok"
    assert parsed["credits"]["daily_limit"] == 2
    assert parsed["credits"]["credits_remaining"] == 2
    assert "user_id" not in parsed["credits"]
    assert "email" not in parsed["credits"]


@pytest.mark.asyncio
async def test_vwap_spends_one_credit(monkeypatch, isolate_usage_event_store):
    monkeypatch.setattr(server.anthropic_auth, "resolve_anthropic_identity", _identity)
    mock_client = AsyncMock()
    mock_client.get_vwap_latest = AsyncMock(
        return_value=VWAPData(
            pair="btc-usd",
            vwap=95432.5,
            timestamp=datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc),
            currency="USD",
        )
    )
    server._client = mock_client

    result = await server.anthropic_get_vwap("btc-usd")

    assert "VWAP [btc-usd]" in result
    assert "Credits remaining today: 1/2" in result
    mock_client.get_vwap_latest.assert_awaited_once_with("BTCUSD")
    assert server._entitlements.status("user-1").credits_remaining == 1

    all_stats = isolate_usage_event_store.summarize(days=1, include_synthetic=True)
    production_stats = isolate_usage_event_store.summarize(days=1, include_synthetic=False)
    assert all_stats["event_counts"]["mcp_data_delivered"] == 1
    assert all_stats["overview"]["paid_calls"] == 1
    assert production_stats["overview"]["paid_calls"] == 0
    assert production_stats["telemetry_scope"]["excluded_synthetic_events"] >= 1


@pytest.mark.asyncio
async def test_vwap_rejects_invalid_symbol_without_spending(monkeypatch):
    monkeypatch.setattr(server.anthropic_auth, "resolve_anthropic_identity", _identity)
    mock_client = AsyncMock()
    server._client = mock_client

    result = await server.anthropic_get_vwap("../bad")
    parsed = json.loads(result)

    assert parsed["error_code"] == "INVALID_SYMBOL"
    assert server._entitlements.status("user-1").credits_remaining == 2
    mock_client.get_vwap_latest.assert_not_called()


@pytest.mark.asyncio
async def test_exhausted_credits_block_live_call(monkeypatch):
    monkeypatch.setattr(server.anthropic_auth, "resolve_anthropic_identity", _identity)
    mock_client = AsyncMock()
    mock_client.get_vwap_latest = AsyncMock(
        return_value=VWAPData(
            pair="btc-usd",
            vwap=95432.5,
            timestamp=datetime(2026, 4, 29, 12, 0, tzinfo=timezone.utc),
            currency="USD",
        )
    )
    server._client = mock_client
    server._entitlements = EntitlementManager(
        server._entitlements.db_path,
        default_daily_credits=1,
    )

    first = await server.anthropic_get_vwap("btc-usd")
    second = await server.anthropic_get_vwap("eth-usd")
    parsed = json.loads(second)

    assert "Credits remaining today: 0/1" in first
    assert parsed["error_code"] == "DAILY_CREDIT_LIMIT_REACHED"
    assert mock_client.get_vwap_latest.await_count == 1


@pytest.mark.asyncio
async def test_upstream_error_refunds_credit(monkeypatch):
    monkeypatch.setattr(server.anthropic_auth, "resolve_anthropic_identity", _identity)
    mock_client = AsyncMock()
    mock_client.get_vwap_latest = AsyncMock(side_effect=BlocksizeAPIError(-1, "Not found"))
    server._client = mock_client

    result = await server.anthropic_get_vwap("bad-pair")
    parsed = json.loads(result)

    assert parsed["error_code"] == "BLOCKSIZE_API_ERROR"
    assert server._entitlements.status("user-1").credits_remaining == 2


@pytest.mark.asyncio
async def test_unexpected_error_refunds_once_and_correlates_outcome(
    monkeypatch,
    isolate_usage_event_store,
):
    monkeypatch.setattr(server.anthropic_auth, "resolve_anthropic_identity", _identity)
    secret_error = "internal-secret-must-not-escape"
    mock_client = AsyncMock()
    mock_client.get_vwap_latest = AsyncMock(side_effect=RuntimeError(secret_error))
    server._client = mock_client

    result = await server.anthropic_get_vwap("btc-usd")
    parsed = json.loads(result)

    assert parsed["error_code"] == "INTERNAL_ERROR"
    assert secret_error not in result
    assert server._entitlements.status("user-1").credits_remaining == 2

    events = isolate_usage_event_store.recent_events(limit=20)
    drawdown = next(event for event in events if event["event"] == "mcp_credit_drawdown_success")
    failure = next(event for event in events if event["event"] == "mcp_tool_error")
    assert drawdown["metadata"]["charge_id"] == failure["metadata"]["charge_id"]
    assert drawdown["metadata"]["attempt_id"] == failure["metadata"]["attempt_id"]
    assert failure["metadata"]["refund_status"] == "refunded"

    server._entitlements.refund(
        "user-1",
        1,
        tool_name="get_vwap",
        subject="BTCUSD",
        charge_id=failure["metadata"]["charge_id"],
    )
    assert server._entitlements.status("user-1").credits_remaining == 2


@pytest.mark.asyncio
async def test_cancelled_live_call_refunds_credit_and_reraises(
    monkeypatch,
    isolate_usage_event_store,
):
    monkeypatch.setattr(server.anthropic_auth, "resolve_anthropic_identity", _identity)
    mock_client = AsyncMock()
    mock_client.get_vwap_latest = AsyncMock(side_effect=asyncio.CancelledError())
    server._client = mock_client

    with pytest.raises(asyncio.CancelledError):
        await server.anthropic_get_vwap("btc-usd")

    assert server._entitlements.status("user-1").credits_remaining == 2
    events = isolate_usage_event_store.recent_events(limit=20)
    failure = next(event for event in events if event["event"] == "mcp_tool_error")
    drawdown = next(event for event in events if event["event"] == "mcp_credit_drawdown_success")
    assert failure["reason"] == "request_cancelled"
    assert failure["metadata"]["charge_id"] == drawdown["metadata"]["charge_id"]
    assert failure["metadata"]["refund_status"] == "refunded"


@pytest.mark.asyncio
async def test_successful_render_finalizes_charge_before_delivery(
    monkeypatch,
    isolate_usage_event_store,
):
    monkeypatch.setattr(server.anthropic_auth, "resolve_anthropic_identity", _identity)
    mock_client = AsyncMock()
    mock_client.get_vwap_latest = AsyncMock(
        return_value=VWAPData(
            pair="btc-usd",
            vwap=95432.5,
            timestamp=datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc),
            currency="USD",
        )
    )
    server._client = mock_client

    result = await server.anthropic_get_vwap("btc-usd")
    events = isolate_usage_event_store.recent_events(limit=20)
    delivered_event = next(event for event in events if event["event"] == "mcp_data_delivered")
    with sqlite3.connect(server._entitlements.db_path) as conn:
        charge_state = conn.execute(
            "SELECT state FROM credit_charges WHERE charge_id = ?",
            (delivered_event["metadata"]["charge_id"],),
        ).fetchone()[0]

    assert "VWAP [btc-usd]" in result
    assert delivered_event["metadata"]["charge_state"] == "delivered"
    assert charge_state == "delivered"


@pytest.mark.asyncio
async def test_finalize_failure_withholds_rendered_live_data(
    monkeypatch,
    isolate_usage_event_store,
):
    monkeypatch.setattr(server.anthropic_auth, "resolve_anthropic_identity", _identity)
    mock_client = AsyncMock()
    mock_client.get_vwap_latest = AsyncMock(
        return_value=VWAPData(
            pair="btc-usd",
            vwap=95432.5,
            timestamp=datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc),
            currency="USD",
        )
    )
    server._client = mock_client
    monkeypatch.setattr(server._entitlements, "finalize_delivery", lambda *_args, **_kwargs: None)

    result = await server.anthropic_get_vwap("btc-usd")
    parsed = json.loads(result)
    events = isolate_usage_event_store.recent_events(limit=20)
    failure = next(event for event in events if event["event"] == "mcp_tool_error")

    assert parsed["error_code"] == "CREDIT_FINALIZATION_FAILED"
    assert "95432.5" not in result
    assert not any(event["event"] == "mcp_data_delivered" for event in events)
    assert failure["reason"] == "credit_finalization_failed"
    assert server._entitlements.schema_status()["charge_states"] == {"pending": 1}


@pytest.mark.asyncio
async def test_next_connector_call_recovers_stale_process_crash(monkeypatch):
    monkeypatch.setattr(server.anthropic_auth, "resolve_anthropic_identity", _identity)
    server._entitlements = EntitlementManager(
        server._entitlements.db_path,
        default_daily_credits=1,
        pending_charge_lease_seconds=300,
    )
    ok, _ = server._entitlements.spend(
        "user-1",
        1,
        tool_name="get_vwap",
        subject="BTCUSD",
        charge_id="process-crash",
    )
    assert ok is True
    with sqlite3.connect(server._entitlements.db_path) as conn:
        conn.execute(
            "UPDATE credit_charges SET created_at = ? WHERE charge_id = ?",
            (
                (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat(),
                "process-crash",
            ),
        )
    mock_client = AsyncMock()
    mock_client.get_vwap_latest = AsyncMock(
        return_value=VWAPData(
            pair="btc-usd",
            vwap=95432.5,
            timestamp=datetime(2026, 7, 29, 12, 0, tzinfo=timezone.utc),
            currency="USD",
        )
    )
    server._client = mock_client

    result = await server.anthropic_get_vwap("btc-usd")
    with sqlite3.connect(server._entitlements.db_path) as conn:
        states = dict(conn.execute("SELECT charge_id, state FROM credit_charges").fetchall())

    assert "VWAP [btc-usd]" in result
    assert states["process-crash"] == "refunded"
    assert list(states.values()).count("delivered") == 1


@pytest.mark.asyncio
async def test_search_pairs_does_not_require_identity():
    mock_client = AsyncMock()
    mock_client.search_pairs = AsyncMock(
        return_value=[
            PairInfo(
                pair="btc-usd",
                base_currency="BTC",
                quote_currency="USD",
                asset_class="crypto",
                services=["vwap"],
                tier="core",
            )
        ]
    )
    server._client = mock_client

    result = await server.anthropic_search_pairs("btc")

    assert "Found 1 instruments" in result
    assert "btc-usd" in result
