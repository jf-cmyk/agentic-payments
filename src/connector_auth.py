"""Connector-neutral OAuth and beta-token helpers for authenticated MCP surfaces."""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from typing import Any, Callable

from fastmcp.server.auth import AccessToken

logger = logging.getLogger(__name__)

BETA_TOKEN_PROVIDERS = {"", "none", "dev", "beta-token"}


@dataclass(frozen=True)
class ConnectorIdentity:
    user_id: str
    email: str | None = None
    source: str = "oauth"


AccessTokenGetter = Callable[[], AccessToken]
HeaderGetter = Callable[..., dict[str, str]]


def build_connector_auth_provider(
    *,
    prefix: str,
    default_public_url: str,
    default_allowed_client_redirect_uris: list[str] | None,
    service_label: str,
    default_oauth_scopes: list[str] | None = None,
):
    """Build a FastMCP OAuth provider for a named connector surface."""
    provider = _env(prefix, "AUTH_PROVIDER").strip().lower()
    base_url = _env(prefix, "MCP_PUBLIC_URL", default_public_url).rstrip("/")
    redirect_path = _env(prefix, "OAUTH_REDIRECT_PATH").strip() or None
    allowed_client_redirect_uris = allowed_client_redirect_uris_for(
        prefix,
        default_allowed_client_redirect_uris,
    )
    jwt_signing_key = _env(prefix, "OAUTH_JWT_SIGNING_KEY").strip() or None
    oauth_scopes = oauth_scopes_for(
        prefix,
        default_oauth_scopes or ["openid", "email", "profile"],
    )

    if provider in BETA_TOKEN_PROVIDERS:
        return None

    if provider == "clerk":
        domain = os.environ.get("CLERK_DOMAIN", "").strip()
        client_id = os.environ.get("CLERK_CLIENT_ID", "").strip()
        client_secret = os.environ.get("CLERK_CLIENT_SECRET", "").strip() or None
        if not domain or not client_id or (not client_secret and not jwt_signing_key):
            logger.warning("%s_AUTH_PROVIDER=clerk but Clerk env vars are incomplete", prefix)
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
            required_scopes=oauth_scopes,
            valid_scopes=oauth_scopes,
            forward_resource=False,
        )

    if provider == "auth0":
        config_url = _env(prefix, "AUTH0_CONFIG_URL", os.environ.get("AUTH0_CONFIG_URL", ""))
        client_id = _env(prefix, "AUTH0_CLIENT_ID", os.environ.get("AUTH0_CLIENT_ID", ""))
        client_secret = _env(
            prefix,
            "AUTH0_CLIENT_SECRET",
            os.environ.get("AUTH0_CLIENT_SECRET", ""),
        )
        audience = _env(prefix, "AUTH0_AUDIENCE", os.environ.get("AUTH0_AUDIENCE", ""))
        if not all([config_url, client_id, client_secret, audience]):
            logger.warning("%s_AUTH_PROVIDER=auth0 but Auth0 env vars are incomplete", prefix)
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
        project_url = _env(
            prefix,
            "SUPABASE_PROJECT_URL",
            os.environ.get("SUPABASE_PROJECT_URL", ""),
        ).strip()
        if not project_url:
            logger.warning("%s_AUTH_PROVIDER=supabase but Supabase env vars are incomplete", prefix)
            return None
        from fastmcp.server.auth.providers.supabase import SupabaseProvider

        return SupabaseProvider(project_url=project_url, base_url=base_url)

    logger.warning("Unknown %s_AUTH_PROVIDER=%s; %s auth disabled", prefix, provider, service_label)
    return None


def resolve_connector_identity(
    *,
    prefix: str,
    get_access_token_fn: AccessTokenGetter,
    get_http_headers_fn: HeaderGetter,
) -> ConnectorIdentity | None:
    """Resolve the current MCP caller from OAuth or configured beta tokens."""
    token = _safe_access_token(get_access_token_fn)
    if token is not None:
        identity = identity_from_access_token(token)
        if identity is not None:
            return identity

    if beta_tokens_enabled_for(prefix):
        bearer = current_bearer_token(get_http_headers_fn)
        if bearer:
            return identity_from_beta_token(prefix, bearer)

    return None


def beta_tokens_enabled_for(prefix: str) -> bool:
    """Return whether static beta bearer tokens should be accepted."""
    provider = _env(prefix, "AUTH_PROVIDER").strip().lower()
    if provider in BETA_TOKEN_PROVIDERS:
        return True
    return _env_truthy(f"{prefix}_ENABLE_BETA_TOKENS")


def oauth_callback_url_for(prefix: str, default_public_url: str) -> str:
    """Return the OAuth callback URL to register with the upstream provider."""
    base_url = _env(prefix, "MCP_PUBLIC_URL", default_public_url).rstrip("/")
    redirect_path = _env(prefix, "OAUTH_REDIRECT_PATH").strip()
    if not redirect_path:
        redirect_path = "/auth/callback"
    elif not redirect_path.startswith("/"):
        redirect_path = f"/{redirect_path}"
    return f"{base_url}{redirect_path}"


def allowed_client_redirect_uris_for(
    prefix: str,
    default_allowed_client_redirect_uris: list[str] | None,
) -> list[str] | None:
    raw = _env(prefix, "ALLOWED_CLIENT_REDIRECT_URIS").strip()
    if raw in {"*", "all"}:
        return None
    if not raw:
        return default_allowed_client_redirect_uris
    return parse_string_list(raw, default_allowed_client_redirect_uris)


def oauth_scopes_for(prefix: str, default_scopes: list[str]) -> list[str]:
    raw = _env(prefix, "OAUTH_SCOPES").strip()
    if not raw:
        return default_scopes
    if raw.startswith("["):
        parsed = parse_string_list(raw, default_scopes)
        return parsed or default_scopes
    return [part.strip() for part in raw.replace(",", " ").split() if part.strip()]


def parse_string_list(raw: str, fallback: list[str] | None) -> list[str] | None:
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("Allowed client redirect URIs must be JSON or CSV")
            return fallback
        if isinstance(parsed, list) and all(isinstance(item, str) for item in parsed):
            return [item.strip() for item in parsed if item.strip()]
        logger.warning("Allowed client redirect URI JSON must be a string list")
        return fallback
    return [part.strip() for part in raw.replace("\n", ",").split(",") if part.strip()]


def identity_from_access_token(token: AccessToken) -> ConnectorIdentity | None:
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
    return ConnectorIdentity(
        user_id=str(user_id),
        email=str(email) if email else None,
        source="oauth",
    )


def current_bearer_token(get_http_headers_fn: HeaderGetter) -> str | None:
    headers = get_http_headers_fn(include_all=True)
    auth_header = headers.get("authorization") or headers.get("Authorization")
    if not auth_header:
        return None
    scheme, _, token = auth_header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        return None
    return token.strip()


def identity_from_beta_token(prefix: str, token: str) -> ConnectorIdentity | None:
    token_map = load_beta_tokens(prefix)
    raw_identity = token_map.get(token)
    if raw_identity is None:
        return None
    if isinstance(raw_identity, str):
        return ConnectorIdentity(user_id=raw_identity, source="beta-token")
    if isinstance(raw_identity, dict):
        user_id = raw_identity.get("user_id") or raw_identity.get("sub")
        if not user_id:
            return None
        email = raw_identity.get("email")
        return ConnectorIdentity(
            user_id=str(user_id),
            email=str(email) if email else None,
            source="beta-token",
        )
    return None


def load_beta_tokens(prefix: str) -> dict[str, Any]:
    raw = _env(prefix, "BETA_TOKENS").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        logger.warning("%s_BETA_TOKENS must be a JSON object", prefix)
        return {}
    if not isinstance(parsed, dict):
        logger.warning("%s_BETA_TOKENS must be a JSON object", prefix)
        return {}
    return parsed


def _env(prefix: str, suffix: str, default: str = "") -> str:
    return os.environ.get(f"{prefix}_{suffix}", default)


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _safe_access_token(get_access_token_fn: AccessTokenGetter) -> AccessToken | None:
    try:
        return get_access_token_fn()
    except Exception:
        return None
