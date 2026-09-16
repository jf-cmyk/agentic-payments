"""Agent delegation security contracts. No real sign-ins, payments or network."""
import asyncio
import json
from types import SimpleNamespace

import jwt
import pytest
from fastmcp.server.auth import AccessToken
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.responses import JSONResponse
from starlette.testclient import TestClient

from src.agent_auth import AgentAuth, ASSERTION_GRANT, CLAIM_GRANT, COOKIE, TokenDispatch
from src.connector_auth import identity_from_access_token

ISSUER = "https://market.example/anthropic/mcp"


class Provider:
    def __init__(self):
        self.token = AccessToken(token="private-upstream", client_id="clerk-app", scopes=["email", "profile"],
                                 expires_at=2000000000, claims={"sub": "user_1", "iss": "clerk",
                                 "aud": "clerk-app", "email": "owner@example.com", "email_verified": True})
        self.exchanges = 0

    async def register_client(self, client):
        self.client = client

    async def authorize(self, client, params):
        self.params = params
        return "https://clerk.example/login"

    async def load_authorization_code(self, client, code):
        if code != "valid":
            return None
        return SimpleNamespace(expires_at=2000000000, redirect_uri=self.params.redirect_uri,
                               code_challenge=self.params.code_challenge)

    async def exchange_authorization_code(self, client, code):
        self.exchanges += 1
        return SimpleNamespace(access_token="private-upstream", expires_in=3600)

    async def load_access_token(self, token):
        return self.token if token == "private-upstream" else None

    load_original_token = load_access_token


@pytest.fixture
def rig(tmp_path):
    provider = Provider()
    service = AgentAuth(provider, issuer=ISSUER, secret="s" * 64, path=str(tmp_path / "auth.db"),
                        scopes=["email", "profile"])

    async def original(request):
        return JSONResponse({"original": dict(await request.form())})

    token_route = Route("/token", original, methods=["POST"])
    token_route.app = TokenDispatch(token_route.app, service)
    app = Starlette(routes=[Mount("/anthropic/mcp", routes=[token_route, *service.routes()])])
    with TestClient(app, base_url="https://market.example", follow_redirects=False) as client:
        yield SimpleNamespace(client=client, provider=provider, service=service, path=tmp_path / "auth.db")


def register(rig, **kwargs):
    response = rig.client.post(ISSUER + "/agent/identity", json={"type": "service_auth",
                               "login_hint": "owner@example.com", **kwargs})
    assert response.status_code == 200, response.text
    return response.json()


def sign_in(rig, registration):
    assert rig.client.get(registration["claim"]["verification_uri"]).status_code == 303
    state = rig.provider.params.state
    response = rig.client.get(ISSUER + "/agent/callback", params={"state": state, "code": "valid"})
    return response


def consent(rig, registration, **kwargs):
    with rig.service.store.transaction() as db:
        session = rig.service.store.get(db, "session", rig.client.cookies.get(COOKIE))
    return rig.client.post(ISSUER + "/agent/consent", headers={"Origin": "https://market.example"},
                           data={"csrf": session["csrf"], "decision": "approve",
                                 "user_code": registration["claim"]["user_code"], **kwargs})


def poll(rig, registration):
    return rig.client.post(ISSUER + "/token", data={"grant_type": CLAIM_GRANT,
                            "claim_token": registration["claim_token"]})


def grant(rig):
    registration = register(rig)
    assert sign_in(rig, registration).status_code == 303
    assert consent(rig, registration).status_code == 200
    result = poll(rig, registration)
    assert result.status_code == 200, result.text
    return registration, result.json()


def exchange(rig, assertion, **kwargs):
    return rig.client.post(ISSUER + "/token", data={"grant_type": ASSERTION_GRANT,
                           "assertion": assertion, "resource": ISSUER + "/", **kwargs})


def test_consent_required_and_poll_throttled(rig):
    reg = register(rig)
    assert "identity_assertion" not in reg and "access_token" not in reg
    assert reg["claim"]["user_code"] not in reg["claim"]["verification_uri"]
    assert poll(rig, reg).json()["error"] == "authorization_pending"
    assert poll(rig, reg).status_code == 429
    assert rig.client.get(ISSUER + "/agent/consent").status_code == 401


def test_full_flow_keeps_identity_and_no_refresh_or_upstream_leak(rig):
    reg, issued = grant(rig)
    token = asyncio.run(rig.service.delegated_token(issued["access_token"]))
    assert identity_from_access_token(token, namespace="ANTHROPIC") == identity_from_access_token(
        rig.provider.token, namespace="ANTHROPIC")
    assert "refresh_token" not in issued and "private-upstream" not in json.dumps(issued)
    assert issued["access_token"].startswith("bsa_")
    assert 0 < issued["expires_in"] <= 3600
    assert exchange(rig, issued["identity_assertion"]).status_code == 200
    for value in [b"owner@example.com", b"private-upstream", issued["access_token"].encode(),
                  reg["claim_token"].encode(), reg["claim"]["user_code"].encode()]:
        assert value not in rig.path.read_bytes()
    restarted = AgentAuth(rig.provider, issuer=ISSUER, secret="s" * 64, path=str(rig.path),
                          scopes=["email", "profile"])
    assert asyncio.run(restarted.delegated_token(issued["access_token"])) is not None


@pytest.mark.parametrize("email,verified", [("attacker@example.com", True),
                                             ("owner@example.com", False)])
def test_wrong_or_unverified_account_cannot_approve(rig, email, verified):
    reg = register(rig)
    rig.provider.token.claims.update(email=email, email_verified=verified)
    response = sign_in(rig, reg)
    if verified:
        assert response.status_code == 303
        assert consent(rig, reg).status_code == 403
    else:
        assert response.status_code == 403


def test_csrf_wrong_codes_and_replay(rig):
    reg = register(rig, agent_name="<script>alert(1)</script>")
    assert sign_in(rig, reg).status_code == 303
    assert "<script>" not in rig.client.get(ISSUER + "/agent/consent").text
    assert consent(rig, reg, csrf="wrong").status_code == 403
    for _ in range(5):
        assert consent(rig, reg, user_code="invalid").json()["error"] == "invalid_user_code"
    assert consent(rig, reg).json()["error"] == "expired_token"
    assert poll(rig, reg).json()["error"] == "access_denied"


def test_state_and_pkce_fail_closed_and_callback_one_use(rig):
    reg = register(rig)
    rig.client.get(reg["claim"]["verification_uri"])
    state = rig.provider.params.state
    assert rig.client.get(ISSUER + "/agent/callback", params={"state": "forged"}).status_code == 400
    rig.provider.params.code_challenge = "wrong"
    params = {"state": state, "code": "valid"}
    assert rig.client.get(ISSUER + "/agent/callback", params=params).status_code == 400
    assert rig.client.get(ISSUER + "/agent/callback", params=params).json()["error"] == "invalid_state"
    assert rig.provider.exchanges == 0


def test_token_revocation_then_owner_revocation(rig):
    reg, issued = grant(rig)
    revoke = ISSUER + "/agent/revoke"
    assert rig.client.post(revoke, data={"token": issued["access_token"]}).status_code == 200
    assert asyncio.run(rig.service.delegated_token(issued["access_token"])) is None
    fresh = exchange(rig, issued["identity_assertion"]).json()
    assert fresh["access_token"] != issued["access_token"]
    with rig.service.store.transaction() as db:
        session = rig.service.store.get(db, "session", rig.client.cookies.get(COOKIE))
    assert rig.client.post(ISSUER + "/agent/agents", headers={"Origin": "https://market.example"},
                           data={"csrf": session["csrf"], "registration_id": reg["registration_id"]}).status_code == 200
    assert asyncio.run(rig.service.delegated_token(fresh["access_token"])) is None
    assert exchange(rig, issued["identity_assertion"]).status_code == 400
    assert rig.client.post(revoke, data={"token": "unknown"}).status_code == 200


@pytest.mark.parametrize("change", [{"iss": "https://evil.example"}, {"aud": "wrong"},
                                    {"exp": 1}, {"sub": "unknown"}, {"typ": "JWT"}])
def test_assertion_substitution_rejected(rig, change):
    _, issued = grant(rig)
    claims = jwt.decode(issued["identity_assertion"], options={"verify_signature": False})
    typ = change.pop("typ", "oauth-id-jag+jwt")
    claims.update(change)
    forged = jwt.encode(claims, rig.service.store.signing_key, algorithm="HS256", headers={"typ": typ})
    assert exchange(rig, forged).status_code == 400
    assert exchange(rig, issued["identity_assertion"], resource="https://evil.example/").status_code == 400


def test_standard_grants_still_reach_original_handler(rig):
    data = {"grant_type": "authorization_code", "code": "a+b&c", "code_verifier": "value"}
    assert rig.client.post(ISSUER + "/token", data=data).json() == {"original": data}
    assert rig.client.post(ISSUER + "/token", content=b"x" * 8193).status_code == 413
    assert rig.client.post(ISSUER + "/token", content="grant_type=a&grant_type=b").status_code == 400


def test_only_supported_type_and_registration_rate_limit(rig):
    for kind in ["anonymous", "identity_assertion"]:
        assert rig.client.post(ISSUER + "/agent/identity", json={"type": kind}).status_code == 400
    for _ in range(10):
        register(rig)
    assert rig.client.post(ISSUER + "/agent/identity", json={"type": "service_auth",
                           "login_hint": "owner@example.com"}).status_code == 429


def test_expired_claim_and_upstream_revocation(rig, monkeypatch):
    reg, issued = grant(rig)
    rig.provider.token = None
    assert asyncio.run(rig.service.delegated_token(issued["access_token"])) is None
    monkeypatch.setattr("src.agent_auth.time.time", lambda: 2000000100)
    assert poll(rig, reg).json()["error"] == "expired_token"


def test_provider_routes_keep_standard_oauth_and_only_exact_callback(monkeypatch, tmp_path):
    from key_value.aio.stores.memory import MemoryStore
    from src.agent_auth_provider import AgentClerkProvider
    monkeypatch.setenv("AGENT_AUTH_SECRET", "s" * 64)
    monkeypatch.setenv("AGENT_AUTH_DB_PATH", str(tmp_path / "provider.db"))
    provider = AgentClerkProvider(domain="clerk.example", client_id="test", client_secret="test",
        base_url=ISSUER, jwt_signing_key="s" * 64, required_scopes=["email", "profile"],
        valid_scopes=["email", "profile"], client_storage=MemoryStore(),
        allowed_client_redirect_uris=["https://claude.ai/api/mcp/auth_callback"])
    paths = [route.path for route in provider.get_routes("/")]
    assert paths.count("/token") == 1
    assert "/agent/identity" in paths and "/auth/callback" in paths
    assert provider._allowed_client_redirect_uris == ["https://claude.ai/api/mcp/auth_callback",
                                                      ISSUER + "/agent/callback"]
    assert provider.agent_auth.metadata()["agent_auth"]["identity_types_supported"] == ["service_auth"]
    with TestClient(Starlette(routes=provider.get_routes("/")), base_url="https://market.example") as client:
        metadata = client.get("/.well-known/oauth-authorization-server").json()
    assert metadata["issuer"] == ISSUER
    assert CLAIM_GRANT in metadata["grant_types_supported"]
    assert metadata["agent_auth"]["identity_endpoint"] == ISSUER + "/agent/identity"
    assert "none" in metadata["token_endpoint_auth_methods_supported"]


def test_configuration_fails_closed(monkeypatch, tmp_path):
    from src.anthropic_auth import build_anthropic_auth_provider
    monkeypatch.setenv("AGENT_AUTH_ENABLED", "true")
    monkeypatch.setenv("ANTHROPIC_AUTH_PROVIDER", "none")
    with pytest.raises(ValueError, match="requires"):
        build_anthropic_auth_provider()
    with pytest.raises(ValueError, match="SECRET"):
        AgentAuth(Provider(), issuer=ISSUER, secret="short", path=str(tmp_path / "bad.db"),
                  scopes=["email", "profile"])


def test_auth_query_secrets_redacted():
    from src.security_config import redact_sensitive_query_values
    value = redact_sensitive_query_values("/agent/claim?claim_attempt_token=secret&state=state-secret")
    assert "secret" not in value and "[REDACTED]" in value
