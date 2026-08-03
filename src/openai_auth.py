"""Authentication helpers for the OpenAI and ChatGPT MCP server."""

from __future__ import annotations

from fastmcp.server.dependencies import get_access_token, get_http_headers

from src.connector_auth import (
    ConnectorIdentity as OpenAIIdentity,
    allowed_client_redirect_uris_for,
    beta_tokens_enabled_for,
    build_connector_auth_provider,
    oauth_callback_url_for,
    oauth_scopes_for,
    resolve_connector_identity,
)
from src.public_metadata import PUBLIC_BASE_URL

PREFIX = "OPENAI"
DEFAULT_PUBLIC_URL = f"{PUBLIC_BASE_URL.rstrip('/')}/openai/mcp"
# ChatGPT supplies a connector-specific callback URL during app setup. Keep
# production allowlisting explicit while permitting loopback clients for local QA.
DEFAULT_ALLOWED_CLIENT_REDIRECT_URIS = [
    "http://localhost:*",
    "http://127.0.0.1:*",
]
DEFAULT_OAUTH_SCOPES = ["openid", "email", "profile", "offline_access"]


def build_openai_auth_provider():
    """Build a FastMCP OAuth provider when OpenAI connector auth is configured."""
    return build_connector_auth_provider(
        prefix=PREFIX,
        default_public_url=DEFAULT_PUBLIC_URL,
        default_allowed_client_redirect_uris=DEFAULT_ALLOWED_CLIENT_REDIRECT_URIS,
        service_label="OpenAI MCP",
        default_oauth_scopes=DEFAULT_OAUTH_SCOPES,
    )


def resolve_openai_identity() -> OpenAIIdentity | None:
    """Resolve the current OpenAI MCP caller from OAuth or beta tokens."""
    return resolve_connector_identity(
        prefix=PREFIX,
        get_access_token_fn=get_access_token,
        get_http_headers_fn=get_http_headers,
    )


def beta_tokens_enabled() -> bool:
    """Return whether static beta bearer tokens should be accepted."""
    return beta_tokens_enabled_for(PREFIX)


def oauth_callback_url() -> str:
    """Return the OAuth callback URL registered with the upstream provider."""
    return oauth_callback_url_for(PREFIX, DEFAULT_PUBLIC_URL)


def oauth_scopes() -> list[str]:
    """Return OAuth scopes advertised and requested for ChatGPT/OpenAI."""
    return oauth_scopes_for(PREFIX, DEFAULT_OAUTH_SCOPES)


def _allowed_client_redirect_uris() -> list[str] | None:
    return allowed_client_redirect_uris_for(PREFIX, DEFAULT_ALLOWED_CLIENT_REDIRECT_URIS)
