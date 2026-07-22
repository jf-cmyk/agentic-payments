#!/usr/bin/env python3
"""Generate RWA consensus source-plan reports."""

from __future__ import annotations

import argparse

from src.rwa_consensus import write_consensus_source_reports


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json-out",
        default="reports/rwa_consensus_source_plan.json",
        help="Path for consensus source-plan JSON output.",
    )
    parser.add_argument(
        "--csv-out",
        default="reports/rwa_consensus_assets.csv",
        help="Path for per-asset consensus source CSV output.",
    )
    parser.add_argument(
        "--exclude-tokenized-stocks",
        action="store_true",
        help="Exclude tokenized stock/xStock rows from primary market feed counts.",
    )
    args = parser.parse_args()

    plan = write_consensus_source_reports(
        json_path=args.json_out,
        csv_path=args.csv_out,
        exclude_tokenized_stocks=args.exclude_tokenized_stocks,
    )
    summary = plan["summary"]
    print(
        "wrote consensus source reports: "
        f"{summary['sourceable_feed_count']} feeds, "
        f"{summary['consensus_sourceable_assets']} consensus-sourceable assets, "
        f"{summary['oracle_feed_entries_lower_bound']} oracle feed entries lower bound"
    )


if __name__ == "__main__":
    main()
