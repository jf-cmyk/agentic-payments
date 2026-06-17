"""
Configuration tests for Blocksize endpoints.
"""

from __future__ import annotations

from src.config import BlocksizeSettings


def test_blocksize_ws_url_converts_https_to_wss():
    cfg = BlocksizeSettings(
        BLOCKSIZE_API_KEY="test-key",
        BLOCKSIZE_BASE_URL="https://data.blocksize.capital/marketdata/v1",
    )

    assert cfg.rest_url == "https://data.blocksize.capital/marketdata/v1/api"
    assert cfg.ws_url == "wss://data.blocksize.capital/marketdata/v1/ws"
