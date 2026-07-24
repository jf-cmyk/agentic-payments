#!/usr/bin/env python3
"""Generate derivative venue discovery reports for RWA coverage expansion."""

from __future__ import annotations

import argparse

from src.rwa_derivative_venues import (
    DEFAULT_DERIVATIVE_VENUE_DISCOVERY_CSV_PATH,
    DEFAULT_DERIVATIVE_VENUE_DISCOVERY_JSON_PATH,
    write_derivative_venue_discovery_reports,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", default=str(DEFAULT_DERIVATIVE_VENUE_DISCOVERY_JSON_PATH))
    parser.add_argument("--csv-out", default=str(DEFAULT_DERIVATIVE_VENUE_DISCOVERY_CSV_PATH))
    args = parser.parse_args()

    report = write_derivative_venue_discovery_reports(
        json_path=args.json_out,
        csv_path=args.csv_out,
    )
    summary = report["summary"]
    print(
        "wrote derivative venue discovery reports: "
        f"{summary['coverage_row_count']} coverage rows, "
        f"{summary['market_row_count']} market rows, "
        f"{summary['sourceable_venue_count']} sourceable venues, "
        f"{summary['blocked_or_gated_venue_count']} blocked/gated venues"
    )


if __name__ == "__main__":
    main()
