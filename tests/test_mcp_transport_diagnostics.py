"""A failed token grant is logged by grant type, client-id prefix and error code."""
import json
import logging

import httpx
import pytest

from src.mcp_transport_diagnostics import TokenEndpointDiagnostics

REFRESH_TOKEN = "rt-secret-value-never-logged"
CLIENT_ID = "client-abcdefghijklmnop"


async def fake_oauth(scope, receive, send):
    """Imitate the mounted connector: /token answers by client, the MCP endpoint 401s."""
    body = b""
    while True:
        message = await receive()
        body += message.get("body", b"")
        if not message.get("more_body", False):
            break
    if scope["path"].endswith("/token"):
        if b"client_id=good" in body:
            payload = {"access_token": "at", "token_type": "bearer"}
            status = 200
        else:
            payload = {"error": "invalid_client", "error_description": "Unknown client."}
            status = 401
    else:
        payload = {"error": "invalid_token"}
        status = 401
    raw = json.dumps(payload).encode()
    headers = [(b"content-type", b"application/json"), (b"content-length", str(len(raw)).encode())]
    await send({"type": "http.response.start", "status": status, "headers": headers})
    await send({"type": "http.response.body", "body": raw[:3], "more_body": True})
    await send({"type": "http.response.body", "body": raw[3:]})


def client():
    app = TokenEndpointDiagnostics(fake_oauth, surface="openai_mcp")
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app, root_path="/openai/mcp"), base_url="https://t"
    )


@pytest.mark.asyncio
async def test_failed_refresh_grant_is_logged_without_its_secrets(caplog):
    caplog.set_level(logging.WARNING, logger="src.mcp_transport_diagnostics")
    async with client() as http:
        response = await http.post(
            "/openai/mcp/token",
            data={"grant_type": "refresh_token", "refresh_token": REFRESH_TOKEN, "client_id": CLIENT_ID},
        )

    assert response.status_code == 401
    assert response.json()["error"] == "invalid_client"
    lines = [record.getMessage() for record in caplog.records]
    assert len(lines) == 1
    assert lines[0] == (
        "OAuth token endpoint 401 on openai_mcp: grant_type=refresh_token "
        f"client_id={CLIENT_ID[:12]}... error=invalid_client"
    )
    assert REFRESH_TOKEN not in lines[0]
    assert CLIENT_ID not in lines[0]


@pytest.mark.asyncio
async def test_successful_grant_and_other_paths_log_nothing(caplog):
    caplog.set_level(logging.WARNING, logger="src.mcp_transport_diagnostics")
    async with client() as http:
        ok = await http.post("/openai/mcp/token", data={"grant_type": "refresh_token", "client_id": "good"})
        mcp = await http.post("/openai/mcp/", content=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}')

    assert ok.status_code == 200 and ok.json()["access_token"] == "at"
    assert mcp.status_code == 401
    assert caplog.records == []


@pytest.mark.asyncio
async def test_missing_form_fields_are_named_not_guessed(caplog):
    caplog.set_level(logging.WARNING, logger="src.mcp_transport_diagnostics")
    async with client() as http:
        await http.post("/openai/mcp/token", content=b"", headers={"content-type": "text/plain"})

    assert [r.getMessage() for r in caplog.records] == [
        "OAuth token endpoint 401 on openai_mcp: grant_type=missing client_id=missing error=invalid_client"
    ]
