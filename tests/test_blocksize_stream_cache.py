"""
Tests for websocket-backed Blocksize subscription cache parsing.
"""

from __future__ import annotations

import pytest

from src.blocksize_stream_cache import BlocksizeStreamCache


@pytest.mark.asyncio
async def test_fixed_vwap_snapshot_is_available_as_24h_read():
    cache = BlocksizeStreamCache(enabled=False, ttl_seconds=3600)
    cache._apply_fixed_vwap_snapshot({
        "snapshot": [
            {"ticker": "BTCUSD", "price": 66800.0, "volume": 1234.0, "ts": 1713567600000},
        ]
    })

    data = await cache.get_vwap_24h("BTCUSD")

    assert data.pair == "BTCUSD"
    assert data.vwap == 66800.0
    assert data.volume == 1234.0
    assert data.source == "blocksize:fixedvwap_subscribe_cache"


@pytest.mark.asyncio
async def test_state_snapshot_is_available_as_state_read():
    cache = BlocksizeStreamCache(enabled=False, ttl_seconds=3600)
    cache._apply_state_snapshot({
        "snapshot": [
            {
                "timestamp": 1713567600,
                "base_symbol": "WSTETH",
                "quote_symbol": "ETH",
                "aggregated_state_price": "1.173842",
            },
        ]
    })

    data = await cache.get_state_price("WSTETHETH")

    assert data.pair == "WSTETHETH"
    assert data.price == 1.173842
    assert data.source == "blocksize:state_subscribe_cache"
