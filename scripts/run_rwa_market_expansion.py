#!/usr/bin/env python3
"""Generate expanded RWA/traditional-asset sourcing and futures-data reports."""

from __future__ import annotations

import argparse

from src.rwa_market_expansion import write_market_expansion_reports


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--expansion-json-out",
        default="reports/rwa_market_expansion_plan.json",
        help="Path for expanded venue/ticker sourcing JSON output.",
    )
    parser.add_argument(
        "--futures-json-out",
        default="reports/rwa_futures_data_plan.json",
        help="Path for futures-derived pricing JSON output.",
    )
    parser.add_argument(
        "--venue-csv-out",
        default="reports/rwa_market_expansion_venues.csv",
        help="Path for expanded venue CSV output.",
    )
    parser.add_argument(
        "--futures-csv-out",
        default="reports/rwa_futures_jobs.csv",
        help="Path for futures sourcing job CSV output.",
    )
    args = parser.parse_args()

    reports = write_market_expansion_reports(
        expansion_json_path=args.expansion_json_out,
        futures_json_path=args.futures_json_out,
        venue_csv_path=args.venue_csv_out,
        futures_csv_path=args.futures_csv_out,
    )
    expansion = reports["market_expansion"]["summary"]
    futures = reports["futures_data_plan"]["summary"]
    print(
        "wrote market expansion reports: "
        f"{expansion['expanded_venue_count']} venues, "
        f"{expansion['equity_universe_count']} equity universes, "
        f"{futures['futures_underlying_jobs']} futures jobs"
    )


if __name__ == "__main__":
    main()
