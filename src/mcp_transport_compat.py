

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
