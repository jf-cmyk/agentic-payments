#!/usr/bin/env python3
"""Discover a canonical Blocksize purchase URL without handling a private key.

Use ``scripts/run_funded_x402_canary.py`` for the bounded official x402 payment
flow. Keeping discovery separate ensures a key is not read until the exact
instrument, service, destination, and maximum price are known.
"""

from __future__ import annotations

import argparse
import asyncio
import json

import httpx


BASE_URL = "https://mcp.blocksize.info"

async def discover(query: str, asset_class: str) -> dict[str, object]:
    async with httpx.AsyncClient(timeout=20, follow_redirects=False) as client:
        response = await client.get(
            f"{BASE_URL}/v1/search",
            params={"q": query, "asset_class": asset_class, "limit": 10},
        )
        response.raise_for_status()
        payload = response.json()
    return {
        "query": query,
        "total": payload.get("total", payload.get("total_matches", 0)),
        "results": [
            {
                key: row.get(key)
                for key in (
                    "canonical_symbol",
                    "asset_class",
                    "recommended_service",
                    "readiness",
                    "price_usdc",
                    "purchase_url",
                    "copy_request",
                )
            }
            for row in payload.get("pairs", [])
        ],
        "next_step": (
            "After reviewing the exact result, run scripts/run_funded_x402_canary.py "
            "with --url, --max-usdc, and an absolute removable-volume key path."
        ),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Resolve a Blocksize instrument for free")
    parser.add_argument("query", help="Ticker or natural-language instrument name")
    parser.add_argument(
        "--asset-class",
        default="all",
        choices=("all", "crypto", "equity", "equities", "fx", "metal"),
    )
    args = parser.parse_args()
    print(json.dumps(asyncio.run(discover(args.query, args.asset_class)), indent=2))


if __name__ == "__main__":
    main()
