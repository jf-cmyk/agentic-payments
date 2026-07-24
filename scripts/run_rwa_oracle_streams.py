#!/usr/bin/env python3
"""Generate RWA oracle-stream coverage reports."""

from __future__ import annotations

import argparse

from src.rwa_oracle_streams import write_oracle_stream_reports


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json-out",
        default="reports/rwa_oracle_stream_coverage.json",
        help="Path for oracle-stream JSON output.",
    )
    parser.add_argument(
        "--csv-out",
        default="reports/rwa_oracle_stream_coverage.csv",
        help="Path for oracle-stream CSV output.",
    )
    args = parser.parse_args()

    coverage = write_oracle_stream_reports(json_path=args.json_out, csv_path=args.csv_out)
    print(
        "wrote oracle stream reports: "
        f"{coverage['summary']['provider_count']} providers, "
        f"{coverage['summary']['known_feed_entries_lower_bound']} known feed entries lower bound"
    )


if __name__ == "__main__":
    main()
