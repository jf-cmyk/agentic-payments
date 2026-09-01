"""
Tests for the FastAPI resource server with x402 middleware.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import subprocess
from pathlib import Path
from unittest.mock import AsyncMock, patch
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from xml.etree import ElementTree

import pytest
import httpx
from fastapi.testclient import TestClient
from starlette.requests import Request

from src import public_metadata, resource_server
from src.blocksize_client import BlocksizeAPIError
from src.rwa_adapters import (
    HyperliquidPAXGAdapter,
    HyperliquidSpotRWAAdapter,
    JupiterRouterAdapter,
    KrakenXStocksAdapter,
    RWAAdapterRegistry,
)
from src.resource_server import app
from src.resource_server import (
    _DISCOVERY_RATE_LIMITER,
    EVM_TRANSFER_TOPIC,
    _evm_transfer_satisfies_requirement,
    _solana_transfer_satisfies_requirement,
    _verify_payment,
)
from src.models import (
    BidAskData,
    FXData,
    MetalData,
    PairInfo,
    StatePriceData,
    VWAP24HrData,
    VWAP30MinData,
    VWAPData,
)
from src.observability import UsageEventStore, configure_global_store
from src.config import settings
from src.credit_manager import CreditManager
from src.public_metadata import GLAMA_MAINTAINER_EMAIL
from src.rwa_store import RWAObservationStore

RWA_TEST_OPERATOR_TOKEN = "rwa-test-operator-token-0123456789abcdef"
OBSERVABILITY_TEST_TOKEN = "obs-7f428cdb969e14017176b82d6b67b4d1"


def _assert_oauth_unavailable(metadata: dict[str, object]) -> None:
    assert metadata["oauth_available"] is False
    assert metadata.get("authorization_servers", []) == []
    assert "authorization_endpoint" not in metadata
    assert "token_endpoint" not in metadata
    assert "registration_endpoint" not in metadata


def test_release_provenance_prefers_git_trigger_sha(monkeypatch):
    railway_sha = "a" * 40
    stale_manual_sha = "b" * 40
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", railway_sha)
    monkeypatch.setenv("RAILWAY_GIT_BRANCH", "main")
    monkeypatch.setenv("RELEASE_COMMIT_SHA", stale_manual_sha)

    provenance = resource_server._load_release_build()

    assert provenance["commit_sha"] == railway_sha
    assert provenance["source_branch"] == "main"
    assert provenance["stamped"] is True


def test_release_provenance_ignores_invalid_environment_sha(monkeypatch):
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA", raising=False)
    monkeypatch.setenv("RELEASE_COMMIT_SHA", "not-a-commit")

    provenance = resource_server._load_release_build()

    assert provenance["commit_sha"] is None
    assert provenance["stamped"] is False


def test_instrument_explorer_is_indexable_and_search_first(test_client):
    response = test_client.get("/instruments?q=bitcoin")

    assert response.status_code == 200
    assert response.headers["x-robots-tag"] == "index, follow"
    assert "Find the right instrument before you pay" in response.text
    assert "/v1/search" in response.text
    assert "price_usdc" in response.text
    assert "__TITLE__" not in response.text
    assert 'rel="canonical" href="https://mcp.blocksize.info/instruments"' in response.text

    category = test_client.get("/instruments/equities")
    assert category.status_code == 200
    assert 'const assetClass="equities"' in category.text


def test_pre_trade_sample_shows_value_without_claiming_live_data(test_client):
    response = test_client.get("/v1/samples/pre-trade")

    assert response.status_code == 200
    payload = response.json()
    assert payload["sample"] is True
    assert payload["live_data"] is False
    assert payload["price_usdc"] == "0.10"
    assert payload["example_response"]["decision"] == "pass"
    assert "PAYMENT-REQUIRED" not in response.headers


def test_unsupported_paid_symbol_fails_before_payment(test_client):
    seeded_at = resource_server.time.monotonic()
    app.state.instrument_catalog_cache["vwap"] = (
        seeded_at,
        frozenset({"BTCUSD"}),
    )
    mock_client = AsyncMock()
    mock_client.search_pairs_page = AsyncMock(return_value=([], 0))
    app.state.blocksize = mock_client

    response = test_client.get("/v1/vwap/NOTREALUSD")

    assert response.status_code == 404
    assert response.json()["error_code"] == "UNSUPPORTED_INSTRUMENT"
    assert "no payment" in response.json()["message"].lower()
    assert "PAYMENT-REQUIRED" not in response.headers


def test_search_returns_a_purchase_ready_canonical_result(test_client):
    mock_client = AsyncMock()
    mock_client.search_pairs_page = AsyncMock(
        return_value=(
            [
                PairInfo(
                    pair="BTCUSD",
                    base_currency="BTC",
                    quote_currency="USD",
                    asset_class="crypto",
                    services=["vwap", "bidask"],
                    tier="core",
                    canonical_symbol="BTCUSD",
                    recommended_service="vwap",
                    recommended_tool="get_vwap",
                    endpoint_path="/v1/vwap/BTCUSD",
                    price_usdc="0.002",
                    readiness="catalog_confirmed",
                    match_type="alias",
                    relevance_score=940,
                )
            ],
            1,
        )
    )
    app.state.blocksize = mock_client

    response = test_client.get("/v1/search?q=bitcoin")

    assert response.status_code == 200
    result = response.json()["pairs"][0]
    assert result["canonical_symbol"] == "BTCUSD"
    assert result["price_usdc"] == "0.002"
    assert result["purchase_url"].startswith(
        "https://mcp.blocksize.info/v1/vwap/BTCUSD?"
    )
    assert "selection_source=public_http_resolver" in result["purchase_url"]
    assert result["copy_request"].startswith("curl -i")


@pytest.fixture
def test_client(monkeypatch, tmp_path):
    """Create a FastAPI test client."""
    monkeypatch.setenv("RWA_MUTATIONS_ENABLED", "true")
    monkeypatch.setenv("RWA_OPERATOR_TOKEN", RWA_TEST_OPERATOR_TOKEN)
    monkeypatch.setenv("RWA_OBSERVATION_DB_PATH", str(tmp_path / "rwa_observations.db"))
    monkeypatch.setenv("CREDIT_DB_PATH", str(tmp_path / "credits.db"))
    monkeypatch.setattr(settings.server, "unverified_http_credits_enabled", True)
    monkeypatch.setattr(
        settings.x402,
        "solana_wallet_address",
        "11111111111111111111111111111111",
    )
    monkeypatch.setattr(
        settings.x402,
        "solana_fee_payer",
        "SysvarRent111111111111111111111111111111111",
    )
    monkeypatch.setattr(
        settings.x402,
        "evm_wallet_address",
        "0x1111111111111111111111111111111111111111",
    )
    monkeypatch.setattr(
        settings.server,
        "observability_dashboard_token",
        OBSERVABILITY_TEST_TOKEN,
    )
    with TestClient(
        app,
        base_url="https://testserver",
        headers={"Authorization": f"Bearer {OBSERVABILITY_TEST_TOKEN}"},
    ) as client:
        # Paid middleware now confirms catalog support before issuing a payment
        # challenge. Seed deterministic catalogs so unit tests never depend on
        # the public upstream; dedicated preflight tests replace these entries.
        seeded_at = resource_server.time.monotonic()
        app.state.instrument_catalog_cache = {
            "vwap": (seeded_at, frozenset({"BTCUSD", "ETHUSD", "SOLUSD", "PYTHUSD"})),
            "bidask": (seeded_at, frozenset({"BTCUSD", "ETHUSD", "SOLUSD", "AAPL", "EURUSD"})),
            "fx": (seeded_at, frozenset({"EURUSD", "GBPUSD", "USDJPY"})),
            "metal": (seeded_at, frozenset({"XAUUSD", "XAGUSD", "XPTUSD", "XPDUSD", "COPPERUSD"})),
            "state": (seeded_at, frozenset({"MSOLUSD", "SOLUSD", "PYTHUSD"})),
        }
        yield client


@pytest.fixture
def observability_store(tmp_path, monkeypatch):
    """Route observability writes into a per-test SQLite database."""
    store = UsageEventStore(tmp_path / "usage_events.db")
    monkeypatch.setattr(resource_server, "OBSERVABILITY", store)
    configure_global_store(store)
    yield store
    configure_global_store(None)


def test_observability_reports_the_canonical_resolver_funnel(observability_store):
    observability_store.record(
        "catalog_search_completed",
        surface="http_api",
        endpoint="/v1/search",
        subject="bitcoin",
    )
    observability_store.record(
        "instrument_resolved",
        surface="http_api",
        endpoint="/v1/search",
        subject="bitcoin",
        metadata={
            "canonical_symbol": "BTCUSD",
            "selection_source": "public_http_resolver",
        },
    )
    observability_store.record(
        "catalog_search_completed",
        surface="http_api",
        endpoint="/v1/search",
        subject="unknown",
    )
    observability_store.record(
        "unsupported_symbol_request",
        surface="http_api",
        endpoint="/v1/search",
        subject="UNKNOWN",
    )

    funnel = observability_store.summarize(days=1)["resolver_funnel"]

    assert funnel["search_events"] == 2
    assert funnel["resolved_events"] == 1
    assert funnel["distinct_resolved_symbols"] == 1
    assert funnel["zero_or_unsupported_events"] == 1
    assert funnel["search_to_resolution_rate"] == 0.5


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
        assert data["payments"]["operational"] is True
        assert data["networks"]["primary"]["operational"] is True
        assert data["networks"]["fallback"]["operational"] is True

    def test_health_distinguishes_liveness_from_payment_readiness(self, test_client):
        previous_support = app.state.facilitator_support
        app.state.facilitator_support = {
            "checked": True,
            "available": True,
            "required": True,
            "reason": None,
            "kinds": [
                {
                    "x402Version": 2,
                    "scheme": "exact",
                    "network": "eip155:84532",
                    "extra": {},
                }
            ],
        }
        try:
            response = test_client.get("/health")
        finally:
            app.state.facilitator_support = previous_support

        data = response.json()
        assert response.status_code == 200
        assert data["status"] == "healthy"
        assert data["payments"]["operational"] is False
        assert data["payments"]["advertised_networks"] == []

    def test_cache_status_is_free(self, test_client):
        response = test_client.get("/v1/cache/status")
        data = response.json()
        assert response.status_code == 200
        assert data["status"] == "ok"
        assert "vwap24h" in data["feeds"]
        assert "links" in data

    def test_anthropic_only_health_hides_payment_metadata(self, test_client, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_ONLY_MODE", "true")
        monkeypatch.setenv("ANTHROPIC_AUTH_PROVIDER", "clerk")
        monkeypatch.setenv("ANTHROPIC_ENABLE_BETA_TOKENS", "false")
        monkeypatch.setenv(
            "ANTHROPIC_MCP_PUBLIC_URL",
            "https://mcp.blocksize.info/anthropic/mcp",
        )

        response = test_client.get("/health")
        data = response.json()

        assert response.status_code == 200
        assert data["service"] == "blocksize-anthropic-mcp-beta"
        assert data["tool_surface"] == "read-only"
        assert data["mcp_url"].endswith("/anthropic/mcp")
        assert data["auth_provider"] == "clerk"
        assert data["beta_tokens_enabled"] is False
        assert data["oauth_available"] is False
        assert "oauth_callback_url" not in data
        assert "oauth_protected_resource_metadata" not in data
        assert "oauth_authorization_server_metadata" not in data
        assert "pricing" not in data
        assert "bulk_pricing" not in data


class TestSecurityHeaders:
    def test_free_response_includes_browser_security_headers(self, test_client):
        response = test_client.get("/health")

        assert response.headers["Strict-Transport-Security"] == (
            "max-age=31536000; includeSubDomains"
        )
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert response.headers["Permissions-Policy"] == (
            "camera=(), microphone=(), geolocation=()"
        )
        assert response.headers["X-Blocksize-Provider"] == "Blocksize"
        assert response.headers["X-Blocksize-Citation"].endswith("/category-hubs.json")

    def test_402_response_keeps_payment_and_security_headers(self, test_client):
        response = test_client.get(
            "/v1/vwap/btc-usd",
            headers={"Origin": "https://mcp.blocksize.info"},
        )

        assert response.status_code == 402
        assert response.headers["Strict-Transport-Security"] == (
            "max-age=31536000; includeSubDomains"
        )
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert response.headers["Permissions-Policy"] == (
            "camera=(), microphone=(), geolocation=()"
        )
        assert "PAYMENT-REQUIRED" in response.headers
        assert response.headers["Cache-Control"] == "no-store"
        assert response.headers["Access-Control-Allow-Origin"] == "https://mcp.blocksize.info"
        exposed = response.headers["Access-Control-Expose-Headers"].lower()
        assert "payment-required" in exposed


class TestPublicListingSurfaces:
    def test_http_client_loggers_do_not_emit_credential_bearing_urls(self):
        assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
        assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING

    def test_anthropic_only_mode_blocks_non_anthropic_surfaces(self, test_client, monkeypatch):
        monkeypatch.setenv("ANTHROPIC_ONLY_MODE", "true")

        paid_response = test_client.get("/v1/vwap/btc-usd")
        public_mcp_response = test_client.get("/mcp/server")
        anthropic_response = test_client.get("/anthropic/mcp")
        auth_metadata_response = test_client.get(
            "/.well-known/oauth-authorization-server/anthropic/mcp"
        )
        support_response = test_client.get("/support")
        prompt_examples_response = test_client.get("/prompt-examples")
        first_price_response = test_client.get("/quickstart/first-price")
        integrations_response = test_client.get("/integrations/agent-frameworks")

        assert paid_response.status_code == 404
        assert paid_response.json()["error_code"] == "ANTHROPIC_ONLY_MODE"
        assert public_mcp_response.status_code == 404
        assert anthropic_response.status_code != 404
        assert "PAYMENT-REQUIRED" not in anthropic_response.headers
        assert auth_metadata_response.status_code == 200
        assert auth_metadata_response.json()["issuer"].endswith("/anthropic/mcp")
        assert support_response.status_code == 200
        assert prompt_examples_response.status_code == 200
        assert first_price_response.status_code == 200
        assert "Get a live Blocksize price" in first_price_response.text
        assert "Get my first live price" in first_price_response.text
        assert "blocksize_first_price_agent_id" not in first_price_response.text
        assert "PAYMENT-SIGNATURE" in first_price_response.text
        assert 'fetch("/v1/vwap/btc-usd"' not in first_price_response.text
        assert integrations_response.status_code == 200
        assert "Six supported agent frameworks" in integrations_response.text
        assert integrations_response.text.count("raw.githubusercontent.com/jf-cmyk/agentic-payments") == 6
        assert integrations_response.text.count(">Download</a>") == 6
        assert "X-BLOCKSIZE-ACTIVATION-SOURCE" not in first_price_response.text

    def test_manifest_exposes_remote_mcp_url(self, test_client):
        response = test_client.get("/mcp/manifest.json")
        assert response.status_code == 200
        data = response.json()
        assert data["transport"]["type"] == "streamable-http"
        assert data["transport"]["url"].endswith("/mcp/server/")
        assert data["links"]["repository"] == (
            "https://github.com/jf-cmyk/agentic-payments"
        )

    def test_public_remote_mcp_endpoint_exists(self, test_client):
        response = test_client.get("/mcp/server")
        assert response.status_code != 404

    def test_root_favicons_exist_for_directory_crawlers(self, test_client):
        ico_response = test_client.get("/favicon.ico")
        svg_response = test_client.get("/favicon.svg")
        touch_response = test_client.get("/apple-touch-icon.png")

        assert ico_response.status_code == 200
        assert "image/x-icon" in ico_response.headers["content-type"]
        assert svg_response.status_code == 200
        assert "image/svg+xml" in svg_response.headers["content-type"]
        assert touch_response.status_code == 200
        assert "image/png" in touch_response.headers["content-type"]

    def test_anthropic_safe_mcp_endpoint_exists(self, test_client):
        response = test_client.get("/anthropic/mcp", follow_redirects=False)
        assert response.status_code != 404
        assert response.status_code not in {307, 308}
        assert "PAYMENT-REQUIRED" not in response.headers

    @pytest.mark.parametrize(
        "mcp_path",
        [
            "/mcp/server",
            "/anthropic/mcp",
            "/cursor/mcp",
            "/openai/mcp",
        ],
    )
    def test_mcp_mount_roots_do_not_redirect_without_trailing_slash(
        self,
        test_client,
        mcp_path,
    ):
        response = test_client.post(
            mcp_path,
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            follow_redirects=False,
        )

        assert response.status_code not in {307, 308}

    @pytest.mark.parametrize(
        "metadata_path",
        [
            "/.well-known/oauth-protected-resource/anthropic/mcp",
            "/.well-known/oauth-protected-resource/anthropic/mcp/",
        ],
    )
    def test_root_anthropic_oauth_protected_resource_metadata(
        self,
        test_client,
        metadata_path,
    ):
        response = test_client.get(metadata_path, follow_redirects=False)
        assert response.status_code == 200
        data = response.json()

        assert data["resource"].endswith("/anthropic/mcp/")
        _assert_oauth_unavailable(data)
        assert data["scopes_supported"] == ["openid", "email", "profile"]
        assert data["bearer_methods_supported"] == ["header"]

    @pytest.mark.parametrize(
        "metadata_path",
        [
            "/.well-known/oauth-authorization-server/anthropic/mcp",
            "/.well-known/openid-configuration/anthropic/mcp",
            "/anthropic/mcp/.well-known/openid-configuration",
        ],
    )
    def test_anthropic_oauth_authorization_server_metadata_aliases(
        self,
        test_client,
        metadata_path,
    ):
        response = test_client.get(metadata_path)
        assert response.status_code == 200
        data = response.json()

        assert data["issuer"].endswith("/anthropic/mcp")
        _assert_oauth_unavailable(data)
        assert data["scopes_supported"] == ["openid", "email", "profile"]

    def test_root_oauth_authorization_server_metadata_defaults_to_anthropic(
        self,
        test_client,
        monkeypatch,
    ):
        monkeypatch.delenv("ANTHROPIC_ONLY_MODE", raising=False)
        monkeypatch.delenv("ROOT_OAUTH_CONNECTOR", raising=False)

        response = test_client.get("/.well-known/oauth-authorization-server")
        assert response.status_code == 200
        data = response.json()

        assert data["issuer"].endswith("/anthropic/mcp")
        _assert_oauth_unavailable(data)

    def test_root_oauth_protected_resource_metadata_defaults_to_anthropic(
        self,
        test_client,
        monkeypatch,
    ):
        monkeypatch.delenv("ANTHROPIC_ONLY_MODE", raising=False)
        monkeypatch.delenv("ROOT_OAUTH_CONNECTOR", raising=False)

        response = test_client.get(
            "/.well-known/oauth-protected-resource",
            follow_redirects=False,
        )
        assert response.status_code == 200
        data = response.json()

        assert data["resource"].endswith("/anthropic/mcp/")
        _assert_oauth_unavailable(data)

    def test_root_oauth_protected_resource_metadata_survives_anthropic_only_mode(
        self,
        test_client,
        monkeypatch,
    ):
        monkeypatch.setenv("ANTHROPIC_ONLY_MODE", "true")
        monkeypatch.delenv("ROOT_OAUTH_CONNECTOR", raising=False)

        response = test_client.get(
            "/.well-known/oauth-protected-resource",
            follow_redirects=False,
        )
        assert response.status_code == 200
        data = response.json()

        assert data["resource"].endswith("/anthropic/mcp/")
        _assert_oauth_unavailable(data)

    def test_cursor_mcp_endpoint_exists(self, test_client):
        response = test_client.get("/cursor/mcp", follow_redirects=False)
        assert response.status_code != 404
        assert response.status_code not in {307, 308}
        assert "PAYMENT-REQUIRED" not in response.headers

    def test_openai_mcp_endpoint_exists(self, test_client):
        response = test_client.get("/openai/mcp", follow_redirects=False)
        assert response.status_code != 404
        assert response.status_code not in {307, 308}
        assert "PAYMENT-REQUIRED" not in response.headers

    @pytest.mark.parametrize(
        "metadata_path",
        [
            "/.well-known/oauth-protected-resource/openai/mcp",
            "/.well-known/oauth-protected-resource/openai/mcp/",
        ],
    )
    def test_openai_oauth_protected_resource_metadata(
        self,
        test_client,
        metadata_path,
    ):
        response = test_client.get(metadata_path, follow_redirects=False)
        assert response.status_code == 200
        data = response.json()

        assert data["resource"].endswith("/openai/mcp/")
        _assert_oauth_unavailable(data)
        assert data["scopes_supported"] == [
            "openid",
            "email",
            "profile",
            "offline_access",
        ]
        assert data["bearer_methods_supported"] == ["header"]

    @pytest.mark.parametrize(
        "metadata_path",
        [
            "/.well-known/oauth-authorization-server/openai/mcp",
            "/.well-known/openid-configuration/openai/mcp",
            "/openai/mcp/.well-known/openid-configuration",
        ],
    )
    def test_openai_oauth_authorization_server_metadata_aliases(
        self,
        test_client,
        metadata_path,
    ):
        response = test_client.get(metadata_path)
        assert response.status_code == 200
        data = response.json()

        assert data["issuer"].endswith("/openai/mcp")
        _assert_oauth_unavailable(data)
        assert "offline_access" in data["scopes_supported"]

    def test_root_oauth_metadata_can_be_openai_for_openai_hosts(
        self,
        test_client,
        monkeypatch,
    ):
        monkeypatch.setenv("ROOT_OAUTH_CONNECTOR", "openai")

        auth_server = test_client.get("/.well-known/oauth-authorization-server")
        protected = test_client.get("/.well-known/oauth-protected-resource")

        assert auth_server.status_code == 200
        assert auth_server.json()["issuer"].endswith("/openai/mcp")
        assert "offline_access" in auth_server.json()["scopes_supported"]
        _assert_oauth_unavailable(auth_server.json())
        assert protected.status_code == 200
        assert protected.json()["resource"].endswith("/openai/mcp/")
        _assert_oauth_unavailable(protected.json())

    @pytest.mark.parametrize(
        "metadata_path",
        [
            "/.well-known/oauth-protected-resource/cursor/mcp",
            "/.well-known/oauth-protected-resource/cursor/mcp/",
        ],
    )
    def test_root_cursor_oauth_protected_resource_metadata(
        self,
        test_client,
        metadata_path,
    ):
        response = test_client.get(metadata_path, follow_redirects=False)
        assert response.status_code == 200
        data = response.json()

        assert data["resource"].endswith("/cursor/mcp/")
        _assert_oauth_unavailable(data)
        assert data["scopes_supported"] == ["email", "profile"]
        assert data["bearer_methods_supported"] == ["header"]

    def test_root_oauth_authorization_server_metadata_can_be_cursor_for_cursor_hosts(
        self,
        test_client,
        monkeypatch,
    ):
        monkeypatch.setenv("ROOT_OAUTH_CONNECTOR", "cursor")

        response = test_client.get("/.well-known/oauth-authorization-server")
        assert response.status_code == 200
        data = response.json()

        assert data["issuer"].endswith("/cursor/mcp")
        _assert_oauth_unavailable(data)
        assert data["scopes_supported"] == ["email", "profile"]

    def test_root_oauth_protected_resource_metadata_can_be_cursor_for_cursor_hosts(
        self,
        test_client,
        monkeypatch,
    ):
        monkeypatch.setenv("ROOT_OAUTH_CONNECTOR", "cursor")

        response = test_client.get(
            "/.well-known/oauth-protected-resource",
            follow_redirects=False,
        )
        assert response.status_code == 200
        data = response.json()

        assert data["resource"].endswith("/cursor/mcp/")
        _assert_oauth_unavailable(data)
        assert data["scopes_supported"] == ["email", "profile"]

    @pytest.mark.parametrize(
        "metadata_path",
        [
            "/.well-known/oauth-authorization-server/cursor/mcp",
            "/.well-known/openid-configuration/cursor/mcp",
            "/cursor/mcp/.well-known/openid-configuration",
        ],
    )
    def test_cursor_oauth_authorization_server_metadata_aliases(
        self,
        test_client,
        metadata_path,
    ):
        response = test_client.get(metadata_path)
        assert response.status_code == 200
        data = response.json()

        assert data["issuer"].endswith("/cursor/mcp")
        _assert_oauth_unavailable(data)

    def test_health_exposes_cursor_connector_metadata(self, test_client):
        response = test_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        cursor = data["cursor_connector"]

        assert cursor["mcp_url"].endswith("/cursor/mcp")
        assert cursor["tool_surface"] == "read-only"
        assert "get_vwap" in cursor["tool_costs"]
        assert "asset_class=equity" in cursor["equities"]
        assert data["equities"]["example_endpoint"] == "/v1/bidask/AAPLXUSD"
        assert data["links"]["cursor_mcp"].endswith("/cursor/mcp/")

    def test_health_exposes_anthropic_connector_metadata(self, test_client):
        response = test_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        anthropic = data["anthropic_connector"]

        assert anthropic["mcp_url"].endswith("/anthropic/mcp")
        assert anthropic["tool_surface"] == "read-only"
        assert "get_vwap" in anthropic["tool_costs"]
        assert "AAPL" in anthropic["equities"]
        assert data["links"]["anthropic_mcp"].endswith("/anthropic/mcp/")
        assert data["links"]["claude_connector"].endswith("/claude-connector")
        assert data["links"]["first_price_quickstart"].endswith("/quickstart/first-price")
        assert data["links"]["category_hubs_json"].endswith("/category-hubs.json")
        assert data["links"]["rwa_market_data"].endswith("/rwa-market-data")
        assert data["links"]["market_data_licensing"].endswith("/market-data-licensing")
        assert data["links"]["signed_oracle_feeds"].endswith("/signed-oracle-feeds")
        assert data["links"]["rwa_coverage_index"].endswith("/evidence/rwa-coverage-index.html")
        assert data["links"]["oracle_lineage_index"].endswith("/evidence/oracle-lineage-index.html")

    def test_public_evidence_indexes_are_served(self, test_client):
        rwa = test_client.get("/evidence/rwa-coverage-index.html")
        lineage = test_client.get("/evidence/oracle-lineage-index.html")
        rwa_pdf = test_client.get("/pdf/Blocksize_RWA_Coverage_Index.pdf")
        lineage_pdf = test_client.get("/pdf/Blocksize_Oracle_Lineage_Index.pdf")

        assert rwa.status_code == 200
        assert "Blocksize RWA Coverage Index" in rwa.text
        assert lineage.status_code == 200
        assert "Blocksize Oracle Lineage" in lineage.text
        assert rwa_pdf.status_code == 200
        assert rwa_pdf.headers["content-type"] == "application/pdf"
        assert lineage_pdf.status_code == 200
        assert lineage_pdf.headers["content-type"] == "application/pdf"

    def test_server_json_is_served(self, test_client):
        response = test_client.get("/server.json")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "info.blocksize.mcp/agentic-payments"
        assert data["title"] == "Blocksize Agentic Market Intelligence"
        assert data["repository"] == {
            "url": "https://github.com/jf-cmyk/agentic-payments",
            "source": "github",
        }
        assert data["remotes"][0]["url"].endswith("/mcp/server/")

    def test_manifest_exposes_market_data_display_name_and_endpoint_builder(self, test_client):
        response = test_client.get("/mcp/manifest.json")
        assert response.status_code == 200
        data = response.json()

        assert data["name"] == "Blocksize Agentic Market Intelligence"
        assert "state prices" in data["description"]
        assert "supported equity ticker" in data["description"]
        assert "trader indicator packages" in data["description"]
        assert data["links"]["llms_txt"].endswith("/llms.txt")
        assert data["links"]["sitemap"].endswith("/sitemap.xml")
        assert data["links"]["data_packages_json"].endswith("/data-packages.json")
        assert data["links"]["category_hubs_json"].endswith("/category-hubs.json")
        assert data["capabilities"]["ai_reader_brief"].endswith("/llms.txt")
        assert data["capabilities"]["data_package_catalog"].endswith("/data-packages.json")
        assert data["capabilities"]["category_hubs"].endswith("/category-hubs.json")
        assert "asset_class=equity" in data["capabilities"]["equities"]
        assert any(tool["name"] == "get_market_data_endpoint" for tool in data["tools"])

    def test_manifest_tool_contract_is_generated_from_public_mcp(self, test_client):
        response = test_client.get("/mcp/manifest.json")
        assert response.status_code == 200
        manifest_tools = response.json()["tools"]
        live_tools = asyncio.run(resource_server.public_mcp.list_tools())

        assert [tool["name"] for tool in manifest_tools] == [
            tool.name for tool in live_tools
        ]
        for manifest_tool, live_tool in zip(manifest_tools, live_tools, strict=True):
            assert manifest_tool["description"] == (live_tool.description or "")
            assert manifest_tool["parameters"] == live_tool.parameters
            assert manifest_tool["annotations"] == live_tool.annotations.model_dump(
                mode="json",
                by_alias=True,
                exclude_none=True,
            )
            assert manifest_tool["payment"] == {"required": False}

    def test_manifest_supports_head_without_a_response_body(self, test_client):
        response = test_client.head("/mcp/manifest.json")

        assert response.status_code == 200
        assert response.content == b""

    def test_crawler_and_ai_reader_files_are_served(self, test_client):
        portal_head = test_client.head("/")
        assert portal_head.status_code == 200
        assert portal_head.headers["cache-control"] == "no-store, max-age=0"

        robots = test_client.get("/robots.txt")
        assert robots.status_code == 200
        assert "Sitemap: https://mcp.blocksize.info/sitemap.xml" in robots.text
        assert "Allow: /llms.txt" in robots.text
        assert "Allow: /data-packages.json" in robots.text
        assert "Allow: /category-hubs.json" in robots.text
        assert "Allow: /og/" in robots.text

        sitemap = test_client.get("/sitemap.xml")
        assert sitemap.status_code == 200
        assert "application/xml" in sitemap.headers["content-type"]
        assert "<loc>https://mcp.blocksize.info/</loc>" in sitemap.text
        assert "<loc>https://mcp.blocksize.info/llms.txt</loc>" in sitemap.text
        assert "<loc>https://mcp.blocksize.info/data-packages.json</loc>" in sitemap.text
        assert "<loc>https://mcp.blocksize.info/category-hubs.json</loc>" in sitemap.text
        assert "<loc>https://mcp.blocksize.info/market-data-api-for-ai-agents</loc>" in sitemap.text
        assert "<loc>https://mcp.blocksize.info/blocksize-market-data-agent-skill</loc>" in sitemap.text
        assert "<loc>https://mcp.blocksize.info/crypto-vwap-api</loc>" in sitemap.text
        assert "<loc>https://mcp.blocksize.info/equities-bidask-api</loc>" in sitemap.text
        assert "<loc>https://mcp.blocksize.info/real-time-price-data-api</loc>" in sitemap.text
        assert "<loc>https://mcp.blocksize.info/mcp-market-data-server</loc>" in sitemap.text
        assert "<loc>https://mcp.blocksize.info/price-data-api-examples</loc>" in sitemap.text
        assert "<loc>https://mcp.blocksize.info/state-price-api</loc>" in sitemap.text
        assert "<loc>https://mcp.blocksize.info/trader-alpha-pack-api</loc>" in sitemap.text
        assert "<loc>https://mcp.blocksize.info/rwa-market-data</loc>" in sitemap.text
        assert "<loc>https://mcp.blocksize.info/market-data-licensing</loc>" in sitemap.text
        assert "<loc>https://mcp.blocksize.info/signed-oracle-feeds</loc>" in sitemap.text
        assert "<loc>https://mcp.blocksize.info/openapi.json</loc>" in sitemap.text

        llms = test_client.get("/llms.txt")
        assert llms.status_code == 200
        assert "Blocksize Agentic Market Intelligence for AI Agents" in llms.text
        assert "Remote MCP discovery server" in llms.text
        assert "real-time price data API" in llms.text
        assert "AMM state price API" in llms.text
        assert "trader alpha signal API" in llms.text
        assert "Data packages JSON" in llms.text
        assert "Crypto VWAP API" in llms.text
        assert "Equities package" in llms.text
        assert "equities bid ask API" in llms.text
        assert "Real-Time Price Data API" in llms.text
        assert "MCP Market Data Server" in llms.text
        assert "Blocksize already provides broad production market-data coverage" in llms.text
        assert "no newly sourced third-party or onchain addition" in llms.text
        assert "signed market data API" in llms.text

    def test_every_sitemapped_url_has_canonical_and_index_headers(self, test_client):
        sitemap = test_client.get("/sitemap.xml")
        root = ElementTree.fromstring(sitemap.content)
        namespace = {"sitemap": "http://www.sitemaps.org/schemas/sitemap/0.9"}
        locations = [
            element.text
            for element in root.findall("sitemap:url/sitemap:loc", namespace)
        ]

        assert locations
        assert len(locations) == len(set(locations))
        for location in locations:
            assert location is not None
            path = urlsplit(location).path or "/"
            response = test_client.get(path)
            assert response.status_code == 200, path
            assert response.headers["x-robots-tag"] == "index, follow", path
            assert f'<{location}>; rel="canonical"' in response.headers["link"], path

    @pytest.mark.parametrize(
        "path",
        [
            "/health",
            "/readyz",
            "/v1/products",
            "/internal/observability/stats",
            "/.well-known/glama.json",
            "/openai/mcp",
        ],
    )
    def test_operational_auth_and_api_surfaces_are_not_indexable(
        self,
        test_client,
        path,
    ):
        response = test_client.get(path, follow_redirects=False)
        assert response.status_code != 404
        assert response.headers["x-robots-tag"] == "noindex, nofollow, noarchive"

    def test_data_package_catalog_and_intent_pages_are_served(self, test_client):
        catalog = test_client.get("/data-packages.json")
        assert catalog.status_code == 200
        data = catalog.json()
        assert data["canonical_url"].endswith("/data-packages.json")
        assert data["remote_mcp_server"].endswith("/mcp/server/")
        assert any(page["url"].endswith("/real-time-price-data-api") for page in data["intent_pages"])
        assert any(page["url"].endswith("/equities-bidask-api") for page in data["intent_pages"])
        assert any(package["id"] == "crypto-vwap" for package in data["packages"])
        assert any(package["id"] == "equities-bidask" for package in data["packages"])
        assert any(package["endpoint_template"] == "/v1/bidask/{pair}" for package in data["packages"])
        assert any(package["request_examples"] for package in data["packages"])
        assert data["indexing_submission"]["google_search_console"]["submit_sitemap"].endswith("/sitemap.xml")
        assert any(
            url.endswith("/mcp-market-data-server")
            for url in data["indexing_submission"]["bing_webmaster_tools"]["request_indexing"]
        )

        catalog_head = test_client.head("/data-packages.json")
        assert catalog_head.status_code == 200

        category_hubs = test_client.get("/category-hubs.json")
        assert category_hubs.status_code == 200
        hubs_data = category_hubs.json()
        assert hubs_data["provider"] == "Blocksize Capital GmbH"
        assert len(hubs_data["hubs"]) == 3
        rwa_hub = next(hub for hub in hubs_data["hubs"] if hub["slug"] == "rwa-market-data")
        assert any(
            item["state"] == "production" and item["value"] == "Live"
            for item in rwa_hub["coverage"]
        )
        assert rwa_hub["expansion_pipeline"]["production_promoted_new_sources"] == 0
        assert rwa_hub["expansion_pipeline"]["existing_blocksize_production_coverage_affected"] is False

        rwa_page = test_client.get("/rwa-market-data")
        assert rwa_page.status_code == 200
        assert "1,169 / 3,438" in rwa_page.text
        assert "2,139 / 5,161" in rwa_page.text
        assert "93 / 1,169" in rwa_page.text
        assert "first baseline" in rwa_page.text
        assert "existing Blocksize market-data coverage" in rwa_page.text
        assert "zero newly sourced third-party or onchain additions" in rwa_page.text
        assert "does not describe or reduce existing Blocksize production coverage" in rwa_page.text
        assert "Definition and coverage boundary" in rwa_page.text

        licensing_page = test_client.get("/market-data-licensing")
        assert licensing_page.status_code == 200
        assert "API access alone does not grant all of those rights" in licensing_page.text

        signed_page = test_client.get("/signed-oracle-feeds")
        assert signed_page.status_code == 200
        assert "not described as signed unless a signature envelope is present" in signed_page.text

        agent_page = test_client.get("/market-data-api-for-ai-agents")
        assert agent_page.status_code == 200
        assert "Market Data API for AI Agents" in agent_page.text
        assert "/data-packages.json" in agent_page.text
        assert "application/ld+json" in agent_page.text
        assert "/go/free-trial?utm_source=mcp.blocksize.info" in agent_page.text
        assert "Verify the claim before production use" in agent_page.text

        skill_page = test_client.get("/blocksize-market-data-agent-skill")
        assert skill_page.status_code == 200
        assert "Blocksize Market Data Agent Skill" in skill_page.text
        assert "OpenAI market data skill" in skill_page.text
        assert "/go/free-trial?utm_source=mcp.blocksize.info" in skill_page.text

        comparison_page = test_client.get("/market-data-api-comparison")
        assert comparison_page.status_code == 200
        assert "How to evaluate this category" in comparison_page.text
        assert "No competitor capability is asserted" in comparison_page.text
        assert "ItemList" in comparison_page.text

        alternatives_page = test_client.get("/crypto-market-data-api-alternatives")
        assert alternatives_page.status_code == 200
        assert "Crypto Market Data API Alternatives" in alternatives_page.text

        oracle_page = test_client.get("/oracle-data-api-for-ai-agents")
        assert oracle_page.status_code == 200
        assert "Oracle Data API for AI Agents" in oracle_page.text

        vwap_page = test_client.get("/crypto-vwap-api")
        assert vwap_page.status_code == 200
        assert "Crypto VWAP API" in vwap_page.text
        assert "/v1/vwap/{pair}" in vwap_page.text
        assert "canonical" in vwap_page.text

        price_data_page = test_client.get("/real-time-price-data-api")
        assert price_data_page.status_code == 200
        assert "Real-Time Price Data API" in price_data_page.text
        assert "BreadcrumbList" in price_data_page.text
        assert "WebAPI" in price_data_page.text
        assert "Examples agents can execute" in price_data_page.text
        assert "Which package should I use?" in price_data_page.text
        assert "/og/real-time-price-data-api.svg" in price_data_page.text

        equities_page = test_client.get("/equities-bidask-api")
        assert equities_page.status_code == 200
        assert "Equities Bid/Ask API" in equities_page.text
        assert "/v1/bidask/{ticker}" in equities_page.text
        assert "AAPL" in equities_page.text

        examples_page = test_client.head("/price-data-api-examples")
        assert examples_page.status_code == 200

        og_image = test_client.get("/og/crypto-vwap-api.svg")
        assert og_image.status_code == 200
        assert "image/svg+xml" in og_image.headers["content-type"]
        assert "Crypto VWAP API" in og_image.text

    def test_repository_metadata_can_still_be_enabled(self, test_client, monkeypatch):
        repository_url = "https://example.com/blocksize/agentic-payments"
        monkeypatch.setattr(public_metadata, "REPOSITORY_URL", repository_url)
        monkeypatch.setattr(public_metadata, "REPOSITORY_SOURCE", "github")
        monkeypatch.setattr(resource_server, "REPOSITORY_URL", repository_url)

        server_json = test_client.get("/server.json").json()
        manifest = test_client.get("/mcp/manifest.json").json()

        assert server_json["repository"] == {
            "url": repository_url,
            "source": "github",
        }
        assert manifest["links"]["repository"] == repository_url

    def test_well_known_claim_files_exist(self, test_client):
        glama = test_client.get("/.well-known/glama.json")
        assert glama.status_code == 200
        assert glama.json()["$schema"] == "https://glama.ai/mcp/schemas/connector.json"
        maintainer = glama.json()["maintainers"][0]
        assert maintainer["email"] == GLAMA_MAINTAINER_EMAIL

        registry_auth = test_client.get("/.well-known/mcp-registry-auth")
        assert registry_auth.status_code == 200
        assert registry_auth.text == public_metadata.MCP_REGISTRY_AUTH_CONTENT

        x402 = test_client.get("/.well-known/x402")
        assert x402.status_code == 200
        x402_data = x402.json()
        assert x402_data["version"] == 1
        assert "/v1/vwap/BTC-USD" in x402_data["resources"][0]
        assert any(resource.endswith("/v1/bidask/AAPLXUSD") for resource in x402_data["resources"])

    @pytest.mark.parametrize(
        "metadata_path",
        [
            "/.well-known/glama.json",
            "/.well-known/mcp-registry-auth",
            "/.well-known/x402",
            "/.well-known/oauth-protected-resource",
            "/.well-known/oauth-authorization-server",
            "/.well-known/oauth-protected-resource/anthropic/mcp",
            "/.well-known/oauth-authorization-server/anthropic/mcp",
            "/.well-known/oauth-protected-resource/cursor/mcp",
            "/.well-known/oauth-authorization-server/cursor/mcp",
            "/.well-known/oauth-protected-resource/openai/mcp",
            "/.well-known/oauth-authorization-server/openai/mcp",
        ],
    )
    def test_well_known_metadata_supports_head_and_conditional_cache(
        self,
        test_client,
        metadata_path,
    ):
        get_response = test_client.get(metadata_path)
        head_response = test_client.head(metadata_path)

        assert get_response.status_code == 200
        assert head_response.status_code == 200
        assert head_response.content == b""
        assert get_response.headers["cache-control"] == "public, max-age=300"
        assert head_response.headers["cache-control"] == "public, max-age=300"
        assert get_response.headers["etag"] == head_response.headers["etag"]

        conditional = test_client.get(
            metadata_path,
            headers={"If-None-Match": get_response.headers["etag"]},
        )
        assert conditional.status_code == 304
        assert conditional.content == b""

    def test_openapi_marks_paid_routes_for_x402_discovery(self, test_client):
        response = test_client.get("/openapi.json")
        assert response.status_code == 200
        data = response.json()

        vwap = data["paths"]["/v1/vwap/{pair}"]["get"]
        payment_info = vwap["x-payment-info"]
        assert payment_info["protocols"] == [{"x402": {}}]
        assert payment_info["price"]["mode"] == "dynamic"

        bidask = data["paths"]["/v1/bidask/{pair}"]["get"]
        assert bidask["x-payment-info"]["price"]["max"] == str(settings.pricing.equities)

        fx = data["paths"]["/v1/fx/{pair}"]["get"]
        assert fx["x-payment-info"]["price"]["amount"] == str(settings.pricing.tradfi)

    def test_support_and_privacy_pages_exist(self, test_client):
        assert test_client.get("/support").status_code == 200
        assert test_client.get("/privacy").status_code == 200
        terms_response = test_client.get("/terms", follow_redirects=False)
        assert terms_response.status_code in {307, 308}
        assert terms_response.headers["location"] == (
            "https://blocksize.info/terms-conditions-data/"
        )
        assert test_client.get("/claude-connector").status_code == 200
        claude = test_client.get("/claude-connector")
        assert "asset_class=equity" in claude.text
        assert "AAPL bid/ask" in claude.text
        assert test_client.get("/quickstart/remote-mcp").status_code == 200
        assert test_client.get("/prompt-examples").status_code == 200

    def test_support_page_does_not_expose_direct_email_or_gitlab_repo(self, test_client):
        response = test_client.get("/support")
        assert response.status_code == 200
        body = response.text
        assert "https://blocksize.info/contact/?utm_source=agentic-widget&utm_medium=ai" in body
        assert "info@blocksize.capital" not in body
        assert "gitlab.com/jfocke/agentic-payments" not in body
        assert "GitLab repository" not in body


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

    def test_bidask_equity_ticker_uses_equity_price(self, test_client):
        response = test_client.get("/v1/bidask/AAPL")
        assert response.status_code == 402
        assert response.json()["price_usdc"] == str(settings.pricing.equities)

    def test_unsupported_methods_are_not_payment_challenged(self, test_client):
        response = test_client.post("/v1/vwap/btc-usd")
        assert response.status_code == 405
        assert "PAYMENT-REQUIRED" not in response.headers

    @pytest.mark.parametrize(
        "path",
        [
            "/v1/briefs/market",
            "/v1/checks/pre-trade",
            "/v1/receipts/price",
            "/v1/snapshots/macro",
            "/v1/monitors/evaluate",
            "/v1/indicators/token-quality",
            "/v1/indicators/state-divergence",
            "/v1/signals/solana-token-brief",
            "/v1/signals/trader-alpha-pack",
            "/v1/rwa/benchmark/blocksize",
        ],
    )
    def test_post_only_paid_routes_reject_get_before_payment(self, test_client, path):
        response = test_client.get(path, headers={"X-PAYMENT": "must-not-be-used"})

        assert response.status_code == 405
        assert "PAYMENT-REQUIRED" not in response.headers
        assert "PAYMENT-RESPONSE" not in response.headers

    def test_unsupported_post_route_method_never_charges_or_draws_credit(
        self,
        observability_store,
        test_client,
        tmp_path,
    ):
        previous_manager = app.state.credits
        manager = CreditManager(str(tmp_path / "credits.db"))
        app.state.credits = manager
        agent_id = "wrong-method-agent-12345678"

        try:
            with patch(
                "src.resource_server._verify_payment",
                new_callable=AsyncMock,
            ) as verify_payment:
                starter_response = test_client.get(
                    "/v1/checks/pre-trade",
                    headers={"X-AGENT-ID": agent_id},
                )
                paid_response = test_client.get(
                    "/v1/checks/pre-trade",
                    headers={"X-PAYMENT": "must-not-be-verified"},
                )
                missing_starter_response = test_client.post(
                    "/v1/checks/pre-trade/not-a-route",
                    headers={"X-AGENT-ID": agent_id},
                )
                missing_paid_response = test_client.post(
                    "/v1/checks/pre-trade/not-a-route",
                    headers={"X-PAYMENT": "also-must-not-be-verified"},
                )
        finally:
            app.state.credits = previous_manager

        assert starter_response.status_code == 405
        assert paid_response.status_code == 405
        assert missing_starter_response.status_code == 404
        assert missing_paid_response.status_code == 404
        assert "PAYMENT-REQUIRED" not in starter_response.headers
        assert "PAYMENT-RESPONSE" not in paid_response.headers
        assert "PAYMENT-REQUIRED" not in missing_starter_response.headers
        assert "PAYMENT-RESPONSE" not in missing_paid_response.headers
        assert manager.get_balance(agent_id) == 0.0
        verify_payment.assert_not_awaited()

        stats = observability_store.summarize(days=1)
        assert stats["event_counts"].get("credit_drawdown_success", 0) == 0
        assert stats["event_counts"].get("payment_proof_submitted", 0) == 0
        assert stats["event_counts"].get("payment_verified", 0) == 0
        assert stats["event_counts"].get("charged_delivery_failed", 0) == 0
        assert stats["reliability"]["client_protocol_responses"] == 4
        assert stats["reliability"]["charged_delivery_failures"] == 0

    def test_state_requires_payment(self, test_client):
        response = test_client.get("/v1/state/MSOLUSD")
        assert response.status_code == 402

    def test_vwap_windows_require_payment(self, test_client):
        assert test_client.get("/v1/vwap30m/SOLUSD").status_code == 402
        assert test_client.get("/v1/vwap24h/BTCUSD").status_code == 402

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
        assert response.headers["Cache-Control"] == "no-store"

        req_b64 = response.headers["PAYMENT-REQUIRED"]
        req_json = json.loads(base64.b64decode(req_b64))
        assert req_json["x402Version"] == 2
        assert req_json["resource"]["url"].startswith("https://mcp.blocksize.info/")
        assert req_json["resource"]["url"].endswith("/v1/vwap/btc-usd")
        resource_url = req_json["resource"]["url"]
        assert isinstance(req_json["accepts"], list)
        assert len(req_json["accepts"]) >= 1
        assert "payTo" in req_json["accepts"][0]
        assert "amount" in req_json["accepts"][0]
        assert req_json["accepts"][0]["asset"] == settings.x402.solana_usdc_address
        assert req_json["accepts"][0]["scheme"] == "exact"
        assert "resource" not in req_json["accepts"][0]
        assert req_json["accepts"][0]["extra"]["resource"] == resource_url
        assert req_json["accepts"][0]["extra"]["feePayer"] == (
            settings.x402.solana_fee_payer
        )
        base_requirement = next(
            item
            for item in req_json["accepts"]
            if item["network"] == settings.x402.base_network
        )
        assert base_requirement["extra"]["name"] == "USD Coin"
        assert base_requirement["extra"]["version"] == "2"
        assert "bazaar" in req_json["extensions"]
        bazaar = req_json["extensions"]["bazaar"]
        assert bazaar["info"]["input"]["type"] == "http"
        assert bazaar["info"]["input"]["method"] == "GET"
        assert bazaar["info"]["output"]["type"] == "json"
        assert bazaar["schema"]["properties"]["input"]["required"] == [
            "type",
            "method",
        ]

    def test_unusable_solana_rail_is_not_advertised(self, test_client, monkeypatch):
        monkeypatch.setattr(settings.x402, "solana_fee_payer", "")

        response = test_client.get("/v1/vwap/btc-usd")
        data = response.json()

        assert response.status_code == 402
        assert [item["network"] for item in data["accepts"]] == [
            settings.x402.base_network
        ]
        assert data["networks"] == [
            {"name": "Base L2", "caip2": settings.x402.base_network}
        ]
        assert "Solana" not in data["message"]

    def test_disabled_base_rail_is_not_advertised(self, test_client, monkeypatch):
        monkeypatch.setattr(settings.x402, "base_payments_enabled", False)

        response = test_client.get("/v1/vwap/btc-usd")
        data = response.json()

        assert response.status_code == 402
        assert [item["network"] for item in data["accepts"]] == [
            settings.x402.solana_network
        ]
        assert data["networks"] == [
            {"name": "Solana", "caip2": settings.x402.solana_network}
        ]
        assert "Base L2" not in data["message"]

    def test_challenge_intersects_configured_rails_with_facilitator_support(
        self,
        test_client,
    ):
        previous_support = app.state.facilitator_support
        app.state.facilitator_support = {
            "checked": True,
            "available": True,
            "required": True,
            "reason": None,
            "kinds": [
                {
                    "x402Version": 2,
                    "scheme": "exact",
                    "network": settings.x402.base_network,
                    "extra": {},
                }
            ],
        }
        try:
            response = test_client.get("/v1/vwap/btc-usd")
        finally:
            app.state.facilitator_support = previous_support

        assert response.status_code == 402
        assert [item["network"] for item in response.json()["accepts"]] == [
            settings.x402.base_network
        ]

    def test_unsupported_facilitator_networks_fail_closed(self, test_client):
        previous_support = app.state.facilitator_support
        app.state.facilitator_support = {
            "checked": True,
            "available": True,
            "required": True,
            "reason": None,
            "kinds": [
                {
                    "x402Version": 2,
                    "scheme": "exact",
                    "network": "eip155:84532",
                    "extra": {},
                }
            ],
        }
        try:
            response = test_client.get("/v1/vwap/btc-usd")
        finally:
            app.state.facilitator_support = previous_support

        assert response.status_code == 503
        assert response.json()["error"] == "Payment Configuration Unavailable"

    def test_no_operational_payment_rail_fails_closed(self, test_client, monkeypatch):
        monkeypatch.setattr(settings.x402, "solana_fee_payer", "")
        monkeypatch.setattr(settings.x402, "evm_wallet_address", "")

        response = test_client.get("/v1/vwap/btc-usd")

        assert response.status_code == 503
        assert response.json()["error"] == "Payment Configuration Unavailable"
        assert "PAYMENT-REQUIRED" not in response.headers

    def test_readiness_reports_configured_but_unusable_payment_rail(
        self,
        test_client,
        monkeypatch,
    ):
        monkeypatch.setenv("READINESS_REQUIRE_PAYMENT_WALLET", "true")
        monkeypatch.setattr(settings.x402, "solana_fee_payer", "")
        monkeypatch.setattr(settings.x402, "evm_wallet_address", "")

        response = test_client.get("/readyz")
        payment_wallet = response.json()["checks"]["payment_wallet"]

        assert response.status_code == 503
        assert payment_wallet["ready"] is False
        assert payment_wallet["configured_rails"] == 1
        assert payment_wallet["operational_rails"] == 0
        assert "fee_payer_missing" in payment_wallet["rails"]["solana"]["blockers"]

    def test_readiness_requires_exact_facilitator_network_intersection(
        self,
        test_client,
        monkeypatch,
    ):
        monkeypatch.setenv("RAILWAY_SERVICE_ID", "test-service")
        previous_support = app.state.facilitator_support
        app.state.facilitator_support = {
            "checked": True,
            "available": True,
            "required": True,
            "reason": None,
            "kinds": [
                {
                    "x402Version": 2,
                    "scheme": "exact",
                    "network": "eip155:84532",
                    "extra": {},
                }
            ],
        }
        try:
            response = test_client.get("/readyz")
        finally:
            app.state.facilitator_support = previous_support

        support = response.json()["checks"]["facilitator_support"]
        assert response.status_code == 503
        assert support["ready"] is False
        assert support["supported_networks"] == ["eip155:84532"]
        assert support["advertised_networks"] == []

    def test_402_exposes_payment_challenge_to_allowed_browser_origin(self, test_client):
        response = test_client.get(
            "/v1/vwap/btc-usd",
            headers={"Origin": "https://mcp.blocksize.info"},
        )

        assert response.status_code == 402
        assert response.headers["Access-Control-Allow-Origin"] == "https://mcp.blocksize.info"
        assert "Origin" in response.headers["Vary"]
        assert "PAYMENT-REQUIRED" in response.headers
        exposed = response.headers["Access-Control-Expose-Headers"].lower()
        assert "payment-required" in exposed
        assert "payment-response" in exposed
        assert "x-payment-response" in exposed

    def test_post_402_uses_canonical_bazaar_body_metadata(self, test_client):
        response = test_client.post("/v1/briefs/market", json={"symbols": ["BTCUSD"]})
        bazaar = response.json()["extensions"]["bazaar"]

        assert response.status_code == 402
        assert bazaar["info"]["input"] == {
            "type": "http",
            "method": "POST",
            "bodyType": "json",
            "body": {},
        }
        assert bazaar["schema"]["properties"]["input"]["required"] == [
            "type",
            "method",
            "bodyType",
            "body",
        ]

    def test_official_x402_evm_client_constructs_payload_from_challenge(self, test_client):
        from eth_account import Account
        from x402.client import x402ClientSync
        from x402.mechanisms.evm.exact.client import ExactEvmScheme
        from x402.schemas import PaymentRequired

        challenge = test_client.get("/v1/vwap/btc-usd").json()
        base_requirement = next(
            item
            for item in challenge["accepts"]
            if item["network"] == settings.x402.base_network
        )
        payment_required = PaymentRequired.model_validate(
            {
                "x402Version": 2,
                "resource": challenge["resource"],
                "accepts": [base_requirement],
                "extensions": challenge["extensions"],
            }
        )
        client = x402ClientSync().register(
            settings.x402.base_network,
            ExactEvmScheme(Account.create()),
        )

        payload = client.create_payment_payload(payment_required)
        encoded = payload.model_dump(by_alias=True, exclude_none=True)

        assert encoded["accepted"] == base_requirement
        assert encoded["payload"]["authorization"]["to"] == base_requirement["payTo"]
        assert encoded["payload"]["authorization"]["value"] == base_requirement["amount"]
        assert encoded["payload"]["signature"].startswith("0x")

    def test_official_x402_solana_client_constructs_payload_from_challenge(
        self,
        test_client,
        monkeypatch,
    ):
        from types import SimpleNamespace

        from solders.hash import Hash
        from solders.keypair import Keypair
        from x402.client import x402ClientSync
        from x402.mechanisms.svm.constants import TOKEN_PROGRAM_ADDRESS
        from x402.mechanisms.svm.exact.client import ExactSvmScheme
        from x402.mechanisms.svm.signers import KeypairSigner
        from x402.schemas import PaymentRequired

        recipient = Keypair()
        fee_payer = Keypair()
        payer = Keypair()
        monkeypatch.setattr(settings.x402, "solana_wallet_address", str(recipient.pubkey()))
        monkeypatch.setattr(settings.x402, "solana_fee_payer", str(fee_payer.pubkey()))
        challenge = test_client.get("/v1/vwap/btc-usd").json()
        solana_requirement = next(
            item
            for item in challenge["accepts"]
            if item["network"] == settings.x402.solana_network
        )
        payment_required = PaymentRequired.model_validate(
            {
                "x402Version": 2,
                "resource": challenge["resource"],
                "accepts": [solana_requirement],
                "extensions": challenge["extensions"],
            }
        )

        mint_data = bytearray(82)
        mint_data[44] = 6

        class FakeSolanaClient:
            def get_account_info(self, _mint):
                return SimpleNamespace(
                    value=SimpleNamespace(
                        owner=TOKEN_PROGRAM_ADDRESS,
                        data=bytes(mint_data),
                    )
                )

            def get_latest_blockhash(self):
                return SimpleNamespace(value=SimpleNamespace(blockhash=Hash.default()))

        scheme = ExactSvmScheme(KeypairSigner(payer))
        monkeypatch.setattr(scheme, "_get_client", lambda _network: FakeSolanaClient())
        client = x402ClientSync().register(settings.x402.solana_network, scheme)

        payload = client.create_payment_payload(payment_required)
        encoded = payload.model_dump(by_alias=True, exclude_none=True)

        assert encoded["accepted"] == solana_requirement
        assert encoded["payload"]["transaction"]

    def test_402_does_not_open_cors_for_unconfigured_origin(self, test_client):
        response = test_client.get(
            "/v1/vwap/btc-usd",
            headers={"Origin": "https://example.invalid"},
        )

        assert response.status_code == 402
        assert "Access-Control-Allow-Origin" not in response.headers
        assert "PAYMENT-REQUIRED" in response.headers

    def test_402_body_contains_price(self, test_client):
        response = test_client.get("/v1/vwap/btc-usd")
        data = response.json()
        assert data["x402Version"] == 2
        assert "price_usdc" in data
        assert data["starter_credits"]["eligibility"] == "authenticated_connector_only"
        assert data["starter_credits"]["available_on_this_surface"] is False
        assert "signed x402" in data["starter_credits"]["direct_public_http"]
        assert data["starter_credits"]["allowance_credits"] == 50.0
        assert "networks" in data
        assert "accepts" in data
        assert "resource" not in data["accepts"][0]
        assert data["accepts"][0]["extra"]["resource"] == data["resource"]["url"]
        assert "legacy_requirements" in data

    def test_search_is_free(self, test_client):
        """Search endpoint should NOT require payment."""
        # Set up mock client since search actually tries to call blocksize
        mock_client = AsyncMock()
        mock_client.search_pairs_page = AsyncMock(return_value=([], 0))
        app.state.blocksize = mock_client

        response = test_client.get("/v1/search?q=btc")
        assert response.status_code == 200

    def test_search_accepts_equity_filter(self, test_client):
        mock_client = AsyncMock()
        mock_client.search_pairs_page = AsyncMock(return_value=([], 0))
        app.state.blocksize = mock_client

        response = test_client.get("/v1/search?q=AAPL&asset_class=equity")

        assert response.status_code == 200
        mock_client.search_pairs_page.assert_awaited_once_with(
            "AAPL", "equity", limit=50, offset=0
        )

    def test_search_reports_full_total_and_pagination(self, test_client):
        mock_client = AsyncMock()
        pairs = [
            PairInfo(
                pair="BTC-USD",
                base_currency="BTC",
                quote_currency="USD",
                asset_class="crypto",
                services=["vwap"],
            )
        ]
        mock_client.search_pairs_page = AsyncMock(return_value=(pairs, 73))
        app.state.blocksize = mock_client

        response = test_client.get("/v1/search?q=BTC&limit=1&offset=20")

        assert response.status_code == 200
        data = response.json()
        assert data["total_matches"] == 73
        assert data["returned_matches"] == 1
        assert data["offset"] == 20
        assert data["next_offset"] == 21
        assert data["has_more"] is True

    def test_unified_coverage_distinguishes_live_and_research(self, test_client):
        mock_client = AsyncMock()
        mock_client.list_vwap_instruments = AsyncMock(return_value=["BTCUSD", "ETHUSD"])
        mock_client.list_bidask_instruments = AsyncMock(return_value=["BTCUSD", "AAPL"])
        mock_client.list_fx_instruments = AsyncMock(return_value=["EURUSD"])
        mock_client.list_metal_instruments = AsyncMock(return_value=["XAUUSD"])
        app.state.blocksize = mock_client

        response = test_client.get("/v1/coverage")

        assert response.status_code == 200
        data = response.json()
        assert data["live_data"]["namespaces"]["vwap"]["enabled_instrument_count"] == 2
        assert data["rwa_registry"]["canonical_assets"] >= 1
        assert data["rwa_registry"]["decision_grade_canonical_assets"] >= 1
        assert data["rwa_registry"]["research_only_or_manual_verification_assets"] >= 1
        assert data["commercial_model"]["discovery_cost_credits"] == 0
        assert data["commercial_model"]["starter_credits_apply_to_products_not_symbols"] is True
        assert data["access"]["live_http"]["availability"] != "locked_pending_funded_canary"
        assert set(data["access"]["live_http"]["rails"]) == {"solana", "base"}

    def test_instruments_is_free(self, test_client):
        """Instruments endpoint should NOT require payment."""
        mock_client = AsyncMock()
        instruments = [f"pair-{index:04d}" for index in reversed(range(605))]
        mock_client.list_vwap_instruments = AsyncMock(return_value=instruments)
        app.state.blocksize = mock_client

        response = test_client.get("/v1/instruments/vwap")
        assert response.status_code == 200
        data = response.json()
        assert data["total_instruments"] == 605
        assert data["returned_instruments"] == 100
        assert data["offset"] == 0
        assert data["limit"] == 100
        assert data["has_more"] is True
        assert data["next_offset"] == 100
        assert data["instruments"] == sorted(instruments)[:100]
        assert len(data["meta"]["snapshot_sha256"]) == 64
        assert data["meta"]["source_observed_at"] is None
        assert data["meta"]["freshness_status"] == "upstream_timestamp_unavailable"

        next_page = test_client.get("/v1/instruments/vwap?limit=100&offset=100")
        assert next_page.status_code == 200
        next_data = next_page.json()
        assert next_data["instruments"] == sorted(instruments)[100:200]
        assert next_data["meta"]["snapshot_sha256"] == data["meta"]["snapshot_sha256"]

    def test_instrument_pagination_bounds_are_enforced(self, test_client):
        assert test_client.get("/v1/instruments/vwap?limit=0").status_code == 422
        assert test_client.get("/v1/instruments/vwap?limit=501").status_code == 422
        assert test_client.get("/v1/instruments/vwap?offset=-1").status_code == 422

    def test_rwa_build_plan_is_free_and_quality_aligned(self, test_client):
        response = test_client.get("/v1/rwa/build-plan")

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "rwa_market_data_build_plan"
        assert "kraken_xstocks" in {venue["id"] for venue in data["first_wave_venues"]}
        assert "ostium" in {venue["id"] for venue in data["first_wave_venues"]}
        assert "gains" in {venue["id"] for venue in data["first_wave_venues"]}
        assert data["quality_alignment"]["outlier_detection"]["primary_method"] == (
            "median_absolute_deviation"
        )
        assert "/v1/rwa/vwap/{symbol}?block_size_usd=10000&venue=kraken_xstocks" in (
            data["target_endpoints"]
        )

    def test_rwa_coverage_filters_symbols_by_asset_class_and_venue(self, test_client):
        response = test_client.get("/v1/rwa/coverage?asset_class=fx&venue=ostium")

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "rwa_market_data_coverage"
        assert data["coverage_summary"]["rows"] == 9
        assert data["coverage_summary"]["coverage_row_count"] == 9
        assert data["coverage_summary"]["canonical_asset_count"] == 5
        assert data["coverage_summary"]["by_asset_class"] == {"fx": 5}
        assert data["coverage_summary"]["by_venue"] == {"ostium": 5}
        assert data["coverage_summary"]["metric_grains"] == {
            "coverage_row_count": "venue_instrument",
            "canonical_asset_count": "canonical_asset",
            "canonical_asset_count_by_asset_class": "asset_class_canonical_asset",
            "canonical_asset_count_by_venue": "venue_canonical_asset",
            "coverage_row_count_by_source_type": "source_type_venue_instrument",
            "identity_quality": "canonical_asset_identity_acceptance",
        }
        assert all(row["asset_class"] == "fx" for row in data["symbols"])
        assert all(row["venue"] == "ostium" for row in data["symbols"])

    def test_rwa_collection_defaults_are_compact_bounded_and_grain_named(
        self,
        test_client,
    ):
        coverage_response = test_client.get("/v1/rwa/coverage")
        assets_response = test_client.get("/v1/rwa/assets")

        assert coverage_response.status_code == 200
        assert assets_response.status_code == 200
        coverage = coverage_response.json()
        assets = assets_response.json()
        assert len(coverage["symbols"]) == 50
        assert coverage["pagination"] == {
            "limit": 50,
            "offset": 0,
            "returned": 50,
            "total": coverage["coverage_summary"]["coverage_row_count"],
            "has_more": True,
            "next_offset": 50,
        }
        assert len(assets["assets"]) == 10
        assert assets["pagination"] == {
            "limit": 10,
            "offset": 0,
            "returned": 10,
            "total": assets["summary"]["canonical_asset_count"],
            "has_more": True,
            "next_offset": 10,
        }
        assert assets["summary"]["metric_grains"]["coverage_row_count"] == (
            "venue_instrument"
        )
        assert len(coverage_response.content) < 250_000
        assert len(assets_response.content) < 1_000_000

    def test_rwa_collection_pagination_is_stable_and_non_overlapping(self, test_client):
        first = test_client.get("/v1/rwa/coverage?limit=7&offset=0").json()
        second = test_client.get("/v1/rwa/coverage?limit=7&offset=7").json()
        repeated = test_client.get("/v1/rwa/coverage?limit=7&offset=0").json()

        assert first["symbols"] == repeated["symbols"]
        first_rows = {json.dumps(row, sort_keys=True) for row in first["symbols"]}
        second_rows = {json.dumps(row, sort_keys=True) for row in second["symbols"]}
        assert first_rows.isdisjoint(second_rows)
        assert second["pagination"]["offset"] == 7
        assert second["pagination"]["next_offset"] == 14

        first_assets = test_client.get("/v1/rwa/assets?limit=7&offset=0").json()
        second_assets = test_client.get("/v1/rwa/assets?limit=7&offset=7").json()
        assert {
            row["asset_id"] for row in first_assets["assets"]
        }.isdisjoint({row["asset_id"] for row in second_assets["assets"]})
        assert first_assets["assets"] == test_client.get(
            "/v1/rwa/assets?limit=7&offset=0"
        ).json()["assets"]

    @pytest.mark.parametrize("path", ["/v1/rwa/coverage", "/v1/rwa/assets"])
    def test_rwa_collection_pagination_rejects_out_of_bounds(self, test_client, path):
        maximum = test_client.get(f"{path}?limit=100")
        assert maximum.status_code == 200
        assert maximum.json()["pagination"]["limit"] == 100
        assert maximum.json()["pagination"]["returned"] == 100
        assert test_client.get(f"{path}?limit=101").status_code == 422
        assert test_client.get(f"{path}?limit=0").status_code == 422
        assert test_client.get(f"{path}?offset=-1").status_code == 422

    def test_rwa_coverage_can_hide_symbol_rows(self, test_client):
        response = test_client.get("/v1/rwa/coverage?include_symbols=false")

        assert response.status_code == 200
        data = response.json()
        assert "symbols" not in data
        assert data["coverage_summary"]["unique_assets"] > 0

    def test_rwa_assets_returns_cross_venue_sourcing_matrix(self, test_client):
        response = test_client.get("/v1/rwa/assets?limit=100")

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "rwa_asset_sourcing_matrix"
        assert data["summary"]["asset_count"] >= 100
        assert data["summary"]["registry_venue_count"] >= 10
        assets = {asset["asset_id"]: asset for asset in data["assets"]}
        assert {"ostium", "gains", "kraken_xstocks", "jupiter_router", "meteora_dlmm"}.issubset(set(assets["AAPL"]["venues"]))
        assert assets["AAPL"]["venues"]["kraken_xstocks"]["coverage_status"] == (
            "catalog_unconfirmed_public_pair_rejected"
        )
        assert assets["AAPL"]["venues"]["kraken_xstocks"]["vwap_support"] == (
            "requires_dynamic_catalog_confirmation"
        )
        assert assets["AAPL"]["sourcing_status"] == "multi_venue"
        assert data["summary"]["reference_only_assets"] >= 900
        assert assets["TBILL"]["sourcing_status"] in {"single_venue", "multi_venue"}
        assert assets["USTB"]["sourcing_status"] in {"single_venue", "multi_venue"}
        assert assets["USCC"]["sourcing_status"] in {"single_venue", "multi_venue"}
        assert any(
            venue["venue"] == "rwa_xyz_new_asset_monitor"
            for venue in data["dynamic_registry_venues"]
        )
        assert "blocksize_state" in assets["TBILL"]["venues"]
        assert "uniswap_v3_v4" in assets["USCC"]["venues"]
        dynamic_venues = {venue["venue"] for venue in data["dynamic_registry_venues"]}
        assert "ondo_stocks" in dynamic_venues
        assert "jupiter_router" in dynamic_venues
        assert "uniswap_v3_v4" in dynamic_venues
        assert "polygon_tradfi_reference" in dynamic_venues

    def test_rwa_assets_include_hyperliquid_spot_symbol_inventory(self, test_client):
        response = test_client.get(
            "/v1/rwa/assets?venue=hyperliquid_rwa_spot&limit=100"
        )

        assert response.status_code == 200
        data = response.json()
        assert data["summary"]["coverage_rows"] == 31
        assert data["summary"]["asset_count"] == 30
        assets = {asset["asset_id"]: asset for asset in data["assets"]}
        assert {"AAPL", "SPY", "THBILL", "XAUT0", "SPCXD", "DIME"}.issubset(assets)
        aapl = assets["AAPL"]["venues"]["hyperliquid_rwa_spot"]
        assert aapl["symbol"] == "AAPL/USDC"
        assert aapl["metadata"]["hyperliquid_coin"] == "@268"
        assert aapl["coverage_status"] == (
            "hyperliquid_spot_candidate_requires_identity_liquidity_and_benchmark_validation"
        )
        assert assets["DIME"]["venues"]["hyperliquid_rwa_spot"]["vwap_support"] == (
            "requires_identity_verification"
        )

    def test_rwa_assets_split_treasury_fund_from_tokenized_fund(self, test_client):
        response = test_client.get(
            "/v1/rwa/assets?asset_class=treasury_fund&limit=100"
        )

        assert response.status_code == 200
        data = response.json()
        assets = {asset["asset_id"]: asset for asset in data["assets"]}
        assert {"BUIDL", "OUSG", "USDY", "TBILL", "USTB"}.issubset(assets)
        assert "USCC" not in assets
        assert all("treasury_fund" in asset["asset_classes"] for asset in assets.values())

    def test_rwa_collections_accept_sovereign_debt_filter(self, test_client):
        coverage_response = test_client.get(
            "/v1/rwa/coverage?asset_class=sovereign_debt"
        )
        assets_response = test_client.get(
            "/v1/rwa/assets?asset_class=sovereign_debt&limit=100"
        )

        assert coverage_response.status_code == 200
        assert assets_response.status_code == 200
        coverage = coverage_response.json()
        assets = assets_response.json()
        assert coverage["coverage_summary"]["coverage_row_count"] > 0
        assert all(row["asset_class"] == "sovereign_debt" for row in coverage["symbols"])
        assert all(
            "sovereign_debt" in row["asset_classes"] for row in assets["assets"]
        )

    def test_rwa_identity_audit_answers_buidl_and_uscc(self, test_client):
        response = test_client.get(
            "/v1/rwa/identity-audit?asset_id=BUIDL,USCC,SGOV"
        )

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "rwa_ticker_identity_audit"
        assert "not a direct Treasury instrument" in data["summary"]["buidl_answer"]
        assets = {row["asset_id"]: row for row in data["rows"]}
        assert assets["BUIDL"]["verified_primary_type"] == "tokenized_liquidity_fund"
        assert assets["BUIDL"]["registry_asset_classes"] == ["treasury_fund"]
        assert assets["USCC"]["verified_primary_type"] == "tokenized_crypto_carry_fund"
        assert assets["USCC"]["registry_asset_classes"] == ["tokenized_fund"]
        assert assets["SGOV"]["verified_primary_type"] == "etf"

    def test_rwa_dex_venues_exposes_quality_plan(self, test_client):
        response = test_client.get("/v1/rwa/dex-venues")

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "rwa_dex_venue_quality_plan"
        assert data["summary"]["dex_venue_count"] >= 8
        assert data["summary"]["by_source_type"]["quote_sweep"] > 0
        assert data["summary"]["by_source_type"]["onchain_clmm_pool"] > 0
        assert data["summary"]["by_source_type"]["onchain_stableswap_pool"] > 0
        venue_ids = {venue["id"] for venue in data["venues"]}
        assert {"jupiter_router", "raydium_clmm", "orca_whirlpool", "meteora_dlmm"}.issubset(venue_ids)
        assert "pool allowlist with token-contract verification" in data["quality_requirements"]["promotion_gates"]

    def test_rwa_dex_allowlist_exposes_route_pool_promotion_jobs(self, test_client):
        response = test_client.get("/v1/rwa/dex-allowlist")

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "rwa_dex_allowlist"
        assert data["summary"]["candidate_count"] >= 45
        assert data["summary"]["candidate_count"] >= 60
        assert data["summary"]["promotion_job_count"] == data["summary"]["candidate_count"]
        assert data["summary"]["provider_dex_count"] >= 10
        assert data["summary"]["by_source_type"]["quote_sweep"] > 0
        assert data["summary"]["by_source_type"]["onchain_clmm_pool"] > 0
        assert data["summary"]["by_source_type"]["onchain_stableswap_pool"] > 0
        assert data["summary"]["by_asset_class"]["tokenized_fund"] >= 4
        assert data["asset_class_minimums"]["fx"]["min_liquidity_usd"] == 1000000
        assert data["asset_class_minimums"]["tokenized_fund"]["min_liquidity_usd"] == 250000
        candidates = {row["allowlist_id"]: row for row in data["candidates"]}
        assert "dex:jupiter_router:AAPLX:USD" in candidates
        assert "dex:uniswap_v3_v4:TBILL:USDC" in candidates
        assert "dex:uniswap_v3_v4:USCC:USDC" in candidates
        assert candidates["dex:jupiter_router:AAPLX:USD"]["candidate_kind"] == "router_route"
        assert candidates["dex:uniswap_v3_v4:USCC:USDC"]["asset_class"] == "tokenized_fund"
        assert "token_contract_or_mint_verification" in candidates["dex:jupiter_router:AAPLX:USD"]["blockers"]
        assert any(job["venue"] == "uniswap_v3_v4" and job["symbol"] == "PAXG/USDC" for job in data["promotion_jobs"])

        filtered = test_client.get(
            "/v1/rwa/dex-allowlist",
            params={"venue": "meteora_dlmm", "status": "planned_adapter"},
        )
        assert filtered.status_code == 200
        filtered_data = filtered.json()
        assert filtered_data["summary"]["candidate_count"] > 0
        assert all(row["venue"] == "meteora_dlmm" for row in filtered_data["candidates"])
        assert all(row["status"] == "planned_adapter" for row in filtered_data["candidates"])

    def test_rwa_non_crypto_feeds_builds_vwap_bidask_and_comparison_plan(self, test_client):
        response = test_client.get("/v1/rwa/non-crypto-feeds")

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "rwa_non_crypto_feed_catalog"
        assert data["summary"]["feed_count"] >= 330
        assert data["summary"]["vwap_feed_count"] > 0
        assert data["summary"]["bidask_feed_count"] > 0
        assert data["summary"]["excluded_tokenized_stock_rows"] > 0
        assert data["summary"]["by_blocksize_benchmark_status"]["ready_for_blocksize_benchmark"] > 0
        assert data["summary"]["by_blocksize_benchmark_status"]["requires_blocksize_state_instrument_check"] == 7
        assert data["summary"]["by_venue"]["blocksize_state"] == 7
        assert all(
            not feed["symbol"].split("/", 1)[0].endswith("x")
            for feed in [*data["vwap_feeds"], *data["bidask_feeds"]]
        )
        assert all(feed["venue"] != "kraken_xstocks" for feed in data["vwap_feeds"])

    def test_rwa_non_crypto_feeds_can_include_tokenized_stock_rows_deliberately(self, test_client):
        response = test_client.get(
            "/v1/rwa/non-crypto-feeds"
            "?exclude_tokenized_stocks=false&venue=jupiter_router&limit=100"
        )

        assert response.status_code == 200
        data = response.json()
        assert data["summary"]["excluded_tokenized_stock_rows"] == 0
        assert not any(feed["venue"] == "kraken_xstocks" for feed in data["vwap_feeds"])
        assert not any(feed["venue"] == "kraken_xstocks" for feed in data["bidask_feeds"])
        assert any(feed["symbol"].startswith("AAPLx/") for feed in data["bidask_feeds"])

    def test_rwa_non_crypto_fx_feeds_map_to_existing_blocksize_fx(self, test_client):
        response = test_client.get("/v1/rwa/non-crypto-feeds?asset_class=fx&venue=ostium")

        assert response.status_code == 200
        data = response.json()
        assert data["summary"]["feed_count"] == 18
        assert data["summary"]["by_blocksize_benchmark_status"] == {
            "ready_for_blocksize_benchmark": 16,
            "requires_blocksize_instrument_check": 2,
        }
        ready_feeds = [
            feed
            for feed in [*data["vwap_feeds"], *data["bidask_feeds"]]
            if feed["blocksize_benchmark"]["status"] == "ready_for_blocksize_benchmark"
        ]
        assert {feed["blocksize_benchmark"]["service"] for feed in ready_feeds} == {"fx"}
        assert any(feed["blocksize_benchmark"]["symbol"] == "EURUSD" for feed in data["bidask_feeds"])
        assert any(
            feed["symbol"] == "USD/KRW"
            and feed["blocksize_benchmark"]["status"] == "requires_blocksize_instrument_check"
            for feed in data["bidask_feeds"]
        )

    def test_rwa_discovery_blocks_candidate_rows_from_production(self, test_client):
        response = test_client.get("/v1/rwa/discovery?include_feed_details=false")

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "rwa_feed_discovery_promotion_audit"
        assert data["summary"]["feed_count"] >= 420
        assert data["summary"]["production_promoted"] == 0
        assert data["summary"]["blocked_from_production"] == data["summary"]["feed_count"]
        assert data["summary"]["candidate_or_supplemental_count"] == data["summary"]["feed_count"]
        assert data["summary"]["by_venue"]["blocksize_state"] == 7
        assert data["summary"]["by_required_gate"]["state_instrument_confirmation"] == 7
        assert data["summary"]["by_required_gate"]["liquidity_depth_volume"] == data["summary"]["feed_count"]
        assert "state_rule" in data["policy"]
        assert data["policy"]["promotion_rule"].startswith("Only rows with every required gate")
        assert "feeds" not in data

        state_response = test_client.get("/v1/rwa/discovery?venue=blocksize_state")
        assert state_response.status_code == 200
        state_data = state_response.json()
        assert state_data["summary"]["feed_count"] == 7
        state_feeds = {feed["symbol"]: feed for feed in state_data["feeds"]}
        assert {"TBILL/USD", "USTB/USD", "USCC/USD"}.issubset(state_feeds)
        tbill = state_feeds["TBILL/USD"]
        assert tbill["source_type"] == "blocksize_state_reference"
        assert tbill["promotion_status"] == "production_blocked_state_reference_only"
        assert tbill["production_promoted"] is False
        assert "state_instrument_confirmation" in tbill["required_gates"]
        assert tbill["gates"]["state_instrument_confirmation"]["status"] != "passed"
        assert tbill["gates"]["liquidity_depth_volume"]["status"] == "blocked"
        assert "issuer_nav_alignment" in tbill["missing_or_blocked_gates"]

    def test_public_rwa_endpoints_use_file_specific_report_overrides(
        self,
        test_client,
        tmp_path,
        monkeypatch,
    ):
        from src import rwa_feed_discovery

        route_override = tmp_path / "jupiter-override.json"
        route_override.write_text(
            json.dumps(
                {
                    "routes": [
                        {
                            "allowlist_id": "dex:jupiter_router:AAPLX:USD",
                            "symbol": "AAPLx/USD",
                            "venue": "jupiter_router",
                            "status": "error",
                            "error": "override-consistency-sentinel",
                        }
                    ]
                }
            ),
            encoding="utf-8",
        )
        evm_override = tmp_path / "evm-override.json"
        evm_override.write_text(
            json.dumps(
                {
                    "summary": {
                        "pool_count": 73,
                        "missing_pair_count": 72,
                        "block_state_captured": 1,
                    },
                    "pools": [],
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv(
            "RWA_JUPITER_ROUTE_ALLOWLIST_PATH",
            str(route_override),
        )
        monkeypatch.setenv("RWA_EVM_POOL_ALLOWLIST_PATH", str(evm_override))
        monkeypatch.setattr(
            rwa_feed_discovery,
            "_load_replay_inventory_evidence",
            lambda _reports_dir: {},
        )

        discovery = test_client.get(
            "/v1/rwa/discovery?venue=jupiter_router&limit=100"
        )
        blocker = test_client.get("/v1/rwa/blocker-resolution")

        assert discovery.status_code == 200
        aapl = next(
            row for row in discovery.json()["feeds"] if row["symbol"] == "AAPLx/USD"
        )
        route_gate = aapl["gates"]["route_or_pool_discovery"]
        assert route_gate["status"] == "blocked"
        assert route_gate["evidence"]["error"] == "override-consistency-sentinel"

        assert blocker.status_code == 200
        issues = {row["issue_id"]: row for row in blocker.json()["rows"]}
        evm_evidence = issues["evm_pool_allowlist_and_rpc_state"]["evidence"]
        assert evm_evidence["public_pair_search_pool_count"] == 73
        assert evm_evidence["public_pair_search_missing_pair_count"] == 72

    def test_rwa_discovery_mitigation_plan_maps_blockers_to_solutions(self, test_client):
        response = test_client.get("/v1/rwa/discovery/mitigation-plan")

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "rwa_discovery_mitigation_plan"
        assert data["summary"]["feed_count"] >= 420
        assert data["summary"]["production_promoted"] == 0
        assert data["summary"]["blocked_from_production"] == data["summary"]["feed_count"]
        assert data["summary"]["critical_open_issue_count"] >= 9
        assert data["policy"]["promotion_rule"].startswith("A feed is production-ready")
        assert {
            "Jupiter Swap API",
            "Hyperliquid Info API",
            "Uniswap v3 SDK quoting guide",
            "Pyth Hermes",
            "Chainlink Data Streams",
        }.issubset({row["source"] for row in data["research_basis"]})

        issues = {row["issue_id"]: row for row in data["issues"]}
        assert issues["liquidity_depth_volume"]["affected_feed_count"] >= 420
        assert "block-size VWAP" in issues["liquidity_depth_volume"]["target_state"]
        assert "Run a 30-minute feed-quality window" in " ".join(
            issues["freshness_cadence"]["mitigation_steps"]
        )
        assert issues["state_instrument_confirmation"]["affected_feed_count"] == 7
        assert "state_pool payload" in issues["state_instrument_confirmation"]["evidence_required"]
        assert issues["issuer_nav_alignment"]["affected_feed_count"] >= 60
        assert issues["rights_and_redistribution"]["affected_feed_count"] == 0

        playbooks = {row["venue_family"]: row for row in data["venue_playbooks"]}
        assert "jupiter_router" in playbooks
        assert "blocksize_state_reference" in playbooks
        assert "oracle_reference" in playbooks
        assert "0/7 matched" in playbooks["blocksize_state_reference"]["solution"]

        actions = {row["action_id"]: row for row in data["immediate_actions"]}
        assert actions["refresh_blocksize_state_discovery"]["status"] == "ready"
        assert "state_instrument_confirmation" in actions["refresh_blocksize_state_discovery"]["unblocks"]
        assert "run_30_minute_quality_windows" in actions

    def test_rwa_source_rights_keeps_redistribution_fail_closed(self, test_client, monkeypatch, tmp_path):
        monkeypatch.setenv("RWA_RIGHTS_CLEARANCE_PATH", str(tmp_path / "missing-rights-clearance.json"))

        response = test_client.get("/v1/rwa/source-rights")

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "rwa_source_rights_registry"
        assert data["summary"]["venue_count"] >= 14
        assert data["summary"]["feed_count"] >= 420
        assert data["summary"]["production_rights_cleared"] == 0
        assert data["summary"]["blocked_or_missing_rights"] == data["summary"]["venue_count"]
        assert data["policy"]["production_rule"].startswith("Production redistribution requires")
        rows = {row["venue"]: row for row in data["rows"]}
        assert rows["hyperliquid_rwa_spot"]["can_source_for_internal_benchmark"] is True
        assert rows["hyperliquid_rwa_spot"]["can_redistribute_production"] is False
        assert "RWA_MARKET_DATA_POLICY_ACK" in rows["hyperliquid_rwa_spot"]["missing_policy_env"]
        assert rows["treasury_nav"]["requires_license_or_contract"] is True
        assert rows["treasury_nav"]["can_source_for_internal_benchmark"] is False

    def test_rwa_source_rights_clearance_separates_legal_from_source_access(self, test_client):
        response = test_client.get("/v1/rwa/source-rights")

        assert response.status_code == 200
        data = response.json()
        assert data["summary"]["production_rights_cleared"] == data["summary"]["venue_count"]
        assert data["summary"]["blocked_or_missing_rights"] == 0
        assert data["summary"]["missing_or_blocked_source_access"] > 0
        assert data["clearance_evidence"]["rights_cleared"] is True
        rows = {row["venue"]: row for row in data["rows"]}
        assert rows["hyperliquid_rwa_spot"]["legal_rights_cleared"] is True
        assert rows["hyperliquid_rwa_spot"]["can_redistribute_production"] is True
        assert rows["treasury_nav"]["legal_rights_cleared"] is True
        assert rows["treasury_nav"]["source_access_ready"] is False

    def test_rwa_replay_inventory_maps_route_pool_identifiers(self, test_client):
        response = test_client.get("/v1/rwa/replay-inventory")

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "rwa_route_pool_replay_inventory"
        assert data["summary"]["candidate_count"] >= 60
        assert data["summary"]["route_plan_available"] >= 10
        assert data["summary"]["raw_payload_available"] >= 10
        assert data["summary"]["missing_or_incomplete_replay"] > 0
        rows = {row["allowlist_id"]: row for row in data["rows"]}
        aapl = rows["dex:jupiter_router:AAPLX:USD"]
        assert aapl["replay_status"] == "route_replay_ready_pending_liquidity_window"
        assert aapl["route_plan_available"] is True
        assert aapl["raw_payload_available"] is True
        assert aapl["pool_or_route_ids"]
        assert aapl["slot_or_block_numbers"]
        assert aapl["fee_tier_status"] == "router_route_fee_tiers_not_exposed"
        assert "continuous_30_minute_freshness_window" in aapl["promotion_blockers"]

        evm = rows["dex:uniswap_v3_v4:TBILL:USDC"]
        assert evm["replay_status"] == "missing_pool_allowlist"
        assert "pool_address" in evm["missing_replay_fields"]
        assert evm["raw_payload_available"] is False

        uniswap_paxg = rows["dex:uniswap_v3_v4:PAXG:USDC"]
        assert uniswap_paxg["replay_status"] == "pool_replay_ready_pending_live_quality"
        assert uniswap_paxg["pool_state_available"] is True
        assert "chain_id" in uniswap_paxg["replay_payload_fields"]
        assert uniswap_paxg["fee_tiers"]

        solana_pool = rows["dex:orca_whirlpool:AAPLX:USD"]
        assert solana_pool["replay_status"] == "pool_replay_ready_pending_live_quality"
        assert solana_pool["pool_state_available"] is True
        assert solana_pool["pool_or_route_ids"]
        assert solana_pool["slot_or_block_numbers"]
        assert solana_pool["raw_payload_available"] is True

    def test_rwa_blocker_resolution_separates_resolved_and_external_blockers(self, test_client):
        response = test_client.get("/v1/rwa/blocker-resolution")

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "rwa_blocker_resolution_ledger"
        assert data["summary"]["rights_blockers_remaining"] == 0
        assert data["summary"]["source_access_ready"] >= 4
        rows = {row["issue_id"]: row for row in data["rows"]}
        assert rows["rights_and_redistribution"]["status"] == "resolved"
        assert rows["solana_pool_allowlist_and_slot_state"]["status"] == "resolved_to_replay_evidence"
        assert rows["solana_pool_allowlist_and_slot_state"]["resolved_count"] >= 12
        assert rows["evm_pool_allowlist_and_rpc_state"]["status"] == "partially_resolved_to_replay_evidence"
        assert rows["evm_pool_allowlist_and_rpc_state"]["resolved_count"] >= 2
        assert rows["evm_pool_allowlist_and_rpc_state"]["evidence"]["block_state_captured"] >= 2
        assert "EVM_RPC_ETHEREUM_URL" in rows["evm_pool_allowlist_and_rpc_state"]["evidence"]["missing_env"]

    def test_rwa_equity_universes_show_sp500_and_apac_sourceability(self, test_client):
        response = test_client.get("/v1/rwa/equity-universes")

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "rwa_equity_universe_sourcing_plan"
        universes = {row["universe_id"]: row for row in data["universes"]}
        assert universes["sp500"]["universe_size"] == 503
        assert universes["sp500"]["coverage_decision"].startswith("yes_cover_whole_universe")
        assert universes["sp500"]["primary_source_venues"] == [
            "us_equity_consolidated_tape",
            "polygon_tradfi_reference",
        ]
        assert {row["kind"] for row in universes["sp500"]["feed_shapes"]} == {"bidask", "vwap"}
        assert universes["hong_kong_equities"]["primary_source_venues"] == ["hkex_licensed_equities"]
        assert universes["china_a_shares"]["primary_source_venues"] == ["china_a_share_licensed_equities"]
        assert universes["south_korea_equities"]["primary_source_venues"] == ["krx_licensed_equities"]
        assert universes["japan_equities"]["primary_source_venues"] == ["jpx_licensed_equities"]
        assert universes["taiwan_equities"]["primary_source_venues"] == ["twse_licensed_equities"]
        assert universes["india_equities"]["primary_source_venues"] == ["india_nse_bse_licensed_equities"]
        assert universes["uk_equities"]["primary_source_venues"] == ["lse_lseg_licensed_equities"]
        assert universes["europe_equities"]["primary_source_venues"] == [
            "euronext_licensed_equities",
            "deutsche_boerse_xetra_licensed_equities",
        ]
        assert universes["canada_equities"]["primary_source_venues"] == ["tsx_licensed_equities"]
        assert universes["australia_equities"]["primary_source_venues"] == ["asx_licensed_equities"]
        assert universes["singapore_equities"]["primary_source_venues"] == ["sgx_licensed_equities"]
        assert data["summary"]["asia_universe_count"] == 7
        assert data["summary"]["licensed_venue_count"] >= 12

    def test_rwa_equity_universes_can_filter_sp500(self, test_client):
        response = test_client.get("/v1/rwa/equity-universes?universe=sp500")

        assert response.status_code == 200
        data = response.json()
        assert len(data["universes"]) == 1
        assert data["universes"][0]["universe_id"] == "sp500"
        assert data["universes"][0]["current_registry_overlap"]["matched_sample_count"] > 0

    def test_rwa_market_expansion_identifies_venues_and_missing_tickers(self, test_client):
        response = test_client.get("/v1/rwa/market-expansion")

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "rwa_market_expansion_plan"
        assert data["summary"]["expanded_venue_count"] >= 15
        venue_ids = {venue["venue_id"] for venue in data["venues"]}
        assert {
            "cta_utp_sip",
            "hkex_omd",
            "sse_szse_china_connect",
            "jpx_arrowhead",
            "krx_market_data",
            "ondo_global_markets",
            "pyth_market_data",
            "evm_rwa_pools",
        }.issubset(venue_ids)
        universes = {row["universe_id"]: row for row in data["equity_universes"]}
        assert {"sp500", "hong_kong_equities", "china_a_shares", "japan_equities", "south_korea_equities"}.issubset(universes)
        assert "0700.HK" in universes["hong_kong_equities"]["sample_symbols"]
        assert any(row["symbol"] == "SPYx/USDC" for row in data["index_and_fund_targets"])

    def test_rwa_futures_data_plan_explains_derived_pricing_components(self, test_client):
        response = test_client.get("/v1/rwa/futures-data-plan")

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "rwa_futures_data_plan"
        venue_ids = {venue["venue_id"] for venue in data["venues"]}
        assert {
            "cme_equity_index_futures",
            "cme_fx_futures",
            "eurex_index_futures",
            "hkex_derivatives",
            "lme_metals",
        }.issubset(venue_ids)
        methods = {method["asset_class"]: method for method in data["pricing_methods"]}
        assert "present_value_expected_dividends" in methods["equity_index"]["formula"]
        assert "cross_currency_basis" in methods["fx"]["required_components"]
        assert "cheapest-to-deliver option" in methods["rates_and_treasuries"]["premium_or_discount_terms"]
        assert data["summary"]["futures_underlying_jobs"] >= 25

    def test_rwa_oracle_streams_count_reference_feed_universe(self, test_client):
        response = test_client.get("/v1/rwa/oracle-streams")

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "rwa_oracle_stream_coverage"
        assert data["summary"]["known_feed_entries_lower_bound"] >= 8500
        providers = {provider["provider_id"]: provider for provider in data["providers"]}
        assert providers["pyth"]["numeric_feed_count"] == 3059
        assert providers["chainlink"]["numeric_feed_count"] == 1683
        assert providers["redstone"]["numeric_feed_count"] == 801
        assert providers["dia"]["numeric_feed_count"] == 3000
        assert "equity" in providers["pyth"]["rwa_buckets"]
        assert "treasury_fund" in providers["chainlink"]["rwa_buckets"]
        assert "executable_liquidity" in providers["redstone"]["not_usable_as"]

    def test_rwa_xyz_monitor_exposes_catalog_and_source_assessment(self, test_client):
        response = test_client.get(
            "/v1/rwa/rwa-xyz-monitor",
            params={"include_token_rows": True, "row_limit": 5},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "rwa_xyz_new_asset_monitor"
        assert data["summary"]["asset_count"] >= 1000
        assert data["summary"]["token_count"] >= 3000
        assert data["summary"]["tokens_with_contract_address"] == data["summary"]["token_count"]
        assert data["source_assessment"]["direct_realtime_price_feed_available_from_monitor"] is False
        assert "rwa_xyz_new_asset_monitor" in data["report_path"]
        assert len(data["token_rows"]) == 5
        assert all(row["address"] for row in data["token_rows"])

    def test_rwa_daily_feed_agent_exposes_latest_monitor_diff(self, test_client):
        response = test_client.get("/v1/rwa/daily-feed-agent")

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "rwa_daily_feed_discovery_agent"
        assert data["summary"]["current_asset_count"] >= 1000
        assert data["summary"]["current_token_count"] >= 3000
        assert "current_unique_token_contract_count" in data["summary"]
        assert data["policy"]["catalog_boundary"].startswith("RWA.xyz additions")
        assert "new_tokens" not in data

        rows_response = test_client.get(
            "/v1/rwa/daily-feed-agent",
            params={"include_rows": True, "row_limit": 3},
        )
        assert rows_response.status_code == 200
        rows_data = rows_response.json()
        assert "new_tokens" in rows_data
        assert "sourcing_actions" in rows_data

    def test_rwa_provider_catalog_tracks_expansion_sources(self, test_client):
        response = test_client.get("/v1/rwa/provider-catalog")

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "rwa_provider_catalog_ingestion"
        assert data["summary"]["provider_count"] >= 70
        assert data["summary"]["ready_to_probe"] >= 2
        assert data["summary"]["blocked_by_auth_or_license"] > data["summary"]["ready_to_probe"]
        assert data["summary"]["by_category"]["tokenized_security"] >= 8
        assert data["summary"]["by_category"]["dex_liquidity"] >= 12
        assert data["summary"]["by_category"]["licensed_exchange"] >= 15
        providers = {provider["provider_id"]: provider for provider in data["providers"]}
        assert {
            "dinari_dshares",
            "databento",
            "pyth",
            "chainlink",
            "rwa_xyz_new_asset_monitor",
            "blackrock_buidl_securitize",
            "cme_group_futures",
            "ebs_fx",
        }.issubset(providers)
        assert providers["hyperliquid_rwa_spot"]["ingestion_status"] == "ready_to_probe"
        assert providers["databento"]["requires_license"] is True
        assert "canonical symbol and underlying identity mapped" in data["promotion_gates"]

        dex_response = test_client.get(
            "/v1/rwa/provider-catalog",
            params={"category": "dex_liquidity", "status": "planned_adapter"},
        )
        assert dex_response.status_code == 200
        dex_data = dex_response.json()
        assert dex_data["summary"]["provider_count"] > 0
        assert all(provider["category"] == "dex_liquidity" for provider in dex_data["providers"])
        assert all(provider["ingestion_status"] == "planned_adapter" for provider in dex_data["providers"])

    def test_rwa_source_readiness_tracks_missing_config_without_secret_values(
        self,
        test_client,
        monkeypatch,
    ):
        monkeypatch.setenv("JUPITER_API_KEY", "super-secret-jupiter-key")
        for env_name in [
            "RWA_SOLANA_TOKEN_MINTS_PATH",
            "SOLANA_RPC_URL",
            "PYTH_API_KEY",
            "CHAINLINK_DATA_STREAMS_API_KEY",
            "CHAINLINK_DATA_STREAMS_API_SECRET",
            "BLOCKSIZE_API_KEY",
        ]:
            monkeypatch.delenv(env_name, raising=False)

        response = test_client.get("/v1/rwa/source-readiness")

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "rwa_source_readiness"
        assert data["summary"]["dependency_count"] >= 30
        assert data["summary"]["configured"] >= 1
        assert data["summary"]["blocked_by_license_or_contract"] > 0
        assert data["summary"]["missing_identifier_mapping"] > 0
        assert data["summary"]["provider_catalog_count"] >= 70
        assert data["summary"]["dex_allowlist_candidates"] >= 45
        serialized = json.dumps(data)
        assert "super-secret-jupiter-key" not in serialized
        dependencies = {row["dependency_id"]: row for row in data["dependencies"]}
        assert dependencies["jupiter_api_key"]["status"] == "configured"
        assert dependencies["jupiter_api_key"]["configured_required_env"] == ["JUPITER_API_KEY"]
        assert dependencies["solana_token_mint_registry"]["missing_required_env"] == [
            "RWA_SOLANA_TOKEN_MINTS_PATH"
        ]
        if dependencies["solana_token_mint_registry"]["configured_artifact_paths"]:
            assert dependencies["solana_token_mint_registry"]["status"] == "configured"
        else:
            assert dependencies["solana_token_mint_registry"]["status"] == "missing_identifier_mapping"
        if dependencies["jupiter_route_allowlist"]["configured_artifact_paths"]:
            assert dependencies["jupiter_route_allowlist"]["status"] == "configured"
        assert dependencies["pyth_access"]["status"] == "blocked_by_license_or_contract"
        assert all(row["secret_safe"] is True for row in data["dependencies"])
        adapters = {row["adapter_or_source"]: row for row in data["adapter_readiness"]}
        if (
            dependencies["solana_token_mint_registry"]["status"] == "configured"
            and dependencies["jupiter_route_allowlist"]["status"] == "configured"
        ):
            assert adapters["jupiter_router"]["status"] == "ready_to_probe"
        else:
            assert adapters["jupiter_router"]["status"] == "missing_identifier_mapping"
            assert "solana_token_mint_registry" in adapters["jupiter_router"]["missing_dependency_ids"]

        filtered = test_client.get(
            "/v1/rwa/source-readiness",
            params={"category": "oracle_reference", "status": "blocked_by_license_or_contract"},
        )
        assert filtered.status_code == 200
        filtered_data = filtered.json()
        assert filtered_data["summary"]["filtered_dependency_count"] > 0
        assert all(row["category"] == "oracle_reference" for row in filtered_data["dependencies"])
        assert all(row["status"] == "blocked_by_license_or_contract" for row in filtered_data["dependencies"])

    def test_rwa_source_readiness_never_exposes_effective_override_paths(
        self,
        test_client,
        monkeypatch,
        tmp_path,
    ):
        sentinel = "operator-secret-path-sentinel-7d33d09b"
        override_path = tmp_path / sentinel / f"{sentinel}.json"
        logical_path = "reports/rwa_solana_token_mints.json"
        monkeypatch.setenv("RWA_SOLANA_TOKEN_MINTS_PATH", str(override_path))

        missing_response = test_client.get("/v1/rwa/source-readiness")

        assert missing_response.status_code == 200
        missing_data = missing_response.json()
        missing_dependency = {
            row["dependency_id"]: row for row in missing_data["dependencies"]
        }["solana_token_mint_registry"]
        assert missing_dependency["status"] == "missing_identifier_mapping"
        assert missing_dependency["configured"] is False
        assert missing_dependency["artifact_paths"] == [logical_path]
        assert missing_dependency["configured_artifact_paths"] == []
        assert missing_dependency["missing_artifact_paths"] == [logical_path]
        assert sentinel not in json.dumps(missing_data)
        assert str(override_path) not in json.dumps(missing_data)

        override_path.parent.mkdir()
        override_path.write_text("{}\n", encoding="utf-8")
        configured_response = test_client.get("/v1/rwa/source-readiness")

        assert configured_response.status_code == 200
        configured_data = configured_response.json()
        configured_dependency = {
            row["dependency_id"]: row for row in configured_data["dependencies"]
        }["solana_token_mint_registry"]
        assert configured_dependency["status"] == "configured"
        assert configured_dependency["configured"] is True
        assert configured_dependency["artifact_paths"] == [logical_path]
        assert configured_dependency["configured_artifact_paths"] == [logical_path]
        assert configured_dependency["missing_artifact_paths"] == []
        assert sentinel not in json.dumps(configured_data)
        assert str(override_path) not in json.dumps(configured_data)
        for response_data in (missing_data, configured_data):
            for dependency in response_data["dependencies"]:
                for field in (
                    "artifact_paths",
                    "configured_artifact_paths",
                    "missing_artifact_paths",
                ):
                    assert all(
                        not Path(public_path).is_absolute()
                        for public_path in dependency[field]
                    )
        legacy_dependency = {
            row["dependency_id"]: row for row in configured_data["dependencies"]
        }["rwa_xyz_monitor_catalog"]
        assert legacy_dependency["artifact_paths"] == [
            "reports/rwa_xyz_new_asset_monitor.json"
        ]

    def test_rwa_solana_discovery_targets_cover_jupiter_and_pool_symbols(self):
        from src.rwa_solana_discovery import build_solana_token_targets

        targets = {row["token_key"]: row for row in build_solana_token_targets()["targets"]}

        assert {"AAPLX", "NVDAX", "SPYX", "EURC", "USDC", "USDY"}.issubset(targets)
        assert "jupiter_router" in targets["AAPLX"]["venues"]
        assert "raydium_clmm" in targets["AAPLX"]["venues"]
        assert "AAPLx/USD" in targets["AAPLX"]["source_symbols"]

    def test_rwa_consensus_sources_groups_primary_oracle_and_futures_layers(self, test_client):
        response = test_client.get("/v1/rwa/consensus/sources")

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "rwa_consensus_source_plan"
        assert data["summary"]["sourceable_feed_count"] >= 340
        assert data["summary"]["sourceable_feed_count"] >= 420
        assert data["summary"]["needs_more_sources_assets"] == 0
        assert data["summary"]["by_consensus_status"]["sourceable_for_consensus"] >= (
            data["summary"]["sourceable_feed_count"]
        )
        assert data["summary"]["oracle_feed_entries_lower_bound"] >= 8500
        assert data["summary"]["futures_underlying_jobs"] >= 40
        assert data["summary"]["provider_catalog_count"] >= 70
        assert data["summary"]["dex_allowlist_candidates"] >= 60
        assert data["summary"]["source_readiness_dependencies"] >= 30
        assert data["summary"]["source_readiness_blocked_by_license_or_contract"] > 0
        layers = {layer["layer"]: layer for layer in data["source_layers"]}
        assert {
            "primary_market",
            "provider_catalog_ingestion",
            "source_readiness",
            "dex_route_pool_allowlist",
            "oracle_reference",
            "blocksize_state_reference",
            "futures_fair_value",
        }.issubset(layers)
        assert layers["oracle_reference"]["quality_gate"].startswith("provider catalog identity")
        assert layers["blocksize_state_reference"]["source_type"] == "blocksize_state_reference"
        assert layers["blocksize_state_reference"]["endpoint_template"] == "/v1/state/{pair}"
        assert layers["provider_catalog_ingestion"]["current_job_count"] >= 70
        assert layers["source_readiness"]["dependency_count"] >= 30
        assert layers["dex_route_pool_allowlist"]["candidate_count"] >= 45
        assets = {row["asset_id"]: row for row in data["assets"]}
        assert assets["AAPL"]["consensus_status"] == "sourceable_for_consensus"
        assert "pyth" in assets["AAPL"]["oracle_reference_providers"]
        assert "Acquire or configure Pyth Pro/Core API credentials for production RWA/API usage." in (
            data["sourcing_status"]["blocked_next_steps"]
        )

    def test_rwa_blocksize_state_methodology_exposes_supplemental_contract(self, test_client):
        response = test_client.get("/v1/rwa/blocksize-state-methodology")
        _DISCOVERY_RATE_LIMITER.clear()

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "rwa_blocksize_state_methodology"
        methodology = data["methodology"]
        assert methodology["source_type"] == "blocksize_state_reference"
        assert methodology["endpoint_template"] == "/v1/state/{pair}"
        assert methodology["source_contract"]["role"] == "supplemental_reference"
        assert methodology["source_contract"]["not_executable_liquidity"] is True
        assert data["usage"]["benchmark"]["endpoint"] == "/v1/rwa/benchmark/blocksize"

    def test_rwa_registry_resolves_symbol_naming_conventions(self, test_client):
        for symbol in ["AAPL", "AAPL/USD", "AAPLx/USD", "AAPLUSD"]:
            response = test_client.get("/v1/rwa/resolve", params={"symbol": symbol})

            assert response.status_code == 200
            data = response.json()
            assert data["product"] == "rwa_symbol_resolution"
            assert data["match_count"] == 1
            assert data["matches"][0]["asset_id"] == "AAPL"
            assert {"ostium", "gains", "jupiter_router", "meteora_dlmm"}.issubset(
                set(data["matches"][0]["venues"])
            )

    def test_rwa_registry_resolves_venue_alias_and_filters_coverage(self, test_client):
        response = test_client.get(
            "/v1/rwa/registry",
            params={"symbol": "AAPLxUSD", "venue": "Meteora DLMM", "include_aliases": True},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "rwa_canonical_registry"
        assert data["resolution"]["query"]["resolved_venue"] == "meteora_dlmm"
        assert data["summary"]["returned_assets"] == 1
        asset = data["assets"][0]
        assert asset["asset_id"] == "AAPL"
        assert "AAPLUSD" in asset["aliases"]
        assert set(asset["venues"]) == {"meteora_dlmm"}

    def test_rwa_registry_venues_explains_venue_coverage(self, test_client):
        response = test_client.get(
            "/v1/rwa/registry/venues",
            params={"venue": "uniswap v3", "include_aliases": True},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "rwa_venue_coverage_registry"
        assert data["summary"]["returned_venues"] == 1
        venue = data["venues"][0]
        assert venue["venue_id"] == "uniswap_v3_v4"
        assert "UNISWAPV3" in venue["aliases"]
        assert "onchain_clmm_pool" in venue["source_types"]
        assert {"PAXG", "OUSG", "USDY", "BUIDL"}.issubset(
            {asset["asset_id"] for asset in venue["assets"]}
        )

    def test_rwa_oracle_parity_exposes_pyth_chainlink_gaps(self, test_client):
        response = test_client.get("/v1/rwa/oracle-parity")

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "rwa_oracle_parity_matrix"
        assert data["oracle_sources"]["pyth"]["source_url"].startswith("https://www.pyth.network")
        assert data["oracle_sources"]["chainlink"]["source_url"].startswith("https://data.chain.link")
        categories = {category["category"]: category for category in data["categories"]}
        assert categories["equity"]["partial"] >= 1
        assert categories["rates"]["missing"] == 3
        assert categories["macro"]["missing"] == 3
        assert categories["proof_of_reserve"]["missing"] == 3
        assert any(item["priority"] == "P0" for item in data["sourcing_backlog"])

    def test_rwa_sourcing_jobs_classifies_probe_and_auth_work(self, test_client):
        response = test_client.get("/v1/rwa/sourcing/jobs")

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "rwa_sourcing_jobs"
        assert data["summary"]["job_count"] > 0
        assert data["summary"]["ready_to_probe"] > 0
        assert data["summary"]["blocked_by_auth_or_license"] > 0
        assert data["pagination"]["returned"] <= 50
        assert data["pagination"]["total"] == data["summary"]["matching_job_count"]
        assert data["summary"]["by_venue"]["pyth_oracle_reference"] > 0
        assert data["summary"]["by_venue"]["chainlink_oracle_reference"] > 0
        assert data["summary"]["by_venue"]["jupiter_router"] > 0
        assert data["summary"]["by_venue"]["meteora_dlmm"] > 0

        kraken_response = test_client.get(
            "/v1/rwa/sourcing/jobs?venue=kraken_xstocks&limit=100"
        )
        kraken_jobs = kraken_response.json()["jobs"]
        assert any(job["status"] == "ready_to_probe" for job in kraken_jobs)
        assert any("Kraken public REST/WS" in job["endpoint_hint"] for job in kraken_jobs)

        hyperliquid_spot_response = test_client.get(
            "/v1/rwa/sourcing/jobs?venue=hyperliquid_rwa_spot&limit=100"
        )
        hyperliquid_spot_jobs = hyperliquid_spot_response.json()["jobs"]
        assert len(hyperliquid_spot_jobs) == 31
        assert any(
            job["symbol"] == "AAPL/USDC"
            and job["status"] == "ready_to_probe"
            and job["metadata"]["hyperliquid_coin"] == "@268"
            for job in hyperliquid_spot_jobs
        )
        assert any(
            job["symbol"] == "DIME/USDC"
            and job["target_status"] == "unverified_identity_hold"
            for job in hyperliquid_spot_jobs
        )
        rwa_xyz_response = test_client.get(
            "/v1/rwa/sourcing/jobs?venue=rwa_xyz_new_asset_monitor&limit=100"
        )
        rwa_xyz_data = rwa_xyz_response.json()
        rwa_xyz_jobs = rwa_xyz_data["jobs"]
        assert rwa_xyz_data["summary"]["matching_job_count"] >= 3000
        assert any(
            job["metadata"]["address"]
            and "pool_or_route_liquidity" in job["missing_source_types"]
            for job in rwa_xyz_jobs
        )

        covered_response = test_client.get(
            "/v1/rwa/sourcing/jobs"
            "?include_completed_targets=true&venue=jupiter_router&limit=100"
        )
        covered_jobs = covered_response.json()["jobs"]
        assert any(
            job["asset_id"] == "AAPL" and job["symbol"] == "AAPLx/USD"
            for job in covered_jobs
        )

        hyperliquid_response = test_client.get(
            "/v1/rwa/sourcing/jobs"
            "?include_completed_targets=true&venue=hyperliquid_paxg&limit=100"
        )
        hyperliquid_jobs = hyperliquid_response.json()["jobs"]
        assert any(
            job["symbol"] == "PAXG/USD" and job["status"] == "ready_to_probe"
            for job in hyperliquid_jobs
        )
        assert not any(job["symbol"] == "XAG/USD" for job in hyperliquid_jobs)

    def test_rwa_vwap_calculator_walks_depth_for_block_size(self, test_client):
        response = test_client.post(
            "/v1/rwa/vwap/calculate",
            json={
                "symbol": "AAPL/USD",
                "venue": "kraken_xstocks",
                "asset_class": "equity",
                "source_type": "native_l2",
                "side": "buy",
                "block_size_usd": 15000,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "levels": [
                    {"price": 100, "size": 100},
                    {"price": 101, "size": 100},
                ],
            },
        )

        assert response.status_code == 200
        result = response.json()["result"]
        assert result["status"] == "full_fill"
        assert result["fillable_notional_usd"] == 15000
        assert result["vwap"] > 100
        assert result["slippage_bps"] > 0
        assert result["quality"]["score"] == 100

    def test_rwa_vwap_calculator_returns_partial_fill(self, test_client):
        response = test_client.post(
            "/v1/rwa/vwap/calculate",
            json={
                "symbol": "TSLA/USD",
                "venue": "kraken_xstocks",
                "asset_class": "equity",
                "source_type": "native_l2",
                "side": "buy",
                "block_size_usd": 25000,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "levels": [{"price": 250, "size": 40}],
            },
        )

        assert response.status_code == 200
        result = response.json()["result"]
        assert result["status"] == "partial_fill"
        assert result["fillable_notional_usd"] == 10000
        assert result["fill_ratio"] == 0.4
        assert "partial_fill" in result["quality"]["flags"]

    def test_rwa_bidask_calculator_flags_wide_spread(self, test_client):
        response = test_client.post(
            "/v1/rwa/bidask/calculate",
            json={
                "symbol": "NVDA/USD",
                "venue": "ostium",
                "asset_class": "equity",
                "source_type": "synthetic_l1",
                "bid": 100,
                "ask": 102,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            },
        )

        assert response.status_code == 200
        result = response.json()["result"]
        assert result["spread_bps"] > 75
        assert "wide_spread" in result["quality"]["flags"]
        assert result["quality"]["score"] < 100

    def test_rwa_quality_check_excludes_mad_outlier(self, test_client):
        now = datetime.now(timezone.utc).isoformat()
        response = test_client.post(
            "/v1/rwa/quality/check",
            json={
                "symbol": "AAPL/USD",
                "asset_class": "equity",
                "benchmark_price": 100,
                "observations": [
                    {"venue": "kraken_xstocks", "value": 100.0, "source_type": "native_l2", "timestamp": now},
                    {"venue": "ostium", "value": 100.2, "source_type": "synthetic_depth", "timestamp": now},
                    {"venue": "gains", "value": 99.9, "source_type": "price_stream_no_book", "timestamp": now},
                    {"venue": "bad_venue", "value": 120.0, "source_type": "unknown", "timestamp": now},
                ],
            },
        )

        assert response.status_code == 200
        result = response.json()["result"]
        excluded = [row for row in result["observations"] if not row["include_in_consolidated"]]
        assert result["excluded_count"] == 1
        assert excluded[0]["venue"] == "bad_venue"
        assert "mad_outlier" in excluded[0]["quality"]["flags"]

    def test_rwa_feeds_lists_adapter_registry_and_todos(self, test_client):
        response = test_client.get("/v1/rwa/feeds")

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "rwa_feed_registry"
        assert data["adapter_count"] >= 10
        adapters = {adapter["venue_id"]: adapter for adapter in data["adapters"]}
        assert adapters["kraken_xstocks"]["implementation"].endswith("KrakenXStocksAdapter")
        assert adapters["kraken_xstocks"]["supports_l2_vwap"] is True
        assert adapters["ondo_stocks"]["supports_bidask"] is True
        assert adapters["polygon_tradfi_reference"]["requires_auth"] is True
        assert adapters["pyth_oracle_reference"]["requires_auth"] is True
        assert adapters["chainlink_oracle_reference"]["requires_auth"] is True
        todos = {todo["id"]: todo for todo in data["todos"]}
        assert todos["adapter-contract"]["status"] == "complete"
        assert todos["ostium-adapter"]["status"] == "planned"
        assert todos["ondo-stocks-adapter"]["status"] == "planned"
        assert todos["pyth-oracle-reference"]["status"] == "planned"
        assert todos["chainlink-oracle-reference"]["status"] == "planned"
        assert "add_new_feed_steps" in data["operating_model"]

    def test_rwa_aggregate_consolidates_quality_gated_observations(self, test_client):
        now = datetime.now(timezone.utc).isoformat()
        response = test_client.post(
            "/v1/rwa/aggregate",
            json={
                "symbol": "AAPL/USD",
                "asset_class": "equity",
                "benchmark_price": 100,
                "block_size_usd": 10000,
                "bidask": [
                    {
                        "venue": "kraken_xstocks",
                        "source_type": "native_l1",
                        "bid": 99.95,
                        "ask": 100.05,
                        "timestamp": now,
                    },
                    {
                        "venue": "bad_venue",
                        "source_type": "unknown",
                        "bid": 119,
                        "ask": 121,
                        "timestamp": now,
                    },
                ],
                "order_books": [
                    {
                        "venue": "kraken_xstocks",
                        "source_type": "native_l2",
                        "timestamp": now,
                        "levels": [
                            {"price": 100, "size": 50},
                            {"price": 100.1, "size": 100},
                        ],
                    }
                ],
            },
        )

        assert response.status_code == 200
        result = response.json()["result"]
        assert result["consolidated"]["total_observations"] == 3
        assert result["consolidated"]["included_observations"] == 2
        assert result["consolidated"]["value"] is not None
        assert result["block_vwaps"][0]["status"] == "full_fill"

    def test_rwa_consensus_calculate_weights_supplemental_sources_and_excludes_outlier(self, test_client):
        now = datetime.now(timezone.utc).isoformat()
        response = test_client.post(
            "/v1/rwa/consensus/calculate",
            json={
                "symbol": "AAPL/USD",
                "asset_class": "equity",
                "benchmark_price": 100,
                "our_venue": "blocksize_aggregator",
                "observations": [
                    {
                        "venue": "blocksize_aggregator",
                        "source_type": "native_l2",
                        "value": 100.0,
                        "timestamp": now,
                        "quality": {"score": 96},
                    },
                    {
                        "venue": "pyth_oracle_reference",
                        "provider": "pyth",
                        "source_type": "oracle_reference",
                        "value": 100.02,
                        "confidence_bps": 5,
                        "timestamp": now,
                    },
                    {
                        "venue": "chainlink_oracle_reference",
                        "provider": "chainlink",
                        "source_type": "oracle_reference",
                        "value": 99.98,
                        "timestamp": now,
                    },
                    {
                        "venue": "bad_source",
                        "source_type": "native_l2",
                        "value": 120.0,
                        "timestamp": now,
                    },
                ],
            },
        )
        _DISCOVERY_RATE_LIMITER.clear()

        assert response.status_code == 200
        result = response.json()["result"]
        assert result["consensus"]["included_observations"] == 3
        assert result["consensus"]["independent_source_count"] == 3
        assert result["consensus"]["decision"] in {"production_candidate", "supplemental_consensus"}
        assert result["consensus"]["reliability_score"] >= 80
        assert result["our_feed_alignment"]["venue"] == "blocksize_aggregator"
        excluded = [row for row in result["observations"] if not row["include_in_consensus"]]
        assert excluded[0]["venue"] == "bad_source"
        assert "benchmark_drift_exclude" in excluded[0]["flags"]
        included_families = result["source_summary"]["by_family"]
        assert included_families["exchange_book"] == 1
        assert included_families["oracle_reference"] == 2

    def test_rwa_consensus_excludes_stale_and_unadjusted_derivative_sources(self, test_client):
        now = datetime.now(timezone.utc)
        response = test_client.post(
            "/v1/rwa/consensus/calculate",
            json={
                "symbol": "EUR/USD",
                "asset_class": "fx",
                "now": now.isoformat(),
                "observations": [
                    {
                        "venue": "pyth_oracle_reference",
                        "provider": "pyth",
                        "source_type": "oracle_reference",
                        "value": 1.1,
                        "timestamp": (now - timedelta(seconds=45)).isoformat(),
                    },
                    {
                        "venue": "gains",
                        "source_type": "price_stream_no_book",
                        "value": 1.1002,
                        "timestamp": now.isoformat(),
                    },
                ],
            },
        )

        assert response.status_code == 200
        result = response.json()["result"]
        # The fresh Gains row is still derivative-derived market data. Without a
        # spot anchor or an explicit basis adjustment it must not enter spot/FX
        # consensus merely because its timestamp is current.
        assert result["consensus"]["included_observations"] == 0
        assert result["consensus"]["decision"] == "insufficient_independent_sources"
        stale = [row for row in result["observations"] if row["venue"] == "pyth_oracle_reference"][0]
        assert "stale" in stale["flags"]
        assert stale["include_in_consensus"] is False
        derivative = [row for row in result["observations"] if row["venue"] == "gains"][0]
        assert "raw_perp_not_spot" in derivative["flags"]
        assert derivative["include_in_consensus"] is False

    def test_rwa_consensus_calculate_labels_blocksize_state_as_supplemental(self, test_client):
        now = datetime.now(timezone.utc).isoformat()
        response = test_client.post(
            "/v1/rwa/consensus/calculate",
            json={
                "symbol": "MSOLUSD",
                "asset_class": "crypto_state",
                "observations": [
                    {
                        "venue": "blocksize_aggregator",
                        "source_type": "native_l2",
                        "value": 215.0,
                        "timestamp": now,
                    },
                    {
                        "venue": "kraken_xstocks",
                        "source_type": "native_l2",
                        "value": 215.05,
                        "timestamp": now,
                    },
                    {
                        "venue": "blocksize_state",
                        "provider": "blocksize",
                        "source_type": "blocksize_state_reference",
                        "value": 215.02,
                        "timestamp": now,
                    },
                ],
            },
        )

        assert response.status_code == 200
        result = response.json()["result"]
        assert result["consensus"]["included_observations"] == 3
        assert result["source_summary"]["by_source_type"]["blocksize_state_reference"] == 1
        state_row = [row for row in result["observations"] if row["venue"] == "blocksize_state"][0]
        assert state_row["source_family"] == "benchmark_reference"
        assert "supplemental_reference" in state_row["flags"]

    def test_rwa_blocksize_benchmark_uses_live_blocksize_shape_with_credits(self, test_client, tmp_path):
        mock_client = AsyncMock()
        mock_client.get_bidask_snapshot = AsyncMock(
            return_value=BidAskData(
                pair="AAPL",
                bid=99.9,
                ask=100.1,
                spread=0.2,
                spread_pct=0.2,
                timestamp=datetime.now(timezone.utc),
                mid=100.0,
            )
        )
        app.state.blocksize = mock_client
        app.state.credits = CreditManager(str(tmp_path / "credits.db"))

        response = test_client.post(
            "/v1/rwa/benchmark/blocksize",
            headers={"X-AGENT-ID": "agent-rwa-benchmark-12345678"},
            json={
                "observations": [
                    {
                        "symbol": "AAPL/USD",
                        "asset_class": "equity",
                        "venue": "kraken_xstocks",
                        "source_type": "native_l2",
                        "value": 100.2,
                    }
                ]
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "rwa_blocksize_benchmark"
        assert data["credit_cost"] == 10.0
        assert data["summary"]["decision"] == "pass"
        benchmark = data["benchmarks"][0]
        assert benchmark["resolved_benchmark"] == {"service": "bidask", "symbol": "AAPL"}
        assert benchmark["basis_bps"] == pytest.approx(20.0)
        assert "stored_observations" not in data
        assert data["meta"]["credits"]["credits_remaining"] == 40.0
        mock_client.get_bidask_snapshot.assert_awaited_once_with("AAPL")

    def test_rwa_blocksize_benchmark_supports_blocksize_state_reference(self, test_client, tmp_path):
        mock_client = AsyncMock()
        mock_client.get_state_price = AsyncMock(
            return_value=StatePriceData(
                pair="MSOLUSD",
                price=215.0,
                timestamp=datetime.now(timezone.utc),
                source="blocksize",
            )
        )
        app.state.blocksize = mock_client
        app.state.credits = CreditManager(str(tmp_path / "credits.db"))
        old_stream_cache = getattr(app.state, "stream_cache", None)
        app.state.stream_cache = None
        try:
            response = test_client.post(
                "/v1/rwa/benchmark/blocksize",
                headers={"X-AGENT-ID": "agent-rwa-state-benchmark-12345678"},
                json={
                    "observations": [
                        {
                            "symbol": "MSOLUSD",
                            "asset_class": "crypto_state",
                            "venue": "blocksize_state",
                            "source_type": "blocksize_state_reference",
                            "benchmark_service": "state",
                            "value": 215.5,
                        }
                    ]
                },
            )
        finally:
            app.state.stream_cache = old_stream_cache
            _DISCOVERY_RATE_LIMITER.clear()

        assert response.status_code == 200
        data = response.json()
        assert data["summary"]["decision"] == "pass"
        benchmark = data["benchmarks"][0]
        assert benchmark["resolved_benchmark"] == {"service": "state", "symbol": "MSOLUSD"}
        assert benchmark["benchmark"]["endpoint"] == "/v1/state/MSOLUSD"
        assert benchmark["benchmark"]["service"] == "state"
        assert benchmark["basis_bps"] == pytest.approx(23.255814, rel=1e-6)
        mock_client.get_state_price.assert_awaited_once_with("MSOLUSD")

    def test_rwa_blocksize_benchmark_marks_wide_drift_exclude(self, test_client, tmp_path):
        mock_client = AsyncMock()
        mock_client.get_bidask_snapshot = AsyncMock(
            return_value=BidAskData(
                pair="AAPL",
                bid=99.95,
                ask=100.05,
                spread=0.1,
                spread_pct=0.1,
                timestamp=datetime.now(timezone.utc),
                mid=100.0,
            )
        )
        app.state.blocksize = mock_client
        app.state.credits = CreditManager(str(tmp_path / "credits.db"))

        response = test_client.post(
            "/v1/rwa/benchmark/blocksize",
            headers={"X-AGENT-ID": "agent-rwa-benchmark-87654321"},
            json={
                "observations": [
                    {
                        "symbol": "AAPLx/USD",
                        "asset_class": "equity",
                        "venue": "bad_source",
                        "source_type": "native_l2",
                        "value": 103.0,
                    }
                ]
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["summary"]["decision"] == "exclude"
        assert data["benchmarks"][0]["decision"] == "exclude"
        assert data["benchmarks"][0]["resolved_benchmark"]["symbol"] == "AAPL"

    def test_rwa_feed_promotion_check_promotes_only_when_all_gates_pass(self, test_client):
        response = test_client.post(
            "/v1/rwa/feeds/promotion-check",
            json={
                "venue": "kraken_xstocks",
                "current_tier": "implemented_unprobed",
                "target_tier": "supplemental",
                "backtest_days": 10,
                "uptime_pct": 99.0,
                "observation_count": 2500,
                "excluded_observation_pct": 3.5,
                "median_abs_benchmark_drift_bps": 12,
                "legal_approved": True,
                "source_type_locked": True,
                "replayable_receipts": True,
            },
        )

        assert response.status_code == 200
        result = response.json()["result"]
        assert result["decision"] == "promote"
        assert result["failed_checks"] == []

    def test_rwa_feed_promotion_check_holds_on_failed_gates(self, test_client):
        response = test_client.post(
            "/v1/rwa/feeds/promotion-check",
            json={
                "venue": "ostium",
                "target_tier": "replacement_candidate",
                "backtest_days": 10,
                "uptime_pct": 97.0,
                "observation_count": 500,
                "excluded_observation_pct": 15,
                "median_abs_benchmark_drift_bps": 60,
                "legal_approved": False,
                "source_type_locked": True,
                "replayable_receipts": False,
            },
        )

        assert response.status_code == 200
        result = response.json()["result"]
        assert result["decision"] == "hold"
        assert "legal_approved" in result["failed_checks"]
        assert "replayable_receipts" in result["failed_checks"]

    def test_rwa_realtime_requirements_expose_venue_cadence(self, test_client):
        response = test_client.get("/v1/rwa/realtime/requirements")

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "rwa_realtime_quality_requirements"
        assert data["venue_profiles"]["gains"]["target_tick_ms"] == 25
        assert data["venue_profiles"]["kraken_xstocks"]["max_age_ms"] == 10000
        assert data["venue_profiles"]["ondo_stocks"]["mode"] == "api_keyed_quote_stream"
        assert data["venue_profiles"]["jupiter_router"]["mode"] == "dex_quote_router"
        assert data["venue_profiles"]["meteora_dlmm"]["mode"] == "onchain_pool_state"
        assert data["venue_profiles"]["uniswap_v3_v4"]["mode"] == "indexed_pool_plus_rpc"
        assert data["venue_profiles"]["us_equity_consolidated_tape"]["mode"] == "licensed_consolidated_equity_feed"
        assert data["venue_profiles"]["jpx_licensed_equities"]["mode"] == "licensed_exchange_feed"
        assert data["venue_profiles"]["sgx_licensed_equities"]["max_age_ms"] == 10000
        assert data["venue_profiles"]["pyth_oracle_reference"]["mode"] == "oracle_reference"
        assert data["venue_profiles"]["chainlink_oracle_reference"]["mode"] == "oracle_reference"
        assert data["venue_profiles"]["treasury_nav"]["mode"] == "nav_reference"
        assert data["venue_profiles"]["hyperliquid_rwa_spot"]["mode"] == "event_driven_spot_book"

    def test_rwa_realtime_quality_marks_live_observation_usable(self, test_client):
        now = datetime.now(timezone.utc)
        response = test_client.post(
            "/v1/rwa/realtime/quality",
            json={
                "now": now.isoformat(),
                "observations": [
                    {
                        "symbol": "AAPL/USD",
                        "venue": "kraken_xstocks",
                        "asset_class": "equity",
                        "source_type": "native_l2",
                        "timestamp": (now - timedelta(milliseconds=200)).isoformat(),
                        "previous_timestamp": (now - timedelta(milliseconds=800)).isoformat(),
                    }
                ],
            },
        )

        assert response.status_code == 200
        result = response.json()["result"]
        assert result["aggregate_status"] == "live"
        assert result["observations"][0]["usable_for_realtime"] is True

    def test_rwa_realtime_quality_rejects_stale_and_cadence_broken_rows(self, test_client):
        now = datetime.now(timezone.utc)
        response = test_client.post(
            "/v1/rwa/realtime/quality",
            json={
                "now": now.isoformat(),
                "observations": [
                    {
                        "symbol": "NVDA/USD",
                        "venue": "kraken_xstocks",
                        "asset_class": "equity",
                        "source_type": "native_l2",
                        "timestamp": (now - timedelta(seconds=30)).isoformat(),
                        "previous_timestamp": (now - timedelta(seconds=31)).isoformat(),
                    },
                    {
                        "symbol": "EUR/USD",
                        "venue": "gains",
                        "asset_class": "fx",
                        "source_type": "price_stream_no_book",
                        "timestamp": (now - timedelta(milliseconds=100)).isoformat(),
                        "tick_interval_ms": 2500,
                    },
                ],
            },
        )

        assert response.status_code == 200
        result = response.json()["result"]
        assert result["aggregate_status"] == "not_realtime_usable"
        flags = {row["venue"]: row["quality"]["flags"] for row in result["observations"]}
        assert "stale" in flags["kraken_xstocks"]
        assert "tick_gap_exceeded" in flags["gains"]

    def test_rwa_realtime_quality_blocks_nav_reference_as_tick_by_tick(self, test_client):
        now = datetime.now(timezone.utc)
        response = test_client.post(
            "/v1/rwa/realtime/quality",
            json={
                "now": now.isoformat(),
                "observations": [
                    {
                        "symbol": "OUSG",
                        "venue": "treasury_nav",
                        "asset_class": "treasury",
                        "source_type": "nav_reference",
                        "timestamp": now.isoformat(),
                        "tick_interval_ms": 60000,
                    }
                ],
            },
        )

        assert response.status_code == 200
        row = response.json()["result"]["observations"][0]
        assert row["usable_for_realtime"] is False
        assert "reference_mode_not_tick_by_tick" in row["quality"]["flags"]

    def test_rwa_observation_store_persists_hashes_and_filters(self, test_client, tmp_path):
        now = datetime.now(timezone.utc).isoformat()
        app.state.rwa_store = RWAObservationStore(str(tmp_path / "rwa_observations.db"))
        response = test_client.post(
            "/v1/rwa/observations/store",
            headers={"Authorization": f"Bearer {RWA_TEST_OPERATOR_TOKEN}"},
            json={
                "raw_payload": {
                    "symbol": "EUR/USD",
                    "venue": "gains",
                    "value": 1.14,
                    "timestamp": now,
                },
                "normalized_observation": {
                    "symbol": "EUR/USD",
                    "venue": "gains",
                    "asset_class": "fx",
                    "source_type": "price_stream_no_book",
                    "value": 1.14,
                    "timestamp": now,
                },
                "realtime_quality": {"aggregate_status": "live"},
                "blocksize_benchmark": {"decision": "pass", "basis_bps": 2.1},
                "promotion": {"decision": "hold"},
                "metadata": {"job_id": "fx:EUR:gains"},
            },
        )

        assert response.status_code == 200
        record = response.json()["record"]
        assert record["observation_id"].startswith("rwaobs_")
        assert record["symbol"] == "EUR/USD"
        assert record["raw_payload_hash"].startswith("sha256:")

        ledger = test_client.get(
            "/v1/rwa/observations",
            params={"symbol": "EUR/USD"},
            headers={"X-RWA-Operator-Token": RWA_TEST_OPERATOR_TOKEN},
        ).json()
        assert ledger["observations"][0]["venue"] == "gains"
        assert ledger["observations"][0]["blocksize_benchmark"]["decision"] == "pass"

        summary = test_client.get("/v1/rwa/observations/summary").json()["summary"]
        assert summary["total_observations"] == 1
        assert summary["by_venue"]["gains"] == 1

    def test_rwa_sourcing_probe_runs_ready_job_and_persists(self, test_client, tmp_path):
        class FakeReadyAdapter:
            venue_id = "kraken_xstocks"

            def metadata(self):
                return {"venue_id": self.venue_id}

            async def fetch_bidask(self, symbol: str):
                return {
                    "symbol": "AMZN/USD",
                    "venue": self.venue_id,
                    "asset_class": "equity",
                    "source_type": "native_l1",
                    "bid": 100.5,
                    "ask": 101.0,
                }

            async def fetch_order_book(self, symbol: str, *, side: str = "buy", depth: int = 100):
                return {
                    "symbol": "AMZN/USD",
                    "venue": self.venue_id,
                    "asset_class": "equity",
                    "source_type": "native_l2",
                    "side": side,
                    "levels": [{"price": 101.0, "size": 50.0}],
                }

        registry = RWAAdapterRegistry()
        registry.register(FakeReadyAdapter())
        app.state.rwa_adapter_registry = registry
        app.state.rwa_store = RWAObservationStore(str(tmp_path / "rwa_observations.db"))

        response = test_client.post(
            "/v1/rwa/sourcing/probe",
            headers={"Authorization": f"Bearer {RWA_TEST_OPERATOR_TOKEN}"},
            json={
                "symbols": ["AMZN/USD"],
                "limit": 1,
                "include_order_book": True,
                "block_size_usd": 1000,
                "persist": True,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "rwa_sourcing_probe"
        assert data["summary"]["jobs_succeeded"] == 1
        assert data["summary"]["observations"] == 2
        assert data["summary"]["persisted"] == 2
        assert data["results"][0]["block_vwap"]["status"] == "full_fill"
        assert data["quality"]["aggregate_status"] == "live"

    def test_rwa_sourcing_probe_runs_hyperliquid_paxg_ready_job(self, test_client):
        now = datetime.now(timezone.utc).isoformat()

        class FakeHyperliquidAdapter:
            venue_id = "hyperliquid_paxg"

            def metadata(self):
                return {"venue_id": self.venue_id}

            async def fetch_bidask(self, symbol: str):
                return {
                    "symbol": "PAXG/USD",
                    "venue": self.venue_id,
                    "asset_class": "metal",
                    "source_type": "native_l1",
                    "bid": 4116.6,
                    "ask": 4116.7,
                    "timestamp": now,
                }

            async def fetch_order_book(self, symbol: str, *, side: str = "buy", depth: int = 100):
                return {
                    "symbol": "PAXG/USD",
                    "venue": self.venue_id,
                    "asset_class": "metal",
                    "source_type": "native_l2",
                    "side": side,
                    "timestamp": now,
                    "levels": [{"price": 4116.7, "size": 1.0}],
                }

        registry = RWAAdapterRegistry()
        registry.register(FakeHyperliquidAdapter())
        app.state.rwa_adapter_registry = registry

        response = test_client.post(
            "/v1/rwa/sourcing/probe",
            headers={"Authorization": f"Bearer {RWA_TEST_OPERATOR_TOKEN}"},
            json={
                "venues": ["hyperliquid_paxg"],
                "symbols": ["PAXG/USD"],
                "include_completed_targets": True,
                "limit": 1,
                "include_order_book": True,
                "block_size_usd": 1000,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["summary"]["jobs_succeeded"] == 1
        assert data["results"][0]["job"]["symbol"] == "PAXG/USD"
        assert data["results"][0]["bidask"]["venue"] == "hyperliquid_paxg"
        assert data["results"][0]["block_vwap"]["status"] == "full_fill"

    def test_rwa_sourcing_probe_runs_hyperliquid_spot_ready_job_by_alias(self, test_client):
        now = datetime.now(timezone.utc).isoformat()

        class FakeHyperliquidSpotAdapter:
            venue_id = "hyperliquid_rwa_spot"

            def metadata(self):
                return {"venue_id": self.venue_id}

            async def fetch_bidask(self, symbol: str):
                assert symbol == "AAPL/USDC"
                return {
                    "symbol": "AAPL/USDC",
                    "venue": self.venue_id,
                    "asset_class": "equity",
                    "source_type": "native_l1",
                    "bid": 314.4,
                    "ask": 316.6,
                    "timestamp": now,
                }

            async def fetch_order_book(self, symbol: str, *, side: str = "buy", depth: int = 100):
                assert symbol == "AAPL/USDC"
                return {
                    "symbol": "AAPL/USDC",
                    "venue": self.venue_id,
                    "asset_class": "equity",
                    "source_type": "native_l2",
                    "side": side,
                    "timestamp": now,
                    "levels": [{"price": 316.6, "size": 100.0}],
                }

        registry = RWAAdapterRegistry()
        registry.register(FakeHyperliquidSpotAdapter())
        app.state.rwa_adapter_registry = registry

        response = test_client.post(
            "/v1/rwa/sourcing/probe",
            headers={"Authorization": f"Bearer {RWA_TEST_OPERATOR_TOKEN}"},
            json={
                "venues": ["hyperliquid_rwa_spot"],
                "symbols": ["AAPL/USD"],
                "limit": 1,
                "include_order_book": True,
                "block_size_usd": 1000,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["summary"]["jobs_succeeded"] == 1
        assert data["results"][0]["job"]["symbol"] == "AAPL/USDC"
        assert data["results"][0]["bidask"]["venue"] == "hyperliquid_rwa_spot"
        assert data["results"][0]["block_vwap"]["status"] == "full_fill"

    @pytest.mark.asyncio
    async def test_kraken_xstocks_adapter_normalizes_ticker_and_depth(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/0/public/AssetPairs":
                return httpx.Response(
                    200,
                    json={
                        "error": [],
                        "result": {
                            "AAPLxUSD": {
                                "altname": "AAPLxUSD",
                                "wsname": "AAPLx/USD",
                            }
                        },
                    },
                )
            if request.url.path == "/0/public/Ticker":
                assert request.url.params.get("pair") == "AAPLxUSD"
                return httpx.Response(
                    200,
                    json={
                        "error": [],
                        "result": {
                            "AAPLxUSD": {
                                "a": ["101.00", "10", "10"],
                                "b": ["100.50", "12", "12"],
                            }
                        },
                    },
                )
            if request.url.path == "/0/public/Depth":
                assert request.url.params.get("pair") == "AAPLxUSD"
                return httpx.Response(
                    200,
                    json={
                        "error": [],
                        "result": {
                            "AAPLxUSD": {
                                "asks": [["101.00", "5", 1], ["101.50", "7", 1]],
                                "bids": [["100.50", "4", 1], ["100.00", "6", 1]],
                            }
                        },
                    },
                )
            return httpx.Response(404, json={"error": ["not found"]})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="https://api.kraken.com") as client:
            adapter = KrakenXStocksAdapter(client=client)
            bidask = await adapter.fetch_bidask("AAPL/USD")
            book = await adapter.fetch_order_book("AAPL/USD", side="buy", depth=2)

        assert bidask["symbol"] == "AAPLx/USD"
        assert bidask["bid"] == 100.5
        assert bidask["ask"] == 101.0
        assert book["source_type"] == "native_l2"
        assert book["levels"] == [
            {"price": 101.0, "size": 5.0},
            {"price": 101.5, "size": 7.0},
        ]

    @pytest.mark.asyncio
    async def test_kraken_xstocks_adapter_rejects_unlisted_public_pair(self):
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/0/public/AssetPairs":
                return httpx.Response(200, json={"error": ["EQuery:Unknown asset pair"]})
            return httpx.Response(500, json={"error": ["unexpected call"]})

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="https://api.kraken.com") as client:
            adapter = KrakenXStocksAdapter(client=client)
            with pytest.raises(ValueError, match="not listed in public AssetPairs"):
                await adapter.fetch_bidask("AAPL/USD")

    @pytest.mark.asyncio
    async def test_hyperliquid_paxg_adapter_parses_bidask_and_depth(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/info"
            payload = json.loads(request.content.decode())
            assert payload == {"type": "l2Book", "coin": "PAXG"}
            return httpx.Response(
                200,
                json={
                    "coin": "PAXG",
                    "time": 1783632167038,
                    "levels": [
                        [
                            {"px": "4116.6", "sz": "0.183", "n": 2},
                            {"px": "4116.4", "sz": "0.869", "n": 1},
                        ],
                        [
                            {"px": "4116.7", "sz": "0.083", "n": 1},
                            {"px": "4116.8", "sz": "0.083", "n": 1},
                        ],
                    ],
                },
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="https://api.hyperliquid.xyz") as client:
            adapter = HyperliquidPAXGAdapter(client=client)
            bidask = await adapter.fetch_bidask("PAXG/USD")
            buy_book = await adapter.fetch_order_book("PAXG/USD", side="buy", depth=2)
            sell_book = await adapter.fetch_order_book("PAXG/USD", side="sell", depth=2)

        assert bidask["symbol"] == "PAXG/USD"
        assert bidask["bid"] == 4116.6
        assert bidask["ask"] == 4116.7
        assert bidask["timestamp"].startswith("2026-07-09")
        assert buy_book["levels"] == [
            {"price": 4116.7, "size": 0.083},
            {"price": 4116.8, "size": 0.083},
        ]
        assert sell_book["levels"] == [
            {"price": 4116.6, "size": 0.183},
            {"price": 4116.4, "size": 0.869},
        ]

    @pytest.mark.asyncio
    async def test_hyperliquid_paxg_adapter_rejects_unsupported_metal_alias(self):
        adapter = HyperliquidPAXGAdapter(client=AsyncMock())
        with pytest.raises(ValueError, match="only PAXG"):
            await adapter.fetch_bidask("XAU/USD")

    @pytest.mark.asyncio
    async def test_hyperliquid_spot_adapter_parses_bidask_and_depth(self):
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/info"
            payload = json.loads(request.content.decode())
            if payload == {"type": "spotMetaAndAssetCtxs"}:
                return httpx.Response(
                    200,
                    json=[
                        {"universe": [{"name": "@268", "index": 0}]},
                        [
                            {
                                "coin": "@268",
                                "dayBaseVlm": "12.5",
                                "dayNtlVlm": "3950.25",
                                "markPx": "315.90",
                                "midPx": "315.58",
                                "prevDayPx": "312.00",
                            }
                        ],
                    ],
                )
            assert payload == {"type": "l2Book", "coin": "@268"}
            return httpx.Response(
                200,
                json={
                    "coin": "@268",
                    "time": 1783632167038,
                    "levels": [
                        [
                            {"px": "314.48", "sz": "7.5", "n": 2},
                            {"px": "314.20", "sz": "4.0", "n": 1},
                        ],
                        [
                            {"px": "316.68", "sz": "3.2", "n": 1},
                            {"px": "317.10", "sz": "5.0", "n": 1},
                        ],
                    ],
                },
            )

        transport = httpx.MockTransport(handler)
        async with httpx.AsyncClient(transport=transport, base_url="https://api.hyperliquid.xyz") as client:
            adapter = HyperliquidSpotRWAAdapter(client=client)
            bidask = await adapter.fetch_bidask("AAPL/USD")
            buy_book = await adapter.fetch_order_book("@268", side="buy", depth=2)
            sell_book = await adapter.fetch_order_book("AAPL/USDC", side="sell", depth=2)
            activity = await adapter.fetch_market_activity("AAPL/USDC")

        assert bidask["symbol"] == "AAPL/USDC"
        assert bidask["bid"] == 314.48
        assert bidask["ask"] == 316.68
        assert bidask["asset_class"] == "equity"
        assert bidask["metadata"]["hyperliquid_coin"] == "@268"
        assert bidask["timestamp"].startswith("2026-07-09")
        assert buy_book["levels"] == [
            {"price": 316.68, "size": 3.2},
            {"price": 317.1, "size": 5.0},
        ]
        assert sell_book["levels"] == [
            {"price": 314.48, "size": 7.5},
            {"price": 314.2, "size": 4.0},
        ]
        assert activity["window_seconds"] == 86_400
        assert activity["base_volume"] == 12.5
        assert activity["notional_volume_usd"] == 3950.25
        assert activity["metadata"]["hyperliquid_coin"] == "@268"

    @pytest.mark.asyncio
    async def test_hyperliquid_spot_adapter_rejects_unsupported_symbol(self):
        adapter = HyperliquidSpotRWAAdapter(client=AsyncMock())
        with pytest.raises(ValueError, match="not in the sourced candidate set"):
            await adapter.fetch_bidask("NVDA/USD")

    @pytest.mark.asyncio
    async def test_jupiter_router_adapter_parses_bidask_and_quote_sweep(self):
        usdc_mint = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v"
        aapl_mint = "AAPLxMint111111111111111111111111111111111"

        def quote_payload(in_amount: int, out_amount: int, *, impact: str) -> dict[str, object]:
            return {
                "inputMint": usdc_mint,
                "inAmount": str(in_amount),
                "outputMint": aapl_mint,
                "outAmount": str(out_amount),
                "otherAmountThreshold": str(int(out_amount * 0.995)),
                "swapMode": "ExactIn",
                "slippageBps": 50,
                "platformFee": None,
                "priceImpactPct": impact,
                "routePlan": [
                    {
                        "swapInfo": {
                            "ammKey": "route-1",
                            "label": "Meteora DLMM",
                            "inputMint": usdc_mint,
                            "outputMint": aapl_mint,
                            "inAmount": str(in_amount),
                            "outAmount": str(out_amount),
                        },
                        "percent": 100,
                    }
                ],
                "contextSlot": 123456,
                "timeTaken": 0.012,
            }

        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.path == "/swap/v1/quote"
            params = request.url.params
            input_mint = params.get("inputMint")
            output_mint = params.get("outputMint")
            amount = int(params.get("amount") or "0")
            assert params.get("restrictIntermediateTokens") == "true"
            assert request.headers.get("x-api-key") == "test-key"
            if input_mint == usdc_mint and output_mint == aapl_mint:
                out_by_amount = {
                    100_000_000: (50_000_000, "0.001"),
                    1_000_000_000: (500_000_000, "0.002"),
                    5_000_000_000: (2_475_000_000, "0.010"),
                    10_000_000_000: (4_900_000_000, "0.018"),
                }
                out_amount, impact = out_by_amount[amount]
                return httpx.Response(200, json=quote_payload(amount, out_amount, impact=impact))
            if input_mint == aapl_mint and output_mint == usdc_mint:
                return httpx.Response(
                    200,
                    json={
                        **quote_payload(amount, 99_500_000, impact="0.001"),
                        "inputMint": aapl_mint,
                        "outputMint": usdc_mint,
                    },
                )
            return httpx.Response(400, json={"error": "unexpected quote"})

        transport = httpx.MockTransport(handler)
        token_mints = {
            "AAPLX": {"symbol": "AAPLx", "mint": aapl_mint, "decimals": 6, "source": "test"},
        }
        async with httpx.AsyncClient(transport=transport, base_url="https://api.jup.ag") as client:
            adapter = JupiterRouterAdapter(client=client, api_key="test-key", token_mints=token_mints)
            bidask = await adapter.fetch_bidask("AAPLx/USD")
            book = await adapter.fetch_order_book("AAPLx/USD", side="buy", depth=3)

        assert adapter.metadata()["requires_auth"] is False
        assert bidask["symbol"] == "AAPLX/USDC"
        assert bidask["source_type"] == "quote_sweep"
        assert bidask["ask"] == 2.0
        assert bidask["bid"] == 1.99
        assert bidask["metadata"]["ask_quote"]["context_slot"] == 123456
        assert book["source_type"] == "quote_sweep"
        assert book["metadata"]["level_semantics"] == "marginalized_from_cumulative_exact_in_route_quotes"
        assert book["levels"][0] == {"price": 2.0, "size": 500.0}
        assert len(book["levels"]) == 3
        assert book["metadata"]["sweep_quotes"][2]["notional_usd"] == 10000.0

    @pytest.mark.asyncio
    async def test_jupiter_router_adapter_requires_token_catalog_or_api_key(self):
        adapter = JupiterRouterAdapter(client=AsyncMock())
        assert adapter.metadata()["requires_auth"] is True
        with pytest.raises(ValueError, match="Jupiter token mint is not configured"):
            await adapter.fetch_bidask("AAPLx/USD")

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

    def test_products_catalog_is_free(self, test_client):
        response = test_client.get("/v1/products")

        assert response.status_code == 200
        data = response.json()
        assert data["starter_allowance"]["eligibility"] == "authenticated_connector_only"
        assert "Signed x402" in data["starter_allowance"]["direct_public_http"]
        assert "Contact sales" in data["starter_allowance"]["upgrade_path"]
        assert data["credit_costs"]["market_brief"] == 10.0


class TestDiscoveryRateLimit:
    def test_discovery_search_is_soft_capped_by_ip(self, test_client, monkeypatch):
        monkeypatch.setattr(settings.server, "discovery_rate_limit_enabled", True)
        monkeypatch.setattr(settings.server, "discovery_rate_limit_per_minute", 2)
        monkeypatch.setattr(settings.server, "discovery_rate_limit_per_day", 100)
        _DISCOVERY_RATE_LIMITER.clear()

        mock_client = AsyncMock()
        mock_client.search_pairs_page = AsyncMock(return_value=([], 0))
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

    def test_untrusted_forwarded_for_cannot_rotate_rate_limit_identity(
        self,
        test_client,
        monkeypatch,
    ):
        monkeypatch.setattr(settings.server, "discovery_rate_limit_enabled", True)
        monkeypatch.setattr(settings.server, "discovery_rate_limit_per_minute", 1)
        monkeypatch.setattr(settings.server, "discovery_rate_limit_per_day", 100)

        mock_client = AsyncMock()
        mock_client.search_pairs_page = AsyncMock(return_value=([], 0))
        app.state.blocksize = mock_client

        first = test_client.get(
            "/v1/search?q=btc",
            headers={"X-Forwarded-For": "203.0.113.10"},
        )
        rotated = test_client.get(
            "/v1/search?q=eth",
            headers={"X-Forwarded-For": "203.0.113.99"},
        )

        assert first.status_code == 200
        assert rotated.status_code == 429


class TestTrustedCreditIdentityBoundary:
    def test_production_connector_without_oauth_is_not_ready(self, monkeypatch):
        monkeypatch.setenv("APP_ENV", "prod")
        monkeypatch.setenv("ANTHROPIC_AUTH_PROVIDER", "none")
        entitlement = {
            "ready": True,
            "blockers": [],
            "path": "/data/anthropic_entitlements.db",
        }

        status = resource_server._connector_readiness(
            "ANTHROPIC",
            type("Connector", (), {"auth": None})(),
            entitlement,
        )

        assert status["ready"] is False
        assert status["production_auth_required"] is True

    def test_production_connector_rejects_relative_entitlement_ledger(
        self,
        monkeypatch,
    ):
        monkeypatch.setenv("APP_ENV", "prod")
        monkeypatch.setenv("ANTHROPIC_ENTITLEMENT_DB_PATH", "relative.db")

        status = resource_server._connector_entitlement_readiness(
            "ANTHROPIC",
            hosted=False,
            production=True,
            initialize=False,
        )

        assert status["ready"] is False
        assert "entitlement_db_not_durable" in status["blockers"]

    def test_connector_ledger_collision_with_credit_store_fails_readiness(self, tmp_path):
        shared = str((tmp_path / "shared.db").resolve())
        statuses = {
            "ANTHROPIC": {
                "ready": True,
                "path": shared,
                "blockers": [],
            }
        }

        resource_server._mark_connector_entitlement_collisions(
            statuses,
            {
                "credit_ledger": shared,
                "observability_store": str(tmp_path / "usage.db"),
                "rwa_store": str(tmp_path / "rwa.db"),
            },
        )

        assert statuses["ANTHROPIC"]["ready"] is False
        assert statuses["ANTHROPIC"]["database_collisions"] == ["credit_ledger"]
        assert "entitlement_db_shared_with_credit_ledger" in statuses["ANTHROPIC"][
            "blockers"
        ]

    def test_unverified_identity_headers_cannot_spend_when_disabled(
        self,
        test_client,
        monkeypatch,
    ):
        monkeypatch.setattr(settings.server, "unverified_http_credits_enabled", False)

        response = test_client.get(
            "/v1/vwap/btc-usd",
            headers={"X-AGENT-ID": "attacker-selected-agent-12345678"},
        )

        assert response.status_code == 402
        challenge = response.json()
        assert challenge["starter_credits"]["identity_headers"] == []
        assert challenge["starter_credits"]["unverified_http_identity_enabled"] is False

    def test_wallet_credit_endpoints_are_hidden_when_unverified_mode_disabled(
        self,
        test_client,
        monkeypatch,
    ):
        monkeypatch.setattr(settings.server, "unverified_http_credits_enabled", False)

        balance = test_client.get("/v1/credits/balance/public-wallet-123456789")
        purchase = test_client.post("/v1/credits/purchase?tier=starter")
        claim = test_client.post("/v1/credits/claim", json={})

        assert balance.status_code == 404
        assert purchase.status_code == 404
        assert claim.status_code == 404

        openapi_paths = test_client.get("/openapi.json").json()["paths"]
        assert "/v1/credits/balance/{wallet}" not in openapi_paths
        assert "/v1/credits/purchase" not in openapi_paths
        assert "/v1/credits/claim" not in openapi_paths
        assert "bulk_discounts" not in test_client.get("/mcp/manifest.json").json()[
            "capabilities"
        ]
        assert "bulk_pricing" not in test_client.get("/health").json()

    @pytest.mark.parametrize("environment", ["prod", "production"])
    def test_legacy_wallet_credit_endpoints_stay_hidden_in_production(
        self,
        test_client,
        monkeypatch,
        environment,
    ):
        monkeypatch.setenv("APP_ENV", environment)
        monkeypatch.setattr(settings.server, "unverified_http_credits_enabled", True)

        assert test_client.get("/v1/credits/balance/public-wallet-123456789").status_code == 404
        assert test_client.post("/v1/credits/purchase?tier=starter").status_code == 404
        assert test_client.post("/v1/credits/claim", json={}).status_code == 404

    @pytest.mark.asyncio
    async def test_cancelled_starter_credit_request_is_refunded_and_reraised(
        self,
        tmp_path,
        monkeypatch,
        observability_store,
    ):
        monkeypatch.delenv("APP_ENV", raising=False)
        monkeypatch.delenv("RAILWAY_ENVIRONMENT_NAME", raising=False)
        monkeypatch.setattr(settings.server, "unverified_http_credits_enabled", True)
        manager = CreditManager(str(tmp_path / "cancelled_http_credits.db"))
        previous_manager = getattr(app.state, "credits", None)
        app.state.credits = manager
        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/v1/vwap/btc-usd",
            "raw_path": b"/v1/vwap/btc-usd",
            "query_string": b"",
            "headers": [(b"x-agent-id", b"cancelled-agent-12345678")],
            "client": ("203.0.113.50", 1234),
            "server": ("testserver", 443),
            "app": app,
        }
        request = Request(scope)

        async def cancelled(_request):
            raise asyncio.CancelledError

        try:
            with pytest.raises(asyncio.CancelledError):
                await resource_server.x402_payment_middleware(request, cancelled)
        finally:
            app.state.credits = previous_manager

        assert manager.get_balance("cancelled-agent-12345678") == 50
        events = observability_store.recent_events(limit=20)
        failure = next(event for event in events if event["event"] == "charged_delivery_failed")
        drawdown = next(event for event in events if event["event"] == "credit_drawdown_success")
        assert failure["reason"] == "request_cancelled"
        assert failure["metadata"]["charge_id"] == drawdown["metadata"]["charge_id"]


class TestObservabilityDashboard:
    def test_campaign_attribution_and_outbound_clicks_are_summarized(
        self,
        observability_store,
        test_client,
    ):
        landing = test_client.get(
            "/market-data-api-comparison"
            "?utm_source=google&utm_medium=organic&utm_campaign=market-data-comparison"
        )
        redirect = test_client.get(
            "/go/free-trial"
            "?utm_source=mcp.blocksize.info&utm_medium=organic_landing"
            "&utm_campaign=market-data-api-comparison",
            follow_redirects=False,
        )

        assert landing.status_code == 200
        assert redirect.status_code == 307
        assert redirect.headers["location"].startswith("https://matrix.blocksize.capital/")
        assert "utm_campaign=market-data-api-comparison" in redirect.headers["location"]

        stats = observability_store.summarize(days=1)
        assert stats["campaign_mix"]["market-data-comparison"] == 1
        assert stats["campaign_mix"]["market-data-api-comparison"] == 1
        assert stats["campaign_source_mix"]["google"] == 1
        assert stats["outbound_destination_mix"]["free-trial"] == 1

        redirect_event = next(
            event
            for event in stats["recent_events"]
            if event["event"] == "outbound_conversion_click"
        )
        assert "utm_campaign" in redirect_event["metadata"]
        assert "token" not in redirect_event["metadata"]

    def test_reliability_separates_protocol_responses_from_server_failures(
        self,
        observability_store,
    ):
        for status_code in (200, 402, 404, 405, 429, 500):
            observability_store.record(
                "http_request",
                surface="http_api",
                endpoint="/v1/example",
                method="GET",
                status_code=status_code,
            )
        observability_store.record(
            "credit_drawdown_success",
            metadata={"charge_id": "http-success"},
        )
        observability_store.record(
            "data_delivered",
            status_code=200,
            metadata={
                "attempt_id": "http-success",
                "charge_id": "http-success",
                "payment_mode": "starter_credit",
            },
        )
        observability_store.record(
            "credit_drawdown_success",
            metadata={"charge_id": "http-failure"},
        )
        observability_store.record(
            "charged_delivery_failed",
            status_code=500,
            metadata={"attempt_id": "http-failure", "charge_id": "http-failure"},
        )
        observability_store.record(
            "mcp_credit_drawdown_success",
            metadata={"attempt_id": "mcp-success", "charge_id": "mcp-success"},
        )
        observability_store.record(
            "mcp_data_delivered",
            metadata={"attempt_id": "mcp-success", "charge_id": "mcp-success"},
        )
        observability_store.record(
            "mcp_credit_drawdown_success",
            metadata={"attempt_id": "mcp-failure", "charge_id": "mcp-failure"},
        )
        observability_store.record(
            "mcp_tool_error",
            metadata={"attempt_id": "mcp-failure", "charge_id": "mcp-failure"},
        )

        stats = observability_store.summarize(days=1)
        reliability = stats["reliability"]

        assert reliability["http_requests"] == 6
        assert reliability["payment_required_responses"] == 1
        assert reliability["client_protocol_responses"] == 2
        assert reliability["rate_limited_responses"] == 1
        assert reliability["server_errors"] == 1
        assert reliability["server_error_rate"] == pytest.approx(1 / 6, abs=0.000001)
        assert reliability["charged_delivery_successes"] == 2
        assert reliability["charged_delivery_failures"] == 2
        assert reliability["charged_delivery_failures_last_24h"] == 2
        assert reliability["latest_charged_delivery_failure_at"]
        assert reliability["post_credit_failure_rate"] == 0.5
        assert stats["overview"]["server_error_rate"] == reliability["server_error_rate"]

    def test_reliability_keeps_historical_failure_without_marking_it_recent(self):
        old_timestamp = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
        reliability = UsageEventStore._reliability_summary(
            [
                {
                    "event": "credit_drawdown_success",
                    "timestamp": old_timestamp,
                    "metadata": {"charge_id": "historical-charge"},
                },
                {
                    "event": "charged_delivery_failed",
                    "timestamp": old_timestamp,
                    "status_code": 502,
                    "metadata": {
                        "attempt_id": "historical-attempt",
                        "charge_id": "historical-charge",
                    },
                }
            ]
        )

        assert reliability["charged_delivery_failures"] == 1
        assert reliability["charged_delivery_failures_last_24h"] == 0
        assert reliability["latest_charged_delivery_failure_at"] == old_timestamp

    def test_unsupported_demand_rejects_prose_like_slug(self, observability_store):
        observability_store.record(
            "unsupported_symbol_request",
            surface="local_mcp",
            subject="DATA-API-FOR-AI",
            asset_class="all",
        )
        observability_store.record(
            "unsupported_symbol_request",
            surface="http_api",
            subject="BTC-USD",
            asset_class="crypto",
        )

        opportunities = observability_store.summarize(days=1)["unsupported_symbol_opportunities"]

        assert opportunities["total_requests"] == 1
        assert opportunities["rows"][0]["symbol"] == "BTC-USD"

    def test_registry_and_payment_challenges_are_summarized(
        self,
        observability_store,
        test_client,
    ):
        registry_response = test_client.get("/server.json")
        challenge_response = test_client.get("/v1/vwap/btc-usd")

        assert registry_response.status_code == 200
        assert challenge_response.status_code == 402

        stats_response = test_client.get("/internal/observability/stats?days=1")
        assert stats_response.status_code == 200
        stats = stats_response.json()

        assert stats["event_counts"]["registry_request"] == 1
        assert stats["event_counts"]["payment_required"] == 1
        assert stats["overview"]["registry_requests"] == 1
        assert stats["overview"]["total_http_requests"] == 2
        assert stats["registry_mix"]["/server.json"] == 1
        assert stats["overview"]["most_used_service"] == "vwap"
        assert stats["service_mix"]["vwap"] == 1
        assert stats["top_subjects"]["BTC-USD"] == 1
        assert stats["data_called"][0]["service"] == "vwap"
        assert stats["data_called"][0]["subject"] == "BTC-USD"
        assert stats["data_called"][0]["payment_prompted"] is True
        assert stats["data_called"][0]["prompt_price_usdc"] == float(settings.pricing.core_crypto)
        assert stats["data_called"][0]["latest_outcome"] == "Prompted to pay; no data returned"
        assert stats["data_called"][0]["last_seen"]
        assert stats["origin_mix"]

    def test_registry_sources_identify_listing_channels(
        self,
        observability_store,
        test_client,
    ):
        glama_response = test_client.get("/.well-known/glama.json")
        pay_response = test_client.get(
            "/server.json",
            headers={"Referer": "https://pay.sh/catalog/blocksize"},
        )
        pay_skills_response = test_client.get(
            "/server.json",
            headers={"User-Agent": "pay-skills-validator"},
        )
        smithery_response = test_client.get(
            "/server.json",
            headers={"Referer": "https://smithery.ai/server/blocksize"},
        )
        x402scan_response = test_client.get(
            "/.well-known/x402",
            headers={"Referer": "https://www.x402scan.com/server/blocksize"},
        )

        assert glama_response.status_code == 200
        assert pay_response.status_code == 200
        assert pay_skills_response.status_code == 200
        assert smithery_response.status_code == 200
        assert x402scan_response.status_code == 200

        stats = observability_store.summarize(days=1)
        assert stats["registry_source_mix"]["Glama"] == 1
        assert stats["registry_source_mix"]["Pay.sh"] == 2
        assert stats["registry_source_mix"]["Smithery"] == 1
        assert stats["registry_source_mix"]["x402scan"] == 1
        assert stats["registry_mix"]["/.well-known/glama.json"] == 1
        assert stats["registry_mix"]["/.well-known/x402"] == 1
        assert stats["registry_mix"]["/server.json"] == 3
        assert stats["timeline"][0]["registry_sources"]["Glama"] == 1
        assert stats["timeline"][0]["registry_sources"]["Pay.sh"] == 2
        assert stats["timeline"][0]["registry_sources"]["Smithery"] == 1
        assert stats["timeline"][0]["registry_sources"]["x402scan"] == 1

    def test_stats_include_distribution_platform_context(
        self,
        observability_store,
        test_client,
    ):
        response = test_client.get("/internal/observability/stats?days=1")

        assert response.status_code == 200
        smithery = response.json()["external_sources"]["smithery"]
        assert smithery["name"] == "Smithery"
        assert smithery["performance_url"].startswith("https://smithery.ai/")
        assert smithery["hosted_mcp_endpoint"].startswith("https://agentic-payments--blocksize")
        assert smithery["metrics_ingestion_configured"] is False
        assert smithery["status"] == "not_ingested"
        pay_sh = response.json()["external_sources"]["pay_sh"]
        assert pay_sh["name"] == "Pay.sh / pay-skills"
        assert pay_sh["listing_url"].startswith("https://pay.sh/")
        assert pay_sh["metrics_ingestion_configured"] is False
        platforms = response.json()["external_sources"]["platforms"]
        assert any(platform["id"] == "pay_sh" for platform in platforms)
        assert any(platform["id"] == "x402scan" for platform in platforms)
        github = next(
            platform for platform in platforms if platform["id"] == "github_package"
        )
        assert github["listing_url"] == "https://github.com/jf-cmyk/agentic-payments"
        assert github["release_status"] == "release_source_v0_6_9"
        assert github["observed_version"] == "0.6.9 candidate"
        gitlab = next(platform for platform in platforms if platform["id"] == "gitlab_mirror")
        assert gitlab["release_status"] == "stale_mirror_not_install_source"
        assert "not a release or package-install source" in gitlab["note"]
        awesome = next(platform for platform in platforms if platform["id"] == "awesome_mcp")
        assert awesome["release_status"] == "merged_listing_stale_wrapper"

        dashboard = test_client.get("/internal/observability/command-center")
        assert dashboard.status_code == 200
        assert "Release Truth" in dashboard.text
        assert "platform.release_status" in dashboard.text

    def test_all_mcp_mounts_use_normalized_observability_labels(self):
        assert resource_server._endpoint_label("/mcp/server/") == "/mcp/server"
        assert resource_server._endpoint_label("/anthropic/mcp/") == "/anthropic/mcp"
        assert resource_server._endpoint_label("/cursor/mcp/") == "/cursor/mcp"
        assert resource_server._endpoint_label("/openai/mcp/") == "/openai/mcp"

    def test_marketplace_metrics_ingestion_configures_platform_coverage(
        self,
        observability_store,
        test_client,
        monkeypatch,
    ):
        ingest = test_client.post(
            "/internal/observability/marketplace-metrics",
            headers={"Authorization": f"Bearer {OBSERVABILITY_TEST_TOKEN}"},
            json={
                "platform_id": "pay_sh",
                "source_url": "https://pay.sh/services/blocksize/market-data",
                "metrics": {
                    "views": 12,
                    "health_checks": 3,
                    "last_status": "healthy",
                },
            },
        )
        assert ingest.status_code == 200
        assert ingest.json()["metrics_recorded"] is True

        response = test_client.get(
            "/internal/observability/stats?days=1",
            headers={"Authorization": f"Bearer {OBSERVABILITY_TEST_TOKEN}"},
        )

        assert response.status_code == 200
        data = response.json()
        pay_sh = data["external_sources"]["pay_sh"]
        assert pay_sh["metrics_ingestion_configured"] is True
        assert pay_sh["status"] == "configured"
        assert pay_sh["latest_external_metrics"]["metrics"]["views"] == 12
        pay_platform = next(
            platform
            for platform in data["external_sources"]["platforms"]
            if platform["id"] == "pay_sh"
        )
        assert pay_platform["external_metrics_configured"] is True
        assert pay_platform["latest_external_metrics"]["metrics"]["health_checks"] == 3
        assert data["marketplace_metrics"]["platforms_configured"] == ["pay_sh"]

    def test_paid_success_records_revenue_and_paid_call(
        self,
        observability_store,
        test_client,
    ):
        mock_vwap = VWAPData(
            pair="btc-usd",
            vwap=95432.50,
            timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc),
            currency="USD",
        )
        mock_client = AsyncMock()
        mock_client.get_vwap_latest = AsyncMock(return_value=mock_vwap)
        app.state.blocksize = mock_client

        with patch(
            "src.resource_server._verify_payment",
            new_callable=AsyncMock,
            return_value={
                "valid": True,
                "network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
                "payment_id": "observability-payment",
                "reservation_id": "observability-reservation",
                "payer": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            },
        ), patch(
            "src.resource_server._settle_payment",
            new_callable=AsyncMock,
            return_value={
                "success": True,
                "transaction": "2" * 88,
                "network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
            },
        ), patch.object(
            CreditManager, "checkpoint_settled_payment", return_value=True
        ), patch.object(CreditManager, "finalize_payment_proof", return_value=True):
            response = test_client.get(
                "/v1/vwap/btc-usd",
                headers={"X-PAYMENT": "mock_sig"},
            )

        assert response.status_code == 200
        stats = observability_store.summarize(days=1)
        assert stats["event_counts"]["payment_proof_submitted"] == 1
        assert stats["event_counts"]["payment_authorization_verified"] == 1
        assert stats["event_counts"]["payment_settled"] == 1
        assert stats["event_counts"]["data_delivered"] == 1
        assert stats["overview"]["paid_calls"] == 1
        assert stats["overview"]["estimated_revenue_usdc"] == float(settings.pricing.core_crypto)
        assert stats["paid_endpoint_mix"]["/v1/vwap/{pair}"] == 1
        assert stats["service_mix"]["vwap"] == 1
        assert stats["data_called"][0]["paid_successes"] == 1
        assert stats["data_called"][0]["revenue_usdc"] == float(settings.pricing.core_crypto)
        assert stats["data_called"][0]["latest_outcome"] == "Data returned after payment or credits"

    def test_unverified_identity_headers_do_not_create_activation_identity(
        self,
        observability_store,
        test_client,
        tmp_path,
    ):
        mock_vwap = VWAPData(
            pair="btc-usd",
            vwap=95432.50,
            timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc),
            currency="USD",
        )
        mock_client = AsyncMock()
        mock_client.get_vwap_latest = AsyncMock(return_value=mock_vwap)
        app.state.blocksize = mock_client
        previous_manager = app.state.credits
        app.state.credits = CreditManager(str(tmp_path / "activation_credits.db"))

        try:
            headers = {
                "X-AGENT-ID": "activation-agent-12345678",
                "X-DEVICE-ID": "activation-device-12345678",
                "X-SESSION-ID": "activation-session-12345678",
            }
            first = test_client.get("/v1/vwap/btc-usd", headers=headers)
            second = test_client.get("/v1/vwap/btc-usd", headers=headers)
        finally:
            app.state.credits = previous_manager

        assert first.status_code == 200
        assert second.status_code == 200
        assert "X-Blocksize-Activation" not in first.headers
        assert "X-Blocksize-Activation" not in second.headers
        stats = observability_store.summarize(days=1)
        assert stats["event_counts"]["data_delivered"] == 2
        assert stats["event_counts"].get("first_live_price_delivered", 0) == 0
        assert stats["overview"]["first_live_price_deliveries"] == 0
        funnel = stats["growth_funnel"]
        assert funnel["summary"]["eligible_identities"] == 0
        assert funnel["summary"]["activated_identities"] == 0
        assert funnel["summary"]["activation_rate"] is None
        assert funnel["summary"]["unattributed_activation_events"] == 0

    def test_growth_funnel_tracks_mature_repeat_and_starter_conversion(self):
        started_at = datetime.now(timezone.utc) - timedelta(days=8)
        activated_at = started_at + timedelta(minutes=2)
        identity_hash = "privacy-safe-growth-identity"

        def event(name, timestamp, **values):
            return {
                "event": name,
                "timestamp": timestamp.isoformat(),
                "wallet_hash": None,
                "metadata": {
                    "identity_hash": identity_hash,
                    "identity_type": "user",
                    "identity_trust": "verified_oauth",
                    **values.pop("metadata", {}),
                },
                **values,
            }

        growth = UsageEventStore._growth_funnel(
            [
                event("mcp_tool_call", started_at),
                event(
                    "mcp_credit_drawdown_success",
                    activated_at,
                    metadata={"attempt_id": "starter-1", "charge_id": "charge-1"},
                ),
                event(
                    "mcp_data_delivered",
                    activated_at,
                    metadata={"attempt_id": "starter-1", "charge_id": "charge-1"},
                ),
                event(
                    "first_live_price_delivered",
                    activated_at,
                    metadata={"payment_mode": "starter_credit"},
                ),
                event(
                    "mcp_credit_drawdown_success",
                    activated_at + timedelta(days=1),
                    metadata={"attempt_id": "starter-2", "charge_id": "charge-2"},
                ),
                event(
                    "mcp_data_delivered",
                    activated_at + timedelta(days=1),
                    metadata={"attempt_id": "starter-2", "charge_id": "charge-2"},
                ),
                event(
                    "payment_proof_submitted",
                    activated_at + timedelta(days=2),
                    metadata={"attempt_id": "paid-1"},
                ),
                event(
                    "payment_authorization_verified",
                    activated_at + timedelta(days=2),
                    metadata={"attempt_id": "paid-1", "payment_id": "payment-1"},
                ),
                event(
                    "payment_settled",
                    activated_at + timedelta(days=2),
                    metadata={
                        "attempt_id": "paid-1",
                        "payment_id": "payment-1",
                        "payment_state": "finalized",
                    },
                ),
                event(
                    "credit_drawdown_failed",
                    activated_at + timedelta(days=3),
                    reason="insufficient_credits",
                    metadata={"credits_remaining": 0},
                ),
            ]
        )

        summary = growth["summary"]
        assert summary["activation_rate"] == 1.0
        assert summary["median_time_to_first_live_price_seconds"] == 120.0
        assert summary["first_live_price_within_3m_rate"] == 1.0
        assert summary["repeat_7d_eligible_identities"] == 1
        assert summary["repeat_7d_identities"] == 1
        assert summary["repeat_7d_rate"] == 1.0
        assert summary["starter_to_paid_identities"] == 1
        assert summary["starter_to_paid_rate"] == 1.0
        assert summary["credits_exhausted_identities"] == 1

    def test_unverified_starter_journey_is_excluded_from_verified_growth_funnel(
        self,
        observability_store,
        test_client,
        tmp_path,
    ):
        mock_client = AsyncMock()
        mock_client.search_pairs_page = AsyncMock(return_value=([], 0))
        mock_client.get_vwap_latest = AsyncMock(
            return_value=VWAPData(
                pair="btc-usd",
                vwap=95432.50,
                timestamp=datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc),
                currency="USD",
            )
        )
        app.state.blocksize = mock_client
        previous_manager = app.state.credits
        app.state.credits = CreditManager(str(tmp_path / "growth_journey_credits.db"))
        headers = {"X-AGENT-ID": "growth-journey-agent-12345678"}

        try:
            discovery = test_client.get("/v1/search?q=BTC", headers=headers)
            starter_responses = [
                test_client.get("/v1/vwap/btc-usd", headers=headers)
                for _ in range(50)
            ]
            exhausted = test_client.get("/v1/vwap/btc-usd", headers=headers)
            with patch(
                "src.resource_server._verify_payment",
                new_callable=AsyncMock,
                return_value={
                    "valid": True,
                    "network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
                    "payment_id": "growth-payment",
                    "reservation_id": "growth-reservation",
                    "payer": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                },
            ), patch(
                "src.resource_server._settle_payment",
                new_callable=AsyncMock,
                return_value={
                    "success": True,
                    "transaction": "2" * 88,
                    "network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
                },
            ), patch.object(
                CreditManager, "checkpoint_settled_payment", return_value=True
            ), patch.object(CreditManager, "finalize_payment_proof", return_value=True):
                paid = test_client.get(
                    "/v1/vwap/btc-usd",
                    headers={**headers, "X-PAYMENT": "growth_journey_proof"},
                )
        finally:
            app.state.credits = previous_manager

        assert discovery.status_code == 200
        assert all(response.status_code == 200 for response in starter_responses)
        assert starter_responses[-1].headers["X-Blocksize-Credits-Remaining"] == "0.0"
        assert exhausted.status_code == 402
        assert paid.status_code == 200

        stats = observability_store.summarize(days=1)
        funnel = stats["growth_funnel"]["summary"]
        assert funnel["activated_identities"] == 1
        assert funnel["starter_activated_identities"] == 0
        assert funnel["credits_exhausted_identities"] == 0
        assert funnel["starter_to_paid_identities"] == 0
        assert funnel["starter_to_paid_rate"] is None
        assert stats["event_counts"]["payment_settled"] == 1
        assert stats["event_counts"]["data_delivered"] == 51

    def test_zero_result_symbol_search_is_ranked_as_coverage_opportunity(
        self,
        observability_store,
        test_client,
    ):
        mock_client = AsyncMock()
        mock_client.search_pairs_page = AsyncMock(return_value=([], 0))
        app.state.blocksize = mock_client

        assert test_client.get("/v1/search?q=nvda&asset_class=equity").status_code == 200
        assert test_client.get("/v1/search?q=NVDA&asset_class=equity").status_code == 200
        assert test_client.get("/v1/search?q=not%20a%20symbol").status_code == 200

        stats = observability_store.summarize(days=1)
        assert stats["overview"]["unsupported_symbol_requests"] == 2
        opportunities = stats["unsupported_symbol_opportunities"]
        assert opportunities["total_requests"] == 2
        assert opportunities["unique_symbol_asset_class_pairs"] == 1
        assert opportunities["rows"][0]["symbol"] == "NVDA"
        assert opportunities["rows"][0]["asset_class"] == "equity"
        assert opportunities["rows"][0]["request_count"] == 2
        assert opportunities["rows"][0]["surfaces"] == ["http_api"]

    def test_credit_drawdown_failure_is_refunded_and_not_delivered(
        self,
        observability_store,
        test_client,
        tmp_path,
    ):
        mock_client = AsyncMock()
        mock_client.get_vwap_latest = AsyncMock(
            side_effect=resource_server.BlocksizeAPIError(-32000, "upstream down")
        )
        app.state.blocksize = mock_client
        previous_manager = app.state.credits
        manager = CreditManager(str(tmp_path / "credits.db"))
        app.state.credits = manager

        try:
            response = test_client.get(
                "/v1/vwap/btc-usd",
                headers={
                    "X-AGENT-ID": "agent-failure-12345678",
                    "X-DEVICE-ID": "device-failure-12345678",
                    "X-SESSION-ID": "session-failure-12345678",
                },
            )
        finally:
            app.state.credits = previous_manager

        assert response.status_code == 502
        assert manager.get_balance("agent-failure-12345678") == 50.0
        assert response.headers["X-Blocksize-Credits-Refunded"] == "1.0"
        assert response.headers["X-Blocksize-Delivery-Status"] == "failed-refunded"
        assert response.headers["X-Blocksize-Retry-Safe"] == "true"
        stats = observability_store.summarize(days=1)
        assert stats["event_counts"]["credit_drawdown_success"] == 1
        assert stats["event_counts"].get("charged_delivery_failed", 0) == 0
        assert stats["event_counts"]["refunded_delivery_failed"] == 1
        assert stats["event_counts"].get("data_delivered", 0) == 0
        assert stats["overview"]["paid_calls"] == 0
        assert stats["reliability"]["charged_delivery_failures"] == 0
        assert stats["reliability"]["refunded_delivery_failures"] == 1
        assert stats["popularity"]["total_failed_after_credit"] == 1
        assert stats["popularity"]["total_refunded_after_credit"] == 1
        failed = next(
            row
            for row in stats["popularity"]["rows"]
            if row["service"] == "vwap" and row["subject"] == "BTC-USD"
        )
        assert failed["delivered"] == 0
        assert failed["failed_after_credit"] == 1
        assert failed["refunded_after_credit"] == 1
        assert failed["credits_spent"] == 0.0

    def test_paid_post_preflight_rejects_missing_symbol_before_credit_drawdown(
        self,
        observability_store,
        test_client,
        tmp_path,
    ):
        previous_manager = app.state.credits
        manager = CreditManager(str(tmp_path / "preflight_credits.db"))
        app.state.credits = manager

        try:
            response = test_client.post(
                "/v1/checks/pre-trade",
                headers={"X-AGENT-ID": "preflight-agent-12345678"},
                json={"side": "buy", "notional_usd": 1000},
            )
        finally:
            app.state.credits = previous_manager

        assert response.status_code == 400
        assert "no credits or payment were used" in response.json()["message"]
        assert manager.get_balance("preflight-agent-12345678") == 0.0
        stats = observability_store.summarize(days=1)
        assert stats["event_counts"]["paid_request_rejected_preflight"] == 1
        assert stats["event_counts"].get("credit_drawdown_success", 0) == 0
        assert stats["event_counts"].get("payment_verified", 0) == 0

    def test_paid_post_preflight_rejects_invalid_json_before_x402_verification(
        self,
        observability_store,
        test_client,
    ):
        with patch(
            "src.resource_server._verify_payment",
            new_callable=AsyncMock,
        ) as verify_payment:
            response = test_client.post(
                "/v1/checks/pre-trade",
                headers={
                    "Content-Type": "application/json",
                    "X-PAYMENT": "must-not-be-verified",
                },
                content="{not-json",
            )

        assert response.status_code == 400
        assert "no credits or payment were used" in response.json()["message"]
        verify_payment.assert_not_awaited()
        stats = observability_store.summarize(days=1)
        assert stats["event_counts"]["paid_request_rejected_preflight"] == 1
        assert stats["event_counts"].get("payment_proof_submitted", 0) == 0

    def test_dashboard_token_can_protect_internal_stats(
        self,
        observability_store,
        test_client,
        monkeypatch,
    ):
        login_response = test_client.get(
            "/internal/observability",
            headers={"Authorization": ""},
        )
        assert login_response.status_code == 401
        assert "Internal Observability" in login_response.text
        assert "Open Dashboard" in login_response.text
        assert 'method="post"' in login_response.text
        assert 'action="/internal/observability/login"' in login_response.text

        query_response = test_client.get(
            f"/internal/observability?token={OBSERVABILITY_TEST_TOKEN}",
            headers={"Authorization": ""},
        )
        assert query_response.status_code == 401
        assert "set-cookie" not in query_response.headers

        wrong_response = test_client.post(
            "/internal/observability/login",
            headers={"Authorization": ""},
            data={"password": "not-the-password"},
            follow_redirects=False,
        )
        assert wrong_response.status_code == 401
        assert "set-cookie" not in wrong_response.headers

        dashboard_response = test_client.post(
            "/internal/observability/login",
            headers={"Authorization": ""},
            data={"password": OBSERVABILITY_TEST_TOKEN},
            follow_redirects=False,
        )
        assert dashboard_response.status_code == 303
        assert dashboard_response.headers["location"] == "/internal/observability/command-center"
        set_cookie = dashboard_response.headers["set-cookie"]
        assert "observability_token" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "Secure" in set_cookie
        assert "SameSite=strict" in set_cookie
        assert "Path=/internal/observability" in set_cookie
        assert "Max-Age=43200" in set_cookie

        stats_response = test_client.get(
            "/internal/observability/stats",
            headers={"Authorization": f"Bearer {OBSERVABILITY_TEST_TOKEN}"},
        )
        assert stats_response.status_code == 200

        cookie_stats_response = test_client.get(
            "/internal/observability/stats",
            headers={"Authorization": ""},
        )
        assert cookie_stats_response.status_code == 200

        legacy_response = test_client.get(
            f"/internal/observability/legacy?token={OBSERVABILITY_TEST_TOKEN}"
        )
        assert legacy_response.status_code == 200
        assert f"token={OBSERVABILITY_TEST_TOKEN}" not in legacy_response.text
        assert "/internal/observability/stats?token=" not in legacy_response.text

    def test_observability_auth_fails_closed_without_strong_token(
        self,
        test_client,
        monkeypatch,
    ):
        monkeypatch.delenv("OBSERVABILITY_DASHBOARD_TOKEN", raising=False)
        monkeypatch.setattr(settings.server, "observability_dashboard_token", "secret")

        response = test_client.get(
            "/internal/observability/stats",
            headers={"Authorization": ""},
        )

        assert response.status_code == 503
        assert response.headers["cache-control"] == "no-store"
        assert "authentication unavailable" in response.json()["error"].lower()

    def test_legacy_observability_escapes_stored_telemetry_before_inner_html(
        self,
        observability_store,
        test_client,
    ):
        attack = (
            '\"><img src=x onerror="globalThis.__xss=1">'
            '<svg onload="globalThis.__xss=2"></svg>'
        )
        observability_store.record(
            "mcp_tool_call",
            surface=attack,
            endpoint=attack,
            user_agent=attack,
            referrer=attack,
            subject=attack,
            asset_class=attack,
            reason=attack,
            tool_name=attack,
        )
        stats_response = test_client.get("/internal/observability/stats?days=1")
        assert stats_response.status_code == 200
        stats = stats_response.json()
        assert any(row.get("surface") == attack for row in stats["recent_events"])

        dashboard = test_client.get("/internal/observability/legacy")
        assert dashboard.status_code == 200
        script = dashboard.text.rsplit("<script>", 1)[1].split("</script>", 1)[0]
        probe = """
globalThis.__xss = 0;
const elements = new Map();
const elementFor = id => {
  if (!elements.has(id)) {
    elements.set(id, {
      id,
      value: id === "window" ? "1" : "",
      innerHTML: "",
      textContent: "",
      addEventListener() {},
      setAttribute() {},
    });
  }
  return elements.get(id);
};
globalThis.document = { getElementById: elementFor };
globalThis.setInterval = () => 1;
globalThis.clearInterval = () => {};
const telemetryPayload = """ + json.dumps(stats) + ";\n" + """
globalThis.fetch = async () => ({
  ok: true,
  json: async () => telemetryPayload,
});
""" + script + """
await new Promise(resolve => setTimeout(resolve, 25));
const rendered = [
  "kpis", "timeline", "services", "origins", "mcp", "subjects",
  "clients", "agents", "data-called", "recent",
].map(id => elementFor(id).innerHTML).join("\\n");
process.stdout.write(JSON.stringify({
  marker: globalThis.__xss,
  rendered,
  freshness: elementFor("freshness").textContent,
}));
"""
        result = subprocess.run(
            ["node", "--input-type=module", "--eval", probe],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

        assert result.returncode == 0, result.stderr
        rendered = json.loads(result.stdout)
        assert rendered["marker"] == 0
        assert "Unable to load stats" not in rendered["freshness"]
        assert attack not in rendered["rendered"]
        assert "<img" not in rendered["rendered"]
        assert "<svg" not in rendered["rendered"]
        assert "&lt;img" in rendered["rendered"]
        assert "&lt;svg" in rendered["rendered"]
        assert 'title="&quot;&gt;&lt;img' in rendered["rendered"]

    def test_observability_login_rejects_duplicate_and_oversize_forms(
        self,
        test_client,
    ):
        duplicate = test_client.post(
            "/internal/observability/login",
            headers={
                "Authorization": "",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            content=(
                f"password={OBSERVABILITY_TEST_TOKEN}"
                f"&password={OBSERVABILITY_TEST_TOKEN}"
            ),
        )
        oversized = test_client.post(
            "/internal/observability/login",
            headers={
                "Authorization": "",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            content="password=" + ("x" * 5000),
        )

        assert duplicate.status_code == 400
        assert oversized.status_code == 413
        assert "set-cookie" not in duplicate.headers
        assert "set-cookie" not in oversized.headers

    def test_observability_logout_expires_the_scoped_secure_cookie(
        self,
        test_client,
    ):
        response = test_client.get(
            "/internal/observability/logout",
            follow_redirects=False,
        )

        assert response.status_code == 303
        set_cookie = response.headers["set-cookie"]
        assert "Path=/internal/observability" in set_cookie
        assert "HttpOnly" in set_cookie
        assert "Secure" in set_cookie
        assert "SameSite=strict" in set_cookie

    def test_command_center_subpage_serves_improved_dashboard(
        self,
        observability_store,
        test_client,
    ):
        response = test_client.get("/internal/observability/command-center")

        assert response.status_code == 200
        assert "Product Usage Command Center" in response.text
        assert "Growth Funnel" in response.text
        assert "renderGrowthFunnel" in response.text
        assert "renderRwaPilot" in response.text
        assert 'id="growth-stages"' in response.text
        assert 'id="growth-targets"' in response.text
        assert 'id="rwa-pilot-table"' in response.text
        assert "Called Data Detail" in response.text
        assert "Recent Event Trace" in response.text
        assert "Glama" in response.text
        assert "Pay.sh" in response.text
        assert "MCP Registry" in response.text
        assert "Smithery" in response.text
        assert 'id="campaigns"' in response.text
        assert 'id="outbound-destinations"' in response.text
        assert "x402scan" in response.text
        assert "Awesome MCP" in response.text
        assert "Pay.sh Marketplace" in response.text
        assert "Smithery Hosted Activity" in response.text
        assert "Platform Coverage" in response.text
        assert "Wallet Inflows" in response.text
        assert "Data Popularity" in response.text
        assert "Daily Executive Brief" in response.text
        assert "What Does Not Work" in response.text
        assert "Improvement Steps" in response.text
        assert "Daily Checks" in response.text
        assert "renderDailyInterpretation" in response.text
        assert "renderPopularity" in response.text
        assert "popularity-table" in response.text
        assert "renderWalletInflows" in response.text
        assert "wallet-inflow-table" in response.text
        assert "renderPayShSource" in response.text
        assert "renderSmitherySource" in response.text
        assert "renderPlatformCoverage" in response.text
        assert "registrySourceWatchlist" in response.text
        assert 'id="telemetry-scope"' in response.text
        assert 'id="decision-confidence"' in response.text
        assert "Operational only" in response.text or "Checking evidence" in response.text
        assert 'statsUrl.searchParams.set("include_synthetic", includeSynthetic)' in response.text
        assert 'id="timeline-dates"' in response.text
        assert "timelineTip" in response.text
        assert "data-tip" in response.text
        assert "Password protected" in response.text
        assert 'href="/internal/observability/logout"' in response.text
        assert 'document.addEventListener("visibilitychange"' in response.text
        assert "live && !document.hidden ? setInterval(load, 10000) : null" in response.text
        assert '${escapeAttr(text(row.subject || row.reason))}' in response.text
        assert "Token not configured" not in response.text

    def test_popularity_rollup_separates_requested_delivered_and_blocked(
        self,
        observability_store,
    ):
        observability_store.record(
            "payment_required",
            surface="http_api",
            endpoint="/v1/vwap/{pair}",
            subject="BTC-USD",
            price_usdc=0.002,
        )
        observability_store.record(
            "mcp_tool_call",
            surface="claude_mcp",
            tool_name="get_vwap",
            subject="BTCUSD",
            metadata={"credit_cost": 1},
        )
        observability_store.record(
            "mcp_credit_drawdown_success",
            surface="claude_mcp",
            tool_name="get_vwap",
            subject="BTCUSD",
            metadata={
                "attempt_id": "attempt-good",
                "charge_id": "charge-good",
                "credits_spent": 1,
            },
        )
        observability_store.record(
            "mcp_data_delivered",
            surface="claude_mcp",
            tool_name="get_vwap",
            subject="BTCUSD",
            metadata={
                "attempt_id": "attempt-good",
                "charge_id": "charge-good",
                "credits_spent": 1,
            },
        )
        observability_store.record(
            "mcp_credit_drawdown_success",
            surface="claude_mcp",
            tool_name="get_vwap",
            subject="BADPAIR",
            metadata={
                "attempt_id": "attempt-bad",
                "charge_id": "charge-bad",
                "credits_spent": 1,
            },
        )
        observability_store.record(
            "mcp_tool_error",
            surface="claude_mcp",
            tool_name="get_vwap",
            subject="BADPAIR",
            reason="blocksize_api_error",
            metadata={"attempt_id": "attempt-bad", "charge_id": "charge-bad"},
        )

        popularity = observability_store.summarize(days=1)["popularity"]

        assert popularity["total_requested"] == 3
        assert popularity["total_delivered"] == 1
        assert popularity["total_blocked"] == 1
        assert popularity["total_failed_after_credit"] == 1
        assert popularity["total_credits_spent"] == 1.0
        rows = {
            (row["surface"], row["service"], row["subject"]): row
            for row in popularity["rows"]
        }
        assert rows[("http_api", "vwap", "BTC-USD")]["blocked"] == 1
        assert rows[("claude_mcp", "vwap", "BTCUSD")]["delivered"] == 1
        assert rows[("claude_mcp", "vwap", "BADPAIR")]["failed_after_credit"] == 1
        assert rows[("claude_mcp", "vwap", "BADPAIR")]["delivered"] == 0

        stats = observability_store.summarize(days=1)
        assert stats["overview"]["paid_calls"] == 1
        called_rows = {
            (row["surface"], row["service"], row["subject"]): row
            for row in stats["data_called"]
        }
        assert called_rows[("claude_mcp", "vwap", "BADPAIR")]["paid_successes"] == 0
        assert called_rows[("claude_mcp", "vwap", "BADPAIR")]["latest_outcome"] == (
            "Credit used then refunded after data retrieval failed"
        )

    def test_internal_stats_include_daily_interpretation(
        self,
        observability_store,
        test_client,
    ):
        observability_store.record(
            "payment_required",
            surface="http_api",
            endpoint="/v1/vwap/{pair}",
            subject="BTC-USD",
            price_usdc=0.002,
        )

        response = test_client.get("/internal/observability/stats?days=1")

        assert response.status_code == 200
        payload = response.json()
        interpretation = payload["daily_interpretation"]
        assert payload["decision_confidence"]["level"] == "limited"
        assert "Reliability debugging" in payload["decision_confidence"]["safe_uses"]
        assert interpretation["title"] == "Daily Executive Brief"
        assert interpretation["status"] == "needs_attention"
        assert any(
            "Payment prompts are not converting" == item["title"]
            for item in interpretation["what_does_not"]
        )
        assert any(step["priority"] == "P0" for step in interpretation["improvement_steps"])
        assert {
            "Data delivery",
            "Payment proof submission",
            "Wallet inflows",
            "Raw evidence",
        }.issubset({check["name"] for check in interpretation["checks"]})
        assert any("Raw evidence review" in line for line in interpretation["executive_summary"])
        assert any(
            "block rate is 100.0%" in line
            for line in interpretation["executive_summary"]
        )

    def test_unreconciled_settlement_is_a_p0_command_center_alert(
        self,
        observability_store,
        test_client,
    ):
        observability_store.record(
            "payment_settlement_unreconciled",
            surface="http_api",
            endpoint="/v1/vwap/{pair}",
            subject="BTC-USD",
            price_usdc=0.002,
            network="eip155:8453",
            reason="settlement_checkpoint_failed_after_remote_settlement",
            metadata={
                "attempt_id": "unreconciled-attempt",
                "payment_id": "unreconciled-payment",
                "payment_state": "settlement_unreconciled",
                "transaction_hash": "hashed-transaction",
            },
        )

        payload = test_client.get("/internal/observability/stats?days=1").json()

        assert payload["economic_correlation"]["unreconciled_settlements"] == 1
        assert payload["decision_confidence"]["level"] == "limited"
        assert payload["daily_interpretation"]["status"] == "needs_attention"
        checks = {
            check["name"]: check
            for check in payload["daily_interpretation"]["checks"]
        }
        assert checks["Settlement reconciliation"]["status"] == "fail"
        assert any(
            item["title"]
            == "Payment settlement outcomes require immediate reconciliation"
            for item in payload["daily_interpretation"]["what_does_not"]
        )

    def test_internal_stats_include_wallet_inflows(
        self,
        observability_store,
        test_client,
        tmp_path,
    ):
        manager = CreditManager(str(tmp_path / "credits.db"))
        manager.record_payment_proof(
            "direct-observed-tx",
            "solana",
            2000,
            "merchant-recipient",
            "GET /v1/vwap/BTC-USD",
        )
        manager.record_payment_proof(
            "credit-observed-tx",
            "solana",
            900000,
            "merchant-recipient",
            "credits:starter",
        )
        manager.add_credits(
            "wallet-observed-1234567890",
            1000,
            "credit-observed-tx",
            0.9,
        )
        previous_manager = app.state.credits
        app.state.credits = manager

        try:
            response = test_client.get("/internal/observability/stats?days=1")
        finally:
            app.state.credits = previous_manager

        assert response.status_code == 200
        inflows = response.json()["wallet_inflows"]
        assert inflows["total_inflows"] == 2
        assert inflows["direct_x402_count"] == 1
        assert inflows["credit_topup_count"] == 1
        assert inflows["total_usdc"] == 0.902
        assert {row["tx_hash"] for row in inflows["rows"]} == {
            "direct-observed-tx",
            "credit-observed-tx",
        }

    def test_internal_stats_do_not_treat_rejected_proof_as_paid_success(
        self,
        observability_store,
        test_client,
    ):
        observability_store.record(
            "payment_required",
            surface="http_api",
            endpoint="/v1/vwap/{pair}",
            subject="BTC-USD",
        )
        observability_store.record(
            "payment_proof_submitted",
            surface="http_api",
            endpoint="/v1/vwap/{pair}",
            subject="BTC-USD",
            metadata={"proof_hash": "rejected-proof", "attempt_id": "rejected-attempt"},
        )
        observability_store.record(
            "payment_failed",
            surface="http_api",
            endpoint="/v1/vwap/{pair}",
            subject="BTC-USD",
            reason="invalid_payment",
            metadata={"attempt_id": "rejected-attempt"},
        )

        response = test_client.get("/internal/observability/stats?days=1")

        assert response.status_code == 200
        payload = response.json()
        assert any(
            "none are settled" in reason
            for reason in payload["decision_confidence"]["reasons"]
        )
        interpretation = payload["daily_interpretation"]
        assert any(
            item["title"] == "Submitted payment proofs are not settling"
            for item in interpretation["what_does_not"]
        )
        checks = {check["name"]: check for check in interpretation["checks"]}
        assert checks["Payment proof submission"]["status"] == "fail"
        assert checks["Raw evidence"]["status"] == "watch"

    def test_transport_and_known_monitors_are_not_product_demand(
        self,
        observability_store,
    ):
        for _ in range(5):
            observability_store.record(
                "http_request",
                surface="public_mcp",
                endpoint="/mcp/server/",
                status_code=200,
                user_agent="registry-health-check",
            )
        observability_store.record(
            "payment_required",
            surface="http_api",
            endpoint="/v1/vwap/{pair}",
            subject="BTC-USD",
            user_agent="CarbonMonitor/1.0",
        )
        observability_store.record(
            "payment_required",
            surface="http_api",
            endpoint="/v1/briefs/market",
            subject="ETH-USD",
            user_agent="customer-agent/1.0",
        )

        stats = observability_store.summarize(days=1)

        assert stats["transport_requests"]["total_transport_requests"] == 5
        assert stats["popularity"]["total_requested"] == 2
        assert stats["request_quality"]["gross_live_data_requests"] == 2
        assert stats["request_quality"]["known_monitor_requests"] == 1
        assert stats["request_quality"]["non_monitor_requests"] == 1
        packages = stats["product_performance"]["market_intelligence_rows"]
        assert any(row["product_id"] == "agent_market_brief" for row in packages)

    def test_stats_reconcile_legacy_inflow_and_expose_alert_feed(
        self,
        observability_store,
        test_client,
        tmp_path,
    ):
        common = {
            "surface": "http_api",
            "endpoint": "/v1/vwap/{pair}",
            "subject": "BTC-USD",
            "price_usdc": 0.002,
        }
        observability_store.record(
            "payment_required",
            **common,
            user_agent="customer-agent/1.0",
        )
        observability_store.record(
            "payment_proof_submitted",
            **common,
            metadata={"attempt_id": "attempt-1"},
        )
        verified = {
            "attempt_id": "attempt-1",
            "payment_id": "payment-1",
            "payment_state": "finalized",
            "identity_hash": "verified-payer",
            "identity_type": "wallet",
            "identity_trust": "verified_x402",
        }
        observability_store.record("payment_authorization_verified", **common, metadata=verified)
        observability_store.record("payment_settled", **common, metadata=verified)
        observability_store.record(
            "data_delivered",
            **common,
            metadata={**verified, "payment_mode": "x402"},
        )
        manager = CreditManager(str(tmp_path / "reconciliation-credits.db"))
        manager.record_payment_proof(
            "recognized-tx",
            "solana",
            2000,
            "merchant-recipient",
            "GET /v1/vwap/BTC-USD",
        )
        manager.record_payment_proof(
            "legacy-tx",
            "solana",
            3000,
            "merchant-recipient",
            "GET /v1/vwap/ETH-USD",
        )
        previous_manager = app.state.credits
        app.state.credits = manager
        try:
            payload = test_client.get("/internal/observability/stats?days=1").json()
            alert_payload = test_client.get(
                "/internal/observability/alerts?days=1"
            ).json()
        finally:
            app.state.credits = previous_manager

        reconciliation = payload["revenue_reconciliation"]
        assert reconciliation["decision_grade_recognized_revenue_usdc"] == 0.002
        assert reconciliation["legacy_transaction_backed_x402_usdc"] == 0.003
        assert reconciliation["status"] == "classified_pending_lifecycle_backfill"
        assert alert_payload["revenue_reconciliation"]["status"] == reconciliation["status"]
        assert alert_payload["alerts"]["active_count"] >= 1
        assert payload["conversion_experiment"]["status"] in {
            "collecting_clean_baseline",
            "ready_to_run",
        }


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

    def test_requirement_selection_requires_exact_caip2_network(self):
        requirements = [
            {"network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"},
            {"network": "eip155:8453"},
        ]

        assert resource_server._select_requirement("eip155:8453", requirements) == requirements[1]
        assert resource_server._select_requirement("eip155:1", requirements) is None
        assert resource_server._select_requirement("solana:devnet", requirements) is None
        assert resource_server._select_requirement("base64", requirements) is None

    def test_payment_freshness_rejects_missing_stale_and_future_time(
        self,
        monkeypatch,
    ):
        monkeypatch.setattr(settings.server, "x402_payment_max_age_seconds", 100)
        monkeypatch.setattr(settings.server, "x402_payment_future_skew_seconds", 10)
        monkeypatch.setattr(resource_server.time, "time", lambda: 1000.0)

        assert resource_server._transaction_is_recent(None) is False
        assert resource_server._transaction_is_recent(899) is False
        assert resource_server._transaction_is_recent(900) is True
        assert resource_server._transaction_is_recent(1010) is True
        assert resource_server._transaction_is_recent(1011) is False

    @pytest.mark.asyncio
    async def test_legacy_solana_verifier_requires_genesis_finality_signature_and_time(
        self,
        monkeypatch,
    ):
        tx_hash = "1" * 88
        recipient = "So11111111111111111111111111111111111111112"
        mint = settings.x402.solana_usdc_address
        requirement = {
            "network": settings.x402.solana_network,
            "payTo": recipient,
            "maxAmountRequired": "100",
            "asset": f"{settings.x402.solana_network}/{mint}",
        }
        transaction = {
            "transaction": {"signatures": [tx_hash]},
            "blockTime": 995,
            "meta": {
                "err": None,
                "preTokenBalances": [
                    {
                        "accountIndex": 1,
                        "mint": mint,
                        "owner": recipient,
                        "uiTokenAmount": {"amount": "0", "decimals": 6},
                    }
                ],
                "postTokenBalances": [
                    {
                        "accountIndex": 1,
                        "mint": mint,
                        "owner": recipient,
                        "uiTokenAmount": {"amount": "100", "decimals": 6},
                    }
                ],
            },
        }
        rpc = AsyncMock(
            side_effect=[settings.x402.solana_network.partition(":")[2], transaction]
        )
        monkeypatch.setattr(resource_server, "_rpc_result", rpc)
        monkeypatch.setattr(resource_server.time, "time", lambda: 1000.0)

        valid, reason = await resource_server._verify_legacy_solana_payment(
            tx_hash,
            settings.x402.solana_network,
            requirement,
        )

        assert (valid, reason) == (True, "ok")
        transaction_params = rpc.await_args_list[1].args[3]
        assert transaction_params[0] == tx_hash
        assert transaction_params[1]["commitment"] == "finalized"

    @pytest.mark.asyncio
    async def test_legacy_evm_verifier_requires_chain_time_confirmations_and_decimals(
        self,
        monkeypatch,
    ):
        tx_hash = "0x" + "12" * 32
        block_hash = "0x" + "34" * 32
        recipient = "0x1111111111111111111111111111111111111111"
        token = settings.x402.base_usdc_address
        recipient_topic = "0x" + recipient.removeprefix("0x").rjust(64, "0")
        requirement = {
            "network": settings.x402.base_network,
            "payTo": recipient,
            "maxAmountRequired": "5000",
            "asset": f"{settings.x402.base_network}/{token}",
        }
        receipt = {
            "transactionHash": tx_hash.upper().replace("0X", "0x"),
            "status": "0x1",
            "blockHash": block_hash,
            "blockNumber": "0x64",
            "logs": [
                {
                    "address": token,
                    "topics": [EVM_TRANSFER_TOPIC, "0x" + "0" * 64, recipient_topic],
                    "data": hex(5000),
                }
            ],
        }
        block = {"hash": block_hash, "number": "0x64", "timestamp": "0x3e3"}
        rpc = AsyncMock(
            side_effect=["0x2105", receipt, "0x66", block, "0x6"]
        )
        monkeypatch.setattr(resource_server, "_rpc_result", rpc)
        monkeypatch.setattr(resource_server.time, "time", lambda: 1000.0)
        monkeypatch.setattr(settings.server, "x402_payment_min_confirmations", 3)

        valid, reason = await resource_server._verify_legacy_evm_payment(
            tx_hash,
            settings.x402.base_network,
            requirement,
        )

        assert (valid, reason) == (True, "ok")

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
                        "uiTokenAmount": {"amount": "50", "decimals": 6},
                    }
                ],
                "postTokenBalances": [
                    {
                        "accountIndex": 4,
                        "mint": mint,
                        "owner": recipient,
                        "uiTokenAmount": {"amount": "175", "decimals": 6},
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


class TestPaymentDeliveryLifecycle:
    @staticmethod
    def _mock_payment_header(proof: str) -> str:
        return base64.b64encode(
            json.dumps(
                {
                    "proof": proof,
                    "network": settings.x402.solana_network,
                }
            ).encode()
        ).decode()

    def test_successful_delivery_finalizes_reserved_payment(
        self,
        test_client,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.setattr(settings.server, "x402_allow_mock_payments", True)
        monkeypatch.setattr(settings.server, "x402_allow_legacy_payments", False)
        manager = CreditManager(str(tmp_path / "lifecycle-success.db"))
        previous_manager = app.state.credits
        app.state.credits = manager
        mock_client = AsyncMock()
        mock_client.get_vwap_latest = AsyncMock(
            return_value=VWAPData(
                pair="btc-usd",
                vwap=95_000,
                timestamp=datetime.now(timezone.utc),
                currency="USD",
            )
        )
        app.state.blocksize = mock_client
        payment_header = self._mock_payment_header("mock_lifecycle_ok")

        try:
            response = test_client.get(
                "/v1/vwap/btc-usd",
                headers={"PAYMENT-SIGNATURE": payment_header},
            )
            replay = test_client.get(
                "/v1/vwap/btc-usd",
                headers={"PAYMENT-SIGNATURE": payment_header},
            )
        finally:
            app.state.credits = previous_manager

        assert response.status_code == 200
        assert response.headers["PAYMENT-RESPONSE"]
        assert replay.status_code == 200
        assert replay.content == response.content
        assert replay.headers["X-Payment-Replayed"] == "true"
        assert replay.headers["PAYMENT-RESPONSE"] == response.headers["PAYMENT-RESPONSE"]
        assert mock_client.get_vwap_latest.await_count == 1
        assert manager.payment_proof_state("mock_lifecycle_ok")["state"] == "finalized"
        assert manager.payment_proof_state("mock_lifecycle_ok")["has_cached_response"] == 1

    def test_official_v2_signature_runs_through_facilitator_and_finalizes(
        self,
        test_client,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.setattr(settings.server, "x402_allow_mock_payments", False)
        monkeypatch.setattr(settings.server, "x402_allow_legacy_payments", False)
        monkeypatch.setattr(settings.x402, "facilitator_url", "https://facilitator.example")
        manager = CreditManager(str(tmp_path / "official-lifecycle.db"))
        previous_manager = app.state.credits
        app.state.credits = manager
        mock_client = AsyncMock()
        mock_client.get_vwap_latest = AsyncMock(
            side_effect=[
                BlocksizeAPIError(503, "temporary upstream failure"),
                VWAPData(
                    pair="btc-usd",
                    vwap=95_000,
                    timestamp=datetime.now(timezone.utc),
                    currency="USD",
                ),
            ]
        )
        app.state.blocksize = mock_client
        calls: list[str] = []

        class FakeFacilitator:
            def __init__(self, *_args, **_kwargs):
                pass

            async def verify(self, _payment, _requirement):
                calls.append("verify")
                return {
                    "isValid": True,
                    "payer": "0x2222222222222222222222222222222222222222",
                }

            async def settle(self, _payment, requirement):
                calls.append("settle")
                return {
                    "success": True,
                    "transaction": "0x" + "34" * 32,
                    "network": requirement["network"],
                    "amount": requirement["amount"],
                }

        try:
            challenge = test_client.get("/v1/vwap/btc-usd").json()
            accepted = next(
                item for item in challenge["accepts"] if item["network"] == "eip155:8453"
            )
            payment = {
                "x402Version": 2,
                "payload": {
                    "signature": "0x" + "ab" * 65,
                    "authorization": {
                        "from": "0x2222222222222222222222222222222222222222",
                        "to": accepted["payTo"],
                        "value": accepted["amount"],
                        "validAfter": "0",
                        "validBefore": "9999999999",
                        "nonce": "0x" + "12" * 32,
                    },
                },
                "accepted": accepted,
                "resource": challenge["resource"],
            }
            header = base64.b64encode(json.dumps(payment).encode()).decode()
            with patch("src.resource_server.FacilitatorAdapter", FakeFacilitator):
                failed = test_client.get(
                    "/v1/vwap/btc-usd",
                    headers={"PAYMENT-SIGNATURE": header},
                )
                response = test_client.get(
                    "/v1/vwap/btc-usd",
                    headers={"PAYMENT-SIGNATURE": header},
                )
                replay = test_client.get(
                    "/v1/vwap/btc-usd",
                    headers={"PAYMENT-SIGNATURE": header},
                )
        finally:
            app.state.credits = previous_manager

        assert failed.status_code == 502
        assert response.status_code == 200
        assert replay.status_code == 200
        assert replay.content == response.content
        assert replay.headers["X-Payment-Replayed"] == "true"
        assert calls == ["verify", "settle"]
        assert mock_client.get_vwap_latest.await_count == 2
        settled = json.loads(base64.b64decode(response.headers["PAYMENT-RESPONSE"]))
        assert settled["transaction"] == "0x" + "34" * 32

    def test_failed_handler_releases_payment_for_exact_retry(
        self,
        test_client,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.setattr(settings.server, "x402_allow_mock_payments", True)
        monkeypatch.setattr(settings.server, "x402_allow_legacy_payments", False)
        manager = CreditManager(str(tmp_path / "lifecycle-retry.db"))
        previous_manager = app.state.credits
        app.state.credits = manager
        mock_client = AsyncMock()
        mock_client.get_vwap_latest = AsyncMock(
            side_effect=[
                BlocksizeAPIError(503, "temporary upstream failure"),
                VWAPData(
                    pair="btc-usd",
                    vwap=95_000,
                    timestamp=datetime.now(timezone.utc),
                    currency="USD",
                ),
            ]
        )
        app.state.blocksize = mock_client
        headers = {
            "PAYMENT-SIGNATURE": self._mock_payment_header("mock_lifecycle_retry")
        }

        try:
            failed = test_client.get("/v1/vwap/btc-usd", headers=headers)
            released = manager.payment_proof_state("mock_lifecycle_retry")
            retried = test_client.get("/v1/vwap/btc-usd", headers=headers)
        finally:
            app.state.credits = previous_manager

        assert failed.status_code == 502
        assert released["state"] == "released"
        assert retried.status_code == 200
        assert manager.payment_proof_state("mock_lifecycle_retry")["state"] == "finalized"

    def test_explicit_settlement_failure_keeps_payment_pending_and_withholds_data(
        self,
        test_client,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.setattr(settings.server, "x402_allow_mock_payments", True)
        manager = CreditManager(str(tmp_path / "lifecycle-pending.db"))
        previous_manager = app.state.credits
        app.state.credits = manager
        mock_client = AsyncMock()
        mock_client.get_vwap_latest = AsyncMock(
            return_value=VWAPData(
                pair="btc-usd",
                vwap=95_000,
                timestamp=datetime.now(timezone.utc),
                currency="USD",
            )
        )
        app.state.blocksize = mock_client

        try:
            with patch(
                "src.resource_server._settle_payment",
                new_callable=AsyncMock,
                return_value={"success": False, "errorReason": "settlement_failed"},
            ):
                response = test_client.get(
                    "/v1/vwap/btc-usd",
                    headers={
                        "PAYMENT-SIGNATURE": self._mock_payment_header(
                            "mock_lifecycle_pending"
                        )
                    },
                )
        finally:
            app.state.credits = previous_manager

        assert response.status_code == 502
        assert "data" not in response.json()
        assert manager.payment_proof_state("mock_lifecycle_pending")["state"] == "pending"

    def test_ambiguous_transport_settlement_quarantines_and_never_resettles(
        self,
        observability_store,
        test_client,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.setattr(settings.server, "x402_allow_mock_payments", True)
        manager = CreditManager(str(tmp_path / "lifecycle-timeout.db"))
        previous_manager = app.state.credits
        app.state.credits = manager
        mock_client = AsyncMock()
        mock_client.get_vwap_latest = AsyncMock(
            return_value=VWAPData(
                pair="btc-usd",
                vwap=95_000,
                timestamp=datetime.now(timezone.utc),
                currency="USD",
            )
        )
        app.state.blocksize = mock_client
        settle = AsyncMock(
            return_value={
                "success": False,
                "errorReason": "facilitator_unavailable",
                "outcomeUnknown": True,
            }
        )
        headers = {
            "PAYMENT-SIGNATURE": self._mock_payment_header("mock_lifecycle_timeout")
        }

        try:
            with patch("src.resource_server._settle_payment", settle):
                response = test_client.get("/v1/vwap/btc-usd", headers=headers)
                retry = test_client.get("/v1/vwap/btc-usd", headers=headers)
        finally:
            app.state.credits = previous_manager

        assert response.status_code == 503
        assert response.json()["error"] == "Payment Settlement Outcome Unknown"
        assert retry.status_code == 402
        assert settle.await_count == 1
        assert mock_client.get_vwap_latest.await_count == 1
        state = manager.payment_proof_state("mock_lifecycle_timeout")
        assert state["state"] == "settlement_unknown"
        alerts = [
            event
            for event in observability_store.recent_events(limit=30)
            if event["event"] == "payment_settlement_unreconciled"
        ]
        assert len(alerts) == 1
        assert alerts[0]["reason"] == "facilitator_unavailable"
        assert alerts[0]["metadata"]["payment_state"] == "settlement_unknown"

    def test_finalize_failure_recovers_checkpoint_without_resettling(
        self,
        test_client,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.setattr(settings.server, "x402_allow_mock_payments", True)
        manager = CreditManager(str(tmp_path / "lifecycle-recovery.db"))
        previous_manager = app.state.credits
        app.state.credits = manager
        mock_client = AsyncMock()
        mock_client.get_vwap_latest = AsyncMock(
            return_value=VWAPData(
                pair="btc-usd",
                vwap=95_000,
                timestamp=datetime.now(timezone.utc),
                currency="USD",
            )
        )
        app.state.blocksize = mock_client
        settle = AsyncMock(
            return_value={
                "success": True,
                "transaction": "recovery-settlement",
                "network": settings.x402.solana_network,
            }
        )
        headers = {
            "PAYMENT-SIGNATURE": self._mock_payment_header("mock_lifecycle_recovery")
        }

        try:
            with patch("src.resource_server._settle_payment", settle), patch.object(
                manager,
                "finalize_payment_proof",
                return_value=False,
            ):
                response = test_client.get("/v1/vwap/btc-usd", headers=headers)
            replay = test_client.get("/v1/vwap/btc-usd", headers=headers)
        finally:
            app.state.credits = previous_manager

        assert response.status_code == 200
        assert replay.status_code == 200
        assert replay.content == response.content
        assert replay.headers["X-Payment-Replayed"] == "true"
        assert settle.await_count == 1
        assert mock_client.get_vwap_latest.await_count == 1
        assert manager.payment_proof_state("mock_lifecycle_recovery")["state"] == "finalized"

    def test_checkpoint_failure_withholds_data_and_records_reconciliation_alert(
        self,
        observability_store,
        test_client,
        tmp_path,
        monkeypatch,
    ):
        monkeypatch.setattr(settings.server, "x402_allow_mock_payments", True)
        manager = CreditManager(str(tmp_path / "lifecycle-unreconciled.db"))
        previous_manager = app.state.credits
        app.state.credits = manager
        mock_client = AsyncMock()
        mock_client.get_vwap_latest = AsyncMock(
            return_value=VWAPData(
                pair="btc-usd",
                vwap=95_000,
                timestamp=datetime.now(timezone.utc),
                currency="USD",
            )
        )
        app.state.blocksize = mock_client

        try:
            with patch.object(
                manager,
                "checkpoint_settled_payment",
                return_value=False,
            ):
                response = test_client.get(
                    "/v1/vwap/btc-usd",
                    headers={
                        "PAYMENT-SIGNATURE": self._mock_payment_header(
                            "mock_lifecycle_unreconciled"
                        )
                    },
                )
        finally:
            app.state.credits = previous_manager

        assert response.status_code == 503
        assert response.json()["error"] == "Payment Reconciliation Required"
        assert "data" not in response.json()
        assert manager.payment_proof_state("mock_lifecycle_unreconciled")["state"] == "pending"
        alerts = [
            event
            for event in observability_store.recent_events(limit=20)
            if event["event"] == "payment_settlement_unreconciled"
        ]
        assert len(alerts) == 1
        assert alerts[0]["metadata"]["payment_state"] == "settlement_unreconciled"
        assert alerts[0]["metadata"]["transaction_hash"]

    @pytest.mark.asyncio
    async def test_settlement_cancellation_quarantines_proof_and_blocks_resettle(
        self,
        test_client,
        observability_store,
        tmp_path,
    ):
        manager = CreditManager(str(tmp_path / "settlement-cancelled.db"))
        previous_manager = app.state.credits
        app.state.credits = manager
        reservation = manager.reserve_payment_proof(
            payment_id="settlement-cancelled-proof",
            network=settings.x402.base_network,
            amount_atomic=2000,
            recipient=settings.x402.evm_wallet_address,
            purpose="GET /v1/vwap/btc-usd",
            request_binding="settlement-cancelled-binding",
            attempt_id="reservation-attempt",
            lease_seconds=30,
            now=1000.0,
        )
        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "scheme": "https",
            "path": "/v1/vwap/btc-usd",
            "raw_path": b"/v1/vwap/btc-usd",
            "query_string": b"",
            "headers": [(b"payment-signature", b"signed-payment")],
            "client": ("203.0.113.51", 1234),
            "server": ("testserver", 443),
            "app": app,
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        async def successful(_request):
            return resource_server.JSONResponse({"data": {"price": 95_000}})

        verification = {
            "valid": True,
            "network": settings.x402.base_network,
            "payment_id": reservation.payment_id,
            "reservation_id": reservation.reservation_id,
            "request_binding": "settlement-cancelled-binding",
        }
        settle = AsyncMock(side_effect=asyncio.CancelledError())
        request = Request(scope, receive=receive)

        try:
            with patch(
                "src.resource_server._verify_payment",
                new_callable=AsyncMock,
                return_value=verification,
            ), patch("src.resource_server._settle_payment", settle):
                with pytest.raises(asyncio.CancelledError):
                    await resource_server.x402_payment_middleware(request, successful)
        finally:
            app.state.credits = previous_manager

        state = manager.payment_proof_state(reservation.payment_id)
        assert state["state"] == "settlement_unknown"
        assert state["settlement_unknown_at"]
        retry = manager.reserve_payment_proof(
            payment_id=reservation.payment_id,
            network=settings.x402.base_network,
            amount_atomic=2000,
            recipient=settings.x402.evm_wallet_address,
            purpose="GET /v1/vwap/btc-usd",
            request_binding="settlement-cancelled-binding",
            attempt_id="retry-attempt",
            lease_seconds=30,
            now=9999.0,
        )
        assert retry.acquired is False
        assert retry.reason == "payment_settlement_reconciliation_required"
        settle.assert_awaited_once()
        alerts = [
            event
            for event in observability_store.recent_events(limit=20)
            if event["event"] == "payment_settlement_unreconciled"
        ]
        assert len(alerts) == 1
        assert alerts[0]["reason"] == "settlement_cancelled_with_unknown_remote_outcome"
        assert alerts[0]["metadata"]["payment_state"] == "settlement_unknown"


# ---------------------------------------------------------------------------
# Data Endpoints (with mocked payment)
# ---------------------------------------------------------------------------

class TestDataEndpoints:
    @pytest.fixture(autouse=True)
    def _mock_payment(self):
        with patch(
            "src.resource_server._verify_payment",
            new_callable=AsyncMock,
            return_value={
                "valid": True,
                "network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
                "payment_id": "test-payment-id",
                "reservation_id": "test-reservation-id",
                "payer": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
            },
        ), patch(
            "src.resource_server._settle_payment",
            new_callable=AsyncMock,
            return_value={
                "success": True,
                "transaction": "2" * 88,
                "network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
            },
        ), patch.object(
            CreditManager, "checkpoint_settled_payment", return_value=True
        ), patch.object(CreditManager, "finalize_payment_proof", return_value=True):
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
        assert data["meta"]["citation"]["provider"] == "Blocksize"
        assert data["meta"]["citation"]["methodology_url"].endswith("/crypto-vwap-api")
        assert data["meta"]["citation"]["lineage"]["symbol"] == "BTCUSD"

    def test_vwap_endpoint_uses_starter_credits(self, test_client, tmp_path):
        mock_vwap = VWAPData(
            pair="btc-usd",
            vwap=95432.50,
            timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc),
            currency="USD",
        )
        mock_client = AsyncMock()
        mock_client.get_vwap_latest = AsyncMock(return_value=mock_vwap)
        app.state.blocksize = mock_client
        app.state.credits = CreditManager(str(tmp_path / "credits.db"))

        response = test_client.get(
            "/v1/vwap/btc-usd",
            headers={
                "X-AGENT-ID": "agent-starter-12345678",
                "X-DEVICE-ID": "device-starter-12345678",
                "X-SESSION-ID": "session-starter-12345678",
            },
        )

        assert response.status_code == 200
        assert response.headers["X-Blocksize-Credit-Mode"] == "starter-allowance"
        assert response.headers["X-Blocksize-Credits-Spent"] == "1.0"
        assert response.headers["X-Blocksize-Credits-Remaining"] == "49.0"
        data = response.json()
        assert data["meta"]["credits"]["credit_cost"] == 1.0
        assert data["meta"]["credits"]["credits_remaining"] == 49.0

    def test_state_endpoint_uses_state_pool_and_starter_credits(self, test_client, tmp_path):
        mock_client = AsyncMock()
        mock_client.get_state_price = AsyncMock(
            return_value=StatePriceData(
                pair="MSOLUSD",
                price=91.9,
                timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc),
            )
        )
        app.state.blocksize = mock_client
        app.state.credits = CreditManager(str(tmp_path / "credits.db"))

        response = test_client.get(
            "/v1/state/MSOLUSD",
            headers={"X-AGENT-ID": "agent-state-raw-12345678"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["data"]["pair"] == "MSOLUSD"
        assert data["data"]["price"] == 91.9
        assert data["meta"]["credits"]["credit_cost"] == 1.0
        assert data["meta"]["upstream_methods"] == ["state_instruments", "state_pool"]
        assert data["meta"]["citation"]["methodology_url"].endswith("/signed-oracle-feeds")

    def test_vwap30m_endpoint_uses_closingprice_and_starter_credits(self, test_client, tmp_path):
        mock_client = AsyncMock()
        mock_client.get_vwap_30min = AsyncMock(
            return_value=VWAP30MinData(
                ticker="SOL",
                vwap=75.27,
                quote_currency="USD",
                timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc),
            )
        )
        app.state.blocksize = mock_client
        app.state.credits = CreditManager(str(tmp_path / "credits.db"))

        response = test_client.get(
            "/v1/vwap30m/SOLUSD",
            headers={"X-AGENT-ID": "agent-vwap30m-raw-12345678"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["data"]["ticker"] == "SOL"
        assert data["data"]["vwap"] == 75.27
        assert data["methodology"]["upstream_method"] == "closingprice_list"
        assert data["meta"]["credits"]["credit_cost"] == 1.0

    def test_vwap24h_endpoint_returns_stream_cache_value(self, test_client, tmp_path):
        mock_client = AsyncMock()
        mock_cache = AsyncMock()
        mock_cache.enabled = True
        mock_cache.get_vwap_24h = AsyncMock(
            return_value=VWAP24HrData(
                pair="BTCUSD",
                vwap=66800.0,
                volume=1234.0,
                timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc),
                source="blocksize:fixedvwap_subscribe_cache",
            )
        )
        app.state.blocksize = mock_client
        app.state.stream_cache = mock_cache
        app.state.credits = CreditManager(str(tmp_path / "credits.db"))

        response = test_client.get(
            "/v1/vwap24h/BTCUSD",
            headers={"X-AGENT-ID": "agent-vwap24h-raw-12345678"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["data"]["vwap"] == 66800.0
        assert data["methodology"]["fallback"] is False
        assert data["methodology"]["upstream_method"] == "fixedvwap_subscribe"
        assert data["meta"]["credits"]["credit_cost"] == 1.0

    def test_agent_market_brief_uses_starter_credits(self, test_client, tmp_path):
        mock_client = AsyncMock()
        mock_client.get_vwap_latest = AsyncMock(
            return_value=VWAPData(
                pair="btc-usd",
                vwap=95432.50,
                timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc),
                currency="USD",
            )
        )
        app.state.blocksize = mock_client
        app.state.credits = CreditManager(str(tmp_path / "credits.db"))

        response = test_client.post(
            "/v1/briefs/market",
            headers={"X-AGENT-ID": "agent-brief-12345678"},
            json={"symbols": ["BTCUSD"], "intent": "demo"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "agent_market_brief"
        assert data["credit_cost"] == 10.0
        assert data["meta"]["credits"]["credits_remaining"] == 40.0
        assert data["provenance"]["receipt_id"].startswith("rcpt_")
        assert data["instruments"][0]["symbol"] == "BTCUSD"

    def test_pre_trade_sanity_check_returns_decision(self, test_client, tmp_path):
        mock_client = AsyncMock()
        mock_client.get_bidask_snapshot = AsyncMock(
            return_value=BidAskData(
                pair="btc-usd",
                bid=95400.0,
                ask=95450.0,
                spread=50.0,
                spread_pct=0.0524,
                timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc),
            )
        )
        app.state.blocksize = mock_client
        app.state.credits = CreditManager(str(tmp_path / "credits.db"))

        response = test_client.post(
            "/v1/checks/pre-trade",
            headers={"X-AGENT-ID": "agent-check-12345678"},
            json={
                "symbol": "BTCUSD",
                "side": "buy",
                "notional_usd": 2500,
                "reference_price": 95425.0,
                "max_spread_bps": 10,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "pre_trade_sanity_check"
        assert data["credit_cost"] == 5.0
        assert data["decision"] in {"pass", "caution", "block"}
        assert data["market"]["service"] == "bidask"
        assert data["meta"]["credits"]["credits_remaining"] == 45.0

    def test_audit_receipt_can_be_looked_up_for_free(self, test_client, tmp_path):
        mock_client = AsyncMock()
        mock_client.get_vwap_latest = AsyncMock(
            return_value=VWAPData(
                pair="btc-usd",
                vwap=95432.50,
                timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc),
                currency="USD",
            )
        )
        app.state.blocksize = mock_client
        app.state.credits = CreditManager(str(tmp_path / "credits.db"))

        receipt_response = test_client.post(
            "/v1/receipts/price",
            headers={"X-AGENT-ID": "agent-receipt-12345678"},
            json={"service": "vwap", "symbol": "BTCUSD", "purpose": "test"},
        )

        assert receipt_response.status_code == 200
        receipt_id = receipt_response.json()["receipt"]["receipt_id"]

        lookup_response = test_client.get(f"/v1/provenance/{receipt_id}")

        assert lookup_response.status_code == 200
        data = lookup_response.json()
        assert data["product"] == "agent_data_provenance"
        assert data["credit_cost"] == 0.0
        assert data["receipt"]["receipt_id"] == receipt_id
        assert data["receipt"]["citation"]["provider"] == "Blocksize"
        assert data["receipt"]["citation"]["receipt_url"].endswith(receipt_id)

    def test_macro_snapshot_supports_multi_asset_package(self, test_client, tmp_path):
        mock_client = AsyncMock()
        mock_client.get_vwap_latest = AsyncMock(
            return_value=VWAPData(
                pair="btc-usd",
                vwap=95432.50,
                timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc),
                currency="USD",
            )
        )
        mock_client.get_fx_rate = AsyncMock(
            return_value=FXData(
                pair="EURUSD",
                base_currency="EUR",
                quote_currency="USD",
                bid=1.08,
                ask=1.081,
                mid=1.0805,
                timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc),
            )
        )
        mock_client.get_metal_price = AsyncMock(
            return_value=MetalData(
                ticker="XAUUSD",
                name="Gold",
                price=2350.0,
                timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc),
            )
        )
        app.state.blocksize = mock_client
        app.state.credits = CreditManager(str(tmp_path / "credits.db"))

        response = test_client.post(
            "/v1/snapshots/macro",
            headers={"X-AGENT-ID": "agent-macro-12345678"},
            json={"universe": ["BTCUSD", "EURUSD", "XAUUSD"]},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "multi_asset_macro_snapshot"
        assert data["credit_cost"] == 25.0
        assert len(data["assets"]) == 3
        assert data["meta"]["credits"]["credits_remaining"] == 25.0

    def test_token_quality_indicator_uses_price_state_and_vwap_windows(self, test_client, tmp_path):
        mock_client = AsyncMock()
        mock_client.get_vwap_latest = AsyncMock(
            return_value=VWAPData(
                pair="sol-usd",
                vwap=150.0,
                timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc),
                currency="USD",
            )
        )
        mock_client.get_bidask_snapshot = AsyncMock(
            return_value=BidAskData(
                pair="sol-usd",
                bid=149.95,
                ask=150.05,
                spread=0.10,
                spread_pct=0.0667,
                timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc),
            )
        )
        mock_client.get_state_price = AsyncMock(
            return_value=StatePriceData(
                pair="sol-usd",
                price=149.90,
                timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc),
            )
        )
        mock_client.get_vwap_30min = AsyncMock(
            return_value=VWAP30MinData(
                ticker="SOL",
                vwap=149.80,
                quote_currency="USD",
                timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc),
            )
        )
        mock_client.get_vwap_24hr = AsyncMock(
            return_value=VWAP24HrData(
                pair="sol-usd",
                vwap=148.75,
                volume=1234567,
                timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc),
            )
        )
        mock_client.list_state_instruments = AsyncMock(
            return_value=[
                {
                    "symbol": "SOLUSD",
                    "pools": [
                        {"network": "solana", "address": "pool-1"},
                        {"network": "ethereum", "address": "pool-2"},
                    ],
                }
            ]
        )
        app.state.blocksize = mock_client
        app.state.credits = CreditManager(str(tmp_path / "credits.db"))

        response = test_client.post(
            "/v1/indicators/token-quality",
            headers={"X-AGENT-ID": "agent-token-quality-12345678"},
            json={
                "symbol": "SOLUSD",
                "include_state_coverage": True,
                "include_state_price": True,
                "include_windows": True,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "token_market_quality_indicator"
        assert data["credit_cost"] == 15.0
        assert data["indicator"]["symbol"] == "SOLUSD"
        assert data["indicator"]["metrics"]["state_divergence_bps"] == pytest.approx(6.6711, rel=1e-3)
        assert data["indicator"]["coverage"]["status"] == "full"
        assert data["indicator"]["metrics"]["state_solana_pool_count"] == 1
        assert data["meta"]["credits"]["credits_remaining"] == 35.0

    def test_state_divergence_indicator_returns_signed_basis(self, test_client, tmp_path):
        mock_client = AsyncMock()
        mock_client.get_vwap_latest = AsyncMock(
            return_value=VWAPData(
                pair="sol-usd",
                vwap=150.0,
                timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc),
                currency="USD",
            )
        )
        mock_client.get_bidask_snapshot = AsyncMock(
            return_value=BidAskData(
                pair="sol-usd",
                bid=149.9,
                ask=150.1,
                spread=0.2,
                spread_pct=0.1333,
                timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc),
            )
        )
        mock_client.get_state_price = AsyncMock(
            return_value=StatePriceData(
                pair="sol-usd",
                price=149.0,
                timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc),
            )
        )
        mock_client.list_state_instruments = AsyncMock(return_value=[{"symbol": "SOLUSD", "pools": []}])
        app.state.blocksize = mock_client
        app.state.credits = CreditManager(str(tmp_path / "credits.db"))

        response = test_client.post(
            "/v1/indicators/state-divergence",
            headers={"X-AGENT-ID": "agent-state-divergence-12345678"},
            json={"symbol": "SOLUSD", "max_divergence_bps": 50},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "state_divergence_indicator"
        assert data["credit_cost"] == 15.0
        assert data["state"]["label"] == "alert"
        assert data["basis"]["vwap_vs_state_bps"] == pytest.approx(67.114, rel=1e-3)
        assert data["meta"]["credits"]["credits_remaining"] == 35.0

    def test_solana_token_brief_rejects_unsupported_symbols_before_charge(self, test_client, tmp_path):
        mock_client = AsyncMock()
        mock_client.get_vwap_latest = AsyncMock(
            side_effect=[
                VWAPData(
                    pair="sol-usd",
                    vwap=150.0,
                    timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc),
                    currency="USD",
                ),
                resource_server.BlocksizeAPIError(-32000, "Unsupported ticker"),
            ]
        )
        mock_client.get_bidask_snapshot = AsyncMock(
            side_effect=[
                BidAskData(
                    pair="sol-usd",
                    bid=149.95,
                    ask=150.05,
                    spread=0.10,
                    spread_pct=0.0667,
                    timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc),
                ),
                resource_server.BlocksizeAPIError(-32000, "Unsupported ticker"),
            ]
        )
        mock_client.get_state_price = AsyncMock(side_effect=resource_server.BlocksizeAPIError(-32000, "State unavailable"))
        mock_client.get_vwap_30min = AsyncMock(side_effect=resource_server.BlocksizeAPIError(-32000, "30m unavailable"))
        mock_client.get_vwap_24hr = AsyncMock(side_effect=resource_server.BlocksizeAPIError(-32000, "24h unavailable"))
        mock_client.list_state_instruments = AsyncMock(
            return_value=[{"symbol": "SOLUSD", "pools": [{"network": "solana", "address": "pool-1"}]}]
        )
        app.state.blocksize = mock_client
        app.state.credits = CreditManager(str(tmp_path / "credits.db"))

        response = test_client.post(
            "/v1/signals/solana-token-brief",
            headers={"X-AGENT-ID": "agent-solana-brief-12345678"},
            json={"symbols": ["SOLUSD", "UNKNOWNUSD"]},
        )

        assert response.status_code == 404
        data = response.json()
        assert data["error_code"] == "UNSUPPORTED_INSTRUMENT"
        assert data["symbol"] == "UNKNOWNUSD"
        assert "no payment" in data["message"].lower()
        assert app.state.credits.get_balance("agent-solana-brief-12345678") == 0.0

    def test_trader_alpha_pack_can_spend_full_starter_allowance(self, test_client, tmp_path):
        mock_client = AsyncMock()
        mock_client.get_vwap_latest = AsyncMock(
            return_value=VWAPData(
                pair="btc-usd",
                vwap=95432.50,
                timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc),
                currency="USD",
            )
        )
        mock_client.get_bidask_snapshot = AsyncMock(
            return_value=BidAskData(
                pair="btc-usd",
                bid=95400.0,
                ask=95450.0,
                spread=50.0,
                spread_pct=0.0524,
                timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc),
            )
        )
        mock_client.get_state_price = AsyncMock(
            return_value=StatePriceData(
                pair="btc-usd",
                price=95430.0,
                timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc),
            )
        )
        mock_client.get_vwap_30min = AsyncMock(
            return_value=VWAP30MinData(
                ticker="BTC",
                vwap=95300.0,
                quote_currency="USD",
                timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc),
            )
        )
        mock_client.get_vwap_24hr = AsyncMock(
            return_value=VWAP24HrData(
                pair="btc-usd",
                vwap=94900.0,
                volume=1234,
                timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc),
            )
        )
        mock_client.list_state_instruments = AsyncMock(return_value=[{"symbol": "BTCUSD", "pools": []}])
        app.state.blocksize = mock_client
        app.state.credits = CreditManager(str(tmp_path / "credits.db"))

        response = test_client.post(
            "/v1/signals/trader-alpha-pack",
            headers={"X-AGENT-ID": "agent-alpha-pack-12345678"},
            json={"watchlist": ["BTCUSD"], "include_state_price": True, "include_windows": True},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "trader_alpha_pack"
        assert data["credit_cost"] == 50.0
        assert data["summary"]["best_quality_symbol"] == "BTCUSD"
        assert data["meta"]["credits"]["credits_remaining"] == 0.0

    def test_capability_check_reports_ready_and_optional_state_coverage(self, test_client):
        mock_client = AsyncMock()
        mock_client.list_vwap_instruments = AsyncMock(return_value=["SOLUSD", "PYTHUSD"])
        mock_client.list_bidask_instruments = AsyncMock(return_value=["SOLUSD", "PYTHUSD"])
        mock_client.list_state_instruments = AsyncMock(
            return_value=[
                {"symbol": "PYTHUSD", "pools": [{"network": "solana", "address": "pool-1"}]},
                {"symbol": "SOLVBTCUSD", "pools": [{"network": "solana", "address": "not-sol"}]},
            ]
        )
        app.state.blocksize = mock_client

        response = test_client.post(
            "/v1/capabilities/check",
            json={
                "product": "solana_token_brief",
                "symbols": ["SOLUSD", "PYTHUSD"],
                "include_state_coverage": True,
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["ready"] is True
        sol, pyth = data["symbols"]
        assert sol["symbol"] == "SOLUSD"
        assert sol["optional_feeds"]["state_instruments"]["available"] is False
        assert sol["optional_feeds"]["state_instruments"]["coverage"]["matched_count"] == 0
        assert pyth["symbol"] == "PYTHUSD"
        assert pyth["optional_feeds"]["state_instruments"]["available"] is True
        assert pyth["optional_feeds"]["state_instruments"]["coverage"]["solana_pool_count"] == 1
        assert data["opt_in_policy"]["optional_default_off"]

    def test_capability_check_marks_state_divergence_not_ready_without_state_pool_coverage(self, test_client):
        mock_client = AsyncMock()
        mock_client.list_vwap_instruments = AsyncMock(return_value=["SOLUSD"])
        mock_client.list_bidask_instruments = AsyncMock(return_value=["SOLUSD"])
        mock_client.list_state_instruments = AsyncMock(return_value=[])
        app.state.blocksize = mock_client

        response = test_client.post(
            "/v1/capabilities/check",
            json={"product": "state_divergence_indicator", "symbol": "SOLUSD"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["ready"] is False
        assert data["symbols"][0]["missing_required"] == ["state_pool"]
        assert data["symbols"][0]["required_feeds"]["state_pool"]["available"] is False

    def test_capability_check_marks_state_divergence_ready_with_state_pool_coverage(self, test_client):
        mock_client = AsyncMock()
        mock_client.list_vwap_instruments = AsyncMock(return_value=["MSOLUSD"])
        mock_client.list_bidask_instruments = AsyncMock(return_value=["MSOLUSD"])
        mock_client.list_state_instruments = AsyncMock(
            return_value=[{"symbol": "MSOLUSD", "pools": [{"network": "solana", "address": "pool-1"}]}]
        )
        app.state.blocksize = mock_client

        response = test_client.post(
            "/v1/capabilities/check",
            json={"product": "state_divergence_indicator", "symbol": "MSOLUSD"},
        )

        assert response.status_code == 200
        data = response.json()
        assert data["ready"] is True
        assert data["symbols"][0]["missing_required"] == []
        assert data["symbols"][0]["required_feeds"]["state_pool"]["available"] is True

    def test_premium_route_402_includes_starter_credit_cost(self, test_client):
        response = test_client.post(
            "/v1/briefs/market",
            json={"symbols": ["BTCUSD"]},
        )

        assert response.status_code == 402
        data = response.json()
        assert data["price_usdc"] == "0.25"
        assert data["starter_credits"]["credit_cost"] == 10.0

    def test_vwap_endpoint_accepts_x_payment_header(self, test_client):
        mock_vwap = VWAPData(
            pair="btc-usd",
            vwap=95432.50,
            timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc),
            currency="USD",
        )
        mock_client = AsyncMock()
        mock_client.get_vwap_latest = AsyncMock(return_value=mock_vwap)
        app.state.blocksize = mock_client

        response = test_client.get(
            "/v1/vwap/btc-usd",
            headers={
                "X-PAYMENT": "mock_sig",
                "Origin": "https://mcp.blocksize.info",
            },
        )

        assert response.status_code == 200
        assert response.headers["Access-Control-Allow-Origin"] == "https://mcp.blocksize.info"
        exposed = response.headers["Access-Control-Expose-Headers"].lower()
        assert "x-payment-response" in exposed
        assert "PAYMENT-RESPONSE" in response.headers
        assert "X-PAYMENT-RESPONSE" in response.headers

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
        assert data["meta"]["asset_class"] == "multi_asset"

    def test_bidask_equity_endpoint_marks_equity_metadata(self, test_client):
        mock_bidask = BidAskData(
            pair="AAPL",
            bid=181.4,
            ask=181.6,
            spread=0.2,
            spread_pct=0.1103,
            timestamp=datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc),
        )
        mock_client = AsyncMock()
        mock_client.get_bidask_snapshot = AsyncMock(return_value=mock_bidask)
        app.state.blocksize = mock_client

        response = test_client.get("/v1/bidask/AAPL", headers={"PAYMENT-SIGNATURE": "mock_sig"})

        assert response.status_code == 200
        data = response.json()
        assert data["data"]["pair"] == "AAPL"
        assert data["meta"]["asset_class"] == "equity"
        assert data["meta"]["equity_ticker"] == "AAPL"
        assert data["meta"]["route_family"] == "shared_bidask"
        mock_client.get_bidask_snapshot.assert_awaited_once_with("AAPL")
