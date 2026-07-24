#!/usr/bin/env python3
"""Generate EVM RWA pool allowlist evidence from public pair metadata."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.rwa_evm_pool_discovery import (  # noqa: E402
    DEFAULT_EVM_POOL_ALLOWLIST_CSV_PATH,
    DEFAULT_EVM_POOL_ALLOWLIST_JSON_PATH,
    write_evm_pool_allowlist_reports,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", default=str(DEFAULT_EVM_POOL_ALLOWLIST_JSON_PATH))
    parser.add_argument("--csv-out", default=str(DEFAULT_EVM_POOL_ALLOWLIST_CSV_PATH))
    args = parser.parse_args()

    report = write_evm_pool_allowlist_reports(json_path=args.json_out, csv_path=args.csv_out)
    summary = report["summary"]
    print(
        "wrote RWA EVM pool allowlist reports: "
        f"{summary['pool_count']}/{summary['candidate_count']} pools, "
        f"{summary['missing_pair_count']} missing, "
        f"{summary['block_state_captured']} block states captured"
    )


if __name__ == "__main__":
    main()
