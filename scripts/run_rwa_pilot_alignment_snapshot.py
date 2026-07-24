#!/usr/bin/env python3
"""Capture the three-feed RWA pilot and compare it with Blocksize references.

This is point-in-time technical evidence. It never completes an independence,
rights, sustained-window, or production-promotion gate.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from scripts.run_rwa_growth_pilot import PILOT_FEEDS, capture_pilot
from src.blocksize_client import BlocksizeClient
from src.rwa_adapters import build_default_registry
from src.rwa_blocksize_benchmark import compare_observation_to_blocksize


BENCHMARK_SPECS = {
    "aapl_hyperliquid_spot": {
        "service": "bidask",
        "symbol": "AAPL",
        "relationship": "direct_equity_underlying_proxy_usdc_vs_usd",
        "max_timestamp_gap_seconds": 90,
        "limitations": "Tokenized AAPL/USDC can diverge from the underlying equity and USDC/USD.",
    },
    "paxg_uniswap_ethereum": {
        "service": "metal",
        "symbol": "XAUUSD",
        "relationship": "gold_spot_proxy_for_paxg_token",
        "max_timestamp_gap_seconds": 30,
        "limitations": "XAU/USD is not PAXG; issuer, tokenization, venue, and USDC basis remain.",
    },
    "eurc_aerodrome_base": {
        "service": "fx",
        "symbol": "EURUSD",
        "relationship": "eurusd_proxy_for_eurc_usdc",
        "max_timestamp_gap_seconds": 30,
        "limitations": "EUR/USD is not EURC/USDC; both stablecoins can trade away from fiat parities.",
    },
}


def _snapshot(
    *,
    service: str,
    symbol: str,
    endpoint: str,
    timestamp: Any,
    value: float,
    data: dict[str, Any],
) -> dict[str, Any]:
    return {
        "status": "ok",
        "service": service,
        "symbol": symbol,
        "endpoint": endpoint,
        "timestamp": timestamp.isoformat() if hasattr(timestamp, "isoformat") else timestamp,
        "value": float(value),
        "data": data,
    }


def _parse_timestamp(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


async def capture_blocksize_benchmarks(client: BlocksizeClient) -> dict[str, dict[str, Any]]:
    """Fetch one timestamped Blocksize reference for every pilot feed."""
    rows: dict[str, dict[str, Any]] = {}
    for pilot_id, spec in BENCHMARK_SPECS.items():
        try:
            if spec["service"] == "bidask":
                item = await client.get_bidask_snapshot(spec["symbol"])
                mid = (float(item.bid) + float(item.ask)) / 2
                rows[pilot_id] = _snapshot(
                    service="bidask",
                    symbol=spec["symbol"],
                    endpoint="bidask_getSnapshot",
                    timestamp=item.timestamp,
                    value=mid,
                    data={"bid": item.bid, "ask": item.ask, "mid": mid},
                )
            elif spec["service"] == "metal":
                item = await client.get_metal_price(spec["symbol"])
                rows[pilot_id] = _snapshot(
                    service="metal",
                    symbol=spec["symbol"],
                    endpoint="bidask_getSnapshot",
                    timestamp=item.timestamp,
                    value=item.price,
                    data={"price": item.price, "currency": item.currency},
                )
            elif spec["service"] == "fx":
                item = await client.get_fx_rate(spec["symbol"])
                rows[pilot_id] = _snapshot(
                    service="fx",
                    symbol=spec["symbol"],
                    endpoint="bidask_getSnapshot",
                    timestamp=item.timestamp,
                    value=item.mid,
                    data={"bid": item.bid, "ask": item.ask, "mid": item.mid},
                )
        except Exception as exc:
            rows[pilot_id] = {
                "status": "error",
                "service": spec["service"],
                "symbol": spec["symbol"],
                "error_type": type(exc).__name__,
                "message": str(exc)[:1000],
            }
    return rows


def evaluate_alignment(
    captures: list[dict[str, Any]],
    benchmarks: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Build an honest, non-promotional point-in-time alignment report."""
    rows = []
    for feed in PILOT_FEEDS:
        pilot_id = str(feed["pilot_id"])
        capture = next((row for row in captures if row.get("pilot_id") == pilot_id), None)
        benchmark = benchmarks.get(pilot_id)
        spec = BENCHMARK_SPECS[pilot_id]
        base = {
            "pilot_id": pilot_id,
            "symbol": feed["symbol"],
            "venue": feed["venue"],
            "source_lane": feed["source_lane"],
            "benchmark_service": spec["service"],
            "benchmark_symbol": spec["symbol"],
            "benchmark_relationship": spec["relationship"],
            "max_timestamp_gap_seconds": spec["max_timestamp_gap_seconds"],
            "limitations": spec["limitations"],
            "production_promoted": False,
        }
        if not capture or capture.get("status") != "ok":
            rows.append(
                {
                    **base,
                    "status": "error",
                    "error": "pilot capture unavailable",
                    "capture": capture,
                }
            )
            continue
        if not benchmark or benchmark.get("status") != "ok":
            rows.append(
                {
                    **base,
                    "status": "error",
                    "error": "Blocksize benchmark unavailable",
                    "benchmark": benchmark,
                }
            )
            continue
        try:
            comparison = compare_observation_to_blocksize(
                capture["raw_observation"],
                benchmark,
            )
        except (TypeError, ValueError) as exc:
            rows.append({**base, "status": "error", "error": str(exc)})
            continue
        capture_timestamp = _parse_timestamp(capture["raw_observation"].get("timestamp"))
        benchmark_timestamp = _parse_timestamp(benchmark.get("timestamp"))
        timestamp_gap_seconds = (
            abs((capture_timestamp - benchmark_timestamp).total_seconds())
            if capture_timestamp is not None and benchmark_timestamp is not None
            else None
        )
        timestamp_alignment_pass = (
            timestamp_gap_seconds is not None
            and timestamp_gap_seconds <= float(spec["max_timestamp_gap_seconds"])
        )
        rows.append(
            {
                **base,
                "status": "ok",
                "capture_checked_at": capture.get("checked_at"),
                "capture_checks": capture.get("checks"),
                "comparison": comparison,
                "timestamp_alignment": {
                    "capture_timestamp": (
                        capture_timestamp.isoformat() if capture_timestamp else None
                    ),
                    "benchmark_timestamp": (
                        benchmark_timestamp.isoformat() if benchmark_timestamp else None
                    ),
                    "gap_seconds": timestamp_gap_seconds,
                    "maximum_gap_seconds": spec["max_timestamp_gap_seconds"],
                    "pass": timestamp_alignment_pass,
                },
                "evidence_decision": (
                    comparison["decision"]
                    if timestamp_alignment_pass
                    else "not_timestamp_aligned"
                ),
                "benchmark": benchmark,
            }
        )

    successful = [row for row in rows if row["status"] == "ok"]
    aligned = [
        row
        for row in successful
        if row.get("timestamp_alignment", {}).get("pass") is True
    ]
    evidence_decisions = Counter(
        str(row["evidence_decision"])
        for row in successful
    )
    return {
        "product": "rwa_pilot_alignment_snapshot",
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "point_in_time_evidence",
        "summary": {
            "feeds_attempted": len(rows),
            "comparisons_succeeded": len(successful),
            "comparisons_failed": len(rows) - len(successful),
            "timestamp_aligned_comparisons": len(aligned),
            "timestamp_misaligned_comparisons": len(successful) - len(aligned),
            "evidence_decisions": dict(sorted(evidence_decisions.items())),
        },
        "gate_assessment": {
            "point_in_time_alignment_observed": len(aligned) == len(PILOT_FEEDS),
            "sustained_alignment_window_complete": False,
            "benchmark_source_independence_confirmed": False,
            "benchmark_rights_confirmed": False,
            "independent_benchmark_alignment_complete": False,
            "production_promotion_allowed": False,
        },
        "policy": {
            "automatic_promotion": False,
            "production_promoted_feed_count": 0,
            "pilot_runtime_tiingo_dependency": False,
            "benchmark_note": (
                "Blocksize references are comparison inputs. Their upstream lineage and rights "
                "must be confirmed before they can satisfy an independent benchmark gate."
            ),
        },
        "rows": rows,
        "next_required_evidence": [
            "Repeat timestamp-aligned comparisons across the full monitoring window.",
            "Replace or supplement proxies with directly matched, independently licensed references.",
            "Document benchmark lineage, independence, rights, and redistribution terms.",
            "Keep every feed candidate-only until depth/manipulation and human approval gates pass.",
        ],
    }


def persist_alignment_report(
    report: dict[str, Any],
    *,
    history_path: Path,
    latest_path: Path,
) -> dict[str, str]:
    """Append one replayable alignment cycle and write its latest status."""
    history_path.parent.mkdir(parents=True, exist_ok=True)
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(report, sort_keys=True, default=str) + "\n")
    latest_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return {
        "history_path": str(history_path),
        "latest_path": str(latest_path),
    }


async def run(timeout: float) -> dict[str, Any]:
    load_dotenv()
    registry = build_default_registry()
    client = BlocksizeClient(timeout=timeout)
    try:
        captures, benchmarks = await asyncio.gather(
            capture_pilot(registry, timeout_seconds=timeout),
            capture_blocksize_benchmarks(client),
        )
        return evaluate_alignment(captures, benchmarks)
    finally:
        await client.close()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/agentic_marketing/rwa_pilot_alignment_latest.json"),
    )
    parser.add_argument("--require-comparisons", type=int, default=0)
    args = parser.parse_args()

    report = asyncio.run(run(max(1.0, args.timeout)))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    if report["summary"]["comparisons_succeeded"] < max(0, args.require_comparisons):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
