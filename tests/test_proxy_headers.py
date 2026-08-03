"""Acceptance coverage for the trusted reverse-proxy boundary."""

from __future__ import annotations

from typing import Any

import pytest

from src import resource_server
from src.proxy_headers import TrustedProxyHeadersMiddleware


async def _captured_scope(
    *,
    peer: str,
    headers: list[tuple[bytes, bytes]],
    trusted_proxy_ips: str,
    scope_type: str = "http",
    scheme: str = "http",
    use_x_real_ip: bool = False,
) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    async def app(scope, _receive, _send):
        captured.update(scope)

    async def receive():
        return {"type": "http.disconnect"}

    async def send(_message):
        return None

    middleware = TrustedProxyHeadersMiddleware(
        app,
        trusted_proxy_ips=trusted_proxy_ips,
        use_x_real_ip=use_x_real_ip,
    )
    scope = {
        "type": scope_type,
        "scheme": scheme,
        "client": (peer, 43210),
        "headers": headers,
    }
    await middleware(scope, receive, send)
    return captured


@pytest.mark.asyncio
async def test_trusted_railway_peer_prefers_one_valid_real_ip() -> None:
    scope = await _captured_scope(
        peer="100.64.0.2",
        trusted_proxy_ips="100.64.0.0/10",
        headers=[
            (b"x-real-ip", b"203.0.113.10"),
            (b"x-forwarded-for", b"192.0.2.66, 198.51.100.20"),
            (b"x-forwarded-proto", b"https"),
        ],
        use_x_real_ip=True,
    )

    assert scope["client"] == ("203.0.113.10", 0)
    assert scope["scheme"] == "https"


@pytest.mark.asyncio
async def test_untrusted_peer_cannot_spoof_forwarded_identity_or_scheme() -> None:
    scope = await _captured_scope(
        peer="198.51.100.7",
        trusted_proxy_ips="100.64.0.0/10",
        headers=[
            (b"x-real-ip", b"203.0.113.10"),
            (b"x-forwarded-for", b"203.0.113.11"),
            (b"x-forwarded-proto", b"https"),
        ],
        use_x_real_ip=True,
    )

    assert scope["client"] == ("198.51.100.7", 43210)
    assert scope["scheme"] == "http"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "headers",
    [
        [
            (b"x-real-ip", b"203.0.113.10"),
            (b"x-real-ip", b"203.0.113.11"),
            (b"x-forwarded-for", b"198.51.100.20"),
        ],
        [
            (b"x-real-ip", b"not-an-ip"),
            (b"x-forwarded-for", b"198.51.100.20"),
        ],
        [
            (b"x-forwarded-for", b"198.51.100.20"),
            (b"x-forwarded-for", b"198.51.100.21"),
        ],
        [(b"x-forwarded-for", b"198.51.100.20, not-an-ip")],
    ],
)
async def test_duplicate_or_malformed_identity_headers_fail_closed(
    headers: list[tuple[bytes, bytes]],
) -> None:
    scope = await _captured_scope(
        peer="100.64.0.2",
        trusted_proxy_ips="100.64.0.0/10",
        headers=headers,
        use_x_real_ip=True,
    )

    assert scope["client"] == ("100.64.0.2", 43210)


@pytest.mark.asyncio
async def test_railway_peer_does_not_fall_back_when_real_ip_is_missing() -> None:
    scope = await _captured_scope(
        peer="100.64.0.2",
        trusted_proxy_ips="100.64.0.0/10",
        headers=[(b"x-forwarded-for", b"203.0.113.99")],
        use_x_real_ip=True,
    )

    assert scope["client"] == ("100.64.0.2", 43210)


@pytest.mark.asyncio
async def test_forwarded_scheme_accepts_only_one_valid_value() -> None:
    accepted = await _captured_scope(
        peer="100.64.0.2",
        trusted_proxy_ips="100.64.0.0/10",
        headers=[(b"x-forwarded-proto", b"https")],
    )
    malformed = await _captured_scope(
        peer="100.64.0.2",
        trusted_proxy_ips="100.64.0.0/10",
        headers=[(b"x-forwarded-proto", b"javascript")],
    )
    duplicate = await _captured_scope(
        peer="100.64.0.2",
        trusted_proxy_ips="100.64.0.0/10",
        headers=[
            (b"x-forwarded-proto", b"https"),
            (b"x-forwarded-proto", b"http"),
        ],
    )

    assert accepted["scheme"] == "https"
    assert malformed["scheme"] == "http"
    assert duplicate["scheme"] == "http"


@pytest.mark.asyncio
async def test_valid_xff_fallback_selects_nearest_untrusted_hop() -> None:
    scope = await _captured_scope(
        peer="10.0.0.2",
        trusted_proxy_ips="10.0.0.0/8",
        headers=[
            (
                b"x-forwarded-for",
                b"192.0.2.66, 203.0.113.20, 10.0.0.3",
            )
        ],
    )

    assert scope["client"] == ("203.0.113.20", 0)


@pytest.mark.asyncio
async def test_non_railway_proxy_ignores_spoofed_real_ip_and_uses_xff() -> None:
    scope = await _captured_scope(
        peer="10.0.0.2",
        trusted_proxy_ips="10.0.0.0/8",
        headers=[
            (b"x-real-ip", b"192.0.2.200"),
            (b"x-forwarded-for", b"203.0.113.20, 10.0.0.3"),
        ],
    )

    assert scope["client"] == ("203.0.113.20", 0)


def test_proxy_middleware_is_the_outermost_fastapi_boundary() -> None:
    configured = resource_server.app.user_middleware[0]

    assert configured.cls is TrustedProxyHeadersMiddleware
    assert configured.kwargs == {
        "trusted_proxy_ips": resource_server.settings.server.forwarded_allow_ips,
        "use_x_real_ip": resource_server._hosted_environment(),
    }


def test_resource_server_disables_uvicorn_proxy_header_rewrite(monkeypatch) -> None:
    import uvicorn

    captured: dict[str, Any] = {}

    class FakeConfig:
        def __init__(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs

    class FakeServer:
        def __init__(self, config):
            captured["config"] = config

        def run(self):
            captured["ran"] = True

    monkeypatch.setattr(uvicorn, "Config", FakeConfig)
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    monkeypatch.setattr(resource_server, "install_sensitive_query_log_filter", lambda: None)

    resource_server.run_resource_server()

    assert captured["kwargs"]["proxy_headers"] is False
    assert "forwarded_allow_ips" not in captured["kwargs"]
    assert captured["ran"] is True
