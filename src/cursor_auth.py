"""Authentication helpers for the Cursor MCP server."""

from __future__ import annotations

from fastmcp.server.dependencies import get_access_token, get_http_headers

from src.connector_auth import (
    ConnectorIdentity as CursorIdentity,
    allowed_client_redirect_uris_for,
    beta_tokens_enabled_for,
    build_connector_auth_provider,
    oauth_callback_url_for,
    oauth_scopes_for,
    resolve_connector_identity,
)
from src.public_metadata import PUBLIC_BASE_URL

PREFIX = "CURSOR"
DEFAULT_PUBLIC_URL = f"{PUBLIC_BASE_URL.rstrip('/')}/cursor/mcp"
DEFAULT_ALLOWED_CLIENT_REDIRECT_URIS = [
    "http://localhost:*",
    "http://127.0.0.1:*",
    "cursor://*",
]
DEFAULT_OAUTH_SCOPES = ["email", "profile"]


def build_cursor_auth_provider():
    """Build a FastMCP OAuth provider for Cursor when env vars are configured."""
    return build_connector_auth_provider(
        prefix=PREFIX,
        default_public_url=DEFAULT_PUBLIC_URL,
        default_allowed_client_redirect_uris=DEFAULT_ALLOWED_CLIENT_REDIRECT_URIS,
        service_label="Cursor MCP",
        default_oauth_scopes=DEFAULT_OAUTH_SCOPES,
    )


def resolve_cursor_identity() -> CursorIdentity | None:
    """Resolve the current Cursor MCP caller from OAuth or beta tokens."""
    return resolve_connector_identity(
        prefix=PREFIX,
        get_access_token_fn=get_access_token,
        get_http_headers_fn=get_http_headers,
    )


def beta_tokens_enabled() -> bool:
    """Return whether static beta bearer tokens should be accepted."""
    return beta_tokens_enabled_for(PREFIX)


def oauth_callback_url() -> str:
    """Return the OAuth callback URL to register with the upstream provider."""
    return oauth_callback_url_for(PREFIX, DEFAULT_PUBLIC_URL)


def oauth_scopes() -> list[str]:
    """Return OAuth scopes advertised and requested for the Cursor connector."""
    return oauth_scopes_for(PREFIX, DEFAULT_OAUTH_SCOPES)


def _allowed_client_redirect_uris() -> list[str] | None:
    return allowed_client_redirect_uris_for(PREFIX, DEFAULT_ALLOWED_CLIENT_REDIRECT_URIS)
