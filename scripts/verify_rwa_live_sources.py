#!/usr/bin/env python3
"""Run a bounded, read-only live check across representative RWA source lanes.

The command never persists observations and never prints credentials. Its JSON
output is intended to distinguish a reachable candidate source from a feed that
has passed the separate production-promotion gates.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


DEFAULT_PROBES = (
    ("hyperliquid_rwa_spot", "AAPL/USDC", "venue_api_order_book"),
    ("gains", "AAPL/USD", "venue_api_reference_stream"),
    ("ostium", "XAU/USD", "venue_api_synthetic_market"),
    ("jupiter_router", "AAPLX/USD", "solana_router_quote"),
    ("raydium_clmm", "AAPLX/USD", "solana_route_filtered_quote"),
    ("uniswap_v3_v4", "PAXG/USDC", "evm_rpc_pool_state"),
    ("aerodrome_slipstream", "EURC/USDC", "evm_rpc_pool_state"),
)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _summary(observation: dict[str, Any], *, checked_at: datetime) -> dict[str, Any]:
    timestamp = _parse_time(observation.get("timestamp"))
    metadata = observation.get("metadata") if isinstance(observation.get("metadata"), dict) else {}
    levels = observation.get("levels") if isinstance(observation.get("levels"), list) else []
    bid = _finite(observation.get("bid"))
    ask = _finite(observation.get("ask"))
    mid = _finite(observation.get("mid"))
    if mid is None and bid is not None and ask is not None:
        mid = (bid + ask) / 2
    route_labels: list[str] = []
    for quote_key in ("bid_quote", "ask_quote"):
        quote = metadata.get(quote_key)
        if not isinstance(quote, dict):
            continue
        for route in quote.get("route_plan") or []:
            if not isinstance(route, dict):
                continue
            swap_info = route.get("swapInfo") if isinstance(route.get("swapInfo"), dict) else route
            label = swap_info.get("label") or swap_info.get("ammName") or swap_info.get("name")
            if label:
                route_labels.append(str(label))
    return {
        "symbol": observation.get("symbol"),
        "source_type": observation.get("source_type"),
        "timestamp": observation.get("timestamp"),
        "freshness_ms": (
            max(0.0, (checked_at - timestamp.astimezone(UTC)).total_seconds() * 1000)
            if timestamp is not None
            else None
        ),
        "bid": bid,
        "ask": ask,
        "mid": mid,
        "level_count": len(levels),
        "endpoint": metadata.get("endpoint"),
        "chain": metadata.get("chain"),
        "block_number": metadata.get("block_number"),
        "book_time_ms": metadata.get("book_time_ms"),
        "context_slot": metadata.get("context_slot"),
        "route_labels": sorted(set(route_labels)),
        "production_gate": metadata.get("production_gate") or metadata.get("promotion_gate"),
    }


async def _run_probe(
    registry: Any,
    *,
    venue: str,
    symbol: str,
    source_lane: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    started_at = datetime.now(UTC)
    result: dict[str, Any] = {
        "venue": venue,
        "requested_symbol": symbol,
        "source_lane": source_lane,
        "started_at": started_at.isoformat(),
        "production_promoted": False,
    }
    try:
        adapter = registry.get(venue)
        observation = await asyncio.wait_for(
            adapter.fetch_bidask(symbol),
            timeout=timeout_seconds,
        )
        checked_at = datetime.now(UTC)
        result.update(
            {
                "checked_at": checked_at.isoformat(),
                "status": "ok",
                "observation": _summary(observation, checked_at=checked_at),
            }
        )
    except Exception as exc:  # The report must retain source-specific blockers.
        result.update(
            {
                "checked_at": datetime.now(UTC).isoformat(),
                "status": "error",
                "error_type": type(exc).__name__,
                "message": str(exc)[:1000],
            }
        )
    return result


async def _run(timeout_seconds: float) -> dict[str, Any]:
    # Load credentials before constructing adapters; values are never emitted.
    load_dotenv()
    from src.rwa_adapters import build_default_registry

    registry = build_default_registry()
    results = []
    for venue, symbol, source_lane in DEFAULT_PROBES:
        results.append(
            await _run_probe(
                registry,
                venue=venue,
                symbol=symbol,
                source_lane=source_lane,
                timeout_seconds=timeout_seconds,
            )
        )
    successes = [row for row in results if row["status"] == "ok"]
    return {
        "product": "rwa_live_source_readiness_check",
        "generated_at": datetime.now(UTC).isoformat(),
        "policy": {
            "read_only": True,
            "persistence": False,
            "candidate_not_promotion": (
                "A successful point-in-time probe proves reachable working data only; it does not "
                "satisfy continuous freshness, replay, manipulation, benchmark, consensus, or signoff gates."
            ),
        },
        "summary": {
            "probe_count": len(results),
            "success_count": len(successes),
            "failure_count": len(results) - len(successes),
            "successful_source_lanes": sorted({row["source_lane"] for row in successes}),
            "tiingo_runtime_dependency": False,
        },
        "results": results,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=20.0, help="Per-source timeout in seconds")
    parser.add_argument(
        "--require-successes",
        type=int,
        default=0,
        help="Exit non-zero unless at least this many representative probes succeed",
    )
    parser.add_argument("--output", type=Path, help="Optional JSON evidence output path")
    args = parser.parse_args()
    report = asyncio.run(_run(max(1.0, args.timeout)))
    serialized = json.dumps(report, indent=2, sort_keys=True) + "\n"
    print(serialized, end="")
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    if report["summary"]["success_count"] < max(0, args.require_successes):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
