"""A connector 401 carries a sign-in hint; the OAuth header is untouched."""
import json

import httpx
import pytest

from src.connector_sign_in import SignInHintMiddleware, sign_in_hint

WWW_AUTH = (
    'Bearer error="invalid_token", error_description="Authentication failed.", '
    'resource_metadata="https://mcp.blocksize.info/.well-known/oauth-protected-resource/openai/mcp/"'
)


async def fake_connector(scope, receive, send):
    """Imitate the pinned FastMCP RequireAuthMiddleware and the OAuth routes."""
    if scope["path"].endswith("/token"):
        body = json.dumps({"error": "invalid_client", "error_description": "Unknown client."}).encode()
        status = 401
        headers = [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode())]
    elif scope["path"].endswith("/ok"):
        body = b'{"fine": true}'
        status = 200
        headers = [(b"content-type", b"application/json")]
    else:
        body = json.dumps({"error": "invalid_token", "error_description": "Authentication failed."}).encode()
        status = 401
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode()),
            (b"www-authenticate", WWW_AUTH.encode()),
        ]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": body[:5], "more_body": True})
    await send({"type": "http.response.body", "body": body[5:]})


def client():
    """The connector app is mounted at /openai/mcp, so Starlette sets that root_path."""
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=wrapped(), root_path="/openai/mcp"), base_url="https://t"
    )


def wrapped():
    return SignInHintMiddleware(
        fake_connector,
        connector="openai",
        mcp_url=lambda: "https://mcp.blocksize.info/openai/mcp",
        base_url="https://mcp.blocksize.info",
        allowance_label=lambda: "30,000",
    )


@pytest.mark.asyncio
async def test_401_without_a_token_says_so_and_points_at_the_sign_in_flow():
    async with client() as http:
        response = await http.get("/openai/mcp/")

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == WWW_AUTH
    assert int(response.headers["content-length"]) == len(response.content)
    body = response.json()
    assert body["error"] == "invalid_token"
    assert body["error_description"].startswith("No bearer token was sent.")
    hint = body["sign_in"]
    assert hint["connector"] == "https://mcp.blocksize.info/openai/mcp/"
    assert hint["token_sent"] is False
    assert "ChatGPT" in hint["how"]
    assert hint["resource_metadata"] == (
        "https://mcp.blocksize.info/.well-known/oauth-protected-resource/openai/mcp"
    )
    assert hint["authorization_server_metadata"].endswith("/oauth-authorization-server/openai/mcp")
    assert hint["auth_guide"] == "https://mcp.blocksize.info/auth.md"
    assert hint["after_sign_in"].startswith("30,000 free live-data credits")
    assert "/mcp/server/" in hint["without_sign_in"]


@pytest.mark.asyncio
async def test_401_with_a_token_keeps_the_servers_description():
    async with client() as http:
        response = await http.get("/openai/mcp/", headers={"Authorization": "Bearer stale"})

    body = response.json()
    assert body["error_description"] == "Authentication failed."
    assert body["sign_in"]["token_sent"] is True


@pytest.mark.asyncio
async def test_other_responses_pass_through_untouched():
    async with client() as http:
        response = await http.get("/openai/mcp/ok")

    assert response.status_code == 200
    assert response.json() == {"fine": True}


@pytest.mark.asyncio
async def test_token_endpoint_401_keeps_its_own_error():
    """A refresh grant from an unknown client answers invalid_client; that must
    not be rewritten into "no bearer token was sent" (the token endpoint takes
    none), which hid the real cause of the /openai/mcp/token 401s."""
    async with client() as http:
        response = await http.post("/openai/mcp/token", data={"grant_type": "refresh_token"})

    assert response.status_code == 401
    body = response.json()
    assert body == {"error": "invalid_client", "error_description": "Unknown client."}
    assert "sign_in" not in body


def test_hint_names_each_client_differently():
    for connector, word in (("anthropic", "Claude"), ("cursor", "Cursor"), ("openai", "ChatGPT")):
        hint = sign_in_hint(
            connector=connector,
            mcp_url=f"https://mcp.blocksize.info/{connector}/mcp",
            base_url="https://mcp.blocksize.info",
            token_sent=False,
            allowance_label="30,000",
        )
        assert word in hint["how"]
        assert hint["resource_metadata"].endswith(f"/{connector}/mcp")
