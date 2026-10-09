"""Shims between current MCP clients and the pinned SDK.

The pinned MCP SDK answers 400 "Unsupported protocol version" to any
``MCP-Protocol-Version`` header outside its own list. Directory scanners run
ahead of the SDK: Anthropic's Toolbox sent ``2026-07-28`` on 8 Oct 2026 and
was rejected before reaching the server. Protocol revisions are additive for
the methods these clients use (initialize, tools/list, tools/call,
resources/list), so a newer version is rewritten to the latest the SDK
supports and the request proceeds. Older unknown values are left alone.
"""
from __future__ import annotations

import json
import logging

from mcp.shared.version import SUPPORTED_PROTOCOL_VERSIONS
from mcp.types import LATEST_PROTOCOL_VERSION

logger = logging.getLogger(__name__)

HEADER = b"mcp-protocol-version"


def compatible_protocol_version(value: str | None) -> str | None:
    """Return the header value the SDK should see, or None to leave it unchanged."""
    if not value:
        return None
    requested = value.strip()
    if requested in SUPPORTED_PROTOCOL_VERSIONS:
        return None
    # Versions are ISO dates, so string order is chronological.
    if requested > LATEST_PROTOCOL_VERSION:
        return LATEST_PROTOCOL_VERSION
    return None


class ProtocolVersionCompat:
    """Pure ASGI: rewrite a newer-than-supported protocol version header."""

    def __init__(self, app, *, surface: str) -> None:
        self.app = app
        self.surface = surface

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = scope.get("headers") or []
            for index, (name, raw) in enumerate(headers):
                if name != HEADER:
                    continue
                replacement = compatible_protocol_version(raw.decode("latin-1", "replace"))
                if replacement is not None:
                    logger.info(
                        "MCP protocol version %s on %s mapped to %s",
                        raw.decode("latin-1", "replace")[:40],
                        self.surface,
                        replacement,
                    )
                    scope = dict(scope)
                    rewritten = list(headers)
                    rewritten[index] = (HEADER, replacement.encode("latin-1"))
                    scope["headers"] = rewritten
                break
        await self.app(scope, receive, send)


# ---------------------------------------------------------------------------
# Listen stream for stateless connectors
# ---------------------------------------------------------------------------
LISTEN_STREAM_PING_SECONDS = 15.0
LISTEN_STREAM_MAX_SECONDS = 300.0


class ListenStreamShim:
    """Answer the streamable-HTTP listen GET on a stateless MCP app.

    FastMCP serves a stateless server on POST and DELETE only, so a GET gets
    405. OpenAI's platform opens the listen stream (GET, Accept
    text/event-stream) when it checks a connector and treats 405 as
    "MCP configuration unavailable". A stateless server has no
    server-initiated messages, so the shim holds an empty event stream open
    with keep-alive comments until the client goes away or the bound ends.
    On an authenticated connector, requests without a bearer token fall
    through to the route (405), so the shim never answers an unauthenticated
    probe; the public discovery server has no tokens and passes
    ``require_token=False``.
    """

    def __init__(self, app, *, require_token: bool = True) -> None:
        self.app = app
        self.require_token = require_token

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "GET":
            await self.app(scope, receive, send)
            return
        # Starlette keeps the full path on a mounted app; strip the mount prefix.
        relative = scope.get("path", "/")[len(scope.get("root_path", "")):]
        has_token = any(
            name == b"authorization" and value.strip()
            for name, value in scope.get("headers") or []
        )
        if relative.rstrip("/") != "" or (self.require_token and not has_token):
            await self.app(scope, receive, send)
            return
        import asyncio

        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"text/event-stream"),
                    (b"cache-control", b"no-store"),
                    (b"x-mcp-listen-stream", b"stateless"),
                ],
            }
        )
        loop = asyncio.get_event_loop()
        deadline = loop.time() + LISTEN_STREAM_MAX_SECONDS
        try:
            await send({"type": "http.response.body", "body": b": stream open\n\n", "more_body": True})
            while loop.time() < deadline:
                await asyncio.sleep(min(LISTEN_STREAM_PING_SECONDS, max(0.0, deadline - loop.time())))
                if loop.time() >= deadline:
                    break
                await send({"type": "http.response.body", "body": b": ping\n\n", "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        except Exception:  # noqa: BLE001 - the client went away; nothing to clean up
            return


# ---------------------------------------------------------------------------
# server/discover for clients on the 2026-07-28 revision
# ---------------------------------------------------------------------------
MODERN_PROTOCOL_VERSION = "2026-07-28"
DISCOVER_METHOD = "server/discover"
DISCOVER_MAX_BODY_BYTES = 65_536
SERVER_INFO_META = "io.modelcontextprotocol/serverInfo"
CLIENT_INFO_META = "io.modelcontextprotocol/clientInfo"


def discover_result(server, *, modern: bool) -> dict:
    """Build the DiscoverResult (MCP 2026-07-28) for a FastMCP server.

    The revision retired the initialize handshake and made ``server/discover``
    mandatory: it returns the protocol versions the server supports, its
    capabilities, identity and instructions. The pinned SDK knows nothing of
    it, so the result is assembled from the same objects the SDK uses to
    answer ``initialize``. ``modern`` adds 2026-07-28 to the advertised
    versions; only a stateless app can honour that, because modern clients
    send every request without initialize or a session.
    """
    from mcp.server.lowlevel import NotificationOptions

    from src.public_metadata import APP_VERSION

    low = server._mcp_server
    capabilities = low.get_capabilities(NotificationOptions(), {}).model_dump(exclude_none=True)
    versions = list(SUPPORTED_PROTOCOL_VERSIONS)
    if modern:
        versions.append(MODERN_PROTOCOL_VERSION)
    result: dict = {
        "resultType": "complete",
        "supportedVersions": sorted(set(versions), reverse=True),
        "capabilities": capabilities,
        "_meta": {
            SERVER_INFO_META: {
                "name": low.name,
                "version": getattr(server, "version", None) or APP_VERSION,
            }
        },
        "ttlMs": 3_600_000,
        "cacheScope": "public",
    }
    instructions = getattr(low, "instructions", None) or getattr(server, "instructions", None)
    if instructions:
        result["instructions"] = instructions
    return result


async def _buffer_request_body(receive, limit: int):
    """Read a bounded request body; return (body or None if over limit, replay)."""
    queued: list[dict] = []
    chunks: list[bytes] = []
    size = 0
    while True:
        message = await receive()
        queued.append(message)
        if message["type"] != "http.request":
            break
        chunk = message.get("body", b"")
        size += len(chunk)
        if size <= limit:
            chunks.append(chunk)
        if not message.get("more_body", False):
            break

    async def replay():
        if queued:
            return queued.pop(0)
        return await receive()

    return (b"".join(chunks) if size <= limit else None), replay


class DiscoverShim:
    """Answer ``server/discover`` on an MCP app built with the pinned SDK.

    Directory scanners and current clients (OpenAI's plugin scanner, Claude
    Code 2.1) open with ``server/discover`` and only fall back to the legacy
    handshake when it fails; the pinned SDK answers "Method not found" (or,
    on a stateful app without a session, a transport 400). The shim answers
    a POST to the MCP endpoint whose body is a ``server/discover`` request
    with a DiscoverResult and replays everything else to the app unchanged.
    The result carries only what ``initialize`` already returns to any
    client, so it is served before authentication, like the OAuth metadata.
    """

    def __init__(self, app, *, server, surface: str, modern: bool = False) -> None:
        self.app = app
        self.server = server
        self.surface = surface
        self.modern = modern

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        relative = scope.get("path", "/")[len(scope.get("root_path", "")):]
        if relative.rstrip("/") != "":
            await self.app(scope, receive, send)
            return
        body, replay = await _buffer_request_body(receive, DISCOVER_MAX_BODY_BYTES)
        message = None
        if body:
            try:
                message = json.loads(body)
            except (ValueError, UnicodeDecodeError):
                message = None
        if (
            not isinstance(message, dict)
            or message.get("method") != DISCOVER_METHOD
            or "id" not in message
        ):
            await self.app(scope, replay, send)
            return
        params = message.get("params")
        client = {}
        if isinstance(params, dict) and isinstance(params.get("_meta"), dict):
            client = params["_meta"].get(CLIENT_INFO_META) or {}
        logger.info(
            "server/discover answered on %s for client %s",
            self.surface,
            str(client.get("name", "unknown"))[:40] if isinstance(client, dict) else "unknown",
        )
        payload = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": discover_result(self.server, modern=self.modern),
            }
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode()),
                    (b"cache-control", b"no-store"),
                    (b"x-mcp-discover", b"shim"),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})
