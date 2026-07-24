#!/usr/bin/env python3
"""Build an auditable, non-promotional readiness packet for the RWA pilot."""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from scripts.run_rwa_growth_pilot import PILOT_FEEDS


def _rows_by_pilot(report: dict[str, Any], key: str) -> dict[str, dict[str, Any]]:
    return {
        str(row.get("pilot_id")): row
        for row in (report.get(key) or [])
        if isinstance(row, dict) and row.get("pilot_id")
    }


def build_promotion_packet(
    monitoring: dict[str, Any],
    alignment: dict[str, Any],
    depth: dict[str, Any],
) -> dict[str, Any]:
    """Join current evidence into a per-feed gate packet that can only hold."""
    monitoring_rows = _rows_by_pilot(monitoring, "feeds")
    alignment_rows = _rows_by_pilot(alignment, "rows")
    depth_rows = _rows_by_pilot(depth, "rows")
    feeds = []
    for feed in PILOT_FEEDS:
        pilot_id = str(feed["pilot_id"])
        monitored = monitoring_rows.get(pilot_id, {})
        aligned = alignment_rows.get(pilot_id, {})
        liquidity = depth_rows.get(pilot_id, {})
        source_lane = str(feed["source_lane"])
        gates = {
            "fourteen_day_monitoring_window": bool(monitored.get("source_monitoring_ready")),
            "current_timestamp_aligned_benchmark_snapshot": (
                aligned.get("timestamp_alignment", {}).get("pass") is True
            ),
            "sustained_independent_benchmark_alignment": False,
            "organic_volume_snapshot": (
                liquidity.get("point_in_time_volume_window_observed") is True
                or isinstance(liquidity.get("organic_volume"), dict)
            ),
            "initialized_tick_replay_snapshot": (
                source_lane == "venue_api_order_book"
                or liquidity.get("point_in_time_tick_replay_observed") is True
            ),
            "required_block_depth_snapshot": (
                liquidity.get("point_in_time_quality_pass") is True
            ),
            "sustained_volume_depth_and_manipulation_review": False,
            "route_or_source_independence_review": False,
            "rights_and_redistribution_signoff": False,
            "rpc_credentials_rotated": source_lane == "venue_api_order_book",
            "human_promotion_approval": False,
        }
        blockers = [name for name, passed in gates.items() if not passed]
        next_actions = []
        if not gates["fourteen_day_monitoring_window"]:
            next_actions.append("Continue the automatic 30-minute capture window to 14 days and 672 samples.")
        if not gates["current_timestamp_aligned_benchmark_snapshot"]:
            next_actions.append("Capture a fresh timestamp-aligned Blocksize comparison.")
        if not gates["organic_volume_snapshot"]:
            next_actions.append("Capture venue-native or decoded onchain 24-hour organic volume.")
        if not gates["initialized_tick_replay_snapshot"]:
            next_actions.append("Capture initialized CLMM ticks and exact-input replay at one block.")
        if not gates["required_block_depth_snapshot"]:
            next_actions.append("Pass the $10k fill and slippage snapshot with complete captured depth.")
        if not gates["rpc_credentials_rotated"]:
            next_actions.append("Rotate and validate the EVM RPC credential outside the repository.")
        next_actions.extend(
            [
                "Complete sustained manipulation, depth, and volume review over the monitoring window.",
                "Confirm independent-source lineage and redistribution rights.",
                "Obtain named human production-promotion approval after every gate passes.",
            ]
        )
        feeds.append(
            {
                **feed,
                "decision": "hold_candidate",
                "production_promoted": False,
                "passed_gate_count": sum(gates.values()),
                "required_gate_count": len(gates),
                "gates": gates,
                "blocking_gates": blockers,
                "next_actions": next_actions,
                "evidence": {
                    "monitoring": monitored,
                    "alignment": aligned,
                    "volume_depth_and_replay": liquidity,
                },
            }
        )
    return {
        "product": "rwa_pilot_production_promotion_packet",
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "all_feeds_held_candidate",
        "production_promoted_feed_count": 0,
        "feeds": feeds,
        "summary": {
            "feed_count": len(feeds),
            "held_candidate_count": len(feeds),
            "promotion_ready_count": 0,
            "blocking_gate_count": sum(len(row["blocking_gates"]) for row in feeds),
        },
        "policy": {
            "automatic_promotion": False,
            "decision_rule": (
                "This scheduler may collect and summarize evidence but cannot promote. "
                "Every gate plus named human approval must be recorded in a separate controlled action."
            ),
            "pilot_runtime_tiingo_dependency": False,
        },
    }


def persist_promotion_packet(
    packet: dict[str, Any],
    *,
    history_path: Path,
    latest_path: Path,
) -> dict[str, str]:
    history_path.parent.mkdir(parents=True, exist_ok=True)
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(packet, sort_keys=True, default=str) + "\n")
    latest_path.write_text(
        json.dumps(packet, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return {"history_path": str(history_path), "latest_path": str(latest_path)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--monitoring", type=Path, required=True)
    parser.add_argument("--alignment", type=Path, required=True)
    parser.add_argument("--depth", type=Path, required=True)
    parser.add_argument("--history", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    packet = build_promotion_packet(
        json.loads(args.monitoring.read_text(encoding="utf-8")),
        json.loads(args.alignment.read_text(encoding="utf-8")),
        json.loads(args.depth.read_text(encoding="utf-8")),
    )
    persist_promotion_packet(packet, history_path=args.history, latest_path=args.output)
    print(json.dumps(packet, indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
