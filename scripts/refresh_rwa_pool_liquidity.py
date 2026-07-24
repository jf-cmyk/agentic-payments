#!/usr/bin/env python3
"""Refresh public pool liquidity for the verified RWA pool candidate list.

The output is discovery evidence only.  Pool TVL and 24h volume are not
substitutes for sequenced executable depth.
"""

from __future__ import annotations

import argparse
import csv
import json
import time
from datetime import UTC, datetime
from pathlib import Path

import httpx


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", type=Path, default=Path("reports/rwa_contract_pool_sources_combined_2026-07-16.csv"))
    parser.add_argument("--output-csv", type=Path, default=Path("reports/rwa_pool_liquidity_refresh_2026-07-22.csv"))
    parser.add_argument("--output-json", type=Path, default=Path("reports/rwa_pool_liquidity_refresh_2026-07-22.json"))
    args = parser.parse_args()

    with args.input.open(newline="", encoding="utf-8-sig") as handle:
        pools = list(csv.DictReader(handle))

    observed_at = datetime.now(UTC).isoformat()
    results: list[dict[str, object]] = []
    with httpx.Client(timeout=20.0, headers={"User-Agent": "Blocksize-RWA-readonly-liquidity-audit/1.0"}) as client:
        for source in pools:
            chain = source.get("chain", "")
            pool = source.get("pool_address", "")
            url = f"https://api.dexscreener.com/latest/dex/pairs/{chain}/{pool}"
            row: dict[str, object] = {
                "allowlist_id": source.get("allowlist_id", ""),
                "asset_id": source.get("asset_id", ""),
                "rwa_ticker": source.get("rwa_ticker", ""),
                "network": source.get("network", ""),
                "chain": chain,
                "dex_id_expected": source.get("dex_id", ""),
                "pool_address": pool,
                "observed_at": observed_at,
                "source_url": url,
                "status": "unavailable",
                "liquidity_usd": None,
                "volume_h24_usd": None,
                "pair_price_usd": None,
                "pair_created_at": None,
                "reason": "",
            }
            try:
                response = client.get(url)
                response.raise_for_status()
                payload = response.json()
                matches = [
                    pair for pair in (payload.get("pairs") or [])
                    if str(pair.get("pairAddress", "")).lower() == pool.lower()
                ]
                pair = matches[0] if matches else None
                if pair:
                    row.update(
                        status="observed_public_pool_snapshot",
                        liquidity_usd=(pair.get("liquidity") or {}).get("usd"),
                        volume_h24_usd=(pair.get("volume") or {}).get("h24"),
                        pair_price_usd=pair.get("priceUsd"),
                        pair_created_at=pair.get("pairCreatedAt"),
                        dex_id_observed=pair.get("dexId"),
                        base_token_address=(pair.get("baseToken") or {}).get("address"),
                        quote_token_address=(pair.get("quoteToken") or {}).get("address"),
                        reason="Public pair snapshot observed; liquidity is pool TVL, not block-size executable depth.",
                    )
                else:
                    row["reason"] = "Public API returned no exact pair-address match."
            except Exception as exc:  # evidence capture must retain failures
                row["reason"] = f"{type(exc).__name__}: {exc}"
            results.append(row)
            time.sleep(0.12)

    args.output_csv.parent.mkdir(parents=True, exist_ok=True)
    fields = list(dict.fromkeys(key for row in results for key in row))
    with args.output_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(results)
    args.output_json.write_text(json.dumps({"observed_at": observed_at, "results": results}, indent=2), encoding="utf-8")
    print(json.dumps({"rows": len(results), "observed": sum(r["status"] == "observed_public_pool_snapshot" for r in results), "output": str(args.output_csv)}))


if __name__ == "__main__":
    main()
