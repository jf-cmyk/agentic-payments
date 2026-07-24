#!/usr/bin/env python3
"""Generate RWA source-readiness dependency reports."""

from __future__ import annotations

import argparse

from src.rwa_source_readiness import write_source_readiness_reports


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json-out",
        default="reports/rwa_source_readiness.json",
        help="Path for source-readiness JSON output.",
    )
    parser.add_argument(
        "--csv-out",
        default="reports/rwa_source_readiness.csv",
        help="Path for source-readiness CSV output.",
    )
    args = parser.parse_args()

    readiness = write_source_readiness_reports(json_path=args.json_out, csv_path=args.csv_out)
    summary = readiness["summary"]
    print(
        "wrote source-readiness reports: "
        f"{summary['dependency_count']} dependencies, "
        f"{summary['configured']} configured, "
        f"{summary['blocked_by_license_or_contract']} blocked by license/contract"
    )


if __name__ == "__main__":
    main()
