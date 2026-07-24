"""LlamaIndex FunctionTool wrappers for Blocksize market data."""

from __future__ import annotations

import os

from llama_index.core.tools import FunctionTool

from integrations.python import BlocksizeHTTPClient


def build_blocksize_tools(agent_id: str | None = None) -> list[FunctionTool]:
    client = BlocksizeHTTPClient(
        agent_id or os.environ.get("BLOCKSIZE_AGENT_ID", "llamaindex-blocksize-agent")
    )

    def blocksize_vwap(pair: str) -> dict:
        """Return Blocksize VWAP, source timestamp, methodology, and citation metadata."""
        return client.get_vwap(pair)

    def blocksize_bid_ask(pair: str) -> dict:
        """Return Blocksize bid/ask data for a supported crypto or equity instrument."""
        return client.get_bid_ask(pair)

    return [
        FunctionTool.from_defaults(fn=blocksize_vwap),
        FunctionTool.from_defaults(fn=blocksize_bid_ask),
    ]
