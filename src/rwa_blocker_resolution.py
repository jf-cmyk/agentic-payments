"""Consolidated blocker-resolution ledger for RWA production promotion."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from src.rwa_feed_discovery import DEFAULT_REPORTS_DIR, build_feed_discovery_audit
from src.rwa_replay_inventory import build_route_pool_replay_inventory
from src.rwa_source_readiness import build_source_readiness
from src.rwa_source_rights import build_source_rights_registry
from src.runtime_data import resolve_required_rwa_report_path


DEFAULT_BLOCKER_RESOLUTION_JSON_PATH = DEFAULT_REPORTS_DIR / "rwa_blocker_resolution.json"
DEFAULT_BLOCKER_RESOLUTION_CSV_PATH = DEFAULT_REPORTS_DIR / "rwa_blocker_resolution.csv"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _issue(
    issue_id: str,
    status: str,
    resolved_count: int,
    remaining_count: int,
    evidence: dict[str, Any],
    next_action: str,
) -> dict[str, Any]:
    return {
        "issue_id": issue_id,
        "status": status,
        "resolved_count": resolved_count,
        "remaining_count": remaining_count,
        "evidence": evidence,
        "next_action": next_action,
    }


def build_blocker_resolution_ledger() -> dict[str, Any]:
    """Return an auditable ledger of resolved and still-blocked production gates."""
    discovery = build_feed_discovery_audit(include_feed_details=False)
    replay = build_route_pool_replay_inventory()
    readiness = build_source_readiness()
    rights = build_source_rights_registry()

    discovery_summary = discovery["summary"]
    replay_summary = replay["summary"]
    rights_summary = rights["summary"]
    missing_gates = discovery_summary.get("by_missing_gate") or {}

    readiness_by_id = {
        row["dependency_id"]: row
        for row in readiness.get("dependencies", [])
        if isinstance(row, dict) and row.get("dependency_id")
    }
    source_rows = {row["venue"]: row for row in rights.get("rows", [])}
    evm_allowlist = _read_json(
        resolve_required_rwa_report_path("rwa_evm_pool_allowlist.json")
    )
    evm_allowlist_summary = evm_allowlist.get("summary") if isinstance(evm_allowlist.get("summary"), dict) else {}
    evm_replay_rows = [
        row
        for row in replay.get("rows", [])
        if isinstance(row, dict) and row.get("chain") in {"ethereum_or_evm", "base"}
    ]
    evm_replay_ready = sum(
        1 for row in evm_replay_rows if row.get("replay_status") == "pool_replay_ready_pending_live_quality"
    )
    evm_identity_found = sum(
        1 for row in evm_replay_rows if row.get("replay_status") != "missing_pool_allowlist"
    )
    evm_missing_pool = sum(
        1 for row in evm_replay_rows if row.get("replay_status") == "missing_pool_allowlist"
    )
    evm_incomplete_pool_state = sum(
        1 for row in evm_replay_rows if row.get("replay_status") == "pool_replay_incomplete"
    )
    evm_issue_status = (
        "resolved_to_replay_evidence"
        if evm_replay_rows and evm_replay_ready == len(evm_replay_rows)
        else "partially_resolved_to_replay_evidence"
        if evm_replay_ready or int(evm_allowlist_summary.get("block_state_captured") or 0)
        else "externally_blocked"
    )

    def missing_env_for_unconfigured(*dependency_ids: str) -> list[str]:
        missing: set[str] = set()
        for dependency_id in dependency_ids:
            dependency = readiness_by_id.get(dependency_id) or {}
            if dependency.get("status") == "configured":
                continue
            missing.update(str(item) for item in dependency.get("missing_required_env") or [])
        return sorted(missing)

    rows = [
        _issue(
            "rights_and_redistribution",
            "resolved",
            int(rights_summary.get("feed_count") or 0),
            int(missing_gates.get("rights_and_redistribution") or 0),
            {
                "production_rights_cleared": rights_summary.get("production_rights_cleared"),
                "blocked_or_missing_rights": rights_summary.get("blocked_or_missing_rights"),
                "clearance_evidence": rights.get("clearance_evidence"),
            },
            "Keep entitlement metadata attached to each observation and promotion receipt.",
        ),
        _issue(
            "solana_pool_allowlist_and_slot_state",
            "resolved_to_replay_evidence",
            int(replay_summary.get("pool_state_available") or 0),
            sum(
                1
                for row in replay.get("rows", [])
                if row.get("chain") == "solana" and row.get("replay_status") == "missing_pool_allowlist"
            ),
            {
                "solana_pool_allowlist_status": (readiness_by_id.get("solana_pool_allowlist") or {}).get("status"),
                "solana_source_access_ready": {
                    venue: source_rows.get(venue, {}).get("source_access_ready")
                    for venue in ("raydium_clmm", "orca_whirlpool", "meteora_dlmm")
                },
                "replay_ready": replay_summary.get("replay_ready"),
                "pool_state_available": replay_summary.get("pool_state_available"),
            },
            "Decode pool-specific fee tiers/ticks/bins and run live 30-minute liquidity windows.",
        ),
        _issue(
            "evm_pool_allowlist_and_rpc_state",
            evm_issue_status,
            evm_replay_ready,
            evm_missing_pool + evm_incomplete_pool_state,
            {
                "evm_rpc_multichain_status": (readiness_by_id.get("evm_rpc_multichain") or {}).get("status"),
                "evm_pool_allowlist_status": (readiness_by_id.get("evm_pool_allowlist") or {}).get("status"),
                "pool_identity_found": evm_identity_found,
                "pool_replay_ready": evm_replay_ready,
                "incomplete_pool_state": evm_incomplete_pool_state,
                "missing_pool_identity": evm_missing_pool,
                "public_pair_search_pool_count": evm_allowlist_summary.get("pool_count"),
                "public_pair_search_missing_pair_count": evm_allowlist_summary.get("missing_pair_count"),
                "block_state_captured": evm_allowlist_summary.get("block_state_captured"),
                "public_rpc_fallback_used": int(evm_allowlist_summary.get("block_state_captured") or 0) > 0,
                "missing_env": missing_env_for_unconfigured("evm_rpc_multichain", "evm_pool_allowlist"),
            },
            "Add missing EVM pair identities, implement Balancer weighted-pool state decoding, and provision dedicated production EVM RPC URLs before promotion.",
        ),
        _issue(
            "issuer_nav_alignment",
            "externally_blocked",
            0,
            int(missing_gates.get("issuer_nav_alignment") or 0),
            {
                "treasury_issuer_pack_status": (readiness_by_id.get("treasury_issuer_pack") or {}).get("status"),
                "missing_env": (readiness_by_id.get("treasury_issuer_pack") or {}).get("missing_required_env"),
            },
            "Provide issuer NAV/reserve endpoints or reviewed issuer artifacts for OpenEden, Superstate, and Matrixdock-style rows.",
        ),
        _issue(
            "continuous_quality_windows",
            "externally_blocked",
            int(discovery_summary.get("feed_count") or 0) - int(missing_gates.get("freshness_cadence") or 0),
            int(missing_gates.get("freshness_cadence") or 0),
            {
                "freshness_missing": missing_gates.get("freshness_cadence"),
                "liquidity_missing": missing_gates.get("liquidity_depth_volume"),
                "manipulation_missing": missing_gates.get("manipulation_concentration"),
                "benchmark_missing": missing_gates.get("blocksize_benchmark_alignment"),
            },
            "Run 30-minute per-feed windows with observation storage enabled; keep feeds candidate-only until the windows pass.",
        ),
        _issue(
            "production_promotion",
            "blocked_until_quality_passes",
            int(discovery_summary.get("production_promoted") or 0),
            int(discovery_summary.get("blocked_from_production") or 0),
            {
                "production_promoted": discovery_summary.get("production_promoted"),
                "blocked_from_production": discovery_summary.get("blocked_from_production"),
                "by_status": discovery_summary.get("by_status"),
            },
            "Do not promote any feed until every required gate is passed, not just evidenced.",
        ),
    ]

    return {
        "product": "rwa_blocker_resolution_ledger",
        "summary": {
            "issue_count": len(rows),
            "resolved_issue_count": sum(1 for row in rows if row["status"] == "resolved"),
            "resolved_to_evidence_issue_count": sum(
                1 for row in rows if row["status"] == "resolved_to_replay_evidence"
            ),
            "partially_resolved_issue_count": sum(
                1 for row in rows if row["status"] == "partially_resolved_to_replay_evidence"
            ),
            "externally_blocked_issue_count": sum(1 for row in rows if row["status"] == "externally_blocked"),
            "production_promoted": discovery_summary.get("production_promoted"),
            "blocked_from_production": discovery_summary.get("blocked_from_production"),
            "rights_blockers_remaining": missing_gates.get("rights_and_redistribution", 0),
            "replay_ready": replay_summary.get("replay_ready"),
            "source_access_ready": rights_summary.get("production_access_ready"),
        },
        "policy": {
            "promotion_rule": "A blocker is resolved for production only when the corresponding gate is passed; evidence-only rows remain candidate feeds.",
            "external_blocker_rule": "Missing RPC, issuer, or live-window evidence cannot be resolved by code without access to the external source.",
        },
        "rows": rows,
    }


def write_blocker_resolution_reports(
    *,
    json_path: str | Path = DEFAULT_BLOCKER_RESOLUTION_JSON_PATH,
    csv_path: str | Path = DEFAULT_BLOCKER_RESOLUTION_CSV_PATH,
) -> dict[str, Any]:
    """Write blocker-resolution reports."""
    ledger = build_blocker_resolution_ledger()
    json_out = Path(json_path)
    csv_out = Path(csv_path)
    json_out.parent.mkdir(parents=True, exist_ok=True)
    csv_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    fieldnames = ["issue_id", "status", "resolved_count", "remaining_count", "evidence", "next_action"]
    with csv_out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in ledger["rows"]:
            writer.writerow(
                {
                    key: json.dumps(row[key], sort_keys=True) if isinstance(row.get(key), dict) else row.get(key)
                    for key in fieldnames
                }
            )
    return ledger
