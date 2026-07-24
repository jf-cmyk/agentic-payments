#!/usr/bin/env python3
"""Generate the RWA production blocker-resolution ledger."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.rwa_blocker_resolution import (  # noqa: E402
    DEFAULT_BLOCKER_RESOLUTION_CSV_PATH,
    DEFAULT_BLOCKER_RESOLUTION_JSON_PATH,
    write_blocker_resolution_reports,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", default=str(DEFAULT_BLOCKER_RESOLUTION_JSON_PATH))
    parser.add_argument("--csv-out", default=str(DEFAULT_BLOCKER_RESOLUTION_CSV_PATH))
    args = parser.parse_args()

    ledger = write_blocker_resolution_reports(json_path=args.json_out, csv_path=args.csv_out)
    summary = ledger["summary"]
    print(
        "wrote RWA blocker-resolution reports: "
        f"{summary['resolved_issue_count']} resolved, "
        f"{summary['resolved_to_evidence_issue_count']} resolved-to-evidence, "
        f"{summary['partially_resolved_issue_count']} partially resolved, "
        f"{summary['externally_blocked_issue_count']} externally blocked, "
        f"{summary['blocked_from_production']} feeds still blocked"
    )


if __name__ == "__main__":
    main()
