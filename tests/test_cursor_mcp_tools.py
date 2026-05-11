from __future__ import annotations

import json
from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

from src import cursor_mcp_server as server
from src.connector_auth import ConnectorIdentity
from src.entitlement_manager import EntitlementManager
from src.models import VWAPData


@pytest.fixture(autouse=True)
def isolated_cursor_state(tmp_path, monkeypatch):
    server._client = None
    server._entitlements = EntitlementManager(
        tmp_path / "cursor_entitlements.db",
        default_daily_credits=2,
    )
    monkeypatch.setattr(server.cursor_auth, "resolve_cursor_identity", lambda: None)
    yield
    server._client = None
    server._entitlements = None


def _identity():
    return ConnectorIdentity(user_id="cursor-user-1", email="cursor@example.com", source="test")


@pytest.mark.asyncio
async def test_cursor_live_tool_requires_identity():
    result = await server.cursor_get_vwap("btc-usd")
    parsed = json.loads(result)

    assert parsed["status"] == "error"
    assert parsed["error_code"] == "AUTH_REQUIRED"


@pytest.mark.asyncio
async def test_cursor_credit_balance_returns_daily_status(monkeypatch):
    monkeypatch.setattr(server.cursor_auth, "resolve_cursor_identity", _identity)

    result = await server.cursor_get_credit_balance()
    parsed = json.loads(result)

    assert parsed["status"] == "ok"
    assert parsed["credits"]["daily_limit"] == 2
    assert parsed["credits"]["credits_remaining"] == 2


@pytest.mark.asyncio
async def test_cursor_vwap_spends_one_credit(monkeypatch):
    monkeypatch.setattr(server.cursor_auth, "resolve_cursor_identity", _identity)
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

    result = await server.cursor_get_vwap("btc-usd")

    assert "VWAP [btc-usd]" in result
    assert "Credits remaining today: 1/2" in result
    mock_client.get_vwap_latest.assert_awaited_once_with("BTCUSD")
    assert server._entitlements.status("cursor-user-1").credits_remaining == 1


@pytest.mark.asyncio
async def test_cursor_invalid_symbol_does_not_spend(monkeypatch):
    monkeypatch.setattr(server.cursor_auth, "resolve_cursor_identity", _identity)
    mock_client = AsyncMock()
    server._client = mock_client

    result = await server.cursor_get_vwap("../bad")
    parsed = json.loads(result)

    assert parsed["error_code"] == "INVALID_SYMBOL"
    assert server._entitlements.status("cursor-user-1").credits_remaining == 2
    mock_client.get_vwap_latest.assert_not_called()
