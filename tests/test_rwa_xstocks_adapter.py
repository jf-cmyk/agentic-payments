from __future__ import annotations

import httpx
import pytest
from unittest.mock import AsyncMock

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


def test_evm_pool_adapter_uses_configured_rpc_fallbacks_before_public(monkeypatch) -> None:
    monkeypatch.setenv("EVM_RPC_BASE_URL", "https://primary.example")
    monkeypatch.setenv(
        "EVM_RPC_BASE_URLS",
        "https://fallback-one.example, https://fallback-two.example,https://primary.example",
    )

    adapter = EVMPoolStateAdapter(venue_id="aerodrome_slipstream")
    candidates = adapter._rpc_candidates("base")

    assert candidates[:3] == [
        ("env:EVM_RPC_BASE_URL", "https://primary.example"),
        ("env:EVM_RPC_BASE_URLS[1]", "https://fallback-one.example"),
        ("env:EVM_RPC_BASE_URLS[2]", "https://fallback-two.example"),
    ]
    assert candidates[-2:] == [
        ("public_fallback:https://mainnet.base.org", "https://mainnet.base.org"),
        ("public_fallback:https://base-rpc.publicnode.com", "https://base-rpc.publicnode.com"),
    ]


def _swap_log(block_number: int, log_index: int = 0) -> dict[str, str | list[str]]:
    return {
        "blockNumber": hex(block_number),
        "transactionHash": f"0x{block_number:064x}",
        "logIndex": hex(log_index),
        "topics": ["0xtopic"],
        "data": "0x",
    }


@pytest.mark.asyncio
async def test_evm_swap_cache_bootstraps_provider_limited_window_and_extends_it(tmp_path) -> None:
    adapter = EVMPoolStateAdapter(
        venue_id="uniswap_v3_v4",
        swap_cache_dir=tmp_path,
    )
    adapter._start_block_for_window = AsyncMock(
        return_value=(100, 0, 2_000, "env:EVM_RPC_ETHEREUM_URL")
    )
    adapter._block_timestamp = AsyncMock(
        side_effect=lambda chain, block: (
            2_000 + (block - 1_000) * 12,
            "env:EVM_RPC_ETHEREUM_URL",
        )
    )
    calls = []

    async def first_swap_logs(**kwargs):
        calls.append(kwargs)
        if kwargs.get("chunk_size") is None:
            raise RWAAdapterBlockedError(
                "evm_rpc_and_pool_state",
                "eth_getLogs is limited to a 5 range",
            )
        return [_swap_log(kwargs["end_block"])], ["env:EVM_RPC_ETHEREUM_URL"]

    adapter._swap_logs = first_swap_logs
    logs, window, _ = await adapter._collect_swap_log_window(
        chain="ethereum",
        contract="0x" + "1" * 40,
        end_block=1_000,
        lookback_seconds=86_400,
    )

    assert calls[-1]["start_block"] == 850
    assert calls[-1]["chunk_size"] == 5
    assert logs[0]["blockNumber"] == hex(1_000)
    assert window["status"] == "collecting"
    assert window["window_coverage_seconds"] == 1_800
    assert window["provider_chunk_size"] == 5
    assert window["cache_persisted"] is True

    reloaded = EVMPoolStateAdapter(
        venue_id="uniswap_v3_v4",
        swap_cache_dir=tmp_path,
    )
    reloaded._start_block_for_window = AsyncMock(
        return_value=(100, 0, 3_800, "env:EVM_RPC_ETHEREUM_URL")
    )
    reloaded._block_timestamp = AsyncMock(
        side_effect=lambda chain, block: (
            2_000 + (block - 1_000) * 12,
            "env:EVM_RPC_ETHEREUM_URL",
        )
    )
    incremental_calls = []

    async def incremental_swap_logs(**kwargs):
        incremental_calls.append(kwargs)
        return [_swap_log(kwargs["end_block"])], ["env:EVM_RPC_ETHEREUM_URL"]

    reloaded._swap_logs = incremental_swap_logs
    logs, window, _ = await reloaded._collect_swap_log_window(
        chain="ethereum",
        contract="0x" + "1" * 40,
        end_block=1_150,
        lookback_seconds=86_400,
    )

    assert incremental_calls == [
        {
            "chain": "ethereum",
            "contract": "0x" + "1" * 40,
            "start_block": 1_001,
            "end_block": 1_150,
            "chunk_size": 5,
        }
    ]
    assert len(logs) == 2
    assert window["status"] == "collecting"
    assert window["window_coverage_seconds"] == 3_600


@pytest.mark.asyncio
async def test_evm_swap_cache_marks_complete_full_window_as_ok(tmp_path) -> None:
    adapter = EVMPoolStateAdapter(
        venue_id="aerodrome_slipstream",
        swap_cache_dir=tmp_path,
    )
    adapter._start_block_for_window = AsyncMock(
        return_value=(100, 13_600, 100_000, "public_fallback")
    )
    adapter._block_timestamp = AsyncMock(
        return_value=(13_600, "public_fallback")
    )
    adapter._swap_logs = AsyncMock(
        return_value=([_swap_log(1_000)], ["public_fallback"])
    )

    _, window, _ = await adapter._collect_swap_log_window(
        chain="base",
        contract="0x" + "2" * 40,
        end_block=1_000,
        lookback_seconds=86_400,
    )

    assert window["status"] == "ok"
    assert window["window_coverage_seconds"] == 86_400
