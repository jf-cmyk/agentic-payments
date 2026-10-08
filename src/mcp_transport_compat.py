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
