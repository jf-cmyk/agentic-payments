"""Bound the lifetime of abandoned public discovery sessions."""

from __future__ import annotations

import json
import logging
import math
from contextlib import asynccontextmanager

from fastmcp import FastMCP
from fastmcp.server.http import StarletteWithLifespan, StreamableHTTPASGIApp
from starlette.datastructures import Headers

PUBLIC_SESSION_IDLE_SECONDS = 30 * 60
SESSION_HEADER = "mcp-session-id"
JSON_TYPE = "application/json"
SSE_TYPE = "text/event-stream"
MAX_INSPECTED_BODY_BYTES = 1_048_576
JSONRPC_INVALID_REQUEST = -32600
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


def session_required_error(
    request_id: object,
    *,
    idle_seconds: float,
    quickstart_url: str | None,
) -> dict[str, object]:
    """Explain the SDK's 'Missing session ID' in a way a client can act on."""
    data: dict[str, object] = {
        "error_code": "MCP_SESSION_REQUIRED",
        "how_to_fix": [
            "POST a JSON-RPC 'initialize' request first; the response carries an "
            f"'{SESSION_HEADER}' header.",
            f"Send that '{SESSION_HEADER}' header on every later request, including "
            "the GET that opens the event stream.",
            f"Sessions close after {int(idle_seconds)} seconds without a request; "
            "initialize again after that.",
        ],
        "initialize_example": {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "your-client", "version": "1.0"},
            },
        },
        "accept_header": f"{JSON_TYPE}, {SSE_TYPE}",
    }
    if quickstart_url:
        data["quickstart"] = quickstart_url
    return {
        "jsonrpc": "2.0",
        "id": request_id if request_id is not None else "server-error",
        "error": {
            "code": JSONRPC_INVALID_REQUEST,
            "message": "Bad Request: Missing session ID",
            "data": data,
        },
    }


class _ClientFriendlyTransport:
    """Accept what MCP clients send, and explain the session rule when it is missed.

    Pure ASGI, applied only to the public discovery app. It rewrites the Accept
    header before the SDK's check and answers a session-less non-initialize
    request itself with the SDK's status and message plus actionable data.
    """

    def __init__(self, app, *, idle_seconds: float, quickstart_url: str | None):
        self.app = app
        self.idle_seconds = idle_seconds
        self.quickstart_url = quickstart_url

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        accept = normalised_accept(headers.get("accept"))
        if accept is not None:
            scope = dict(scope)
            scope["headers"] = [
                (name, value) for name, value in scope["headers"] if name != b"accept"
            ] + [(b"accept", accept.encode("latin-1"))]
        method = scope["method"]
        if headers.get(SESSION_HEADER) or method not in {"GET", "POST", "DELETE"}:
            await self.app(scope, receive, send)
            return
        if method != "POST":
            await self._send_session_error(send, None)
            return
        body, replay = await self._read_body(receive)
        request_id = None
        if body is not None:
            try:
                message = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                message = None
            if isinstance(message, dict) and message.get("method") != "initialize":
                request_id = message.get("id")
                await self._send_session_error(send, request_id)
                return
        await self.app(scope, replay, send)

    @staticmethod
    async def _read_body(receive):
        """Buffer a bounded request body and return a receive that replays it."""
        chunks: list[bytes] = []
        more = True
        size = 0
        messages = []
        while more:
            message = await receive()
            messages.append(message)
            if message["type"] != "http.request":
                break
            chunk = message.get("body", b"")
            size += len(chunk)
            chunks.append(chunk)
            more = message.get("more_body", False)
            if size > MAX_INSPECTED_BODY_BYTES:
                break
        queued = list(messages)

        async def replay():
            if queued:
                return queued.pop(0)
            return await receive()

        body = b"".join(chunks) if size <= MAX_INSPECTED_BODY_BYTES else None
        return body, replay

    async def _send_session_error(self, send, request_id):
        payload = session_required_error(
            request_id,
            idle_seconds=self.idle_seconds,
            quickstart_url=self.quickstart_url,
        )
        body = json.dumps(payload).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 400,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode()),
                    (b"cache-control", b"no-store"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


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
    server: FastMCP,
    *,
    idle_seconds: float = PUBLIC_SESSION_IDLE_SECONDS,
    quickstart_url: str | None = None,
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
    app.add_middleware(
        _ClientFriendlyTransport, idle_seconds=idle_seconds, quickstart_url=quickstart_url
    )
    return app
