#!/usr/bin/env python3
"""Export ranked zero-result symbol demand from privacy-safe usage telemetry."""

from __future__ import annotations

import argparse
import csv
import json
import re
from datetime import UTC, datetime
from pathlib import Path

from src.observability import UsageEventStore
from src.public_metadata import DATA_PACKAGES


ROOT = Path(__file__).resolve().parents[1]
SYNTHETIC_PREFIXES = ("TEST", "MOCK", "DEMO", "SAMPLE")


def normalized_symbol(value: str) -> str:
    return re.sub(r"[^A-Z0-9]", "", value.upper())


def supported_example_symbols() -> set[str]:
    symbols: set[str] = set()
    common_quotes = ("USDT", "USDC", "USD", "EUR", "BTC", "ETH")
    for package in DATA_PACKAGES:
        for example in package.get("examples", []):
            normalized = normalized_symbol(str(example))
            if not normalized:
                continue
            symbols.add(normalized)
            for quote in common_quotes:
                if normalized.endswith(quote) and len(normalized) > len(quote):
                    symbols.add(normalized[: -len(quote)])
                    break
    return symbols


def classify_opportunity(row: dict[str, object]) -> dict[str, object]:
    symbol = str(row.get("symbol") or "")
    request_count = int(row.get("request_count") or 0)
    surfaces = row.get("surfaces") if isinstance(row.get("surfaces"), list) else []
    normalized = normalized_symbol(symbol)
    if normalized.startswith(SYNTHETIC_PREFIXES) or re.fullmatch(
        r"([A-Z0-9])\1{3,}", normalized
    ):
        classification = "synthetic_or_test"
        priority = "exclude"
        action = "Exclude from the product backlog and tag the generating test client."
    elif normalized in supported_example_symbols():
        classification = "known_supported_symbol"
        priority = "P0" if request_count >= 3 else "P1"
        action = "Investigate normalization, route selection, upstream readiness, or a coverage regression."
    else:
        classification = "candidate_demand"
        priority = "P1" if request_count >= 5 or len(surfaces) >= 2 else "P2"
        action = "Validate rights, provider availability, buyer intent, and unit economics before adding coverage."
    return {
        **row,
        "normalized_symbol": normalized,
        "classification": classification,
        "priority": priority,
        "recommended_action": action,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default=str(ROOT / "usage_events.db"))
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--output-dir", default=str(ROOT / "reports" / "agentic_marketing"))
    parser.add_argument(
        "--include-synthetic",
        action="store_true",
        help="Include tagged test/smoke telemetry (excluded by default).",
    )
    args = parser.parse_args()

    store = UsageEventStore(args.db)
    summary = store.summarize(
        days=args.days,
        include_synthetic=args.include_synthetic,
    )
    opportunities = summary["unsupported_symbol_opportunities"]
    triaged_rows = [classify_opportunity(row) for row in opportunities["rows"]]
    triage_summary: dict[str, int] = {}
    for row in triaged_rows:
        key = str(row["classification"])
        triage_summary[key] = triage_summary.get(key, 0) + 1
    generated_at = datetime.now(UTC).isoformat()
    payload = {
        "generated_at": generated_at,
        "window_days": args.days,
        "instrumentation_start": "Events accrue only after unsupported_symbol_request instrumentation is deployed.",
        "privacy_boundary": "Only bounded symbol-like zero-result queries are retained; arbitrary free text is excluded.",
        "telemetry_scope": summary["telemetry_scope"],
        **opportunities,
        "rows": triaged_rows,
        "triage_summary": triage_summary,
    }

    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    json_path = output_dir / "unsupported_symbol_opportunities_latest.json"
    csv_path = output_dir / "unsupported_symbol_opportunities_latest.csv"
    json_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    fieldnames = [
        "priority",
        "classification",
        "symbol",
        "normalized_symbol",
        "asset_class",
        "request_count",
        "surfaces",
        "first_seen",
        "last_seen",
        "recommended_action",
    ]
    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in triaged_rows:
            writer.writerow({**row, "surfaces": "|".join(row["surfaces"])})
    print(json.dumps({"json": str(json_path), "csv": str(csv_path), "total_requests": opportunities["total_requests"], "unique_pairs": opportunities["unique_symbol_asset_class_pairs"]}, indent=2))


if __name__ == "__main__":
    main()
