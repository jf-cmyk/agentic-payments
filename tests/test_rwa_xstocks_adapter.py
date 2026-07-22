from __future__ import annotations

import httpx
import pytest

from src.rwa_adapters import EVMPoolStateAdapter, RWAAdapterBlockedError, XStocksPublicPriceAdapter


@pytest.mark.asyncio
async def test_xstocks_public_adapter_returns_reference_price_and_raw_payload() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v2/public/assets/TSLAx":
            return httpx.Response(
                200,
                json={
                    "symbol": "TSLAx",
                    "underlyingSymbol": "TSLA",
                    "isin": "CH1436219252",
                    "underlyingIsin": "US88160R1014",
                    "isTradingHalted": False,
                    "deployments": [{"network": "Solana", "address": "XsToken"}],
                },
            )
        if request.url.path == "/api/v2/public/assets/TSLAx/price-data":
            return httpx.Response(200, json={"quote": 390.075}, headers={"Date": "Thu, 16 Jul 2026 22:00:00 GMT"})
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        adapter = XStocksPublicPriceAdapter(client=client)
        row = await adapter.fetch_bidask("TSLAx/USD")

    assert row["source_type"] == "issuer_reference_price"
    assert row["price"] == 390.075
    assert row["timestamp"] is None
    assert "bid" not in row and "ask" not in row
    assert row["metadata"]["raw_payload"]["price_data"] == {"quote": 390.075}
    assert row["metadata"]["reference_only_exception"]


@pytest.mark.asyncio
async def test_xstocks_public_adapter_does_not_invent_order_book_depth() -> None:
    adapter = XStocksPublicPriceAdapter()
    with pytest.raises(RWAAdapterBlockedError, match="not an order book"):
        await adapter.fetch_order_book("TSLAx/USD")


def test_evm_pool_adapter_rejects_uniswap_v4_pool_id_as_contract() -> None:
    with pytest.raises(RWAAdapterBlockedError, match="not a pool contract address"):
        EVMPoolStateAdapter._pool_contract(
            {
                "symbol": "DGLD/USDC",
                "pool_id": "0x" + "12" * 32,
            }
        )
