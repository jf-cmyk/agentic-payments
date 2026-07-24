"""LangChain tools backed by Blocksize production market data."""

from __future__ import annotations

import os

from langchain.tools import tool

from integrations.python import BlocksizeHTTPClient


client = BlocksizeHTTPClient(os.environ.get("BLOCKSIZE_AGENT_ID", "langchain-blocksize-agent"))


@tool
def blocksize_vwap(pair: str) -> dict:
    """Get the latest Blocksize VWAP for a pair such as BTC-USD, including timestamp and citation metadata."""
    return client.get_vwap(pair)


@tool
def blocksize_bid_ask(pair: str) -> dict:
    """Get the latest Blocksize bid/ask for a supported crypto or equity instrument."""
    return client.get_bid_ask(pair)


BLOCKSIZE_TOOLS = [blocksize_vwap, blocksize_bid_ask]
