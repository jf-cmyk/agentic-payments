"""OpenAI's plugin scanner records tools only when their schemas avoid anyOf-nullable
parameters, and uses at most the first 512 characters of the server instructions."""
import json

import pytest
from fastmcp import Client

from src.authenticated_mcp_server import nullable_without_anyof
from src.openai_mcp_server import openai_mcp

OPENAI_INSTRUCTIONS_MAX = 512


def test_nullable_without_anyof_rewrites_in_place():
    schema = {
        "type": "object",
        "properties": {
            "price": {
                "anyOf": [{"type": "number", "exclusiveMinimum": 0}, {"type": "null"}],
                "default": None,
                "description": "optional price",
            },
            "symbols": {
                "anyOf": [
                    {"type": "array", "items": {"type": "string"}, "maxItems": 12},
                    {"type": "null"},
                ],
                "default": None,
            },
            "side": {"type": "string", "enum": ["buy", "sell"], "default": "buy"},
            "nested": {
                "type": "object",
                "properties": {"note": {"anyOf": [{"type": "string"}, {"type": "null"}], "default": None}},
            },
            "choice": {"anyOf": [{"type": "string"}, {"type": "integer"}], "default": None},
        },
    }
    nullable_without_anyof(schema)
    props = schema["properties"]
    assert props["price"] == {
        "type": ["number", "null"], "exclusiveMinimum": 0, "description": "optional price",
    }
    assert props["symbols"] == {"type": ["array", "null"], "items": {"type": "string"}, "maxItems": 12}
    assert props["side"] == {"type": "string", "enum": ["buy", "sell"], "default": "buy"}
    assert props["nested"]["properties"]["note"] == {"type": ["string", "null"]}
    # A genuine union is left alone; only its null default goes.
    assert props["choice"] == {"anyOf": [{"type": "string"}, {"type": "integer"}]}


@pytest.mark.asyncio
async def test_openai_tools_advertise_no_anyof_and_no_null_defaults():
    async with Client(openai_mcp) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    assert len(tools) == 18
    for name, tool in tools.items():
        rendered = json.dumps(tool.inputSchema)
        assert "anyOf" not in rendered, name
        assert '"default": null' not in rendered, name
    price = tools["run_pre_trade_check"].inputSchema["properties"]["reference_price"]
    assert price["type"] == ["number", "null"] and price["exclusiveMinimum"] == 0
    assert "default" not in price
    for name in ("get_macro_snapshot", "get_solana_token_brief", "get_trader_alpha_pack"):
        symbols = tools[name].inputSchema["properties"]["symbols"]
        assert symbols["type"] == ["array", "null"], name
        assert symbols["items"] == {"type": "string"}
    assert tools["create_price_receipt"].inputSchema["properties"]["purpose"]["type"] == ["string", "null"]


def test_openai_instructions_fit_the_scanner_window():
    instructions = openai_mcp.instructions
    assert len(instructions) <= OPENAI_INSTRUCTIONS_MAX, len(instructions)
    assert "get_credit_balance" in instructions
    assert "Cite the provider timestamp" in instructions
    assert "trades" in instructions
