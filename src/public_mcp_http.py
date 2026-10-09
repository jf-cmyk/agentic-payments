"""Serve the public discovery MCP without sessions.

Real clients post ``tools/list`` and ``notifications/initialized`` to the
public endpoint without ever initializing (several per minute in October
2026) and were answered 400 "Missing session ID" with a hint; clients on the
2026-07-28 revision never initialize at all. The app therefore runs the
pinned SDK's stateless mode: every request stands alone, ``initialize`` is
still answered but mints no ``Mcp-Session-Id``, and a session header from an
older client is ignored. The SDK serves a stateless app on POST only, so the
listen GET gets the same empty, bounded event stream as the OpenAI connector
and DELETE (session termination) answers 405, which the specification allows.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastmcp import FastMCP
from fastmcp.server.http import StarletteWithLifespan, StreamableHTTPASGIApp
from starlette.datastructures import Headers

from src.mcp_transport_compat import ListenStreamShim

JSON_TYPE = "application/json"
SSE_TYPE = "text/event-stream"
logger = logging.getLogger(__name__)


def normalised_accept(value: str | None) -> str | None:
    """Return an Accept header the pinned SDK accepts, or None when it already is.

    The SDK insists on literal ``application/json`` and ``text/event-stream``
    entries and answers 406 to ``*/*``, a missing header, or either type alone.
    Real MCP clients send all of those, so the public discovery server adds the
    missing entries instead of refusing the request.
    """
    raw = (value or "").strip()
    entries = [part.strip() for part in raw.split(",") if part.strip()]
    media = [entry.split(";", 1)[0].strip().lower() for entry in entries]
    missing = [
        media_type
        for media_type in (JSON_TYPE, SSE_TYPE)
        if not any(item.startswith(media_type) for item in media)
    ]
    if not missing:
        return None
    return ", ".join([*entries, *missing])


class _LenientAccept:
    """Pure ASGI: add the Accept entries the pinned SDK insists on."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            accept = normalised_accept(Headers(scope=scope).get("accept"))
            if accept is not None:
                scope = dict(scope)
                scope["headers"] = [
                    (name, value) for name, value in scope["headers"] if name != b"accept"
                ] + [(b"accept", accept.encode("latin-1"))]
        await self.app(scope, receive, send)


def create_public_http_app(server: FastMCP) -> StarletteWithLifespan:
    """Stateless streamable HTTP for the public discovery server.

    FastMCP creates its session manager during lifespan entry, not at app
    construction, so the stateless mode is verified there: a stateful manager
    would reintroduce the session rule for every client.
    """
    app = server.http_app(path="/", transport="streamable-http", stateless_http=True)
    original_lifespan = app.lifespan

    @asynccontextmanager
    async def checked_lifespan(application):
        async with original_lifespan(application):
            endpoints = [
                route.endpoint
                for route in app.routes
                if isinstance(getattr(route, "endpoint", None), StreamableHTTPASGIApp)
            ]
            if len(endpoints) != 1 or endpoints[0].session_manager is None:
                raise RuntimeError("Public MCP session manager unavailable")
            manager = endpoints[0].session_manager
            if not manager.stateless:
                raise RuntimeError("Public MCP must run stateless")
            logger.info("Public MCP session policy: stateless")
            yield

    app.router.lifespan_context = checked_lifespan
    app.add_middleware(ListenStreamShim, require_token=False)
    app.add_middleware(_LenientAccept)
    return app
