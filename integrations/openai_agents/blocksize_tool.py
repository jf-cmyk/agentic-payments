"""OpenAI Agents SDK function tools for Blocksize production data."""

from __future__ import annotations

import os

from agents import function_tool

from integrations.python import BlocksizeHTTPClient


client = BlocksizeHTTPClient(os.environ.get("BLOCKSIZE_AGENT_ID", "openai-agents-blocksize"))


@function_tool
def blocksize_vwap(pair: str) -> dict:
    """Get a live Blocksize VWAP with its source timestamp and citation metadata.

    Args:
        pair: Instrument such as BTC-USD.
    """
    return client.get_vwap(pair)


@function_tool
def blocksize_bid_ask(pair: str) -> dict:
    """Get a live Blocksize bid/ask for a supported crypto or equity instrument.

    Args:
        pair: Instrument such as ETH-USD or AAPL.
    """
    return client.get_bid_ask(pair)


BLOCKSIZE_TOOLS = [blocksize_vwap, blocksize_bid_ask]
