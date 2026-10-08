"""Log what an MCP client sent when the transport answers 400.

Directory scanners (OpenAI's plugin review among them) post requests the
pinned SDK rejects with a bare 400, and the only trace we keep is the status
code. This pure-ASGI middleware buffers a bounded prefix of the request body
and, only when the response status is 400, logs the method, path, the
JSON-RPC method if the body parses, and a short excerpt. It never logs
headers, so bearer tokens stay out of the log.
"""
from __future__ import annotations

import json
import logging

logger = logging.getLogger(__name__)

BODY_EXCERPT_BYTES = 600
MAX_BUFFERED_BYTES = 65_536


class BadRequestDiagnostics:
    def __init__(self, app, *, surface: str) -> None:
        self.app = app
        self.surface = surface

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] not in {"POST", "PUT"}:
            await self.app(scope, receive, send)
            return

        chunks: list[bytes] = []
        buffered = 0
        queued: list[dict] = []

        async def read_all():
            nonlocal buffered
            while True:
                message = await receive()
                queued.append(message)
                if message["type"] != "http.request":
                    return
                chunk = message.get("body", b"")
                if buffered < MAX_BUFFERED_BYTES:
                    chunks.append(chunk[: MAX_BUFFERED_BYTES - buffered])
                    buffered += len(chunk)
                if not message.get("more_body", False):
                    return

        await read_all()

        async def replay():
            if queued:
                return queued.pop(0)
            return await receive()

        status = None

        async def observe(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        await self.app(scope, replay, observe)
        if status != 400:
            return
        body = b"".join(chunks)
        rpc_method = None
        rpc_version = None
        try:
            parsed = json.loads(body) if body else None
            if isinstance(parsed, dict):
                rpc_method = parsed.get("method")
                params = parsed.get("params")
                if isinstance(params, dict):
                    rpc_version = params.get("protocolVersion")
            elif isinstance(parsed, list):
                rpc_method = f"batch[{len(parsed)}]"
        except ValueError:
            rpc_method = "unparseable"
        protocol_header = None
        for name, value in scope.get("headers") or []:
            if name == b"mcp-protocol-version":
                protocol_header = value.decode("latin-1", "replace")[:40]
        logger.warning(
            "MCP transport 400 on %s %s (%s): rpc_method=%s protocolVersion=%s header_version=%s body=%r",
            scope["method"],
            scope.get("path"),
            self.surface,
            rpc_method,
            rpc_version,
            protocol_header,
            body[:BODY_EXCERPT_BYTES].decode("utf-8", "replace"),
        )
