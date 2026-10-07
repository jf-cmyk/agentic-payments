"""Exercise real pinned SDK session lifecycle without upstream calls or payment."""

import asyncio

import httpx
import pytest
from fastmcp import FastMCP
from fastmcp.server.http import StreamableHTTPASGIApp

from src.public_mcp_http import PUBLIC_SESSION_IDLE_SECONDS, create_public_http_app


def manager_for(app):
    return next(
        route.endpoint.session_manager
        for route in app.routes
        if isinstance(getattr(route, "endpoint", None), StreamableHTTPASGIApp)
    )


def client_for(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
        headers={"Accept": "application/json, text/event-stream"},
    )


async def initialize(client):
    response = await client.post("/", json={
        "jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                   "clientInfo": {"name": "session-cleanup-test", "version": "1"}},
    })
    assert response.status_code == 200
    session = response.headers["mcp-session-id"]
    response = await client.post("/", headers={"mcp-session-id": session}, json={
        "jsonrpc": "2.0", "method": "notifications/initialized",
    })
    assert response.status_code == 202
    return session


async def list_tools(client, session):
    return await client.post("/", headers={"mcp-session-id": session}, json={
        "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
    })


@pytest.mark.asyncio
async def test_default_policy_is_installed_before_first_request():
    app = create_public_http_app(FastMCP("default-timeout"))
    async with app.lifespan(app):
        assert manager_for(app).session_idle_timeout == PUBLIC_SESSION_IDLE_SECONDS == 1800


@pytest.mark.parametrize("seconds", [0, -1, float("inf"), float("nan")])
def test_invalid_timeout_is_rejected(seconds):
    with pytest.raises(ValueError):
        create_public_http_app(FastMCP("invalid-timeout"), idle_seconds=seconds)


@pytest.mark.asyncio
async def test_abandoned_sessions_expire_and_client_can_reinitialize():
    app = create_public_http_app(FastMCP("expiry"), idle_seconds=0.2)
    async with app.lifespan(app), client_for(app) as client:
        old_session = await initialize(client)
        assert (await list_tools(client, old_session)).status_code == 200
        await asyncio.sleep(0.4)
        assert manager_for(app)._server_instances == {}
        assert manager_for(app)._session_owners == {}
        assert (await list_tools(client, old_session)).status_code == 404
        new_session = await initialize(client)
        assert new_session != old_session
        assert (await list_tools(client, new_session)).status_code == 200


@pytest.mark.asyncio
async def test_activity_extends_session_and_delete_still_cleans_up():
    app = create_public_http_app(FastMCP("active-session"), idle_seconds=0.5)
    async with app.lifespan(app), client_for(app) as client:
        session = await initialize(client)
        for _ in range(4):
            await asyncio.sleep(0.2)
            assert (await list_tools(client, session)).status_code == 200
        response = await client.delete("/", headers={"mcp-session-id": session})
        assert response.status_code == 200
        await asyncio.sleep(0.05)
        assert session not in manager_for(app)._server_instances
        assert (await list_tools(client, session)).status_code == 404


@pytest.mark.asyncio
async def test_rejected_delete_does_not_remove_another_session():
    app = create_public_http_app(FastMCP("invalid-delete"))
    async with app.lifespan(app), client_for(app) as client:
        session = await initialize(client)
        response = await client.delete("/", headers={"mcp-session-id": "not-a-session"})
        assert response.status_code == 404
        assert session in manager_for(app)._server_instances
        assert (await list_tools(client, session)).status_code == 200


@pytest.mark.asyncio
async def test_uninitialized_sessions_are_also_reaped():
    app = create_public_http_app(FastMCP("abandoned-handshake"), idle_seconds=0.1)
    async with app.lifespan(app), client_for(app) as client:
        # Even clients that never send notifications/initialized must expire.
        for index in range(10):
            response = await client.post("/", json={
                "jsonrpc": "2.0", "id": index, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "abandoned", "version": "1"}},
            })
            assert response.status_code == 200
        await asyncio.sleep(0.3)
        assert manager_for(app)._server_instances == {}


@pytest.mark.asyncio
async def test_stateless_mode_has_no_idle_timer(monkeypatch):
    server = FastMCP("stateless")
    original = server.http_app
    monkeypatch.setattr(server, "http_app", lambda **kwargs: original(**kwargs, stateless_http=True))
    app = create_public_http_app(server)
    async with app.lifespan(app):
        assert manager_for(app).stateless is True
        assert manager_for(app).session_idle_timeout is None


@pytest.mark.asyncio
async def test_fresh_manager_receives_policy_on_each_lifespan_entry():
    app = create_public_http_app(FastMCP("restart"))
    async with app.lifespan(app):
        first_manager = manager_for(app)
    async with app.lifespan(app):
        assert manager_for(app) is not first_manager
        assert manager_for(app).session_idle_timeout == 1800


@pytest.mark.parametrize("accept", [None, "*/*", "application/json", "text/event-stream", "application/*"])
@pytest.mark.asyncio
async def test_initialize_is_accepted_whatever_accept_header_the_client_sends(accept):
    app = create_public_http_app(FastMCP("accept"))
    async with app.lifespan(app):
        headers = {} if accept is None else {"Accept": accept}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost", headers=headers
        ) as client:
            response = await client.post("/", json={
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {"protocolVersion": "2025-03-26", "capabilities": {},
                           "clientInfo": {"name": "lenient-accept", "version": "1"}},
            })
    assert response.status_code == 200, response.text
    assert "mcp-session-id" in response.headers


@pytest.mark.asyncio
async def test_call_without_a_session_explains_how_to_start_one():
    app = create_public_http_app(FastMCP("session-hint"), quickstart_url="https://example.test/q")
    async with app.lifespan(app), client_for(app) as client:
        response = await client.post("/", json={
            "jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {},
        })
        listen = await client.get("/")
        close = await client.delete("/")

    for reply in (response, listen, close):
        assert reply.status_code == 400
        body = reply.json()
        assert body["error"]["message"] == "Bad Request: Missing session ID"
        data = body["error"]["data"]
        assert data["error_code"] == "MCP_SESSION_REQUIRED"
        assert data["initialize_example"]["method"] == "initialize"
        assert data["accept_header"] == "application/json, text/event-stream"
        assert data["quickstart"] == "https://example.test/q"
        assert any("mcp-session-id" in step for step in data["how_to_fix"])
    assert response.json()["id"] == 7
    assert listen.json()["id"] == "server-error"


@pytest.mark.asyncio
async def test_session_rule_still_applies_after_the_hint():
    app = create_public_http_app(FastMCP("session-kept"))
    async with app.lifespan(app), client_for(app) as client:
        session = await initialize(client)
        assert (await list_tools(client, session)).status_code == 200
        stale = await client.post("/", headers={"mcp-session-id": "not-a-session"}, json={
            "jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {},
        })
    assert stale.status_code == 404


@pytest.mark.asyncio
async def test_malformed_json_without_a_session_still_reaches_the_sdk_parser():
    app = create_public_http_app(FastMCP("parse-error"))
    async with app.lifespan(app), client_for(app) as client:
        response = await client.post("/", content=b"{not json", headers={"Content-Type": "application/json"})
    assert response.status_code == 400
    assert "Parse error" in response.json()["error"]["message"]


def test_normalised_accept_only_adds_what_is_missing():
    from src.public_mcp_http import normalised_accept

    assert normalised_accept("application/json, text/event-stream") is None
    assert normalised_accept("text/event-stream;q=0.9, application/json") is None
    assert normalised_accept(None) == "application/json, text/event-stream"
    assert normalised_accept("*/*") == "*/*, application/json, text/event-stream"
    assert normalised_accept("application/json") == "application/json, text/event-stream"
    assert normalised_accept("text/html") == "text/html, application/json, text/event-stream"
