#!/usr/bin/env python3
"""Generate Solana RWA pool allowlist evidence from Jupiter routes and RPC state."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.rwa_solana_pool_discovery import (  # noqa: E402
    DEFAULT_JUPITER_ROUTE_ALLOWLIST_PATH,
    DEFAULT_SOLANA_POOL_ALLOWLIST_CSV_PATH,
    DEFAULT_SOLANA_POOL_ALLOWLIST_JSON_PATH,
    write_solana_pool_allowlist_reports,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", default=str(DEFAULT_SOLANA_POOL_ALLOWLIST_JSON_PATH))
    parser.add_argument("--csv-out", default=str(DEFAULT_SOLANA_POOL_ALLOWLIST_CSV_PATH))
    parser.add_argument("--route-path", default=str(DEFAULT_JUPITER_ROUTE_ALLOWLIST_PATH))
    parser.add_argument(
        "--no-rpc",
        action="store_true",
        help="Derive pool IDs from route plans only and skip Solana RPC account-state reads.",
    )
    args = parser.parse_args()

    report = write_solana_pool_allowlist_reports(
        json_path=args.json_out,
        csv_path=args.csv_out,
        route_path=args.route_path,
        rpc_url="" if args.no_rpc else None,
    )
    summary = report["summary"]
    print(
        "wrote RWA Solana pool allowlist reports: "
        f"{summary['pool_count']} pools, "
        f"{summary['rpc_state_captured']} RPC states captured, "
        f"{summary['fee_tiers_missing']} missing fee tiers"
    )


if __name__ == "__main__":
    main()
