#!/usr/bin/env python3
"""Probe every current RWA feed row with the adapters that are live-wired today.

The output deliberately keeps catalog coverage separate from executable source
coverage. Rows backed by planned/static adapters are recorded as not_live_wired
instead of being silently skipped.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import math
import os
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import perf_counter
from typing import Any

from dotenv import load_dotenv

from src.rwa_adapters import RWA_ADAPTER_REGISTRY, RWAAdapterBlockedError
from src.rwa_pricing import calculate_block_vwap


DEFAULT_INPUT = Path("reports/rwa_feed_discovery.csv")
DEFAULT_JSON_OUTPUT = Path("reports/feed_quality_all_current_probe.json")
DEFAULT_CSV_OUTPUT = Path("reports/feed_quality_all_current_probe.csv")
LIVE_WIRED_STATUSES = {
    "implemented_unprobed",
    "implemented_blocked_on_token_catalog_or_api_key",
    "implemented_blocked_on_discovery_report",
}
REFERENCE_ONLY_SOURCE_TYPES = {
    "native_mark_reference",
    "price_stream_no_book",
    "issuer_reference_price",
    "blocksize_state_reference",
    "nav_reference",
    "oracle_reference",
    "benchmark_reference",
}
NON_POINT_PROBE_PRODUCTION_GATES = {
    "continuous_quality_windows_missing",
    "benchmark_alignment_missing",
    "manipulation_depth_checks_missing",
    "rights_clearance_missing",
    "multi_source_consensus_missing",
}
TRANSIENT_BLOCKERS = {
    "quota_rate_limit",
    "empty_or_one_sided_book",
    "adapter_or_source_error",
}


@dataclass(frozen=True)
class ProbeSpec:
    feed_id: str
    kind: str
    symbol: str
    venue: str
    source_type: str
    asset_id: str
    asset_classes: str
    promotion_status: str
    production_promoted: str


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso_now() -> str:
    return _utc_now().isoformat()


def _parse_timestamp(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        raw = float(value)
        if raw > 10_000_000_000_000:
            raw /= 1_000_000
        elif raw > 10_000_000_000:
            raw /= 1_000
        return datetime.fromtimestamp(raw, tz=UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _freshness_ms(timestamp: Any, *, now: datetime) -> float | None:
    parsed = _parse_timestamp(timestamp)
    if parsed is None:
        return None
    return max(0.0, (now - parsed).total_seconds() * 1000)


def _number(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _mid(row: dict[str, Any]) -> float | None:
    mid = _number(row.get("mid"))
    if mid is not None:
        return mid
    bid = _number(row.get("bid"))
    ask = _number(row.get("ask"))
    if bid is None or ask is None:
        return None
    return (bid + ask) / 2


def _value(row: dict[str, Any]) -> float | None:
    for key in ("value", "mid", "vwap", "price", "last"):
        parsed = _number(row.get(key))
        if parsed is not None:
            return parsed
    return _mid(row)


def _valid_positive(value: float | None) -> bool:
    return value is not None and math.isfinite(value) and value > 0


def _bid_ask_order_valid(row: dict[str, Any]) -> bool | None:
    bid = _number(row.get("bid"))
    ask = _number(row.get("ask"))
    if bid is None and ask is None:
        return None
    return bid is not None and ask is not None and bid > 0 and ask > 0 and bid <= ask


def _compact_json(value: Any) -> str:
    if value is None or value == "":
        return ""
    try:
        return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    except TypeError:
        return str(value)


def _is_reference_only(source_type: str) -> bool:
    return source_type in REFERENCE_ONLY_SOURCE_TYPES


def _has_replayable_raw_payload(metadata: dict[str, Any]) -> bool:
    return bool(
        metadata.get("raw_payload")
        or metadata.get("raw_payload_sha256")
        or metadata.get("raw_payload_ref")
        or metadata.get("replay_payload_ref")
    )


def _classify_error(exc: Exception) -> str:
    if isinstance(exc, RWAAdapterBlockedError):
        return exc.blocker_category
    message = str(exc).lower()
    if "quota/rate limit" in message or "http 429" in message or "too many requests" in message:
        return "quota_rate_limit"
    if "not tradable" in message or "token_not_tradable" in message or "bad mint" in message:
        return "token_not_tradable_or_bad_mint"
    if "did not route through" in message:
        return "route_label_mismatch"
    if "orderly" in message and ("order-book" in message or "orderbook" in message or "l2" in message):
        return "orderly_l2_depth_blocked"
    if "no fillable" in message or "did not include both bid and ask" in message:
        return "empty_or_one_sided_book"
    return "adapter_or_source_error"


def _blocker_from_adapter_status(adapter_status: str) -> str:
    if adapter_status.startswith("implemented_blocked_on_"):
        return adapter_status.removeprefix("implemented_blocked_on_")
    if adapter_status == "planned":
        return "adapter_not_live_wired"
    return f"adapter_status_{adapter_status}"


def _production_blockers(
    *,
    value: float | None,
    bid_ask_order_valid: bool | None,
    source_timestamp_present: bool,
    reference_only: bool,
    raw_payload_replayable: bool,
) -> list[str]:
    blockers: set[str] = set(NON_POINT_PROBE_PRODUCTION_GATES)
    if not _valid_positive(value):
        blockers.add("non_positive_or_missing_value")
    if bid_ask_order_valid is False:
        blockers.add("bid_ask_crossed_or_invalid")
    if not source_timestamp_present:
        blockers.add("source_timestamp_missing")
    if reference_only:
        blockers.add("reference_only_not_l2_liquidity")
    if not raw_payload_replayable:
        blockers.add("raw_payload_replay_missing")
    return sorted(blockers)


def _read_specs(path: Path) -> list[ProbeSpec]:
    with path.open("r", newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    return [
        ProbeSpec(
            feed_id=str(row.get("feed_id") or ""),
            kind=str(row.get("kind") or ""),
            symbol=str(row.get("symbol") or ""),
            venue=str(row.get("venue") or ""),
            source_type=str(row.get("source_type") or ""),
            asset_id=str(row.get("asset_id") or ""),
            asset_classes=str(row.get("asset_classes") or ""),
            promotion_status=str(row.get("promotion_status") or ""),
            production_promoted=str(row.get("production_promoted") or ""),
        )
        for row in rows
    ]


async def _fetch_observation(spec: ProbeSpec) -> dict[str, Any]:
    adapter = RWA_ADAPTER_REGISTRY.get(spec.venue)
    if spec.kind == "bidask":
        row = await adapter.fetch_bidask(spec.symbol)
        mid = _mid(row)
        return {**row, "kind": "bidask", "mid": mid, "value": mid}
    if spec.kind == "vwap":
        row = await adapter.fetch_order_book(spec.symbol, side="buy", depth=20)
        vwap = calculate_block_vwap({**row, "block_size_usd": 10_000})
        return {
            **row,
            "kind": "vwap",
            "vwap": vwap.get("vwap"),
            "value": vwap.get("vwap"),
            "fill_status": vwap.get("status"),
            "fillable_notional_usd": vwap.get("fillable_notional_usd"),
        }
    raise ValueError(f"unsupported feed kind: {spec.kind}")


async def _probe_one(
    spec: ProbeSpec,
    *,
    metadata_by_venue: dict[str, dict[str, Any]],
    semaphore: asyncio.Semaphore,
    retries: int = 0,
) -> dict[str, Any]:
    metadata = metadata_by_venue.get(spec.venue, {})
    adapter_status = str(metadata.get("status") or "unknown")
    implementation = str(metadata.get("implementation") or "")
    base = {
        "feed_id": spec.feed_id,
        "kind": spec.kind,
        "symbol": spec.symbol,
        "venue": spec.venue,
        "source_type": spec.source_type,
        "asset_id": spec.asset_id,
        "asset_classes": spec.asset_classes,
        "promotion_status": spec.promotion_status,
        "production_promoted": spec.production_promoted,
        "adapter_status": adapter_status,
        "implementation": implementation,
        "tested_at": _iso_now(),
    }
    if adapter_status not in LIVE_WIRED_STATUSES:
        notes = metadata.get("notes") if isinstance(metadata.get("notes"), list) else []
        note_text = "; ".join(str(note) for note in notes if str(note))
        blocked_reason = (
            f"adapter status is {adapter_status}"
            + (f"; {note_text}" if note_text else "")
        )
        return {
            **base,
            "probe_status": "not_live_wired",
            "availability": False,
            "valid_positive_price": False,
            "bid_ask_order_valid": None,
            "source_timestamp_present": False,
            "reference_only": _is_reference_only(spec.source_type),
            "raw_payload_replayable": False,
            "production_gate_status": "blocked_not_live_wired",
            "production_blockers": _compact_json(
                sorted(
                    {
                        "adapter_not_live_wired",
                        "raw_payload_replay_missing",
                        *NON_POINT_PROBE_PRODUCTION_GATES,
                    }
                )
            ),
            "blocker_category": _blocker_from_adapter_status(adapter_status),
            "blocked_reason": blocked_reason,
            "source_metadata": "",
            "latency_ms": None,
            "freshness_ms": None,
            "value": None,
            "bid": None,
            "ask": None,
            "vwap": None,
            "fill_status": None,
            "error": blocked_reason,
        }

    async with semaphore:
        start = perf_counter()
        last_exc: Exception | None = None
        last_blocker_category = "adapter_or_source_error"
        attempts = max(1, int(retries) + 1)
        for attempt in range(attempts):
            try:
                observation = await _fetch_observation(spec)
                latency_ms = (perf_counter() - start) * 1000
                break
            except Exception as exc:  # noqa: BLE001 - report exact adapter failure per feed.
                last_exc = exc
                last_blocker_category = _classify_error(exc)
                if last_blocker_category not in TRANSIENT_BLOCKERS or attempt >= attempts - 1:
                    observation = None
                    latency_ms = (perf_counter() - start) * 1000
                    break
                await asyncio.sleep(float(os.getenv("RWA_PROBE_RETRY_BACKOFF_SECONDS", "1.0")) * (attempt + 1))
        if observation is None:
            assert last_exc is not None
            blocker_category = last_blocker_category
            return {
                **base,
                "probe_status": "error",
                "availability": False,
                "valid_positive_price": False,
                "bid_ask_order_valid": None,
                "source_timestamp_present": False,
                "reference_only": _is_reference_only(spec.source_type),
                "raw_payload_replayable": False,
                "production_gate_status": "blocked_probe_error",
                "production_blockers": _compact_json(
                    sorted(
                        {
                            blocker_category,
                            "raw_payload_replay_missing",
                            *NON_POINT_PROBE_PRODUCTION_GATES,
                        }
                    )
                ),
                "blocker_category": blocker_category,
                "blocked_reason": str(last_exc),
                "source_metadata": "",
                "latency_ms": round(latency_ms, 6),
                "freshness_ms": None,
                "value": None,
                "bid": None,
                "ask": None,
                "vwap": None,
                "fill_status": None,
                "error": str(last_exc),
            }

    value = _value(observation)
    now = _utc_now()
    bid_ask_order_valid = _bid_ask_order_valid(observation)
    source_timestamp_present = _parse_timestamp(observation.get("timestamp")) is not None
    observation_metadata = observation.get("metadata") if isinstance(observation.get("metadata"), dict) else {}
    observation_source_type = str(observation.get("source_type") or spec.source_type)
    reference_only = _is_reference_only(observation_source_type)
    raw_payload_replayable = _has_replayable_raw_payload(observation_metadata)
    point_in_time_valid = _valid_positive(value) and bid_ask_order_valid is not False
    production_blockers = _production_blockers(
        value=value,
        bid_ask_order_valid=bid_ask_order_valid,
        source_timestamp_present=source_timestamp_present,
        reference_only=reference_only,
        raw_payload_replayable=raw_payload_replayable,
    )
    return {
        **base,
        "probe_status": "ok" if point_in_time_valid else "invalid_value",
        "availability": True,
        "valid_positive_price": _valid_positive(value),
        "bid_ask_order_valid": bid_ask_order_valid,
        "source_timestamp_present": source_timestamp_present,
        "reference_only": reference_only,
        "raw_payload_replayable": raw_payload_replayable,
        # A successful point probe is candidate evidence only. Rolling quality,
        # replay, benchmark, manipulation/depth, rights and consensus gates are
        # evaluated elsewhere and therefore can never be implied by this script.
        "production_gate_status": "candidate_only_full_promotion_gates_required",
        "production_blockers": _compact_json(production_blockers),
        "blocker_category": "" if point_in_time_valid else "invalid_point_observation",
        "blocked_reason": "" if point_in_time_valid else "point observation failed value or bid/ask checks",
        "source_metadata": observation_metadata,
        "latency_ms": round(latency_ms, 6),
        "freshness_ms": (
            round(freshness, 6)
            if (freshness := _freshness_ms(observation.get("timestamp"), now=now)) is not None
            else None
        ),
        "value": value,
        "bid": _number(observation.get("bid")),
        "ask": _number(observation.get("ask")),
        "vwap": _number(observation.get("vwap")),
        "fill_status": observation.get("fill_status"),
        "error": "",
    }


def _summary(results: list[dict[str, Any]]) -> dict[str, Any]:
    by_status = Counter(str(row.get("probe_status")) for row in results)
    by_venue_status = Counter(
        f"{row.get('venue')}::{row.get('probe_status')}" for row in results
    )
    live_attempted = [row for row in results if row.get("probe_status") != "not_live_wired"]
    ok = [row for row in results if row.get("probe_status") == "ok"]
    return {
        "total_feed_rows": len(results),
        "probe_status_counts": dict(sorted(by_status.items())),
        "venue_status_counts": dict(sorted(by_venue_status.items())),
        "live_attempted_rows": len(live_attempted),
        "ok_rows": len(ok),
        "unique_ok_symbols": len({row.get("symbol") for row in ok}),
        "unique_ok_venues": len({row.get("venue") for row in ok}),
        "production_promoted_rows": sum(str(row.get("production_promoted")) == "True" for row in results),
        "generated_at": _iso_now(),
    }


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = [
        "feed_id",
        "kind",
        "symbol",
        "venue",
        "source_type",
        "asset_id",
        "asset_classes",
        "promotion_status",
        "production_promoted",
        "adapter_status",
        "implementation",
        "probe_status",
        "availability",
        "valid_positive_price",
        "bid_ask_order_valid",
        "source_timestamp_present",
        "reference_only",
        "raw_payload_replayable",
        "production_gate_status",
        "production_blockers",
        "blocker_category",
        "blocked_reason",
        "source_metadata",
        "latency_ms",
        "freshness_ms",
        "value",
        "bid",
        "ask",
        "vwap",
        "fill_status",
        "error",
        "tested_at",
    ]
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {
                    field: (
                        _compact_json(row.get(field))
                        if isinstance(row.get(field), (dict, list))
                        else row.get(field)
                    )
                    for field in fields
                }
            )


async def _run(args: argparse.Namespace) -> dict[str, Any]:
    started_at = _utc_now()
    specs = _read_specs(args.input)
    metadata_by_venue = {
        str(row["venue_id"]): row for row in RWA_ADAPTER_REGISTRY.list_metadata()
    }
    semaphore = asyncio.Semaphore(args.concurrency)
    results = await asyncio.gather(
        *[
            _probe_one(spec, metadata_by_venue=metadata_by_venue, semaphore=semaphore, retries=args.retries)
            for spec in specs
        ]
    )
    ended_at = _utc_now()
    summary = _summary(results)
    summary.update(
        {
            "run_started_at": started_at.isoformat(),
            "run_ended_at": ended_at.isoformat(),
            "run_duration_seconds": round((ended_at - started_at).total_seconds(), 6),
        }
    )
    payload = {
        "product": "all_current_feed_probe",
        "input": str(args.input),
        "summary": summary,
        "adapter_metadata": sorted(metadata_by_venue.values(), key=lambda row: str(row["venue_id"])),
        "results": sorted(results, key=lambda row: str(row["feed_id"])),
    }
    args.output_json.parent.mkdir(parents=True, exist_ok=True)
    args.output_json.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    _write_csv(args.output_csv, payload["results"])
    return payload


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output-json", type=Path, default=DEFAULT_JSON_OUTPUT)
    parser.add_argument("--output-csv", type=Path, default=DEFAULT_CSV_OUTPUT)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--retries", type=int, default=0, help="Retries for transient adapter failures.")
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    payload = asyncio.run(_run(parse_args()))
    print(json.dumps(payload["summary"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
