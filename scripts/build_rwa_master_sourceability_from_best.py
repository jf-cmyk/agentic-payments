#!/usr/bin/env python3
"""Build RWA master sourceability CSVs from a best-price lane file."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


SOURCEABILITY_FIELDS = [
    "sourceability_status",
    "currently_sourceable",
    "best_source_symbol",
    "best_venue",
    "best_price_type",
    "best_price",
    "candidate_venues",
    "price_source_lane",
    "sourceability_next_action",
]


def _read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _write_csv(path: Path, fields: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _sourceability(best: dict[str, Any] | None) -> dict[str, Any]:
    if best and best.get("best_price_status") == "candidate_price_fetched":
        return {
            "sourceability_status": "candidate_price_fetched_source_all",
            "currently_sourceable": "True",
            "best_source_symbol": best.get("best_source_symbol", ""),
            "best_venue": best.get("best_venue", ""),
            "best_price_type": best.get("best_price_type", ""),
            "best_price": best.get("best_price", ""),
            "candidate_venues": best.get("candidate_venues", ""),
            "price_source_lane": best.get("price_source_lane", ""),
            "sourceability_next_action": best.get("next_action", ""),
        }
    return {
        "sourceability_status": "not_fetched_lane_requires_access_or_adapter",
        "currently_sourceable": "False",
        "best_source_symbol": "",
        "best_venue": "",
        "best_price_type": "",
        "best_price": "",
        "candidate_venues": "",
        "price_source_lane": best.get("price_source_lane", "") if best else "",
        "sourceability_next_action": best.get("next_action", "") if best else "",
    }


def _unique_ticker_rows(sourceable_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in sourceable_rows:
        ticker = str(row.get("rwa_xyz_ticker") or row.get("asset_id") or row.get("symbol") or "")
        grouped[ticker].append(row)

    rows: list[dict[str, Any]] = []
    for ticker, items in grouped.items():
        first = items[0]
        venues = sorted({str(row.get("best_venue") or "") for row in items if row.get("best_venue")})
        contracts = sorted({str(row.get("address") or "") for row in items if row.get("address")})
        networks = sorted({str(row.get("network") or "") for row in items if row.get("network")})
        rows.append(
            {
                "rwa_xyz_ticker": ticker,
                "asset_id": first.get("asset_id", ""),
                "asset_name": first.get("asset_name", ""),
                "asset_class": first.get("asset_class", ""),
                "rwa_xyz_asset_class": first.get("rwa_xyz_asset_class", ""),
                "issuer_name": first.get("issuer_name", ""),
                "platforms": "|".join(sorted({str(row.get("platform") or "") for row in items if row.get("platform")})),
                "networks": "|".join(networks),
                "token_contract_count": len(contracts),
                "token_contract_addresses": "|".join(contracts),
                "sourceable_token_rows": len(items),
                "candidate_venues": "|".join(venues),
                "best_price_types": "|".join(sorted({str(row.get("best_price_type") or "") for row in items if row.get("best_price_type")})),
                "best_source_symbols": "|".join(sorted({str(row.get("best_source_symbol") or "") for row in items if row.get("best_source_symbol")})),
            }
        )
    return sorted(rows, key=lambda row: (str(row.get("asset_class") or ""), str(row.get("rwa_xyz_ticker") or "")))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tokens-csv", type=Path, default=Path("reports/rwa_xyz_new_asset_monitor_tokens.csv"))
    parser.add_argument("--best-csv", type=Path, required=True)
    parser.add_argument("--all-out", type=Path, required=True)
    parser.add_argument("--sourceable-out", type=Path, required=True)
    parser.add_argument("--unique-out", type=Path, required=True)
    parser.add_argument("--summary-json-out", type=Path, required=True)
    args = parser.parse_args()

    tokens = _read_csv(args.tokens_csv)
    best_by_token_row = {str(row.get("token_row_id") or ""): row for row in _read_csv(args.best_csv)}

    token_fields = list(tokens[0].keys()) if tokens else []
    output_fields = token_fields + SOURCEABILITY_FIELDS
    all_rows: list[dict[str, Any]] = []
    missing_best = 0
    for token in tokens:
        best = best_by_token_row.get(str(token.get("token_row_id") or ""))
        if best is None:
            missing_best += 1
        all_rows.append({**token, **_sourceability(best)})

    sourceable_rows = [row for row in all_rows if row.get("currently_sourceable") == "True"]
    unique_rows = _unique_ticker_rows(sourceable_rows)
    _write_csv(args.all_out, output_fields, all_rows)
    _write_csv(args.sourceable_out, output_fields, sourceable_rows)
    unique_fields = list(unique_rows[0].keys()) if unique_rows else []
    _write_csv(args.unique_out, unique_fields, unique_rows)

    summary = {
        "token_rows": len(all_rows),
        "sourceable_token_rows": len(sourceable_rows),
        "sourceable_unique_tickers": len(unique_rows),
        "sourceable_unique_asset_ids": len({row.get("asset_id") for row in sourceable_rows if row.get("asset_id")}),
        "missing_best_join_rows": missing_best,
        "by_sourceability_status": dict(sorted(Counter(row["sourceability_status"] for row in all_rows).items())),
        "sourceable_by_asset_class": dict(sorted(Counter(row.get("asset_class", "") for row in sourceable_rows).items())),
        "sourceable_by_venue": dict(sorted(Counter(row.get("best_venue", "") for row in sourceable_rows).items())),
    }
    args.summary_json_out.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
