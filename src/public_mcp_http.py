"""Bound the lifetime of abandoned public discovery sessions."""

from __future__ import annotations

import logging
import math
from contextlib import asynccontextmanager

from fastmcp import FastMCP
from fastmcp.server.http import StarletteWithLifespan, StreamableHTTPASGIApp
from starlette.datastructures import Headers

PUBLIC_SESSION_IDLE_SECONDS = 30 * 60
logger = logging.getLogger(__name__)


class _ClosedSessionCleanup:
    """Remove successful DELETE tombstones retained by pinned mcp 1.28.1.

    The SDK terminates transport streams but retains these two maps. Only prune
    after its validated successful response, never based on a claimed ID alone.
    Integration tests protect this narrow dependency on the pinned SDK internals.
    """

    def __init__(self, app, *, public_app):
        self.app = app
        self.public_app = public_app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "DELETE":
            await self.app(scope, receive, send)
            return
        status = None

        async def track_response(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        await self.app(scope, receive, track_response)
        if status != 200:
            return
        session_id = Headers(scope=scope).get("mcp-session-id")
        manager = self.public_app.state.public_session_manager
        transport = manager._server_instances.get(session_id)
        if transport is not None and transport.is_terminated:
            manager._server_instances.pop(session_id, None)
            manager._session_owners.pop(session_id, None)


def create_public_http_app(
    server: FastMCP, *, idle_seconds: float = PUBLIC_SESSION_IDLE_SECONDS
) -> StarletteWithLifespan:
    """Use the pinned MCP SDK's cleanup; do not touch authenticated connectors.

    FastMCP creates its manager during lifespan entry, not app construction.
    Configure it before yielding readiness, so every new stateful session has
    the SDK's cancellation deadline. Stateless mode needs no session cleanup.
    """
    if not math.isfinite(idle_seconds) or idle_seconds <= 0:
        raise ValueError("Public MCP idle timeout must be finite and positive")
    app = server.http_app(path="/", transport="streamable-http")
    original_lifespan = app.lifespan

    @asynccontextmanager
    async def bounded_lifespan(application):
        async with original_lifespan(application):
            endpoints = [
                route.endpoint
                for route in app.routes
                if isinstance(getattr(route, "endpoint", None), StreamableHTTPASGIApp)
            ]
            if len(endpoints) != 1 or endpoints[0].session_manager is None:
                raise RuntimeError("Public MCP session manager unavailable for idle cleanup")
            manager = endpoints[0].session_manager
            application.state.public_session_manager = manager
            if not manager.stateless:
                manager.session_idle_timeout = idle_seconds
            logger.info(
                "Public MCP session policy: stateless=%s idle_seconds=%s",
                manager.stateless,
                manager.session_idle_timeout,
            )
            yield

    app.router.lifespan_context = bounded_lifespan
    app.add_middleware(_ClosedSessionCleanup, public_app=app)
    return app
