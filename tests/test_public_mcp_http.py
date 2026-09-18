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
