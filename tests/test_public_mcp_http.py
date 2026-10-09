"""The public discovery MCP serves every request without a session."""

import httpx
import pytest
from fastmcp import FastMCP
from fastmcp.server.http import StreamableHTTPASGIApp

from src.public_mcp_http import create_public_http_app


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


def server():
    public = FastMCP("public", instructions="discovery")

    @public.tool
    def ping() -> str:
        return "pong"

    return public


INITIALIZE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {"protocolVersion": "2025-03-26", "capabilities": {},
               "clientInfo": {"name": "stateless-test", "version": "1"}},
}
TOOLS_LIST = {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}}


@pytest.mark.asyncio
async def test_public_app_runs_stateless():
    app = create_public_http_app(server())
    async with app.lifespan(app):
        manager = manager_for(app)
        assert manager.stateless is True
        assert manager.session_idle_timeout is None


@pytest.mark.asyncio
async def test_a_stateful_manager_is_refused(monkeypatch):
    public = server()
    original = public.http_app

    def stateful(**kwargs):
        kwargs.pop("stateless_http", None)
        return original(**kwargs)

    monkeypatch.setattr(public, "http_app", stateful)
    app = create_public_http_app(public)
    # The lifespan runs inside a task group, which wraps the error.
    with pytest.raises(BaseExceptionGroup) as info:
        async with app.lifespan(app):
            pass
    assert info.group_contains(RuntimeError, match="must run stateless")


@pytest.mark.asyncio
async def test_requests_are_served_without_initialize_or_session():
    """Real clients post tools/list and notifications/initialized without ever
    initializing; they used to get 400 "Missing session ID"."""
    app = create_public_http_app(server())
    async with app.lifespan(app), client_for(app) as client:
        tools = await client.post("/", json=TOOLS_LIST)
        assert tools.status_code == 200, tools.text
        assert "ping" in tools.text
        initialized = await client.post(
            "/", json={"jsonrpc": "2.0", "method": "notifications/initialized"}
        )
        assert initialized.status_code == 202
        call = await client.post("/", json={
            "jsonrpc": "2.0", "id": 3, "method": "tools/call",
            "params": {"name": "ping", "arguments": {}},
        })
        assert call.status_code == 200 and "pong" in call.text


@pytest.mark.asyncio
async def test_initialize_still_answers_but_mints_no_session():
    app = create_public_http_app(server())
    async with app.lifespan(app), client_for(app) as client:
        initialize = await client.post("/", json=INITIALIZE)
        assert initialize.status_code == 200, initialize.text
        assert "mcp-session-id" not in initialize.headers
        assert "discovery" in initialize.text
        # A session header from an older client is ignored, not rejected.
        stale = await client.post("/", headers={"mcp-session-id": "from-before"}, json=TOOLS_LIST)
        assert stale.status_code == 200


@pytest.mark.asyncio
async def test_listen_get_is_an_empty_stream_and_delete_is_not_supported():
    """The SDK serves a stateless app on POST only; the listen GET gets the
    bounded empty stream (as on the OpenAI connector) without a bearer token,
    and session termination answers 405 as the specification allows."""
    app = create_public_http_app(server())
    async with app.lifespan(app), client_for(app) as client:
        listen = await client.get("/")
        assert listen.status_code == 200
        assert listen.headers["content-type"].startswith("text/event-stream")
        assert listen.headers["x-mcp-listen-stream"] == "stateless"
        assert listen.text.startswith(": stream open")
        close = await client.delete("/", headers={"mcp-session-id": "anything"})
        assert close.status_code == 405


@pytest.mark.parametrize("accept", [None, "*/*", "application/json", "text/event-stream", "application/*"])
@pytest.mark.asyncio
async def test_requests_are_accepted_whatever_accept_header_the_client_sends(accept):
    app = create_public_http_app(server())
    async with app.lifespan(app):
        headers = {} if accept is None else {"Accept": accept}
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://localhost", headers=headers
        ) as client:
            response = await client.post("/", json=TOOLS_LIST)
    assert response.status_code == 200, response.text
    assert "ping" in response.text


@pytest.mark.asyncio
async def test_malformed_json_still_reaches_the_sdk_parser():
    app = create_public_http_app(server())
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
