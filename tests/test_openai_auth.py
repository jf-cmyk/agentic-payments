from __future__ import annotations

import json

from src import openai_auth


def _raise_no_access_token():
    raise RuntimeError("no request token in unit test")


def test_openai_default_scopes_include_refresh_access(monkeypatch):
    monkeypatch.delenv("OPENAI_OAUTH_SCOPES", raising=False)

    assert openai_auth.oauth_scopes() == [
        "openid",
        "email",
        "profile",
        "offline_access",
    ]


def test_openai_redirect_allowlist_defaults_to_loopback(monkeypatch):
    monkeypatch.delenv("OPENAI_ALLOWED_CLIENT_REDIRECT_URIS", raising=False)

    assert openai_auth._allowed_client_redirect_uris() == [
        "http://localhost:*",
        "http://127.0.0.1:*",
    ]


def test_openai_beta_identity_can_be_enabled_for_private_testing(monkeypatch):
    token = "openai-test-token"
    monkeypatch.setenv("OPENAI_AUTH_PROVIDER", "clerk")
    monkeypatch.setenv("OPENAI_ENABLE_BETA_TOKENS", "true")
    monkeypatch.setenv(
        "OPENAI_BETA_TOKENS",
        json.dumps({token: {"user_id": "openai-user", "email": "openai@example.com"}}),
    )
    monkeypatch.setattr(openai_auth, "get_access_token", _raise_no_access_token)
    monkeypatch.setattr(
        openai_auth,
        "get_http_headers",
        lambda include_all=True: {"authorization": f"Bearer {token}"},
    )

    identity = openai_auth.resolve_openai_identity()

    assert identity is not None
    assert identity.user_id == "openai-user"
    assert identity.email == "openai@example.com"
    assert identity.source == "beta-token"


def test_openai_oauth_callback_uses_connector_public_url(monkeypatch):
    monkeypatch.setenv(
        "OPENAI_MCP_PUBLIC_URL",
        "https://mcp.blocksize.info/openai/mcp",
    )

    assert (
        openai_auth.oauth_callback_url()
        == "https://mcp.blocksize.info/openai/mcp/auth/callback"
    )
