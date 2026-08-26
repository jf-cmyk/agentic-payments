from __future__ import annotations

import json
from unittest.mock import AsyncMock

import pytest

from src import anthropic_mcp_server, cursor_mcp_server, openai_mcp_server


PROVIDERS = (
    (
        anthropic_mcp_server,
        anthropic_mcp_server.anthropic_list_instruments,
        anthropic_mcp_server.anthropic_search_pairs,
        anthropic_mcp_server.anthropic_mcp,
    ),
    (
        cursor_mcp_server,
        cursor_mcp_server.cursor_list_instruments,
        cursor_mcp_server.cursor_search_pairs,
        cursor_mcp_server.cursor_mcp,
    ),
    (
        openai_mcp_server,
        openai_mcp_server.openai_list_instruments,
        openai_mcp_server.openai_search_pairs,
        openai_mcp_server.openai_mcp,
    ),
)


def _details(payload: str) -> dict[str, object]:
    return json.loads(payload.split("<details>\n", 1)[1].split("\n</details>", 1)[0])


@pytest.mark.asyncio
@pytest.mark.parametrize("module,list_tool,search_tool,mcp", PROVIDERS)
async def test_authenticated_discovery_is_bounded_and_provenanced(
    monkeypatch,
    module,
    list_tool,
    search_tool,
    mcp,
):
    instruments = [f"PAIR-{index:04d}" for index in reversed(range(605))]
    client = AsyncMock()
    client.list_vwap_instruments = AsyncMock(return_value=instruments)
    client.search_pairs_page = AsyncMock(return_value=([], 0))
    monkeypatch.setattr(module, "_client", client)

    listed = _details(await list_tool("vwap", limit=100, offset=100))
    missing = _details(await search_tool("missing"))
    tools = {tool.name: tool for tool in await mcp.list_tools()}
    schema = tools["list_instruments"].parameters

    assert listed["total_instruments"] == 605
    assert listed["returned_instruments"] == 100
    assert listed["offset"] == 100
    assert listed["next_offset"] == 200
    assert listed["instruments"] == sorted(instruments)[100:200]
    assert listed["meta"]["snapshot_scope"] == "full_upstream_catalog"
    assert listed["meta"]["source_observed_at"] is None
    assert listed["meta"]["freshness_status"] == "upstream_timestamp_unavailable"
    assert len(listed["meta"]["snapshot_sha256"]) == 64

    assert missing["total_matches"] == 0
    assert missing["returned_matches"] == 0
    assert missing["has_more"] is False
    assert missing["meta"]["snapshot_scope"] == "returned_search_page"
    assert len(missing["meta"]["snapshot_sha256"]) == 64

    assert schema["properties"]["limit"]["default"] == 100
    assert schema["properties"]["limit"]["minimum"] == 1
    assert schema["properties"]["limit"]["maximum"] == 500
    assert schema["properties"]["offset"]["default"] == 0
    assert schema["properties"]["offset"]["minimum"] == 0

    search_schema = tools["search_pairs"].parameters
    assert search_schema["properties"]["limit"]["default"] == 50
    assert search_schema["properties"]["limit"]["maximum"] == 500
    assert search_schema["properties"]["offset"]["default"] == 0
