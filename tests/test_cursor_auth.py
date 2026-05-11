from __future__ import annotations

import json

from src import cursor_auth


def _raise_no_access_token():
    raise RuntimeError("no request token in unit test")


def test_cursor_default_redirect_allowlist_supports_cursor_and_loopback(monkeypatch):
    monkeypatch.delenv("CURSOR_ALLOWED_CLIENT_REDIRECT_URIS", raising=False)

    allowed = cursor_auth._allowed_client_redirect_uris()

    assert allowed == [
        "http://localhost:*",
        "http://127.0.0.1:*",
        "cursor://*",
    ]


def test_cursor_redirect_allowlist_can_be_open_for_private_oauth_qa(monkeypatch):
    monkeypatch.setenv("CURSOR_ALLOWED_CLIENT_REDIRECT_URIS", "*")

    assert cursor_auth._allowed_client_redirect_uris() is None


def test_cursor_beta_tokens_disabled_by_default_when_oauth_provider(monkeypatch):
    monkeypatch.setenv("CURSOR_AUTH_PROVIDER", "clerk")
    monkeypatch.delenv("CURSOR_ENABLE_BETA_TOKENS", raising=False)

    assert cursor_auth.beta_tokens_enabled() is False


def test_cursor_beta_token_identity_can_be_enabled_for_private_testing(monkeypatch):
    token = "cursor-test-token"
    monkeypatch.setenv("CURSOR_AUTH_PROVIDER", "clerk")
    monkeypatch.setenv("CURSOR_ENABLE_BETA_TOKENS", "true")
    monkeypatch.setenv(
        "CURSOR_BETA_TOKENS",
        json.dumps({token: {"user_id": "cursor-user", "email": "cursor@example.com"}}),
    )
    monkeypatch.setattr(cursor_auth, "get_access_token", _raise_no_access_token)
    monkeypatch.setattr(
        cursor_auth,
        "get_http_headers",
        lambda include_all=True: {"authorization": f"Bearer {token}"},
    )

    identity = cursor_auth.resolve_cursor_identity()

    assert identity is not None
    assert identity.user_id == "cursor-user"
    assert identity.email == "cursor@example.com"
    assert identity.source == "beta-token"


def test_cursor_oauth_callback_url_uses_cursor_public_url(monkeypatch):
    monkeypatch.setenv(
        "CURSOR_MCP_PUBLIC_URL",
        "https://mcp.blocksize.info/cursor/mcp",
    )

    assert (
        cursor_auth.oauth_callback_url()
        == "https://mcp.blocksize.info/cursor/mcp/auth/callback"
    )
