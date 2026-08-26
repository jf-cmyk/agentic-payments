"""Spoof-resistant ASGI handling for explicitly trusted reverse proxies."""

from __future__ import annotations

import ipaddress
from collections.abc import Awaitable, Callable, Sequence
from typing import Any


ASGIReceive = Callable[[], Awaitable[dict[str, Any]]]
ASGISend = Callable[[dict[str, Any]], Awaitable[None]]
ASGIApp = Callable[[dict[str, Any], ASGIReceive, ASGISend], Awaitable[None]]
IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
IPNetwork = ipaddress.IPv4Network | ipaddress.IPv6Network

_MAX_FORWARDED_HOPS = 32


def parse_trusted_proxy_networks(value: Sequence[str] | str | None) -> tuple[IPNetwork, ...]:
    """Parse a bounded IP/CIDR allowlist, dropping unsafe or malformed entries."""
    if value is None:
        return ()
    entries = value.split(",") if isinstance(value, str) else value
    networks: list[IPNetwork] = []
    for raw_entry in entries:
        entry = str(raw_entry).strip()
        if not entry or entry == "*":
            continue
        try:
            network = ipaddress.ip_network(entry, strict=False)
        except ValueError:
            continue
        if network.prefixlen == 0:
            continue
        networks.append(network)
    return tuple(networks)


def _address_is_trusted(address: IPAddress, networks: tuple[IPNetwork, ...]) -> bool:
    return any(address.version == network.version and address in network for network in networks)


def _header_values(scope: dict[str, Any], name: bytes) -> list[str] | None:
    values: list[str] = []
    for raw_name, raw_value in scope.get("headers", ()):  # ASGI headers preserve duplicates.
        if bytes(raw_name).lower() != name:
            continue
        try:
            values.append(bytes(raw_value).decode("latin-1").strip())
        except (AttributeError, UnicodeDecodeError, ValueError):
            return None
    return values


def _single_valid_ip_header(scope: dict[str, Any], name: bytes) -> tuple[bool, str | None]:
    """Return (present, canonical IP); a present invalid/duplicate header fails closed."""
    values = _header_values(scope, name)
    if values is None:
        return True, None
    if not values:
        return False, None
    if len(values) != 1 or not values[0]:
        return True, None
    try:
        return True, str(ipaddress.ip_address(values[0]))
    except ValueError:
        return True, None


def _forwarded_for_client(
    scope: dict[str, Any],
    trusted_networks: tuple[IPNetwork, ...],
) -> str | None:
    """Select the nearest untrusted XFF hop, but only from one fully valid chain."""
    values = _header_values(scope, b"x-forwarded-for")
    if values is None or len(values) != 1:
        return None
    raw_hops = [item.strip() for item in values[0].split(",")]
    if not raw_hops or len(raw_hops) > _MAX_FORWARDED_HOPS or any(not item for item in raw_hops):
        return None
    try:
        hops = [ipaddress.ip_address(item) for item in raw_hops]
    except ValueError:
        return None

    for hop in reversed(hops):
        if not _address_is_trusted(hop, trusted_networks):
            return str(hop)
    return str(hops[0])


def _forwarded_scheme(scope: dict[str, Any]) -> str | None:
    values = _header_values(scope, b"x-forwarded-proto")
    if values is None or len(values) != 1:
        return None
    forwarded = values[0].lower()
    if scope.get("type") == "websocket":
        return {
            "http": "ws",
            "https": "wss",
            "ws": "ws",
            "wss": "wss",
        }.get(forwarded)
    return forwarded if forwarded in {"http", "https"} else None


class TrustedProxyHeadersMiddleware:
    """Apply proxy identity only when the raw TCP peer is explicitly trusted.

    Railway documents ``X-Real-IP`` as its client-IP header. In Railway mode one
    valid value is required; other trusted proxies use a single, fully valid
    ``X-Forwarded-For`` chain and never trust a caller-controlled X-Real-IP.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        trusted_proxy_ips: Sequence[str] | str | None,
        use_x_real_ip: bool = False,
    ) -> None:
        self.app = app
        self.trusted_networks = parse_trusted_proxy_networks(trusted_proxy_ips)
        self.use_x_real_ip = use_x_real_ip

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: ASGIReceive,
        send: ASGISend,
    ) -> None:
        if scope.get("type") not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        raw_client = scope.get("client")
        raw_host = raw_client[0] if raw_client else None
        try:
            peer = ipaddress.ip_address(raw_host) if raw_host else None
        except ValueError:
            peer = None
        if peer is None or not _address_is_trusted(peer, self.trusted_networks):
            await self.app(scope, receive, send)
            return

        if self.use_x_real_ip:
            _, real_ip = _single_valid_ip_header(scope, b"x-real-ip")
            if real_ip is not None:
                scope["client"] = (real_ip, 0)
        else:
            forwarded_client = _forwarded_for_client(scope, self.trusted_networks)
            if forwarded_client is not None:
                scope["client"] = (forwarded_client, 0)

        scheme = _forwarded_scheme(scope)
        if scheme is not None:
            scope["scheme"] = scheme

        await self.app(scope, receive, send)


__all__ = [
    "TrustedProxyHeadersMiddleware",
    "parse_trusted_proxy_networks",
]
