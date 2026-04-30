from __future__ import annotations

import json

from fastmcp.server.auth import AccessToken

from src import anthropic_auth


def _raise_no_access_token():
    raise RuntimeError("no request token in unit test")


def test_beta_tokens_enabled_for_closed_beta(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_PROVIDER", "none")

    assert anthropic_auth.beta_tokens_enabled() is True


def test_beta_tokens_disabled_by_default_when_oauth_provider(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_AUTH_PROVIDER", "clerk")
    monkeypatch.delenv("ANTHROPIC_ENABLE_BETA_TOKENS", raising=False)

    assert anthropic_auth.beta_tokens_enabled() is False


def test_beta_token_identity_ignored_when_oauth_provider_is_enabled(monkeypatch):
    token = "test-token"
    monkeypatch.setenv("ANTHROPIC_AUTH_PROVIDER", "clerk")
    monkeypatch.setenv(
        "ANTHROPIC_BETA_TOKENS",
        json.dumps({token: {"user_id": "beta-user", "email": "beta@example.com"}}),
    )
    monkeypatch.setattr(anthropic_auth, "get_access_token", _raise_no_access_token)
    monkeypatch.setattr(
        anthropic_auth,
        "get_http_headers",
        lambda include_all=True: {"authorization": f"Bearer {token}"},
    )

    assert anthropic_auth.resolve_anthropic_identity() is None


def test_beta_token_identity_can_be_explicitly_enabled_for_oauth_testing(monkeypatch):
    token = "test-token"
    monkeypatch.setenv("ANTHROPIC_AUTH_PROVIDER", "clerk")
    monkeypatch.setenv("ANTHROPIC_ENABLE_BETA_TOKENS", "true")
    monkeypatch.setenv(
        "ANTHROPIC_BETA_TOKENS",
        json.dumps({token: {"user_id": "beta-user", "email": "beta@example.com"}}),
    )
    monkeypatch.setattr(anthropic_auth, "get_access_token", _raise_no_access_token)
    monkeypatch.setattr(
        anthropic_auth,
        "get_http_headers",
        lambda include_all=True: {"authorization": f"Bearer {token}"},
    )

    identity = anthropic_auth.resolve_anthropic_identity()

    assert identity is not None
    assert identity.user_id == "beta-user"
    assert identity.email == "beta@example.com"
    assert identity.source == "beta-token"


def test_oauth_identity_uses_upstream_claims(monkeypatch):
    access_token = AccessToken(
        token="oauth-token",
        client_id="oauth-client",
        scopes=["openid", "email", "profile"],
        claims={
            "sub": "proxy-sub",
            "upstream_claims": {
                "sub": "clerk-user-123",
                "email": "user@example.com",
            },
        },
    )
    monkeypatch.setenv("ANTHROPIC_AUTH_PROVIDER", "clerk")
    monkeypatch.setattr(anthropic_auth, "get_access_token", lambda: access_token)

    identity = anthropic_auth.resolve_anthropic_identity()

    assert identity is not None
    assert identity.user_id == "clerk-user-123"
    assert identity.email == "user@example.com"
    assert identity.source == "oauth"


def test_default_client_redirect_allowlist_is_claude_and_loopback(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_ALLOWED_CLIENT_REDIRECT_URIS", raising=False)

    allowed = anthropic_auth._allowed_client_redirect_uris()

    assert allowed == [
        "https://claude.ai/api/mcp/auth_callback",
        "http://localhost:*",
        "http://127.0.0.1:*",
    ]


def test_oauth_callback_url_uses_mcp_public_url(monkeypatch):
    monkeypatch.setenv(
        "ANTHROPIC_MCP_PUBLIC_URL",
        "https://anthropic-mcp-beta-production.up.railway.app/anthropic/mcp",
    )

    assert (
        anthropic_auth.oauth_callback_url()
        == "https://anthropic-mcp-beta-production.up.railway.app/anthropic/mcp/auth/callback"
    )
