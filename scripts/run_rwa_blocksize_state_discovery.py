#!/usr/bin/env python3
"""Discover Blocksize state_instruments coverage for RWA state-reference rows."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.blocksize_client import BlocksizeClient  # noqa: E402
from src.rwa_non_crypto_feeds import build_non_crypto_feed_catalog  # noqa: E402


DEFAULT_JSON_PATH = Path("reports/rwa_blocksize_state_discovery.json")


def _clean_symbol(symbol: str) -> str:
    return symbol.upper().replace("-", "").replace("/", "")


def _split_pair(clean: str) -> tuple[str, str]:
    for quote in ("USDT", "USDC", "USD"):
        if clean.endswith(quote) and len(clean) > len(quote):
            return clean[: -len(quote)], quote
    return clean, "USD"


def _matching_state_instruments(symbol: str, instruments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    clean = _clean_symbol(symbol)
    base, _quote = _split_pair(clean)
    targets = {clean, f"{base}USD", f"{base}USDC", f"{base}USDT"}
    matches: list[dict[str, Any]] = []
    for item in instruments:
        item_symbol = str(item.get("symbol") or "").upper()
        if item_symbol in targets:
            matches.append(item)
    return matches


def _state_targets() -> list[dict[str, Any]]:
    catalog = build_non_crypto_feed_catalog(exclude_tokenized_stocks=False, venue="blocksize_state")
    seen: set[str] = set()
    targets: list[dict[str, Any]] = []
    for feed in [*catalog["vwap_feeds"], *catalog["bidask_feeds"]]:
        metadata = feed.get("metadata") if isinstance(feed.get("metadata"), dict) else {}
        state_symbol = str(metadata.get("state_symbol") or feed["symbol"]).upper().replace("/", "")
        if state_symbol in seen:
            continue
        seen.add(state_symbol)
        targets.append(
            {
                "asset_id": feed["asset_id"],
                "asset_classes": feed["asset_classes"],
                "symbol": feed["symbol"],
                "state_symbol": state_symbol,
                "venue": feed["venue"],
                "source_type": feed["source_type"],
            }
        )
    return sorted(targets, key=lambda row: str(row["state_symbol"]))


async def build_state_discovery() -> dict[str, Any]:
    client = BlocksizeClient()
    try:
        instruments = await client.list_state_instruments()
    finally:
        await client.close()

    targets = _state_targets()
    rows: list[dict[str, Any]] = []
    for target in targets:
        matches = _matching_state_instruments(target["state_symbol"], instruments)
        rows.append(
            {
                **target,
                "status": "state_instrument_matched" if matches else "missing_state_instrument",
                "match_count": len(matches),
                "matched_symbols": sorted(
                    {
                        str(match.get("symbol") or "").upper()
                        for match in matches
                        if match.get("symbol")
                    }
                ),
                "matched_instruments": matches[:10],
                "promotion_status": (
                    "candidate_requires_state_pool_freshness_nav_and_benchmark_checks"
                    if matches
                    else "blocked_missing_state_instruments_coverage"
                ),
            }
        )

    matched = sum(1 for row in rows if row["match_count"] > 0)
    return {
        "product": "rwa_blocksize_state_discovery",
        "as_of": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "target_count": len(rows),
            "matched": matched,
            "missing_state_instrument": len(rows) - matched,
            "blocksize_state_instrument_count": len(instruments),
        },
        "policy": {
            "promotion_rule": "Matched state_instruments are still supplemental until state_pool freshness, issuer/NAV alignment, manipulation/stale-value checks, and Blocksize benchmark alignment pass.",
            "not_live_liquidity": True,
        },
        "symbols": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", default=str(DEFAULT_JSON_PATH))
    args = parser.parse_args()

    report = asyncio.run(build_state_discovery())
    out = Path(args.json_out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    summary = report["summary"]
    print(
        "wrote Blocksize state discovery report: "
        f"{summary['matched']}/{summary['target_count']} targets matched "
        f"against {summary['blocksize_state_instrument_count']} state instruments"
    )


if __name__ == "__main__":
    main()
