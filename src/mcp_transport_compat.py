"""Accept MCP protocol versions newer than the pinned SDK knows.

The pinned MCP SDK answers 400 "Unsupported protocol version" to any
``MCP-Protocol-Version`` header outside its own list. Directory scanners run
ahead of the SDK: Anthropic's Toolbox sent ``2026-07-28`` on 8 Oct 2026 and
was rejected before reaching the server. Protocol revisions are additive for
the methods these clients use (initialize, tools/list, tools/call,
resources/list), so a newer version is rewritten to the latest the SDK
supports and the request proceeds. Older unknown values are left alone.
"""
from __future__ import annotations

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


def listen_stream_bounds() -> tuple[float, float]:
    """Return (ping interval, maximum lifetime) for the stateless listen stream.

    OpenAI's platform reads the listen GET as part of loading a connector's
    details and waits for the response to finish, so a long-lived stream
    stalls its dashboard. The default closes the empty stream after two
    seconds; ``OPENAI_LISTEN_STREAM_MAX_SECONDS`` raises or lowers it.
    """
    import os

    try:
        max_seconds = float(os.environ.get("OPENAI_LISTEN_STREAM_MAX_SECONDS", "2"))
    except ValueError:
        max_seconds = 2.0
    max_seconds = min(LISTEN_STREAM_MAX_SECONDS, max(0.0, max_seconds))
    return min(LISTEN_STREAM_PING_SECONDS, max(0.01, max_seconds / 2)), max_seconds


class ListenStreamShim:
    """Answer the streamable-HTTP listen GET on a stateless MCP app.

    FastMCP serves a stateless server on POST and DELETE only, so a GET gets
    405. OpenAI's platform opens the listen stream (GET, Accept
    text/event-stream) when it checks a connector and treats 405 as
    "MCP configuration unavailable". A stateless server has no
    server-initiated messages, so the shim holds an empty event stream open
    with keep-alive comments until the client goes away or the bound ends.
    Requests without a bearer token fall through to the route (405), so the
    shim never answers an unauthenticated probe.
    """

    def __init__(self, app) -> None:
        self.app = app

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
        if relative.rstrip("/") != "" or not has_token:
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
        ping_seconds, max_seconds = listen_stream_bounds()
        deadline = loop.time() + max_seconds
        try:
            await send({"type": "http.response.body", "body": b": stream open\n\n", "more_body": True})
            while loop.time() < deadline:
                await asyncio.sleep(min(ping_seconds, max(0.0, deadline - loop.time())))
                if loop.time() >= deadline:
                    break
                await send({"type": "http.response.body", "body": b": ping\n\n", "more_body": True})
            await send({"type": "http.response.body", "body": b"", "more_body": False})
        except Exception:  # noqa: BLE001 - the client went away; nothing to clean up
            return
