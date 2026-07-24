#!/usr/bin/env python3
"""Generate RWA discovery blocker mitigation reports."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.rwa_discovery_mitigation import (  # noqa: E402
    DEFAULT_MITIGATION_CSV_PATH,
    DEFAULT_MITIGATION_JSON_PATH,
    DEFAULT_REPORTS_DIR,
    write_discovery_mitigation_reports,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", default=str(DEFAULT_MITIGATION_JSON_PATH))
    parser.add_argument("--csv-out", default=str(DEFAULT_MITIGATION_CSV_PATH))
    parser.add_argument("--reports-dir", default=str(DEFAULT_REPORTS_DIR))
    parser.add_argument(
        "--exclude-tokenized-stocks",
        action="store_true",
        help="Exclude tokenized stock/xStock rows from the mitigation counts.",
    )
    args = parser.parse_args()

    plan = write_discovery_mitigation_reports(
        json_path=args.json_out,
        csv_path=args.csv_out,
        reports_dir=args.reports_dir,
        exclude_tokenized_stocks=args.exclude_tokenized_stocks,
    )
    summary = plan["summary"]
    print(
        "wrote RWA discovery mitigation reports: "
        f"{summary['open_issue_count']} open blocker groups, "
        f"{summary['critical_open_issue_count']} critical, "
        f"{summary['blocked_from_production']} feeds blocked"
    )


if __name__ == "__main__":
    main()
