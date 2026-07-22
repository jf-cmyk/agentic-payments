#!/usr/bin/env python3
"""Build a one-row-per-RWA-ticker sourceability and expansion inventory."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path


def split_values(value: str) -> list[str]:
    return [part.strip() for part in value.split("|") if part.strip()]


def sorted_values(values: set[str]) -> str:
    return "|".join(sorted((value for value in values if value), key=str.casefold))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--input",
        default="reports/rwa_master_all_token_contracts_sourceability_source_all_2026-07-16.csv",
    )
    parser.add_argument(
        "--output",
        default="reports/rwa_unique_ticker_sourceability_opportunity_2026-07-16.csv",
    )
    parser.add_argument(
        "--summary",
        default="reports/rwa_unique_ticker_sourceability_opportunity_summary_2026-07-16.json",
    )
    parser.add_argument(
        "--current-output",
        default="reports/rwa_tickers_sourceable_now_2026-07-16.csv",
    )
    parser.add_argument(
        "--market-output",
        default="reports/rwa_tickers_additional_market_price_candidates_2026-07-16.csv",
    )
    parser.add_argument(
        "--issuer-output",
        default="reports/rwa_tickers_additional_issuer_reference_candidates_2026-07-16.csv",
    )
    return parser.parse_args()


def classify(rows: list[dict[str, str]]) -> tuple[str, str, str, str]:
    sourceable = [row for row in rows if row["currently_sourceable"] == "True"]
    lanes = {row["price_source_lane"] for row in rows if row["price_source_lane"]}

    if sourceable:
        price_types = {row["best_price_type"] for row in sourceable if row["best_price_type"]}
        return (
            "candidate_sourceable_now",
            sorted_values(price_types) or "candidate_price",
            "already_candidate_sourceable",
            "Complete replay, continuous quality windows, benchmark/depth checks, rights clearance, and consensus before production promotion.",
        )

    if "venue_orderbook_or_tokenized_spot + dex_pool_discovery" in lanes:
        return (
            "additional_market_price_candidate",
            "BidAsk_or_block_size_VWAP",
            "conditional_on_adapter_access_mapping_and_liquidity",
            "Add a documented venue/order-book adapter or verified direct DEX pool mapping, then confirm liquidity and redistribution rights.",
        )

    return (
        "additional_issuer_reference_candidate",
        "StateData_reference; VWAP_only_if_liquid_secondary_market",
        "conditional_on_issuer_or_transfer_agent_access_and_rights",
        "Obtain timestamped issuer NAV/share-price or transfer-agent data and redistribution rights; add a verified secondary pool where available.",
    )


def write_csv(path: Path, rows: list[dict[str, object]], fieldnames: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def main() -> None:
    args = parse_args()
    input_path = Path(args.input)
    output_path = Path(args.output)
    summary_path = Path(args.summary)

    with input_path.open(newline="", encoding="utf-8") as handle:
        source_rows = list(csv.DictReader(handle))

    grouped: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in source_rows:
        ticker = row["rwa_xyz_ticker"].strip()
        if not ticker:
            continue
        grouped[ticker.upper()].append(row)

    output_rows: list[dict[str, object]] = []
    for ticker_key, rows in sorted(grouped.items()):
        sourceable = [row for row in rows if row["currently_sourceable"] == "True"]
        status, use_case, potential, next_unlock = classify(rows)
        all_addresses = {row["address"] for row in rows if row["address"]}
        sourceable_addresses = {row["address"] for row in sourceable if row["address"]}

        output_rows.append(
            {
                "rwa_xyz_ticker": sorted({row["rwa_xyz_ticker"] for row in rows})[0],
                "current_status": status,
                "currently_candidate_sourceable": "True" if sourceable else "False",
                "production_grade": "False",
                "asset_ids": sorted_values({row["asset_id"] for row in rows}),
                "asset_names": sorted_values({row["asset_name"] for row in rows}),
                "asset_classes": sorted_values({row["asset_class"] for row in rows}),
                "platforms": sorted_values({row["platform"] for row in rows}),
                "networks": sorted_values({row["network"] for row in rows}),
                "token_contract_count": len(all_addresses),
                "token_contract_addresses": sorted_values(all_addresses),
                "sourceable_contract_count": len(sourceable_addresses),
                "sourceable_contract_addresses": sorted_values(sourceable_addresses),
                "current_venues": sorted_values({row["best_venue"] for row in sourceable}),
                "current_price_types": sorted_values(
                    {row["best_price_type"] for row in sourceable}
                ),
                "current_source_symbols": sorted_values(
                    {row["best_source_symbol"] for row in sourceable}
                ),
                "candidate_venues": sorted_values(
                    {
                        venue
                        for row in rows
                        for venue in split_values(row["candidate_venues"])
                    }
                ),
                "required_price_lane": sorted_values(
                    {row["price_source_lane"] for row in rows}
                ),
                "intended_use_case": use_case,
                "additional_sourceability": potential,
                "next_unlock": next_unlock,
            }
        )

    fieldnames = list(output_rows[0])
    write_csv(output_path, output_rows, fieldnames)
    write_csv(
        Path(args.current_output),
        [row for row in output_rows if row["current_status"] == "candidate_sourceable_now"],
        fieldnames,
    )
    write_csv(
        Path(args.market_output),
        [
            row
            for row in output_rows
            if row["current_status"] == "additional_market_price_candidate"
        ],
        fieldnames,
    )
    write_csv(
        Path(args.issuer_output),
        [
            row
            for row in output_rows
            if row["current_status"] == "additional_issuer_reference_candidate"
        ],
        fieldnames,
    )

    status_counts = Counter(str(row["current_status"]) for row in output_rows)
    summary = {
        "input_token_rows": len(source_rows),
        "unique_rwa_tickers": len(output_rows),
        "candidate_sourceable_now": status_counts["candidate_sourceable_now"],
        "additional_market_price_candidates": status_counts[
            "additional_market_price_candidate"
        ],
        "additional_issuer_reference_candidates": status_counts[
            "additional_issuer_reference_candidate"
        ],
        "total_additional_conditional_opportunity": len(output_rows)
        - status_counts["candidate_sourceable_now"],
        "maximum_catalog_coverage_if_all_conditions_are_solved": len(output_rows),
        "production_grade_tickers": 0,
        "status_counts": dict(sorted(status_counts.items())),
    }
    summary_path.write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
