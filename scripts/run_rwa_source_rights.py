#!/usr/bin/env python3
"""Generate RWA rights-to-source reports."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.rwa_source_rights import (  # noqa: E402
    DEFAULT_SOURCE_RIGHTS_CSV_PATH,
    DEFAULT_SOURCE_RIGHTS_JSON_PATH,
    write_source_rights_reports,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", default=str(DEFAULT_SOURCE_RIGHTS_JSON_PATH))
    parser.add_argument("--csv-out", default=str(DEFAULT_SOURCE_RIGHTS_CSV_PATH))
    args = parser.parse_args()

    registry = write_source_rights_reports(json_path=args.json_out, csv_path=args.csv_out)
    summary = registry["summary"]
    print(
        "wrote RWA source-rights reports: "
        f"{summary['venue_count']} venues, "
        f"{summary['internal_benchmark_sourceable']} internal-benchmark sourceable, "
        f"{summary['production_rights_cleared']} production-rights cleared"
    )


if __name__ == "__main__":
    main()
