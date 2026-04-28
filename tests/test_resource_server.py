"""
Tests for the FastAPI resource server with x402 middleware.
"""

from __future__ import annotations

import base64
import json
from unittest.mock import AsyncMock, patch
from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from src.resource_server import app
from src.resource_server import (
    _DISCOVERY_RATE_LIMITER,
    EVM_TRANSFER_TOPIC,
    _evm_transfer_satisfies_requirement,
    _solana_transfer_satisfies_requirement,
    _verify_payment,
)
from src.models import VWAPData, BidAskData
from src.public_metadata import REPOSITORY_URL
from src.config import settings


@pytest.fixture
def test_client():
    """Create a FastAPI test client."""
    with TestClient(app) as client:
        yield client


# ---------------------------------------------------------------------------
# Health Endpoint (Free)
# ---------------------------------------------------------------------------

class TestHealthEndpoint:
    def test_health_returns_200(self, test_client):
        response = test_client.get("/health")
        assert response.status_code == 200

    def test_health_contains_status(self, test_client):
        response = test_client.get("/health")
        data = response.json()
        assert data["status"] == "healthy"
        assert "pricing" in data
        assert "networks" in data
        assert "links" in data


class TestPublicListingSurfaces:
    def test_manifest_exposes_remote_mcp_url(self, test_client):
        response = test_client.get("/mcp/manifest.json")
        assert response.status_code == 200
        data = response.json()
        assert data["transport"]["type"] == "streamable-http"
        assert data["transport"]["url"].endswith("/mcp/server/")

    def test_public_remote_mcp_endpoint_exists(self, test_client):
        response = test_client.get("/mcp/server")
        assert response.status_code != 404

    def test_server_json_is_served(self, test_client):
        response = test_client.get("/server.json")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "info.blocksize.mcp/agentic-payments"
        assert data["repository"]["url"] == REPOSITORY_URL
        assert data["remotes"][0]["url"].endswith("/mcp/server/")

    def test_well_known_claim_files_exist(self, test_client):
        glama = test_client.get("/.well-known/glama.json")
        assert glama.status_code == 200
        assert glama.json()["maintainers"][0]["email"] == "info@blocksize.capital"

        registry_auth = test_client.get("/.well-known/mcp-registry-auth")
        assert registry_auth.status_code == 200
        assert registry_auth.text.startswith("v=MCPv1; k=ed25519; p=")

    def test_support_and_privacy_pages_exist(self, test_client):
        assert test_client.get("/support").status_code == 200
        assert test_client.get("/privacy").status_code == 200
        assert test_client.get("/quickstart/remote-mcp").status_code == 200
        assert test_client.get("/prompt-examples").status_code == 200


# ---------------------------------------------------------------------------
# x402 Payment Gate
# ---------------------------------------------------------------------------

class TestPaymentGate:
    def test_vwap_requires_payment(self, test_client):
        response = test_client.get("/v1/vwap/btc-usd")
        assert response.status_code == 402

    def test_bidask_requires_payment(self, test_client):
        response = test_client.get("/v1/bidask/btc-usd")
        assert response.status_code == 402

    def test_state_is_not_offered(self, test_client):
        response = test_client.get("/v1/state/btc-usd")
        assert response.status_code == 404

    def test_fx_requires_payment(self, test_client):
        response = test_client.get("/v1/fx/eurusd")
        assert response.status_code == 402

    def test_metal_requires_payment(self, test_client):
        response = test_client.get("/v1/metal/xauusd")
        assert response.status_code == 402

    def test_rate_is_not_offered(self, test_client):
        response = test_client.get("/v1/rate/10Y")
        assert response.status_code == 404

    def test_402_includes_payment_required_header(self, test_client):
        response = test_client.get("/v1/vwap/btc-usd")
        assert "PAYMENT-REQUIRED" in response.headers

        req_b64 = response.headers["PAYMENT-REQUIRED"]
        req_json = json.loads(base64.b64decode(req_b64))
        # Should be a list of requirements (multi-network)
        assert isinstance(req_json, list)
        assert len(req_json) >= 1
        assert "payTo" in req_json[0]
        assert req_json[0]["scheme"] == "exact"

    def test_402_body_contains_price(self, test_client):
        response = test_client.get("/v1/vwap/btc-usd")
        data = response.json()
        assert "price_usdc" in data
        assert "networks" in data

    def test_search_is_free(self, test_client):
        """Search endpoint should NOT require payment."""
        # Set up mock client since search actually tries to call blocksize
        mock_client = AsyncMock()
        mock_client.search_pairs = AsyncMock(return_value=[])
        app.state.blocksize = mock_client

        response = test_client.get("/v1/search?q=btc")
        assert response.status_code == 200

    def test_instruments_is_free(self, test_client):
        """Instruments endpoint should NOT require payment."""
        mock_client = AsyncMock()
        mock_client.list_vwap_instruments = AsyncMock(return_value=["btc-usd"])
        app.state.blocksize = mock_client

        response = test_client.get("/v1/instruments/vwap")
        assert response.status_code == 200

    def test_health_does_not_require_payment(self, test_client):
        response = test_client.get("/health")
        assert response.status_code == 200

    def test_batch_requires_total_payment(self, test_client):
        response = test_client.get("/v1/batch?reqs=vwap:BTCUSD,fx:EURUSD")
        assert response.status_code == 402
        assert response.json()["price_usdc"] == "0.007"

    def test_batch_rejects_excessive_items(self, test_client):
        reqs = ",".join(f"vwap:BTC{i}USD" for i in range(settings.server.max_batch_size + 1))
        response = test_client.get(f"/v1/batch?reqs={reqs}")
        assert response.status_code == 400

    def test_invalid_wallet_header_is_rejected(self, test_client):
        response = test_client.get(
            "/v1/vwap/btc-usd",
            headers={"X-AGENT-WALLET": "bad"},
        )
        assert response.status_code == 400


class TestDiscoveryRateLimit:
    def test_discovery_search_is_soft_capped_by_ip(self, test_client, monkeypatch):
        monkeypatch.setattr(settings.server, "discovery_rate_limit_enabled", True)
        monkeypatch.setattr(settings.server, "discovery_rate_limit_per_minute", 2)
        monkeypatch.setattr(settings.server, "discovery_rate_limit_per_day", 100)
        _DISCOVERY_RATE_LIMITER.clear()

        mock_client = AsyncMock()
        mock_client.search_pairs = AsyncMock(return_value=[])
        app.state.blocksize = mock_client

        headers = {"X-Forwarded-For": "203.0.113.10"}
        assert test_client.get("/v1/search?q=btc", headers=headers).status_code == 200
        assert test_client.get("/v1/search?q=eth", headers=headers).status_code == 200

        response = test_client.get("/v1/search?q=sol", headers=headers)

        assert response.status_code == 429
        assert response.headers["Retry-After"]
        assert response.json()["limit_window"] == "minute"

    def test_paid_routes_are_not_discovery_rate_limited(self, test_client, monkeypatch):
        monkeypatch.setattr(settings.server, "discovery_rate_limit_enabled", True)
        monkeypatch.setattr(settings.server, "discovery_rate_limit_per_minute", 1)
        monkeypatch.setattr(settings.server, "discovery_rate_limit_per_day", 1)
        _DISCOVERY_RATE_LIMITER.clear()

        headers = {"X-Forwarded-For": "203.0.113.11"}
        first = test_client.get("/v1/vwap/btc-usd", headers=headers)
        second = test_client.get("/v1/vwap/eth-usd", headers=headers)

        assert first.status_code == 402
        assert second.status_code == 402


class TestNativePaymentValidation:
    @pytest.mark.asyncio
    async def test_verify_payment_rejects_invalid_payload(self):
        requirement = {
            "network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
            "payTo": "11111111111111111111111111111111",
            "maxAmountRequired": "1",
            "asset": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp/EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        }
        result = await _verify_payment("not-base64", [requirement])
        assert result["valid"] is False

    def test_solana_transfer_must_match_recipient_and_amount(self):
        recipient = "So11111111111111111111111111111111111111112"
        mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        requirement = {
            "payTo": recipient,
            "maxAmountRequired": "100",
            "asset": f"solana:mainnet/{mint}",
        }
        transaction = {
            "meta": {
                "preTokenBalances": [
                    {
                        "accountIndex": 4,
                        "mint": mint,
                        "owner": recipient,
                        "uiTokenAmount": {"amount": "50"},
                    }
                ],
                "postTokenBalances": [
                    {
                        "accountIndex": 4,
                        "mint": mint,
                        "owner": recipient,
                        "uiTokenAmount": {"amount": "175"},
                    }
                ],
            }
        }
        assert _solana_transfer_satisfies_requirement(transaction, requirement)[0] is True

        too_expensive = {**requirement, "maxAmountRequired": "200"}
        assert _solana_transfer_satisfies_requirement(transaction, too_expensive)[0] is False

    def test_evm_transfer_must_match_contract_recipient_and_amount(self):
        recipient = "0x1111111111111111111111111111111111111111"
        token = "0x2222222222222222222222222222222222222222"
        recipient_topic = "0x" + recipient.removeprefix("0x").rjust(64, "0")
        requirement = {
            "payTo": recipient,
            "maxAmountRequired": "5000",
            "asset": f"eip155:8453/{token}",
        }
        receipt = {
            "logs": [
                {
                    "address": token,
                    "topics": [
                        EVM_TRANSFER_TOPIC,
                        "0x" + "0" * 64,
                        recipient_topic,
                    ],
                    "data": hex(7000),
                }
            ]
        }
        assert _evm_transfer_satisfies_requirement(receipt, requirement)[0] is True

        too_expensive = {**requirement, "maxAmountRequired": "8000"}
        assert _evm_transfer_satisfies_requirement(receipt, too_expensive)[0] is False


# ---------------------------------------------------------------------------
# Data Endpoints (with mocked payment)
# ---------------------------------------------------------------------------

class TestDataEndpoints:
    @pytest.fixture(autouse=True)
    def _mock_payment(self):
        with patch("src.resource_server._verify_payment", new_callable=AsyncMock, return_value={"valid": True}), \
             patch("src.resource_server._settle_payment", new_callable=AsyncMock, return_value={"success": True}):
            yield

    def test_vwap_endpoint(self, test_client):
        mock_vwap = VWAPData(pair="btc-usd", vwap=95432.50, timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc), currency="USD")
        mock_client = AsyncMock()
        mock_client.get_vwap_latest = AsyncMock(return_value=mock_vwap)
        app.state.blocksize = mock_client

        response = test_client.get("/v1/vwap/btc-usd", headers={"PAYMENT-SIGNATURE": "mock_sig"})
        assert response.status_code == 200
        data = response.json()
        assert data["data"]["pair"] == "btc-usd"
        assert data["data"]["vwap"] == 95432.50

    def test_bidask_endpoint(self, test_client):
        mock_bidask = BidAskData(pair="btc-usd", bid=95400.0, ask=95450.0, spread=50.0, spread_pct=0.0524, timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc))
        mock_client = AsyncMock()
        mock_client.get_bidask_snapshot = AsyncMock(return_value=mock_bidask)
        app.state.blocksize = mock_client

        response = test_client.get("/v1/bidask/btc-usd", headers={"PAYMENT-SIGNATURE": "mock_sig"})
        assert response.status_code == 200
        data = response.json()
        assert data["data"]["bid"] == 95400.0
        assert data["data"]["spread"] == 50.0
