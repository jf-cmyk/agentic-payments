"""Promotion-gate discovery audit for sourced RWA feed definitions."""

from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.rwa_non_crypto_feeds import build_non_crypto_feed_catalog
from src.rwa_rights_clearance import (
    load_rights_clearance,
    rights_clearance_summary,
    rights_cleared_for_venue,
)
from src.runtime_data import RWA_REPORTS_DIR, effective_rwa_report_paths


DEFAULT_REPORTS_DIR = RWA_REPORTS_DIR
DEFAULT_DISCOVERY_JSON_PATH = DEFAULT_REPORTS_DIR / "rwa_feed_discovery.json"
DEFAULT_DISCOVERY_CSV_PATH = DEFAULT_REPORTS_DIR / "rwa_feed_discovery.csv"

PROMOTED_GATE_STATUS = "passed"
BLOCKING_GATE_STATUSES = {"missing", "blocked", "evidence_found"}

GATE_DEFINITIONS: dict[str, dict[str, str]] = {
    "canonical_identity": {
        "description": "Canonical symbol, asset class, and underlying identity are verified.",
        "production_rule": "Required for every feed before it can enter production.",
    },
    "venue_identifier": {
        "description": "Venue-specific market, coin, pair, contract, or feed identifier is known.",
        "production_rule": "Required for every source because symbols alone are not executable identifiers.",
    },
    "token_or_contract_discovery": {
        "description": "Onchain token mint or EVM contract identity is verified against issuer/registry data.",
        "production_rule": "Required for DEX, tokenized fund, and tokenized security rows.",
    },
    "route_or_pool_discovery": {
        "description": "Executable route, pool, fee tier, tick/bin state, or router path is discovered.",
        "production_rule": "Required for quote-sweep and pool-state feeds before they can represent live liquidity.",
    },
    "state_instrument_confirmation": {
        "description": "Blocksize state_instruments has matching pool/instrument coverage for the symbol.",
        "production_rule": "Required for Blocksize state rows and state benchmark mappings.",
    },
    "liquidity_depth_volume": {
        "description": "Live depth, fillability, price impact, and organic 24h volume clear asset-class minimums.",
        "production_rule": "Required for every live-liquidity source and every feed that contributes to VWAP.",
    },
    "freshness_cadence": {
        "description": "Observed ticks or state snapshots meet freshness and cadence requirements over a live window.",
        "production_rule": "Required for real-time production use; point-in-time probes are supplemental evidence only.",
    },
    "manipulation_concentration": {
        "description": "Venue concentration, pool ownership, liquidity source quality, and outlier risks are checked.",
        "production_rule": "Required before any onchain or thin venue can influence consensus.",
    },
    "issuer_nav_alignment": {
        "description": "Issuer NAV, administrator reference, attestation, or reserve data aligns with market quotes.",
        "production_rule": "Required for treasury funds, tokenized funds, NAV references, and issuer-priced assets.",
    },
    "blocksize_benchmark_alignment": {
        "description": "Live observations are benchmarked against Blocksize or a state/fair-value reference.",
        "production_rule": "Required for replacement analysis and agentic-payment feed quality scoring.",
    },
    "rights_and_redistribution": {
        "description": "API terms, exchange data rights, issuer terms, and redistribution policy are cleared.",
        "production_rule": "Required for production redistribution, even for technically accessible feeds.",
    },
    "replayable_payload": {
        "description": "Raw request/response, route, pool-state, order-book, or state payload can be replayed.",
        "production_rule": "Required for QA, incident review, and deterministic feed benchmarking.",
    },
}

ONCHAIN_SOURCE_TYPES = {"quote_sweep", "onchain_clmm_pool", "onchain_stableswap_pool"}
TOKENIZED_ASSET_CLASSES = {"equity", "etf", "treasury_fund", "tokenized_fund"}
ISSUER_NAV_ASSET_CLASSES = {"treasury_fund", "tokenized_fund"}


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _rows_from(payload: dict[str, Any], *keys: str) -> list[dict[str, Any]]:
    for key in keys:
        rows = payload.get(key)
        if isinstance(rows, list):
            return [row for row in rows if isinstance(row, dict)]
    return []


def _normal_symbol(value: Any) -> str:
    return str(value or "").upper().replace("-", "/")


def _base_symbol(symbol: str) -> str:
    return _normal_symbol(symbol).split("/", 1)[0]


def _allowlist_id_for(feed: dict[str, Any]) -> str:
    base = _base_symbol(str(feed.get("symbol") or ""))
    quote = "USD"
    symbol = _normal_symbol(feed.get("symbol"))
    if "/" in symbol:
        quote = symbol.split("/", 1)[1]
    return f"dex:{feed.get('venue')}:{base}:{quote}"


def _gate(
    *,
    status: str,
    message: str,
    required: bool = True,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "required": required,
        "message": message,
        "evidence": evidence or {},
    }


def _not_applicable(message: str) -> dict[str, Any]:
    return _gate(status="not_applicable", required=False, message=message)


def _evidence_found(message: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return _gate(status="evidence_found", message=message, evidence=evidence)


def _passed(message: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return _gate(status="passed", message=message, evidence=evidence)


def _missing(message: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return _gate(status="missing", message=message, evidence=evidence)


def _blocked(message: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return _gate(status="blocked", message=message, evidence=evidence)


def _load_jupiter_route_evidence(reports_dir: Path) -> dict[str, dict[str, Any]]:
    payload = _read_json(
        effective_rwa_report_paths(reports_dir=reports_dir)[
            "rwa_jupiter_route_allowlist.json"
        ]
    )
    route_rows = _rows_from(payload, "routes", "candidates", "allowlist")
    evidence: dict[str, dict[str, Any]] = {}
    for row in route_rows:
        allowlist_id = str(row.get("allowlist_id") or "")
        if allowlist_id:
            evidence[allowlist_id] = row
        venue = str(row.get("venue") or "jupiter_router")
        symbol = str(row.get("symbol") or "")
        if symbol:
            base = _base_symbol(symbol)
            quote = "USD"
            if "/" in _normal_symbol(symbol):
                quote = _normal_symbol(symbol).split("/", 1)[1]
            evidence[f"dex:{venue}:{base}:{quote}"] = row
    return evidence


def _load_solana_token_evidence(reports_dir: Path) -> dict[str, dict[str, Any]]:
    payload = _read_json(
        effective_rwa_report_paths(reports_dir=reports_dir)[
            "rwa_solana_token_mints.json"
        ]
    )
    token_rows = _rows_from(payload, "tokens", "targets", "registry")
    evidence: dict[str, dict[str, Any]] = {}
    for row in token_rows:
        keys = [
            row.get("token_key"),
            row.get("symbol"),
            row.get("source_symbol"),
            row.get("mint"),
        ]
        for key in keys:
            if key:
                evidence[str(key).upper()] = row
        for source_symbol in row.get("source_symbols") or []:
            evidence[_base_symbol(str(source_symbol))] = row
    return evidence


def _load_state_evidence(reports_dir: Path) -> dict[str, dict[str, Any]]:
    payload = _read_json(
        effective_rwa_report_paths(reports_dir=reports_dir)[
            "rwa_blocksize_state_discovery.json"
        ]
    )
    state_rows = _rows_from(payload, "symbols", "rows", "targets")
    evidence: dict[str, dict[str, Any]] = {}
    for row in state_rows:
        for key in (row.get("symbol"), row.get("state_symbol"), row.get("pair")):
            if key:
                evidence[str(key).upper().replace("/", "")] = row
    return evidence


def _load_hyperliquid_probe_evidence(reports_dir: Path) -> dict[str, dict[str, Any]]:
    payload = _read_json(
        effective_rwa_report_paths(reports_dir=reports_dir)[
            "rwa_hyperliquid_paxg_probe.json"
        ]
    )
    evidence: dict[str, dict[str, Any]] = {}
    result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    for row in _rows_from(result, "results"):
        job = row.get("job") if isinstance(row.get("job"), dict) else {}
        bidask = row.get("bidask") if isinstance(row.get("bidask"), dict) else {}
        symbol = bidask.get("symbol") or job.get("symbol")
        venue = bidask.get("venue") or job.get("venue") or "hyperliquid_paxg"
        if symbol:
            evidence[f"{venue}:{_normal_symbol(symbol)}"] = row
    return evidence


def _load_hyperliquid_candidate_evidence(reports_dir: Path) -> dict[str, dict[str, Any]]:
    payload = _read_json(reports_dir / "rwa_hyperliquid_rwa_candidates.json")
    rows = _rows_from(payload, "active_spot_pairs", "rows", "candidates")
    evidence: dict[str, dict[str, Any]] = {}
    for row in rows:
        display_pair = row.get("display_pair")
        if display_pair:
            evidence[_normal_symbol(display_pair)] = row
        symbol = row.get("symbol")
        if symbol:
            evidence[str(symbol).upper()] = row
    return evidence


def _load_replay_inventory_evidence(reports_dir: Path) -> dict[str, dict[str, Any]]:
    payload = _read_json(reports_dir / "rwa_route_pool_replay_inventory.json")
    rows = _rows_from(payload, "rows", "candidates")
    evidence: dict[str, dict[str, Any]] = {}
    for row in rows:
        allowlist_id = str(row.get("allowlist_id") or "")
        if allowlist_id:
            evidence[allowlist_id] = row
        venue = row.get("venue")
        symbol = row.get("symbol")
        if venue and symbol:
            base, quote = _base_symbol(str(symbol)), "USD"
            if "/" in _normal_symbol(symbol):
                quote = _normal_symbol(symbol).split("/", 1)[1]
            evidence[f"dex:{venue}:{base}:{quote}"] = row
    return evidence


def _load_discovery_evidence(reports_dir: Path) -> dict[str, Any]:
    rights_clearance = load_rights_clearance()
    return {
        "jupiter_routes": _load_jupiter_route_evidence(reports_dir),
        "solana_tokens": _load_solana_token_evidence(reports_dir),
        "blocksize_state": _load_state_evidence(reports_dir),
        "hyperliquid_probe": _load_hyperliquid_probe_evidence(reports_dir),
        "hyperliquid_candidates": _load_hyperliquid_candidate_evidence(reports_dir),
        "replay_inventory": _load_replay_inventory_evidence(reports_dir),
        "rights_clearance": rights_clearance,
        "rights_clearance_summary": rights_clearance_summary(rights_clearance),
    }


def _required_gates_for(feed: dict[str, Any]) -> list[str]:
    source_type = str(feed.get("source_type") or "")
    venue = str(feed.get("venue") or "")
    asset_classes = set(feed.get("asset_classes") or [])
    benchmark = feed.get("blocksize_benchmark") or {}
    required = [
        "canonical_identity",
        "venue_identifier",
        "liquidity_depth_volume",
        "freshness_cadence",
        "manipulation_concentration",
        "blocksize_benchmark_alignment",
        "rights_and_redistribution",
        "replayable_payload",
    ]
    if source_type in ONCHAIN_SOURCE_TYPES:
        required.extend(["token_or_contract_discovery", "route_or_pool_discovery"])
    if asset_classes.intersection(TOKENIZED_ASSET_CLASSES) and source_type in ONCHAIN_SOURCE_TYPES:
        if "token_or_contract_discovery" not in required:
            required.append("token_or_contract_discovery")
    if venue == "blocksize_state" or source_type == "blocksize_state_reference" or benchmark.get("service") == "state":
        required.append("state_instrument_confirmation")
    if asset_classes.intersection(ISSUER_NAV_ASSET_CLASSES) or source_type == "nav_reference":
        required.append("issuer_nav_alignment")
    return list(dict.fromkeys(required))


def _base_gates(feed: dict[str, Any]) -> dict[str, dict[str, Any]]:
    required = set(_required_gates_for(feed))
    gates: dict[str, dict[str, Any]] = {}
    for gate_id in GATE_DEFINITIONS:
        if gate_id in required:
            gates[gate_id] = _missing("No discovery evidence recorded for this required gate.")
        else:
            gates[gate_id] = _not_applicable("Not required for this feed/source type.")
    return gates


def _apply_identity_gates(feed: dict[str, Any], gates: dict[str, dict[str, Any]]) -> None:
    metadata = feed.get("metadata") if isinstance(feed.get("metadata"), dict) else {}
    coverage_status = str(feed.get("coverage_status") or "")
    if "unverified_identity" in coverage_status or feed.get("support") == "requires_identity_verification":
        gates["canonical_identity"] = _blocked(
            "Identity is explicitly marked unverified and needs manual issuer/instrument review.",
            {"coverage_status": coverage_status},
        )
        return
    if feed.get("asset_id") and feed.get("asset_classes"):
        gates["canonical_identity"] = _passed(
            "Canonical asset id and asset class are present in the registry.",
            {"asset_id": feed.get("asset_id"), "asset_classes": feed.get("asset_classes")},
        )
    if metadata.get("hyperliquid_coin") or metadata.get("pair_index"):
        gates["venue_identifier"] = _passed(
            "Hyperliquid venue identifier is mapped.",
            {
                "hyperliquid_coin": metadata.get("hyperliquid_coin"),
                "pair_index": metadata.get("pair_index"),
            },
        )
    elif metadata.get("state_symbol"):
        gates["venue_identifier"] = _evidence_found(
            "Blocksize state symbol is mapped, pending state_instruments confirmation.",
            {"state_symbol": metadata.get("state_symbol")},
        )
    elif feed.get("venue") in {"ostium", "gains", "hyperliquid_paxg"}:
        gates["venue_identifier"] = _evidence_found(
            "Documented venue symbol exists, pending live market/instrument replay.",
            {"symbol": feed.get("symbol"), "venue": feed.get("venue")},
        )
    elif metadata.get("venue_market_id") or metadata.get("venue_symbol"):
        gates["venue_identifier"] = _evidence_found(
            "Venue market identifier is mapped; live order-book, funding, and replay evidence are still required.",
            {
                "venue_market_id": metadata.get("venue_market_id"),
                "venue_symbol": metadata.get("venue_symbol"),
                "market_type": metadata.get("market_type"),
            },
        )
    elif feed.get("source_type") in ONCHAIN_SOURCE_TYPES:
        gates["venue_identifier"] = _missing(
            "Symbol is seeded, but token/pool or route identifier still needs discovery."
        )


def _apply_jupiter_gates(
    feed: dict[str, Any],
    gates: dict[str, dict[str, Any]],
    evidence: dict[str, Any],
) -> None:
    if feed.get("venue") != "jupiter_router":
        return
    route = evidence["jupiter_routes"].get(_allowlist_id_for(feed))
    token = evidence["solana_tokens"].get(_base_symbol(str(feed.get("symbol") or "")))
    if token:
        token_status = str(token.get("status") or token.get("review_status") or "")
        verified = bool(token.get("is_verified")) or "verified" in token_status.lower()
        status = "passed" if verified else "evidence_found"
        gates["token_or_contract_discovery"] = _gate(
            status=status,
            required=True,
            message=(
                "Solana token registry evidence exists."
                if verified
                else "Token evidence exists but verification status is not production-ready."
            ),
            evidence={
                "mint": token.get("mint"),
                "is_verified": token.get("is_verified"),
                "liquidity": token.get("liquidity"),
                "organic_score": token.get("organic_score"),
            },
        )
        if token.get("liquidity") is not None:
            gates["liquidity_depth_volume"] = _evidence_found(
                "Token-level liquidity evidence exists; live route depth and organic 24h volume still need validation.",
                {"liquidity": token.get("liquidity"), "organic_score": token.get("organic_score")},
            )
    if not route:
        gates["route_or_pool_discovery"] = _missing(
            "No Jupiter route evidence artifact was found for this allowlist id.",
            {"allowlist_id": _allowlist_id_for(feed)},
        )
        return
    if str(route.get("status")) == "route_discovered":
        gates["route_or_pool_discovery"] = _evidence_found(
            "Jupiter route was discovered; still needs route replay, liquidity window, and manipulation checks.",
            {
                "allowlist_id": route.get("allowlist_id"),
                "context_slot": route.get("context_slot"),
                "route_labels": route.get("route_labels") or route.get("dex_labels"),
            },
        )
        gates["replayable_payload"] = _evidence_found(
            "Route quote payload is available as point-in-time evidence.",
            {"allowlist_id": route.get("allowlist_id"), "context_slot": route.get("context_slot")},
        )
        if route.get("context_slot") is not None or route.get("timestamp"):
            gates["freshness_cadence"] = _evidence_found(
                "Point-in-time context slot/timestamp exists; a continuous 30-minute cadence test is still required.",
                {"context_slot": route.get("context_slot"), "timestamp": route.get("timestamp")},
            )
    else:
        gates["route_or_pool_discovery"] = _blocked(
            "Route artifact exists but the route was not discovered.",
            {
                "allowlist_id": route.get("allowlist_id"),
                "status": route.get("status"),
                "error": route.get("error"),
            },
        )


def _apply_state_gates(
    feed: dict[str, Any],
    gates: dict[str, dict[str, Any]],
    evidence: dict[str, Any],
) -> None:
    if gates["state_instrument_confirmation"]["status"] == "not_applicable":
        return
    metadata = feed.get("metadata") if isinstance(feed.get("metadata"), dict) else {}
    state_symbol = str(metadata.get("state_symbol") or feed.get("symbol") or "").upper().replace("/", "")
    row = evidence["blocksize_state"].get(state_symbol)
    if not row:
        gates["state_instrument_confirmation"] = _missing(
            "No Blocksize state_instruments discovery artifact confirms this state symbol.",
            {"state_symbol": state_symbol},
        )
        return
    match_count = int(row.get("match_count") or row.get("matched_instrument_count") or 0)
    if match_count > 0 or row.get("status") in {"matched", "state_instrument_matched"}:
        gates["state_instrument_confirmation"] = _evidence_found(
            "Blocksize state_instruments has matching coverage; live state_pool freshness still needs validation.",
            {"state_symbol": state_symbol, "match_count": match_count, "status": row.get("status")},
        )
        gates["replayable_payload"] = _evidence_found(
            "State discovery payload can be replayed from the local report.",
            {"state_symbol": state_symbol, "artifact": "reports/rwa_blocksize_state_discovery.json"},
        )
    else:
        gates["state_instrument_confirmation"] = _blocked(
            "State discovery artifact exists but no matching state instrument was found.",
            {"state_symbol": state_symbol, "status": row.get("status")},
        )


def _apply_replay_inventory_gates(
    feed: dict[str, Any],
    gates: dict[str, dict[str, Any]],
    evidence: dict[str, Any],
) -> None:
    if str(feed.get("source_type") or "") not in ONCHAIN_SOURCE_TYPES:
        return
    row = evidence["replay_inventory"].get(_allowlist_id_for(feed))
    if not row:
        return
    replay_status = str(row.get("replay_status") or "")
    replay_ready = replay_status in {
        "route_replay_ready_pending_liquidity_window",
        "pool_replay_ready_pending_live_quality",
    }
    if replay_ready:
        gates["route_or_pool_discovery"] = _evidence_found(
            "Route/pool replay inventory has mapped executable identifiers and replay evidence; live quality gates still need promotion windows.",
            {
                "allowlist_id": row.get("allowlist_id"),
                "replay_status": replay_status,
                "pool_or_route_ids": row.get("pool_or_route_ids"),
                "slot_or_block_numbers": row.get("slot_or_block_numbers"),
                "fee_tiers": row.get("fee_tiers"),
                "fee_tier_status": row.get("fee_tier_status"),
            },
        )
        gates["replayable_payload"] = _evidence_found(
            "Replay inventory has route or pool-state payload evidence.",
            {
                "allowlist_id": row.get("allowlist_id"),
                "raw_payload_artifact": row.get("raw_payload_artifact"),
                "raw_payload_available": row.get("raw_payload_available"),
                "replay_payload_fields": row.get("replay_payload_fields"),
            },
        )
        if row.get("slot_or_block_numbers"):
            gates["freshness_cadence"] = _evidence_found(
                "Point-in-time slot/block evidence exists; continuous 30-minute cadence is still required.",
                {"slot_or_block_numbers": row.get("slot_or_block_numbers")},
            )
    elif replay_status and replay_status != "missing_pool_allowlist":
        gates["route_or_pool_discovery"] = _blocked(
            "Replay inventory exists but is incomplete.",
            {
                "allowlist_id": row.get("allowlist_id"),
                "replay_status": replay_status,
                "missing_replay_fields": row.get("missing_replay_fields"),
            },
        )
    if row.get("base_identifier") and row.get("quote_identifier"):
        gates["token_or_contract_discovery"] = _evidence_found(
            "Token or contract identifiers are mapped in replay inventory.",
            {
                "base_identifier": row.get("base_identifier"),
                "quote_identifier": row.get("quote_identifier"),
                "token_identifier_status": row.get("token_identifier_status"),
            },
        )


def _apply_hyperliquid_gates(
    feed: dict[str, Any],
    gates: dict[str, dict[str, Any]],
    evidence: dict[str, Any],
) -> None:
    venue = str(feed.get("venue") or "")
    symbol = _normal_symbol(feed.get("symbol"))
    if not venue.startswith("hyperliquid"):
        return
    candidate = evidence["hyperliquid_candidates"].get(symbol) or evidence["hyperliquid_candidates"].get(_base_symbol(symbol))
    if candidate:
        gates["venue_identifier"] = _passed(
            "Hyperliquid spot candidate includes coin/index identifiers.",
            {
                "hyperliquid_coin": candidate.get("hyperliquid_coin"),
                "pair_index": candidate.get("pair_index"),
                "token_id": candidate.get("token_id"),
            },
        )
        if candidate.get("token_id") or candidate.get("evm_contract"):
            gates["token_or_contract_discovery"] = _evidence_found(
                "Token/contract identifier exists, pending issuer identity and transfer-restriction review.",
                {"token_id": candidate.get("token_id"), "evm_contract": candidate.get("evm_contract")},
            )
    probe = evidence["hyperliquid_probe"].get(f"{venue}:{symbol}")
    if not probe:
        return
    block_vwap = probe.get("block_vwap") if isinstance(probe.get("block_vwap"), dict) else {}
    bidask = probe.get("bidask") if isinstance(probe.get("bidask"), dict) else {}
    if block_vwap.get("status") == "full_fill":
        gates["liquidity_depth_volume"] = _evidence_found(
            "Point-in-time block VWAP filled; continuous liquidity and 24h volume still need validation.",
            {
                "block_size_usd": block_vwap.get("block_size_usd"),
                "fill_ratio": block_vwap.get("fill_ratio"),
                "slippage_bps": block_vwap.get("slippage_bps"),
            },
        )
    if bidask.get("timestamp"):
        gates["freshness_cadence"] = _evidence_found(
            "Point-in-time bid/ask timestamp exists; a 30-minute cadence benchmark is still required.",
            {"timestamp": bidask.get("timestamp")},
        )
        gates["replayable_payload"] = _evidence_found(
            "Hyperliquid probe payload can be replayed from the local report.",
            {"artifact": "reports/rwa_hyperliquid_paxg_probe.json"},
        )


def _apply_default_quality_gates(feed: dict[str, Any], gates: dict[str, dict[str, Any]]) -> None:
    benchmark = feed.get("blocksize_benchmark") if isinstance(feed.get("blocksize_benchmark"), dict) else {}
    status = str(benchmark.get("status") or "")
    if status == "ready_for_blocksize_benchmark":
        gates["blocksize_benchmark_alignment"] = _evidence_found(
            "Blocksize comparable symbol is mapped, but live drift test is not yet recorded.",
            {"service": benchmark.get("service"), "symbol": benchmark.get("symbol")},
        )
    elif status == "requires_blocksize_state_instrument_check":
        gates["blocksize_benchmark_alignment"] = _missing(
            "Blocksize state benchmark requires state_instruments confirmation before comparison.",
            {"service": benchmark.get("service"), "symbol": benchmark.get("symbol")},
        )
    else:
        gates["blocksize_benchmark_alignment"] = _missing(
            "No comparable Blocksize benchmark run is recorded for this feed.",
            {"benchmark_status": status, "service": benchmark.get("service"), "symbol": benchmark.get("symbol")},
        )

    source_type = str(feed.get("source_type") or "")
    if source_type in {"synthetic_depth", "price_stream_no_book", "native_l2"}:
        if gates["replayable_payload"]["status"] == "missing":
            gates["replayable_payload"] = _missing(
                "Live adapter payload capture has not been recorded for this feed."
            )
    if source_type in {"blocksize_state_reference", "nav_reference"}:
        gates["liquidity_depth_volume"] = _blocked(
            "Reference/NAV/state rows are not executable liquidity and cannot supply live depth.",
            {"source_type": source_type},
        )
        gates["manipulation_concentration"] = _missing(
            "Reference row still needs upstream methodology, stale-value, and issuer/source concentration review."
        )
    if gates["issuer_nav_alignment"]["status"] == "missing":
        gates["issuer_nav_alignment"] = _missing(
            "No issuer NAV, administrator reference, reserve, or attestation alignment is recorded."
        )
    if gates["freshness_cadence"]["status"] == "missing":
        gates["freshness_cadence"] = _missing(
            "No continuous 30-minute freshness and tick-frequency benchmark is recorded."
        )
    if gates["liquidity_depth_volume"]["status"] == "missing":
        gates["liquidity_depth_volume"] = _missing(
            "No live depth, fillability, spread, or 24h organic volume validation is recorded."
        )
    if gates["manipulation_concentration"]["status"] == "missing":
        gates["manipulation_concentration"] = _missing(
            "No manipulation, concentration, wash-volume, or outlier-sensitivity check is recorded."
        )


def _apply_rights_gate(
    feed: dict[str, Any],
    gates: dict[str, dict[str, Any]],
    evidence: dict[str, Any],
) -> None:
    clearance = evidence.get("rights_clearance") if isinstance(evidence.get("rights_clearance"), dict) else {}
    venue = str(feed.get("venue") or "")
    if rights_cleared_for_venue(venue, clearance=clearance):
        gates["rights_and_redistribution"] = _passed(
            "Production redistribution/legal rights clearance is recorded for this venue.",
            {
                "venue": venue,
                "clearance_id": clearance.get("clearance_id"),
                "cleared_at": clearance.get("cleared_at"),
                "artifact": evidence.get("rights_clearance_summary", {}).get("path"),
            },
        )
    else:
        gates["rights_and_redistribution"] = _missing(
            "Production redistribution/legal rights signoff is not recorded for this source.",
            {"venue": venue},
        )


def _classify(feed: dict[str, Any], gates: dict[str, dict[str, Any]]) -> dict[str, Any]:
    required = [gate_id for gate_id, gate in gates.items() if gate.get("required")]
    missing = [gate_id for gate_id in required if gates[gate_id]["status"] in {"missing", "blocked"}]
    evidence_only = [gate_id for gate_id in required if gates[gate_id]["status"] == "evidence_found"]
    passed = [gate_id for gate_id in required if gates[gate_id]["status"] == PROMOTED_GATE_STATUS]
    production_promoted = len(required) > 0 and len(passed) == len(required)

    if production_promoted:
        status = "production_promoted"
        action = "Eligible for production use subject to final operator approval."
    elif str(feed.get("source_type")) == "blocksize_state_reference":
        status = "production_blocked_state_reference_only"
        action = "Run state_instruments and state_pool discovery, then issuer/NAV and freshness checks."
    elif str(feed.get("source_type")) == "nav_reference":
        status = "production_blocked_nav_reference_only"
        action = "Attach issuer/admin NAV evidence and keep as reference until market-liquidity evidence exists."
    elif missing:
        status = "production_blocked_missing_discovery"
        action = f"Resolve required gates: {', '.join(missing[:6])}"
    elif evidence_only:
        status = "evidence_found_not_promoted"
        action = f"Upgrade evidence-only gates to passed: {', '.join(evidence_only[:6])}"
    else:
        status = "ready_for_live_probe_not_promoted"
        action = "Run live 30-minute probe, benchmark, and promotion check before production."

    evidence_sources = sorted(
        gate_id
        for gate_id, gate in gates.items()
        if (
            gate.get("required")
            and gate.get("status") in {"passed", "evidence_found"}
            and gate_id != "canonical_identity"
        )
    )
    return {
        "promotion_status": status,
        "production_promoted": production_promoted,
        "required_gate_count": len(required),
        "passed_gate_count": len(passed),
        "evidence_only_gate_count": len(evidence_only),
        "missing_or_blocked_gate_count": len(missing),
        "required_gates": required,
        "passed_gates": passed,
        "evidence_only_gates": evidence_only,
        "missing_or_blocked_gates": missing,
        "evidence_sources": evidence_sources,
        "next_action": action,
    }


def _audit_feed(feed: dict[str, Any], evidence: dict[str, Any]) -> dict[str, Any]:
    gates = _base_gates(feed)
    _apply_identity_gates(feed, gates)
    _apply_jupiter_gates(feed, gates, evidence)
    _apply_replay_inventory_gates(feed, gates, evidence)
    _apply_state_gates(feed, gates, evidence)
    _apply_hyperliquid_gates(feed, gates, evidence)
    _apply_default_quality_gates(feed, gates)
    _apply_rights_gate(feed, gates, evidence)
    classification = _classify(feed, gates)
    return {
        "feed_id": feed["feed_id"],
        "kind": feed["kind"],
        "asset_id": feed["asset_id"],
        "asset_classes": feed["asset_classes"],
        "symbol": feed["symbol"],
        "venue": feed["venue"],
        "source_type": feed["source_type"],
        "coverage_status": feed["coverage_status"],
        "support": feed["support"],
        "blocksize_benchmark": feed.get("blocksize_benchmark"),
        "metadata": feed.get("metadata", {}),
        **classification,
        "gates": gates,
    }


def _filter_rows(
    rows: list[dict[str, Any]],
    *,
    venue: str,
    asset_class: str,
    status: str,
) -> list[dict[str, Any]]:
    venue_filter = venue.strip().lower()
    class_filter = asset_class.strip().lower()
    status_filter = status.strip().lower()
    filtered: list[dict[str, Any]] = []
    for row in rows:
        if venue_filter != "all" and str(row["venue"]).lower() != venue_filter:
            continue
        if class_filter != "all" and class_filter not in {str(cls).lower() for cls in row["asset_classes"]}:
            continue
        if status_filter != "all" and str(row["promotion_status"]).lower() != status_filter:
            continue
        filtered.append(row)
    return filtered


def build_feed_discovery_audit(
    *,
    exclude_tokenized_stocks: bool = False,
    venue: str = "all",
    asset_class: str = "all",
    status: str = "all",
    include_feed_details: bool = True,
    reports_dir: str | Path = DEFAULT_REPORTS_DIR,
) -> dict[str, Any]:
    """Return the discovery and promotion gate state for every sourced feed."""
    reports_path = Path(reports_dir)
    catalog = build_non_crypto_feed_catalog(
        exclude_tokenized_stocks=exclude_tokenized_stocks,
        asset_class=None if asset_class == "all" else asset_class,
        venue=None if venue == "all" else venue,
    )
    evidence = _load_discovery_evidence(reports_path)
    feeds = [*catalog["vwap_feeds"], *catalog["bidask_feeds"]]
    audited = [_audit_feed(feed, evidence) for feed in feeds]
    filtered = _filter_rows(audited, venue=venue, asset_class=asset_class, status=status)

    by_status = Counter(row["promotion_status"] for row in filtered)
    by_venue = Counter(row["venue"] for row in filtered)
    by_source_type = Counter(row["source_type"] for row in filtered)
    by_asset_class = Counter(cls for row in filtered for cls in row["asset_classes"])
    by_required_gate = Counter(
        gate_id
        for row in filtered
        for gate_id in row["required_gates"]
    )
    by_missing_gate = Counter(
        gate_id
        for row in filtered
        for gate_id in row["missing_or_blocked_gates"]
    )
    by_evidence_gate = Counter(
        gate_id
        for row in filtered
        for gate_id in row["evidence_only_gates"]
    )
    production_promoted = sum(1 for row in filtered if row["production_promoted"])
    evidence_found = sum(1 for row in filtered if row["evidence_sources"])

    result: dict[str, Any] = {
        "summary": {
            "feed_count": len(filtered),
            "unfiltered_feed_count": len(audited),
            "production_promoted": production_promoted,
            "blocked_from_production": len(filtered) - production_promoted,
            "evidence_found_not_promoted": sum(
                1 for row in filtered if row["promotion_status"] == "evidence_found_not_promoted"
            ),
            "feeds_with_any_discovery_evidence": evidence_found,
            "feeds_missing_discovery_or_blocked": sum(
                1 for row in filtered if row["missing_or_blocked_gate_count"] > 0
            ),
            "candidate_or_supplemental_count": sum(
                1
                for row in filtered
                if row["promotion_status"] != "production_promoted"
            ),
            "by_status": dict(sorted(by_status.items())),
            "by_venue": dict(sorted(by_venue.items())),
            "by_source_type": dict(sorted(by_source_type.items())),
            "by_asset_class": dict(sorted(by_asset_class.items())),
            "by_required_gate": dict(sorted(by_required_gate.items())),
            "by_missing_gate": dict(sorted(by_missing_gate.items())),
            "by_evidence_gate": dict(sorted(by_evidence_gate.items())),
        },
        "filters": {
            "exclude_tokenized_stocks": exclude_tokenized_stocks,
            "venue": venue,
            "asset_class": asset_class,
            "status": status,
            "include_feed_details": include_feed_details,
            "reports_dir": str(reports_path),
        },
        "policy": {
            "default": "candidate_or_supplemental_until_all_required_gates_pass",
            "promotion_rule": "Only rows with every required gate marked passed may be promoted to production live liquidity.",
            "evidence_rule": "Point-in-time token, route, state, or order-book evidence is not enough for production promotion.",
            "state_rule": "Blocksize state rows are reference/supplemental until state_instruments, state_pool freshness, issuer/NAV alignment, and benchmark checks pass.",
        },
        "rights_clearance": evidence["rights_clearance_summary"],
        "gate_definitions": GATE_DEFINITIONS,
    }
    if include_feed_details:
        result["feeds"] = sorted(
            filtered,
            key=lambda row: (str(row["venue"]), str(row["asset_id"]), str(row["kind"]), str(row["feed_id"])),
        )
    return result


def write_feed_discovery_reports(
    *,
    json_path: str | Path = DEFAULT_DISCOVERY_JSON_PATH,
    csv_path: str | Path = DEFAULT_DISCOVERY_CSV_PATH,
    exclude_tokenized_stocks: bool = False,
    reports_dir: str | Path = DEFAULT_REPORTS_DIR,
) -> dict[str, Any]:
    """Write JSON and CSV discovery promotion audit reports."""
    report = build_feed_discovery_audit(
        exclude_tokenized_stocks=exclude_tokenized_stocks,
        include_feed_details=True,
        reports_dir=reports_dir,
    )
    json_out = Path(json_path)
    csv_out = Path(csv_path)
    json_out.parent.mkdir(parents=True, exist_ok=True)
    csv_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    fieldnames = [
        "feed_id",
        "kind",
        "asset_id",
        "asset_classes",
        "symbol",
        "venue",
        "source_type",
        "coverage_status",
        "promotion_status",
        "production_promoted",
        "required_gate_count",
        "passed_gate_count",
        "evidence_only_gate_count",
        "missing_or_blocked_gate_count",
        "required_gates",
        "passed_gates",
        "evidence_only_gates",
        "missing_or_blocked_gates",
        "evidence_sources",
        "next_action",
    ]
    with csv_out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in report["feeds"]:
            writer.writerow(
                {
                    key: json.dumps(row[key]) if isinstance(row.get(key), (list, dict)) else row.get(key)
                    for key in fieldnames
                }
            )
    return report
