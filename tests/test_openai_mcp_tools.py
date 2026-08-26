from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src import openai_mcp_server as server
from src.connector_auth import ConnectorIdentity
from src.entitlement_manager import EntitlementManager
from src.models import VWAPData


@pytest.fixture(autouse=True)
def isolated_openai_state(tmp_path, monkeypatch):
    server._client = None
    server._entitlements = EntitlementManager(
        tmp_path / "openai_entitlements.db",
        default_daily_credits=2,
    )
    monkeypatch.setattr(server.openai_auth, "resolve_openai_identity", lambda: None)
    yield
    server._client = None
    server._entitlements = None


def _identity():
    return ConnectorIdentity(
        user_id="openai-user-1",
        email="openai@example.com",
        source="test",
        principal_id="openai:test-scope:openai-user-1",
    )


@pytest.mark.asyncio
async def test_openai_live_tool_requires_identity():
    result = await server.openai_get_vwap("btc-usd")
    parsed = json.loads(result)

    assert parsed["status"] == "error"
    assert parsed["error_code"] == "AUTH_REQUIRED"


@pytest.mark.asyncio
async def test_openai_credit_balance_omits_direct_identifiers(monkeypatch):
    monkeypatch.setattr(server.openai_auth, "resolve_openai_identity", _identity)

    result = await server.openai_get_credit_balance()
    parsed = json.loads(result)

    assert parsed["status"] == "ok"
    assert parsed["credits"]["daily_limit"] == 2
    assert parsed["credits"]["credits_remaining"] == 2
    assert "user_id" not in parsed["credits"]
    assert "email" not in parsed["credits"]


@pytest.mark.asyncio
async def test_openai_vwap_returns_live_observation_and_spends_credit(monkeypatch):
    monkeypatch.setattr(server.openai_auth, "resolve_openai_identity", _identity)
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

    result = await server.openai_get_vwap("btc-usd")

    assert "VWAP [btc-usd]" in result
    assert "Credits remaining today: 1/2" in result
    mock_client.get_vwap_latest.assert_awaited_once_with("BTCUSD")
    assert server._entitlements.status("openai-user-1").credits_remaining == 1
    assert server._entitlements.schema_status()["charge_states"] == {"delivered": 1}
