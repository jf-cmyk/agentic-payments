"""Smoke-test the Anthropic-safe remote MCP connector.

This script intentionally keeps paid live-data checks behind an explicit flag so
it can be used during deploy validation without accidentally spending credits.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
from dataclasses import dataclass
from typing import Any

from fastmcp import Client


DEFAULT_URL = "https://anthropic-mcp-beta-production.up.railway.app/anthropic/mcp/"
EXPECTED_TOOLS = {
    "search_pairs",
    "list_instruments",
    "get_credit_balance",
    "get_vwap",
    "get_bid_ask",
    "get_fx_rate",
    "get_metal_price",
}


@dataclass
class Check:
    name: str
    ok: bool
    detail: str


def _parse_json_payload(value: Any) -> dict[str, Any] | None:
    if isinstance(value, dict):
        return value
    if not isinstance(value, str):
        return None
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _credit_snapshot(value: Any) -> dict[str, Any] | None:
    payload = _parse_json_payload(value)
    if not payload:
        return None
    credits = payload.get("credits")
    return credits if isinstance(credits, dict) else None


def _is_error_code(value: Any, code: str) -> bool:
    payload = _parse_json_payload(value)
    return bool(payload and payload.get("status") == "error" and payload.get("error_code") == code)


def _short(value: Any, limit: int = 240) -> str:
    text = str(value).replace("\n", " ")
    return text if len(text) <= limit else f"{text[:limit]}..."


async def _list_tools(client: Client) -> set[str]:
    tools = await client.list_tools()
    return {tool.name for tool in tools}


async def run_checks(
    url: str,
    token: str | None,
    spend_live: bool,
    expect_auth_fail: bool,
) -> list[Check]:
    checks: list[Check] = []

    async with Client(url) as client:
        tool_names = await _list_tools(client)
        missing = sorted(EXPECTED_TOOLS - tool_names)
        extra = sorted(tool_names - EXPECTED_TOOLS)
        checks.append(
            Check(
                "public tool discovery",
                not missing,
                f"found={sorted(tool_names)} missing={missing} extra={extra}",
            )
        )

        search = await client.call_tool("search_pairs", {"query": "btc", "asset_class": "crypto"})
        checks.append(
            Check(
                "public search_pairs",
                "Found" in str(search.data) or "No instruments" in str(search.data),
                _short(search.data),
            )
        )

        instruments = await client.call_tool("list_instruments", {"service": "fx"})
        checks.append(
            Check(
                "public list_instruments",
                "EURUSD" in str(instruments.data),
                _short(instruments.data),
            )
        )

        no_auth_balance = await client.call_tool("get_credit_balance", {})
        checks.append(
            Check(
                "unauth balance blocked",
                _is_error_code(no_auth_balance.data, "AUTH_REQUIRED"),
                _short(no_auth_balance.data),
            )
        )

        no_auth_live = await client.call_tool("get_vwap", {"pair": "BTCUSD"})
        checks.append(
            Check(
                "unauth live data blocked",
                _is_error_code(no_auth_live.data, "AUTH_REQUIRED"),
                _short(no_auth_live.data),
            )
        )

    if not token:
        checks.append(
            Check(
                "authenticated checks skipped",
                True,
                "set BETA_TOKEN or ANTHROPIC_BETA_TOKEN to test beta-token access",
            )
        )
        return checks

    if expect_auth_fail:
        async with Client(url, auth=token) as client:
            denied_balance = await client.call_tool("get_credit_balance", {})
            checks.append(
                Check(
                    "rotated token credit balance blocked",
                    _is_error_code(denied_balance.data, "AUTH_REQUIRED"),
                    _short(denied_balance.data),
                )
            )

            denied_live = await client.call_tool("get_vwap", {"pair": "BTCUSD"})
            checks.append(
                Check(
                    "rotated token live data blocked",
                    _is_error_code(denied_live.data, "AUTH_REQUIRED"),
                    _short(denied_live.data),
                )
            )
        return checks

    async with Client(url, auth=token) as client:
        auth_tools = await _list_tools(client)
        checks.append(
            Check(
                "authenticated tool discovery",
                EXPECTED_TOOLS.issubset(auth_tools),
                f"found={sorted(auth_tools)}",
            )
        )

        initial_balance = await client.call_tool("get_credit_balance", {})
        initial_credits = _credit_snapshot(initial_balance.data)
        checks.append(
            Check(
                "authenticated credit balance",
                bool(initial_credits and initial_credits.get("status") == "active"),
                _short(initial_balance.data),
            )
        )

        invalid = await client.call_tool("get_vwap", {"pair": "../bad"})
        checks.append(
            Check(
                "invalid symbol rejected before spend",
                _is_error_code(invalid.data, "INVALID_SYMBOL"),
                _short(invalid.data),
            )
        )

        after_invalid = await client.call_tool("get_credit_balance", {})
        after_invalid_credits = _credit_snapshot(after_invalid.data)
        unchanged = True
        if initial_credits and after_invalid_credits:
            unchanged = (
                initial_credits.get("credits_spent") == after_invalid_credits.get("credits_spent")
                and initial_credits.get("credits_remaining")
                == after_invalid_credits.get("credits_remaining")
            )
        checks.append(
            Check(
                "invalid symbol did not spend credits",
                unchanged,
                _short(after_invalid.data),
            )
        )

        if not spend_live:
            checks.append(
                Check(
                    "paid live checks skipped",
                    True,
                    "pass --spend-live to spend credits on VWAP, bid/ask, FX, and metal checks",
                )
            )
            return checks

        live_calls = [
            ("get_vwap BTCUSD", "get_vwap", {"pair": "BTCUSD"}),
            ("get_bid_ask ADABTC", "get_bid_ask", {"pair": "ADABTC"}),
            ("get_fx_rate EURUSD", "get_fx_rate", {"pair": "EURUSD"}),
            ("get_metal_price XAUUSD", "get_metal_price", {"ticker": "XAUUSD"}),
        ]
        for name, tool, args in live_calls:
            result = await client.call_tool(tool, args)
            checks.append(
                Check(
                    name,
                    "Credits remaining today:" in str(result.data),
                    _short(result.data),
                )
            )

        final_balance = await client.call_tool("get_credit_balance", {})
        checks.append(Check("final credit balance", True, _short(final_balance.data)))

    return checks


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=os.environ.get("ANTHROPIC_MCP_URL", DEFAULT_URL),
        help="Remote MCP URL. Defaults to ANTHROPIC_MCP_URL or the Railway beta URL.",
    )
    parser.add_argument(
        "--token",
        default=os.environ.get("BETA_TOKEN") or os.environ.get("ANTHROPIC_BETA_TOKEN"),
        help="Optional bearer token. Defaults to BETA_TOKEN or ANTHROPIC_BETA_TOKEN.",
    )
    parser.add_argument(
        "--spend-live",
        action="store_true",
        help="Call paid live-data tools. This spends up to 6 daily credits.",
    )
    parser.add_argument(
        "--expect-auth-fail",
        action="store_true",
        help="Treat the supplied token as rotated/invalid and assert live tools are blocked.",
    )
    args = parser.parse_args()

    if args.expect_auth_fail and not args.token:
        parser.error("--expect-auth-fail requires --token, BETA_TOKEN, or ANTHROPIC_BETA_TOKEN")

    checks = asyncio.run(run_checks(args.url, args.token, args.spend_live, args.expect_auth_fail))
    for check in checks:
        mark = "PASS" if check.ok else "FAIL"
        print(f"{mark} {check.name}: {check.detail}")

    return 0 if all(check.ok for check in checks) else 1


if __name__ == "__main__":
    raise SystemExit(main())
