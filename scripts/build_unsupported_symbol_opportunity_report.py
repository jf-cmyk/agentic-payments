#!/usr/bin/env python3
"""Export ranked zero-result symbol demand from privacy-safe usage telemetry."""

from __future__ import annotations

import argparse
import csv
import json
from datetime import UTC, datetime
from pathlib import Path

from src.observability import UsageEventStore


ROOT = Path(__file__).resolve().parents[1]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(ROOT / "usage_events.db"))
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--output-dir", default=str(ROOT / "reports" / "agentic_marketing"))
    args = parser.parse_args()

    store = UsageEventStore(args.db)
    opportunities = store.summarize(days=args.days)["unsupported_symbol_opportunities"]
    generated_at = datetime.now(UTC).isoformat()
    payload = {
        "generated_at": generated_at,
        "window_days": args.days,
        "instrumentation_start": "Events accrue only after unsupported_symbol_request instrumentation is deployed.",
        "privacy_boundary": "Only bounded symbol-like zero-result queries are retained; arbitrary free text is excluded.",
        **opportunities,
    }

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "unsupported_symbol_opportunities_latest.json"
    csv_path = output_dir / "unsupported_symbol_opportunities_latest.csv"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    fieldnames = ["symbol", "asset_class", "request_count", "surfaces", "first_seen", "last_seen"]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in opportunities["rows"]:
            writer.writerow({**row, "surfaces": "|".join(row["surfaces"])})
    print(json.dumps({"json": str(json_path), "csv": str(csv_path), "total_requests": opportunities["total_requests"], "unique_pairs": opportunities["unique_symbol_asset_class_pairs"]}, indent=2))


if __name__ == "__main__":
    main()
