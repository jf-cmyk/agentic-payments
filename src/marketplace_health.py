"""Public, credential-free health checks for Blocksize distribution listings.

These checks prove only that a listing URL can be reached. They deliberately do
not claim marketplace views, installs, hosted calls, users, or revenue.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any, Iterable

import httpx


async def probe_listing(
    client: httpx.AsyncClient,
    *,
    platform_id: str,
    listing_url: str,
) -> dict[str, Any]:
    """Return bounded listing-health metadata without retaining response bodies."""
    started = time.monotonic()
    checked_at = datetime.now(UTC).isoformat()
    try:
        response = await client.get(listing_url)
    except httpx.HTTPError as exc:
        return {
            "platform_id": platform_id,
            "source_url": listing_url,
            "status": "unreachable",
            "metrics": {
                "metric_scope": "listing_health",
                "checked_at": checked_at,
                "reachable": False,
                "healthy": False,
                "latency_ms": round((time.monotonic() - started) * 1000, 2),
                "error_type": type(exc).__name__,
                "measurement_note": "Public URL reachability only; not marketplace demand or revenue.",
            },
        }

    latency_ms = round((time.monotonic() - started) * 1000, 2)
    healthy = 200 <= response.status_code < 400
    return {
        "platform_id": platform_id,
        "source_url": listing_url,
        "status": "healthy" if healthy else "degraded",
        "metrics": {
            "metric_scope": "listing_health",
            "checked_at": checked_at,
            "reachable": True,
            "healthy": healthy,
            "http_status": response.status_code,
            "latency_ms": latency_ms,
            "content_type": response.headers.get("content-type", "").split(";", 1)[0][:100],
            "measurement_note": "Public URL reachability only; not marketplace demand or revenue.",
        },
    }


async def collect_listing_health(
    platforms: Iterable[dict[str, Any]],
    *,
    timeout_seconds: float = 10.0,
) -> list[dict[str, Any]]:
    """Probe the fixed distribution catalog concurrently with a bounded timeout."""
    targets = [
        (str(platform.get("id") or ""), str(platform.get("listing_url") or ""))
        for platform in platforms
        if platform.get("id") and platform.get("listing_url")
    ]
    async with httpx.AsyncClient(
        timeout=httpx.Timeout(timeout_seconds),
        follow_redirects=True,
        headers={"User-Agent": "Blocksize-Listing-Health/1.0"},
    ) as client:
        return list(
            await asyncio.gather(
                *(
                    probe_listing(client, platform_id=platform_id, listing_url=listing_url)
                    for platform_id, listing_url in targets
                )
            )
        )
