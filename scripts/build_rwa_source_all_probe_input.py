#!/usr/bin/env python3
"""Build one probe input from current venue feeds and discovered contract pools."""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from typing import Any


FIELDS = [
    "feed_id",
    "kind",
    "asset_id",
    "asset_classes",
    "symbol",
    "venue",
    "source_type",
    "promotion_status",
    "production_promoted",
]


def _read_csv(path: Path) -> list[dict[str, Any]]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _row_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("kind") or ""),
        str(row.get("venue") or ""),
        str(row.get("symbol") or ""),
        str(row.get("feed_id") or ""),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current-feed-input", type=Path, default=Path("reports/rwa_feed_discovery.csv"))
    parser.add_argument("--contract-pool-input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for path in (args.current_feed_input, args.contract_pool_input):
        for row in _read_csv(path):
            key = _row_key(row)
            if key in seen:
                continue
            seen.add(key)
            normalized = {field: row.get(field, "") for field in FIELDS}
            if not normalized["asset_classes"]:
                normalized["asset_classes"] = json.dumps([row.get("asset_class") or "unknown"])
            rows.append(normalized)

    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDS)
        writer.writeheader()
        writer.writerows(rows)
    print(json.dumps({"rows": len(rows), "output": str(args.output)}, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
