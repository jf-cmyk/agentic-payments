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
from urllib.parse import parse_qs

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


# ---------------------------------------------------------------------------
# OAuth token endpoint failures
# ---------------------------------------------------------------------------
CLIENT_ID_PREFIX_CHARS = 12
TOKEN_FORM_MAX_BYTES = 16_384


class TokenEndpointDiagnostics:
    """Log why a POST to the connector's /token endpoint failed.

    The server log showed /openai/mcp/token answering 401 in bursts with no
    trace of which grant or client it was (the body is a form with secrets,
    so nothing is logged on success). For a 4xx or 5xx this middleware logs
    the grant type, a prefix of the client id (a public identifier) and the
    OAuth error code from the response. Codes, refresh tokens, verifiers and
    client secrets are never logged.
    """

    def __init__(self, app, *, surface: str) -> None:
        self.app = app
        self.surface = surface

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            await self.app(scope, receive, send)
            return
        relative = scope.get("path", "/")[len(scope.get("root_path", "")):]
        if relative.rstrip("/") != "/token":
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
                if buffered < TOKEN_FORM_MAX_BYTES:
                    chunks.append(chunk[: TOKEN_FORM_MAX_BYTES - buffered])
                    buffered += len(chunk)
                if not message.get("more_body", False):
                    return

        await read_all()

        async def replay():
            if queued:
                return queued.pop(0)
            return await receive()

        status = None
        response_chunks: list[bytes] = []

        async def observe(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            elif message["type"] == "http.response.body" and status and status >= 400:
                response_chunks.append(message.get("body", b"")[:4096])
            await send(message)

        await self.app(scope, replay, observe)
        if status is None or status < 400:
            return
        form = parse_qs(b"".join(chunks).decode("utf-8", "replace"), keep_blank_values=True)
        grant_type = (form.get("grant_type") or ["missing"])[0][:40]
        client_id = (form.get("client_id") or [""])[0]
        error = None
        try:
            parsed = json.loads(b"".join(response_chunks) or b"null")
            if isinstance(parsed, dict):
                error = parsed.get("error")
        except ValueError:
            error = "unparseable"
        logger.warning(
            "OAuth token endpoint %s on %s: grant_type=%s client_id=%s error=%s",
            status,
            self.surface,
            grant_type,
            f"{client_id[:CLIENT_ID_PREFIX_CHARS]}..." if client_id else "missing",
            str(error)[:40] if error is not None else "none",
        )
