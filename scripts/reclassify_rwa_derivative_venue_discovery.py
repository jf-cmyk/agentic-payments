#!/usr/bin/env python3
"""Re-normalize a captured derivative report without any network refetch."""

from __future__ import annotations

import argparse

from src.rwa_derivative_venues import (
    DEFAULT_DERIVATIVE_VENUE_DISCOVERY_CSV_PATH,
    DEFAULT_DERIVATIVE_VENUE_DISCOVERY_JSON_PATH,
    write_reclassified_derivative_venue_discovery_reports,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--input-json",
        default=str(DEFAULT_DERIVATIVE_VENUE_DISCOVERY_JSON_PATH),
    )
    parser.add_argument(
        "--json-out",
        default=str(DEFAULT_DERIVATIVE_VENUE_DISCOVERY_JSON_PATH),
    )
    parser.add_argument(
        "--csv-out",
        default=str(DEFAULT_DERIVATIVE_VENUE_DISCOVERY_CSV_PATH),
    )
    args = parser.parse_args()
    report = write_reclassified_derivative_venue_discovery_reports(
        input_json_path=args.input_json,
        json_path=args.json_out,
        csv_path=args.csv_out,
    )
    quality = report["summary"]["identity_quality"]
    print(
        "reclassified captured derivative report without refetch: "
        f"{report['summary']['coverage_row_count']} coverage rows; "
        f"{quality['raw_mixed_class_asset_id_count']} raw mixed ids -> "
        f"{quality['canonical_mixed_class_asset_id_count']} canonical mixed ids; "
        f"acceptance={quality['acceptance']['status']}"
    )


if __name__ == "__main__":
    main()
