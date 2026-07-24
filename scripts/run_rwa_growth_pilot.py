#!/usr/bin/env python3
"""Capture and score the three-feed RWA growth pilot without auto-promotion."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


PILOT_FEEDS = (
    {
        "pilot_id": "aapl_hyperliquid_spot",
        "venue": "hyperliquid_rwa_spot",
        "symbol": "AAPL/USDC",
        "source_lane": "venue_api_order_book",
        "freshness_limit_seconds": 90,
    },
    {
        "pilot_id": "paxg_uniswap_ethereum",
        "venue": "uniswap_v3_v4",
        "symbol": "PAXG/USDC",
        "source_lane": "ethereum_rpc_pool_state",
        "freshness_limit_seconds": 30,
    },
    {
        "pilot_id": "eurc_aerodrome_base",
        "venue": "aerodrome_slipstream",
        "symbol": "EURC/USDC",
        "source_lane": "base_rpc_pool_state",
        "freshness_limit_seconds": 30,
    },
)

MINIMUM_WINDOW_DAYS = 14
MINIMUM_SAMPLES_PER_FEED = 672
MINIMUM_SUCCESS_RATE = 0.99
MINIMUM_FRESHNESS_RATE = 0.99


def _parse_timestamp(value: Any) -> datetime | None:
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


def _observation_checks(
    observation: dict[str, Any],
    *,
    checked_at: datetime,
    freshness_limit_seconds: float,
) -> dict[str, Any]:
    timestamp = _parse_timestamp(observation.get("timestamp"))
    freshness_seconds = (
        max(0.0, (checked_at - timestamp.astimezone(UTC)).total_seconds())
        if timestamp is not None
        else None
    )
    bid = _finite(observation.get("bid"))
    ask = _finite(observation.get("ask"))
    bidask_sane = bid is not None and ask is not None and 0 < bid <= ask
    return {
        "freshness_seconds": freshness_seconds,
        "freshness_limit_seconds": freshness_limit_seconds,
        "freshness_pass": freshness_seconds is not None and freshness_seconds <= freshness_limit_seconds,
        "bidask_sanity_pass": bidask_sane,
    }


async def _capture_feed(registry: Any, feed: dict[str, Any], timeout_seconds: float) -> dict[str, Any]:
    started_at = datetime.now(UTC)
    record = {
        **feed,
        "started_at": started_at.isoformat(),
        "production_promoted": False,
    }
    try:
        adapter = registry.get(feed["venue"])
        observation = await asyncio.wait_for(
            adapter.fetch_bidask(feed["symbol"]),
            timeout=timeout_seconds,
        )
        checked_at = datetime.now(UTC)
        record.update(
            {
                "checked_at": checked_at.isoformat(),
                "status": "ok",
                "checks": _observation_checks(
                    observation,
                    checked_at=checked_at,
                    freshness_limit_seconds=float(feed["freshness_limit_seconds"]),
                ),
                "raw_observation": observation,
            }
        )
    except Exception as exc:
        record.update(
            {
                "checked_at": datetime.now(UTC).isoformat(),
                "status": "error",
                "error_type": type(exc).__name__,
                "message": str(exc)[:1000],
                "checks": {"freshness_pass": False, "bidask_sanity_pass": False},
            }
        )
    return record


def _load_history(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def evaluate_history(rows: list[dict[str, Any]], *, now: datetime | None = None) -> dict[str, Any]:
    now = now or datetime.now(UTC)
    feed_rows = []
    all_source_gates_pass = True
    for feed in PILOT_FEEDS:
        matching = [row for row in rows if row.get("pilot_id") == feed["pilot_id"]]
        timestamps = [
            timestamp
            for row in matching
            if (timestamp := _parse_timestamp(row.get("checked_at"))) is not None
        ]
        successful = [row for row in matching if row.get("status") == "ok"]
        fresh = [row for row in successful if row.get("checks", {}).get("freshness_pass") is True]
        sane = [row for row in successful if row.get("checks", {}).get("bidask_sanity_pass") is True]
        window_days = (
            max(0.0, (max(timestamps) - min(timestamps)).total_seconds() / 86400)
            if len(timestamps) >= 2
            else 0.0
        )
        sample_count = len(matching)
        success_rate = len(successful) / sample_count if sample_count else None
        freshness_rate = len(fresh) / len(successful) if successful else None
        sanity_rate = len(sane) / len(successful) if successful else None
        source_gates = {
            "minimum_window_pass": window_days >= MINIMUM_WINDOW_DAYS,
            "minimum_samples_pass": sample_count >= MINIMUM_SAMPLES_PER_FEED,
            "success_rate_pass": success_rate is not None and success_rate >= MINIMUM_SUCCESS_RATE,
            "freshness_rate_pass": freshness_rate is not None and freshness_rate >= MINIMUM_FRESHNESS_RATE,
            "bidask_sanity_pass": sanity_rate == 1.0,
        }
        source_ready = all(source_gates.values())
        all_source_gates_pass = all_source_gates_pass and source_ready
        feed_rows.append(
            {
                **feed,
                "sample_count": sample_count,
                "successful_samples": len(successful),
                "window_days": round(window_days, 4),
                "success_rate": success_rate,
                "freshness_rate": freshness_rate,
                "bidask_sanity_rate": sanity_rate,
                "source_monitoring_gates": source_gates,
                "source_monitoring_ready": source_ready,
                "last_checked_at": max(timestamps).isoformat() if timestamps else None,
            }
        )

    non_monitoring_gates = {
        "independent_benchmark_alignment": False,
        "manipulation_and_depth_review": False,
        "source_independence_review": False,
        "rights_and_redistribution_signoff": False,
        "human_promotion_approval": False,
    }
    return {
        "product": "rwa_growth_pilot_readiness",
        "generated_at": now.isoformat(),
        "status": "candidate_monitoring",
        "production_promoted_feed_count": 0,
        "source_monitoring_ready": all_source_gates_pass,
        "promotion_ready": all_source_gates_pass and all(non_monitoring_gates.values()),
        "thresholds": {
            "minimum_window_days": MINIMUM_WINDOW_DAYS,
            "minimum_samples_per_feed": MINIMUM_SAMPLES_PER_FEED,
            "minimum_success_rate": MINIMUM_SUCCESS_RATE,
            "minimum_freshness_rate": MINIMUM_FRESHNESS_RATE,
        },
        "non_monitoring_gates": non_monitoring_gates,
        "feeds": feed_rows,
        "policy": {
            "automatic_promotion": False,
            "catalog_boundary": "Pilot observations remain candidate data until every monitoring and non-monitoring gate passes and a human approves promotion.",
            "tiingo_runtime_dependency": False,
        },
    }


async def _run(timeout_seconds: float) -> list[dict[str, Any]]:
    load_dotenv()
    from src.rwa_adapters import build_default_registry

    registry = build_default_registry()
    return await capture_pilot(registry, timeout_seconds=timeout_seconds)


async def capture_pilot(registry: Any, *, timeout_seconds: float = 20.0) -> list[dict[str, Any]]:
    """Capture one read-only observation for every configured pilot feed."""
    return [
        await _capture_feed(registry, feed, timeout_seconds)
        for feed in PILOT_FEEDS
    ]


def persist_capture(
    history_path: Path,
    captures: list[dict[str, Any]],
    *,
    status_output: Path | None = None,
    observation_store: Any | None = None,
    alignment_report: dict[str, Any] | None = None,
    depth_report: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Append replayable observations and write the latest readiness status."""
    history = _load_history(history_path)
    history_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", encoding="utf-8") as handle:
        for row in captures:
            handle.write(json.dumps(row, sort_keys=True, default=str) + "\n")
    report = evaluate_history([*history, *captures])
    report["current_capture"] = {
        "attempted": len(captures),
        "succeeded": sum(row.get("status") == "ok" for row in captures),
        "rows": captures,
    }
    ledger_rows: list[dict[str, Any]] = []
    alignment_by_pilot = {
        str(row.get("pilot_id")): row
        for row in ((alignment_report or {}).get("rows") or [])
        if isinstance(row, dict) and row.get("pilot_id")
    }
    depth_by_pilot = {
        str(row.get("pilot_id")): row
        for row in ((depth_report or {}).get("rows") or [])
        if isinstance(row, dict) and row.get("pilot_id")
    }
    if observation_store is not None:
        for capture in captures:
            observation = capture.get("raw_observation")
            if capture.get("status") != "ok" or not isinstance(observation, dict):
                continue
            alignment = alignment_by_pilot.get(str(capture.get("pilot_id")), {})
            depth_evidence = depth_by_pilot.get(str(capture.get("pilot_id")), {})
            benchmark_evidence = {
                key: alignment.get(key)
                for key in (
                    "status",
                    "benchmark_service",
                    "benchmark_symbol",
                    "benchmark_relationship",
                    "limitations",
                    "comparison",
                    "timestamp_alignment",
                    "evidence_decision",
                    "benchmark",
                    "error",
                )
                if alignment.get(key) is not None
            }
            ledger_rows.append(
                observation_store.store_observation(
                    {
                        "created_at": capture.get("checked_at"),
                        "symbol": capture.get("symbol"),
                        "venue": capture.get("venue"),
                        "asset_class": observation.get("asset_class"),
                        "source_type": capture.get("source_lane") or observation.get("source_type"),
                        "raw_payload": observation,
                        "normalized_observation": observation,
                        "realtime_quality": {
                            **capture.get("checks", {}),
                            "liquidity_depth_evidence": depth_evidence,
                        },
                        "blocksize_benchmark": benchmark_evidence,
                        "promotion": {
                            "production_promoted": False,
                            "status": "candidate_monitoring",
                        },
                        "metadata": {
                            "pilot_id": capture.get("pilot_id"),
                            "source_lane": capture.get("source_lane"),
                            "replay_history_path": str(history_path),
                        },
                    }
                )
            )
    report["current_capture"]["ledger_persisted"] = len(ledger_rows)
    report["current_capture"]["ledger_observation_ids"] = [
        row["observation_id"] for row in ledger_rows
    ]
    if alignment_report is not None:
        report["current_capture"]["benchmark_alignment"] = alignment_report.get(
            "summary", {}
        )
        report["benchmark_alignment_latest"] = {
            "generated_at": alignment_report.get("generated_at"),
            "status": alignment_report.get("status"),
            "summary": alignment_report.get("summary", {}),
            "gate_assessment": alignment_report.get("gate_assessment", {}),
        }
    if depth_report is not None:
        report["current_capture"]["depth_and_manipulation_evidence"] = depth_report.get(
            "summary", {}
        )
        report["depth_and_manipulation_latest"] = {
            "generated_at": depth_report.get("generated_at"),
            "status": depth_report.get("status"),
            "summary": depth_report.get("summary", {}),
            "gate_assessment": depth_report.get("gate_assessment", {}),
        }
    if observation_store is not None:
        report["observation_ledger"] = observation_store.summary()
    if status_output is not None:
        status_output.parent.mkdir(parents=True, exist_ok=True)
        status_output.write_text(
            json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
            encoding="utf-8",
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument(
        "--history",
        type=Path,
        default=Path(os.environ.get("RWA_GROWTH_PILOT_HISTORY_PATH", "reports/rwa_growth_pilot_history.jsonl")),
    )
    parser.add_argument("--status-output", type=Path)
    parser.add_argument("--no-append", action="store_true")
    parser.add_argument("--require-successes", type=int, default=0)
    args = parser.parse_args()

    captures = asyncio.run(_run(max(1.0, args.timeout)))
    if args.no_append:
        report = evaluate_history([*_load_history(args.history), *captures])
        report["current_capture"] = {
            "attempted": len(captures),
            "succeeded": sum(row.get("status") == "ok" for row in captures),
            "rows": captures,
        }
    else:
        report = persist_capture(args.history, captures, status_output=args.status_output)
    serialized = json.dumps(report, indent=2, sort_keys=True, default=str) + "\n"
    print(serialized, end="")
    if args.status_output and args.no_append:
        args.status_output.parent.mkdir(parents=True, exist_ok=True)
        args.status_output.write_text(serialized, encoding="utf-8")
    if report["current_capture"]["succeeded"] < max(0, args.require_successes):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
