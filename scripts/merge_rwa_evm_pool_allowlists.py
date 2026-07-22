#!/usr/bin/env python3
"""Merge RWA EVM pool allowlist JSON files into one probeable allowlist."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def _key(row: dict[str, Any]) -> tuple[str, str, str, str]:
    return (
        str(row.get("venue") or ""),
        str(row.get("chain") or row.get("chain_id") or ""),
        str(row.get("pool_address") or row.get("pool_id") or "").lower(),
        str(row.get("base_token") or "").lower(),
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", action="append", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    pools_by_key: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    source_counts: Counter[str] = Counter()
    for path in args.input:
        payload = json.loads(path.read_text(encoding="utf-8"))
        pools = payload.get("pools") if isinstance(payload.get("pools"), list) else []
        source_counts[str(path)] = len(pools)
        for row in pools:
            if not isinstance(row, dict):
                continue
            key = _key(row)
            existing = pools_by_key.get(key)
            if existing is None:
                pools_by_key[key] = row
                continue
            existing_liquidity = float(existing.get("liquidity_usd") or 0)
            row_liquidity = float(row.get("liquidity_usd") or 0)
            if row_liquidity > existing_liquidity:
                pools_by_key[key] = row

    pools = sorted(
        pools_by_key.values(),
        key=lambda row: (
            str(row.get("venue") or ""),
            str(row.get("asset_id") or ""),
            str(row.get("base_token") or ""),
        ),
    )
    report = {
        "product": "rwa_evm_pool_allowlist_merged",
        "generated_at": datetime.now(UTC).isoformat(),
        "inputs": {path: count for path, count in sorted(source_counts.items())},
        "summary": {
            "pool_count": len(pools),
            "by_venue": dict(sorted(Counter(str(row.get("venue") or "") for row in pools).items())),
            "by_chain": dict(sorted(Counter(str(row.get("chain") or row.get("chain_id") or "") for row in pools).items())),
        },
        "pools": pools,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(report["summary"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
