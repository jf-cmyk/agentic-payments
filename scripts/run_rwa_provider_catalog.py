#!/usr/bin/env python3
"""Generate RWA provider catalog ingestion reports."""

from __future__ import annotations

import argparse

from src.rwa_provider_catalog import write_provider_catalog_reports


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--json-out",
        default="reports/rwa_provider_catalog.json",
        help="Path for provider catalog JSON output.",
    )
    parser.add_argument(
        "--csv-out",
        default="reports/rwa_provider_catalog.csv",
        help="Path for provider catalog CSV output.",
    )
    args = parser.parse_args()

    catalog = write_provider_catalog_reports(json_path=args.json_out, csv_path=args.csv_out)
    summary = catalog["summary"]
    print(
        "wrote provider catalog reports: "
        f"{summary['provider_count']} providers, "
        f"{summary['job_count']} ingestion jobs, "
        f"{summary['blocked_by_auth_or_license']} blocked by auth/license"
    )


if __name__ == "__main__":
    main()
