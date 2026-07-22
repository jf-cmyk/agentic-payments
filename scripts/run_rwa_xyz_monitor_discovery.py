#!/usr/bin/env python3
"""Refresh RWA.xyz New Asset Monitor discovery reports."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.rwa_xyz_monitor import (
    DEFAULT_RWA_XYZ_ASSET_CSV_PATH,
    DEFAULT_RWA_XYZ_REPORT_JSON_PATH,
    DEFAULT_RWA_XYZ_TOKEN_CSV_PATH,
    fetch_rwa_xyz_monitor_payload,
    load_payload_from_file,
    write_rwa_xyz_monitor_reports,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", help="Optional local monitor JSON or HTML file to parse instead of fetching live")
    parser.add_argument("--json-out", default=str(DEFAULT_RWA_XYZ_REPORT_JSON_PATH))
    parser.add_argument("--assets-csv-out", default=str(DEFAULT_RWA_XYZ_ASSET_CSV_PATH))
    parser.add_argument("--tokens-csv-out", default=str(DEFAULT_RWA_XYZ_TOKEN_CSV_PATH))
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    if args.input:
        payload, metadata = load_payload_from_file(args.input)
    else:
        payload, metadata = fetch_rwa_xyz_monitor_payload(timeout=args.timeout)
    report = write_rwa_xyz_monitor_reports(
        json_path=args.json_out,
        asset_csv_path=args.assets_csv_out,
        token_csv_path=args.tokens_csv_out,
        payload=payload,
        fetch_metadata=metadata,
    )
    summary = report["summary"]
    print(
        "wrote RWA.xyz monitor discovery reports: "
        f"{summary['asset_count']} assets, "
        f"{summary['token_count']} tokens, "
        f"{summary['coverage_row_count']} coverage rows, "
        f"{summary['recent_30d_asset_count']} added in the last 30 days"
    )


if __name__ == "__main__":
    main()
