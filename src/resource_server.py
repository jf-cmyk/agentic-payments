"""
FastAPI Resource Server with x402 Payment Middleware.

This server gates Blocksize Capital data endpoints behind the x402
payment protocol. Supports tiered pricing and dual-network settlement
(Solana primary, Base L2 fallback).

Endpoints:
  GET /v1/vwap/{pair}             — Real-time VWAP (crypto tier pricing)
  GET /v1/bidask/{pair}           — Bid/Ask snapshot (shared upstream namespace)
  GET /v1/fx/{pair}               — FX rate ($0.005)
  GET /v1/metal/{ticker}          — Metal price ($0.005)
  GET /v1/search?q={query}        — Pair search (FREE)
  GET /v1/instruments/{service}   — Instrument list (FREE)
  GET /health                     — Health check (FREE)
"""

from __future__ import annotations

import os
import base64
import binascii
import json
import logging
import re
import secrets
import time
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Deque

import httpx
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles

from src.blocksize_client import (
    FIAT_CURRENCIES,
    METAL_TICKERS,
    BlocksizeAPIError,
    BlocksizeClient,
)
from src.blocksize_stream_cache import BlocksizeStreamCache
from src.config import TOP_250_CRYPTO, settings
from src.credit_manager import (
    CREDIT_COSTS,
    STARTER_CREDIT_ALLOWANCE,
    CreditManager,
    BULK_TIERS,
)
from src.models import (
    BidAskResponse,
    ErrorResponse,
    InstrumentListResponse,
    PairSearchResponse,
    VWAPResponse,
)
from src.observability import (
    UsageEventStore,
    configure_global_store,
    fingerprint,
    record_usage_event,
    registry_name_for_path,
    surface_for_path,
)
from src.public_metadata import (
    AGENT_MANUAL_URL,
    APP_VERSION,
    CLAUDE_CONNECTOR_URL,
    DATA_CATALOG_URL,
    DATA_PACKAGES_JSON_URL,
    GLAMA_MAINTAINER_EMAIL,
    GLAMA_WELL_KNOWN_URL,
    LLMS_TXT_URL,
    MCP_MANIFEST_URL,
    MCP_REGISTRY_AUTH_CONTENT,
    MCP_REGISTRY_AUTH_URL,
    OPENAPI_URL,
    PRICING_GUIDE_URL,
    PRIVACY_POLICY_URL,
    PROMPT_EXAMPLES_URL,
    PUBLIC_BASE_URL,
    PUBLIC_DESCRIPTION,
    PUBLIC_DISPLAY_NAME,
    QUICKSTART_URL,
    REMOTE_MCP_PATH,
    REMOTE_MCP_URL,
    REPOSITORY_URL,
    ROBOTS_URL,
    SERVER_JSON_URL,
    SEO_LANDING_PAGES,
    SITEMAP_URL,
    SUPPORT_URL,
    SWAGGER_URL,
    USER_FLOW_URL,
    build_data_packages_json,
    build_llms_txt,
    build_open_graph_svg,
    build_robots_txt,
    build_server_json,
    build_seo_landing_page,
    build_sitemap_xml,
)
from src import anthropic_auth
from src import cursor_auth
from src.anthropic_mcp_server import TOOL_COSTS as ANTHROPIC_TOOL_COSTS
from src.anthropic_mcp_server import anthropic_mcp
from src.cursor_mcp_server import TOOL_COSTS as CURSOR_TOOL_COSTS
from src.cursor_mcp_server import cursor_mcp
from src.public_mcp_server import public_mcp

logger = logging.getLogger(__name__)
DOCS_DIR = Path("docs")
PUBLIC_MCP_HTTP_APP = public_mcp.http_app(path="/", transport="streamable-http")
ANTHROPIC_MCP_HTTP_APP = anthropic_mcp.http_app(path="/", transport="streamable-http")
CURSOR_MCP_HTTP_APP = cursor_mcp.http_app(path="/", transport="streamable-http")
OBSERVABILITY = (
    UsageEventStore(settings.server.observability_db_path)
    if settings.server.observability_enabled
    else None
)
configure_global_store(OBSERVABILITY)
SMITHERY_LISTING_URL = os.getenv(
    "SMITHERY_LISTING_URL",
    "https://smithery.ai/servers/blocksize/agentic-payments#performance",
)
SMITHERY_HOSTED_MCP_ENDPOINT = os.getenv(
    "SMITHERY_HOSTED_MCP_ENDPOINT",
    "https://agentic-payments--blocksize.run.tools",
)
SMITHERY_METRICS_API_URL = os.getenv("SMITHERY_METRICS_API_URL", "").strip()


class _SlashlessMountEndpoint:
    """Forward mount-root requests into a mounted app without a slash redirect."""

    def __init__(self, mounted_app: Any, mount_path: str) -> None:
        self.mounted_app = mounted_app
        self.mount_path = mount_path.rstrip("/")

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        root_path = scope.get("root_path", "")
        mounted_root_path = f"{root_path}{self.mount_path}"
        child_scope = dict(scope)
        child_scope["app_root_path"] = scope.get("app_root_path", root_path)
        child_scope["root_path"] = mounted_root_path
        child_scope["path"] = f"{mounted_root_path}/"
        if "raw_path" in child_scope:
            child_scope["raw_path"] = child_scope["path"].encode()
        await self.mounted_app(child_scope, receive, send)


# ---------------------------------------------------------------------------
# Application Lifecycle
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Manage the Blocksize client and Credit manager lifecycle."""
    app.state.blocksize = BlocksizeClient()
    app.state.stream_cache = BlocksizeStreamCache(rest_client=app.state.blocksize)
    app.state.credits = CreditManager()
    logger.info("Blocksize MCP Resource Server starting (with Credit Drawdown engine)")
    logger.info("Solana wallet configured: %s", bool(settings.x402.solana_wallet_address))
    logger.info("Base wallet configured: %s", bool(settings.x402.evm_wallet_address))
    await app.state.stream_cache.start()
    async with PUBLIC_MCP_HTTP_APP.lifespan(PUBLIC_MCP_HTTP_APP):
        async with ANTHROPIC_MCP_HTTP_APP.lifespan(ANTHROPIC_MCP_HTTP_APP):
            async with CURSOR_MCP_HTTP_APP.lifespan(CURSOR_MCP_HTTP_APP):
                yield
    await app.state.stream_cache.stop()
    await app.state.blocksize.close()
    logger.info("Blocksize MCP Resource Server shut down")


app = FastAPI(
    title=PUBLIC_DISPLAY_NAME,
    version=APP_VERSION,
    description=f"""
Institutional-grade real-time market data gateway for autonomous AI agents.
Supports x402 USDC settlement, wallet credits, and a public read-only remote
MCP discovery surface for directory listings and client onboarding.

### Public Integration Surfaces
- **Developer Portal**: [Homepage]({PUBLIC_BASE_URL}/)
- **Remote MCP URL**: [Streamable HTTP]({REMOTE_MCP_URL})
- **MCP Manifest**: [Listing metadata]({MCP_MANIFEST_URL})
- **OpenAPI**: [JSON schema]({OPENAPI_URL})
- **Swagger UI**: [Interactive docs]({SWAGGER_URL})
- **Quickstart**: [Remote MCP install guide]({QUICKSTART_URL})
- **Prompt Examples**: [Example prompts]({PROMPT_EXAMPLES_URL})
- **Privacy Policy**: [Privacy]({PRIVACY_POLICY_URL})
- **Support**: [Contact and troubleshooting]({SUPPORT_URL})
    """,
    lifespan=lifespan
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.server.cors_origins,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
    expose_headers=["PAYMENT-REQUIRED", "PAYMENT-RESPONSE", "X-PAYMENT-RESPONSE"],
)


X402_EXPOSE_HEADERS = "PAYMENT-REQUIRED, PAYMENT-RESPONSE, X-PAYMENT-RESPONSE"
SECURITY_HEADERS = {
    "Strict-Transport-Security": "max-age=31536000; includeSubDomains",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "strict-origin-when-cross-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
}


def _apply_security_headers(response: Any) -> Any:
    for name, value in SECURITY_HEADERS.items():
        response.headers.setdefault(name, value)
    return response


def _apply_x402_cors_headers(request: Request, response: JSONResponse) -> JSONResponse:
    """Expose payment challenge details on early paid-route responses."""
    _apply_security_headers(response)
    response.headers.setdefault("Cache-Control", "no-store")

    origin = request.headers.get("origin")
    if not origin:
        return response

    allowed_origins = settings.server.cors_origins
    if "*" in allowed_origins:
        response.headers["Access-Control-Allow-Origin"] = "*"
    elif origin in allowed_origins:
        response.headers["Access-Control-Allow-Origin"] = origin
    else:
        return response

    response.headers["Access-Control-Expose-Headers"] = X402_EXPOSE_HEADERS
    vary = response.headers.get("Vary")
    if vary:
        vary_items = {item.strip().lower() for item in vary.split(",")}
        if "origin" not in vary_items:
            response.headers["Vary"] = f"{vary}, Origin"
    else:
        response.headers["Vary"] = "Origin"
    return response


def _request_client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",", 1)[0].strip()
    return request.client.host if request.client else "unknown"


def _wallet_hash(wallet: str | None) -> str | None:
    return fingerprint(wallet.strip()) if wallet else None


def _endpoint_label(path: str) -> str:
    if path.startswith("/v1/vwap/"):
        return "/v1/vwap/{pair}"
    if path.startswith("/v1/bidask/"):
        return "/v1/bidask/{pair}"
    if path.startswith("/v1/fx/"):
        return "/v1/fx/{pair}"
    if path.startswith("/v1/metal/"):
        return "/v1/metal/{ticker}"
    if path.startswith("/v1/instruments/"):
        return "/v1/instruments/{service}"
    if path.startswith("/v1/credits/balance/"):
        return "/v1/credits/balance/{wallet}"
    if path == "/mcp/server" or path.startswith("/mcp/server/"):
        return "/mcp/server"
    if path == "/anthropic/mcp" or path.startswith("/anthropic/mcp/"):
        return "/anthropic/mcp"
    if path == "/cursor/mcp" or path.startswith("/cursor/mcp/"):
        return "/cursor/mcp"
    return path


def _subject_for_request(request: Request) -> str | None:
    path = request.url.path
    if path == "/v1/search":
        return request.query_params.get("q")
    if path == "/v1/batch":
        return request.query_params.get("reqs")
    if path.startswith("/v1/instruments/"):
        return path.rstrip("/").rsplit("/", 1)[-1]
    if path.startswith(("/v1/vwap/", "/v1/bidask/", "/v1/fx/", "/v1/metal/")):
        return path.rstrip("/").rsplit("/", 1)[-1].upper()
    if path.startswith("/v1/credits/purchase"):
        return request.query_params.get("tier")
    return None


def _asset_class_for_request(request: Request) -> str | None:
    path = request.url.path
    if path.startswith("/v1/vwap/"):
        return "crypto"
    if path.startswith("/v1/bidask/"):
        subject = _subject_for_request(request) or ""
        return "equity" if _looks_like_equity_bidask_symbol(subject) else "multi_asset"
    if path.startswith("/v1/fx/"):
        return "fx"
    if path.startswith("/v1/metal/"):
        return "metal"
    if path == "/v1/search":
        return request.query_params.get("asset_class") or "all"
    return None


def _request_event_fields(
    request: Request,
    *,
    status_code: int | None = None,
    latency_ms: float | None = None,
) -> dict[str, Any]:
    return {
        "surface": surface_for_path(request.url.path),
        "endpoint": _endpoint_label(request.url.path),
        "method": request.method.upper(),
        "status_code": status_code,
        "latency_ms": latency_ms,
        "ip_hash": fingerprint(_request_client_ip(request)),
        "user_agent": request.headers.get("user-agent"),
        "referrer": request.headers.get("referer") or request.headers.get("referrer"),
        "wallet_hash": _wallet_hash(request.headers.get("X-AGENT-WALLET")),
        "subject": _subject_for_request(request),
        "asset_class": _asset_class_for_request(request),
    }


def _record_http_usage(request: Request, status_code: int, latency_ms: float) -> None:
    if request.url.path.startswith("/internal/observability"):
        return

    fields = _request_event_fields(
        request,
        status_code=status_code,
        latency_ms=latency_ms,
    )
    record_usage_event("http_request", **fields)

    registry_name = registry_name_for_path(request.url.path)
    if registry_name:
        record_usage_event(
            "registry_request",
            **fields,
            metadata={"registry": registry_name},
        )
    elif _is_discovery_rate_limited_path(request.url.path):
        record_usage_event("free_discovery_call", **fields)


def _record_product_event(
    event: str,
    request: Request,
    *,
    price_usdc: Decimal | float | str | None = None,
    network: str | None = None,
    reason: str | None = None,
    wallet_hash: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> None:
    fields = _request_event_fields(request)
    if wallet_hash is not None:
        fields["wallet_hash"] = wallet_hash
    record_usage_event(
        event,
        **fields,
        price_usdc=str(price_usdc) if price_usdc is not None else None,
        network=network,
        reason=reason,
        metadata=metadata,
    )


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    response = await call_next(request)
    return _apply_security_headers(response)


def _anthropic_only_mode() -> bool:
    value = os.environ.get("ANTHROPIC_ONLY_MODE", "").strip().lower()
    return value in {"1", "true", "yes", "on"}


def _root_oauth_connector() -> str:
    """Choose the connector advertised by the root OAuth metadata endpoint."""
    value = os.environ.get("ROOT_OAUTH_CONNECTOR", "anthropic").strip().lower()
    if value in {"anthropic", "claude"}:
        return "anthropic"
    if value == "cursor":
        return "cursor"
    logger.warning(
        "Invalid ROOT_OAUTH_CONNECTOR=%r; defaulting root OAuth metadata to Anthropic",
        value,
    )
    return "anthropic"


def _anthropic_only_allowed_path(path: str) -> bool:
    clean_path = path.rstrip("/") or "/"
    allowed_exact_paths = {
        "/health",
        "/privacy",
        "/prompt-examples",
        "/support",
        "/claude-connector",
        "/robots.txt",
        "/sitemap.xml",
        "/llms.txt",
        "/data-packages.json",
        "/server.json",
        "/mcp/manifest.json",
        "/favicon.ico",
        "/favicon.svg",
        "/apple-touch-icon.png",
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-authorization-server/anthropic/mcp",
        "/.well-known/openid-configuration/anthropic/mcp",
        "/.well-known/oauth-protected-resource/anthropic/mcp",
    }
    seo_paths = {f"/{slug}" for slug in SEO_LANDING_PAGES}
    return (
        clean_path in allowed_exact_paths
        or clean_path in seo_paths
        or clean_path.startswith("/anthropic/mcp")
        or clean_path.startswith("/og/")
    )


def _anthropic_only_block_response() -> JSONResponse:
    return JSONResponse(
        status_code=404,
        content={
            "status": "error",
            "error_code": "ANTHROPIC_ONLY_MODE",
            "message": "This deployment exposes only the Anthropic-safe MCP endpoint.",
        },
    )

# Serve the Developer Portal as the main landing page
@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
async def get_portal():
    """Serve the institutional developer portal."""
    portal_path = DOCS_DIR / "developer_portal.html"
    if portal_path.exists():
        return FileResponse(
            portal_path,
            headers={"Cache-Control": "no-store, max-age=0"},
        )
    raise HTTPException(status_code=404, detail="Developer Portal not found")


@app.api_route("/favicon.ico", methods=["GET", "HEAD"], include_in_schema=False)
async def get_favicon_ico():
    """Serve the root favicon for browser and directory crawlers."""
    return FileResponse(DOCS_DIR / "assets" / "favicon.ico", media_type="image/x-icon")


@app.api_route("/favicon.svg", methods=["GET", "HEAD"], include_in_schema=False)
async def get_favicon_svg():
    """Serve the square Blocksize mark as an SVG favicon."""
    return FileResponse(DOCS_DIR / "assets" / "logo-square.svg", media_type="image/svg+xml")


@app.api_route("/apple-touch-icon.png", methods=["GET", "HEAD"], include_in_schema=False)
async def get_apple_touch_icon():
    """Serve the square Blocksize mark for touch icons."""
    return FileResponse(DOCS_DIR / "assets" / "favicon.png", media_type="image/png")


def _serve_doc(filename: str, description: str) -> FileResponse:
    """Serve a static documentation page from the docs directory."""
    path = DOCS_DIR / filename
    if not path.exists():
        raise HTTPException(status_code=404, detail=f"{description} not found")
    return FileResponse(path)


@app.api_route("/quickstart/remote-mcp", methods=["GET", "HEAD"], include_in_schema=False)
async def get_remote_quickstart():
    """Serve the public remote MCP quickstart page."""
    return _serve_doc("remote_mcp_quickstart.html", "Remote MCP quickstart")


@app.api_route("/prompt-examples", methods=["GET", "HEAD"], include_in_schema=False)
async def get_prompt_examples():
    """Serve prompt examples for reviewers and users."""
    return _serve_doc("prompt_examples.html", "Prompt examples")


@app.api_route("/privacy", methods=["GET", "HEAD"], include_in_schema=False)
async def get_privacy_policy():
    """Serve the privacy policy page."""
    return _serve_doc("privacy_policy.html", "Privacy policy")


@app.api_route("/support", methods=["GET", "HEAD"], include_in_schema=False)
async def get_support_page():
    """Serve the support and troubleshooting page."""
    return _serve_doc("support.html", "Support page")


@app.get("/terms", include_in_schema=False)
async def get_terms_page():
    """Send reviewers to the published Blocksize data terms."""
    return RedirectResponse("https://blocksize.info/terms-conditions-data/")


@app.api_route("/claude-connector", methods=["GET", "HEAD"], include_in_schema=False)
async def get_claude_connector_page():
    """Serve Claude connector setup and review documentation."""
    return _serve_doc("claude_connector.html", "Claude connector page")


@app.api_route("/server.json", methods=["GET", "HEAD"], include_in_schema=False)
async def get_server_json():
    """Serve the official MCP Registry metadata file."""
    return JSONResponse(build_server_json())


@app.api_route("/robots.txt", methods=["GET", "HEAD"], include_in_schema=False)
async def get_robots_txt() -> PlainTextResponse:
    """Serve crawler guidance and sitemap discovery."""
    return PlainTextResponse(build_robots_txt(), media_type="text/plain; charset=utf-8")


@app.api_route("/sitemap.xml", methods=["GET", "HEAD"], include_in_schema=False)
async def get_sitemap_xml() -> PlainTextResponse:
    """Serve canonical public URL discovery for search engines."""
    return PlainTextResponse(
        build_sitemap_xml(),
        media_type="application/xml; charset=utf-8",
    )


@app.api_route("/llms.txt", methods=["GET", "HEAD"], include_in_schema=False)
async def get_llms_txt() -> PlainTextResponse:
    """Serve a compact AI-reader brief for retrieval and citation systems."""
    return PlainTextResponse(build_llms_txt(), media_type="text/plain; charset=utf-8")


@app.api_route("/data-packages.json", methods=["GET", "HEAD"], include_in_schema=False)
async def get_data_packages_json() -> JSONResponse:
    """Serve the machine-readable Blocksize data package catalog."""
    return JSONResponse(build_data_packages_json())


@app.get("/v1/products")
async def get_products() -> dict[str, Any]:
    """Serve raw data and premium workflow product catalog. FREE."""
    catalog = build_data_packages_json()
    return {
        "status": "ok",
        "starter_allowance": {
            "positioning": "Start with 50 live data credits",
            "allowance_credits": STARTER_CREDIT_ALLOWANCE,
            "not_free_forever": True,
            "upgrade_path": "x402 payment or prepaid credit top-ups",
        },
        "credit_costs": CREDIT_COSTS,
        "catalog": catalog,
    }


async def get_open_graph_svg(request: Request) -> Response:
    """Serve lightweight social preview artwork for high-intent pages."""
    filename = request.path_params.get("filename", "")
    slug = str(filename).removesuffix(".svg")
    if slug not in SEO_LANDING_PAGES:
        raise HTTPException(status_code=404, detail="OpenGraph image not found")
    return Response(
        build_open_graph_svg(slug),
        media_type="image/svg+xml; charset=utf-8",
    )


app.add_api_route(
    "/og/{filename}",
    get_open_graph_svg,
    methods=["GET", "HEAD"],
    include_in_schema=False,
)


async def get_seo_landing_page(request: Request) -> HTMLResponse:
    """Serve an additive high-intent landing page without changing the homepage."""
    slug = request.url.path.strip("/")
    if slug not in SEO_LANDING_PAGES:
        raise HTTPException(status_code=404, detail="SEO landing page not found")
    return HTMLResponse(build_seo_landing_page(slug))


for _seo_slug in SEO_LANDING_PAGES:
    app.add_api_route(
        f"/{_seo_slug}",
        get_seo_landing_page,
        methods=["GET", "HEAD"],
        include_in_schema=False,
    )


@app.get("/.well-known/glama.json", include_in_schema=False)
async def get_glama_well_known() -> dict[str, object]:
    """Serve the Glama connector claim file."""
    return {
        "$schema": "https://glama.ai/mcp/schemas/connector.json",
        "maintainers": [{"email": GLAMA_MAINTAINER_EMAIL}],
    }


@app.get("/.well-known/mcp-registry-auth", include_in_schema=False)
async def get_mcp_registry_auth() -> PlainTextResponse:
    """Serve the MCP Registry HTTP domain verification file."""
    return PlainTextResponse(MCP_REGISTRY_AUTH_CONTENT)


@app.get("/.well-known/x402", include_in_schema=False)
async def get_x402_well_known() -> dict[str, object]:
    """Serve x402scan-compatible paid resource discovery."""
    return {
        "version": 1,
        "resources": X402_WELL_KNOWN_RESOURCES,
        "instructions": (
            "Register the listed paid HTTP endpoints individually. "
            "Public MCP discovery remains available at /mcp/server/."
        ),
    }


def _connector_mcp_url(env_var: str, default_path: str) -> str:
    return os.environ.get(
        env_var,
        f"{PUBLIC_BASE_URL.rstrip('/')}{default_path}",
    ).rstrip("/")


def _oauth_protected_resource_metadata(
    *,
    mcp_url: str,
    scopes: list[str],
) -> dict[str, object]:
    return {
        "resource": f"{mcp_url}/",
        "authorization_servers": [mcp_url],
        "scopes_supported": scopes,
        "bearer_methods_supported": ["header"],
    }


def _oauth_authorization_server_metadata(
    *,
    mcp_url: str,
    scopes: list[str],
) -> dict[str, object]:
    return {
        "issuer": mcp_url,
        "authorization_endpoint": f"{mcp_url}/authorize",
        "token_endpoint": f"{mcp_url}/token",
        "registration_endpoint": f"{mcp_url}/register",
        "scopes_supported": scopes,
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "token_endpoint_auth_methods_supported": [
            "client_secret_post",
            "client_secret_basic",
        ],
        "code_challenge_methods_supported": ["S256"],
        "client_id_metadata_document_supported": True,
    }


def _anthropic_mcp_url() -> str:
    return _connector_mcp_url("ANTHROPIC_MCP_PUBLIC_URL", "/anthropic/mcp")


def _cursor_mcp_url() -> str:
    return _connector_mcp_url("CURSOR_MCP_PUBLIC_URL", "/cursor/mcp")


@app.get("/.well-known/oauth-protected-resource/anthropic/mcp", include_in_schema=False)
@app.get("/.well-known/oauth-protected-resource/anthropic/mcp/", include_in_schema=False)
async def get_anthropic_oauth_protected_resource_metadata() -> dict[str, object]:
    """Serve Claude MCP OAuth protected-resource metadata at the challenged URL."""
    return _oauth_protected_resource_metadata(
        mcp_url=_anthropic_mcp_url(),
        scopes=anthropic_auth.oauth_scopes(),
    )


@app.get("/.well-known/oauth-protected-resource", include_in_schema=False)
async def get_root_oauth_protected_resource_metadata() -> dict[str, object]:
    """Serve root protected-resource metadata for clients that ignore path scope."""
    if _anthropic_only_mode() or _root_oauth_connector() == "anthropic":
        return _oauth_protected_resource_metadata(
            mcp_url=_anthropic_mcp_url(),
            scopes=anthropic_auth.oauth_scopes(),
        )
    return _oauth_protected_resource_metadata(
        mcp_url=_cursor_mcp_url(),
        scopes=cursor_auth.oauth_scopes(),
    )


@app.get("/.well-known/oauth-protected-resource/cursor/mcp", include_in_schema=False)
@app.get("/.well-known/oauth-protected-resource/cursor/mcp/", include_in_schema=False)
async def get_cursor_oauth_protected_resource_metadata() -> dict[str, object]:
    """Serve Cursor MCP OAuth protected-resource metadata at the challenged URL."""
    return _oauth_protected_resource_metadata(
        mcp_url=_cursor_mcp_url(),
        scopes=cursor_auth.oauth_scopes(),
    )


@app.get("/anthropic/mcp/.well-known/openid-configuration", include_in_schema=False)
@app.get("/.well-known/openid-configuration/anthropic/mcp", include_in_schema=False)
@app.get("/.well-known/oauth-authorization-server/anthropic/mcp", include_in_schema=False)
async def get_anthropic_oauth_authorization_server_metadata() -> dict[str, object]:
    """Serve Claude MCP OAuth server metadata for path-scoped discovery."""
    return _oauth_authorization_server_metadata(
        mcp_url=_anthropic_mcp_url(),
        scopes=anthropic_auth.oauth_scopes(),
    )


@app.get("/.well-known/oauth-authorization-server", include_in_schema=False)
async def get_root_oauth_authorization_server_metadata() -> dict[str, object]:
    """Serve root OAuth metadata for clients that ignore path-scoped discovery."""
    if _anthropic_only_mode() or _root_oauth_connector() == "anthropic":
        return _oauth_authorization_server_metadata(
            mcp_url=_anthropic_mcp_url(),
            scopes=anthropic_auth.oauth_scopes(),
        )
    return _oauth_authorization_server_metadata(
        mcp_url=_cursor_mcp_url(),
        scopes=cursor_auth.oauth_scopes(),
    )


@app.get("/cursor/mcp/.well-known/openid-configuration", include_in_schema=False)
@app.get("/.well-known/openid-configuration/cursor/mcp", include_in_schema=False)
@app.get("/.well-known/oauth-authorization-server/cursor/mcp", include_in_schema=False)
async def get_cursor_oauth_authorization_server_metadata() -> dict[str, object]:
    """Serve Cursor MCP OAuth server metadata for clients probing the root path."""
    return _oauth_authorization_server_metadata(
        mcp_url=_cursor_mcp_url(),
        scopes=cursor_auth.oauth_scopes(),
    )


# Mount assets, PDFs, and the public remote MCP discovery server
app.mount("/assets", StaticFiles(directory="docs/assets"), name="assets")
app.mount("/pdf", StaticFiles(directory="docs/pdf"), name="pdf")
app.add_route(
    REMOTE_MCP_PATH.rstrip("/"),
    _SlashlessMountEndpoint(PUBLIC_MCP_HTTP_APP, REMOTE_MCP_PATH),
    include_in_schema=False,
)
app.mount(REMOTE_MCP_PATH, PUBLIC_MCP_HTTP_APP, name="public-mcp")
app.add_route(
    "/anthropic/mcp",
    _SlashlessMountEndpoint(ANTHROPIC_MCP_HTTP_APP, "/anthropic/mcp"),
    include_in_schema=False,
)
app.mount("/anthropic/mcp", ANTHROPIC_MCP_HTTP_APP, name="anthropic-mcp")
app.add_route(
    "/cursor/mcp",
    _SlashlessMountEndpoint(CURSOR_MCP_HTTP_APP, "/cursor/mcp"),
    include_in_schema=False,
)
app.mount("/cursor/mcp", CURSOR_MCP_HTTP_APP, name="cursor-mcp")


# ---------------------------------------------------------------------------
# x402 Payment Middleware — Tiered Pricing
# ---------------------------------------------------------------------------

# Route → price mapping (None = free)
ROUTE_PRICING: dict[str, Decimal | None] = {
    # Crypto — dynamic pricing based on asset tier (handled separately)
    "/v1/vwap/": None,  # set dynamically
    "/v1/bidask/": None,  # set dynamically
    "/v1/state/": None,  # set dynamically
    "/v1/vwap30m/": None,  # set dynamically
    "/v1/vwap24h/": None,  # set dynamically
    # TradFi
    "/v1/fx/": settings.pricing.tradfi,
    "/v1/metal/": settings.pricing.tradfi,
    "/v1/briefs/market": Decimal("0.25"),
    "/v1/checks/pre-trade": Decimal("0.10"),
    "/v1/receipts/price": Decimal("0.25"),
    "/v1/snapshots/macro": Decimal("1.00"),
    "/v1/monitors/evaluate": Decimal("0.25"),
    "/v1/indicators/token-quality": Decimal("0.50"),
    "/v1/indicators/state-divergence": Decimal("0.50"),
    "/v1/signals/solana-token-brief": Decimal("1.00"),
    "/v1/signals/trader-alpha-pack": Decimal("2.50"),
    # Free
    "/v1/search": None,
    "/v1/instruments/": None,
    "/v1/cache/status": None,
    "/v1/provenance/": None,
    "/health": None,
    "/v1/credits/": None,  # Credit endpoints define their own x402 challenges
}

SUPPORTED_BATCH_SERVICES = {"vwap", "bidask", "fx", "metal", "state", "vwap30m", "vwap24h"}
QUOTE_SUFFIXES = ("USDT", "USDC", "USD", "EUR", "GBP", "JPY", "BTC", "ETH")
SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,32}$")
WALLET_ID_RE = re.compile(r"^[A-Za-z0-9:._-]{20,128}$")
STARTER_ID_RE = re.compile(r"^[A-Za-z0-9:._@-]{8,160}$")
EVM_TRANSFER_TOPIC = "0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef"
DISCOVERY_RATE_LIMIT_PATHS = ("/v1/search", "/v1/instruments/")
# ---------------------------------------------------------------------------
# Documentation & Schemas
# ---------------------------------------------------------------------------

X402_RESPONSE = {
    "402": {
        "description": "Payment Required. Returns a PAYMENT-REQUIRED header with an x402 PaymentRequired challenge.",
        "headers": {
            "PAYMENT-REQUIRED": {
                "description": "Base64 encoded x402 PaymentRequired object.",
                "schema": {"type": "string"}
            }
        },
        "content": {
            "application/json": {
                "example": {
                    "x402Version": 2,
                    "error": "Payment Required",
                    "message": "This endpoint requires a payment of $0.002 USDC.",
                    "price_usdc": "0.002",
                    "accepts": [
                        {
                            "scheme": "exact",
                            "network": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
                            "amount": "2000",
                            "asset": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp/EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
                            "payTo": "recipient-wallet",
                            "maxTimeoutSeconds": 30,
                        }
                    ],
                    "networks": [
                        {"name": "Solana", "caip2": "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp"},
                        {"name": "Base", "caip2": "eip155:8453"}
                    ]
                }
            }
        }
    }
}


X402_PROTOCOLS = [{"x402": {}}]
X402_CRYPTO_PAYMENT_INFO = {
    "x-payment-info": {
        "price": {
            "mode": "dynamic",
            "currency": "USD",
            "min": str(settings.pricing.core_crypto),
            "max": str(settings.pricing.extended_crypto),
        },
        "protocols": X402_PROTOCOLS,
    }
}
X402_BIDASK_PAYMENT_INFO = {
    "x-payment-info": {
        "price": {
            "mode": "dynamic",
            "currency": "USD",
            "min": str(settings.pricing.core_crypto),
            "max": str(max(settings.pricing.extended_crypto, settings.pricing.equities)),
        },
        "protocols": X402_PROTOCOLS,
    }
}
X402_TRADFI_PAYMENT_INFO = {
    "x-payment-info": {
        "price": {
            "mode": "fixed",
            "currency": "USD",
            "amount": str(settings.pricing.tradfi),
        },
        "protocols": X402_PROTOCOLS,
    }
}
X402_BATCH_PAYMENT_INFO = {
    "x-payment-info": {
        "price": {
            "mode": "dynamic",
            "currency": "USD",
            "min": str(settings.pricing.core_crypto),
            "max": str(
                max(
                    settings.pricing.extended_crypto,
                    settings.pricing.tradfi,
                    settings.pricing.equities,
                )
                * Decimal(settings.server.max_batch_size)
            ),
        },
        "protocols": X402_PROTOCOLS,
    }
}
X402_WELL_KNOWN_RESOURCES = [
    f"{PUBLIC_BASE_URL}/v1/vwap/BTC-USD",
    f"{PUBLIC_BASE_URL}/v1/bidask/BTC-USD",
    f"{PUBLIC_BASE_URL}/v1/state/MSOLUSD",
    f"{PUBLIC_BASE_URL}/v1/vwap30m/SOLUSD",
    f"{PUBLIC_BASE_URL}/v1/vwap24h/BTCUSD",
    f"{PUBLIC_BASE_URL}/v1/bidask/AAPL",
    f"{PUBLIC_BASE_URL}/v1/fx/EURUSD",
    f"{PUBLIC_BASE_URL}/v1/metal/XAUUSD",
    f"{PUBLIC_BASE_URL}/v1/briefs/market",
    f"{PUBLIC_BASE_URL}/v1/checks/pre-trade",
    f"{PUBLIC_BASE_URL}/v1/receipts/price",
    f"{PUBLIC_BASE_URL}/v1/snapshots/macro",
    f"{PUBLIC_BASE_URL}/v1/indicators/token-quality",
    f"{PUBLIC_BASE_URL}/v1/indicators/state-divergence",
    f"{PUBLIC_BASE_URL}/v1/signals/solana-token-brief",
    f"{PUBLIC_BASE_URL}/v1/signals/trader-alpha-pack",
]


def _x402_endpoint_description(path: str) -> str:
    if path.startswith("/v1/vwap/"):
        return "Real-time crypto VWAP market data from Blocksize Capital."
    if path.startswith("/v1/bidask/"):
        return "Real-time shared bid/ask snapshot data, including crypto pairs and supported equity tickers, from Blocksize Capital."
    if path.startswith("/v1/fx/"):
        return "Real-time FX market data from Blocksize Capital."
    if path.startswith("/v1/metal/"):
        return "Real-time precious metals market data from Blocksize Capital."
    if path.startswith("/v1/batch"):
        return "Batch real-time market data queries from Blocksize Capital."
    if path.startswith("/v1/briefs/market"):
        return "Agent Market Brief: decision-ready market package with provenance."
    if path.startswith("/v1/checks/pre-trade"):
        return "Pre-Trade Sanity Check: spread, freshness, and reference-price guardrails."
    if path.startswith("/v1/receipts/price"):
        return "Audit-grade price receipt with source inputs and reproducibility metadata."
    if path.startswith("/v1/snapshots/macro"):
        return "Multi-asset macro snapshot across crypto, FX, metals, and risk context."
    if path.startswith("/v1/indicators/token-quality"):
        return "Token Market Quality Score: trader-grade freshness, spread, state, and VWAP-window indicator."
    if path.startswith("/v1/indicators/state-divergence"):
        return "Oracle/state price divergence indicator using live Blocksize market and state prices."
    if path.startswith("/v1/signals/solana-token-brief"):
        return "Solana token brief for supported token symbols, with explicit feed coverage and trader signals."
    if path.startswith("/v1/signals/trader-alpha-pack"):
        return "Trader alpha-style signal pack built from Blocksize price, bid/ask, state, and VWAP-window data."
    if path.startswith("/v1/credits/purchase"):
        return "Bulk wallet credit purchase for Blocksize Capital paid data."
    return "Blocksize Capital x402-protected market data."


def _x402_query_schema_for_request(request: Request) -> dict[str, Any]:
    if request.url.path.startswith("/v1/batch"):
        return {
            "type": "object",
            "properties": {
                "reqs": {
                    "type": "string",
                    "description": "Comma-separated service:symbol items, for example vwap:BTCUSD,fx:EURUSD.",
                }
            },
            "required": ["reqs"],
            "additionalProperties": False,
        }
    if request.url.path.startswith("/v1/credits/purchase"):
        return {
            "type": "object",
            "properties": {
                "tier": {
                    "type": "string",
                    "enum": ["starter", "pro", "institutional"],
                }
            },
            "required": ["tier"],
            "additionalProperties": False,
        }
    return {"type": "object", "properties": {}, "additionalProperties": False}


def _x402_bazaar_extension(request: Request) -> dict[str, Any]:
    query_schema = _x402_query_schema_for_request(request)
    query_example: dict[str, Any] = {}
    if "reqs" in query_schema.get("properties", {}):
        query_example["reqs"] = "vwap:BTCUSD,fx:EURUSD"
    if "tier" in query_schema.get("properties", {}):
        query_example["tier"] = "starter"

    output_example = {
        "status": "ok",
        "data": {},
        "meta": {"provider": "Blocksize Capital"},
    }
    return {
        "info": {
            "input": {
                "method": request.method.upper(),
                "queryParams": query_example,
            },
            "output": output_example,
        },
        "schema": {
            "type": "object",
            "properties": {
                "input": {
                    "type": "object",
                    "properties": {"queryParams": query_schema},
                },
                "output": {
                    "type": "object",
                    "properties": {"example": output_example},
                },
            },
        },
    }


def _x402_v2_accepts(
    payment_requirements: list[dict],
    resource_url: str | None = None,
) -> list[dict[str, Any]]:
    accepts: list[dict[str, Any]] = []
    for requirement in payment_requirements:
        extra = requirement.get("extra")
        accept_extra = dict(extra) if isinstance(extra, dict) else {}
        if resource_url:
            accept_extra["resource"] = resource_url
        fallback_asset = (
            settings.x402.solana_usdc_address
            if _network_kind(str(requirement.get("network", ""))) == "solana"
            else settings.x402.base_usdc_address
        )
        accept = {
            "scheme": str(requirement.get("scheme") or "exact"),
            "network": str(requirement.get("network") or ""),
            "amount": str(
                requirement.get("amount")
                or requirement.get("maxAmountRequired")
                or "0"
            ),
            "asset": _requirement_asset(requirement, fallback_asset),
            "payTo": str(requirement.get("payTo") or requirement.get("resource") or ""),
            "maxTimeoutSeconds": int(requirement.get("maxTimeoutSeconds") or 60),
            "extra": accept_extra,
        }
        if resource_url:
            accept["resource"] = resource_url
        accepts.append(accept)
    return accepts


def _public_request_url(request: Request) -> str:
    """Build the canonical public URL used inside payment challenges."""
    base = PUBLIC_BASE_URL.rstrip("/")
    path = request.url.path
    query = request.url.query
    url = f"{base}{path}"
    return f"{url}?{query}" if query else url


def _x402_payment_required(
    request: Request,
    payment_requirements: list[dict],
) -> dict[str, Any]:
    resource_url = _public_request_url(request)
    return {
        "x402Version": 2,
        "error": "Payment Required",
        "resource": {
            "url": resource_url,
            "description": _x402_endpoint_description(request.url.path),
            "mimeType": "application/json",
        },
        "accepts": _x402_v2_accepts(payment_requirements, resource_url),
        "extensions": {"bazaar": _x402_bazaar_extension(request)},
    }


def _encode_payment_required(payment_required: dict[str, Any]) -> str:
    return base64.b64encode(json.dumps(payment_required).encode()).decode()


def _normalise_symbol(value: str, field_name: str = "symbol") -> str:
    """Return an upstream-safe alphanumeric symbol."""
    raw = value.strip()
    if len(raw) > 64:
        raise ValueError(f"{field_name} is too long")
    clean = raw.replace("-", "").replace("/", "").replace("_", "").upper()
    if not SYMBOL_RE.fullmatch(clean):
        raise ValueError(f"Invalid {field_name}; use 2-32 letters or digits")
    return clean


def _parse_batch_reqs(reqs: str) -> list[tuple[str, str, str]]:
    """Parse and validate batch requests as (service, clean_symbol, original)."""
    raw_queries = [item.strip() for item in reqs.split(",") if item.strip()]
    if not raw_queries:
        raise ValueError("Batch request must include at least one service:symbol item")
    if len(raw_queries) > settings.server.max_batch_size:
        raise ValueError(
            f"Batch request exceeds MAX_BATCH_SIZE={settings.server.max_batch_size}"
        )

    parsed: list[tuple[str, str, str]] = []
    for raw_query in raw_queries:
        if ":" not in raw_query:
            raise ValueError("Batch items must use service:symbol format")
        svc, ticker = raw_query.split(":", 1)
        svc = svc.strip().lower()
        if svc not in SUPPORTED_BATCH_SERVICES:
            raise ValueError(f"Unsupported batch service: {svc}")
        parsed.append((svc, _normalise_symbol(ticker, "batch symbol"), raw_query))

    return parsed


def _base_from_symbol(symbol: str) -> str:
    """Extract a likely base asset from a compact pair symbol."""
    for quote in QUOTE_SUFFIXES:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[: -len(quote)]
    return symbol[: len(symbol) // 2].upper()


def _quote_from_symbol(symbol: str) -> str:
    """Extract a supported quote suffix from a compact symbol, when present."""
    for quote in QUOTE_SUFFIXES:
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return quote
    return ""


def _looks_like_equity_bidask_symbol(symbol: str) -> bool:
    """Heuristically identify equity tickers routed through /v1/bidask."""
    clean = symbol.upper()
    quote = _quote_from_symbol(clean)
    base = clean[: -len(quote)] if quote else clean

    if clean in METAL_TICKERS or base in FIAT_CURRENCIES:
        return False

    if quote in {"USD", "USDT", "USDC"}:
        return base.endswith("X")

    return clean.isalpha() and 1 <= len(clean) <= 5 and clean not in TOP_250_CRYPTO


def _bidask_price_for_symbol(symbol: str) -> Decimal:
    """Price shared bid/ask calls across crypto pairs and equity tickers."""
    if _looks_like_equity_bidask_symbol(symbol):
        return settings.pricing.equities
    return settings.pricing.get_crypto_price(_base_from_symbol(symbol))


class InMemoryRateLimiter:
    """Small fixed-window limiter for public discovery traffic."""

    def __init__(self) -> None:
        self._minute_hits: dict[str, Deque[float]] = defaultdict(deque)
        self._day_hits: dict[str, Deque[float]] = defaultdict(deque)

    def clear(self) -> None:
        self._minute_hits.clear()
        self._day_hits.clear()

    @staticmethod
    def _prune(hits: Deque[float], now: float, window_seconds: int) -> None:
        cutoff = now - window_seconds
        while hits and hits[0] <= cutoff:
            hits.popleft()

    @staticmethod
    def _retry_after(hits: Deque[float], now: float, window_seconds: int) -> int:
        if not hits:
            return window_seconds
        return max(1, int(hits[0] + window_seconds - now) + 1)

    def check(
        self,
        key: str,
        *,
        per_minute: int,
        per_day: int,
        now: float | None = None,
    ) -> tuple[bool, int | None, str | None]:
        """Return whether the request is allowed, retry delay, and limit label."""
        if per_minute <= 0 and per_day <= 0:
            return True, None, None

        now = now or time.time()
        minute_hits = self._minute_hits[key]
        day_hits = self._day_hits[key]

        self._prune(minute_hits, now, 60)
        self._prune(day_hits, now, 86_400)

        if per_minute > 0 and len(minute_hits) >= per_minute:
            return False, self._retry_after(minute_hits, now, 60), "minute"
        if per_day > 0 and len(day_hits) >= per_day:
            return False, self._retry_after(day_hits, now, 86_400), "day"

        if per_minute > 0:
            minute_hits.append(now)
        if per_day > 0:
            day_hits.append(now)
        return True, None, None


_DISCOVERY_RATE_LIMITER = InMemoryRateLimiter()


def _client_ip(request: Request) -> str:
    """Best-effort client identifier for soft public discovery limits."""
    return _request_client_ip(request)


def _is_discovery_rate_limited_path(path: str) -> bool:
    """Limit public discovery calls without throttling docs, manifests, or paid routes."""
    remote_mcp_path = REMOTE_MCP_PATH.rstrip("/")
    return (
        path == DISCOVERY_RATE_LIMIT_PATHS[0]
        or path.startswith(DISCOVERY_RATE_LIMIT_PATHS[1])
        or path == remote_mcp_path
        or path.startswith(f"{remote_mcp_path}/")
    )


def _discovery_rate_limit_response(request: Request) -> JSONResponse | None:
    """Return a 429 response when public discovery traffic exceeds fair-use limits."""
    if not settings.server.discovery_rate_limit_enabled:
        return None

    path = request.url.path
    if not _is_discovery_rate_limited_path(path):
        return None

    client_ip = _client_ip(request)
    allowed, retry_after, limit_window = _DISCOVERY_RATE_LIMITER.check(
        f"discovery:{client_ip}",
        per_minute=settings.server.discovery_rate_limit_per_minute,
        per_day=settings.server.discovery_rate_limit_per_day,
    )
    if allowed:
        return None

    retry_after = retry_after or 60
    logger.warning(
        "Discovery rate limit exceeded for %s on %s (%s limit)",
        client_ip,
        path,
        limit_window,
    )
    return JSONResponse(
        status_code=429,
        headers={
            "Retry-After": str(retry_after),
            "X-RateLimit-Policy": (
                f"{settings.server.discovery_rate_limit_per_minute}/minute; "
                f"{settings.server.discovery_rate_limit_per_day}/day"
            ),
        },
        content={
            "error": "Too Many Requests",
            "message": (
                "Free discovery traffic is temporarily rate limited. "
                "Please retry later or use paid data routes for production traffic."
            ),
            "retry_after_seconds": retry_after,
            "limit_window": limit_window,
        },
    )


def _get_price_for_request(request: Request) -> Decimal | None:
    """Determine the price for a given request."""
    path = request.url.path
    paid_get_prefixes = (
        "/v1/batch",
        "/v1/vwap/",
        "/v1/bidask/",
        "/v1/state/",
        "/v1/vwap30m/",
        "/v1/vwap24h/",
        "/v1/fx/",
        "/v1/metal/",
    )
    if path.startswith(paid_get_prefixes) and request.method.upper() not in {"GET", "HEAD"}:
        return None
    
    # Handle Batch endpoint dynamically
    if path.startswith("/v1/batch"):
        reqs = request.query_params.get("reqs", "")
        if not reqs:
            return None
        
        total = Decimal("0.0")
        for svc, pair, _raw_query in _parse_batch_reqs(reqs):
            if svc in {"vwap", "state", "vwap30m", "vwap24h"}:
                base = _base_from_symbol(pair)
                svc_price = settings.pricing.get_crypto_price(base)
                total += svc_price
            elif svc == "bidask":
                total += _bidask_price_for_symbol(pair)
            else:
                mock_path = f"/v1/{svc}/{pair}"
                for prefix, price in ROUTE_PRICING.items():
                    if mock_path.startswith(prefix) and price is not None:
                        total += price
                        break
        return total if total > 0 else None

    # Crypto uses dynamic tier pricing
    if (
        path.startswith("/v1/vwap/")
        or path.startswith("/v1/bidask/")
        or path.startswith("/v1/state/")
        or path.startswith("/v1/vwap30m/")
        or path.startswith("/v1/vwap24h/")
    ):
        parts = path.rstrip("/").split("/")
        if len(parts) >= 4:
            pair = _normalise_symbol(parts[3], "pair")
            if path.startswith("/v1/bidask/"):
                return _bidask_price_for_symbol(pair)
            base = _base_from_symbol(pair)
            return settings.pricing.get_crypto_price(base)
        return settings.pricing.core_crypto

    # Check static route pricing
    for route_prefix, price in ROUTE_PRICING.items():
        if path.startswith(route_prefix):
            return price

    return None  # Free endpoint


def _credit_cost_for_request(request: Request) -> float | None:
    """Return starter-credit cost for a paid request, independent of USDC price."""
    path = request.url.path
    if path.startswith("/v1/batch"):
        reqs = request.query_params.get("reqs", "")
        if not reqs:
            return None
        total = 0.0
        for svc, _pair, _raw_query in _parse_batch_reqs(reqs):
            if svc == "vwap":
                total += CREDIT_COSTS["raw_vwap"]
            elif svc == "bidask":
                total += CREDIT_COSTS["raw_bidask"]
            elif svc == "state":
                total += CREDIT_COSTS["raw_state"]
            elif svc == "vwap30m":
                total += CREDIT_COSTS["raw_vwap_30m"]
            elif svc == "vwap24h":
                total += CREDIT_COSTS["raw_vwap_24h"]
            elif svc == "fx":
                total += CREDIT_COSTS["fx"]
            elif svc == "metal":
                total += CREDIT_COSTS["metals"]
        return total
    if path.startswith("/v1/vwap/"):
        return CREDIT_COSTS["raw_vwap"]
    if path.startswith("/v1/bidask/"):
        return CREDIT_COSTS["raw_bidask"]
    if path.startswith("/v1/state/"):
        return CREDIT_COSTS["raw_state"]
    if path.startswith("/v1/vwap30m/"):
        return CREDIT_COSTS["raw_vwap_30m"]
    if path.startswith("/v1/vwap24h/"):
        return CREDIT_COSTS["raw_vwap_24h"]
    if path.startswith("/v1/fx/"):
        return CREDIT_COSTS["fx"]
    if path.startswith("/v1/metal/"):
        return CREDIT_COSTS["metals"]
    if path.startswith("/v1/briefs/market"):
        return CREDIT_COSTS["market_brief"]
    if path.startswith("/v1/checks/pre-trade"):
        return CREDIT_COSTS["pre_trade_check"]
    if path.startswith("/v1/receipts/price"):
        return CREDIT_COSTS["audit_receipt"]
    if path.startswith("/v1/snapshots/macro"):
        return CREDIT_COSTS["macro_snapshot"]
    if path.startswith("/v1/monitors/evaluate"):
        return CREDIT_COSTS["market_brief"]
    if path.startswith("/v1/indicators/token-quality"):
        return CREDIT_COSTS["token_quality_indicator"]
    if path.startswith("/v1/indicators/state-divergence"):
        return CREDIT_COSTS["state_divergence_indicator"]
    if path.startswith("/v1/signals/solana-token-brief"):
        return CREDIT_COSTS["solana_token_brief"]
    if path.startswith("/v1/signals/trader-alpha-pack"):
        return CREDIT_COSTS["trader_alpha_pack"]
    if path.startswith("/v1/provenance/"):
        return CREDIT_COSTS["provenance_lookup"]
    return None


def _starter_credit_subject(request: Request) -> tuple[str, str, bool] | None:
    """Resolve the best starter-credit subject from wallet/user/agent hints."""
    wallet = request.headers.get("X-AGENT-WALLET")
    if wallet:
        clean_wallet = wallet.strip()
        if not WALLET_ID_RE.fullmatch(clean_wallet):
            raise ValueError("Invalid X-AGENT-WALLET header")
        return clean_wallet, "wallet", True

    for header_name, subject_type in (
        ("X-AUTHENTICATED-USER", "user"),
        ("X-USER-ID", "user"),
        ("X-AGENT-ID", "agent"),
        ("X-DEVICE-ID", "device"),
        ("X-SESSION-ID", "session"),
    ):
        value = request.headers.get(header_name)
        if value:
            clean_value = value.strip()
            if not STARTER_ID_RE.fullmatch(clean_value):
                raise ValueError(f"Invalid {header_name} header")
            return clean_value, subject_type, False

    return None


def _credit_meta_for_request(request: Request) -> dict[str, Any] | None:
    context = getattr(request.state, "starter_credit_context", None)
    if not isinstance(context, dict):
        return None
    return {
        "credit_mode": "starter_allowance",
        "credit_cost": context["credits_spent"],
        "credits_remaining": context["credits_remaining"],
        "starter_allowance_credits": STARTER_CREDIT_ALLOWANCE,
        "upgrade_path": "Use x402 payment or prepaid credit top-ups after starter credits are exhausted.",
    }


def _apply_credit_response_headers(response: Response, request: Request) -> Response:
    context = getattr(request.state, "starter_credit_context", None)
    if isinstance(context, dict):
        response.headers["X-Blocksize-Credit-Mode"] = "starter-allowance"
        response.headers["X-Blocksize-Credits-Spent"] = str(context["credits_spent"])
        response.headers["X-Blocksize-Credits-Remaining"] = str(context["credits_remaining"])
        response.headers["X-Blocksize-Starter-Allowance"] = str(STARTER_CREDIT_ALLOWANCE)
        response.headers["X-Blocksize-Upgrade-Path"] = "x402-or-prepaid-credits"
    return response


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _json_hash(payload: Any) -> str:
    stable = json.dumps(payload, default=str, sort_keys=True, separators=(",", ":"))
    return f"sha256:{fingerprint(stable, salt_env='RECEIPT_HASH_SALT')}"


def _response_receipt(
    request: Request,
    *,
    product: str,
    subject: str,
    request_payload: dict[str, Any],
    response_payload: dict[str, Any],
    source_endpoints: list[str],
) -> dict[str, Any]:
    created_at = _utc_now_iso()
    receipt_basis = {
        "product": product,
        "subject": subject,
        "created_at": created_at,
        "request": request_payload,
        "response": response_payload,
    }
    receipt_id = f"rcpt_{str(fingerprint(json.dumps(receipt_basis, default=str, sort_keys=True), salt_env='RECEIPT_ID_SALT'))[:24]}"
    receipt = {
        "receipt_id": receipt_id,
        "product": product,
        "created_at": created_at,
        "provider": "Blocksize Capital",
        "subject": subject,
        "request_hash": _json_hash(request_payload),
        "response_hash": _json_hash(response_payload),
        "source_endpoints": source_endpoints,
        "lookup_url": f"{PUBLIC_BASE_URL}/v1/provenance/{receipt_id}",
    }
    request.app.state.credits.store_price_receipt(
        receipt_id=receipt_id,
        product=product,
        subject=subject,
        payload={
            "status": "ok",
            "product": "agent_data_provenance",
            "credit_cost": CREDIT_COSTS["provenance_lookup"],
            "receipt": receipt,
            "request": request_payload,
            "response": response_payload,
            "payment": _credit_meta_for_request(request) or {
                "mode": "x402_or_external",
                "credits_spent": None,
            },
        },
    )
    return receipt


def _service_for_symbol(symbol: str, requested_service: str | None = None) -> str:
    if requested_service:
        service = requested_service.strip().lower()
        if service not in SUPPORTED_BATCH_SERVICES:
            raise ValueError(f"Unsupported service: {requested_service}")
        return service
    clean = _normalise_symbol(symbol)
    if clean in METAL_TICKERS:
        return "metal"
    base = _base_from_symbol(clean)
    quote = _quote_from_symbol(clean)
    if base in FIAT_CURRENCIES and quote in FIAT_CURRENCIES:
        return "fx"
    if _looks_like_equity_bidask_symbol(clean):
        return "bidask"
    return "vwap"


async def _fetch_service_snapshot(
    client: BlocksizeClient,
    *,
    service: str,
    symbol: str,
) -> dict[str, Any]:
    clean = _normalise_symbol(symbol)
    if service == "vwap":
        data = await client.get_vwap_latest(clean)
        return {
            "service": "vwap",
            "symbol": clean,
            "asset_class": "crypto",
            "endpoint": f"/v1/vwap/{clean}",
            "data": data.model_dump(mode="json"),
            "timestamp": data.timestamp.isoformat(),
            "value": data.vwap,
        }
    if service == "bidask":
        data = await client.get_bidask_snapshot(clean)
        mid = (data.bid + data.ask) / 2 if data.ask else data.bid
        return {
            "service": "bidask",
            "symbol": clean,
            "asset_class": "equity" if _looks_like_equity_bidask_symbol(clean) else "multi_asset",
            "endpoint": f"/v1/bidask/{clean}",
            "data": data.model_dump(mode="json"),
            "timestamp": data.timestamp.isoformat(),
            "value": mid,
            "spread_bps": (data.spread / mid * 10000) if mid else None,
        }
    if service == "fx":
        data = await client.get_fx_rate(clean)
        value = data.mid or data.bid or data.ask or 0
        return {
            "service": "fx",
            "symbol": clean,
            "asset_class": "fx",
            "endpoint": f"/v1/fx/{clean}",
            "data": data.model_dump(mode="json"),
            "timestamp": data.timestamp.isoformat(),
            "value": value,
        }
    if service == "metal":
        data = await client.get_metal_price(clean)
        return {
            "service": "metal",
            "symbol": clean,
            "asset_class": "metal",
            "endpoint": f"/v1/metal/{clean}",
            "data": data.model_dump(mode="json"),
            "timestamp": data.timestamp.isoformat(),
            "value": data.price,
        }
    raise ValueError(f"Unsupported service: {service}")


def _freshness_ms(timestamp_value: str | None) -> int | None:
    if not timestamp_value:
        return None
    try:
        ts = datetime.fromisoformat(timestamp_value.replace("Z", "+00:00"))
        if ts.tzinfo is None:
            ts = ts.replace(tzinfo=UTC)
        return max(0, int((datetime.now(UTC) - ts).total_seconds() * 1000))
    except ValueError:
        return None


def _quality_flags(snapshot: dict[str, Any]) -> list[str]:
    flags: list[str] = []
    freshness = _freshness_ms(str(snapshot.get("timestamp") or ""))
    if freshness is not None and freshness > 60_000:
        flags.append("stale_over_60s")
    spread_bps = snapshot.get("spread_bps")
    if isinstance(spread_bps, (int, float)) and spread_bps > 50:
        flags.append("wide_spread")
    if not snapshot.get("value"):
        flags.append("missing_value")
    return flags


def _bps_delta(value: float | None, reference: float | None) -> float | None:
    if not value or not reference:
        return None
    return (value - reference) / reference * 10000


def _abs_bps_delta(value: float | None, reference: float | None) -> float | None:
    delta = _bps_delta(value, reference)
    return abs(delta) if delta is not None else None


def _base_ticker_from_symbol(symbol: str) -> str:
    return _base_from_symbol(_normalise_symbol(symbol, "symbol"))


def _indicator_signal_from_score(score: int) -> str:
    if score >= 85:
        return "strong_quality"
    if score >= 70:
        return "usable"
    if score >= 50:
        return "caution"
    return "avoid_or_verify"


def _coverage_status(components: dict[str, dict[str, Any]]) -> str:
    ok_count = sum(1 for item in components.values() if item.get("status") == "ok")
    if ok_count == len(components):
        return "full"
    if ok_count:
        return "partial"
    return "none"


def _has_current_market_component(components: dict[str, dict[str, Any]]) -> bool:
    return any(
        components.get(name, {}).get("status") == "ok"
        and components.get(name, {}).get("value") is not None
        for name in ("vwap", "bidask")
    )


def _component_source_endpoints(
    symbol: str,
    components: dict[str, dict[str, Any]],
) -> list[str]:
    endpoints: list[str] = []
    for name, item in components.items():
        if item.get("status") != "ok":
            continue
        if endpoint := item.get("endpoint"):
            endpoints.append(str(endpoint))
            continue
        if name == "state":
            endpoints.append(f"state_pool:{symbol}")
        elif name == "state_coverage":
            endpoints.append("state_instruments")
        elif name == "vwap_30m":
            endpoints.append(f"closingprice_list:{_base_ticker_from_symbol(symbol)}")
        elif name == "vwap_24h":
            endpoints.append(f"fixedvwap_subscribe_cache:{symbol}")
    return endpoints


def _state_coverage_from_instruments(
    *,
    symbol: str,
    instruments: list[Any],
) -> dict[str, Any]:
    clean = _normalise_symbol(symbol, "symbol")
    base = _base_ticker_from_symbol(clean)
    target_symbols = {clean, f"{base}USD", f"{base}USDC", f"{base}USDT"}
    matches: list[dict[str, Any]] = []
    for item in instruments:
        if not isinstance(item, dict):
            continue
        item_symbol = str(item.get("symbol") or "").upper()
        if item_symbol in target_symbols:
            pools = item.get("pools") if isinstance(item.get("pools"), list) else []
            networks = sorted({
                str(pool.get("network")).lower()
                for pool in pools
                if isinstance(pool, dict) and pool.get("network")
            })
            matches.append(
                {
                    "symbol": item_symbol,
                    "pool_count": len(pools),
                    "networks": networks,
                    "solana_pool_count": sum(
                        1
                        for pool in pools
                        if isinstance(pool, dict)
                        and str(pool.get("network", "")).lower() == "solana"
                    ),
                    "pools": pools[:10],
                    "truncated": len(pools) > 10,
                }
            )
    return {
        "matched_count": len(matches),
        "matches": matches[:10],
        "truncated": len(matches) > 10,
        "pool_count": sum(item["pool_count"] for item in matches),
        "solana_pool_count": sum(item["solana_pool_count"] for item in matches),
        "networks": sorted({network for item in matches for network in item["networks"]}),
    }


async def _fetch_state_instrument_coverage(
    client: BlocksizeClient,
    *,
    symbol: str,
) -> dict[str, Any]:
    clean = _normalise_symbol(symbol, "symbol")
    base = _base_ticker_from_symbol(clean)
    try:
        instruments = await client.list_state_instruments()
    except BlocksizeAPIError as exc:
        return {
            "status": "unavailable",
            "endpoint": "state_instruments",
            "error_code": "BLOCKSIZE_ERROR",
            "message": str(exc),
        }
    except (httpx.HTTPError, ValueError) as exc:
        return {
            "status": "unavailable",
            "endpoint": "state_instruments",
            "error_code": "FEED_UNAVAILABLE",
            "message": str(exc),
        }
    if not isinstance(instruments, list):
        return {
            "status": "unavailable",
            "endpoint": "state_instruments",
            "error_code": "UNEXPECTED_STATE_INSTRUMENTS",
            "message": "state_instruments did not return a list",
        }

    coverage = _state_coverage_from_instruments(symbol=clean, instruments=instruments)
    return {
        "status": "ok",
        "endpoint": "state_instruments",
        "instrument_count": len(instruments),
        **coverage,
    }


async def _fetch_indicator_components(
    client: BlocksizeClient,
    *,
    symbol: str,
    stream_cache: BlocksizeStreamCache | None = None,
    include_state_price: bool = False,
    include_windows: bool = False,
    include_state_coverage: bool = False,
) -> dict[str, dict[str, Any]]:
    import asyncio

    clean = _normalise_symbol(symbol, "symbol")
    base = _base_ticker_from_symbol(clean)
    tasks: dict[str, Any] = {
        "vwap": client.get_vwap_latest(clean),
        "bidask": client.get_bidask_snapshot(clean),
    }
    if include_state_price:
        tasks["state"] = (
            stream_cache.get_state_price(clean)
            if stream_cache and stream_cache.enabled
            else client.get_state_price(clean)
        )
    if include_windows:
        tasks["vwap_30m"] = client.get_vwap_30min(base)
        tasks["vwap_24h"] = (
            stream_cache.get_vwap_24h(clean)
            if stream_cache and stream_cache.enabled
            else client.get_vwap_24hr(clean)
        )
    if include_state_coverage:
        tasks["state_coverage"] = _fetch_state_instrument_coverage(client, symbol=clean)

    async def collect(name: str, coro: Any) -> tuple[str, dict[str, Any]]:
        try:
            data = await coro
        except BlocksizeAPIError as exc:
            return (
                name,
                {
                    "status": "unavailable",
                    "error_code": "BLOCKSIZE_ERROR",
                    "message": str(exc),
                },
            )
        except (httpx.HTTPError, ValueError) as exc:
            return (
                name,
                {
                    "status": "unavailable",
                    "error_code": "FEED_UNAVAILABLE",
                    "message": str(exc),
                },
            )

        if name == "vwap":
            return (
                name,
                {
                    "status": "ok",
                    "endpoint": f"/v1/vwap/{clean}",
                    "value": data.vwap,
                    "timestamp": data.timestamp.isoformat(),
                    "raw": data.model_dump(mode="json"),
                },
            )
        if name == "bidask":
            mid = (data.bid + data.ask) / 2 if data.ask else data.bid
            return (
                name,
                {
                    "status": "ok",
                    "endpoint": f"/v1/bidask/{clean}",
                    "value": mid,
                    "bid": data.bid,
                    "ask": data.ask,
                    "spread": data.spread,
                    "spread_bps": (data.spread / mid * 10000) if mid else None,
                    "timestamp": data.timestamp.isoformat(),
                    "raw": data.model_dump(mode="json"),
                },
            )
        if name == "state":
            endpoint = (
                "state_subscribe_cache"
                if data.source.endswith("state_subscribe_cache")
                else "state_pool"
            )
            return (
                name,
                {
                    "status": "ok",
                    "endpoint": endpoint,
                    "value": data.price,
                    "timestamp": data.timestamp.isoformat(),
                    "raw": data.model_dump(mode="json"),
                },
            )
        if name == "vwap_30m":
            return (
                name,
                {
                    "status": "ok",
                    "endpoint": "closingprice_list",
                    "value": data.vwap,
                    "timestamp": data.timestamp.isoformat(),
                    "raw": data.model_dump(mode="json"),
                },
            )
        if name == "vwap_24h":
            return (
                name,
                {
                    "status": "ok",
                    "endpoint": "fixedvwap_subscribe_cache"
                    if data.source.endswith("fixedvwap_subscribe_cache")
                    else "vwap_24h_latest",
                    "value": data.vwap,
                    "volume": data.volume,
                    "timestamp": data.timestamp.isoformat(),
                    "raw": data.model_dump(mode="json"),
                },
            )
        if name == "state_coverage" and isinstance(data, dict):
            return name, data
        return name, {"status": "unavailable", "error_code": "UNKNOWN_COMPONENT"}

    results = await asyncio.gather(
        *(collect(name, coro) for name, coro in tasks.items())
    )
    return dict(results)


def _build_token_quality_indicator(
    *,
    symbol: str,
    components: dict[str, dict[str, Any]],
    max_spread_bps: float = 50,
    max_state_divergence_bps: float = 75,
) -> dict[str, Any]:
    vwap = components.get("vwap", {})
    bidask = components.get("bidask", {})
    state = components.get("state", {})
    state_coverage = components.get("state_coverage", {})
    vwap_30m = components.get("vwap_30m", {})
    vwap_24h = components.get("vwap_24h", {})

    current_value = (
        vwap.get("value")
        if vwap.get("status") == "ok"
        else bidask.get("value") if bidask.get("status") == "ok" else None
    )
    state_value = state.get("value") if state.get("status") == "ok" else None
    vwap_30m_value = vwap_30m.get("value") if vwap_30m.get("status") == "ok" else None
    vwap_24h_value = vwap_24h.get("value") if vwap_24h.get("status") == "ok" else None
    spread_bps = bidask.get("spread_bps") if bidask.get("status") == "ok" else None
    state_divergence_bps = _abs_bps_delta(current_value, state_value)
    vwap_30m_drift_bps = _bps_delta(current_value, vwap_30m_value)
    vwap_24h_drift_bps = _bps_delta(current_value, vwap_24h_value)

    score = 100
    flags: list[str] = []
    if current_value is None:
        score -= 45
        flags.append("missing_current_market_price")
    if spread_bps is None:
        score -= 12
        flags.append("missing_bidask_spread")
    elif spread_bps > max_spread_bps:
        score -= min(30, int((spread_bps - max_spread_bps) / max_spread_bps * 30))
        flags.append("wide_spread")
    if "state" in components and state_divergence_bps is None:
        score -= 10
        flags.append("state_price_unavailable")
    elif state_divergence_bps is not None and state_divergence_bps > max_state_divergence_bps:
        score -= min(
            30,
            int(
                (state_divergence_bps - max_state_divergence_bps)
                / max_state_divergence_bps
                * 30
            ),
        )
        flags.append("state_price_divergence")
    if "state_coverage" in components:
        if state_coverage.get("status") != "ok":
            score -= 5
            flags.append("state_instrument_coverage_unavailable")
        elif not state_coverage.get("matched_count"):
            score -= 5
            flags.append("state_instrument_not_listed")
    if "vwap_30m" in components and vwap_30m_value is None:
        score -= 5
        flags.append("vwap_30m_unavailable")
    if "vwap_24h" in components and vwap_24h_value is None:
        score -= 5
        flags.append("vwap_24h_unavailable")

    freshness_values = [
        _freshness_ms(str(item.get("timestamp") or ""))
        for item in components.values()
        if item.get("status") == "ok"
    ]
    stale_count = sum(1 for item in freshness_values if item is not None and item > 60_000)
    if stale_count:
        score -= min(25, stale_count * 8)
        flags.append("stale_component")

    clean_score = max(0, min(100, score))
    return {
        "symbol": symbol,
        "network_hint": "solana" if _base_ticker_from_symbol(symbol) in {"SOL", "JUP", "PYTH", "JTO", "RAY", "ORCA", "BONK", "WIF"} else None,
        "score": clean_score,
        "signal": _indicator_signal_from_score(clean_score),
        "metrics": {
            "current_value": current_value,
            "spread_bps": spread_bps,
            "state_price": state_value,
            "state_divergence_bps": state_divergence_bps,
            "vwap_30m": vwap_30m_value,
            "vwap_30m_drift_bps": vwap_30m_drift_bps,
            "vwap_24h": vwap_24h_value,
            "vwap_24h_drift_bps": vwap_24h_drift_bps,
            "state_instrument_matched_count": (
                state_coverage.get("matched_count")
                if state_coverage.get("status") == "ok"
                else None
            ),
            "state_pool_count": (
                state_coverage.get("pool_count")
                if state_coverage.get("status") == "ok"
                else None
            ),
            "state_solana_pool_count": (
                state_coverage.get("solana_pool_count")
                if state_coverage.get("status") == "ok"
                else None
            ),
            "state_networks": (
                state_coverage.get("networks")
                if state_coverage.get("status") == "ok"
                else None
            ),
            "freshness_ms_max": max(
                (item for item in freshness_values if item is not None),
                default=None,
            ),
        },
        "coverage": {
            "status": _coverage_status(components),
            "feeds": {
                name: item.get("status", "unknown")
                for name, item in components.items()
            },
        },
        "flags": sorted(set(flags)),
    }


_SEEN_TX_HASHES: set[str] = set()


def _decode_payment_payload(payment_payload: str) -> dict[str, Any]:
    """Decode and lightly validate the x402 proof header payload."""
    if len(payment_payload) > 4096:
        raise ValueError("Payment payload is too large")
    try:
        decoded = base64.b64decode(payment_payload, validate=True).decode("utf-8")
        payload = json.loads(decoded)
    except (binascii.Error, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("Payment payload must be base64-encoded JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("Payment payload must be a JSON object")
    return payload


def _network_kind(network: str) -> str | None:
    network_lower = network.lower()
    if "solana" in network_lower:
        return "solana"
    if "eip155" in network_lower or "base" in network_lower:
        return "evm"
    return None


def _select_requirement(network: str, payment_requirements: list[dict]) -> dict[str, Any] | None:
    """Find the payment requirement matching the proof network."""
    kind = _network_kind(network)
    for requirement in payment_requirements:
        req_network = str(requirement.get("network", ""))
        req_kind = _network_kind(req_network)
        if kind and req_kind == kind:
            return requirement
        if network and network == req_network:
            return requirement
    return None


def _requirement_amount_atomic(requirement: dict[str, Any]) -> int:
    """Read the required amount as USDC atomic units."""
    raw = requirement.get("maxAmountRequired")
    if raw is None and "amount" in requirement:
        return int(Decimal(str(requirement["amount"])) * Decimal("1000000"))
    return int(Decimal(str(raw or "0")))


def _requirement_recipient(requirement: dict[str, Any]) -> str:
    """Read the configured recipient from an x402 requirement."""
    return str(
        requirement.get("payTo")
        or requirement.get("recipient")
        or requirement.get("resource")
        or ""
    )


def _requirement_asset(requirement: dict[str, Any], fallback: str) -> str:
    """Read the token mint/contract address from an x402 requirement."""
    asset = str(requirement.get("asset") or "")
    if "/" in asset:
        return asset.rsplit("/", 1)[-1]
    return fallback


def _token_amount_atomic(balance: dict[str, Any]) -> int:
    amount = balance.get("uiTokenAmount", {}).get("amount")
    return int(amount or 0)


def _solana_transfer_satisfies_requirement(
    transaction: dict[str, Any],
    requirement: dict[str, Any],
) -> tuple[bool, str]:
    """Validate a Solana USDC payment by recipient owner, mint, and net amount."""
    meta = transaction.get("meta") or {}
    expected_recipient = _requirement_recipient(requirement)
    expected_mint = _requirement_asset(requirement, settings.x402.solana_usdc_address)
    required_amount = _requirement_amount_atomic(requirement)

    if not expected_recipient:
        return False, "Solana payment recipient is not configured"
    if required_amount <= 0:
        return False, "Solana payment amount is not configured"

    pre_balances = {
        (item.get("accountIndex"), item.get("mint")): _token_amount_atomic(item)
        for item in meta.get("preTokenBalances", [])
    }

    for post in meta.get("postTokenBalances", []):
        if post.get("mint") != expected_mint:
            continue
        if post.get("owner") != expected_recipient:
            continue
        key = (post.get("accountIndex"), post.get("mint"))
        delta = _token_amount_atomic(post) - pre_balances.get(key, 0)
        if delta >= required_amount:
            return True, "ok"

    return (
        False,
        "No Solana USDC transfer matched the configured recipient and required amount",
    )


def _evm_transfer_satisfies_requirement(
    receipt: dict[str, Any],
    requirement: dict[str, Any],
) -> tuple[bool, str]:
    """Validate an EVM USDC Transfer log by contract, recipient, and amount."""
    expected_recipient = _requirement_recipient(requirement).lower()
    expected_token = _requirement_asset(requirement, settings.x402.base_usdc_address).lower()
    required_amount = _requirement_amount_atomic(requirement)

    if not expected_recipient.startswith("0x") or len(expected_recipient) != 42:
        return False, "EVM payment recipient is not configured"
    if required_amount <= 0:
        return False, "EVM payment amount is not configured"

    recipient_topic = "0x" + expected_recipient.removeprefix("0x").rjust(64, "0")
    for log_item in receipt.get("logs", []):
        if str(log_item.get("address", "")).lower() != expected_token:
            continue
        topics = [str(topic).lower() for topic in log_item.get("topics", [])]
        if len(topics) < 3 or topics[0] != EVM_TRANSFER_TOPIC:
            continue
        if topics[2] != recipient_topic:
            continue
        try:
            amount = int(str(log_item.get("data", "0x0")), 16)
        except ValueError:
            continue
        if amount >= required_amount:
            return True, "ok"

    return False, "No Base USDC Transfer matched the configured recipient and required amount"


def _transaction_is_recent(block_time: int | None) -> bool:
    max_age = settings.server.x402_payment_max_age_seconds
    if max_age <= 0 or not block_time:
        return True
    return time.time() - block_time <= max_age


def _record_payment_use(
    tx_hash: str,
    network: str,
    requirement: dict[str, Any],
    credit_manager: CreditManager | None,
    purpose: str,
) -> tuple[bool, str]:
    """Persist proof usage so a transaction cannot be replayed."""
    if tx_hash in _SEEN_TX_HASHES:
        return False, "Transaction hash has already been used"

    amount_atomic = _requirement_amount_atomic(requirement)
    recipient = _requirement_recipient(requirement)
    if credit_manager and not credit_manager.record_payment_proof(
        tx_hash=tx_hash,
        network=network,
        amount_atomic=amount_atomic,
        recipient=recipient,
        purpose=purpose,
    ):
        return False, "Transaction hash has already been used"

    _SEEN_TX_HASHES.add(tx_hash)
    return True, "ok"


async def _verify_payment(
    payment_payload: str,
    payment_requirements: list[dict],
    credit_manager: CreditManager | None = None,
    purpose: str = "data",
) -> dict:
    """Verify an x402 payment against chain data, amount, token, and recipient."""
    try:
        payload = _decode_payment_payload(payment_payload)
        tx_hash = str(payload.get("proof") or payload.get("tx_hash") or "").strip()
        network = str(payload.get("network") or "solana").strip()
        
        if not tx_hash:
            return {"valid": False, "reason": "Missing tx_hash/proof in payload"}

        requirement = _select_requirement(network, payment_requirements)
        if requirement is None:
            return {"valid": False, "reason": f"No payment requirement configured for {network}"}

        allow_mock = os.getenv("X402_ALLOW_MOCK_PAYMENTS", "").lower() in {"1", "true", "yes"}
        if allow_mock and str(tx_hash).startswith(("mock_", "test_")):
            if requirement is None:
                return {"valid": False, "reason": "Mock payment has no matching requirement"}
            recorded, reason = _record_payment_use(
                tx_hash,
                network,
                requirement,
                credit_manager,
                purpose,
            )
            if not recorded:
                return {"valid": False, "reason": reason}
            logger.warning("Accepted mock x402 proof for local/demo mode only: %s", tx_hash)
            return {"valid": True, "mock": True, "network": network}
            
        if "solana" in network:
            rpc_url = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
            logger.info(f"Verifying Solana payment via RPC: {rpc_url.split('?')[0]}")
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(
                    rpc_url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "getTransaction",
                        "params": [
                            tx_hash,
                            {"encoding": "jsonParsed", "maxSupportedTransactionVersion": 0, "commitment": "confirmed"}
                        ]
                    }
                )
                res.raise_for_status()
                data = res.json()
                
            result = data.get("result")
            if not result:
                return {"valid": False, "reason": "Transaction not found on chain or not yet finalized"}
                
            meta = result.get("meta")
            if meta and meta.get("err") is not None:
                return {"valid": False, "reason": f"Transaction reverted on chain: {meta['err']}"}

            if not _transaction_is_recent(result.get("blockTime")):
                return {"valid": False, "reason": "Transaction is older than allowed payment window"}

            matched, reason = _solana_transfer_satisfies_requirement(result, requirement)
            if not matched:
                return {"valid": False, "reason": reason}

        elif "eip155" in network or "base" in network:
            rpc_url = os.getenv("BASE_RPC_URL", "https://mainnet.base.org")
            async with httpx.AsyncClient(timeout=10.0) as client:
                res = await client.post(
                    rpc_url,
                    json={
                        "jsonrpc": "2.0",
                        "id": 1,
                        "method": "eth_getTransactionReceipt",
                        "params": [tx_hash]
                    }
                )
                res.raise_for_status()
                data = res.json()
            
            result = data.get("result")
            if not result:
                return {"valid": False, "reason": "EVM Transaction not found or not yet finalized"}
                
            status = result.get("status")
            if status not in ("0x1", 1):
                return {"valid": False, "reason": f"EVM Transaction reverted on chain: status={status}"}

            matched, reason = _evm_transfer_satisfies_requirement(result, requirement)
            if not matched:
                return {"valid": False, "reason": reason}
        
        else:
            return {"valid": False, "reason": f"Unsupported network: {network}"}

        recorded, reason = _record_payment_use(
            tx_hash,
            network,
            requirement,
            credit_manager,
            purpose,
        )
        if not recorded:
            return {"valid": False, "reason": reason}

        logger.info("NATIVELY VERIFIED %s: %s", network, tx_hash)
        return {"valid": True, "network": network}
        
    except ValueError as e:
        return {"valid": False, "reason": str(e)}
    except Exception as e:
        logger.error("Native RPC verification failed: %s", e)
        return {"valid": False, "reason": f"Native RPC failure: {str(e)}"}

async def _settle_payment(payment_payload: str, payment_requirements: list[dict]) -> dict:
    """Payment has inherently settled on chain during Native RPC Verification."""
    return {"success": True}


@app.middleware("http")
async def x402_payment_middleware(request: Request, call_next):
    """
    x402 payment gate middleware with tiered pricing.

    Flow:
    1. Check if the route is priced (None = free, pass through)
    2. Check for PAYMENT-SIGNATURE header
    3. If missing → 402 with payment requirements for BOTH networks
    4. If present → verify → proceed or reject
    5. After response → settle payment
    """
    if _anthropic_only_mode() and not _anthropic_only_allowed_path(request.url.path):
        return _anthropic_only_block_response()

    discovery_limit = _discovery_rate_limit_response(request)
    if discovery_limit is not None:
        return discovery_limit

    path = request.url.path
    try:
        price = _get_price_for_request(request)
    except ValueError as e:
        return _apply_x402_cors_headers(
            request,
            JSONResponse(
                status_code=400,
                content={"error": "Bad Request", "message": str(e)},
            ),
        )

    # Free endpoints pass through
    if price is None:
        return await call_next(request)

    try:
        credit_subject = _starter_credit_subject(request)
    except ValueError as e:
        _record_product_event(
            "credit_drawdown_failed",
            request,
            price_usdc=price,
            reason="invalid_starter_identity_header",
        )
        return _apply_x402_cors_headers(
            request,
            JSONResponse(
                status_code=400,
                content={
                    "error": "Bad Request",
                    "message": str(e),
                },
            ),
        )

    if credit_subject is not None:
        subject, subject_type, require_wallet_history = credit_subject
        credit_cost = _credit_cost_for_request(request)
        if credit_cost is None:
            _record_product_event(
                "credit_drawdown_failed",
                request,
                price_usdc=price,
                reason="missing_credit_cost",
            )
            return _apply_x402_cors_headers(
                request,
                JSONResponse(
                    status_code=500,
                    content={
                        "error": "Internal Error",
                        "message": "Credit cost is not configured for this paid route.",
                    },
                ),
            )

        mgr: CreditManager = request.app.state.credits
        client_ip = _request_client_ip(request)
        starter = await mgr.ensure_starter_allowance(
            subject=subject,
            subject_type=subject_type,
            ip=client_ip,
            device_id=request.headers.get("X-DEVICE-ID"),
            session_id=request.headers.get("X-SESSION-ID"),
            user_agent=request.headers.get("user-agent"),
            require_wallet_history=require_wallet_history,
        )

        if mgr.spend_credits(subject, credit_cost):
            credits_remaining = mgr.get_balance(subject)
            request.state.starter_credit_context = {
                "subject_type": subject_type,
                "credits_spent": credit_cost,
                "credits_remaining": credits_remaining,
                "starter_granted": starter.granted_credits,
            }
            logger.info(
                "CREDIT DRAWDOWN: Spent %.1f from %s:%s for %s",
                credit_cost,
                subject_type,
                subject,
                path,
            )
            _record_product_event(
                "credit_drawdown_success",
                request,
                price_usdc=price,
                wallet_hash=_wallet_hash(subject),
                metadata={
                    "credits_spent": credit_cost,
                    "credits_remaining": credits_remaining,
                    "starter_subject_type": subject_type,
                    "starter_granted": starter.granted_credits,
                },
            )
            response = await call_next(request)
            return _apply_credit_response_headers(response, request)
        else:
            logger.warning("INSUFFICIENT CREDITS: %s:%s for %s", subject_type, subject, path)
            _record_product_event(
                "credit_drawdown_failed",
                request,
                price_usdc=price,
                reason="insufficient_credits",
                wallet_hash=_wallet_hash(subject),
                metadata={
                    "credits_required": credit_cost,
                    "credits_remaining": mgr.get_balance(subject),
                    "starter_subject_type": subject_type,
                    "starter_reason": starter.reason,
                },
            )
            # Proceed to normal 402 challenge below

    # Build multi-network payment requirements
    payment_reqs = settings.payment_requirements(price)

    # Check for x402 payment proof. X-PAYMENT is the standard retry header;
    # PAYMENT-SIGNATURE is retained for existing Blocksize demo clients.
    payment_header = (
        request.headers.get("X-PAYMENT")
        or request.headers.get("PAYMENT-SIGNATURE")
    )

    if not payment_header:
        payment_required = _x402_payment_required(request, payment_reqs)
        requirements_b64 = _encode_payment_required(payment_required)
        _record_product_event(
            "payment_required",
            request,
            price_usdc=price,
            metadata={
                "networks_offered": [
                    str(requirement.get("network"))
                    for requirement in payment_reqs
                    if requirement.get("network")
                ],
            },
        )

        return _apply_x402_cors_headers(
            request,
            JSONResponse(
                status_code=402,
                content={
                    **payment_required,
                    "error": "Payment Required",
                    "message": (
                        f"This endpoint requires a payment of ${price} USDC. "
                        f"Send a signed x402 payment in the X-PAYMENT header. "
                        f"Accepted networks: Solana (preferred), Base L2."
                    ),
                    "price_usdc": str(price),
                    "starter_credits": {
                        "positioning": "Start with 50 live data credits",
                        "allowance_credits": STARTER_CREDIT_ALLOWANCE,
                        "credit_cost": _credit_cost_for_request(request),
                        "identity_headers": [
                            "X-AGENT-WALLET",
                            "X-AUTHENTICATED-USER",
                            "X-USER-ID",
                            "X-AGENT-ID",
                            "X-DEVICE-ID",
                            "X-SESSION-ID",
                        ],
                        "upgrade_path": "After starter credits are exhausted, use x402 payment or prepaid credit top-ups.",
                    },
                    "networks": [
                        {"name": "Solana", "caip2": settings.x402.solana_network},
                        {"name": "Base", "caip2": settings.x402.base_network},
                    ],
                    "legacy_requirements": payment_reqs,
                },
                headers={
                    "PAYMENT-REQUIRED": requirements_b64,
                },
            ),
        )

    # Verify the payment
    _record_product_event(
        "payment_proof_submitted",
        request,
        price_usdc=price,
        metadata={"proof_hash": fingerprint(payment_header)},
    )
    try:
        verification = await _verify_payment(
            payment_header,
            payment_reqs,
            request.app.state.credits,
            purpose=f"{request.method} {path}",
        )
        if not verification.get("valid", False):
            _record_product_event(
                "payment_failed",
                request,
                price_usdc=price,
                reason=str(verification.get("reason", "unknown")),
            )
            return _apply_x402_cors_headers(
                request,
                JSONResponse(
                    status_code=402,
                    content={
                        "error": "Payment Invalid",
                        "message": "Payment verification failed.",
                        "details": verification.get("reason", "Unknown"),
                    },
                ),
            )
    except httpx.HTTPError as e:
        logger.error("Facilitator verification failed: %s", e)
        _record_product_event(
            "payment_failed",
            request,
            price_usdc=price,
            reason="verification_unavailable",
        )
        return _apply_x402_cors_headers(
            request,
            JSONResponse(
                status_code=502,
                content={
                    "error": "Payment Verification Unavailable",
                    "message": "Could not reach payment facilitator.",
                },
            ),
        )

    # Payment verified — serve the request
    _record_product_event(
        "payment_verified",
        request,
        price_usdc=price,
        network=str(verification.get("network") or ""),
        metadata={"mock": bool(verification.get("mock"))},
    )
    response = await call_next(request)

    # Settle payment (best-effort)
    try:
        settlement = await _settle_payment(payment_header, payment_reqs)
        if settlement.get("success"):
            settlement_b64 = base64.b64encode(json.dumps(settlement).encode()).decode()
            response.headers["PAYMENT-RESPONSE"] = settlement_b64
            response.headers["X-PAYMENT-RESPONSE"] = settlement_b64
            logger.info("Payment settled: %s USDC for %s", price, path)
    except httpx.HTTPError as e:
        logger.error("Payment settlement failed: %s", e)

    return response


@app.middleware("http")
async def observability_middleware(request: Request, call_next):
    started = time.perf_counter()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        return response
    finally:
        latency_ms = (time.perf_counter() - started) * 1000
        _record_http_usage(request, status_code, latency_ms)


# ---------------------------------------------------------------------------
# Data Endpoints
# ---------------------------------------------------------------------------

@app.get("/v1/vwap/{pair}", responses=X402_RESPONSE, openapi_extra=X402_CRYPTO_PAYMENT_INFO)
async def get_vwap(pair: str, request: Request) -> dict[str, Any]:
    """Get real-time VWAP for a crypto pair. Cost: $0.002–$0.004 USDC."""
    try:
        client: BlocksizeClient = request.app.state.blocksize
        clean = _normalise_symbol(pair, "pair")
        vwap_data = await client.get_vwap_latest(clean)
        resp = VWAPResponse(data=vwap_data).model_dump()
        resp["meta"] = {"provider": "Blocksize Capital", "endpoint": "Real-Time VWAP", "asset_class": "crypto"}
        if credit_meta := _credit_meta_for_request(request):
            resp["meta"]["credits"] = credit_meta
        return resp
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except BlocksizeAPIError as e:
        raise HTTPException(status_code=502, detail=ErrorResponse(
            error_code="BLOCKSIZE_ERROR", message=f"Failed to retrieve VWAP for {pair}", details=str(e),
        ).model_dump())


@app.get("/v1/bidask/{pair}", responses=X402_RESPONSE, openapi_extra=X402_BIDASK_PAYMENT_INFO)
async def get_bidask(pair: str, request: Request) -> dict[str, Any]:
    """Get bid/ask snapshot for a shared symbol. Cost: $0.002–$0.008 USDC."""
    try:
        client: BlocksizeClient = request.app.state.blocksize
        clean = _normalise_symbol(pair, "pair")
        bidask_data = await client.get_bidask_snapshot(clean)
        resp = BidAskResponse(data=bidask_data).model_dump()
        resp["meta"] = {"provider": "Blocksize Capital", "endpoint": "Bid/Ask Snapshot", "asset_class": "multi_asset"}
        if credit_meta := _credit_meta_for_request(request):
            resp["meta"]["credits"] = credit_meta
        return resp
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except BlocksizeAPIError as e:
        raise HTTPException(status_code=502, detail=ErrorResponse(
            error_code="BLOCKSIZE_ERROR", message=f"Failed to retrieve bid/ask for {pair}", details=str(e),
        ).model_dump())


@app.get("/v1/state/{pair}", responses=X402_RESPONSE, openapi_extra=X402_CRYPTO_PAYMENT_INFO)
async def get_state_price_endpoint(pair: str, request: Request) -> dict[str, Any]:
    """Get pool-derived state price for a covered crypto/protocol pair."""
    try:
        client: BlocksizeClient = request.app.state.blocksize
        stream_cache: BlocksizeStreamCache | None = getattr(request.app.state, "stream_cache", None)
        clean = _normalise_symbol(pair, "pair")
        cache_error: str | None = None
        try:
            if stream_cache and stream_cache.enabled:
                data = await stream_cache.get_state_price(clean)
            else:
                raise BlocksizeAPIError(-32004, "state stream cache disabled")
        except BlocksizeAPIError as exc:
            cache_error = exc.message
            data = await client.get_state_price(clean)
        source_method = (
            "state_subscribe_cache"
            if data.source.endswith("state_subscribe_cache")
            else "state_instruments+state_pool"
        )
        resp = {
            "status": "ok",
            "data": data.model_dump(),
            "methodology": {
                "type": "blocksize_state_price_from_pool_v1",
                "steps": [
                    "Normalize the requested symbol.",
                    "Read aggregate state price from state_subscribe cache when available.",
                    "If the cache does not cover the symbol, resolve matching pool instruments through state_instruments.",
                    "Fetch documented state_pool snapshots for matching pools and return a weighted state price.",
                ],
                "source_method": source_method,
                "limitations": [
                    "Not every market symbol has state-pool coverage; plain SOLUSD is distinct from pool symbols such as MSOLUSD or JUPSOLUSD.",
                ],
            },
            "meta": {
                "provider": "Blocksize Capital",
                "endpoint": "AMM State Price",
                "asset_class": "crypto_state",
                "upstream_methods": (
                    ["state_subscribe"]
                    if source_method == "state_subscribe_cache"
                    else ["state_instruments", "state_pool"]
                ),
            },
        }
        if cache_error:
            resp["meta"]["cache_note"] = cache_error
        if credit_meta := _credit_meta_for_request(request):
            resp["meta"]["credits"] = credit_meta
        return resp
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except BlocksizeAPIError as e:
        raise HTTPException(status_code=502, detail=ErrorResponse(
            error_code="BLOCKSIZE_ERROR", message=f"Failed to retrieve state price for {pair}", details=str(e),
        ).model_dump())


@app.get("/v1/vwap30m/{pair}", responses=X402_RESPONSE, openapi_extra=X402_CRYPTO_PAYMENT_INFO)
async def get_vwap_30m_endpoint(
    pair: str,
    request: Request,
    include_trades: bool = Query(False, description="Include closingprice_trades evidence"),
) -> dict[str, Any]:
    """Get latest completed 30-minute close via Blocksize closingprice_list."""
    try:
        client: BlocksizeClient = request.app.state.blocksize
        clean = _normalise_symbol(pair, "pair")
        data = await client.get_vwap_30min(clean)
        evidence: dict[str, Any] = {"included": False}
        if include_trades:
            trades = await client.get_vwap_30min_trades(clean)
            evidence = {
                "included": True,
                "upstream_method": "closingprice_trades",
                "trade_count": len(trades),
                "trades": trades,
            }
        resp = {
            "status": "ok",
            "data": data.model_dump(),
            "methodology": {
                "type": "blocksize_latest_30m_close_v1",
                "steps": [
                    "Normalize requested symbol into base and quote.",
                    "Request closingprice_list for the latest completed 30-minute UTC boundary.",
                    "Return the matching base/quote close from the upstream prices list.",
                    "When include_trades=true, attach closingprice_trades evidence for the same base/quote/window.",
                ],
                "upstream_method": "closingprice_list",
                "evidence": evidence,
            },
            "meta": {
                "provider": "Blocksize Capital",
                "endpoint": "30-Minute VWAP Close",
                "asset_class": "crypto",
            },
        }
        if credit_meta := _credit_meta_for_request(request):
            resp["meta"]["credits"] = credit_meta
        return resp
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except BlocksizeAPIError as e:
        raise HTTPException(status_code=502, detail=ErrorResponse(
            error_code="BLOCKSIZE_ERROR", message=f"Failed to retrieve 30-minute VWAP for {pair}", details=str(e),
        ).model_dump())


@app.get("/v1/vwap24h/{pair}", responses=X402_RESPONSE, openapi_extra=X402_CRYPTO_PAYMENT_INFO)
async def get_vwap_24h_endpoint(pair: str, request: Request) -> dict[str, Any]:
    """Get true 24-hour fixed VWAP from the websocket-backed cache."""
    try:
        client: BlocksizeClient = request.app.state.blocksize
        stream_cache: BlocksizeStreamCache | None = getattr(request.app.state, "stream_cache", None)
        clean = _normalise_symbol(pair, "pair")
        if stream_cache and stream_cache.enabled:
            data = await stream_cache.get_vwap_24h(clean)
        else:
            data = await client.get_vwap_24hr(clean)
        resp = {
            "status": "ok",
            "data": data.model_dump(),
            "methodology": {
                "type": "blocksize_fixed_24h_vwap_stream_cache_v1",
                "steps": [
                    "Maintain an authenticated Blocksize websocket subscription to fixedvwap_subscribe.",
                    "Store snapshot and update messages in a bounded in-memory cache.",
                    "Serve paid HTTP reads from the fresh cache entry for the requested pair.",
                ],
                "upstream_method": "fixedvwap_subscribe"
                if data.source.endswith("fixedvwap_subscribe_cache")
                else "vwap_24h_latest",
                "fallback": False,
                "limitations": [],
            },
            "meta": {
                "provider": "Blocksize Capital",
                "endpoint": "24-Hour Fixed VWAP",
                "asset_class": "crypto",
            },
        }
        if credit_meta := _credit_meta_for_request(request):
            resp["meta"]["credits"] = credit_meta
        return resp
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except BlocksizeAPIError as e:
        raise HTTPException(status_code=502, detail=ErrorResponse(
            error_code="BLOCKSIZE_ERROR", message=f"Failed to retrieve 24-hour VWAP for {pair}", details=str(e),
        ).model_dump())


@app.get("/v1/fx/{pair}", responses=X402_RESPONSE, openapi_extra=X402_TRADFI_PAYMENT_INFO)
async def get_fx(pair: str, request: Request) -> dict[str, Any]:
    """Get FX rate. Cost: $0.005 USDC."""
    try:
        client: BlocksizeClient = request.app.state.blocksize
        clean = _normalise_symbol(pair, "pair")
        data = await client.get_fx_rate(clean)
        resp = {"status": "ok", "data": data.model_dump(), "meta": {"provider": "Blocksize Capital", "endpoint": "FX Rate", "asset_class": "fx"}}
        if credit_meta := _credit_meta_for_request(request):
            resp["meta"]["credits"] = credit_meta
        return resp
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except BlocksizeAPIError as e:
        raise HTTPException(status_code=502, detail=ErrorResponse(
            error_code="BLOCKSIZE_ERROR", message=f"Failed to retrieve FX for {pair}", details=str(e),
        ).model_dump())

@app.get("/v1/metal/{ticker}", responses=X402_RESPONSE, openapi_extra=X402_TRADFI_PAYMENT_INFO)
async def get_metal(ticker: str, request: Request) -> dict[str, Any]:
    """Get metal spot price. Cost: $0.005 USDC."""
    try:
        client: BlocksizeClient = request.app.state.blocksize
        clean = _normalise_symbol(ticker, "ticker")
        data = await client.get_metal_price(clean)
        resp = {"status": "ok", "data": data.model_dump(), "meta": {"provider": "Blocksize Capital", "endpoint": "Metal Price", "asset_class": "metal"}}
        if credit_meta := _credit_meta_for_request(request):
            resp["meta"]["credits"] = credit_meta
        return resp
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except BlocksizeAPIError as e:
        raise HTTPException(status_code=502, detail=ErrorResponse(
            error_code="BLOCKSIZE_ERROR", message=f"Failed to retrieve metal for {ticker}", details=str(e),
        ).model_dump())


@app.get("/v1/batch", responses=X402_RESPONSE, openapi_extra=X402_BATCH_PAYMENT_INFO)
async def batch_request(reqs: str, request: Request) -> dict[str, Any]:
    """
    Execute a batch of data queries.
    Pass a comma separated list of svc:pair in the `reqs` query parameter.
    Example: /v1/batch?reqs=vwap:BTCUSD,bidask:ETHUSD,fx:EURUSD
    """
    import asyncio
    
    try:
        queries = _parse_batch_reqs(reqs)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    results = []
    
    # We execute requests concurrently for extreme speed
    tasks = []
    
    async def _safe_fetch(svc: str, ticker: str):
        try:
            if svc == "vwap":
                return await get_vwap(ticker, request)
            elif svc == "bidask":
                return await get_bidask(ticker, request)
            elif svc == "state":
                return await get_state_price_endpoint(ticker, request)
            elif svc == "vwap30m":
                return await get_vwap_30m_endpoint(ticker, request)
            elif svc == "vwap24h":
                return await get_vwap_24h_endpoint(ticker, request)
            elif svc == "fx":
                return await get_fx(ticker, request)
            elif svc == "metal":
                return await get_metal(ticker, request)
            else:
                return {
                    "status": "error",
                    "error_code": "UNSUPPORTED_SERVICE",
                    "message": f"Service type {svc} is not currently offered by this gateway",
                    "meta": {"endpoint": "Unsupported", "asset_class": "unknown"}
                }
        except HTTPException as he:
            return he.detail
        except Exception as e:
            return {"status": "error", "error_code": "BATCH_EXECUTION_ERROR", "message": str(e)}

    for svc, ticker, _raw_query in queries:
        tasks.append(_safe_fetch(svc, ticker))
        
    gathered_results = await asyncio.gather(*tasks)
    
    for (_svc, _ticker, raw_query), result in zip(queries, gathered_results):
        results.append({
            "request": raw_query,
            "response": result
        })
        
    return {
        "status": "ok",
        "batch_size": len(results),
        "results": results,
        "meta": {
            "credits": _credit_meta_for_request(request),
        },
    }


READINESS_PRODUCTS = {
    "token_market_quality_indicator",
    "state_divergence_indicator",
    "solana_token_brief",
    "trader_alpha_pack",
}


def _symbols_from_payload(payload: dict[str, Any]) -> list[str]:
    symbols = payload.get("symbols") or payload.get("watchlist") or payload.get("symbol")
    if symbols is None:
        symbols = ["SOLUSD"]
    if isinstance(symbols, str):
        if "," in symbols:
            symbols = [item.strip() for item in symbols.split(",") if item.strip()]
        else:
            symbols = [symbols]
    if not isinstance(symbols, list) or not symbols:
        raise ValueError("symbols, watchlist, or symbol must include at least one symbol")
    if len(symbols) > 25:
        raise ValueError("Capability check supports up to 25 symbols")
    return [_normalise_symbol(str(symbol), "symbol") for symbol in symbols]


def _optional_feed_flags(payload: dict[str, Any]) -> dict[str, bool]:
    optional = payload.get("optional_feeds") if isinstance(payload.get("optional_feeds"), dict) else {}
    return {
        "state_coverage": bool(
            payload.get("include_state_coverage", optional.get("state_coverage", False))
        ),
        "state_price": bool(
            payload.get("include_state_price", optional.get("state_price", False))
        ),
        "vwap_windows": bool(
            payload.get("include_windows", optional.get("vwap_windows", False))
        ),
    }


@app.post("/v1/capabilities/check")
async def check_data_capabilities(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """
    Check whether the current key has enough feed coverage for a product.

    This is a free readiness check. It uses instrument catalogs and known method
    availability, not paid price snapshots.
    """
    product = str(payload.get("product") or "token_market_quality_indicator")
    if product not in READINESS_PRODUCTS:
        raise HTTPException(status_code=400, detail=f"Unsupported product: {product}")
    try:
        symbols = _symbols_from_payload(payload)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    optional_flags = _optional_feed_flags(payload)
    client: BlocksizeClient = request.app.state.blocksize
    stream_cache: BlocksizeStreamCache | None = getattr(request.app.state, "stream_cache", None)

    try:
        vwap_symbols = {item.upper() for item in await client.list_vwap_instruments()}
    except BlocksizeAPIError as exc:
        vwap_symbols = set()
        vwap_catalog_error = str(exc)
    else:
        vwap_catalog_error = None

    try:
        bidask_symbols = {item.upper() for item in await client.list_bidask_instruments()}
    except BlocksizeAPIError as exc:
        bidask_symbols = set()
        bidask_catalog_error = str(exc)
    else:
        bidask_catalog_error = None

    state_instruments: list[Any] = []
    state_catalog_error = None
    state_catalog_required = (
        optional_flags["state_coverage"]
        or optional_flags["state_price"]
        or product == "state_divergence_indicator"
    )
    if state_catalog_required:
        try:
            state_instruments = await client.list_state_instruments()
        except BlocksizeAPIError as exc:
            state_catalog_error = str(exc)

    symbol_results: list[dict[str, Any]] = []
    for symbol in symbols:
        vwap_available = symbol in vwap_symbols
        bidask_available = symbol in bidask_symbols
        has_current_market = vwap_available or bidask_available
        state_coverage = (
            _state_coverage_from_instruments(symbol=symbol, instruments=state_instruments)
            if state_catalog_required and state_catalog_error is None
            else None
        )
        fixed_vwap_cached = bool(
            optional_flags["vwap_windows"]
            and stream_cache
            and stream_cache.enabled
            and stream_cache.has_vwap_24h(symbol)
        )

        required_feeds: dict[str, Any] = {
            "current_market_price": {
                "required": True,
                "available": has_current_market,
                "satisfied_by": [
                    name
                    for name, available in (
                        ("vwap_latest", vwap_available),
                        ("bidask_getSnapshot", bidask_available),
                    )
                    if available
                ],
            }
        }
        if product == "state_divergence_indicator":
            required_feeds["state_pool"] = {
                "required": True,
                "available": bool(
                    state_catalog_error is None
                    and state_coverage is not None
                    and state_coverage["matched_count"] > 0
                ),
                "satisfied_by": ["state_instruments", "state_pool"]
                if state_coverage is not None and state_coverage["matched_count"] > 0
                else [],
                "coverage": state_coverage,
                "reason": (
                    None
                    if state_coverage is not None and state_coverage["matched_count"] > 0
                    else "No matching state_instruments pools for this symbol."
                ),
            }

        optional_feeds = {
            "state_instruments": {
                "requested": optional_flags["state_coverage"],
                "available": bool(
                    optional_flags["state_coverage"]
                    and state_catalog_error is None
                    and state_coverage is not None
                    and state_coverage["matched_count"] > 0
                ),
                "coverage": state_coverage,
                "error": state_catalog_error,
            },
            "state_pool": {
                "requested": optional_flags["state_price"],
                "available": bool(
                    optional_flags["state_price"]
                    and state_catalog_error is None
                    and state_coverage is not None
                    and state_coverage["matched_count"] > 0
                ),
                "coverage": state_coverage,
                "reason": (
                    "state_pool is documented HTTP, resolved through state_instruments pools."
                    if state_coverage is not None and state_coverage["matched_count"] > 0
                    else "No matching state_instruments pools for this symbol."
                ),
            },
            "closingprice_list": {
                "requested": optional_flags["vwap_windows"],
                "available": optional_flags["vwap_windows"],
                "satisfied_by": ["closingprice_list"],
                "reason": "30-minute close is available through documented closingprice_list HTTP method.",
            },
            "fixedvwap_subscribe": {
                "requested": optional_flags["vwap_windows"],
                "available": fixed_vwap_cached,
                "satisfied_by": ["fixedvwap_subscribe_cache"] if fixed_vwap_cached else [],
                "reason": (
                    "24-hour fixed VWAP is available from the local fixedvwap_subscribe cache."
                    if fixed_vwap_cached
                    else "24-hour fixed VWAP requires the fixedvwap_subscribe stream cache to be enabled and populated for this symbol."
                ),
            },
        }
        missing_required = [
            name for name, item in required_feeds.items() if not item.get("available")
        ]
        symbol_results.append(
            {
                "symbol": symbol,
                "ready": not missing_required,
                "missing_required": missing_required,
                "required_feeds": required_feeds,
                "optional_feeds": optional_feeds,
            }
        )

    all_ready = all(item["ready"] for item in symbol_results)
    return {
        "status": "ok",
        "product": product,
        "ready": all_ready,
        "methodology": {
            "type": "blocksize_product_data_readiness_v1",
            "steps": [
                "Normalize requested symbols.",
                "Check VWAP and bid/ask instrument catalogs for current market coverage.",
                "Check state_instruments when state coverage, state price, or state-divergence products are requested.",
                "Resolve state price through documented state_pool pool snapshots rather than an undocumented ticker-level method.",
                "Mark 30-minute VWAP-window support available through closingprice_list and 24-hour fixed VWAP available when fixedvwap_subscribe cache is populated.",
                "Return missing required feeds before the caller spends credits on a paid workflow.",
            ],
        },
        "opt_in_policy": {
            "default_required": ["current_market_price"],
            "optional_default_off": [
                "state_instruments",
                "state_pool",
                "closingprice_list",
                "fixedvwap_subscribe_cache",
            ],
        },
        "catalog_errors": {
            "vwap_instruments": vwap_catalog_error,
            "bidask_instruments": bidask_catalog_error,
            "state_instruments": state_catalog_error,
        },
        "symbols": symbol_results,
    }


@app.post("/v1/briefs/market", responses=X402_RESPONSE)
async def agent_market_brief(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """Create a decision-ready market brief. Cost: 10 credits or $0.25 USDC."""
    import asyncio

    symbols = payload.get("symbols") or payload.get("symbol")
    if isinstance(symbols, str):
        symbols = [symbols]
    if not isinstance(symbols, list) or not symbols:
        raise HTTPException(status_code=400, detail="symbols must include at least one symbol")
    if len(symbols) > 8:
        raise HTTPException(status_code=400, detail="Market brief supports up to 8 symbols")

    client: BlocksizeClient = request.app.state.blocksize
    requested_service = payload.get("service")

    async def fetch_one(raw_symbol: Any) -> dict[str, Any]:
        symbol = _normalise_symbol(str(raw_symbol), "symbol")
        service = _service_for_symbol(symbol, str(requested_service) if requested_service else None)
        try:
            snapshot = await _fetch_service_snapshot(client, service=service, symbol=symbol)
            flags = _quality_flags(snapshot)
            return {
                "symbol": symbol,
                "asset_class": snapshot["asset_class"],
                "service": snapshot["service"],
                "latest": snapshot["data"],
                "value": snapshot.get("value"),
                "freshness_ms": _freshness_ms(snapshot.get("timestamp")),
                "spread_bps": snapshot.get("spread_bps"),
                "quality_flags": flags,
            }
        except BlocksizeAPIError as e:
            return {
                "symbol": symbol,
                "status": "error",
                "error_code": "BLOCKSIZE_ERROR",
                "message": str(e),
            }

    instruments = await asyncio.gather(*(fetch_one(symbol) for symbol in symbols))
    successful = [item for item in instruments if item.get("status") != "error"]
    if not successful:
        raise HTTPException(status_code=502, detail={"error_code": "NO_MARKET_DATA", "items": instruments})

    any_flags = sorted({flag for item in successful for flag in item.get("quality_flags", [])})
    market_state = "caution" if any_flags else "normal"
    response_core = {
        "methodology": {
            "type": "agent_market_brief_v1",
            "steps": [
                "Normalize and classify each requested symbol.",
                "Fetch one bounded live snapshot per symbol from the matching Blocksize raw-data service.",
                "Compute freshness, spread, and missing-value quality flags where available.",
                "Summarize actionability from returned data quality flags; no trade is executed.",
                "Persist a receipt with request hash, response hash, source endpoints, and lookup URL.",
            ],
            "limitations": [
                "This is market-data context, not investment advice.",
                "MVP brief uses current snapshots only; historical trend analytics are not included.",
            ],
        },
        "summary": {
            "headline": f"{len(successful)} of {len(instruments)} requested instruments returned live data.",
            "market_state": market_state,
            "actionability": "usable_with_caution" if any_flags else "usable_for_small_decision",
        },
        "instruments": instruments,
        "risks": [
            {
                "severity": "medium" if flag == "wide_spread" else "low",
                "code": flag,
                "message": flag.replace("_", " "),
            }
            for flag in any_flags
        ],
    }
    source_endpoints = [
        f"/v1/{item['service']}/{item['symbol']}"
        for item in successful
        if item.get("service") and item.get("symbol")
    ]
    receipt = _response_receipt(
        request,
        product="agent_market_brief",
        subject=",".join(str(s).upper() for s in symbols),
        request_payload=payload,
        response_payload=response_core,
        source_endpoints=source_endpoints,
    )
    return {
        "status": "ok",
        "product": "agent_market_brief",
        "credit_cost": CREDIT_COSTS["market_brief"],
        "as_of": _utc_now_iso(),
        **response_core,
        "provenance": receipt,
        "meta": {"credits": _credit_meta_for_request(request)},
    }


@app.post("/v1/checks/pre-trade", responses=X402_RESPONSE)
async def pre_trade_sanity_check(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """Run a read-only pre-trade sanity check. Cost: 5 credits or $0.10 USDC."""
    symbol = _normalise_symbol(str(payload.get("symbol") or ""), "symbol")
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")

    side = str(payload.get("side") or "unknown").lower()
    notional_usd = float(payload.get("notional_usd") or 0)
    reference_price = payload.get("reference_price")
    reference_price = float(reference_price) if reference_price is not None else None
    max_spread_bps = float(payload.get("max_spread_bps") or 50)
    max_age_ms = int(payload.get("max_age_ms") or 60_000)

    client: BlocksizeClient = request.app.state.blocksize
    service = _service_for_symbol(symbol, str(payload.get("service")) if payload.get("service") else None)
    if service == "vwap":
        service = "bidask"
    snapshot = await _fetch_service_snapshot(client, service=service, symbol=symbol)
    freshness = _freshness_ms(snapshot.get("timestamp"))
    spread_bps = snapshot.get("spread_bps")
    market_value = float(snapshot.get("value") or 0)
    reference_drift_bps = (
        abs(market_value - reference_price) / reference_price * 10000
        if reference_price and market_value
        else None
    )
    checks = {
        "instrument_supported": True,
        "quote_fresh": freshness is None or freshness <= max_age_ms,
        "spread_within_limit": spread_bps is None or spread_bps <= max_spread_bps,
        "reference_price_drift_bps": reference_drift_bps,
        "reference_price_within_limit": reference_drift_bps is None or reference_drift_bps <= max_spread_bps,
        "notional_supplied": notional_usd > 0,
    }
    blocking = not checks["quote_fresh"] or not checks["spread_within_limit"]
    decision = "block" if blocking else ("caution" if not checks["reference_price_within_limit"] else "pass")
    response_core = {
        "methodology": {
            "type": "pre_trade_sanity_check_v1",
            "steps": [
                "Normalize the requested symbol and route to the relevant read-only market-data service.",
                "Fetch the latest bid/ask-style snapshot where possible.",
                "Compare quote age against max_age_ms.",
                "Compare spread_bps against max_spread_bps when spread is available.",
                "Compare supplied reference_price against current market value when provided.",
                "Return pass, caution, or block without executing or routing any trade.",
            ],
            "decision_rules": {
                "block": "Quote is stale or spread exceeds configured max_spread_bps.",
                "caution": "Reference price drift exceeds configured threshold.",
                "pass": "Configured freshness, spread, and reference checks pass.",
            },
        },
        "decision": decision,
        "side": side,
        "notional_usd": notional_usd,
        "checks": checks,
        "market": snapshot,
        "recommendation": {
            "blocking": blocking,
            "message": (
                "Do not proceed until a fresher/tighter quote is available."
                if blocking
                else "Market data passed configured sanity checks."
            ),
        },
    }
    receipt = _response_receipt(
        request,
        product="pre_trade_sanity_check",
        subject=symbol,
        request_payload=payload,
        response_payload=response_core,
        source_endpoints=[snapshot["endpoint"]],
    )
    return {
        "status": "ok",
        "product": "pre_trade_sanity_check",
        "credit_cost": CREDIT_COSTS["pre_trade_check"],
        **response_core,
        "provenance": receipt,
        "meta": {"credits": _credit_meta_for_request(request)},
    }


@app.post("/v1/receipts/price", responses=X402_RESPONSE)
async def audit_grade_price_receipt(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """Create an audit-grade receipt for one price lookup. Cost: 10 credits or $0.25 USDC."""
    symbol = _normalise_symbol(str(payload.get("symbol") or ""), "symbol")
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")
    service = _service_for_symbol(symbol, str(payload.get("service")) if payload.get("service") else None)
    snapshot = await _fetch_service_snapshot(request.app.state.blocksize, service=service, symbol=symbol)
    response_core = {
        "methodology": {
            "type": "audit_grade_price_receipt_v1",
            "steps": [
                "Normalize symbol and requested source service.",
                "Fetch the corresponding live Blocksize raw-data snapshot.",
                "Capture the returned value, timestamp, raw payload, request hash, and response hash.",
                "Store receipt metadata in the credit ledger for later provenance lookup.",
            ],
            "hashing": "Stable JSON serialization with sha256 fingerprints; receipt ids are derived from product, subject, timestamp, request, and response.",
        },
        "price": {
            "service": service,
            "symbol": symbol,
            "value": snapshot.get("value"),
            "timestamp": snapshot.get("timestamp"),
            "raw": snapshot.get("data"),
        },
        "client_reference_id": payload.get("client_reference_id"),
        "purpose": payload.get("purpose"),
    }
    receipt = _response_receipt(
        request,
        product="audit_grade_price_receipt",
        subject=symbol,
        request_payload=payload,
        response_payload=response_core,
        source_endpoints=[snapshot["endpoint"]],
    )
    return {
        "status": "ok",
        "product": "audit_grade_price_receipt",
        "credit_cost": CREDIT_COSTS["audit_receipt"],
        "receipt": receipt,
        **response_core,
        "meta": {"credits": _credit_meta_for_request(request)},
    }


@app.post("/v1/snapshots/macro", responses=X402_RESPONSE)
async def multi_asset_macro_snapshot(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """Create a bounded multi-asset macro snapshot. Cost: 25 credits or $1.00 USDC."""
    import asyncio

    universe = payload.get("universe") or payload.get("symbols") or ["BTCUSD", "ETHUSD", "EURUSD", "XAUUSD"]
    if isinstance(universe, str):
        universe = [item.strip() for item in universe.split(",") if item.strip()]
    if not isinstance(universe, list) or not universe:
        raise HTTPException(status_code=400, detail="universe must include at least one symbol")
    if len(universe) > 12:
        raise HTTPException(status_code=400, detail="Macro snapshot supports up to 12 symbols")

    client: BlocksizeClient = request.app.state.blocksize

    async def fetch_macro(raw_symbol: Any) -> dict[str, Any]:
        symbol = _normalise_symbol(str(raw_symbol), "symbol")
        service = _service_for_symbol(symbol)
        try:
            snapshot = await _fetch_service_snapshot(client, service=service, symbol=symbol)
            return {
                "symbol": symbol,
                "asset_class": snapshot["asset_class"],
                "service": service,
                "latest": snapshot["data"],
                "value": snapshot.get("value"),
                "quality_flags": _quality_flags(snapshot),
            }
        except BlocksizeAPIError as e:
            return {"symbol": symbol, "status": "error", "message": str(e)}

    assets = await asyncio.gather(*(fetch_macro(symbol) for symbol in universe))
    successful = [asset for asset in assets if asset.get("status") != "error"]
    if not successful:
        raise HTTPException(status_code=502, detail={"error_code": "NO_MARKET_DATA", "items": assets})
    flags = sorted({flag for asset in successful for flag in asset.get("quality_flags", [])})
    regime_label = "mixed" if flags else "orderly"
    response_core = {
        "methodology": {
            "type": "multi_asset_macro_snapshot_v1",
            "steps": [
                "Normalize and classify a bounded universe of up to 12 symbols.",
                "Fetch one current snapshot per symbol across crypto, FX, metals, or bid/ask routes.",
                "Evaluate data quality flags for freshness, spread, and missing values.",
                "Assign a simple MVP regime label from quality flags and coverage.",
                "Persist a receipt linking all source endpoints used in the snapshot.",
            ],
            "limitations": [
                "MVP market_regime is a quality/context heuristic, not a predictive macro model.",
                "No portfolio holdings, historical returns, or volatility model is applied.",
            ],
        },
        "market_regime": {
            "label": regime_label,
            "confidence": 0.68 if flags else 0.82,
            "drivers": flags or ["requested_assets_available", "no_quality_flags"],
        },
        "assets": assets,
        "brief": {
            "headline": f"Macro snapshot returned {len(successful)} of {len(assets)} requested assets.",
            "watch_items": flags,
        },
    }
    source_endpoints = [
        f"/v1/{asset['service']}/{asset['symbol']}"
        for asset in successful
        if asset.get("service") and asset.get("symbol")
    ]
    receipt = _response_receipt(
        request,
        product="multi_asset_macro_snapshot",
        subject=",".join(str(s).upper() for s in universe),
        request_payload=payload,
        response_payload=response_core,
        source_endpoints=source_endpoints,
    )
    return {
        "status": "ok",
        "product": "multi_asset_macro_snapshot",
        "credit_cost": CREDIT_COSTS["macro_snapshot"],
        "as_of": _utc_now_iso(),
        **response_core,
        "provenance": receipt,
        "meta": {"credits": _credit_meta_for_request(request)},
    }


@app.post("/v1/monitors/evaluate", responses=X402_RESPONSE)
async def spend_controlled_market_monitor(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """Evaluate a bounded market monitor immediately. Cost: 10 credits or $0.25 USDC."""
    brief = await agent_market_brief(request, payload)
    rules = payload.get("rules") or []
    matches: list[dict[str, Any]] = []
    for rule in rules if isinstance(rules, list) else []:
        metric = rule.get("metric")
        operator = rule.get("operator")
        value = float(rule.get("value") or 0)
        for item in brief.get("instruments", []):
            observed = item.get(metric)
            if not isinstance(observed, (int, float)):
                continue
            hit = (
                (operator == ">" and observed > value)
                or (operator == ">=" and observed >= value)
                or (operator == "<" and observed < value)
                or (operator == "<=" and observed <= value)
            )
            if hit:
                matches.append({"symbol": item.get("symbol"), "metric": metric, "observed": observed, "rule": rule})
    return {
        "status": "ok",
        "product": "spend_controlled_market_monitor",
        "credit_cost": CREDIT_COSTS["market_brief"],
        "methodology": {
            "type": "spend_controlled_market_monitor_v1",
            "steps": [
                "Reuse Agent Market Brief to fetch bounded current snapshots.",
                "Evaluate supplied numeric rules against returned instrument fields.",
                "Return matching triggers plus explicit credit budget metadata.",
                "Do not persist a scheduler or poll automatically in the MVP.",
            ],
            "limitations": [
                "This endpoint evaluates now only; recurring monitor scheduling is not enabled.",
            ],
        },
        "mode": "evaluate_now",
        "matches": matches,
        "brief": brief,
        "spend_control": {
            "max_credits": payload.get("max_credits"),
            "credits_spent": CREDIT_COSTS["market_brief"],
            "remaining_budget": (
                float(payload.get("max_credits")) - CREDIT_COSTS["market_brief"]
                if payload.get("max_credits") is not None
                else None
            ),
        },
        "meta": {"credits": _credit_meta_for_request(request)},
    }


@app.post("/v1/indicators/token-quality", responses=X402_RESPONSE)
async def token_market_quality_indicator(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """Score one token using live price, bid/ask, state, and VWAP-window feeds."""
    symbol = _normalise_symbol(str(payload.get("symbol") or ""), "symbol")
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")

    max_spread_bps = float(payload.get("max_spread_bps") or 50)
    max_state_divergence_bps = float(payload.get("max_state_divergence_bps") or 75)
    components = await _fetch_indicator_components(
        request.app.state.blocksize,
        symbol=symbol,
        stream_cache=getattr(request.app.state, "stream_cache", None),
        include_state_price=bool(payload.get("include_state_price", False)),
        include_windows=bool(payload.get("include_windows", False)),
        include_state_coverage=bool(payload.get("include_state_coverage", False)),
    )
    if not _has_current_market_component(components):
        raise HTTPException(
            status_code=502,
            detail={"error_code": "NO_INDICATOR_FEEDS", "components": components},
        )

    indicator = _build_token_quality_indicator(
        symbol=symbol,
        components=components,
        max_spread_bps=max_spread_bps,
        max_state_divergence_bps=max_state_divergence_bps,
    )
    response_core = {
        "methodology": {
            "type": "token_market_quality_indicator_v1",
            "steps": [
                "Normalize the token symbol and fetch current VWAP plus bid/ask from Blocksize raw-data feeds.",
                "Fetch available Blocksize state instrument coverage, including pool/network metadata, when enabled.",
                "Optionally resolve state/reference price through state_instruments plus state_pool, and fetch VWAP-window feeds if those upstream methods are enabled.",
                "Compute spread, freshness, state coverage, optional state divergence, optional VWAP-window drift, feed coverage, and transparent score penalties.",
                "Return a trader decision-support signal without executing or recommending a trade.",
                "Persist a provenance receipt with source endpoints and stable request/response hashes.",
            ],
            "score_rules": {
                "starts_at": 100,
                "penalties": [
                    "missing current market price",
                    "missing or wide bid/ask spread",
                    "missing or divergent state price when requested",
                    "missing VWAP windows when requested",
                    "missing state instrument coverage",
                    "stale returned components",
                ],
            },
            "limitations": [
                "Decision-support only; this is not investment advice or guaranteed alpha.",
                "State instrument and state_pool coverage is symbol-dependent; plain SOLUSD is not the same as protocol symbols such as MSOLUSD or JUPSOLUSD.",
                "Oracle confidence and VWAP-window latest methods are not available on the current deployed key surface.",
            ],
        },
        "indicator": indicator,
        "components": components,
    }
    receipt = _response_receipt(
        request,
        product="token_market_quality_indicator",
        subject=symbol,
        request_payload=payload,
        response_payload=response_core,
        source_endpoints=_component_source_endpoints(symbol, components),
    )
    return {
        "status": "ok",
        "product": "token_market_quality_indicator",
        "credit_cost": CREDIT_COSTS["token_quality_indicator"],
        "as_of": _utc_now_iso(),
        **response_core,
        "provenance": receipt,
        "meta": {"credits": _credit_meta_for_request(request)},
    }


@app.post("/v1/indicators/state-divergence", responses=X402_RESPONSE)
async def state_divergence_indicator(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """Compare Blocksize market prices with state/reference prices."""
    symbol = _normalise_symbol(str(payload.get("symbol") or ""), "symbol")
    if not symbol:
        raise HTTPException(status_code=400, detail="symbol is required")

    max_divergence_bps = float(payload.get("max_divergence_bps") or 75)
    components = await _fetch_indicator_components(
        request.app.state.blocksize,
        symbol=symbol,
        stream_cache=getattr(request.app.state, "stream_cache", None),
        include_state_price=True,
        include_windows=False,
        include_state_coverage=bool(payload.get("include_state_coverage", False)),
    )
    state = components.get("state", {})
    if state.get("status") != "ok":
        raise HTTPException(
            status_code=502,
            detail={
                "error_code": "STATE_PRICE_UNAVAILABLE",
                "message": "Blocksize state_pool did not return a usable value for this symbol. Check state_instruments coverage first.",
                "components": components,
            },
        )

    vwap = components.get("vwap", {})
    bidask = components.get("bidask", {})
    state_value = state.get("value")
    vwap_vs_state_bps = _bps_delta(
        vwap.get("value") if vwap.get("status") == "ok" else None,
        state_value,
    )
    mid_vs_state_bps = _bps_delta(
        bidask.get("value") if bidask.get("status") == "ok" else None,
        state_value,
    )
    max_observed = max(
        (
            abs(item)
            for item in (vwap_vs_state_bps, mid_vs_state_bps)
            if item is not None
        ),
        default=None,
    )
    state_label = (
        "normal"
        if max_observed is not None and max_observed <= max_divergence_bps
        else "alert"
        if max_observed is not None
        else "insufficient_market_price"
    )
    response_core = {
        "methodology": {
            "type": "state_divergence_indicator_v1",
            "steps": [
                "Fetch current VWAP and bid/ask market prices for the symbol.",
                "Resolve the symbol through state_instruments and fetch Blocksize state_pool snapshots for matching pools.",
                "Compute signed basis in basis points from VWAP and bid/ask mid versus state price.",
                "Classify the result as normal or alert against max_divergence_bps.",
                "Persist receipt provenance for audit and replay context.",
            ],
            "decision_rules": {
                "normal": "Absolute observed divergence is within max_divergence_bps.",
                "alert": "At least one observed market-vs-state basis exceeds max_divergence_bps.",
                "insufficient_market_price": "State price exists but no current market price was returned.",
            },
            "limitations": [
                "State divergence can reflect stale, paused, or unsupported feeds as well as real market dislocation.",
                "Decision-support only; no trade is executed.",
            ],
        },
        "symbol": symbol,
        "state": {
            "label": state_label,
            "max_divergence_bps": max_divergence_bps,
            "max_observed_abs_bps": max_observed,
        },
        "basis": {
            "vwap_vs_state_bps": vwap_vs_state_bps,
            "bidask_mid_vs_state_bps": mid_vs_state_bps,
        },
        "components": components,
    }
    receipt = _response_receipt(
        request,
        product="state_divergence_indicator",
        subject=symbol,
        request_payload=payload,
        response_payload=response_core,
        source_endpoints=_component_source_endpoints(symbol, components),
    )
    return {
        "status": "ok",
        "product": "state_divergence_indicator",
        "credit_cost": CREDIT_COSTS["state_divergence_indicator"],
        "as_of": _utc_now_iso(),
        **response_core,
        "provenance": receipt,
        "meta": {"credits": _credit_meta_for_request(request)},
    }


@app.post("/v1/signals/solana-token-brief", responses=X402_RESPONSE)
async def solana_token_brief(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """Build a Solana-oriented token signal brief for supported price symbols."""
    import asyncio

    symbols = payload.get("symbols") or payload.get("symbol") or ["SOLUSD"]
    if isinstance(symbols, str):
        symbols = [symbols]
    if not isinstance(symbols, list) or not symbols:
        raise HTTPException(status_code=400, detail="symbols must include at least one symbol")
    if len(symbols) > 10:
        raise HTTPException(status_code=400, detail="Solana token brief supports up to 10 symbols")

    client: BlocksizeClient = request.app.state.blocksize
    max_spread_bps = float(payload.get("max_spread_bps") or 50)
    max_state_divergence_bps = float(payload.get("max_state_divergence_bps") or 75)

    async def score_symbol(raw_symbol: Any) -> dict[str, Any]:
        symbol = _normalise_symbol(str(raw_symbol), "symbol")
        components = await _fetch_indicator_components(
            client,
            symbol=symbol,
            stream_cache=getattr(request.app.state, "stream_cache", None),
            include_state_price=bool(payload.get("include_state_price", False)),
            include_windows=bool(payload.get("include_windows", False)),
            include_state_coverage=bool(payload.get("include_state_coverage", False)),
        )
        if not _has_current_market_component(components):
            return {
                "symbol": symbol,
                "status": "unsupported_or_unavailable",
                "coverage": {"status": "none"},
                "components": components,
            }
        return {
            "symbol": symbol,
            "status": "ok",
            "indicator": _build_token_quality_indicator(
                symbol=symbol,
                components=components,
                max_spread_bps=max_spread_bps,
                max_state_divergence_bps=max_state_divergence_bps,
            ),
            "components": components,
        }

    token_results = await asyncio.gather(*(score_symbol(symbol) for symbol in symbols))
    successful = [item for item in token_results if item.get("status") == "ok"]
    if not successful:
        raise HTTPException(
            status_code=502,
            detail={"error_code": "NO_SOLANA_TOKEN_FEEDS", "tokens": token_results},
        )

    ranked = sorted(
        successful,
        key=lambda item: item["indicator"]["score"],
        reverse=True,
    )
    response_core = {
        "methodology": {
            "type": "solana_token_brief_v1",
            "steps": [
                "Treat requested symbols as a Solana-oriented token watchlist.",
                "Fetch only actual Blocksize feed data available for each symbol.",
                "Compute token market quality indicators from live VWAP, bid/ask, and available state instrument/pool coverage.",
                "Return unsupported protocol, DEX, oracle-price, or pool-price symbols as explicit coverage misses instead of synthetic data.",
                "Persist receipt provenance for the successful source endpoints.",
            ],
            "solana_scope": [
                "Supported now: token symbols available in Blocksize VWAP and bid/ask feeds, plus state_instruments pool/network metadata.",
                "Not synthesized in this MVP: DEX pool liquidity, oracle account confidence, state price, protocol TVL, perps funding, or VWAP windows unless Blocksize exposes those feeds.",
            ],
        },
        "network": "solana",
        "summary": {
            "headline": f"{len(successful)} of {len(token_results)} requested Solana-oriented symbols returned usable feed data.",
            "top_symbol": ranked[0]["symbol"],
            "top_score": ranked[0]["indicator"]["score"],
            "coverage_status": "partial" if len(successful) < len(token_results) else "full",
        },
        "tokens": token_results,
        "ranked_symbols": [
            {
                "symbol": item["symbol"],
                "score": item["indicator"]["score"],
                "signal": item["indicator"]["signal"],
            }
            for item in ranked
        ],
    }
    source_endpoints: list[str] = []
    for item in successful:
        source_endpoints.extend(
            _component_source_endpoints(item["symbol"], item.get("components", {}))
        )
    receipt = _response_receipt(
        request,
        product="solana_token_brief",
        subject=",".join(str(s).upper() for s in symbols),
        request_payload=payload,
        response_payload=response_core,
        source_endpoints=sorted(set(source_endpoints)),
    )
    return {
        "status": "ok",
        "product": "solana_token_brief",
        "credit_cost": CREDIT_COSTS["solana_token_brief"],
        "as_of": _utc_now_iso(),
        **response_core,
        "provenance": receipt,
        "meta": {"credits": _credit_meta_for_request(request)},
    }


@app.post("/v1/signals/trader-alpha-pack", responses=X402_RESPONSE)
async def trader_alpha_pack(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    """Bundle token quality indicators and macro context into a trader signal pack."""
    import asyncio

    symbols = payload.get("symbols") or payload.get("watchlist") or ["BTCUSD", "ETHUSD", "SOLUSD"]
    if isinstance(symbols, str):
        symbols = [item.strip() for item in symbols.split(",") if item.strip()]
    if not isinstance(symbols, list) or not symbols:
        raise HTTPException(status_code=400, detail="symbols must include at least one symbol")
    if len(symbols) > 12:
        raise HTTPException(status_code=400, detail="Trader alpha pack supports up to 12 symbols")

    client: BlocksizeClient = request.app.state.blocksize
    max_spread_bps = float(payload.get("max_spread_bps") or 50)
    max_state_divergence_bps = float(payload.get("max_state_divergence_bps") or 75)

    async def build_one(raw_symbol: Any) -> dict[str, Any]:
        symbol = _normalise_symbol(str(raw_symbol), "symbol")
        components = await _fetch_indicator_components(
            client,
            symbol=symbol,
            stream_cache=getattr(request.app.state, "stream_cache", None),
            include_state_price=bool(payload.get("include_state_price", False)),
            include_windows=bool(payload.get("include_windows", False)),
            include_state_coverage=bool(payload.get("include_state_coverage", False)),
        )
        if not _has_current_market_component(components):
            return {
                "symbol": symbol,
                "status": "unsupported_or_unavailable",
                "components": components,
            }
        return {
            "symbol": symbol,
            "status": "ok",
            "indicator": _build_token_quality_indicator(
                symbol=symbol,
                components=components,
                max_spread_bps=max_spread_bps,
                max_state_divergence_bps=max_state_divergence_bps,
            ),
            "components": components,
        }

    items = await asyncio.gather(*(build_one(symbol) for symbol in symbols))
    successful = [item for item in items if item.get("status") == "ok"]
    if not successful:
        raise HTTPException(
            status_code=502,
            detail={"error_code": "NO_TRADER_SIGNAL_FEEDS", "signals": items},
        )
    ranked = sorted(
        successful,
        key=lambda item: item["indicator"]["score"],
        reverse=True,
    )
    alerts = [
        {
            "symbol": item["symbol"],
            "flags": item["indicator"]["flags"],
            "signal": item["indicator"]["signal"],
        }
        for item in successful
        if item["indicator"]["flags"]
    ]
    response_core = {
        "methodology": {
            "type": "trader_alpha_pack_v1",
            "steps": [
                "Evaluate a bounded watchlist with the Token Market Quality methodology.",
                "Rank symbols by transparent quality score and identify caution/alert flags.",
                "Highlight spread quality, stale components, state instrument/pool coverage, and optional state/window metrics as trader decision-support data.",
                "Return coverage misses explicitly; no synthetic protocol or pool data is invented.",
                "Persist provenance for later receipt lookup.",
            ],
            "alpha_definition": (
                "In this MVP, alpha means faster, auditable signal packaging from live Blocksize data, "
                "not a promise of predictive returns."
            ),
            "limitations": [
                "Not investment advice.",
                "State instrument/pool coverage is available through state_instruments; DEX pool depth, state price, oracle confidence intervals, protocol TVL, and perps funding require additional Blocksize feed connectors before they can be scored directly.",
            ],
        },
        "summary": {
            "headline": f"{len(successful)} of {len(items)} watchlist symbols produced trader indicators.",
            "best_quality_symbol": ranked[0]["symbol"],
            "best_quality_score": ranked[0]["indicator"]["score"],
            "alerts_count": len(alerts),
        },
        "ranked_signals": [
            {
                "symbol": item["symbol"],
                "score": item["indicator"]["score"],
                "signal": item["indicator"]["signal"],
                "metrics": item["indicator"]["metrics"],
            }
            for item in ranked
        ],
        "alerts": alerts,
        "signals": items,
    }
    source_endpoints: list[str] = []
    for item in successful:
        source_endpoints.extend(
            _component_source_endpoints(item["symbol"], item.get("components", {}))
        )
    receipt = _response_receipt(
        request,
        product="trader_alpha_pack",
        subject=",".join(str(s).upper() for s in symbols),
        request_payload=payload,
        response_payload=response_core,
        source_endpoints=sorted(set(source_endpoints)),
    )
    return {
        "status": "ok",
        "product": "trader_alpha_pack",
        "credit_cost": CREDIT_COSTS["trader_alpha_pack"],
        "as_of": _utc_now_iso(),
        **response_core,
        "provenance": receipt,
        "meta": {"credits": _credit_meta_for_request(request)},
    }


@app.get("/v1/provenance/{receipt_id}")
async def lookup_price_receipt(request: Request, receipt_id: str) -> dict[str, Any]:
    """Lookup provenance for a prior paid or credited call. FREE when receipt exists."""
    if not re.fullmatch(r"rcpt_[A-Za-z0-9]{12,64}", receipt_id):
        raise HTTPException(status_code=400, detail="Invalid receipt id")
    receipt = request.app.state.credits.get_price_receipt(receipt_id)
    if receipt is None:
        raise HTTPException(status_code=404, detail="Receipt not found")
    receipt.setdefault(
        "methodology",
        {
            "type": "agent_data_provenance_lookup_v1",
            "steps": [
                "Validate receipt id format.",
                "Fetch stored receipt payload from the Blocksize receipt ledger.",
                "Return original request, response, receipt hashes, source endpoints, and payment context.",
            ],
        },
    )
    return receipt


@app.get("/v1/search")
async def search_pairs(
    q: str = Query(..., min_length=1, max_length=64, description="Search query"),
    asset_class: str = Query(
        "all",
        pattern="^(all|crypto|equity|equities|fx|metal)$",
        description="Asset class filter",
    ),
    request: Request = None,  # type: ignore[assignment]
) -> dict[str, Any]:
    """Search instruments. FREE."""
    try:
        client: BlocksizeClient = request.app.state.blocksize
        pairs = await client.search_pairs(q, asset_class)
        return PairSearchResponse(query=q, total_matches=len(pairs), pairs=pairs).model_dump()
    except BlocksizeAPIError as e:
        raise HTTPException(status_code=502, detail=ErrorResponse(
            error_code="BLOCKSIZE_ERROR", message=f"Search failed for '{q}'", details=str(e),
        ).model_dump())


@app.get("/v1/instruments/{service}")
async def list_instruments(service: str, request: Request) -> dict[str, Any]:
    """List instruments for a service. FREE."""
    try:
        client: BlocksizeClient = request.app.state.blocksize
        if service == "vwap":
            instruments = await client.list_vwap_instruments()
        elif service == "bidask":
            instruments = await client.list_bidask_instruments()
        elif service == "fx":
            instruments = await client.list_fx_instruments()
        elif service == "metal":
            instruments = await client.list_metal_instruments()
        else:
            raise HTTPException(status_code=400, detail=f"Unknown service: {service}")
        return InstrumentListResponse(
            service=service, total_instruments=len(instruments), instruments=instruments,
        ).model_dump()
    except BlocksizeAPIError as e:
        raise HTTPException(status_code=502, detail=ErrorResponse(
            error_code="BLOCKSIZE_ERROR", message=f"Failed to list for {service}", details=str(e),
        ).model_dump())


@app.get("/v1/cache/status")
async def get_cache_status(request: Request) -> dict[str, Any]:
    """Report stream-cache readiness for websocket-backed feeds. FREE."""
    stream_cache: BlocksizeStreamCache | None = getattr(request.app.state, "stream_cache", None)
    status = stream_cache.status() if stream_cache else {"enabled": False, "ready": False}
    return {
        "status": "ok",
        "cache": status,
        "feeds": {
            "vwap24h": {
                "source": "fixedvwap_subscribe",
                "http_product": "/v1/vwap24h/{pair}",
                "ready": bool(status.get("enabled") and status.get("cached_24h_vwap", 0) > 0),
            },
            "state": {
                "source": "state_subscribe plus state_instruments/state_pool fallback",
                "http_product": "/v1/state/{pair}",
                "ready": bool(
                    status.get("enabled")
                    and (
                        status.get("cached_state", 0) > 0
                        or status.get("state_mode") == "configured"
                    )
                ),
            },
            "vwap30m": {
                "source": "closingprice_list",
                "http_product": "/v1/vwap30m/{pair}",
                "ready": True,
            },
        },
        "links": {
            "capability_check": "/v1/capabilities/check",
            "docs": "/docs",
        },
    }


# ---------------------------------------------------------------------------
# Credit Management
# ---------------------------------------------------------------------------

@app.get("/v1/credits/balance/{wallet}")
async def get_credit_balance(request: Request, wallet: str):
    """View current drawdown credit balance for a specific wallet."""
    mgr: CreditManager = request.app.state.credits
    balance = mgr.get_balance(wallet)
    return {
        "wallet": wallet,
        "balance_credits": balance,
        "credit_unit": "Blocksize service credit",
        "starter_allowance": {
            "positioning": "Start with 50 live data credits",
            "allowance_credits": STARTER_CREDIT_ALLOWANCE,
            "not_free_forever": True,
        },
        "upgrade_path": "Use x402 payment or prepaid credit top-ups when credits are exhausted.",
    }

@app.post("/v1/credits/purchase")
async def purchase_credits_challenge(
    request: Request,
    tier: str = Query(..., pattern="^(starter|pro|institutional)$"),
):
    """
    Triggers an x402 challenge for bulk credits.
    Tiers: starter ($0.90), pro ($8.00), institutional ($60.00)
    """
    tier_data = BULK_TIERS.get(tier)
    price = Decimal(str(tier_data["price"]))
    
    # Return 402 challenge
    requirements = settings.payment_requirements(price)
    payment_required = _x402_payment_required(request, requirements)
    payload = _encode_payment_required(payment_required)
    _record_product_event(
        "bulk_credit_challenge",
        request,
        price_usdc=price,
        metadata={"tier": tier, "credits": tier_data["credits"]},
    )
    
    return JSONResponse(
        status_code=402,
        headers={"PAYMENT-REQUIRED": payload},
        content={
            **payment_required,
            "error": "Payment Required",
            "message": f"Purchase {tier_data['credits']} credits for ${price} USDC.",
            "tier": tier,
            "credits_to_add": tier_data["credits"],
            "legacy_requirements": requirements,
        }
    )

@app.post("/v1/credits/claim")
async def claim_credits(request: Request, payload: dict):
    """Verify a bulk payment and credit the agent's drawdown balance."""
    mgr: CreditManager = request.app.state.credits
    tx_hash = payload.get("proof")
    network = payload.get("network", "solana")
    tier = payload.get("tier")
    wallet = payload.get("wallet") # The address to credit
    
    if not all([tx_hash, tier, wallet]):
        _record_product_event("bulk_credit_claim_failed", request, reason="missing_fields")
        raise HTTPException(status_code=400, detail="Missing tx_hash, tier, or wallet")
    wallet = str(wallet).strip()
    if not WALLET_ID_RE.fullmatch(wallet):
        _record_product_event("bulk_credit_claim_failed", request, reason="invalid_wallet")
        raise HTTPException(status_code=400, detail="Invalid wallet")
        
    tier_data = BULK_TIERS.get(tier)
    if not tier_data:
        _record_product_event("bulk_credit_claim_failed", request, reason="invalid_tier")
        raise HTTPException(status_code=400, detail="Invalid tier")

    # Native RPC verification of the bulk payment
    payment_reqs = settings.payment_requirements(Decimal(str(tier_data["price"])))
    verification = await _verify_payment(base64.b64encode(json.dumps({
        "proof": tx_hash,
        "network": network
    }).encode()).decode(), payment_reqs, mgr, purpose=f"credits:{tier}")
    
    if not verification.get("valid"):
        _record_product_event(
            "bulk_credit_claim_failed",
            request,
            price_usdc=tier_data["price"],
            network=str(network),
            reason=str(verification.get("reason", "unknown")),
            metadata={"tier": tier},
        )
        raise HTTPException(status_code=402, detail=f"Bulk payment verification failed: {verification.get('reason')}")
    
    # Credit the wallet
    mgr.add_credits(
        address=wallet, 
        credits=tier_data["credits"], 
        tx_hash=tx_hash, 
        amount_usdc=tier_data["price"]
    )
    _record_product_event(
        "bulk_credit_claimed",
        request,
        price_usdc=tier_data["price"],
        network=str(network),
        wallet_hash=_wallet_hash(wallet),
        metadata={
            "tier": tier,
            "credits_added": tier_data["credits"],
            "proof_hash": fingerprint(str(tx_hash)),
        },
    )
    
    return {
        "status": "success",
        "added": tier_data["credits"],
        "new_balance": mgr.get_balance(wallet),
        "message": f"Successfully credited {tier_data['credits']} to {wallet}"
    }

# ---------------------------------------------------------------------------
# Discovery & MCP
# ---------------------------------------------------------------------------

@app.get("/mcp/manifest.json")
async def mcp_manifest():
    """
    Model Context Protocol (MCP) Manifest.
    Provides listing metadata for the public remote discovery server.
    """
    manifest: dict[str, object] = {
        "mcp_version": "1.0",
        "name": PUBLIC_DISPLAY_NAME,
        "description": PUBLIC_DESCRIPTION,
        "version": APP_VERSION,
        "transport": {
            "type": "streamable-http",
            "url": REMOTE_MCP_URL,
        },
        "capabilities": {
            "discovery_modes": ["instrument-search", "pricing-inspection", "document-search"],
            "paid_api_modes": ["real-time-x402", "credit-drawdown"],
            "bulk_discounts": "up to 40% via /v1/credits/purchase",
            "public_remote_server": "read-only and listing-safe",
            "ai_reader_brief": LLMS_TXT_URL,
            "sitemap": SITEMAP_URL,
            "data_package_catalog": DATA_PACKAGES_JSON_URL,
            "starter_allowance": "Start with 50 live data credits across raw data and premium workflow products.",
        },
        "links": {
            "homepage": PUBLIC_BASE_URL,
            "openapi": OPENAPI_URL,
            "swagger": SWAGGER_URL,
            "robots": ROBOTS_URL,
            "sitemap": SITEMAP_URL,
            "llms_txt": LLMS_TXT_URL,
            "data_packages_json": DATA_PACKAGES_JSON_URL,
            "quickstart": QUICKSTART_URL,
            "prompt_examples": PROMPT_EXAMPLES_URL,
            "privacy_policy": PRIVACY_POLICY_URL,
            "support": SUPPORT_URL,
            "agent_manual": AGENT_MANUAL_URL,
            "pricing_guide": PRICING_GUIDE_URL,
            "data_catalog": DATA_CATALOG_URL,
            "user_flow": USER_FLOW_URL,
            "server_json": SERVER_JSON_URL,
            "glama_claim": GLAMA_WELL_KNOWN_URL,
            "mcp_registry_auth": MCP_REGISTRY_AUTH_URL,
        },
        "tools": [
            {
                "name": "search_pairs",
                "description": (
                    "Search supported crypto, equity, FX, and metal symbols. "
                    "Returns catalog metadata only; free, read-only, and no live prices."
                ),
                "parameters": {
                    "query": {"type": "string", "example": "BTC"},
                    "asset_class": {"type": "string", "example": "crypto"},
                },
                "payment": {"required": False},
                "annotations": {"readOnlyHint": True, "idempotentHint": True},
            },
            {
                "name": "list_instruments",
                "description": (
                    "List supported instruments for one service such as vwap, bidask, fx, "
                    "or metal. Free read-only catalog metadata."
                ),
                "parameters": {"service": {"type": "string", "example": "metal"}},
                "payment": {"required": False},
                "annotations": {"readOnlyHint": True, "idempotentHint": True},
            },
            {
                "name": "get_pricing_info",
                "description": (
                    "Inspect current per-call pricing, bulk credit tiers, and supported "
                    "USDC settlement networks. Free and read-only; no payment is started."
                ),
                "parameters": {},
                "payment": {"required": False},
                "annotations": {"readOnlyHint": True, "idempotentHint": True},
            },
            {
                "name": "get_market_data_endpoint",
                "description": (
                    "Build the exact x402-protected HTTP URL for one live market-data "
                    "request. Free and read-only; does not fetch prices or charge a wallet."
                ),
                "parameters": {
                    "service": {"type": "string", "example": "bidask"},
                    "symbol": {"type": "string", "example": "AAPL"},
                },
                "payment": {"required": False},
                "annotations": {"readOnlyHint": True, "idempotentHint": True},
            },
            {
                "name": "get_product_catalog",
                "description": (
                    "Inspect raw data and premium agent-native workflow products, "
                    "including starter-credit positioning, credit costs, suggested "
                    "paid prices, endpoint templates, and upgrade path."
                ),
                "parameters": {},
                "payment": {"required": False},
                "annotations": {"readOnlyHint": True, "idempotentHint": True},
            },
            {
                "name": "get_workflow_endpoint",
                "description": (
                    "Build the exact paid HTTP endpoint, method, starter-credit "
                    "cost, and example body for a premium Blocksize workflow. "
                    "Free and read-only; does not fetch data or charge credits."
                ),
                "parameters": {
                    "product": {
                        "type": "string",
                        "example": "agent_market_brief",
                    },
                },
                "payment": {"required": False},
                "annotations": {"readOnlyHint": True, "idempotentHint": True},
            },
            {
                "name": "search",
                "description": (
                    "Search Blocksize docs and instrument metadata. Returns ids for fetch; "
                    "free, read-only, and no live prices."
                ),
                "parameters": {"query": {"type": "string", "example": "pricing"}},
                "payment": {"required": False},
                "annotations": {"readOnlyHint": True, "idempotentHint": True},
            },
            {
                "name": "fetch",
                "description": (
                    "Fetch one document or instrument guide by id. Free, read-only, and "
                    "no account, credential, payment, or live-price side effects."
                ),
                "parameters": {"id": {"type": "string", "example": "doc:quickstart"}},
                "payment": {"required": False},
                "annotations": {"readOnlyHint": True, "idempotentHint": True},
            },
        ],
        "paid_api": {
            "openapi_url": OPENAPI_URL,
            "swagger_url": SWAGGER_URL,
            "payment_model": "x402 or wallet credits",
            "starter_allowance": {
                "positioning": "Start with 50 live data credits",
                "allowance_credits": STARTER_CREDIT_ALLOWANCE,
                "applies_to": [
                    "raw_vwap",
                    "bid_ask",
                    "fx",
                    "metals",
                    "batch",
                    "market_briefs",
                    "pre_trade_checks",
                    "audit_receipts",
                    "macro_snapshots",
                    "token_quality_indicators",
                    "state_divergence_indicators",
                    "solana_token_briefs",
                    "trader_alpha_packs",
                    "provenance",
                ],
                "upgrade_path": "x402 payment or prepaid credit top-ups",
            },
        },
    }
    if REPOSITORY_URL:
        links = manifest["links"]
        if isinstance(links, dict):
            links["repository"] = REPOSITORY_URL
    return manifest


# ---------------------------------------------------------------------------


def _observability_authorized(request: Request) -> bool:
    expected = settings.server.observability_dashboard_token
    if not expected:
        return True
    header_token = request.headers.get("x-observability-token", "")
    cookie_token = request.cookies.get("observability_token", "")
    auth_header = request.headers.get("authorization", "")
    bearer_token = ""
    if auth_header.lower().startswith("bearer "):
        bearer_token = auth_header.split(" ", 1)[1].strip()
    query_token = request.query_params.get("token", "")
    return any(
        secrets.compare_digest(expected, token)
        for token in (header_token, bearer_token, query_token, cookie_token)
        if token
    )


def _observability_unauthorized() -> JSONResponse:
    return JSONResponse(
        status_code=401,
        headers={"WWW-Authenticate": "Bearer", "Cache-Control": "no-store"},
        content={
            "error": "Unauthorized",
            "message": "Set OBSERVABILITY_DASHBOARD_TOKEN and provide it as a bearer token.",
        },
    )


def _with_external_observability_context(summary: dict[str, Any]) -> dict[str, Any]:
    """Add marketplace context that is not stored in the local event database."""
    registry_sources = summary.get("registry_source_mix")
    if not isinstance(registry_sources, dict):
        registry_sources = {}
    overview = summary.get("overview")
    if not isinstance(overview, dict):
        overview = {}

    summary["external_sources"] = {
        "smithery": {
            "name": "Smithery",
            "listing_url": SMITHERY_LISTING_URL,
            "performance_url": SMITHERY_LISTING_URL,
            "hosted_mcp_endpoint": SMITHERY_HOSTED_MCP_ENDPOINT,
            "local_recorded_registry_calls": int(registry_sources.get("Smithery") or 0),
            "local_recorded_mcp_tool_calls": 0,
            "all_recorded_mcp_tool_calls": int(overview.get("mcp_tool_calls") or 0),
            "metrics_ingestion_configured": bool(SMITHERY_METRICS_API_URL),
            "metrics_api_url": SMITHERY_METRICS_API_URL or None,
            "status": (
                "configured"
                if SMITHERY_METRICS_API_URL
                else "not_ingested"
            ),
            "note": (
                "Smithery marketplace performance is only shown here when a metrics feed "
                "or hosted endpoint logs are wired into this observability database."
            ),
        }
    }
    return summary


@app.get("/internal/observability/stats", include_in_schema=False)
async def observability_stats(
    request: Request,
    days: int = Query(30, ge=1, le=180),
) -> JSONResponse:
    """Return dashboard-ready usage, registry, MCP, and monetization rollups."""
    if not _observability_authorized(request):
        return _observability_unauthorized()
    if OBSERVABILITY is None:
        return JSONResponse(
            status_code=503,
            headers={"Cache-Control": "no-store"},
            content={"error": "Observability disabled"},
        )
    return JSONResponse(
        headers={"Cache-Control": "no-store"},
        content=_with_external_observability_context(OBSERVABILITY.summarize(days=days)),
    )


@app.get("/internal/observability", include_in_schema=False, response_model=None)
async def observability_dashboard(request: Request) -> Any:
    """Serve a lightweight internal product observability dashboard."""
    return await observability_command_center(request)


def _observability_login_page(request: Request) -> HTMLResponse:
    target = request.url.path
    return HTMLResponse(
        status_code=401,
        headers={"Cache-Control": "no-store", "WWW-Authenticate": "Bearer"},
        content=f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Blocksize Observability Access</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #18201c;
      --muted: #68736d;
      --line: #d9ded9;
      --bg: #f5f7f2;
      --panel: #ffffff;
      --green: #1d6f45;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      min-height: 100vh;
      display: grid;
      place-items: center;
      background: var(--bg);
      color: var(--ink);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      padding: 24px;
    }}
    main {{
      width: min(440px, 100%);
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 28px;
    }}
    h1 {{ margin: 0 0 8px; font-size: 24px; letter-spacing: 0; }}
    p {{ margin: 0 0 20px; color: var(--muted); line-height: 1.5; }}
    label {{ display: grid; gap: 8px; color: var(--muted); font-size: 13px; }}
    input {{
      width: 100%;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 11px 12px;
      font: inherit;
      color: var(--ink);
      background: #fff;
    }}
    button {{
      width: 100%;
      margin-top: 14px;
      border: 1px solid var(--green);
      border-radius: 6px;
      padding: 11px 12px;
      color: #fff;
      background: var(--green);
      font-weight: 700;
      cursor: pointer;
    }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }}
  </style>
</head>
<body>
  <main>
    <h1>Internal Observability</h1>
    <p>This console is private. Enter the observability password configured for this environment.</p>
    <form method="get" action="{target}">
      <label>Password
        <input name="token" type="password" autocomplete="current-password" autofocus />
      </label>
      <button type="submit">Open Dashboard</button>
    </form>
  </main>
</body>
</html>""",
    )


def _observability_command_center_html(*, stats_path: str, token_required: bool) -> str:
    html = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Blocksize Observability Command Center</title>
  <style>
    :root {
      color-scheme: light;
      --ink: #18201c;
      --muted: #66716b;
      --faint: #8b968f;
      --line: #d7ddd8;
      --soft-line: #e7ebe7;
      --bg: #f5f7f2;
      --panel: #ffffff;
      --panel-soft: #fbfcf9;
      --green: #1d6f45;
      --green-soft: #e7f4eb;
      --blue: #2f5f9d;
      --blue-soft: #e9f0fb;
      --amber: #9b6418;
      --amber-soft: #f7eedf;
      --red: #a33d34;
      --red-soft: #f8e8e6;
      --glama: #7c3aed;
      --pay-sh: #0f9f8a;
      --mcp-registry: #d97706;
      --smithery: #2563eb;
      --awesome-mcp: #be123c;
      --github-source: #24292f;
      --gitlab-source: #e24329;
      --openapi-source: #0891b2;
      --x402-directory: #65a30d;
      --listing-asset: #7c2d12;
      --registry-other: #8b5e34;
      --shadow: 0 1px 1px rgba(24, 32, 28, .04);
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      color: var(--ink);
      background: var(--bg);
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    }
    .layout {
      display: grid;
      grid-template-columns: 248px minmax(0, 1fr);
      min-height: 100vh;
    }
    aside {
      position: sticky;
      top: 0;
      align-self: start;
      height: 100vh;
      padding: 22px 16px;
      background: #ffffff;
      border-right: 1px solid var(--line);
    }
    .brand {
      display: flex;
      align-items: center;
      gap: 10px;
      padding: 0 6px 20px;
      border-bottom: 1px solid var(--soft-line);
    }
    .mark {
      display: grid;
      place-items: center;
      width: 34px;
      height: 34px;
      border-radius: 6px;
      color: #fff;
      background: var(--green);
      font-weight: 800;
    }
    .brand strong { display: block; font-size: 14px; }
    .brand span { color: var(--muted); font-size: 12px; }
    nav { display: grid; gap: 6px; padding-top: 18px; }
    nav a {
      display: flex;
      align-items: center;
      gap: 9px;
      min-height: 36px;
      padding: 8px 10px;
      border-radius: 6px;
      color: var(--muted);
      text-decoration: none;
      font-size: 13px;
    }
    nav a:hover { background: var(--panel-soft); color: var(--ink); }
    nav a.active { background: var(--green-soft); color: var(--green); font-weight: 700; }
    main {
      min-width: 0;
      padding: 26px 30px 48px;
    }
    header {
      display: flex;
      justify-content: space-between;
      align-items: flex-start;
      gap: 18px;
      margin-bottom: 20px;
    }
    h1 { margin: 0; font-size: 28px; letter-spacing: 0; }
    h2 { margin: 0 0 12px; font-size: 16px; letter-spacing: 0; }
    h3 { margin: 0 0 8px; font-size: 13px; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }
    .sub { color: var(--muted); font-size: 13px; line-height: 1.45; }
    .toolbar { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; justify-content: flex-end; }
    button, select, input {
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #fff;
      color: var(--ink);
      font: inherit;
    }
    button { padding: 8px 11px; cursor: pointer; }
    button[aria-pressed="true"] { border-color: var(--green); color: var(--green); font-weight: 700; }
    select { padding: 8px 10px; }
    input { padding: 9px 10px; min-width: 220px; }
    .pill {
      display: inline-flex;
      align-items: center;
      gap: 7px;
      min-height: 28px;
      padding: 4px 10px;
      border-radius: 999px;
      background: var(--panel);
      border: 1px solid var(--line);
      color: var(--muted);
      font-size: 12px;
      white-space: nowrap;
    }
    .pill::before {
      content: "";
      width: 7px;
      height: 7px;
      border-radius: 50%;
      background: var(--green);
    }
    .unprotected::before { background: var(--amber); }
    .grid { display: grid; gap: 14px; }
    .hero {
      grid-template-columns: minmax(360px, 1.35fr) minmax(280px, .65fr);
      margin-bottom: 14px;
    }
    .kpis { grid-template-columns: repeat(4, minmax(150px, 1fr)); }
    .two { grid-template-columns: minmax(0, 1fr) minmax(320px, .78fr); }
    .three { grid-template-columns: repeat(3, minmax(220px, 1fr)); }
    .card {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      box-shadow: var(--shadow);
      min-width: 0;
    }
    .section { margin-top: 14px; }
    .metric {
      display: grid;
      gap: 8px;
      min-height: 116px;
    }
    .metric-label { color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }
    .metric-value { font-size: 28px; font-weight: 800; overflow-wrap: anywhere; }
    .metric-note { color: var(--muted); font-size: 12px; line-height: 1.35; }
    .headline {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 18px;
      align-items: start;
    }
    .headline-value { font-size: 42px; line-height: 1; font-weight: 850; margin: 10px 0 8px; }
    .status-list { display: grid; gap: 10px; }
    .status-item {
      display: grid;
      grid-template-columns: 24px minmax(0, 1fr);
      gap: 9px;
      align-items: start;
    }
    .status-dot {
      width: 11px;
      height: 11px;
      border-radius: 50%;
      margin-top: 4px;
      background: var(--green);
    }
    .status-dot.warn { background: var(--amber); }
    .status-dot.bad { background: var(--red); }
    .status-item strong { display: block; font-size: 13px; }
    .status-item span { display: block; margin-top: 2px; color: var(--muted); font-size: 12px; line-height: 1.35; }
    .bars { display: grid; gap: 9px; }
    .bar-row {
      display: grid;
      grid-template-columns: minmax(100px, 180px) minmax(90px, 1fr) 54px;
      gap: 10px;
      align-items: center;
      font-size: 13px;
    }
    .track { height: 10px; background: #eef1ee; border-radius: 999px; overflow: hidden; }
    .fill { height: 100%; background: var(--green); }
    .fill.blue { background: var(--blue); }
    .fill.amber { background: var(--amber); }
    .fill.red { background: var(--red); }
    .timeline {
      display: flex;
      align-items: end;
      gap: 4px;
      height: 168px;
      padding-top: 10px;
      border-bottom: 1px solid var(--line);
      overflow: visible;
    }
    .day { position: relative; flex: 1; min-width: 5px; display: flex; flex-direction: column; justify-content: end; gap: 2px; height: 100%; outline: none; }
    .day:hover::after, .day:focus-visible::after {
      content: attr(data-tip);
      position: absolute;
      left: 50%;
      bottom: calc(100% + 10px);
      z-index: 20;
      width: max-content;
      max-width: 230px;
      transform: translateX(-50%);
      padding: 9px 10px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: #18201c;
      color: #fff;
      box-shadow: 0 8px 22px rgba(24,32,28,.16);
      font-size: 12px;
      line-height: 1.35;
      text-align: left;
      white-space: pre-line;
      pointer-events: none;
    }
    .day:hover::before, .day:focus-visible::before {
      content: "";
      position: absolute;
      left: 50%;
      bottom: calc(100% + 4px);
      z-index: 21;
      width: 10px;
      height: 10px;
      transform: translateX(-50%) rotate(45deg);
      background: #18201c;
      border-right: 1px solid var(--line);
      border-bottom: 1px solid var(--line);
      pointer-events: none;
    }
    .seg { width: 100%; min-height: 1px; }
    .seg.http { background: var(--blue); }
    .seg.mcp { background: var(--green); }
    .seg.registry { background: var(--amber); }
    .seg.glama { background: var(--glama); }
    .seg.pay-sh { background: var(--pay-sh); }
    .seg.mcp-registry { background: var(--mcp-registry); }
    .seg.smithery { background: var(--smithery); }
    .seg.awesome-mcp { background: var(--awesome-mcp); }
    .seg.github-source { background: var(--github-source); }
    .seg.gitlab-source { background: var(--gitlab-source); }
    .seg.openapi-source { background: var(--openapi-source); }
    .seg.x402-directory { background: var(--x402-directory); }
    .seg.listing-asset { background: var(--listing-asset); }
    .seg.registry-other { background: var(--registry-other); }
    .legend { display: flex; gap: 14px; flex-wrap: wrap; color: var(--muted); font-size: 12px; margin-top: 10px; }
    .legend i { display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 5px; }
    .date-axis {
      display: flex;
      gap: 4px;
      min-height: 22px;
      padding-top: 6px;
      color: var(--muted);
      font-size: 11px;
      line-height: 1.1;
    }
    .date-tick {
      flex: 1;
      min-width: 5px;
      text-align: center;
      white-space: nowrap;
    }
    .filters { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 12px; }
    .source-card {
      display: grid;
      gap: 10px;
      font-size: 13px;
    }
    .source-card .big {
      font-size: 30px;
      font-weight: 800;
      line-height: 1;
    }
    .source-row {
      display: grid;
      grid-template-columns: minmax(92px, 132px) minmax(0, 1fr);
      gap: 10px;
      align-items: start;
    }
    .source-row span:first-child { color: var(--muted); }
    .source-row a { color: var(--blue); overflow-wrap: anywhere; }
    table { width: 100%; border-collapse: collapse; font-size: 13px; }
    th, td { padding: 9px 8px; border-bottom: 1px solid var(--soft-line); text-align: left; vertical-align: top; }
    th { color: var(--muted); font-weight: 700; white-space: nowrap; }
    tbody tr:hover { background: var(--panel-soft); }
    code { font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }
    .badge {
      display: inline-flex;
      align-items: center;
      min-height: 24px;
      padding: 3px 8px;
      border-radius: 999px;
      background: var(--green-soft);
      color: var(--green);
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }
    .badge.warn { background: var(--amber-soft); color: var(--amber); }
    .badge.bad { background: var(--red-soft); color: var(--red); }
    .badge.neutral { background: var(--blue-soft); color: var(--blue); }
    .empty { color: var(--muted); font-size: 13px; padding: 14px 0; }
    .scroll { overflow-x: auto; }
    @media (max-width: 1160px) {
      .layout { grid-template-columns: 1fr; }
      aside { position: relative; height: auto; border-right: 0; border-bottom: 1px solid var(--line); }
      nav { grid-template-columns: repeat(3, minmax(0, 1fr)); }
      .hero, .two, .three, .kpis { grid-template-columns: 1fr 1fr; }
    }
    @media (max-width: 760px) {
      main { padding: 18px; }
      header, .headline { grid-template-columns: 1fr; display: grid; }
      .hero, .two, .three, .kpis { grid-template-columns: 1fr; }
      nav { grid-template-columns: 1fr 1fr; }
      .bar-row { grid-template-columns: minmax(95px, 1fr) 1fr 44px; }
    }
  </style>
</head>
<body>
  <div class="layout">
    <aside>
      <div class="brand">
        <div class="mark">B</div>
        <div><strong>Observability</strong><span>Internal console</span></div>
      </div>
      <nav>
        <a class="active" href="#overview">Overview</a>
        <a href="#acquisition">Acquisition</a>
        <a href="#monetization">Monetization</a>
        <a href="#called-data">Called Data</a>
        <a href="#events">Event Trace</a>
      </nav>
    </aside>
    <main>
      <header id="overview">
        <div>
          <h1>Product Usage Command Center</h1>
          <div class="sub" id="freshness">Loading live telemetry...</div>
        </div>
        <div class="toolbar">
          <span id="security" class="pill">Protected</span>
          <button id="live" type="button" aria-pressed="true">Live 10s</button>
          <button id="refresh" type="button">Refresh</button>
          <label class="sub">Window
            <select id="window">
              <option value="1">24 hours</option>
              <option value="7">7 days</option>
              <option value="30" selected>30 days</option>
              <option value="90">90 days</option>
              <option value="180">180 days</option>
            </select>
          </label>
        </div>
      </header>

      <section class="grid hero">
        <div class="card headline">
          <div>
            <h3>Primary signal</h3>
            <div id="headline-value" class="headline-value">...</div>
            <div id="headline-note" class="sub">Waiting for telemetry.</div>
          </div>
          <div class="status-list" id="attention"></div>
        </div>
        <div class="grid kpis" id="kpis"></div>
      </section>

      <section class="grid two section">
        <div class="card">
          <h2>Usage Trend</h2>
          <div class="timeline" id="timeline"></div>
          <div class="date-axis" id="timeline-dates" aria-label="Usage trend dates"></div>
          <div class="legend">
            <span><i style="background: var(--blue)"></i>HTTP</span>
            <span><i style="background: var(--green)"></i>MCP tools</span>
            <span><i style="background: var(--glama)"></i>Glama</span>
            <span><i style="background: var(--pay-sh)"></i>Pay.sh</span>
            <span><i style="background: var(--mcp-registry)"></i>MCP Registry</span>
            <span><i style="background: var(--smithery)"></i>Smithery</span>
            <span><i style="background: var(--awesome-mcp)"></i>Awesome MCP</span>
            <span><i style="background: var(--github-source)"></i>GitHub</span>
            <span><i style="background: var(--gitlab-source)"></i>GitLab</span>
            <span><i style="background: var(--openapi-source)"></i>OpenAPI</span>
            <span><i style="background: var(--x402-directory)"></i>x402 Directory</span>
            <span><i style="background: var(--listing-asset)"></i>Listing assets</span>
            <span><i style="background: var(--registry-other)"></i>Other registry</span>
          </div>
        </div>
        <div class="card" id="monetization">
          <h2>Payment Funnel</h2>
          <div id="funnel" class="bars"></div>
        </div>
      </section>

      <section class="grid three section" id="acquisition">
        <div class="card"><h2>Registry Sources</h2><div id="registry-sources" class="bars"></div></div>
        <div class="card"><h2>Smithery Hosted Activity</h2><div id="smithery-source" class="source-card"></div></div>
        <div class="card"><h2>Most Used Services</h2><div id="services" class="bars"></div></div>
        <div class="card"><h2>Origins and Clients</h2><div id="origins" class="bars"></div></div>
      </section>

      <section class="grid three section">
        <div class="card"><h2>MCP Tool Mix</h2><div id="mcp" class="bars"></div></div>
        <div class="card"><h2>Registry Surfaces</h2><div id="registries" class="bars"></div></div>
        <div class="card"><h2>User Agents</h2><div id="agents" class="bars"></div></div>
      </section>

      <section class="card section" id="called-data">
        <div class="headline">
          <div>
            <h2>Called Data Detail</h2>
            <div class="sub">What was queried, when it was viewed, where it came from, and whether data was returned.</div>
          </div>
          <div class="filters">
            <input id="data-search" type="search" placeholder="Search service, symbol, outcome..." />
            <select id="outcome-filter">
              <option value="">All outcomes</option>
              <option value="Prompted">Prompted to pay</option>
              <option value="Data returned">Data returned</option>
              <option value="failed">Failed</option>
              <option value="rejected">Rejected</option>
            </select>
          </div>
        </div>
        <div class="scroll"><table id="data-called"></table></div>
      </section>

      <section class="card section" id="events">
        <div class="headline">
          <div>
            <h2>Recent Event Trace</h2>
            <div class="sub">Latest raw telemetry events for debugging acquisition, payment, and data access paths.</div>
          </div>
          <div class="filters">
            <input id="event-search" type="search" placeholder="Search event, endpoint, subject..." />
          </div>
        </div>
        <div class="scroll"><table id="recent"></table></div>
      </section>
    </main>
  </div>
  <script>
    const statsPath = __STATS_PATH__;
    const tokenRequired = __TOKEN_REQUIRED__;
    const fmt = new Intl.NumberFormat(undefined, { maximumFractionDigits: 3 });
    const money = new Intl.NumberFormat(undefined, { style: "currency", currency: "USD", maximumFractionDigits: 4 });
    const pct = value => value == null ? "n/a" : `${Math.round(value * 1000) / 10}%`;
    const text = value => value == null || value === "" ? "n/a" : String(value);
    let live = true;
    let refreshTimer = null;
    let currentData = null;

    const url = new URL(window.location.href);
    if (url.searchParams.get("token")) {
      history.replaceState(null, "", url.pathname + (url.searchParams.get("days") ? `?days=${url.searchParams.get("days")}` : "") + url.hash);
    }
    document.getElementById("security").textContent = tokenRequired ? "Password protected" : "Token not configured";
    if (!tokenRequired) document.getElementById("security").classList.add("unprotected");

    function metric(label, value, note = "") {
      return `<div class="card metric"><div class="metric-label">${label}</div><div class="metric-value">${value}</div><div class="metric-note">${note}</div></div>`;
    }

    function rowBadge(value) {
      const label = text(value);
      const lower = label.toLowerCase();
      let cls = "neutral";
      if (lower.includes("returned") || lower.includes("verified") || lower.includes("success")) cls = "";
      if (lower.includes("prompted") || lower.includes("required")) cls = "warn";
      if (lower.includes("failed") || lower.includes("error") || lower.includes("rejected")) cls = "bad";
      return `<span class="badge ${cls}">${label}</span>`;
    }

    function bars(target, data, color = "") {
      const entries = Object.entries(data || {}).slice(0, 8);
      const max = Math.max(1, ...entries.map(([, v]) => Number(v)));
      document.getElementById(target).innerHTML = entries.length ? entries.map(([k, v]) => `
        <div class="bar-row">
          <code title="${k}">${k}</code>
          <div class="track"><div class="fill ${color}" style="width:${Math.max(3, Number(v) / max * 100)}%"></div></div>
          <strong>${fmt.format(v)}</strong>
        </div>`).join("") : `<div class="empty">No activity in this window.</div>`;
    }

    const registrySourceWatchlist = [
      "Glama",
      "Pay.sh",
      "MCP Registry",
      "Smithery",
      "Awesome MCP",
      "GitHub",
      "GitLab",
      "OpenAPI crawlers",
      "x402 Directory",
      "Listing asset crawler",
    ];

    function registrySourceBars(data) {
      const known = new Set(registrySourceWatchlist);
      const watched = registrySourceWatchlist.map(label => [label, Number(data?.[label] || 0)]);
      const extra = Object.entries(data || {}).filter(([label]) => !known.has(label));
      const entries = [...watched, ...extra].slice(0, 14);
      const max = Math.max(1, ...entries.map(([, value]) => Number(value)));
      document.getElementById("registry-sources").innerHTML = entries.map(([label, value]) => `
        <div class="bar-row">
          <code title="${escapeAttr(label)}">${label}</code>
          <div class="track"><div class="fill amber" style="width:${Number(value) > 0 ? Math.max(3, Number(value) / max * 100) : 0}%"></div></div>
          <strong>${fmt.format(value)}</strong>
        </div>`).join("");
    }

    function renderSmitherySource(data) {
      const smithery = data.external_sources?.smithery || {};
      const localRegistry = Number(smithery.local_recorded_registry_calls || 0);
      const localMcp = Number(smithery.local_recorded_mcp_tool_calls || 0);
      const allMcp = Number(smithery.all_recorded_mcp_tool_calls || 0);
      const configured = Boolean(smithery.metrics_ingestion_configured);
      const status = configured ? "Metrics feed configured" : "Hosted metrics not ingested";
      const statusClass = configured ? "neutral" : "warn";
      document.getElementById("smithery-source").innerHTML = `
        <div>
          <div class="big">${fmt.format(localRegistry + localMcp)}</div>
          <div class="metric-note">Smithery-attributed calls recorded locally in this window</div>
        </div>
        <div>${rowBadge(status).replace('class="badge neutral"', `class="badge ${statusClass}"`)}</div>
        <div class="source-row"><span>Listing</span><a href="${escapeAttr(smithery.performance_url || "")}" target="_blank" rel="noreferrer">Smithery performance</a></div>
        <div class="source-row"><span>Hosted MCP</span><code title="${escapeAttr(smithery.hosted_mcp_endpoint || "")}">${text(smithery.hosted_mcp_endpoint)}</code></div>
        <div class="source-row"><span>All MCP tools</span><strong>${fmt.format(allMcp)}</strong></div>
        <div class="metric-note">${text(smithery.note)}</div>
      `;
    }

    function shortDate(isoDate) {
      const [year, month, day] = String(isoDate || "").split("-");
      if (!month || !day) return text(isoDate);
      return `${Number(month)}/${Number(day)}`;
    }

    function escapeAttr(value) {
      return String(value ?? "")
        .replace(/&/g, "&amp;")
        .replace(/"/g, "&quot;")
        .replace(/</g, "&lt;")
        .replace(/>/g, "&gt;");
    }

    function registryBreakdown(sources) {
      const ordered = [
        ["Glama", "glama", Number(sources?.["Glama"] || 0)],
        ["Pay.sh", "pay-sh", Number(sources?.["Pay.sh"] || 0)],
        ["MCP Registry", "mcp-registry", Number(sources?.["MCP Registry"] || 0)],
        ["Smithery", "smithery", Number(sources?.["Smithery"] || 0)],
        ["Awesome MCP", "awesome-mcp", Number(sources?.["Awesome MCP"] || 0)],
        ["GitHub", "github-source", Number(sources?.["GitHub"] || 0)],
        ["GitLab", "gitlab-source", Number(sources?.["GitLab"] || 0)],
        ["OpenAPI crawlers", "openapi-source", Number(sources?.["OpenAPI crawlers"] || 0)],
        ["x402 Directory", "x402-directory", Number(sources?.["x402 Directory"] || 0)],
        ["Listing asset crawler", "listing-asset", Number(sources?.["Listing asset crawler"] || 0)],
      ];
      const known = new Set(ordered.map(([label]) => label));
      const other = Object.entries(sources || {})
        .filter(([label]) => !known.has(label))
        .reduce((sum, [, value]) => sum + Number(value || 0), 0);
      return [...ordered, ["Other registry", "registry-other", other]];
    }

    function registrySourceSegments(sources, total, height) {
      return registryBreakdown(sources).filter(([, , value]) => value > 0).map(([label, cls, value]) => {
        const segmentHeight = Math.max(1, value / total * height);
        const labelText = `${label}: ${fmt.format(value)}`;
        return `<div class="seg ${cls}" style="height:${segmentHeight}%" title="${escapeAttr(labelText)}"></div>`;
      }).join("");
    }

    function timelineTip(d) {
      const total = Number(d.http_requests || 0) + Number(d.mcp_tool_calls || 0) + Number(d.registry_requests || 0);
      const registryRows = registryBreakdown(d.registry_sources || {})
        .map(([label, , value]) => `${label}: ${fmt.format(value)}`);
      return [
        text(d.date),
        `Total: ${fmt.format(total)}`,
        `HTTP: ${fmt.format(d.http_requests || 0)}`,
        `MCP tools: ${fmt.format(d.mcp_tool_calls || 0)}`,
        ...registryRows,
      ].join("\\n");
    }

    function timeline(rows) {
      const max = Math.max(1, ...rows.map(d => d.http_requests + d.mcp_tool_calls + d.registry_requests));
      const labelEvery = rows.length <= 10 ? 1 : rows.length <= 31 ? 7 : rows.length <= 90 ? 14 : 30;
      document.getElementById("timeline").innerHTML = rows.map(d => {
        const total = Math.max(1, d.http_requests + d.mcp_tool_calls + d.registry_requests);
        const h = Math.max(1, total / max * 100);
        const http = Math.max(1, d.http_requests / total * h);
        const mcp = d.mcp_tool_calls ? Math.max(1, d.mcp_tool_calls / total * h) : 0;
        const reg = registrySourceSegments(d.registry_sources || {}, total, h);
        const tip = timelineTip(d);
        return `<div class="day" tabindex="0" data-tip="${escapeAttr(tip)}" title="${escapeAttr(tip)}">
          ${reg}
          <div class="seg mcp" style="height:${mcp}%" title="MCP tools: ${fmt.format(d.mcp_tool_calls || 0)}"></div>
          <div class="seg http" style="height:${http}%" title="HTTP: ${fmt.format(d.http_requests || 0)}"></div>
        </div>`;
      }).join("");
      document.getElementById("timeline-dates").innerHTML = rows.map((d, index) => {
        const farEnoughFromEnd = rows.length - 1 - index >= Math.ceil(labelEvery / 2);
        const show = index === 0 || index === rows.length - 1 || (index % labelEvery === 0 && farEnoughFromEnd);
        return `<span class="date-tick" title="${text(d.date)}">${show ? shortDate(d.date) : ""}</span>`;
      }).join("");
    }

    function renderAttention(data) {
      const o = data.overview || {};
      const prompts = Number(data.event_counts?.payment_required || 0);
      const verified = Number(data.event_counts?.payment_verified || 0) + Number(data.event_counts?.credit_drawdown_success || 0);
      const abandoned = Math.max(0, prompts - verified);
      const errorRate = o.http_error_rate == null ? 0 : Number(o.http_error_rate);
      const registrySources = Object.keys(data.registry_source_mix || {}).length;
      const items = [
        {
          tone: abandoned > 0 ? "warn" : "",
          title: `${fmt.format(abandoned)} payment prompt${abandoned === 1 ? "" : "s"} without paid success`,
          note: abandoned > 0 ? "Follow up on pricing, wallet flow, or x402 client friction." : "No abandoned payment prompts in this window."
        },
        {
          tone: errorRate > 0 ? "bad" : "",
          title: `${pct(errorRate)} HTTP error rate`,
          note: errorRate > 0 ? "Review rejected, failed, or upstream events in the trace." : "No HTTP errors observed in this window."
        },
        {
          tone: registrySources ? "" : "warn",
          title: `${fmt.format(registrySources)} registry source${registrySources === 1 ? "" : "s"} observed`,
          note: registrySources ? "Directory attribution is flowing into the dashboard." : "No Glama, Pay.sh, or MCP registry traffic observed."
        }
      ];
      document.getElementById("attention").innerHTML = items.map(item => `
        <div class="status-item"><span class="status-dot ${item.tone}"></span><div><strong>${item.title}</strong><span>${item.note}</span></div></div>
      `).join("");
    }

    function recent(rows) {
      const q = document.getElementById("event-search").value.trim().toLowerCase();
      const filtered = (rows || []).filter(row => !q || JSON.stringify(row).toLowerCase().includes(q)).slice(0, 18);
      const table = document.getElementById("recent");
      table.innerHTML = `<thead><tr><th>Time</th><th>Event</th><th>Surface</th><th>Endpoint</th><th>Subject</th><th>Status</th><th>Price</th></tr></thead><tbody>` +
        (filtered.length ? filtered.map(row => `<tr>
          <td><code>${text(row.timestamp).slice(0, 19).replace("T", " ")}</code></td>
          <td>${rowBadge(row.event)}</td>
          <td>${text(row.surface)}</td>
          <td><code>${text(row.endpoint || row.tool_name)}</code></td>
          <td><code>${text(row.subject || row.reason)}</code></td>
          <td>${text(row.status_code)}</td>
          <td>${row.price_usdc == null ? "n/a" : money.format(row.price_usdc)}</td>
        </tr>`).join("") : `<tr><td colspan="7" class="empty">No matching events in this window.</td></tr>`) + `</tbody>`;
    }

    function dataCalled(rows) {
      const q = document.getElementById("data-search").value.trim().toLowerCase();
      const outcome = document.getElementById("outcome-filter").value.toLowerCase();
      const filtered = (rows || []).filter(row => {
        const haystack = `${row.service} ${row.subject} ${row.asset_class} ${row.surface} ${row.latest_outcome}`.toLowerCase();
        return (!q || haystack.includes(q)) && (!outcome || text(row.latest_outcome).toLowerCase().includes(outcome));
      }).slice(0, 40);
      const table = document.getElementById("data-called");
      table.innerHTML = `<thead><tr><th>Last Viewed</th><th>Service</th><th>Data</th><th>Asset Class</th><th>Origin</th><th>Outcome</th><th>Prompt Price</th><th>Paid</th><th>Revenue</th></tr></thead><tbody>` +
        (filtered.length ? filtered.map(row => `<tr>
          <td><code>${text(row.last_seen).slice(0, 19).replace("T", " ")}</code></td>
          <td><code>${text(row.service)}</code></td>
          <td><code>${text(row.subject)}</code></td>
          <td>${text(row.asset_class)}</td>
          <td>${text(row.surface)}</td>
          <td>${rowBadge(row.latest_outcome)}</td>
          <td>${row.prompt_price_usdc == null ? "n/a" : money.format(row.prompt_price_usdc)}</td>
          <td>${fmt.format(row.paid_successes || 0)}</td>
          <td>${money.format(row.revenue_usdc || 0)}</td>
        </tr>`).join("") : `<tr><td colspan="9" class="empty">No matching called data in this window.</td></tr>`) + `</tbody>`;
    }

    async function load() {
      const days = document.getElementById("window").value;
      const statsUrl = new URL(statsPath, window.location.origin);
      statsUrl.searchParams.set("days", days);
      const res = await fetch(statsUrl.toString(), { cache: "no-store", credentials: "same-origin" });
      const data = await res.json();
      if (!res.ok) throw new Error(data.message || data.error || "Unable to load stats");
      currentData = data;
      const o = data.overview || {};
      const prompts = Number(data.event_counts?.payment_required || 0);
      const paid = Number(o.paid_calls || 0);
      const conversion = prompts ? paid / prompts : null;
      document.getElementById("freshness").textContent = `Generated ${new Date(data.generated_at).toLocaleString()} over the last ${data.window_days} day${data.window_days === 1 ? "" : "s"}. ${live ? "Live refresh is on." : "Live refresh is paused."}`;
      document.getElementById("headline-value").textContent = text(o.most_used_service || "No usage yet");
      document.getElementById("headline-note").textContent = o.most_used_service ? "Most used service by called data volume in the selected window." : "No called data has been recorded in this window.";
      document.getElementById("kpis").innerHTML = [
        metric("Data Calls", fmt.format((data.data_called || []).reduce((sum, row) => sum + Number(row.calls || 0), 0)), "Called data subjects across API and MCP"),
        metric("Payment Prompts", fmt.format(prompts), "x402 challenges shown"),
        metric("Paid Calls", fmt.format(paid), "Verified x402, credits, and MCP paid usage"),
        metric("Revenue", money.format(o.estimated_revenue_usdc || 0), "Direct x402 + bulk credit claims"),
        metric("Registry Hits", fmt.format(o.registry_requests || 0), "Directory and metadata discovery"),
        metric("MCP Tools", fmt.format(o.mcp_tool_calls || 0), "Tool-level activity"),
        metric("Unique Clients", fmt.format(o.unique_client_fingerprints || 0), "Privacy-safe hashed fingerprints"),
        metric("Prompt Conversion", pct(conversion), "Paid calls / payment prompts"),
      ].join("");
      renderAttention(data);
      timeline(data.timeline || []);
      bars("funnel", {
        "payment prompts": prompts,
        "proof submissions": data.event_counts?.payment_proof_submitted || 0,
        "verified payments": data.event_counts?.payment_verified || 0,
        "credit drawdowns": data.event_counts?.credit_drawdown_success || 0,
        "bulk claims": data.event_counts?.bulk_credit_claimed || 0,
      }, "amber");
      registrySourceBars(data.registry_source_mix);
      renderSmitherySource(data);
      bars("services", data.service_mix);
      bars("origins", data.origin_mix, "blue");
      bars("mcp", data.mcp_tool_mix, "blue");
      bars("registries", data.registry_mix, "amber");
      bars("agents", data.user_agent_mix, "blue");
      dataCalled(data.data_called);
      recent(data.recent_events);
    }

    function rerenderTables() {
      if (!currentData) return;
      dataCalled(currentData.data_called);
      recent(currentData.recent_events);
    }

    function scheduleLive() {
      if (refreshTimer) clearInterval(refreshTimer);
      refreshTimer = live ? setInterval(load, 10000) : null;
      document.getElementById("live").setAttribute("aria-pressed", String(live));
      document.getElementById("live").textContent = live ? "Live 10s" : "Live off";
    }

    document.getElementById("window").addEventListener("change", load);
    document.getElementById("refresh").addEventListener("click", load);
    document.getElementById("data-search").addEventListener("input", rerenderTables);
    document.getElementById("outcome-filter").addEventListener("change", rerenderTables);
    document.getElementById("event-search").addEventListener("input", rerenderTables);
    document.getElementById("live").addEventListener("click", () => {
      live = !live;
      scheduleLive();
      load();
    });
    scheduleLive();
    load().catch(err => {
      document.getElementById("freshness").textContent = err.message;
    });
  </script>
</body>
</html>"""
    return html.replace("__STATS_PATH__", json.dumps(stats_path)).replace(
        "__TOKEN_REQUIRED__",
        "true" if token_required else "false",
    )


@app.get("/internal/observability/command-center", include_in_schema=False, response_model=None)
async def observability_command_center(request: Request) -> Any:
    """Serve the protected internal product usage command center."""
    if not _observability_authorized(request):
        return _observability_login_page(request)
    if OBSERVABILITY is None:
        return JSONResponse(
            status_code=503,
            headers={"Cache-Control": "no-store"},
            content={"error": "Observability disabled"},
        )

    token = request.query_params.get("token")
    response = HTMLResponse(
        headers={"Cache-Control": "no-store"},
        content=_observability_command_center_html(
            stats_path="/internal/observability/stats",
            token_required=bool(settings.server.observability_dashboard_token),
        ),
    )
    if token:
        response.set_cookie(
            "observability_token",
            token,
            max_age=60 * 60 * 12,
            httponly=True,
            samesite="strict",
        )
    return response


@app.get("/internal/observability/logout", include_in_schema=False, response_model=None)
async def observability_logout() -> RedirectResponse:
    """Clear local observability access and return to the login screen."""
    response = RedirectResponse("/internal/observability/command-center", status_code=303)
    response.delete_cookie("observability_token")
    return response


@app.get("/internal/observability/legacy", include_in_schema=False, response_model=None)
async def observability_legacy_dashboard(request: Request) -> Any:
    """Serve the original lightweight internal product observability dashboard."""
    if not _observability_authorized(request):
        return _observability_login_page(request)
    token = request.query_params.get("token")
    stats_path = "/internal/observability/stats"
    if token:
        stats_path = f"{stats_path}?token={token}"
    return HTMLResponse(
        headers={"Cache-Control": "no-store"},
        content=f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Blocksize Observability</title>
  <style>
    :root {{
      color-scheme: light;
      --ink: #17201c;
      --muted: #637068;
      --line: #d7ddd8;
      --bg: #f7f8f5;
      --panel: #ffffff;
      --green: #1f7a4d;
      --blue: #315e9f;
      --amber: #9a6418;
      --red: #a53b32;
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--ink);
      background: var(--bg);
    }}
    header {{
      padding: 28px 32px 18px;
      border-bottom: 1px solid var(--line);
      background: #ffffff;
    }}
    h1 {{ margin: 0; font-size: 26px; letter-spacing: 0; }}
    .sub {{ margin-top: 6px; color: var(--muted); font-size: 14px; }}
    main {{ padding: 24px 32px 40px; max-width: 1440px; margin: 0 auto; }}
    .toolbar {{ display: flex; justify-content: space-between; gap: 16px; align-items: center; margin-bottom: 18px; flex-wrap: wrap; }}
    .controls {{ display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }}
    button {{ border: 1px solid var(--line); border-radius: 6px; background: white; color: var(--ink); padding: 8px 10px; cursor: pointer; }}
    button[aria-pressed="true"] {{ border-color: var(--green); color: var(--green); font-weight: 700; }}
    select {{ border: 1px solid var(--line); border-radius: 6px; background: white; color: var(--ink); padding: 8px 10px; }}
    .grid {{ display: grid; gap: 16px; }}
    .kpis {{ grid-template-columns: repeat(6, minmax(150px, 1fr)); }}
    .two {{ grid-template-columns: minmax(0, 1.1fr) minmax(360px, .9fr); margin-top: 16px; }}
    .three {{ grid-template-columns: repeat(3, minmax(260px, 1fr)); margin-top: 16px; }}
    .wide {{ margin-top: 16px; }}
    .card {{
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      min-width: 0;
    }}
    .metric-label {{ color: var(--muted); font-size: 12px; text-transform: uppercase; letter-spacing: .04em; }}
    .metric-value {{ font-size: 25px; font-weight: 700; margin-top: 8px; overflow-wrap: anywhere; }}
    .metric-note {{ color: var(--muted); font-size: 12px; margin-top: 6px; min-height: 16px; }}
    h2 {{ font-size: 16px; margin: 0 0 12px; letter-spacing: 0; }}
    table {{ width: 100%; border-collapse: collapse; font-size: 13px; }}
    th, td {{ padding: 8px 6px; border-bottom: 1px solid var(--line); text-align: left; vertical-align: top; }}
    th {{ color: var(--muted); font-weight: 600; }}
    .bars {{ display: grid; gap: 8px; }}
    .bar-row {{ display: grid; grid-template-columns: minmax(90px, 160px) 1fr 64px; gap: 10px; align-items: center; font-size: 13px; }}
    .track {{ height: 10px; background: #e8ece8; border-radius: 999px; overflow: hidden; }}
    .fill {{ height: 100%; background: var(--green); }}
    .fill.blue {{ background: var(--blue); }}
    .fill.amber {{ background: var(--amber); }}
    .timeline {{ display: flex; align-items: end; gap: 5px; height: 170px; padding-top: 8px; border-bottom: 1px solid var(--line); }}
    .day {{ flex: 1; min-width: 5px; display: flex; flex-direction: column; justify-content: end; gap: 2px; height: 100%; }}
    .seg {{ width: 100%; min-height: 1px; }}
    .seg.http {{ background: var(--blue); }}
    .seg.mcp {{ background: var(--green); }}
    .seg.registry {{ background: var(--amber); }}
    .legend {{ display: flex; gap: 14px; flex-wrap: wrap; color: var(--muted); font-size: 12px; margin-top: 10px; }}
    .dot {{ display: inline-block; width: 9px; height: 9px; border-radius: 50%; margin-right: 5px; }}
    code {{ font-family: ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; font-size: 12px; }}
    .empty {{ color: var(--muted); font-size: 13px; }}
    @media (max-width: 1000px) {{
      main, header {{ padding-left: 18px; padding-right: 18px; }}
      .kpis, .two, .three {{ grid-template-columns: 1fr 1fr; }}
    }}
    @media (max-width: 680px) {{
      .kpis, .two, .three {{ grid-template-columns: 1fr; }}
      .bar-row {{ grid-template-columns: minmax(90px, 1fr) 1fr 48px; }}
    }}
  </style>
</head>
<body>
  <header>
    <h1>Blocksize Observability</h1>
    <div class="sub" id="freshness">Loading usage telemetry...</div>
  </header>
  <main>
    <div class="toolbar">
      <div class="sub">Registry, MCP, discovery, payment, credit, and data usage.</div>
      <div class="controls">
        <button id="live" type="button" aria-pressed="true">Live 10s</button>
        <button id="refresh" type="button">Refresh</button>
        <label class="sub">Window
          <select id="window">
            <option value="7">7 days</option>
            <option value="30" selected>30 days</option>
            <option value="90">90 days</option>
            <option value="180">180 days</option>
          </select>
        </label>
      </div>
    </div>
    <section class="grid kpis" id="kpis"></section>
    <section class="grid two">
      <div class="card">
        <h2>Usage Trend</h2>
        <div class="timeline" id="timeline"></div>
        <div class="legend">
          <span><i class="dot" style="background: var(--blue)"></i>HTTP</span>
          <span><i class="dot" style="background: var(--green)"></i>MCP tools</span>
          <span><i class="dot" style="background: var(--amber)"></i>Registries</span>
        </div>
      </div>
      <div class="card">
        <h2>Monetization Funnel</h2>
        <div id="funnel" class="bars"></div>
      </div>
    </section>
    <section class="grid three">
      <div class="card"><h2>Most Used Services</h2><div id="services" class="bars"></div></div>
      <div class="card"><h2>Call Origins</h2><div id="origins" class="bars"></div></div>
      <div class="card"><h2>Data Called</h2><div id="subjects" class="bars"></div></div>
    </section>
    <section class="grid two">
      <div class="card"><h2>Registry Sources</h2><div id="registry-sources" class="bars"></div></div>
      <div class="card"><h2>Registry Surfaces</h2><div id="registries" class="bars"></div></div>
    </section>
    <section class="grid two">
      <div class="card"><h2>MCP Tool Mix</h2><div id="mcp" class="bars"></div></div>
      <div class="card"><h2>Referrers and Clients</h2><div id="clients" class="bars"></div></div>
    </section>
    <section class="grid two">
      <div class="card"><h2>Paid Endpoint Mix</h2><div id="paid" class="bars"></div></div>
      <div class="card"><h2>User Agent Families</h2><div id="agents" class="bars"></div></div>
    </section>
    <section class="card wide">
      <h2>Called Data Detail</h2>
      <table id="data-called"></table>
    </section>
    <section class="grid two">
      <div class="card"><h2>Recent Events</h2><table id="recent"></table></div>
    </section>
  </main>
  <script>
    const statsPath = {json.dumps(stats_path)};
    const fmt = new Intl.NumberFormat(undefined, {{ maximumFractionDigits: 3 }});
    const money = new Intl.NumberFormat(undefined, {{ style: "currency", currency: "USD", maximumFractionDigits: 4 }});
    const pct = value => value == null ? "n/a" : `${{Math.round(value * 1000) / 10}}%`;
    const text = value => value == null || value === "" ? "n/a" : String(value);
    let live = true;
    let refreshTimer = null;

    function metric(label, value, note = "") {{
      return `<div class="card"><div class="metric-label">${{label}}</div><div class="metric-value">${{value}}</div><div class="metric-note">${{note}}</div></div>`;
    }}

    function bars(target, data, color = "") {{
      const entries = Object.entries(data || {{}}).slice(0, 10);
      const max = Math.max(1, ...entries.map(([, v]) => Number(v)));
      document.getElementById(target).innerHTML = entries.length ? entries.map(([k, v]) => `
        <div class="bar-row">
          <code title="${{k}}">${{k}}</code>
          <div class="track"><div class="fill ${{color}}" style="width:${{Math.max(2, Number(v) / max * 100)}}%"></div></div>
          <strong>${{fmt.format(v)}}</strong>
        </div>`).join("") : `<div class="empty">No activity in this window.</div>`;
    }}

    function timeline(rows) {{
      const max = Math.max(1, ...rows.map(d => d.http_requests + d.mcp_tool_calls + d.registry_requests));
      document.getElementById("timeline").innerHTML = rows.map(d => {{
        const h = Math.max(1, (d.http_requests + d.mcp_tool_calls + d.registry_requests) / max * 100);
        const http = Math.max(1, d.http_requests / Math.max(1, d.http_requests + d.mcp_tool_calls + d.registry_requests) * h);
        const mcp = d.mcp_tool_calls ? Math.max(1, d.mcp_tool_calls / Math.max(1, d.http_requests + d.mcp_tool_calls + d.registry_requests) * h) : 0;
        const reg = d.registry_requests ? Math.max(1, d.registry_requests / Math.max(1, d.http_requests + d.mcp_tool_calls + d.registry_requests) * h) : 0;
        return `<div class="day" title="${{d.date}}: ${{d.http_requests}} HTTP, ${{d.mcp_tool_calls}} MCP, ${{d.registry_requests}} registry">
          <div class="seg registry" style="height:${{reg}}%"></div>
          <div class="seg mcp" style="height:${{mcp}}%"></div>
          <div class="seg http" style="height:${{http}}%"></div>
        </div>`;
      }}).join("");
    }}

    function recent(rows) {{
      const table = document.getElementById("recent");
      table.innerHTML = `<thead><tr><th>Time</th><th>Event</th><th>Surface</th><th>Subject</th></tr></thead><tbody>` +
        (rows || []).slice(0, 12).map(row => `<tr>
          <td><code>${{text(row.timestamp).slice(0, 19).replace("T", " ")}}</code></td>
          <td>${{text(row.event)}}</td>
          <td>${{text(row.surface || row.endpoint)}}</td>
          <td><code>${{text(row.tool_name || row.subject || row.reason)}}</code></td>
        </tr>`).join("") + `</tbody>`;
    }}

    function dataCalled(rows) {{
      const table = document.getElementById("data-called");
      const items = (rows || []).slice(0, 20);
      table.innerHTML = `<thead><tr><th>Last Viewed</th><th>Service</th><th>Data</th><th>Asset Class</th><th>Origin</th><th>Outcome</th><th>Prompt Price</th><th>Paid Success</th><th>Revenue</th></tr></thead><tbody>` +
        (items.length ? items.map(row => `<tr>
          <td><code>${{text(row.last_seen).slice(0, 19).replace("T", " ")}}</code></td>
          <td><code>${{text(row.service)}}</code></td>
          <td><code>${{text(row.subject)}}</code></td>
          <td>${{text(row.asset_class)}}</td>
          <td>${{text(row.surface)}}</td>
          <td>${{text(row.latest_outcome)}}</td>
          <td>${{row.prompt_price_usdc == null ? "n/a" : money.format(row.prompt_price_usdc)}}</td>
          <td>${{fmt.format(row.paid_successes || 0)}}</td>
          <td>${{money.format(row.revenue_usdc || 0)}}</td>
        </tr>`).join("") : `<tr><td colspan="9" class="empty">No called data in this window.</td></tr>`) + `</tbody>`;
    }}

    async function load() {{
      const days = document.getElementById("window").value;
      const sep = statsPath.includes("?") ? "&" : "?";
      const res = await fetch(`${{statsPath}}${{sep}}days=${{days}}`, {{ cache: "no-store" }});
      const data = await res.json();
      if (!res.ok) throw new Error(data.message || data.error || "Unable to load stats");
      const o = data.overview;
      document.getElementById("freshness").textContent = `Generated ${{new Date(data.generated_at).toLocaleString()}} over the last ${{data.window_days}} days. ${{live ? "Live refresh is on." : "Live refresh is paused."}}`;
      document.getElementById("kpis").innerHTML = [
        metric("Paid Calls", fmt.format(o.paid_calls), "x402 + credit + MCP credit usage"),
        metric("Revenue", money.format(o.estimated_revenue_usdc), "direct x402 + bulk credit claims"),
        metric("MCP Tools", fmt.format(o.mcp_tool_calls), "tool-level activity"),
        metric("Registry Hits", fmt.format(o.registry_requests), "directory and metadata discovery"),
        metric("Top Service", text(o.most_used_service), "highest-volume called service"),
        metric("Unique Clients", fmt.format(o.unique_client_fingerprints), "hashed IP fingerprints"),
        metric("Payment Success", pct(o.payment_success_rate), "verified / submitted proofs"),
      ].join("");
      timeline(data.timeline || []);
      bars("funnel", {{
        "free discovery": o.free_discovery_calls,
        "402 challenges": data.event_counts.payment_required || 0,
        "proof submissions": data.event_counts.payment_proof_submitted || 0,
        "verified payments": data.event_counts.payment_verified || 0,
        "credit drawdowns": data.event_counts.credit_drawdown_success || 0,
        "bulk claims": data.event_counts.bulk_credit_claimed || 0,
      }}, "amber");
      bars("services", data.service_mix);
      bars("origins", data.origin_mix, "blue");
      bars("registry-sources", data.registry_source_mix, "amber");
      bars("registries", data.registry_mix, "amber");
      bars("mcp", data.mcp_tool_mix, "blue");
      bars("paid", data.paid_endpoint_mix);
      bars("subjects", data.top_subjects, "blue");
      bars("clients", data.referrer_mix && Object.keys(data.referrer_mix).length ? data.referrer_mix : data.client_fingerprint_mix, "amber");
      bars("agents", data.user_agent_mix, "blue");
      dataCalled(data.data_called);
      recent(data.recent_events);
    }}
    function scheduleLive() {{
      if (refreshTimer) clearInterval(refreshTimer);
      refreshTimer = live ? setInterval(load, 10000) : null;
      document.getElementById("live").setAttribute("aria-pressed", String(live));
      document.getElementById("live").textContent = live ? "Live 10s" : "Live off";
    }}
    document.getElementById("window").addEventListener("change", load);
    document.getElementById("refresh").addEventListener("click", load);
    document.getElementById("live").addEventListener("click", () => {{
      live = !live;
      scheduleLive();
      load();
    }});
    scheduleLive();
    load().catch(err => {{
      document.getElementById("freshness").textContent = err.message;
    }});
  </script>
</body>
</html>""",
    )


@app.get("/health")
async def health_check() -> dict[str, Any]:
    """Health check — free."""
    if _anthropic_only_mode():
        return {
            "status": "healthy",
            "service": "blocksize-anthropic-mcp-beta",
            "version": APP_VERSION,
            "mcp_url": _anthropic_mcp_url(),
            "transport": "streamable-http",
            "auth_provider": os.environ.get("ANTHROPIC_AUTH_PROVIDER", "none"),
            "oauth_callback_url": anthropic_auth.oauth_callback_url(),
            "oauth_protected_resource_metadata": (
                f"{PUBLIC_BASE_URL.rstrip()}/.well-known/"
                "oauth-protected-resource/anthropic/mcp/"
            ),
            "oauth_authorization_server_metadata": (
                f"{PUBLIC_BASE_URL.rstrip()}/.well-known/"
                "oauth-authorization-server/anthropic/mcp"
            ),
            "documentation": CLAUDE_CONNECTOR_URL,
            "privacy_policy": PRIVACY_POLICY_URL,
            "support": SUPPORT_URL,
            "beta_tokens_enabled": anthropic_auth.beta_tokens_enabled(),
            "daily_credits": int(os.environ.get("ANTHROPIC_DAILY_CREDITS", "50")),
            "starter_allowance": {
                "positioning": "Start with 50 live data credits",
                "allowance_credits": STARTER_CREDIT_ALLOWANCE,
            },
            "tool_surface": "read-only",
            "tool_costs": ANTHROPIC_TOOL_COSTS,
        }

    return {
        "status": "healthy",
        "service": "blocksize-mcp-x402",
        "version": APP_VERSION,
        "engine": "Shielded x402 Gateway (Iron Dome Active)",
        "networks": {
            "primary": {
                "name": "Solana",
                "configured": bool(settings.x402.solana_wallet_address),
            },
            "fallback": {
                "name": "Base",
                "configured": bool(settings.x402.evm_wallet_address),
            },
        },
        "pricing": settings.pricing_summary,
        "bulk_pricing": BULK_TIERS,
        "starter_allowance": {
            "positioning": "Start with 50 live data credits",
            "allowance_credits": STARTER_CREDIT_ALLOWANCE,
            "applies_to": "raw data, batches, market briefs, pre-trade checks, audit receipts, macro snapshots, and provenance lookups",
            "upgrade_path": "x402 payment or prepaid credit top-ups",
        },
        "links": {
            "remote_mcp": REMOTE_MCP_URL,
            "manifest": MCP_MANIFEST_URL,
            "robots": ROBOTS_URL,
            "sitemap": SITEMAP_URL,
            "llms_txt": LLMS_TXT_URL,
            "quickstart": QUICKSTART_URL,
            "prompt_examples": PROMPT_EXAMPLES_URL,
            "privacy_policy": PRIVACY_POLICY_URL,
            "support": SUPPORT_URL,
            "server_json": SERVER_JSON_URL,
            "glama_claim": GLAMA_WELL_KNOWN_URL,
            "mcp_registry_auth": MCP_REGISTRY_AUTH_URL,
            "anthropic_mcp": f"{PUBLIC_BASE_URL.rstrip('/')}/anthropic/mcp/",
            "anthropic_oauth_callback": anthropic_auth.oauth_callback_url(),
            "claude_connector": CLAUDE_CONNECTOR_URL,
            "cursor_mcp": f"{PUBLIC_BASE_URL.rstrip('/')}/cursor/mcp/",
            "cursor_oauth_callback": cursor_auth.oauth_callback_url(),
        },
        "anthropic_connector": {
            "mcp_url": _anthropic_mcp_url(),
            "auth_provider": os.environ.get("ANTHROPIC_AUTH_PROVIDER", "none"),
            "oauth_callback_url": anthropic_auth.oauth_callback_url(),
            "beta_tokens_enabled": anthropic_auth.beta_tokens_enabled(),
            "tool_surface": "read-only",
            "tool_costs": ANTHROPIC_TOOL_COSTS,
            "submission_docs": CLAUDE_CONNECTOR_URL,
        },
        "cursor_connector": {
            "mcp_url": _cursor_mcp_url(),
            "auth_provider": os.environ.get("CURSOR_AUTH_PROVIDER", "none"),
            "oauth_callback_url": cursor_auth.oauth_callback_url(),
            "beta_tokens_enabled": cursor_auth.beta_tokens_enabled(),
            "tool_surface": "read-only",
            "tool_costs": CURSOR_TOOL_COSTS,
        },
    }


def run_resource_server() -> None:
    """Start the resource server with uvicorn."""
    import uvicorn
    port = int(os.environ.get("PORT", settings.server.resource_server_port))
    uvicorn.run(
        "src.resource_server:app",
        host="0.0.0.0",
        port=port,
        log_level=settings.server.log_level.lower(),
        proxy_headers=True,
        forwarded_allow_ips=settings.server.forwarded_allow_ips,
        reload=False,
    )


if __name__ == "__main__":
    run_resource_server()
