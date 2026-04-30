"""Authentication helpers for the Anthropic-safe MCP server."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any

from fastmcp.server.auth import AccessToken
from fastmcp.server.dependencies import get_access_token, get_http_headers

from src.public_metadata import PUBLIC_BASE_URL

logger = logging.getLogger(__name__)

BETA_TOKEN_PROVIDERS = {"", "none", "dev", "beta-token"}
DEFAULT_ALLOWED_CLIENT_REDIRECT_URIS = [
    "https://claude.ai/api/mcp/auth_callback",
    "http://localhost:*",
    "http://127.0.0.1:*",
]


@dataclass(frozen=True)
class AnthropicIdentity:
    user_id: str
    email: str | None = None
    source: str = "oauth"


def build_anthropic_auth_provider():
    """Build a FastMCP OAuth provider when auth env vars are configured."""
    provider = os.environ.get("ANTHROPIC_AUTH_PROVIDER", "").strip().lower()
    base_url = os.environ.get(
        "ANTHROPIC_MCP_PUBLIC_URL",
        f"{PUBLIC_BASE_URL.rstrip('/')}/anthropic/mcp",
    ).rstrip("/")
    redirect_path = os.environ.get("ANTHROPIC_OAUTH_REDIRECT_PATH", "").strip() or None
    allowed_client_redirect_uris = _allowed_client_redirect_uris()
    jwt_signing_key = os.environ.get("ANTHROPIC_OAUTH_JWT_SIGNING_KEY", "").strip() or None

    if provider in BETA_TOKEN_PROVIDERS:
        return None

    if provider == "clerk":
        domain = os.environ.get("CLERK_DOMAIN", "").strip()
        client_id = os.environ.get("CLERK_CLIENT_ID", "").strip()
        client_secret = os.environ.get("CLERK_CLIENT_SECRET", "").strip() or None
        if not domain or not client_id or (not client_secret and not jwt_signing_key):
            logger.warning("ANTHROPIC_AUTH_PROVIDER=clerk but Clerk env vars are incomplete")
            return None
        from fastmcp.server.auth.providers.clerk import ClerkProvider

        return ClerkProvider(
            domain=domain,
            client_id=client_id,
            client_secret=client_secret,
            base_url=base_url,
            redirect_path=redirect_path,
            allowed_client_redirect_uris=allowed_client_redirect_uris,
            jwt_signing_key=jwt_signing_key,
            require_authorization_consent="external",
        )

    if provider == "auth0":
        config_url = os.environ.get("AUTH0_CONFIG_URL", "").strip()
        client_id = os.environ.get("AUTH0_CLIENT_ID", "").strip()
        client_secret = os.environ.get("AUTH0_CLIENT_SECRET", "").strip()
        audience = os.environ.get("AUTH0_AUDIENCE", "").strip()
        if not all([config_url, client_id, client_secret, audience]):
            logger.warning("ANTHROPIC_AUTH_PROVIDER=auth0 but Auth0 env vars are incomplete")
            return None
        from fastmcp.server.auth.providers.auth0 import Auth0Provider

        return Auth0Provider(
            config_url=config_url,
            client_id=client_id,
            client_secret=client_secret,
            audience=audience,
            base_url=base_url,
            required_scopes=["openid", "email", "profile"],
            redirect_path=redirect_path,
            allowed_client_redirect_uris=allowed_client_redirect_uris,
            jwt_signing_key=jwt_signing_key,
            require_authorization_consent="external",
        )

    if provider == "supabase":
        project_url = os.environ.get("SUPABASE_PROJECT_URL", "").strip()
        if not project_url:
            logger.warning("ANTHROPIC_AUTH_PROVIDER=supabase but SUPABASE_PROJECT_URL is missing")
            return None
        from fastmcp.server.auth.providers.supabase import SupabaseProvider

        return SupabaseProvider(project_url=project_url, base_url=base_url)

    logger.warning("Unknown ANTHROPIC_AUTH_PROVIDER=%s; Anthropic MCP auth disabled", provider)
    return None


def resolve_anthropic_identity() -> AnthropicIdentity | None:
    """Resolve the current MCP caller from OAuth or configured beta tokens."""
    token = _safe_access_token()
    if token is not None:
        identity = _identity_from_access_token(token)
        if identity is not None:
            return identity

    if beta_tokens_enabled():
        bearer = _current_bearer_token()
        if bearer:
            return _identity_from_beta_token(bearer)

    return None


def beta_tokens_enabled() -> bool:
    """Return whether static beta bearer tokens should be accepted."""
    provider = os.environ.get("ANTHROPIC_AUTH_PROVIDER", "").strip().lower()
    if provider in BETA_TOKEN_PROVIDERS:
        return True
    return _env_truthy("ANTHROPIC_ENABLE_BETA_TOKENS")


def oauth_callback_url() -> str:
    """Return the OAuth callback URL to register with the upstream provider."""
    base_url = os.environ.get(
        "ANTHROPIC_MCP_PUBLIC_URL",
        f"{PUBLIC_BASE_URL.rstrip('/')}/anthropic/mcp",
    ).rstrip("/")
    redirect_path = os.environ.get("ANTHROPIC_OAUTH_REDIRECT_PATH", "").strip()
    if not redirect_path:
        redirect_path = "/auth/callback"
    elif not redirect_path.startswith("/"):
        redirect_path = f"/{redirect_path}"
    return f"{base_url}{redirect_path}"


def _allowed_client_redirect_uris() -> list[str] | None:
    raw = os.environ.get("ANTHROPIC_ALLOWED_CLIENT_REDIRECT_URIS", "").strip()
    if raw in {"*", "all"}:
        return None
    if not raw:
        return DEFAULT_ALLOWED_CLIENT_REDIRECT_URIS
    return _parse_string_list(raw)


def _parse_string_list(raw: str) -> list[str]:
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("ANTHROPIC_ALLOWED_CLIENT_REDIRECT_URIS must be JSON or CSV")
            return DEFAULT_ALLOWED_CLIENT_REDIRECT_URIS
        if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
            return [item.strip() for item in parsed if item.strip()]
        logger.warning("ANTHROPIC_ALLOWED_CLIENT_REDIRECT_URIS JSON must be a string list")
        return DEFAULT_ALLOWED_CLIENT_REDIRECT_URIS
    return [part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()]


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _safe_access_token() -> AccessToken | None:
    try:
        return get_access_token()
    except Exception:
        return None


def _identity_from_access_token(token: AccessToken) -> AnthropicIdentity | None:
    claims = token.claims or {}
    upstream_claims = claims.get("upstream_claims")
    if isinstance(upstream_claims, dict):
        claims = {**claims, **upstream_claims}

    user_id = (
        claims.get("sub")
        or claims.get("user_id")
        or claims.get("oid")
        or claims.get("client_id")
        or token.client_id
    )
    if not user_id:
        return None

    email = claims.get("email") or claims.get("preferred_username")
    return AnthropicIdentity(
        user_id=str(user_id),
        email=str(email) if email else None,
        source="oauth",
    )


def _current_bearer_token() -> str | None:
    headers = get_http_headers(include_all=True)
    auth_header = headers.get("authorization") or headers.get("Authorization")
    if not auth_header:
        return None
    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def _identity_from_beta_token(token: str) -> AnthropicIdentity | None:
    token_map = _load_beta_tokens()
    raw_identity = token_map.get(token)
    if raw_identity is None:
        return None
    if isinstance(raw_identity, str):
        return AnthropicIdentity(user_id=raw_identity, source="beta-token")
    if isinstance(raw_identity, dict):
        user_id = raw_identity.get("user_id") or raw_identity.get("sub")
        if not user_id:
            return None
        email = raw_identity.get("email")
        return AnthropicIdentity(
            user_id=str(user_id),
            email=str(email) if email else None,
            source="beta-token",
        )
    return None


def _load_beta_tokens() -> dict[str, Any]:
    raw = os.environ.get("ANTHROPIC_BETA_TOKENS", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("ANTHROPIC_BETA_TOKENS must be a JSON object")
        return {}
    if not isinstance(parsed, dict):
        logger.warning("ANTHROPIC_BETA_TOKENS must be a JSON object")
        return {}
    return parsed
