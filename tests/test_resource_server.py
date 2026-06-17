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

from src import public_metadata, resource_server
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
    StatePriceData,
    VWAP24HrData,
    VWAP30MinData,
    VWAPData,
)
from src.observability import UsageEventStore, configure_global_store
from src.config import settings
from src.credit_manager import CreditManager
from src.public_metadata import GLAMA_MAINTAINER_EMAIL


@pytest.fixture
def test_client():
    """Create a FastAPI test client."""
    with TestClient(app) as client:
        yield client


@pytest.fixture
def observability_store(tmp_path, monkeypatch):
    """Route observability writes into a per-test SQLite database."""
    store = UsageEventStore(tmp_path / "usage_events.db")
    monkeypatch.setattr(resource_server, "OBSERVABILITY", store)
    configure_global_store(store)
    yield store
    configure_global_store(None)


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
        assert data["oauth_callback_url"].endswith("/anthropic/mcp/auth/callback")
        assert data["oauth_protected_resource_metadata"].endswith(
            "/.well-known/oauth-protected-resource/anthropic/mcp/"
        )
        assert data["oauth_authorization_server_metadata"].endswith(
            "/.well-known/oauth-authorization-server/anthropic/mcp"
        )
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

        assert paid_response.status_code == 404
        assert paid_response.json()["error_code"] == "ANTHROPIC_ONLY_MODE"
        assert public_mcp_response.status_code == 404
        assert anthropic_response.status_code != 404
        assert "PAYMENT-REQUIRED" not in anthropic_response.headers
        assert auth_metadata_response.status_code == 200
        assert auth_metadata_response.json()["issuer"].endswith("/anthropic/mcp")
        assert support_response.status_code == 200
        assert prompt_examples_response.status_code == 200

    def test_manifest_exposes_remote_mcp_url(self, test_client):
        response = test_client.get("/mcp/manifest.json")
        assert response.status_code == 200
        data = response.json()
        assert data["transport"]["type"] == "streamable-http"
        assert data["transport"]["url"].endswith("/mcp/server/")
        assert "repository" not in data["links"]

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
        assert data["authorization_servers"][0].endswith("/anthropic/mcp")
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
        assert data["registration_endpoint"].endswith("/anthropic/mcp/register")
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
        assert data["authorization_endpoint"].endswith("/anthropic/mcp/authorize")

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
        assert data["authorization_servers"][0].endswith("/anthropic/mcp")

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
        assert data["authorization_servers"][0].endswith("/anthropic/mcp")

    def test_cursor_mcp_endpoint_exists(self, test_client):
        response = test_client.get("/cursor/mcp", follow_redirects=False)
        assert response.status_code != 404
        assert response.status_code not in {307, 308}
        assert "PAYMENT-REQUIRED" not in response.headers

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
        assert data["authorization_servers"][0].endswith("/cursor/mcp")
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
        assert data["authorization_endpoint"].endswith("/cursor/mcp/authorize")
        assert data["token_endpoint"].endswith("/cursor/mcp/token")
        assert data["registration_endpoint"].endswith("/cursor/mcp/register")
        assert data["scopes_supported"] == ["email", "profile"]
        assert data["code_challenge_methods_supported"] == ["S256"]

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
        assert data["authorization_servers"][0].endswith("/cursor/mcp")
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
        assert data["registration_endpoint"].endswith("/cursor/mcp/register")

    def test_health_exposes_cursor_connector_metadata(self, test_client):
        response = test_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        cursor = data["cursor_connector"]

        assert cursor["mcp_url"].endswith("/cursor/mcp")
        assert cursor["tool_surface"] == "read-only"
        assert "get_vwap" in cursor["tool_costs"]
        assert data["links"]["cursor_mcp"].endswith("/cursor/mcp/")

    def test_health_exposes_anthropic_connector_metadata(self, test_client):
        response = test_client.get("/health")
        assert response.status_code == 200
        data = response.json()
        anthropic = data["anthropic_connector"]

        assert anthropic["mcp_url"].endswith("/anthropic/mcp")
        assert anthropic["tool_surface"] == "read-only"
        assert "get_vwap" in anthropic["tool_costs"]
        assert data["links"]["anthropic_mcp"].endswith("/anthropic/mcp/")
        assert data["links"]["claude_connector"].endswith("/claude-connector")

    def test_server_json_is_served(self, test_client):
        response = test_client.get("/server.json")
        assert response.status_code == 200
        data = response.json()
        assert data["name"] == "info.blocksize.mcp/agentic-payments"
        assert data["title"] == "Blocksize Real Time Market Data"
        assert "repository" not in data
        assert data["remotes"][0]["url"].endswith("/mcp/server/")

    def test_manifest_exposes_market_data_display_name_and_endpoint_builder(self, test_client):
        response = test_client.get("/mcp/manifest.json")
        assert response.status_code == 200
        data = response.json()

        assert data["name"] == "Blocksize Real Time Market Data"
        assert "real-time crypto" in data["description"]
        assert data["links"]["llms_txt"].endswith("/llms.txt")
        assert data["links"]["sitemap"].endswith("/sitemap.xml")
        assert data["links"]["data_packages_json"].endswith("/data-packages.json")
        assert data["capabilities"]["ai_reader_brief"].endswith("/llms.txt")
        assert data["capabilities"]["data_package_catalog"].endswith("/data-packages.json")
        assert any(tool["name"] == "get_market_data_endpoint" for tool in data["tools"])

    def test_crawler_and_ai_reader_files_are_served(self, test_client):
        portal_head = test_client.head("/")
        assert portal_head.status_code == 200

        robots = test_client.get("/robots.txt")
        assert robots.status_code == 200
        assert "Sitemap: https://mcp.blocksize.info/sitemap.xml" in robots.text
        assert "Allow: /llms.txt" in robots.text
        assert "Allow: /data-packages.json" in robots.text
        assert "Allow: /og/" in robots.text

        sitemap = test_client.get("/sitemap.xml")
        assert sitemap.status_code == 200
        assert "application/xml" in sitemap.headers["content-type"]
        assert "<loc>https://mcp.blocksize.info/</loc>" in sitemap.text
        assert "<loc>https://mcp.blocksize.info/llms.txt</loc>" in sitemap.text
        assert "<loc>https://mcp.blocksize.info/data-packages.json</loc>" in sitemap.text
        assert "<loc>https://mcp.blocksize.info/market-data-api-for-ai-agents</loc>" in sitemap.text
        assert "<loc>https://mcp.blocksize.info/crypto-vwap-api</loc>" in sitemap.text
        assert "<loc>https://mcp.blocksize.info/real-time-price-data-api</loc>" in sitemap.text
        assert "<loc>https://mcp.blocksize.info/mcp-market-data-server</loc>" in sitemap.text
        assert "<loc>https://mcp.blocksize.info/price-data-api-examples</loc>" in sitemap.text
        assert "<loc>https://mcp.blocksize.info/openapi.json</loc>" in sitemap.text

        llms = test_client.get("/llms.txt")
        assert llms.status_code == 200
        assert "Blocksize Real-Time Price Data for AI Agents" in llms.text
        assert "Remote MCP discovery server" in llms.text
        assert "real-time price data API" in llms.text
        assert "Data packages JSON" in llms.text
        assert "Crypto VWAP API" in llms.text
        assert "Real-Time Price Data API" in llms.text
        assert "MCP Market Data Server" in llms.text

    def test_data_package_catalog_and_intent_pages_are_served(self, test_client):
        catalog = test_client.get("/data-packages.json")
        assert catalog.status_code == 200
        data = catalog.json()
        assert data["canonical_url"].endswith("/data-packages.json")
        assert data["remote_mcp_server"].endswith("/mcp/server/")
        assert any(page["url"].endswith("/real-time-price-data-api") for page in data["intent_pages"])
        assert any(package["id"] == "crypto-vwap" for package in data["packages"])
        assert any(package["endpoint_template"] == "/v1/bidask/{pair}" for package in data["packages"])
        assert any(package["request_examples"] for package in data["packages"])
        assert data["indexing_submission"]["google_search_console"]["submit_sitemap"].endswith("/sitemap.xml")
        assert any(
            url.endswith("/mcp-market-data-server")
            for url in data["indexing_submission"]["bing_webmaster_tools"]["request_indexing"]
        )

        catalog_head = test_client.head("/data-packages.json")
        assert catalog_head.status_code == 200

        agent_page = test_client.get("/market-data-api-for-ai-agents")
        assert agent_page.status_code == 200
        assert "Market Data API for AI Agents" in agent_page.text
        assert "/data-packages.json" in agent_page.text
        assert "application/ld+json" in agent_page.text

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
        maintainer = glama.json()["maintainers"][0]
        assert maintainer["email"] == GLAMA_MAINTAINER_EMAIL

        registry_auth = test_client.get("/.well-known/mcp-registry-auth")
        assert registry_auth.status_code == 200
        assert registry_auth.text.startswith("v=MCPv1; k=ed25519; p=")

        x402 = test_client.get("/.well-known/x402")
        assert x402.status_code == 200
        x402_data = x402.json()
        assert x402_data["version"] == 1
        assert "/v1/vwap/BTC-USD" in x402_data["resources"][0]
        assert any(resource.endswith("/v1/bidask/AAPL") for resource in x402_data["resources"])

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
        assert req_json["accepts"][0]["resource"] == resource_url
        assert req_json["accepts"][0]["extra"]["resource"] == resource_url
        assert "bazaar" in req_json["extensions"]

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
        assert data["starter_credits"]["positioning"] == "Start with 50 live data credits"
        assert data["starter_credits"]["allowance_credits"] == 50.0
        assert "networks" in data
        assert "accepts" in data
        assert data["accepts"][0]["resource"] == data["resource"]["url"]
        assert data["accepts"][0]["extra"]["resource"] == data["resource"]["url"]
        assert "legacy_requirements" in data

    def test_search_is_free(self, test_client):
        """Search endpoint should NOT require payment."""
        # Set up mock client since search actually tries to call blocksize
        mock_client = AsyncMock()
        mock_client.search_pairs = AsyncMock(return_value=[])
        app.state.blocksize = mock_client

        response = test_client.get("/v1/search?q=btc")
        assert response.status_code == 200

    def test_search_accepts_equity_filter(self, test_client):
        mock_client = AsyncMock()
        mock_client.search_pairs = AsyncMock(return_value=[])
        app.state.blocksize = mock_client

        response = test_client.get("/v1/search?q=AAPL&asset_class=equity")

        assert response.status_code == 200
        mock_client.search_pairs.assert_awaited_once_with("AAPL", "equity")

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

    def test_products_catalog_is_free(self, test_client):
        response = test_client.get("/v1/products")

        assert response.status_code == 200
        data = response.json()
        assert data["starter_allowance"]["positioning"] == "Start with 50 live data credits"
        assert data["credit_costs"]["market_brief"] == 10.0


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


class TestObservabilityDashboard:
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
        smithery_response = test_client.get(
            "/server.json",
            headers={"Referer": "https://smithery.ai/server/blocksize"},
        )

        assert glama_response.status_code == 200
        assert pay_response.status_code == 200
        assert smithery_response.status_code == 200

        stats = observability_store.summarize(days=1)
        assert stats["registry_source_mix"]["Glama"] == 1
        assert stats["registry_source_mix"]["Pay.sh"] == 1
        assert stats["registry_source_mix"]["Smithery"] == 1
        assert stats["registry_mix"]["/.well-known/glama.json"] == 1
        assert stats["registry_mix"]["/server.json"] == 2
        assert stats["timeline"][0]["registry_sources"]["Glama"] == 1
        assert stats["timeline"][0]["registry_sources"]["Pay.sh"] == 1
        assert stats["timeline"][0]["registry_sources"]["Smithery"] == 1

    def test_stats_include_smithery_external_context(
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
            return_value={"valid": True, "network": "solana"},
        ), patch(
            "src.resource_server._settle_payment",
            new_callable=AsyncMock,
            return_value={"success": True},
        ):
            response = test_client.get(
                "/v1/vwap/btc-usd",
                headers={"X-PAYMENT": "mock_sig"},
            )

        assert response.status_code == 200
        stats = observability_store.summarize(days=1)
        assert stats["event_counts"]["payment_proof_submitted"] == 1
        assert stats["event_counts"]["payment_verified"] == 1
        assert stats["overview"]["paid_calls"] == 1
        assert stats["overview"]["estimated_revenue_usdc"] == float(settings.pricing.core_crypto)
        assert stats["paid_endpoint_mix"]["/v1/vwap/{pair}"] == 1
        assert stats["service_mix"]["vwap"] == 1
        assert stats["data_called"][0]["paid_successes"] == 1
        assert stats["data_called"][0]["revenue_usdc"] == float(settings.pricing.core_crypto)
        assert stats["data_called"][0]["latest_outcome"] == "Data returned after payment or credits"

    def test_dashboard_token_can_protect_internal_stats(
        self,
        observability_store,
        test_client,
        monkeypatch,
    ):
        monkeypatch.setattr(settings.server, "observability_dashboard_token", "secret")

        login_response = test_client.get("/internal/observability")
        assert login_response.status_code == 401
        assert "Internal Observability" in login_response.text
        assert "Open Dashboard" in login_response.text

        dashboard_response = test_client.get("/internal/observability?token=secret")
        assert dashboard_response.status_code == 200
        assert "Product Usage Command Center" in dashboard_response.text
        assert "observability_token" in dashboard_response.headers["set-cookie"]

        stats_response = test_client.get(
            "/internal/observability/stats",
            headers={"Authorization": "Bearer secret"},
        )
        assert stats_response.status_code == 200

        cookie_stats_response = test_client.get("/internal/observability/stats")
        assert cookie_stats_response.status_code == 200

    def test_command_center_subpage_serves_improved_dashboard(
        self,
        observability_store,
        test_client,
    ):
        response = test_client.get("/internal/observability/command-center")

        assert response.status_code == 200
        assert "Product Usage Command Center" in response.text
        assert "Called Data Detail" in response.text
        assert "Recent Event Trace" in response.text
        assert "Glama" in response.text
        assert "Pay.sh" in response.text
        assert "MCP Registry" in response.text
        assert "Smithery" in response.text
        assert "Awesome MCP" in response.text
        assert "Smithery Hosted Activity" in response.text
        assert "renderSmitherySource" in response.text
        assert "registrySourceWatchlist" in response.text
        assert 'id="timeline-dates"' in response.text
        assert "timelineTip" in response.text
        assert "data-tip" in response.text
        assert "Token not configured" in response.text


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

    def test_solana_token_brief_reports_supported_and_unsupported_symbols(self, test_client, tmp_path):
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

        assert response.status_code == 200
        data = response.json()
        assert data["product"] == "solana_token_brief"
        assert data["credit_cost"] == 25.0
        assert data["summary"]["coverage_status"] == "partial"
        assert data["tokens"][1]["status"] == "unsupported_or_unavailable"
        assert data["meta"]["credits"]["credits_remaining"] == 25.0

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
