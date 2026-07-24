#!/usr/bin/env python3
"""Generate RWA DEX allowlist reports."""

from __future__ import annotations

import argparse

from src.rwa_dex_allowlist import write_dex_allowlist_reports


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json-out",
        default="reports/rwa_dex_allowlist.json",
        help="Path for DEX allowlist JSON output.",
    )
    parser.add_argument(
        "--csv-out",
        default="reports/rwa_dex_allowlist.csv",
        help="Path for DEX allowlist CSV output.",
    )
    args = parser.parse_args()

    allowlist = write_dex_allowlist_reports(json_path=args.json_out, csv_path=args.csv_out)
    summary = allowlist["summary"]
    print(
        "wrote DEX allowlist reports: "
        f"{summary['candidate_count']} candidates, "
        f"{summary['promotion_job_count']} promotion jobs"
    )


if __name__ == "__main__":
    main()
