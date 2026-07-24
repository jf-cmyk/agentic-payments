#!/usr/bin/env python3
"""Generate live Hyperliquid tradeable feed coverage from meta and spotMeta."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.rwa_coverage import _coverage_rows  # noqa: E402
from src.rwa_hyperliquid_discovery import (  # noqa: E402
    DEFAULT_HYPERLIQUID_TRADEABLE_FEEDS_CSV_PATH,
    DEFAULT_HYPERLIQUID_TRADEABLE_FEEDS_JSON_PATH,
    write_hyperliquid_tradeable_feed_reports,
)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json-out", default=str(DEFAULT_HYPERLIQUID_TRADEABLE_FEEDS_JSON_PATH))
    parser.add_argument("--csv-out", default=str(DEFAULT_HYPERLIQUID_TRADEABLE_FEEDS_CSV_PATH))
    args = parser.parse_args()

    existing_rows = [
        row
        for row in _coverage_rows()
        if row.get("venue") not in {"hyperliquid_perps", "hyperliquid_spot"}
    ]
    report = write_hyperliquid_tradeable_feed_reports(
        existing_rows=existing_rows,
        json_path=args.json_out,
        csv_path=args.csv_out,
    )
    summary = report["summary"]
    print(
        "wrote Hyperliquid tradeable feed reports: "
        f"{summary['active_perp_market_count']} active perps, "
        f"{summary['spot_pair_count']} spot pairs, "
        f"{summary['rwa_or_traditional_coverage_rows']} RWA/traditional rows, "
        f"{summary['crypto_coverage_rows']} crypto rows"
    )


if __name__ == "__main__":
    main()
