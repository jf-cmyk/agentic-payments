from __future__ import annotations

import ast
import json
from email.message import Message
from pathlib import Path
from urllib.error import HTTPError

import pytest

from integrations.python import BlocksizeHTTPClient, BlocksizePaymentRequired


ROOT = Path(__file__).resolve().parents[1]


class FakeResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def test_shared_python_client_sends_stable_identity_and_preserves_payload() -> None:
    captured = {}

    def opener(request, *, timeout):
        captured["url"] = request.full_url
        captured["agent_id"] = request.headers["X-agent-id"]
        captured["timeout"] = timeout
        return FakeResponse({"status": "ok", "data": {"vwap": 100.0}})

    client = BlocksizeHTTPClient("framework-agent-123", opener=opener)
    payload = client.get_vwap("BTC/USD")

    assert captured == {
        "url": "https://mcp.blocksize.info/v1/vwap/btc-usd",
        "agent_id": "framework-agent-123",
        "timeout": 20.0,
    }
    assert payload["data"]["vwap"] == 100.0


def test_shared_python_client_exposes_x402_challenge() -> None:
    headers = Message()
    headers["PAYMENT-REQUIRED"] = "encoded-challenge"

    def opener(request, *, timeout):
        raise HTTPError(request.full_url, 402, "Payment Required", headers, None)

    client = BlocksizeHTTPClient("framework-agent-123", opener=opener)
    with pytest.raises(BlocksizePaymentRequired) as error:
        client.get_bid_ask("AAPL")

    assert error.value.payment_required == "encoded-challenge"


@pytest.mark.parametrize(
    "relative_path",
    [
        "integrations/langchain/blocksize_tool.py",
        "integrations/llamaindex/blocksize_tool.py",
        "integrations/openai_agents/blocksize_tool.py",
    ],
)
def test_python_framework_examples_parse(relative_path: str) -> None:
    ast.parse((ROOT / relative_path).read_text(encoding="utf-8"))


def test_typescript_framework_examples_are_complete_and_version_pinned() -> None:
    package = json.loads((ROOT / "integrations/typescript/package.json").read_text(encoding="utf-8"))
    assert package["dependencies"] == {
        "@goat-sdk/core": "0.5.0",
        "ai": "7.0.37",
        "solana-agent-kit": "2.0.10",
        "zod": "3.25.76",
    }
    expected_markers = {
        "integrations/vercel-ai-sdk/blocksize-tool.ts": ["inputSchema", "getBlocksizeVwap"],
        "integrations/goat/blocksize.plugin.ts": ["PluginBase", "BlocksizeService"],
        "integrations/solana-agent-kit/blocksize-tools.ts": ["createVercelAITools", "createBlocksizeTools"],
    }
    for relative_path, markers in expected_markers.items():
        source = (ROOT / relative_path).read_text(encoding="utf-8")
        assert all(marker in source for marker in markers)
