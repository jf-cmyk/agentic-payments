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
from src.models import VWAPData, BidAskData
from src.public_metadata import REPOSITORY_URL


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
        assert data["name"] == "io.github.jf-cmyk/blocksize-agentic-payments"
        assert data["repository"]["url"] == REPOSITORY_URL
        assert data["remotes"][0]["url"].endswith("/mcp/server/")

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
