"""Turn a connector's bare OAuth 401 into a sign-in hint an agent can act on.

The pinned FastMCP answers every unauthenticated request with
``invalid_token`` and a description that says the token is invalid or
expired, even when no token was sent. The WWW-Authenticate header (which OAuth
clients need for discovery) is kept untouched; only the JSON body gains a
``sign_in`` block and, when no bearer token was sent, an honest description.
"""
from __future__ import annotations

import json
from collections.abc import Callable

from starlette.datastructures import Headers

CLIENT_SETUP = {
    "anthropic": "Add the connector URL as a custom connector in Claude (Settings, Connectors) and sign in with a verified email when prompted.",
    "cursor": "Add the connector URL to Cursor's MCP settings and complete the sign-in it opens.",
    "openai": "Add the connector URL as a connector in ChatGPT (Settings, Connectors) or in Codex and sign in with a verified email when prompted.",
}


def sign_in_hint(
    *,
    connector: str,
    mcp_url: str,
    base_url: str,
    token_sent: bool,
    allowance_label: str,
) -> dict[str, object]:
    base = base_url.rstrip("/")
    path = mcp_url.removeprefix(base).strip("/")
    return {
        "connector": f"{mcp_url.rstrip('/')}/",
        "token_sent": token_sent,
        "how": CLIENT_SETUP.get(connector, CLIENT_SETUP["anthropic"]),
        "flow": (
            "The client completes OAuth from the resource_metadata URL in the "
            "WWW-Authenticate header; no API key is involved."
        ),
        "resource_metadata": f"{base}/.well-known/oauth-protected-resource/{path}",
        "authorization_server_metadata": f"{base}/.well-known/oauth-authorization-server/{path}",
        "auth_guide": f"{base}/auth.md",
        "after_sign_in": (
            f"{allowance_label} free live-data credits every calendar month on the "
            "same tools."
        ),
        "without_sign_in": (
            f"{base}/mcp/server/ serves discovery without an account; live prices "
            "there are paid per call with x402."
        ),
    }


class SignInHintMiddleware:
    """Pure ASGI: rewrite a 401 JSON body, pass everything else through."""

    def __init__(
        self,
        app,
        *,
        connector: str,
        mcp_url: Callable[[], str],
        base_url: str,
        allowance_label: Callable[[], str],
    ) -> None:
        self.app = app
        self.connector = connector
        self.mcp_url = mcp_url
        self.base_url = base_url
        self.allowance_label = allowance_label

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        token_sent = bool(Headers(scope=scope).get("authorization"))
        start: dict | None = None
        chunks: list[bytes] = []

        async def capture(message):
            nonlocal start
            if message["type"] == "http.response.start":
                if message["status"] != 401:
                    await send(message)
                    return
                start = message
                return
            if start is None:
                await send(message)
                return
            chunks.append(message.get("body", b""))
            if message.get("more_body", False):
                return
            await self._send_rewritten(send, start, b"".join(chunks), token_sent)

        await self.app(scope, receive, capture)

    async def _send_rewritten(self, send, start, body: bytes, token_sent: bool):
        try:
            payload = json.loads(body) if body else {}
        except (ValueError, UnicodeDecodeError):
            payload = {"error": "unauthorized", "error_description": body.decode("utf-8", "replace")}
        if not isinstance(payload, dict):
            payload = {"error": "unauthorized", "detail": payload}
        if not token_sent:
            payload["error_description"] = (
                "No bearer token was sent. Sign in through the connector flow "
                "described under sign_in; the token is then sent for you."
            )
        payload["sign_in"] = sign_in_hint(
            connector=self.connector,
            mcp_url=self.mcp_url(),
            base_url=self.base_url,
            token_sent=token_sent,
            allowance_label=self.allowance_label(),
        )
        new_body = json.dumps(payload).encode()
        headers = [
            (name, value)
            for name, value in start["headers"]
            if name.lower() not in {b"content-length", b"content-type"}
        ]
        headers.append((b"content-type", b"application/json"))
        headers.append((b"content-length", str(len(new_body)).encode()))
        await send({"type": "http.response.start", "status": 401, "headers": headers})
        await send({"type": "http.response.body", "body": new_body})
