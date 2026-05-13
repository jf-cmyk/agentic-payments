"""Authentication helpers for the Anthropic-safe MCP server."""

from __future__ import annotations

from fastmcp.server.dependencies import get_access_token, get_http_headers

from src.connector_auth import (
    ConnectorIdentity as AnthropicIdentity,
    allowed_client_redirect_uris_for,
    beta_tokens_enabled_for,
    build_connector_auth_provider,
    current_bearer_token,
    identity_from_access_token,
    identity_from_beta_token,
    load_beta_tokens,
    oauth_callback_url_for,
    oauth_scopes_for,
    parse_string_list,
    resolve_connector_identity,
)
from src.public_metadata import PUBLIC_BASE_URL

PREFIX = "ANTHROPIC"
DEFAULT_PUBLIC_URL = f"{PUBLIC_BASE_URL.rstrip('/')}/anthropic/mcp"
DEFAULT_ALLOWED_CLIENT_REDIRECT_URIS = [
    "https://claude.ai/api/mcp/auth_callback",
    "http://localhost:*",
    "http://127.0.0.1:*",
]
DEFAULT_OAUTH_SCOPES = ["openid", "email", "profile"]


def build_anthropic_auth_provider():
    """Build a FastMCP OAuth provider when auth env vars are configured."""
    return build_connector_auth_provider(
        prefix=PREFIX,
        default_public_url=DEFAULT_PUBLIC_URL,
        default_allowed_client_redirect_uris=DEFAULT_ALLOWED_CLIENT_REDIRECT_URIS,
        service_label="Anthropic MCP",
        default_oauth_scopes=DEFAULT_OAUTH_SCOPES,
    )


def resolve_anthropic_identity() -> AnthropicIdentity | None:
    """Resolve the current MCP caller from OAuth or configured beta tokens."""
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
    """Return OAuth scopes advertised and requested for the Claude connector."""
    return oauth_scopes_for(PREFIX, DEFAULT_OAUTH_SCOPES)


def _allowed_client_redirect_uris() -> list[str] | None:
    return allowed_client_redirect_uris_for(PREFIX, DEFAULT_ALLOWED_CLIENT_REDIRECT_URIS)


def _parse_string_list(raw: str) -> list[str] | None:
    return parse_string_list(raw, DEFAULT_ALLOWED_CLIENT_REDIRECT_URIS)


def _identity_from_access_token(token) -> AnthropicIdentity | None:
    return identity_from_access_token(token)


def _current_bearer_token() -> str | None:
    return current_bearer_token(get_http_headers)


def _identity_from_beta_token(token: str) -> AnthropicIdentity | None:
    return identity_from_beta_token(PREFIX, token)


def _load_beta_tokens():
    return load_beta_tokens(PREFIX)
