#!/usr/bin/env python3
"""Exercise the public, read-only MCP discovery surface without paid calls."""

from __future__ import annotations

import argparse
import json
from typing import Any

import httpx


PROTOCOL_VERSION = "2025-03-26"
TOOL_CALLS: dict[str, dict[str, Any]] = {
    "search_pairs": {"query": "BTC", "asset_class": "crypto"},
    "list_instruments": {"service": "vwap"},
    "get_pricing_info": {},
    "get_product_catalog": {},
    "get_workflow_endpoint": {"product": "agent_market_brief"},
    "get_market_data_endpoint": {"service": "vwap", "symbol": "BTC-USD"},
    "search": {"query": "pricing"},
    "fetch": {"id": "doc:pricing"},
}


def _sse_json(response: httpx.Response) -> dict[str, Any]:
    response.raise_for_status()
    for line in response.text.splitlines():
        if line.startswith("data:"):
            payload = json.loads(line.removeprefix("data:").strip())
            if isinstance(payload, dict):
                return payload
    raise RuntimeError("MCP response did not contain a JSON SSE data event")


def audit(base_url: str) -> dict[str, Any]:
    endpoint = f"{base_url.rstrip('/')}/mcp/server/"
    base_headers = {
        "Accept": "application/json, text/event-stream",
        "Content-Type": "application/json",
    }
    with httpx.Client(timeout=30.0, follow_redirects=True) as client:
        initialize = client.post(
            endpoint,
            headers=base_headers,
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": {
                        "name": "blocksize-public-mcp-readonly-audit",
                        "version": "1.0",
                    },
                },
            },
        )
        init_payload = _sse_json(initialize)
        session_id = initialize.headers.get("mcp-session-id")
        if not session_id:
            raise RuntimeError("MCP initialize response omitted mcp-session-id")

        session_headers = {**base_headers, "Mcp-Session-Id": session_id}
        initialized = client.post(
            endpoint,
            headers=session_headers,
            json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
        if initialized.status_code != 202:
            raise RuntimeError(
                f"MCP initialized notification returned {initialized.status_code}, expected 202"
            )

        tools_response = client.post(
            endpoint,
            headers=session_headers,
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        tools_payload = _sse_json(tools_response)
        tools = tools_payload.get("result", {}).get("tools", [])
        tool_names = sorted(
            str(tool.get("name")) for tool in tools if isinstance(tool, dict) and tool.get("name")
        )

        call_results: dict[str, dict[str, Any]] = {}
        for request_id, (tool_name, arguments) in enumerate(TOOL_CALLS.items(), start=10):
            response = client.post(
                endpoint,
                headers=session_headers,
                json={
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {"name": tool_name, "arguments": arguments},
                },
            )
            payload = _sse_json(response)
            result = payload.get("result", {})
            content = result.get("content", []) if isinstance(result, dict) else []
            text_size = sum(
                len(str(item.get("text", "")))
                for item in content
                if isinstance(item, dict) and item.get("type") == "text"
            )
            call_results[tool_name] = {
                "http_status": response.status_code,
                "is_error": bool(result.get("isError", False))
                if isinstance(result, dict)
                else True,
                "content_blocks": len(content),
                "text_bytes": text_size,
            }

        terminated = client.delete(endpoint, headers=session_headers)

    server_result = init_payload.get("result", {})
    expected_names = sorted(TOOL_CALLS)
    return {
        "endpoint": endpoint,
        "initialize_http_status": initialize.status_code,
        "protocol_version": server_result.get("protocolVersion"),
        "server_info": server_result.get("serverInfo"),
        "tools_list_http_status": tools_response.status_code,
        "tool_names": tool_names,
        "expected_tool_names": expected_names,
        "tool_catalog_matches_expected": tool_names == expected_names,
        "tool_calls": call_results,
        "all_tool_calls_passed": all(
            not result["is_error"] and result["http_status"] == 200
            for result in call_results.values()
        ),
        "terminate_http_status": terminated.status_code,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("base_url", nargs="?", default="https://mcp.blocksize.info")
    args = parser.parse_args()
    result = audit(args.base_url)
    result["passed"] = bool(
        result["initialize_http_status"] == 200
        and result["protocol_version"] == PROTOCOL_VERSION
        and result["tools_list_http_status"] == 200
        and result["tool_catalog_matches_expected"]
        and result["all_tool_calls_passed"]
        and result["terminate_http_status"] == 200
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
