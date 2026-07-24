"""Route, pool, state, and replay evidence inventory for RWA DEX promotion."""

from __future__ import annotations

import csv
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any

from src.rwa_dex_allowlist import build_dex_allowlist
from src.rwa_feed_discovery import DEFAULT_REPORTS_DIR
from src.rwa_rights_clearance import (
    load_rights_clearance,
    rights_clearance_summary,
    rights_cleared_for_venue,
)


DEFAULT_REPLAY_INVENTORY_JSON_PATH = DEFAULT_REPORTS_DIR / "rwa_route_pool_replay_inventory.json"
DEFAULT_REPLAY_INVENTORY_CSV_PATH = DEFAULT_REPORTS_DIR / "rwa_route_pool_replay_inventory.csv"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _artifact_path(env_name: str, default: str) -> Path:
    return Path(os.getenv(env_name, default)).expanduser()


def _base_quote(symbol: str) -> tuple[str, str]:
    if "/" in symbol:
        base, quote = symbol.split("/", 1)
    else:
        base, quote = symbol, "USD"
    return base.upper(), quote.upper()


def _load_jupiter_routes(reports_dir: Path) -> dict[str, dict[str, Any]]:
    payload = _read_json(reports_dir / "rwa_jupiter_route_allowlist.json")
    rows = payload.get("routes") if isinstance(payload.get("routes"), list) else []
    return {
        str(row.get("allowlist_id")): row
        for row in rows
        if isinstance(row, dict) and row.get("allowlist_id")
    }


def _load_token_registry(reports_dir: Path) -> dict[str, dict[str, Any]]:
    payload = _read_json(reports_dir / "rwa_solana_token_mints.json")
    rows = payload.get("tokens") if isinstance(payload.get("tokens"), list) else []
    registry: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        for key in (row.get("token_key"), row.get("query_symbol"), row.get("symbol")):
            if key:
                registry[str(key).upper().replace("/", "").replace("-", "")] = row
    return registry


def _load_pool_allowlist(path: Path) -> dict[str, dict[str, Any]]:
    payload = _read_json(path)
    rows = (
        payload.get("pools")
        or payload.get("rows")
        or payload.get("candidates")
        or payload.get("allowlist")
        or []
    )
    if not isinstance(rows, list):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        allowlist_id = row.get("allowlist_id")
        if allowlist_id:
            result[str(allowlist_id)] = row
        venue = row.get("venue")
        symbol = row.get("symbol")
        if venue and symbol:
            base, quote = _base_quote(str(symbol))
            result[f"dex:{venue}:{base}:{quote}"] = row
    return result


def _load_state_discovery(reports_dir: Path) -> dict[str, dict[str, Any]]:
    payload = _read_json(reports_dir / "rwa_blocksize_state_discovery.json")
    rows = payload.get("symbols") if isinstance(payload.get("symbols"), list) else []
    return {
        str(row.get("state_symbol") or row.get("symbol") or "").upper().replace("/", ""): row
        for row in rows
        if isinstance(row, dict)
    }


def _route_steps(route_row: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for side in ("ask", "bid"):
        for step in route_row.get(f"{side}_route_steps") or []:
            if not isinstance(step, dict):
                continue
            steps.append(
                {
                    "side": side,
                    "amm_key": step.get("amm_key"),
                    "label": step.get("label"),
                    "input_mint": step.get("input_mint"),
                    "output_mint": step.get("output_mint"),
                    "bps": step.get("bps"),
                    "percent": step.get("percent"),
                }
            )
    for quote in route_row.get("sweep_quotes") or []:
        if not isinstance(quote, dict):
            continue
        notional = quote.get("notional_usd")
        for item in quote.get("route_plan") or []:
            if not isinstance(item, dict):
                continue
            swap_info = item.get("swapInfo") if isinstance(item.get("swapInfo"), dict) else {}
            steps.append(
                {
                    "side": "sweep_buy",
                    "notional_usd": notional,
                    "amm_key": swap_info.get("ammKey"),
                    "label": swap_info.get("label"),
                    "input_mint": swap_info.get("inputMint"),
                    "output_mint": swap_info.get("outputMint"),
                    "in_amount": swap_info.get("inAmount"),
                    "out_amount": swap_info.get("outAmount"),
                    "bps": item.get("bps"),
                    "percent": item.get("percent"),
                    "context_slot": quote.get("context_slot"),
                }
            )
    return steps


def _unique(values: list[Any]) -> list[Any]:
    seen: set[str] = set()
    result: list[Any] = []
    for value in values:
        if value in {None, ""}:
            continue
        key = str(value)
        if key in seen:
            continue
        seen.add(key)
        result.append(value)
    return result


def _filter_rights_blocker(blockers: list[Any], *, rights_cleared: bool) -> list[Any]:
    if not rights_cleared:
        return blockers
    return [blocker for blocker in blockers if blocker != "rights_and_redistribution_clearance"]


def _jupiter_replay_row(candidate: dict[str, Any], route_row: dict[str, Any]) -> dict[str, Any]:
    steps = _route_steps(route_row)
    context_slots = _unique(
        [
            route_row.get("ask_context_slot"),
            route_row.get("bid_context_slot"),
            *[quote.get("context_slot") for quote in route_row.get("sweep_quotes") or [] if isinstance(quote, dict)],
        ]
    )
    pool_or_route_ids = _unique([step.get("amm_key") for step in steps])
    has_route = str(route_row.get("status")) == "route_discovered"
    return {
        "replay_status": "route_replay_ready_pending_liquidity_window" if has_route else "route_discovery_failed",
        "identifier_status": "route_plan_mapped" if has_route else "missing_route_plan",
        "base_identifier": (route_row.get("base_token") or {}).get("mint"),
        "quote_identifier": (route_row.get("quote_token") or {}).get("mint"),
        "route_plan_available": has_route,
        "pool_state_available": False,
        "pool_or_route_ids": pool_or_route_ids,
        "fee_tiers": [],
        "fee_tier_status": "router_route_fee_tiers_not_exposed",
        "slot_or_block_numbers": context_slots,
        "slot_or_block_status": "context_slot_recorded" if context_slots else "missing_context_slot",
        "route_steps": steps,
        "sweep_quotes": route_row.get("sweep_quotes") or [],
        "raw_payload_artifact": "reports/rwa_jupiter_route_allowlist.json",
        "raw_payload_available": has_route,
        "replay_payload_fields": [
            "base_token",
            "quote_token",
            "ask_route_steps",
            "bid_route_steps",
            "sweep_quotes",
            "context_slot",
            "price_impact_pct",
        ],
        "missing_replay_fields": [] if has_route else ["route_plan", "context_slot", "quote_payload"],
        "promotion_blockers": [
            "continuous_30_minute_freshness_window",
            "block_size_fillability_and_organic_volume",
            "manipulation_and_route_diversity_checks",
            "rights_and_redistribution_clearance",
            "benchmark_alignment",
        ],
    }


def _pool_replay_row(candidate: dict[str, Any], pool_row: dict[str, Any] | None) -> dict[str, Any]:
    if not pool_row:
        return {
            "replay_status": "missing_pool_allowlist",
            "identifier_status": "missing_pool_or_route_identifier",
            "base_identifier": None,
            "quote_identifier": None,
            "route_plan_available": False,
            "pool_state_available": False,
            "pool_or_route_ids": [],
            "fee_tiers": [],
            "fee_tier_status": "missing_fee_tier_or_pool_curve_parameters",
            "slot_or_block_numbers": [],
            "slot_or_block_status": "missing_slot_or_block_state",
            "route_steps": [],
            "sweep_quotes": [],
            "raw_payload_artifact": None,
            "raw_payload_available": False,
            "replay_payload_fields": [],
            "missing_replay_fields": candidate.get("required_identifiers") or [],
            "promotion_blockers": candidate.get("blockers") or [],
        }

    pool_id = (
        pool_row.get("pool_id")
        or pool_row.get("pool_address")
        or pool_row.get("whirlpool_address")
        or pool_row.get("amm_key")
    )
    fee_tier = pool_row.get("fee_tier") or pool_row.get("fee") or pool_row.get("swap_fee")
    block_number = pool_row.get("block_number") or pool_row.get("slot") or pool_row.get("context_slot")
    base_identifier = pool_row.get("base_token") or pool_row.get("base_mint") or pool_row.get("token0")
    quote_identifier = pool_row.get("quote_token") or pool_row.get("quote_mint") or pool_row.get("token1")
    required = set(str(item) for item in candidate.get("required_identifiers") or [])
    present = {
        "chain_id": pool_row.get("chain_id") or pool_row.get("chain"),
        "pool_id": pool_id,
        "pool_address": pool_row.get("pool_address"),
        "whirlpool_address": pool_row.get("whirlpool_address"),
        "base_token": base_identifier,
        "base_mint": base_identifier,
        "quote_token": quote_identifier,
        "quote_mint": quote_identifier,
        "fee_tier": fee_tier,
        "tick_state": pool_row.get("tick_state") or pool_row.get("tick_arrays") or pool_row.get("active_bin"),
        "tick_or_curve_state": pool_row.get("tick_or_curve_state") or pool_row.get("tick_state"),
        "tick_arrays": pool_row.get("tick_arrays"),
        "active_bin": pool_row.get("active_bin"),
        "block_number": block_number,
        "slot": block_number,
        "balances": pool_row.get("balances"),
        "weights_or_amplification": pool_row.get("weights") or pool_row.get("amplification"),
    }
    missing = sorted(identifier for identifier in required if not present.get(identifier))
    return {
        "replay_status": "pool_replay_ready_pending_live_quality" if not missing else "pool_replay_incomplete",
        "identifier_status": "pool_identifier_mapped" if pool_id else "missing_pool_id",
        "base_identifier": base_identifier,
        "quote_identifier": quote_identifier,
        "route_plan_available": False,
        "pool_state_available": not missing,
        "pool_or_route_ids": _unique([pool_id]),
        "fee_tiers": _unique([fee_tier]),
        "fee_tier_status": "fee_or_curve_parameters_recorded" if fee_tier else "missing_fee_tier_or_curve_parameters",
        "slot_or_block_numbers": _unique([block_number]),
        "slot_or_block_status": "slot_or_block_recorded" if block_number else "missing_slot_or_block_state",
        "route_steps": [],
        "sweep_quotes": [],
        "raw_payload_artifact": str(
            pool_row.get("artifact") or pool_row.get("source_artifact") or "configured_pool_allowlist"
        ),
        "raw_payload_available": True,
        "replay_payload_fields": sorted(key for key, value in present.items() if value),
        "missing_replay_fields": missing,
        "promotion_blockers": [
            "continuous_30_minute_freshness_window",
            "block_size_fillability_and_organic_volume",
            "manipulation_and_concentration_checks",
            "rights_and_redistribution_clearance",
            "benchmark_alignment",
        ],
    }


def _token_identifiers(candidate: dict[str, Any], token_registry: dict[str, dict[str, Any]]) -> dict[str, Any]:
    base, quote = _base_quote(str(candidate["symbol"]))
    quote_key = "USDC" if quote == "USD" else quote
    base_row = token_registry.get(base.replace("X", "X"))
    quote_row = token_registry.get(quote_key)
    return {
        "base_identifier": (base_row or {}).get("mint"),
        "quote_identifier": (quote_row or {}).get("mint"),
        "token_identifier_status": (
            "token_mints_mapped"
            if (base_row or {}).get("mint") and (quote_row or {}).get("mint")
            else "missing_token_mint_mapping"
        ),
    }


def build_route_pool_replay_inventory(
    *,
    venue: str = "all",
    status: str = "all",
    reports_dir: str | Path = DEFAULT_REPORTS_DIR,
) -> dict[str, Any]:
    """Return replay evidence for route/pool candidates."""
    reports_path = Path(reports_dir)
    route_rows = _load_jupiter_routes(reports_path)
    token_registry = _load_token_registry(reports_path)
    state_rows = _load_state_discovery(reports_path)
    rights_clearance = load_rights_clearance()
    solana_pool_rows = _load_pool_allowlist(
        _artifact_path("RWA_SOLANA_POOL_ALLOWLIST_PATH", "reports/rwa_solana_pool_allowlist.json")
    )
    evm_pool_rows = _load_pool_allowlist(
        _artifact_path("RWA_EVM_POOL_ALLOWLIST_PATH", "reports/rwa_evm_pool_allowlist.json")
    )

    allowlist = build_dex_allowlist(venue=venue, status="all")
    rows: list[dict[str, Any]] = []
    for candidate in allowlist["candidates"]:
        if venue != "all" and candidate["venue"] != venue:
            continue
        allowlist_id = str(candidate["allowlist_id"])
        source_type = str(candidate["source_type"])
        base_identifiers = _token_identifiers(candidate, token_registry)
        if candidate["venue"] == "jupiter_router":
            replay = _jupiter_replay_row(candidate, route_rows.get(allowlist_id, {}))
            if not replay.get("base_identifier"):
                replay["base_identifier"] = base_identifiers["base_identifier"]
            if not replay.get("quote_identifier"):
                replay["quote_identifier"] = base_identifiers["quote_identifier"]
            replay["token_identifier_status"] = base_identifiers["token_identifier_status"]
        elif candidate["chain"] == "solana":
            replay = _pool_replay_row(candidate, solana_pool_rows.get(allowlist_id))
            replay["token_identifier_status"] = base_identifiers["token_identifier_status"]
            if not replay.get("base_identifier"):
                replay["base_identifier"] = base_identifiers["base_identifier"]
            if not replay.get("quote_identifier"):
                replay["quote_identifier"] = base_identifiers["quote_identifier"]
        else:
            replay = _pool_replay_row(candidate, evm_pool_rows.get(allowlist_id))
            replay["token_identifier_status"] = (
                "missing_evm_contract_mapping"
                if not replay.get("base_identifier") or not replay.get("quote_identifier")
                else "token_contracts_mapped"
            )

        row = {
            "allowlist_id": allowlist_id,
            "venue": candidate["venue"],
            "symbol": candidate["symbol"],
            "asset_id": candidate["asset_id"],
            "asset_class": candidate["asset_class"],
            "chain": candidate["chain"],
            "chain_id": candidate["chain_id"],
            "source_type": source_type,
            "candidate_kind": candidate["candidate_kind"],
            "required_identifiers": candidate["required_identifiers"],
            "required_observation_fields": candidate["required_observation_fields"],
            **replay,
        }
        row["rights_and_redistribution_cleared"] = rights_cleared_for_venue(
            str(row["venue"]),
            clearance=rights_clearance,
        )
        row["promotion_blockers"] = _filter_rights_blocker(
            list(row.get("promotion_blockers") or []),
            rights_cleared=bool(row["rights_and_redistribution_cleared"]),
        )
        if status == "all" or row["replay_status"] == status:
            rows.append(row)

    by_status = Counter(str(row["replay_status"]) for row in rows)
    by_venue = Counter(str(row["venue"]) for row in rows)
    by_source_type = Counter(str(row["source_type"]) for row in rows)
    by_chain = Counter(str(row["chain"]) for row in rows)
    return {
        "summary": {
            "candidate_count": len(rows),
            "replay_ready": sum(
                1
                for row in rows
                if row["replay_status"] in {
                    "route_replay_ready_pending_liquidity_window",
                    "pool_replay_ready_pending_live_quality",
                }
            ),
            "missing_or_incomplete_replay": sum(
                1
                for row in rows
                if row["replay_status"]
                not in {
                    "route_replay_ready_pending_liquidity_window",
                    "pool_replay_ready_pending_live_quality",
                }
            ),
            "route_plan_available": sum(1 for row in rows if row["route_plan_available"]),
            "pool_state_available": sum(1 for row in rows if row["pool_state_available"]),
            "raw_payload_available": sum(1 for row in rows if row["raw_payload_available"]),
            "by_status": dict(sorted(by_status.items())),
            "by_venue": dict(sorted(by_venue.items())),
            "by_source_type": dict(sorted(by_source_type.items())),
            "by_chain": dict(sorted(by_chain.items())),
        },
        "filters": {"venue": venue, "status": status, "reports_dir": str(reports_path)},
        "rights_clearance": rights_clearance_summary(rights_clearance),
        "state_reference_note": {
            "blocksize_state_discovery_rows": len(state_rows),
            "state_rows_are_not_executable_liquidity": True,
        },
        "rows": sorted(rows, key=lambda row: (str(row["venue"]), str(row["asset_id"]), str(row["allowlist_id"]))),
    }


def write_route_pool_replay_inventory_reports(
    *,
    json_path: str | Path = DEFAULT_REPLAY_INVENTORY_JSON_PATH,
    csv_path: str | Path = DEFAULT_REPLAY_INVENTORY_CSV_PATH,
) -> dict[str, Any]:
    """Write route/pool replay inventory reports."""
    inventory = build_route_pool_replay_inventory()
    json_out = Path(json_path)
    csv_out = Path(csv_path)
    json_out.parent.mkdir(parents=True, exist_ok=True)
    csv_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(inventory, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    fieldnames = [
        "allowlist_id",
        "venue",
        "symbol",
        "asset_id",
        "asset_class",
        "chain",
        "source_type",
        "replay_status",
        "identifier_status",
        "token_identifier_status",
        "base_identifier",
        "quote_identifier",
        "pool_or_route_ids",
        "fee_tiers",
        "slot_or_block_numbers",
        "rights_and_redistribution_cleared",
        "raw_payload_available",
        "missing_replay_fields",
        "promotion_blockers",
    ]
    with csv_out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in inventory["rows"]:
            writer.writerow(
                {
                    key: json.dumps(row[key], sort_keys=True) if isinstance(row.get(key), (list, dict)) else row.get(key)
                    for key in fieldnames
                }
            )
    return inventory
