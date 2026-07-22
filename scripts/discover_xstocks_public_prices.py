#!/usr/bin/env python3
"""Discover exact xStocks catalog matches and build reference-price probe rows."""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx


DEFAULT_MASTER = Path("reports/rwa_unique_ticker_sourceability_opportunity_2026-07-16.csv")
DEFAULT_JSON = Path("reports/rwa_xstocks_public_price_discovery_2026-07-16.json")
DEFAULT_CSV = Path("reports/rwa_xstocks_public_price_discovery_2026-07-16.csv")
DEFAULT_PROBE = Path("reports/rwa_xstocks_public_price_probe_input_2026-07-16.csv")
BASE_URL = "https://api.backed.fi/api/v2"


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _source_symbol(ticker: str) -> str | None:
    clean = ticker.strip()
    if not clean.lower().endswith("x"):
        return None
    return f"{clean[:-1]}x"


def _deployments(asset: dict[str, Any]) -> list[dict[str, Any]]:
    value = asset.get("deployments")
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


async def _discover_one(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    row: dict[str, str],
) -> dict[str, Any] | None:
    source_symbol = _source_symbol(row.get("rwa_xyz_ticker", ""))
    if source_symbol is None:
        return None
    async with semaphore:
        asset_response = await client.get(f"{BASE_URL}/public/assets/{source_symbol}")
        if asset_response.status_code == 404:
            return None
        asset_response.raise_for_status()
        price_response = await client.get(f"{BASE_URL}/public/assets/{source_symbol}/price-data")
        price_response.raise_for_status()
    asset = asset_response.json()
    price_data = price_response.json()
    quote = price_data.get("quote") if isinstance(price_data, dict) else None
    deployments = _deployments(asset if isinstance(asset, dict) else {})
    official_addresses = [str(item.get("address")) for item in deployments if item.get("address")]
    master_addresses = [item for item in row.get("token_contract_addresses", "").split("|") if item]
    address_matches = sorted(set(official_addresses) & set(master_addresses))
    return {
        "rwa_xyz_ticker": row.get("rwa_xyz_ticker", ""),
        "asset_ids": row.get("asset_ids", ""),
        "asset_names": row.get("asset_names", ""),
        "asset_classes": row.get("asset_classes", ""),
        "platforms": row.get("platforms", ""),
        "current_status": row.get("current_status", ""),
        "source_symbol": asset.get("symbol") if isinstance(asset, dict) else source_symbol,
        "underlying_symbol": asset.get("underlyingSymbol") if isinstance(asset, dict) else "",
        "quote": quote,
        "is_trading_halted": asset.get("isTradingHalted") if isinstance(asset, dict) else None,
        "official_token_contracts": "|".join(official_addresses),
        "official_networks": "|".join(str(item.get("network")) for item in deployments if item.get("network")),
        "master_token_contracts": row.get("token_contract_addresses", ""),
        "contract_match_count": len(address_matches),
        "matched_contracts": "|".join(address_matches),
        "source_type": "issuer_reference_price",
        "use_case": "reference_price_only_not_bidask_or_vwap",
        "source_timestamp_present": False,
        "raw_payload_replayable": True,
        "production_grade": False,
        "production_blockers": (
            "source_timestamp_missing,reference_only_not_l2_liquidity,continuous_quality_windows_missing,"
            "benchmark_alignment_missing,rights_clearance_missing,multi_source_consensus_missing"
        ),
        "asset_payload": asset,
        "price_payload": price_data,
    }


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    master = _read_csv(args.master)
    candidates = [
        row for row in master
        if "xStocks" in row.get("platforms", "") and _source_symbol(row.get("rwa_xyz_ticker", ""))
    ]
    semaphore = asyncio.Semaphore(args.concurrency)
    async with httpx.AsyncClient(timeout=20) as client:
        discovered = await asyncio.gather(
            *[_discover_one(client, semaphore, row) for row in candidates],
            return_exceptions=True,
        )
    errors = [str(item) for item in discovered if isinstance(item, Exception)]
    rows = sorted(
        [item for item in discovered if isinstance(item, dict)],
        key=lambda item: str(item["rwa_xyz_ticker"]),
    )
    positive = [row for row in rows if isinstance(row.get("quote"), (int, float)) and row["quote"] > 0]
    newly_sourceable = [row for row in positive if row.get("current_status") != "candidate_sourceable_now"]
    summary = {
        "master_tickers": len(master),
        "xstocks_exact_symbol_candidates": len(candidates),
        "official_catalog_matches": len(rows),
        "positive_public_quotes": len(positive),
        "new_reference_price_tickers": len(newly_sourceable),
        "already_candidate_sourceable": len(positive) - len(newly_sourceable),
        "contract_matched_tickers": sum(int(row.get("contract_match_count") or 0) > 0 for row in rows),
        "request_errors": len(errors),
        "generated_at": datetime.now(UTC).isoformat(),
    }
    payload = {
        "summary": summary,
        "source": {
            "docs": "https://docs.xstocks.fi/developers",
            "base_url": BASE_URL,
            "authentication": "none_for_public_endpoints",
            "semantics": "issuer_reference_price_only_not_native_bidask_or_vwap",
        },
        "errors": errors,
        "rows": rows,
    }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")

    csv_fields = [key for key in rows[0] if key not in {"asset_payload", "price_payload"}] if rows else []
    with args.output_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=csv_fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: row.get(key) for key in csv_fields})

    probe_fields = [
        "feed_id", "kind", "asset_id", "asset_classes", "symbol", "venue", "source_type",
        "promotion_status", "production_promoted",
    ]
    with args.output_probe.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=probe_fields)
        writer.writeheader()
        for row in positive:
            ticker = str(row["rwa_xyz_ticker"])
            writer.writerow(
                {
                    "feed_id": f"rwa_reference:xstocks_public:{ticker}:issuer_reference_price",
                    "kind": "bidask",
                    "asset_id": str(row.get("asset_ids") or ticker).split("|")[0],
                    "asset_classes": json.dumps(str(row.get("asset_classes") or "equity").split("|")),
                    "symbol": f"{ticker}/USD",
                    "venue": "xstocks_public",
                    "source_type": "issuer_reference_price",
                    "promotion_status": "candidate_reference_not_production",
                    "production_promoted": "False",
                }
            )
    return payload


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master", type=Path, default=DEFAULT_MASTER)
    parser.add_argument("--output-json", type=Path, default=DEFAULT_JSON)
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_CSV)
    parser.add_argument("--output-probe", type=Path, default=DEFAULT_PROBE)
    parser.add_argument("--concurrency", type=int, default=8)
    args = parser.parse_args()
    payload = asyncio.run(_run(args))
    print(json.dumps(payload["summary"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
