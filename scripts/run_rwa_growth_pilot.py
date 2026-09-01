#!/usr/bin/env python3
"""Capture and score the three-feed RWA growth pilot without auto-promotion."""

from __future__ import annotations

import argparse
import asyncio
import copy
import json
import math
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from src.rwa_security import public_probe_error_message
from src.rwa_store import RWAObservationStore, RWA_PILOT_MAX_FUTURE_SKEW_SECONDS


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
PILOT_STALE_AFTER_SECONDS = 3_900.0
PILOT_LEDGER_HISTORY_LIMIT = 10_000
PILOT_EXPECTED_INTERVAL_SECONDS = 1_800
MINIMUM_ACTIVE_DAYS = 14


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
    future_skew_seconds = (
        (timestamp.astimezone(UTC) - checked_at).total_seconds()
        if timestamp is not None
        else None
    )
    timestamp_pass = bool(
        timestamp is not None
        and future_skew_seconds is not None
        and future_skew_seconds <= RWA_PILOT_MAX_FUTURE_SKEW_SECONDS
    )
    freshness_seconds = (
        max(0.0, (checked_at - timestamp.astimezone(UTC)).total_seconds())
        if timestamp_pass and timestamp is not None
        else None
    )
    bid = _finite(observation.get("bid"))
    ask = _finite(observation.get("ask"))
    bidask_sane = bid is not None and ask is not None and 0 < bid <= ask
    return {
        "source_timestamp_pass": timestamp_pass,
        "future_skew_seconds": (
            round(max(0.0, future_skew_seconds), 6)
            if future_skew_seconds is not None
            else None
        ),
        "freshness_seconds": freshness_seconds,
        "freshness_limit_seconds": freshness_limit_seconds,
        "freshness_pass": (
            timestamp_pass
            and freshness_seconds is not None
            and freshness_seconds <= freshness_limit_seconds
        ),
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
        checks = _observation_checks(
            observation,
            checked_at=checked_at,
            freshness_limit_seconds=float(feed["freshness_limit_seconds"]),
        )
        if not checks["source_timestamp_pass"]:
            raise ValueError("upstream source timestamp is missing or in the future")
        record.update(
            {
                "checked_at": checked_at.isoformat(),
                "status": "ok",
                "checks": checks,
                "raw_observation": observation,
            }
        )
    except Exception as exc:
        record.update(
            {
                "checked_at": datetime.now(UTC).isoformat(),
                "status": "error",
                "error_type": type(exc).__name__,
                "message": public_probe_error_message(exc),
                "checks": {"freshness_pass": False, "bidask_sanity_pass": False},
            }
        )
    return record


def import_legacy_history(
    store: RWAObservationStore,
    path: Path,
) -> dict[str, Any]:
    """Idempotently migrate legacy JSONL without performing a live capture."""
    result: dict[str, Any] = {
        "product": "rwa_growth_pilot_legacy_migration",
        "source_of_truth": "rwa_observation_store",
        "source_path": str(path),
        "attempted": 0,
        "imported": 0,
        "duplicates": 0,
        "rejected": 0,
        "rejections": [],
        "production_promoted_feed_count": 0,
        "automatic_promotion": False,
    }
    try:
        handle = path.open("r", encoding="utf-8")
    except OSError as exc:
        raise ValueError("legacy growth-pilot history is unavailable") from exc
    with handle:
        for line_number, line in enumerate(handle, start=1):
            if not line.strip():
                continue
            result["attempted"] += 1
            try:
                if len(line.encode("utf-8")) > 128 * 1024:
                    raise ValueError("legacy row exceeds the 131072-byte limit")
                row = json.loads(line)
                if not isinstance(row, dict):
                    raise ValueError("legacy row must be an object")
                capture = dict(row)
                if str(capture.get("status") or "").lower() == "error":
                    # Historical exception strings may contain URLs, query tokens,
                    # or raw response fragments. Preserve the bounded error type but
                    # replace the message with the same safe operator-facing class
                    # used for new captures.
                    capture["message"] = (
                        "Upstream adapter timed out."
                        if capture.get("error_type") == "TimeoutError"
                        else "Upstream adapter request failed."
                    )
                stored = store.store_pilot_outcomes([capture])[0]
            except (json.JSONDecodeError, ValueError) as exc:
                result["rejected"] += 1
                if len(result["rejections"]) < 50:
                    result["rejections"].append(
                        {
                            "line": line_number,
                            "reason": str(exc)[:200],
                        }
                    )
                continue
            if stored["inserted"]:
                result["imported"] += 1
            else:
                result["duplicates"] += 1
    result["complete"] = result["rejected"] == 0
    return result


def evaluate_history(
    rows: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    stale_after_seconds: float = PILOT_STALE_AFTER_SECONDS,
) -> dict[str, Any]:
    now = (now or datetime.now(UTC)).astimezone(UTC)
    feed_rows = []
    all_source_gates_pass = True
    for feed in PILOT_FEEDS:
        rows_for_id = [row for row in rows if row.get("pilot_id") == feed["pilot_id"]]

        def identity_matches(row: dict[str, Any]) -> bool:
            try:
                row_limit = float(row.get("freshness_limit_seconds"))
            except (TypeError, ValueError):
                return False
            return (
                str(row.get("symbol") or "").strip().upper()
                == str(feed["symbol"]).upper()
                and str(row.get("venue") or "").strip().lower()
                == str(feed["venue"]).lower()
                and str(row.get("source_lane") or "").strip().lower()
                == str(feed["source_lane"]).lower()
                and row_limit == float(feed["freshness_limit_seconds"])
            )

        matching = [row for row in rows_for_id if identity_matches(row)]
        identity_mismatch_count = len(rows_for_id) - len(matching)
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
        distinct_monitoring_slots = len(
            {
                int(timestamp.timestamp() // PILOT_EXPECTED_INTERVAL_SECONDS)
                for timestamp in timestamps
            }
        )
        active_day_count = len(
            {timestamp.astimezone(UTC).date().isoformat() for timestamp in timestamps}
        )
        success_rate = len(successful) / sample_count if sample_count else None
        freshness_rate = len(fresh) / len(successful) if successful else None
        sanity_rate = len(sane) / len(successful) if successful else None
        source_gates = {
            "minimum_window_pass": window_days >= MINIMUM_WINDOW_DAYS,
            "minimum_samples_pass": sample_count >= MINIMUM_SAMPLES_PER_FEED,
            "success_rate_pass": success_rate is not None and success_rate >= MINIMUM_SUCCESS_RATE,
            "freshness_rate_pass": freshness_rate is not None and freshness_rate >= MINIMUM_FRESHNESS_RATE,
            "bidask_sanity_pass": sanity_rate == 1.0,
            "identity_match_pass": identity_mismatch_count == 0,
            "temporal_slot_coverage_pass": (
                distinct_monitoring_slots >= MINIMUM_SAMPLES_PER_FEED
            ),
            "active_day_coverage_pass": active_day_count >= MINIMUM_ACTIVE_DAYS,
            "latest_capture_not_stale": bool(
                timestamps
                and max(0.0, (now - max(timestamps)).total_seconds())
                <= stale_after_seconds
            ),
        }
        source_ready = all(source_gates.values())
        all_source_gates_pass = all_source_gates_pass and source_ready
        feed_rows.append(
            {
                **feed,
                "sample_count": sample_count,
                "rows_with_pilot_id": len(rows_for_id),
                "identity_mismatch_count": identity_mismatch_count,
                "successful_samples": len(successful),
                "distinct_monitoring_slots": distinct_monitoring_slots,
                "active_day_count": active_day_count,
                "window_days": round(window_days, 4),
                "success_rate": success_rate,
                "freshness_rate": freshness_rate,
                "bidask_sanity_rate": sanity_rate,
                "source_monitoring_gates": source_gates,
                "source_monitoring_ready": source_ready,
                "last_checked_at": max(timestamps).isoformat() if timestamps else None,
                "last_capture_age_seconds": (
                    round(max(0.0, (now - max(timestamps)).total_seconds()), 3)
                    if timestamps
                    else None
                ),
                "stale": not source_gates["latest_capture_not_stale"],
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
        "status": (
            "not_started"
            if not rows
            else "stale"
            if any(row["stale"] for row in feed_rows)
            else "candidate_monitoring"
        ),
        "production_promoted_feed_count": 0,
        "source_monitoring_ready": all_source_gates_pass,
        "promotion_ready": all_source_gates_pass and all(non_monitoring_gates.values()),
        "thresholds": {
            "minimum_window_days": MINIMUM_WINDOW_DAYS,
            "minimum_samples_per_feed": MINIMUM_SAMPLES_PER_FEED,
            "minimum_success_rate": MINIMUM_SUCCESS_RATE,
            "minimum_freshness_rate": MINIMUM_FRESHNESS_RATE,
            "expected_interval_seconds": PILOT_EXPECTED_INTERVAL_SECONDS,
            "minimum_distinct_monitoring_slots": MINIMUM_SAMPLES_PER_FEED,
            "minimum_active_days": MINIMUM_ACTIVE_DAYS,
            "stale_after_seconds": stale_after_seconds,
        },
        "non_monitoring_gates": non_monitoring_gates,
        "feeds": feed_rows,
        "policy": {
            "automatic_promotion": False,
            "catalog_boundary": "Pilot observations remain candidate data until every monitoring and non-monitoring gate passes and a human approves promotion.",
            "tiingo_runtime_dependency": False,
        },
    }


def evaluate_store(
    store: RWAObservationStore,
    *,
    now: datetime | None = None,
    stale_after_seconds: float = PILOT_STALE_AFTER_SECONDS,
) -> dict[str, Any]:
    """Build pilot readiness only from the authoritative SQLite ledger."""
    pilot_ids = [feed["pilot_id"] for feed in PILOT_FEEDS]
    rows = store.list_pilot_outcomes(
        pilot_ids=pilot_ids,
        limit=PILOT_LEDGER_HISTORY_LIMIT,
    )
    report = evaluate_history(
        rows,
        now=now,
        stale_after_seconds=stale_after_seconds,
    )
    freshness = store.pilot_freshness(
        pilot_ids,
        stale_after_seconds=stale_after_seconds,
        now=now,
    )
    report["ledger"] = {
        "source_of_truth": "rwa_observation_store",
        "schema_version": store.schema_status()["schema_version"],
        "history_rows_evaluated": len(rows),
        "history_limit": PILOT_LEDGER_HISTORY_LIMIT,
        "authoritative": True,
        "status_snapshot_export_authoritative": False,
    }
    report["freshness"] = freshness
    return report


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


def _ledger_depth_evidence(depth_evidence: dict[str, Any]) -> dict[str, Any]:
    """Keep the observation ledger bounded while preserving replay provenance."""
    bounded = copy.deepcopy(depth_evidence)
    replay = bounded.get("replay_evidence")
    if not isinstance(replay, dict):
        return bounded

    raw_pool_state = replay.pop("raw_pool_state_payload", None)
    tick_and_swap = replay.pop("tick_and_swap_payload", None)
    if raw_pool_state is None and tick_and_swap is None:
        return bounded

    replay["payloads_omitted_from_observation_ledger"] = True
    replay["payload_storage"] = "rwa_depth_report"
    replay["raw_pool_state_payload_present"] = raw_pool_state is not None
    if isinstance(tick_and_swap, dict):
        replay["tick_and_swap_payload_summary"] = {
            "bitmap_word_count": len(tick_and_swap.get("bitmap_words") or []),
            "initialized_tick_count": len(tick_and_swap.get("initialized_ticks") or []),
            "swap_log_count": len(tick_and_swap.get("swap_logs") or []),
        }
    else:
        replay["tick_and_swap_payload_summary"] = None
    return bounded


def persist_capture(
    store: RWAObservationStore,
    captures: list[dict[str, Any]],
    *,
    status_output: Path | None = None,
    observation_store: Any | None = None,
    alignment_report: dict[str, Any] | None = None,
    depth_report: dict[str, Any] | None = None,
    now: datetime | None = None,
    stale_after_seconds: float = PILOT_STALE_AFTER_SECONDS,
) -> dict[str, Any]:
    """Persist a capture atomically, then derive readiness from that ledger."""
    stored = store.store_pilot_outcomes(captures)
    report = evaluate_store(
        store,
        now=now,
        stale_after_seconds=stale_after_seconds,
    )
    report["current_capture"] = {
        "attempted": len(captures),
        "succeeded": sum(row.get("status") == "ok" for row in captures),
        "failed": sum(row.get("status") == "error" for row in captures),
        "inserted": sum(row["inserted"] for row in stored),
        "rows": stored,
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
                        "source_type": observation.get("source_type")
                        or capture.get("source_lane"),
                        "raw_payload": observation,
                        "normalized_observation": observation,
                        "realtime_quality": {
                            **capture.get("checks", {}),
                            "liquidity_depth_evidence": _ledger_depth_evidence(
                                depth_evidence
                            ),
                        },
                        "blocksize_benchmark": benchmark_evidence,
                        "promotion": {
                            "production_promoted": False,
                            "status": "candidate_monitoring",
                        },
                        "metadata": {
                            "pilot_id": capture.get("pilot_id"),
                            "source_lane": capture.get("source_lane"),
                            "pilot_outcome_source": "rwa_observation_store",
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
        "--db-path",
        default=os.environ.get("RWA_OBSERVATION_DB_PATH"),
        help="Authoritative RWA SQLite ledger (defaults to RWA_OBSERVATION_DB_PATH).",
    )
    parser.add_argument("--legacy-history", type=Path)
    parser.add_argument(
        "--import-only",
        action="store_true",
        help="Migrate --legacy-history into SQLite and exit without a live capture.",
    )
    parser.add_argument("--status-output", type=Path)
    parser.add_argument("--no-append", action="store_true")
    parser.add_argument("--require-successes", type=int, default=0)
    args = parser.parse_args()

    if args.import_only and args.legacy_history is None:
        parser.error("--import-only requires --legacy-history")
    store = RWAObservationStore(args.db_path)
    migration = None
    if args.legacy_history:
        migration = import_legacy_history(store, args.legacy_history)
    if args.import_only:
        assert migration is not None
        print(json.dumps(migration, indent=2, sort_keys=True) + "\n", end="")
        if migration["rejected"]:
            raise SystemExit(1)
        return
    captures = asyncio.run(_run(max(1.0, args.timeout)))
    if args.no_append:
        history = store.list_pilot_outcomes(
            pilot_ids=[feed["pilot_id"] for feed in PILOT_FEEDS],
            limit=PILOT_LEDGER_HISTORY_LIMIT,
        )
        report = evaluate_history([*history, *captures])
        report["ledger"] = {
            "source_of_truth": "rwa_observation_store",
            "authoritative": False,
            "reason": "dry_run_capture_not_persisted",
        }
        report["current_capture"] = {
            "attempted": len(captures),
            "succeeded": sum(row.get("status") == "ok" for row in captures),
            "failed": sum(row.get("status") == "error" for row in captures),
            "inserted": 0,
            "rows": captures,
        }
    else:
        report = persist_capture(store, captures, status_output=args.status_output)
    if migration is not None:
        report["legacy_migration"] = migration
    serialized = json.dumps(report, indent=2, sort_keys=True, default=str) + "\n"
    print(serialized, end="")
    if args.status_output and args.no_append:
        args.status_output.parent.mkdir(parents=True, exist_ok=True)
        args.status_output.write_text(serialized, encoding="utf-8")
    if report["current_capture"]["succeeded"] < max(0, args.require_successes):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
