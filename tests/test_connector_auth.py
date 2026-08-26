from __future__ import annotations

import pytest
from fastmcp.server.auth import AccessToken

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


@pytest.mark.parametrize("namespace", ["ANTHROPIC", "CURSOR", "OPENAI"])
def test_oauth_ledger_subject_is_connector_issuer_and_audience_scoped(namespace):
    def identity(*, issuer: str, audience: list[str]):
        return connector_auth.identity_from_access_token(
            AccessToken(
                token="oauth-token",
                client_id="connector-client",
                scopes=["openid"],
                claims={
                    "sub": "shared-upstream-subject",
                    "iss": issuer,
                    "aud": audience,
                },
            ),
            namespace=namespace,
        )

    baseline = identity(issuer="https://issuer.example", audience=["api-b", "api-a"])
    reordered = identity(issuer="https://issuer.example", audience=["api-a", "api-b"])
    other_issuer = identity(issuer="https://other.example", audience=["api-a", "api-b"])
    other_audience = identity(issuer="https://issuer.example", audience=["api-c"])

    assert baseline is not None
    assert reordered is not None
    assert other_issuer is not None
    assert other_audience is not None
    assert baseline.user_id == "shared-upstream-subject"
    assert baseline.legacy_ledger_subject == "shared-upstream-subject"
    assert baseline.ledger_subject.startswith(f"{namespace.lower()}:")
    assert baseline.ledger_subject == reordered.ledger_subject
    assert baseline.ledger_subject != other_issuer.ledger_subject
    assert baseline.ledger_subject != other_audience.ledger_subject
