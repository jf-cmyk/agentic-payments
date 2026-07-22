#!/usr/bin/env python3
"""Generate RWA feed discovery and promotion-gate reports."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.rwa_feed_discovery import (  # noqa: E402
    DEFAULT_DISCOVERY_CSV_PATH,
    DEFAULT_DISCOVERY_JSON_PATH,
    DEFAULT_REPORTS_DIR,
    write_feed_discovery_reports,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", default=str(DEFAULT_DISCOVERY_JSON_PATH))
    parser.add_argument("--csv-out", default=str(DEFAULT_DISCOVERY_CSV_PATH))
    parser.add_argument("--reports-dir", default=str(DEFAULT_REPORTS_DIR))
    parser.add_argument(
        "--exclude-tokenized-stocks",
        action="store_true",
        help="Exclude tokenized stock/xStock rows from the discovery audit.",
    )
    args = parser.parse_args()

    report = write_feed_discovery_reports(
        json_path=args.json_out,
        csv_path=args.csv_out,
        reports_dir=args.reports_dir,
        exclude_tokenized_stocks=args.exclude_tokenized_stocks,
    )
    summary = report["summary"]
    print(
        "wrote RWA feed discovery reports: "
        f"{summary['feed_count']} feeds, "
        f"{summary['production_promoted']} production-promoted, "
        f"{summary['blocked_from_production']} blocked/candidate"
    )


if __name__ == "__main__":
    main()
