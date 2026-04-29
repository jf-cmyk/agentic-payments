"""Shared public metadata for listing, docs, and remote MCP surfaces."""

from __future__ import annotations

import os
from urllib.parse import quote_plus

APP_VERSION = "0.6.1"


def _normalized_url(env_var: str, default: str) -> str:
    """Return a stable URL value without a trailing slash."""
    return os.getenv(env_var, default).rstrip("/")


PUBLIC_BASE_URL = _normalized_url(
    "PUBLIC_BASE_URL",
    "https://mcp.blocksize.info",
)
REPOSITORY_URL = os.getenv("PUBLIC_REPOSITORY_URL", "").strip()
REPOSITORY_SOURCE = os.getenv("PUBLIC_REPOSITORY_SOURCE", "git")

REMOTE_MCP_PATH = "/mcp/server"
REMOTE_MCP_URL = f"{PUBLIC_BASE_URL}{REMOTE_MCP_PATH}/"
MCP_MANIFEST_URL = f"{PUBLIC_BASE_URL}/mcp/manifest.json"
SERVER_JSON_URL = f"{PUBLIC_BASE_URL}/server.json"
OPENAPI_URL = f"{PUBLIC_BASE_URL}/openapi.json"
SWAGGER_URL = f"{PUBLIC_BASE_URL}/docs"

QUICKSTART_URL = f"{PUBLIC_BASE_URL}/quickstart/remote-mcp"
PROMPT_EXAMPLES_URL = f"{PUBLIC_BASE_URL}/prompt-examples"
PRIVACY_POLICY_URL = f"{PUBLIC_BASE_URL}/privacy"
SUPPORT_URL = f"{PUBLIC_BASE_URL}/support"
GLAMA_WELL_KNOWN_URL = f"{PUBLIC_BASE_URL}/.well-known/glama.json"
MCP_REGISTRY_AUTH_URL = f"{PUBLIC_BASE_URL}/.well-known/mcp-registry-auth"
MCP_REGISTRY_AUTH_CONTENT = (
    "v=MCPv1; k=ed25519; p=0E8nsn4Fk8E2qn6zUIP2VItzW7+etYnGuiBVMnjJaas="
)

AGENT_MANUAL_URL = f"{PUBLIC_BASE_URL}/pdf/Blocksize_Agent_Manual.pdf"
PRICING_GUIDE_URL = f"{PUBLIC_BASE_URL}/pdf/Blocksize_Pricing_Guide.pdf"
DATA_CATALOG_URL = f"{PUBLIC_BASE_URL}/pdf/Blocksize_Data_Catalog.pdf"
USER_FLOW_URL = f"{PUBLIC_BASE_URL}/pdf/Blocksize_User_Flow.pdf"

GLAMA_MAINTAINER_EMAIL = os.getenv(
    "PUBLIC_GLAMA_MAINTAINER_EMAIL",
    "jf@blocksize-capital.com",
).strip()
CONTACT_PHONE = "+49 (0)69 870 0990 80"

DISCOVERABLE_SYMBOL_COUNT = 6_368

INSTRUMENT_COUNTS = {
    "crypto_vwap_pairs": 6362,
    "shared_bidask_instruments": 2365,
    "fx_pairs": 3,
    "metals": 5,
}

OFFICIAL_REGISTRY_NAME = os.getenv(
    "PUBLIC_REGISTRY_NAME",
    "info.blocksize.mcp/agentic-payments",
)


def build_server_json() -> dict[str, object]:
    """Build the official MCP Registry metadata payload."""
    payload: dict[str, object] = {
        "$schema": "https://static.modelcontextprotocol.io/schemas/2025-12-11/server.schema.json",
        "name": OFFICIAL_REGISTRY_NAME,
        "description": (
            "Public MCP discovery for Blocksize market data, pricing, and docs."
        ),
        "homepage": f"{PUBLIC_BASE_URL}/",
        "version": APP_VERSION,
        "remotes": [
            {
                "url": REMOTE_MCP_URL,
                "type": "streamable-http",
            }
        ],
    }
    if REPOSITORY_URL:
        payload["repository"] = {
            "url": REPOSITORY_URL,
            "source": REPOSITORY_SOURCE,
        }
    return payload


STATIC_DOCUMENTS = {
    "portal": {
        "title": "Blocksize Developer Portal",
        "url": PUBLIC_BASE_URL,
        "keywords": ["portal", "overview", "landing", "blocksize", "agentic payments"],
        "text": (
            "Overview of Blocksize Capital's agentic market data platform, pricing, "
            "HTTP endpoints, remote MCP discovery endpoint, and documentation links."
        ),
    },
    "quickstart": {
        "title": "Remote MCP Quickstart",
        "url": QUICKSTART_URL,
        "keywords": ["quickstart", "install", "chatgpt", "cursor", "claude", "codex", "remote mcp"],
        "text": (
            "Installation guide for the public Blocksize remote MCP discovery server "
            "using the streamable HTTP transport."
        ),
    },
    "pricing": {
        "title": "Pricing Guide",
        "url": PRICING_GUIDE_URL,
        "keywords": ["pricing", "credits", "cost", "usdc", "x402"],
        "text": (
            "Per-call pricing for crypto, FX, metals, and bulk credit tiers for "
            "Blocksize Capital's paid HTTP market data API."
        ),
    },
    "manual": {
        "title": "Agent Integration Guide",
        "url": AGENT_MANUAL_URL,
        "keywords": ["manual", "integration", "agent", "x402", "payments"],
        "text": (
            "Detailed explanation of the x402 payment flow, agent wallet credits, "
            "integration patterns, and security constraints."
        ),
    },
    "api": {
        "title": "OpenAPI Reference",
        "url": SWAGGER_URL,
        "keywords": ["swagger", "openapi", "api", "reference", "http"],
        "text": (
            "Interactive OpenAPI documentation for the paid HTTP API endpoints and "
            "free discovery endpoints."
        ),
    },
    "prompts": {
        "title": "Prompt Examples",
        "url": PROMPT_EXAMPLES_URL,
        "keywords": ["examples", "prompts", "use cases", "claude", "chatgpt", "cursor"],
        "text": (
            "Working prompt examples that demonstrate how to use the public discovery "
            "tools and how to route from discovery into paid HTTP data access."
        ),
    },
    "privacy": {
        "title": "Privacy Policy",
        "url": PRIVACY_POLICY_URL,
        "keywords": ["privacy", "policy", "data retention", "logging"],
        "text": (
            "Privacy policy describing request metadata, wallet-related headers, "
            "payment proof handling, and operational logging."
        ),
    },
    "support": {
        "title": "Support and Contact",
        "url": SUPPORT_URL,
        "keywords": ["support", "contact", "help", "troubleshooting"],
        "text": (
            "Support channels, troubleshooting guidance, issue reporting details, "
            "and product contact information."
        ),
    },
}


def search_static_documents(query: str) -> list[dict[str, object]]:
    """Return static documentation search results for the OpenAI-style search tool."""
    needle = query.strip().lower()
    results: list[dict[str, object]] = []

    for slug, doc in STATIC_DOCUMENTS.items():
        haystack = " ".join(
            [doc["title"], doc["text"], *doc["keywords"]]
        ).lower()
        if needle and needle not in haystack:
            continue
        results.append(
            {
                "id": f"doc:{slug}",
                "title": doc["title"],
                "url": doc["url"],
                "metadata": {
                    "type": "documentation",
                    "keywords": doc["keywords"],
                },
            }
        )

    if results or needle:
        return results

    for slug, doc in STATIC_DOCUMENTS.items():
        results.append(
            {
                "id": f"doc:{slug}",
                "title": doc["title"],
                "url": doc["url"],
                "metadata": {"type": "documentation"},
            }
        )
    return results


def get_static_document(document_id: str) -> dict[str, object] | None:
    """Return the full document payload for a known static document id."""
    slug = document_id.removeprefix("doc:")
    doc = STATIC_DOCUMENTS.get(slug)
    if not doc:
        return None

    return {
        "id": f"doc:{slug}",
        "title": doc["title"],
        "text": doc["text"],
        "url": doc["url"],
        "metadata": {
            "type": "documentation",
            "keywords": doc["keywords"],
        },
    }


def search_api_url(query: str) -> str:
    """Build a canonical public URL for search results."""
    return f"{PUBLIC_BASE_URL}/v1/search?q={quote_plus(query)}"
