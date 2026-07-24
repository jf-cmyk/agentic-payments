#!/usr/bin/env python3
"""Generate RWA route/pool replay inventory reports."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.rwa_replay_inventory import (  # noqa: E402
    DEFAULT_REPLAY_INVENTORY_CSV_PATH,
    DEFAULT_REPLAY_INVENTORY_JSON_PATH,
    write_route_pool_replay_inventory_reports,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", default=str(DEFAULT_REPLAY_INVENTORY_JSON_PATH))
    parser.add_argument("--csv-out", default=str(DEFAULT_REPLAY_INVENTORY_CSV_PATH))
    args = parser.parse_args()

    inventory = write_route_pool_replay_inventory_reports(
        json_path=args.json_out,
        csv_path=args.csv_out,
    )
    summary = inventory["summary"]
    print(
        "wrote RWA replay inventory reports: "
        f"{summary['candidate_count']} candidates, "
        f"{summary['replay_ready']} replay-ready, "
        f"{summary['missing_or_incomplete_replay']} missing/incomplete"
    )


if __name__ == "__main__":
    main()
