from __future__ import annotations

import json
from datetime import datetime, timezone
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


@pytest.mark.asyncio
async def test_vwap_spends_one_credit(monkeypatch):
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
    assert server._entitlements.status("user-1").credits_remaining == 1


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
