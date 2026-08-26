#!/usr/bin/env python3
"""Run the daily RWA feed discovery agent."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.rwa_daily_feed_agent import (
    DEFAULT_DAILY_AGENT_CSV_PATH,
    DEFAULT_DAILY_AGENT_HISTORY_DIR,
    DEFAULT_DAILY_AGENT_JSON_PATH,
    write_daily_feed_agent_baseline,
    write_daily_feed_agent_report,
)
from src.rwa_xyz_monitor import (
    DEFAULT_RWA_XYZ_ASSET_CSV_PATH,
    DEFAULT_RWA_XYZ_REPORT_JSON_PATH,
    DEFAULT_RWA_XYZ_TOKEN_CSV_PATH,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", help="Optional local RWA.xyz monitor JSON or HTML file")
    parser.add_argument("--json-out", default=str(DEFAULT_DAILY_AGENT_JSON_PATH))
    parser.add_argument("--csv-out", default=str(DEFAULT_DAILY_AGENT_CSV_PATH))
    parser.add_argument("--history-dir", default=str(DEFAULT_DAILY_AGENT_HISTORY_DIR))
    parser.add_argument("--refresh-json-out", default=str(DEFAULT_RWA_XYZ_REPORT_JSON_PATH))
    parser.add_argument("--refresh-assets-csv-out", default=str(DEFAULT_RWA_XYZ_ASSET_CSV_PATH))
    parser.add_argument("--refresh-tokens-csv-out", default=str(DEFAULT_RWA_XYZ_TOKEN_CSV_PATH))
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument(
        "--reconcile-canonical",
        action="store_true",
        help=(
            "Rebuild a verified baseline from the existing canonical monitor artifact without "
            "claiming a historical daily diff"
        ),
    )
    args = parser.parse_args()

    if args.reconcile_canonical:
        if args.input:
            parser.error("--input cannot be combined with --reconcile-canonical")
        report = write_daily_feed_agent_baseline(
            json_path=args.json_out,
            csv_path=args.csv_out,
            history_dir=args.history_dir,
            current_report_path=args.refresh_json_out,
        )
    else:
        report = write_daily_feed_agent_report(
            input_path=args.input,
            json_path=args.json_out,
            csv_path=args.csv_out,
            history_dir=args.history_dir,
            refresh_json_path=args.refresh_json_out,
            refresh_asset_csv_path=args.refresh_assets_csv_out,
            refresh_token_csv_path=args.refresh_tokens_csv_out,
            timeout=args.timeout,
        )
    summary = report["summary"]
    print(
        "daily RWA feed agent complete: "
        f"{summary['alert_level']}, "
        f"{summary['new_asset_count']} new assets, "
        f"{summary['new_token_count']} new tokens, "
        f"{summary['new_p0_token_count']} P0 token actions, "
        f"{summary['current_asset_count']} total assets, "
        f"{summary['current_token_count']} total tokens"
    )


if __name__ == "__main__":
    main()
