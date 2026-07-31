"""Daily RWA feed discovery agent for newly listed tokenized products."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import tempfile
from collections import Counter
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.rwa_xyz_monitor import (
    DEFAULT_RWA_XYZ_ASSET_CSV_PATH,
    DEFAULT_RWA_XYZ_REPORT_JSON_PATH,
    DEFAULT_RWA_XYZ_TOKEN_CSV_PATH,
    RWA_XYZ_VENUE_ID,
    fetch_rwa_xyz_monitor_payload,
    load_payload_from_file,
    load_rwa_xyz_monitor_report,
    rwa_xyz_contract_identity_key,
    write_rwa_xyz_monitor_reports,
)
from src.runtime_data import (
    RWA_REPORTS_DIR,
    persisted_rwa_report_reference,
    resolve_required_rwa_report_path,
    resolve_rwa_report_reference,
)


DEFAULT_DAILY_AGENT_JSON_PATH = resolve_required_rwa_report_path(
    "rwa_daily_feed_agent.json"
)
DEFAULT_DAILY_AGENT_CSV_PATH = RWA_REPORTS_DIR / "rwa_daily_new_tokens.csv"
DEFAULT_DAILY_AGENT_HISTORY_DIR = RWA_REPORTS_DIR / "rwa_daily_feed_agent_history"
DAILY_AGENT_SNAPSHOT_SCHEMA = "blocksize.rwa_xyz_monitor_snapshot.v1"

P0_ASSET_CLASSES = {"equity", "etf", "treasury_fund", "metal", "tokenized_fund"}
SOLANA_NETWORKS = {"solana"}
EVM_NETWORKS = {
    "arbitrum",
    "avalanche-c-chain",
    "base",
    "bnb-chain",
    "ethereum",
    "gnosis",
    "hyperevm",
    "ink",
    "mantle",
    "monad",
    "optimism",
    "pharos",
    "plasma",
    "plume",
    "polygon",
    "sei",
    "zksync-era",
}


def _canonical_json_text(payload: dict[str, Any]) -> str:
    return json.dumps(payload, indent=2, sort_keys=True) + "\n"


def _parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _atomic_write_bytes(path: Path, payload: bytes) -> None:
    """Replace one artifact atomically after fully writing it on the same filesystem."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_path = Path(handle.name)
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _atomic_write_text(path: Path, payload: str) -> None:
    _atomic_write_bytes(path, payload.encode("utf-8"))


def rwa_xyz_token_contract_key(row: dict[str, Any]) -> str:
    """Return the chain-aware identity for an RWA.xyz token deployment."""
    contract_key = rwa_xyz_contract_identity_key(row)
    return contract_key or str(
        row.get("token_row_id") or row.get("rwa_xyz_token_id") or ""
    )


def _token_key(row: dict[str, Any]) -> str:
    return rwa_xyz_token_contract_key(row)


def _asset_key(row: dict[str, Any]) -> str:
    return str(row.get("rwa_xyz_asset_id") or row.get("asset_id") or row.get("symbol") or "")


def _by_key(rows: list[dict[str, Any]], key_fn) -> dict[str, dict[str, Any]]:
    keyed: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = key_fn(row)
        if key:
            keyed[key] = row
    return keyed


def _monitor_snapshot_errors(report: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    generated_at = _parse_datetime(report.get("generated_at"))
    if generated_at is None:
        errors.append("monitor generated_at is missing or invalid")

    asset_rows = report.get("asset_rows")
    token_rows = report.get("token_rows")
    coverage_rows = report.get("coverage_rows")
    if not isinstance(asset_rows, list):
        errors.append("monitor asset_rows is not a list")
        asset_rows = []
    if not isinstance(token_rows, list):
        errors.append("monitor token_rows is not a list")
        token_rows = []
    if not isinstance(coverage_rows, list):
        errors.append("monitor coverage_rows is not a list")
        coverage_rows = []

    summary = report.get("summary")
    if not isinstance(summary, dict):
        errors.append("monitor summary is not an object")
        summary = {}
    expected_counts = {
        "asset_count": len(asset_rows),
        "token_count": len(token_rows),
        "coverage_row_count": len(coverage_rows),
    }
    for field, expected in expected_counts.items():
        if summary.get(field) != expected:
            errors.append(
                f"monitor summary.{field}={summary.get(field)!r} does not equal row count {expected}"
            )
    return errors


def rwa_xyz_monitor_snapshot_identity(report: dict[str, Any]) -> dict[str, Any]:
    """Build a deterministic identity for one internally reconciled monitor artifact."""
    errors = _monitor_snapshot_errors(report)
    if errors:
        raise ValueError("invalid RWA.xyz monitor snapshot: " + "; ".join(errors))

    asset_rows = report["asset_rows"]
    token_rows = report["token_rows"]
    coverage_rows = report["coverage_rows"]
    source = report.get("source") if isinstance(report.get("source"), dict) else {}
    canonical_payload = _canonical_json_text(report).encode("utf-8")
    return {
        "schema": DAILY_AGENT_SNAPSHOT_SCHEMA,
        "canonical_json_sha256": hashlib.sha256(canonical_payload).hexdigest(),
        "generated_at": report.get("generated_at"),
        "fetched_at": source.get("fetched_at"),
        "next_build_id": source.get("next_build_id"),
        "asset_count": len(asset_rows),
        "unique_asset_count": len(_by_key(asset_rows, _asset_key)),
        "token_count": len(token_rows),
        "unique_token_contract_count": len(_by_key(token_rows, _token_key)),
        "coverage_row_count": len(coverage_rows),
    }


def _same_snapshot(left: Any, right: Any) -> bool:
    if not isinstance(left, dict) or not isinstance(right, dict):
        return False
    return (
        left.get("schema") == DAILY_AGENT_SNAPSHOT_SCHEMA
        and right.get("schema") == DAILY_AGENT_SNAPSHOT_SCHEMA
        and left.get("canonical_json_sha256") == right.get("canonical_json_sha256")
    )


def _snapshot_identity_errors(identity: Any, *, label: str) -> list[str]:
    if not isinstance(identity, dict):
        return [f"{label} is missing"]
    errors: list[str] = []
    if identity.get("schema") != DAILY_AGENT_SNAPSHOT_SCHEMA:
        errors.append(f"{label}.schema is invalid")
    digest = identity.get("canonical_json_sha256")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
    ):
        errors.append(f"{label}.canonical_json_sha256 is invalid")
    if _parse_datetime(identity.get("generated_at")) is None:
        errors.append(f"{label}.generated_at is missing or invalid")
    for field in (
        "asset_count",
        "unique_asset_count",
        "token_count",
        "unique_token_contract_count",
        "coverage_row_count",
    ):
        value = identity.get(field)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            errors.append(f"{label}.{field} is not a non-negative integer")
    return errors


def rwa_xyz_token_priority(row: dict[str, Any]) -> str:
    asset_class = str(row.get("asset_class") or "").lower()
    network = str(row.get("network_slug") or "").lower()
    if asset_class in P0_ASSET_CLASSES and network in SOLANA_NETWORKS | EVM_NETWORKS:
        return "P0"
    if asset_class in P0_ASSET_CLASSES:
        return "P1"
    return "P2"


def _priority_for_token(row: dict[str, Any]) -> str:
    return rwa_xyz_token_priority(row)


def rwa_xyz_token_discovery_lane(row: dict[str, Any]) -> str:
    network = str(row.get("network_slug") or "").lower()
    if network in SOLANA_NETWORKS:
        return "solana_token_route_and_pool_discovery"
    if network in EVM_NETWORKS:
        return "evm_token_pool_and_router_discovery"
    if network in {"stellar", "xrp-ledger", "xdc", "hedera", "liquid-network"}:
        return "non_evm_chain_native_or_partner_discovery"
    if network == "robinhood":
        return "partner_or_native_platform_quote_discovery"
    return "manual_network_adapter_triage"


def _discovery_lane(row: dict[str, Any]) -> str:
    return rwa_xyz_token_discovery_lane(row)


def build_rwa_xyz_token_action(row: dict[str, Any]) -> dict[str, Any]:
    lane = rwa_xyz_token_discovery_lane(row)
    base = {
        "priority": _priority_for_token(row),
        "lane": lane,
        "asset_id": row.get("asset_id"),
        "symbol": row.get("symbol"),
        "network": row.get("network"),
        "network_slug": row.get("network_slug"),
        "platform": row.get("platform"),
        "address": row.get("address"),
        "rwa_xyz_asset_id": row.get("rwa_xyz_asset_id"),
        "rwa_xyz_token_id": row.get("rwa_xyz_token_id"),
    }
    if lane == "solana_token_route_and_pool_discovery":
        base["next_action"] = (
            "Use Solana RPC/Jupiter/Helius to confirm mint metadata, decimals, holders, routePlan, "
            "AMM labels, pool ids, slot freshness, and replayable quote/pool evidence."
        )
    elif lane == "evm_token_pool_and_router_discovery":
        base["next_action"] = (
            "Use EVM RPC plus Uniswap/Curve/Balancer/Aerodrome/indexer discovery to map token contracts, "
            "pool addresses, fee tiers, block state, balances/liquidity, and swap simulations."
        )
    elif lane == "partner_or_native_platform_quote_discovery":
        base["next_action"] = (
            "Confirm whether the platform exposes partner/native quote or order-book data; keep catalog-only until rights, "
            "freshness, and benchmark checks pass."
        )
    else:
        base["next_action"] = (
            "Assign a chain/platform adapter owner and require token identity, executable venue, replay, and quality evidence."
        )
    return base


def _token_action(row: dict[str, Any]) -> dict[str, Any]:
    return build_rwa_xyz_token_action(row)


def _counter(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
    return dict(sorted(Counter(str(row.get(field) or "unknown") for row in rows).items()))


def build_daily_feed_agent_report(
    *,
    previous_report: dict[str, Any],
    current_report: dict[str, Any],
    generated_at: str | None = None,
    current_report_path: str | Path = DEFAULT_RWA_XYZ_REPORT_JSON_PATH,
) -> dict[str, Any]:
    """Compare the current RWA.xyz monitor with the previous saved report."""
    current_snapshot = rwa_xyz_monitor_snapshot_identity(current_report)
    previous_assets = previous_report.get("asset_rows") if isinstance(previous_report.get("asset_rows"), list) else []
    current_assets = current_report.get("asset_rows") if isinstance(current_report.get("asset_rows"), list) else []
    previous_tokens = previous_report.get("token_rows") if isinstance(previous_report.get("token_rows"), list) else []
    current_tokens = current_report.get("token_rows") if isinstance(current_report.get("token_rows"), list) else []
    previous_coverage = (
        previous_report.get("coverage_rows")
        if isinstance(previous_report.get("coverage_rows"), list)
        else []
    )
    current_coverage = (
        current_report.get("coverage_rows")
        if isinstance(current_report.get("coverage_rows"), list)
        else []
    )

    previous_asset_map = _by_key(previous_assets, _asset_key)
    current_asset_map = _by_key(current_assets, _asset_key)
    previous_token_map = _by_key(previous_tokens, _token_key)
    current_token_map = _by_key(current_tokens, _token_key)

    baseline_created = not previous_asset_map and not previous_token_map
    previous_snapshot = (
        None if baseline_created else rwa_xyz_monitor_snapshot_identity(previous_report)
    )
    new_asset_keys = [] if baseline_created else sorted(set(current_asset_map) - set(previous_asset_map))
    new_token_keys = [] if baseline_created else sorted(set(current_token_map) - set(previous_token_map))
    removed_asset_keys = [] if baseline_created else sorted(set(previous_asset_map) - set(current_asset_map))
    removed_token_keys = [] if baseline_created else sorted(set(previous_token_map) - set(current_token_map))

    new_assets = [current_asset_map[key] for key in new_asset_keys]
    new_tokens = [current_token_map[key] for key in new_token_keys]
    removed_assets = [previous_asset_map[key] for key in removed_asset_keys]
    removed_tokens = [previous_token_map[key] for key in removed_token_keys]
    actions = [_token_action(row) for row in new_tokens]
    p0_actions = [row for row in actions if row["priority"] == "P0"]
    by_lane = Counter(row["lane"] for row in actions)

    unchanged_capture = _same_snapshot(previous_snapshot, current_snapshot)
    alert_level = (
        "baseline_created"
        if baseline_created
        else "snapshot_unchanged"
        if unchanged_capture
        else "new_p0_tokens"
        if p0_actions
        else "new_tokens"
        if new_tokens
        else "new_assets"
        if new_assets
        else "feeds_removed"
        if removed_assets or removed_tokens
        else "no_new_feeds"
    )

    # A report derived from the same canonical capture is byte-for-byte reproducible.
    effective_generated_at = generated_at or str(current_snapshot["generated_at"])
    report = {
        "product": "rwa_daily_feed_discovery_agent",
        "generated_at": effective_generated_at,
        "status": {
            "acceptance": "passed",
            "decision_usable": True,
            "snapshot_reconciled": True,
        },
        "source": {
            "venue": RWA_XYZ_VENUE_ID,
            "current_report": persisted_rwa_report_reference(current_report_path),
            "method": "daily diff of normalized RWA.xyz New Asset Monitor assets and token contracts",
        },
        "source_snapshot": current_snapshot,
        "previous_source_snapshot": previous_snapshot,
        "comparison": {
            "state": (
                "baseline_created"
                if baseline_created
                else "snapshot_unchanged"
                if unchanged_capture
                else "verified_distinct_snapshots"
            ),
            "previous_snapshot_available": previous_snapshot is not None,
            "snapshots_distinct": (
                None
                if previous_snapshot is None
                else not unchanged_capture
            ),
        },
        "summary": {
            "alert_level": alert_level,
            "baseline_created": baseline_created,
            "previous_asset_count": len(previous_assets),
            "current_asset_count": len(current_assets),
            "previous_unique_asset_count": len(previous_asset_map),
            "current_unique_asset_count": len(current_asset_map),
            "previous_token_count": len(previous_tokens),
            "current_token_count": len(current_tokens),
            "previous_unique_token_contract_count": len(previous_token_map),
            "current_unique_token_contract_count": len(current_token_map),
            "previous_coverage_row_count": len(previous_coverage),
            "current_coverage_row_count": len(current_coverage),
            "new_asset_count": len(new_assets),
            "new_token_count": len(new_tokens),
            "removed_asset_count": len(removed_assets),
            "removed_token_count": len(removed_tokens),
            "new_p0_token_count": len(p0_actions),
            "new_by_asset_class": _counter(new_assets, "asset_class"),
            "new_tokens_by_asset_class": _counter(new_tokens, "asset_class"),
            "new_tokens_by_network": _counter(new_tokens, "network"),
            "new_tokens_by_platform": _counter(new_tokens, "platform"),
            "new_actions_by_lane": dict(sorted(by_lane.items())),
        },
        "policy": {
            "catalog_boundary": "RWA.xyz additions are catalog/reference candidates only until executable venue or pool data passes promotion gates.",
            "promotion_required_gates": [
                "canonical identity and underlying mapping",
                "token contract and decimal verification",
                "pool, route, issuer quote, or order-book discovery",
                "fee tier, route plan, slot/block state, and replayable raw payload",
                "liquidity, spread, fillability, and organic volume checks",
                "30-minute freshness, latency, tick-frequency, and uptime window",
                "manipulation, concentration, stale-value, and outlier checks",
                "issuer NAV, reserve, primary-market, and benchmark alignment where applicable",
                "Blocksize benchmark comparison before consensus or replacement use",
            ],
        },
        "new_assets": sorted(new_assets, key=lambda row: (str(row.get("created_at")), str(row.get("asset_id"))), reverse=True),
        "new_tokens": sorted(new_tokens, key=lambda row: (str(row.get("asset_id")), str(row.get("network")), str(row.get("platform")))),
        "removed_assets": sorted(removed_assets, key=lambda row: str(row.get("asset_id"))),
        "removed_tokens": sorted(removed_tokens, key=lambda row: str(row.get("token_row_id"))),
        "sourcing_actions": sorted(actions, key=lambda row: (str(row["priority"]), str(row["lane"]), str(row["asset_id"]))),
        "next_steps": [
            "Treat no_new_feeds as actionable only when status.acceptance is passed and comparison.state is verified_distinct_snapshots.",
            "For P0 new tokens, immediately run token/pool/route discovery by lane and attach replayable evidence.",
            "Refresh /v1/rwa/sourcing/jobs after every daily run so the new token contracts are visible in the backlog.",
            "Do not promote any new catalog row into VWAP, bid/ask, or consensus until the promotion_required_gates pass.",
        ],
    }
    errors = validate_daily_feed_agent_report(report, current_report=current_report)
    if errors:
        raise ValueError("daily RWA feed report failed acceptance: " + "; ".join(errors))
    return report


def validate_daily_feed_agent_report(
    report: dict[str, Any],
    *,
    current_report: dict[str, Any],
) -> list[str]:
    """Return acceptance failures against the exact canonical monitor snapshot."""
    errors: list[str] = []
    try:
        expected_snapshot = rwa_xyz_monitor_snapshot_identity(current_report)
    except ValueError as exc:
        return [str(exc)]

    reported_snapshot = report.get("source_snapshot")
    errors.extend(_snapshot_identity_errors(reported_snapshot, label="daily source_snapshot"))
    if not _same_snapshot(reported_snapshot, expected_snapshot):
        errors.append("daily source_snapshot does not match the canonical monitor snapshot")
    elif reported_snapshot != expected_snapshot:
        errors.append("daily source_snapshot metadata or counts do not exactly reconcile")

    summary = report.get("summary")
    if not isinstance(summary, dict):
        errors.append("daily summary is not an object")
        summary = {}
    count_contract = {
        "current_asset_count": expected_snapshot["asset_count"],
        "current_unique_asset_count": expected_snapshot["unique_asset_count"],
        "current_token_count": expected_snapshot["token_count"],
        "current_unique_token_contract_count": expected_snapshot[
            "unique_token_contract_count"
        ],
        "current_coverage_row_count": expected_snapshot["coverage_row_count"],
    }
    for field, expected in count_contract.items():
        if summary.get(field) != expected:
            errors.append(f"daily summary.{field} does not equal canonical value {expected}")

    row_count_contract: dict[str, int] = {}
    for summary_field, row_field in (
        ("new_asset_count", "new_assets"),
        ("new_token_count", "new_tokens"),
        ("removed_asset_count", "removed_assets"),
        ("removed_token_count", "removed_tokens"),
    ):
        rows = report.get(row_field)
        if not isinstance(rows, list):
            errors.append(f"daily {row_field} is not a list")
            rows = []
        row_count_contract[summary_field] = len(rows)
    for field, expected in row_count_contract.items():
        if summary.get(field) != expected:
            errors.append(f"daily summary.{field} does not equal report row count {expected}")
    actions = report.get("sourcing_actions")
    if not isinstance(actions, list):
        errors.append("daily sourcing_actions is not a list")
        actions = []
    if len(actions) != row_count_contract["new_token_count"]:
        errors.append("daily sourcing_actions does not contain exactly one action per new token")
    new_p0_count = sum(
        isinstance(action, dict) and action.get("priority") == "P0" for action in actions
    )
    if summary.get("new_p0_token_count") != new_p0_count:
        errors.append("daily summary.new_p0_token_count does not reconcile with actions")

    daily_generated_at = _parse_datetime(report.get("generated_at"))
    monitor_generated_at = _parse_datetime(expected_snapshot.get("generated_at"))
    if daily_generated_at is None:
        errors.append("daily generated_at is missing or invalid")
    elif monitor_generated_at is not None and daily_generated_at < monitor_generated_at:
        errors.append("daily report predates its canonical monitor snapshot")

    alert_level = summary.get("alert_level")
    comparison = report.get("comparison")
    if not isinstance(comparison, dict):
        errors.append("daily comparison evidence is missing")
        comparison = {}
    baseline_created = summary.get("baseline_created") is True
    previous_snapshot = report.get("previous_source_snapshot")
    if baseline_created:
        if alert_level != "baseline_created" or comparison.get("state") != "baseline_created":
            errors.append("baseline report does not carry baseline_created decision evidence")
        if previous_snapshot is not None:
            errors.append("baseline report cannot claim a previous source snapshot")
        if any(
            summary.get(field) != 0
            for field in (
                "previous_asset_count",
                "previous_unique_asset_count",
                "previous_token_count",
                "previous_unique_token_contract_count",
                "previous_coverage_row_count",
                "new_asset_count",
                "new_token_count",
                "removed_asset_count",
                "removed_token_count",
            )
        ):
            errors.append("baseline report must have zero previous and delta counts")
    else:
        errors.extend(
            _snapshot_identity_errors(
                previous_snapshot,
                label="daily previous_source_snapshot",
            )
        )
        if isinstance(previous_snapshot, dict):
            previous_count_contract = {
                "previous_asset_count": previous_snapshot.get("asset_count"),
                "previous_unique_asset_count": previous_snapshot.get("unique_asset_count"),
                "previous_token_count": previous_snapshot.get("token_count"),
                "previous_unique_token_contract_count": previous_snapshot.get(
                    "unique_token_contract_count"
                ),
                "previous_coverage_row_count": previous_snapshot.get("coverage_row_count"),
            }
            for field, expected in previous_count_contract.items():
                if summary.get(field) != expected:
                    errors.append(
                        f"daily summary.{field} does not reconcile with the previous snapshot"
                    )
            previous_generated_at = _parse_datetime(previous_snapshot.get("generated_at"))
            same_snapshot = _same_snapshot(previous_snapshot, expected_snapshot)
            if alert_level == "snapshot_unchanged":
                if comparison.get("state") != "snapshot_unchanged" or not same_snapshot:
                    errors.append("snapshot_unchanged decision does not compare the same snapshot")
            else:
                if comparison.get("state") != "verified_distinct_snapshots" or same_snapshot:
                    errors.append("daily diff does not compare verified distinct snapshots")
                if (
                    previous_generated_at is not None
                    and monitor_generated_at is not None
                    and previous_generated_at >= monitor_generated_at
                ):
                    errors.append("previous snapshot is not older than current snapshot")
    if alert_level == "no_new_feeds":
        if comparison.get("state") != "verified_distinct_snapshots":
            errors.append("no_new_feeds requires a verified comparison of distinct snapshots")
        if not isinstance(previous_snapshot, dict):
            errors.append("no_new_feeds requires a previous source snapshot")
        elif _same_snapshot(previous_snapshot, expected_snapshot):
            errors.append("no_new_feeds cannot compare a snapshot with itself")
        if any(
            summary.get(field) != 0
            for field in (
                "new_asset_count",
                "new_token_count",
                "removed_asset_count",
                "removed_token_count",
            )
        ):
            errors.append("no_new_feeds cannot contain additions or removals")

    status = report.get("status")
    if not isinstance(status, dict) or status.get("acceptance") != "passed":
        errors.append("daily status.acceptance is not passed")
    return errors


def _fail_closed_report(report: dict[str, Any], errors: list[str]) -> dict[str, Any]:
    rejected = deepcopy(report)
    summary = rejected.get("summary")
    if not isinstance(summary, dict):
        summary = {}
        rejected["summary"] = summary
    reported_alert_level = summary.get("alert_level")
    if reported_alert_level is not None:
        summary["reported_alert_level"] = reported_alert_level
    summary["alert_level"] = "source_snapshot_rejected"
    rejected["status"] = {
        "acceptance": "failed_closed",
        "decision_usable": False,
        "snapshot_reconciled": False,
        "errors": list(errors),
    }
    for key in ("new_assets", "new_tokens", "removed_assets", "removed_tokens", "sourcing_actions"):
        rejected[key] = []
    rejected["next_steps"] = [
        "Regenerate the daily report from the current canonical RWA.xyz monitor artifact before making discovery decisions."
    ]
    return rejected


def write_daily_feed_agent_report(
    *,
    input_path: str | Path | None = None,
    json_path: str | Path = DEFAULT_DAILY_AGENT_JSON_PATH,
    csv_path: str | Path = DEFAULT_DAILY_AGENT_CSV_PATH,
    history_dir: str | Path = DEFAULT_DAILY_AGENT_HISTORY_DIR,
    refresh_json_path: str | Path = DEFAULT_RWA_XYZ_REPORT_JSON_PATH,
    refresh_asset_csv_path: str | Path = DEFAULT_RWA_XYZ_ASSET_CSV_PATH,
    refresh_token_csv_path: str | Path = DEFAULT_RWA_XYZ_TOKEN_CSV_PATH,
    timeout: float = 30.0,
) -> dict[str, Any]:
    """Refresh RWA.xyz, verify the snapshot, and atomically publish daily outputs."""
    previous_report = load_rwa_xyz_monitor_report(refresh_json_path)
    if input_path:
        payload, metadata = load_payload_from_file(input_path)
    else:
        payload, metadata = fetch_rwa_xyz_monitor_payload(timeout=timeout)
    refresh_json_out = Path(refresh_json_path)
    refresh_asset_csv_out = Path(refresh_asset_csv_path)
    refresh_token_csv_out = Path(refresh_token_csv_path)
    refresh_json_out.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        dir=refresh_json_out.parent,
        prefix=".rwa_xyz_refresh.",
    ) as staging_dir:
        staging_root = Path(staging_dir)
        staged_json = staging_root / "monitor.json"
        staged_assets = staging_root / "assets.csv"
        staged_tokens = staging_root / "tokens.csv"
        current_report = write_rwa_xyz_monitor_reports(
            json_path=staged_json,
            asset_csv_path=staged_assets,
            token_csv_path=staged_tokens,
            payload=payload,
            fetch_metadata=metadata,
        )
        staged_report = json.loads(staged_json.read_text(encoding="utf-8"))
        if rwa_xyz_monitor_snapshot_identity(staged_report) != rwa_xyz_monitor_snapshot_identity(
            current_report
        ):
            raise ValueError("staged RWA.xyz monitor artifact did not reconcile with generated data")
        _atomic_write_bytes(refresh_asset_csv_out, staged_assets.read_bytes())
        _atomic_write_bytes(refresh_token_csv_out, staged_tokens.read_bytes())
        _atomic_write_bytes(refresh_json_out, staged_json.read_bytes())

    canonical_report = load_rwa_xyz_monitor_report(refresh_json_out)
    if rwa_xyz_monitor_snapshot_identity(canonical_report) != rwa_xyz_monitor_snapshot_identity(
        current_report
    ):
        raise ValueError("published RWA.xyz monitor artifact did not reconcile with generated data")
    report = build_daily_feed_agent_report(
        previous_report=previous_report,
        current_report=canonical_report,
        current_report_path=refresh_json_out,
    )
    return _publish_daily_feed_agent_report(
        report,
        current_report=canonical_report,
        trusted_current_report_path=refresh_json_out,
        json_path=json_path,
        csv_path=csv_path,
        history_dir=history_dir,
    )


def write_daily_feed_agent_baseline(
    *,
    json_path: str | Path = DEFAULT_DAILY_AGENT_JSON_PATH,
    csv_path: str | Path = DEFAULT_DAILY_AGENT_CSV_PATH,
    history_dir: str | Path = DEFAULT_DAILY_AGENT_HISTORY_DIR,
    current_report_path: str | Path = DEFAULT_RWA_XYZ_REPORT_JSON_PATH,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Reconcile an untrusted/missing daily artifact without inventing a historical diff."""
    current_report = load_rwa_xyz_monitor_report(current_report_path)
    report = build_daily_feed_agent_report(
        previous_report={},
        current_report=current_report,
        generated_at=generated_at,
        current_report_path=current_report_path,
    )
    return _publish_daily_feed_agent_report(
        report,
        current_report=current_report,
        trusted_current_report_path=current_report_path,
        json_path=json_path,
        csv_path=csv_path,
        history_dir=history_dir,
    )


def _publish_daily_feed_agent_report(
    report: dict[str, Any],
    *,
    current_report: dict[str, Any],
    trusted_current_report_path: str | Path | None,
    json_path: str | Path,
    csv_path: str | Path,
    history_dir: str | Path,
) -> dict[str, Any]:
    errors = validate_daily_feed_agent_report(report, current_report=current_report)
    if errors:
        raise ValueError("daily RWA feed report failed acceptance: " + "; ".join(errors))

    source = report.get("source") if isinstance(report.get("source"), dict) else {}
    canonical_path = (
        Path(trusted_current_report_path).expanduser()
        if trusted_current_report_path is not None
        else resolve_rwa_report_reference(
            source.get("current_report"),
            default_filename="rwa_xyz_new_asset_monitor.json",
        )
    )
    on_disk_report = load_rwa_xyz_monitor_report(canonical_path)
    if rwa_xyz_monitor_snapshot_identity(on_disk_report) != rwa_xyz_monitor_snapshot_identity(
        current_report
    ):
        raise ValueError("canonical RWA.xyz monitor changed before daily report publication")

    report_timestamp = _parse_datetime(report.get("generated_at"))
    if report_timestamp is None:
        raise ValueError("daily report generated_at is missing or invalid")
    json_out = Path(json_path)
    csv_out = Path(csv_path)
    history_out = Path(history_dir) / f"{report_timestamp.date().isoformat()}.json"
    _write_new_token_csv(csv_out, report["new_tokens"], report["sourcing_actions"])
    serialized = _canonical_json_text(report)
    _atomic_write_text(history_out, serialized)
    _atomic_write_text(json_out, serialized)
    return report


def load_daily_feed_agent_report(
    path: str | Path = DEFAULT_DAILY_AGENT_JSON_PATH,
    *,
    current_report_path: str | Path | None = None,
) -> dict[str, Any]:
    report_path = Path(path)
    if not report_path.exists():
        return {
            "product": "rwa_daily_feed_discovery_agent",
            "generated_at": None,
            "summary": {
                "alert_level": "report_missing",
                "current_asset_count": 0,
                "current_token_count": 0,
                "current_coverage_row_count": 0,
                "new_asset_count": 0,
                "new_token_count": 0,
                "new_p0_token_count": 0,
            },
            "new_assets": [],
            "new_tokens": [],
            "sourcing_actions": [],
            "status": {
                "acceptance": "failed_closed",
                "decision_usable": False,
                "snapshot_reconciled": False,
                "errors": ["daily report is missing"],
            },
            "next_steps": ["Run scripts/run_rwa_daily_feed_agent.py to create the first daily report."],
        }
    try:
        payload = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {
            "product": "rwa_daily_feed_discovery_agent",
            "generated_at": None,
            "summary": {"alert_level": "report_unreadable"},
            "new_assets": [],
            "new_tokens": [],
            "sourcing_actions": [],
            "status": {
                "acceptance": "failed_closed",
                "decision_usable": False,
                "snapshot_reconciled": False,
                "errors": ["daily report is unreadable"],
            },
        }
    if not isinstance(payload, dict):
        return _fail_closed_report({}, ["daily report root is not an object"])
    source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
    try:
        canonical_path = (
            Path(current_report_path).expanduser()
            if current_report_path is not None
            else resolve_rwa_report_reference(
                source.get("current_report"),
                default_filename="rwa_xyz_new_asset_monitor.json",
            )
        )
    except ValueError:
        sanitized_payload = deepcopy(payload)
        sanitized_source = sanitized_payload.get("source")
        if isinstance(sanitized_source, dict):
            sanitized_source["current_report"] = None
        return _fail_closed_report(
            sanitized_payload,
            ["daily persisted source reference is unsafe"],
        )
    current_report = load_rwa_xyz_monitor_report(canonical_path)
    errors = validate_daily_feed_agent_report(payload, current_report=current_report)
    return _fail_closed_report(payload, errors) if errors else payload


def build_daily_feed_agent_view(
    *,
    include_rows: bool = False,
    row_limit: int = 100,
    path: str | Path = DEFAULT_DAILY_AGENT_JSON_PATH,
    current_report_path: str | Path | None = None,
) -> dict[str, Any]:
    report = load_daily_feed_agent_report(
        path,
        current_report_path=current_report_path,
    )
    result = {
        key: deepcopy(value)
        for key, value in report.items()
        if key not in {"new_assets", "new_tokens", "removed_assets", "removed_tokens", "sourcing_actions"}
    }
    result["report_path"] = str(path)
    result["available_row_sets"] = {
        "new_assets": len(report.get("new_assets") or []),
        "new_tokens": len(report.get("new_tokens") or []),
        "removed_assets": len(report.get("removed_assets") or []),
        "removed_tokens": len(report.get("removed_tokens") or []),
        "sourcing_actions": len(report.get("sourcing_actions") or []),
    }
    if include_rows:
        limit = max(0, int(row_limit))
        result["new_assets"] = deepcopy((report.get("new_assets") or [])[:limit])
        result["new_tokens"] = deepcopy((report.get("new_tokens") or [])[:limit])
        result["sourcing_actions"] = deepcopy((report.get("sourcing_actions") or [])[:limit])
    return result


def _write_new_token_csv(
    path: Path,
    token_rows: list[dict[str, Any]],
    action_rows: list[dict[str, Any]],
) -> None:
    actions_by_key = {
        f"{row.get('network_slug')}:{str(row.get('address') or '').lower()}": row
        for row in action_rows
    }
    fieldnames = [
        "priority",
        "lane",
        "asset_id",
        "symbol",
        "asset_class",
        "rwa_xyz_ticker",
        "asset_name",
        "issuer_name",
        "platform",
        "network",
        "address",
        "standards",
        "next_action",
    ]
    handle = io.StringIO(newline="")
    writer = csv.DictWriter(handle, fieldnames=fieldnames)
    writer.writeheader()
    for token in token_rows:
        action = actions_by_key.get(
            f"{token.get('network_slug')}:{str(token.get('address') or '').lower()}",
            {},
        )
        writer.writerow(
            {
                "priority": action.get("priority"),
                "lane": action.get("lane"),
                "asset_id": token.get("asset_id"),
                "symbol": token.get("symbol"),
                "asset_class": token.get("asset_class"),
                "rwa_xyz_ticker": token.get("rwa_xyz_ticker"),
                "asset_name": token.get("asset_name"),
                "issuer_name": token.get("issuer_name"),
                "platform": token.get("platform"),
                "network": token.get("network"),
                "address": token.get("address"),
                "standards": json.dumps(token.get("standards") or []),
                "next_action": action.get("next_action"),
            }
        )
    _atomic_write_text(path, handle.getvalue())
