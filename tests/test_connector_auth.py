from __future__ import annotations

from src import connector_auth


def test_oauth_client_storage_is_disabled_without_storage_dir(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_OAUTH_STORAGE_DIR", raising=False)

    assert (
        connector_auth.oauth_client_storage_for(
            "ANTHROPIC",
            jwt_signing_key=None,
            fallback_secret="stable-clerk-secret",
        )
        is None
    )


async def test_oauth_client_storage_persists_in_configured_directory(tmp_path, monkeypatch):
    storage_dir = tmp_path / "anthropic_oauth"
    monkeypatch.setenv("ANTHROPIC_OAUTH_STORAGE_DIR", str(storage_dir))
    monkeypatch.delenv("ANTHROPIC_OAUTH_STORAGE_ENCRYPTION_KEY", raising=False)

    first = connector_auth.oauth_client_storage_for(
        "ANTHROPIC",
        jwt_signing_key=None,
        fallback_secret="stable-clerk-secret",
    )
    assert first is not None
    await first.put("jti-1", {"upstream_token_id": "token-1"}, collection="mcp-jti-mappings")

    second = connector_auth.oauth_client_storage_for(
        "ANTHROPIC",
        jwt_signing_key=None,
        fallback_secret="stable-clerk-secret",
    )
    assert second is not None

    assert await second.get("jti-1", collection="mcp-jti-mappings") == {
        "upstream_token_id": "token-1"
    }
    assert storage_dir.exists()
