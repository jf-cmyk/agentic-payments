from __future__ import annotations

import httpx

from src.marketplace_health import probe_listing


async def test_probe_listing_labels_health_without_claiming_demand_metrics():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers={"content-type": "text/html; charset=utf-8"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await probe_listing(
            client,
            platform_id="pay_sh",
            listing_url="https://pay.sh/services/blocksize/market-data",
        )

    assert result["status"] == "healthy"
    assert result["metrics"]["metric_scope"] == "listing_health"
    assert result["metrics"]["healthy"] is True
    assert result["metrics"]["http_status"] == 200
    assert "views" not in result["metrics"]
    assert "installs" not in result["metrics"]


async def test_probe_listing_records_bounded_failure_metadata():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("unavailable", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        result = await probe_listing(
            client,
            platform_id="smithery",
            listing_url="https://smithery.ai/server/blocksize",
        )

    assert result["status"] == "unreachable"
    assert result["metrics"]["reachable"] is False
    assert result["metrics"]["error_type"] == "ConnectError"
    assert "unavailable" not in str(result)
