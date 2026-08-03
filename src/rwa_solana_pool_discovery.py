"""Derive Solana pool allowlist evidence from Jupiter route plans and RPC state."""

from __future__ import annotations

import base64
import csv
import hashlib
import json
import os
import urllib.error
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.rwa_feed_discovery import DEFAULT_REPORTS_DIR
from src.runtime_data import resolve_required_rwa_report_path


DEFAULT_SOLANA_POOL_ALLOWLIST_JSON_PATH = resolve_required_rwa_report_path(
    "rwa_solana_pool_allowlist.json"
)
DEFAULT_SOLANA_POOL_ALLOWLIST_CSV_PATH = DEFAULT_REPORTS_DIR / "rwa_solana_pool_allowlist.csv"
DEFAULT_JUPITER_ROUTE_ALLOWLIST_PATH = resolve_required_rwa_report_path(
    "rwa_jupiter_route_allowlist.json"
)

ROUTE_LABEL_TO_VENUE = {
    "Meteora DLMM": "meteora_dlmm",
    "Raydium CLMM": "raydium_clmm",
    "Whirlpool": "orca_whirlpool",
}


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _base_quote(symbol: str) -> tuple[str, str]:
    if "/" in symbol:
        base, quote = symbol.split("/", 1)
    else:
        base, quote = symbol, "USD"
    return base.upper(), quote.upper()


def _pool_field_for_venue(venue: str) -> str:
    if venue == "orca_whirlpool":
        return "whirlpool_address"
    return "pool_id"


def _state_field_for_venue(venue: str) -> str:
    if venue == "meteora_dlmm":
        return "active_bin"
    if venue == "orca_whirlpool":
        return "tick_arrays"
    return "tick_or_curve_state"


def _iter_route_steps(route: dict[str, Any]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for side in ("ask", "bid"):
        for step in route.get(f"{side}_route_steps") or []:
            if isinstance(step, dict):
                steps.append(
                    {
                        "side": side,
                        "amm_key": step.get("amm_key"),
                        "label": step.get("label"),
                        "input_mint": step.get("input_mint"),
                        "output_mint": step.get("output_mint"),
                        "context_slot": route.get(f"{side}_context_slot"),
                        "bps": step.get("bps"),
                        "percent": step.get("percent"),
                    }
                )
    for quote in route.get("sweep_quotes") or []:
        if not isinstance(quote, dict):
            continue
        for item in quote.get("route_plan") or []:
            if not isinstance(item, dict):
                continue
            swap_info = item.get("swapInfo") if isinstance(item.get("swapInfo"), dict) else {}
            steps.append(
                {
                    "side": "sweep_buy",
                    "amm_key": swap_info.get("ammKey"),
                    "label": swap_info.get("label"),
                    "input_mint": swap_info.get("inputMint"),
                    "output_mint": swap_info.get("outputMint"),
                    "context_slot": quote.get("context_slot"),
                    "bps": item.get("bps"),
                    "percent": item.get("percent"),
                    "notional_usd": quote.get("notional_usd"),
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


def _rpc_call(rpc_url: str, method: str, params: list[Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        rpc_url,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if payload.get("error"):
        raise RuntimeError(json.dumps(payload["error"], sort_keys=True))
    result = payload.get("result")
    return result if isinstance(result, dict) else {}


def _fetch_account_state(rpc_url: str, pubkeys: list[str]) -> dict[str, dict[str, Any]]:
    if not rpc_url or not pubkeys:
        return {}
    result = _rpc_call(
        rpc_url,
        "getMultipleAccounts",
        [pubkeys, {"encoding": "base64", "commitment": "confirmed"}],
    )
    slot = result.get("context", {}).get("slot")
    values = result.get("value") if isinstance(result.get("value"), list) else []
    account_state: dict[str, dict[str, Any]] = {}
    for pubkey, account in zip(pubkeys, values):
        if not isinstance(account, dict):
            account_state[pubkey] = {"status": "missing_account", "slot": slot}
            continue
        encoded = None
        data = account.get("data")
        if isinstance(data, list) and data:
            encoded = data[0]
        raw = base64.b64decode(encoded) if isinstance(encoded, str) else b""
        account_state[pubkey] = {
            "status": "account_state_captured",
            "slot": slot,
            "owner": account.get("owner"),
            "lamports": account.get("lamports"),
            "executable": account.get("executable"),
            "rent_epoch": account.get("rentEpoch"),
            "data_length": len(raw),
            "data_sha256": hashlib.sha256(raw).hexdigest() if raw else None,
            "encoding": "base64_hash_only",
        }
    return account_state


def _candidate_rows(route_payload: dict[str, Any]) -> list[dict[str, Any]]:
    rows_by_key: dict[tuple[str, str], dict[str, Any]] = {}
    routes = route_payload.get("routes") if isinstance(route_payload.get("routes"), list) else []
    for route in routes:
        if not isinstance(route, dict) or route.get("status") != "route_discovered":
            continue
        base, quote = _base_quote(str(route.get("symbol") or ""))
        for step in _iter_route_steps(route):
            label = str(step.get("label") or "")
            venue = ROUTE_LABEL_TO_VENUE.get(label)
            amm_key = step.get("amm_key")
            if not venue or not amm_key:
                continue
            quote_asset = quote
            allowlist_id = f"dex:{venue}:{base}:{quote_asset}"
            key = (allowlist_id, str(amm_key))
            row = rows_by_key.setdefault(
                key,
                {
                    "allowlist_id": allowlist_id,
                    "venue": venue,
                    "symbol": f"{base}/{quote_asset}",
                    "asset_id": base.replace("X", ""),
                    "quote_asset": quote_asset,
                    "chain": "solana",
                    "chain_id": "solana-mainnet",
                    "pool_id": amm_key,
                    "whirlpool_address": amm_key if venue == "orca_whirlpool" else None,
                    "base_mint": (route.get("base_token") or {}).get("mint"),
                    "quote_mint": (route.get("quote_token") or {}).get("mint"),
                    "fee_tier": None,
                    "fee_tier_status": "not_exposed_by_jupiter_route_plan",
                    "tick_or_curve_state": None,
                    "tick_arrays": None,
                    "active_bin": None,
                    "slot": None,
                    "context_slots": [],
                    "route_labels": [],
                    "route_plan_sources": [],
                    "raw_payload_artifact": str(DEFAULT_JUPITER_ROUTE_ALLOWLIST_PATH),
                    "source": "derived_from_jupiter_route_plan",
                    "review_status": "pool_id_and_slot_ready_pending_fee_and_tick_state_decode",
                },
            )
            row["context_slots"] = _unique([*row["context_slots"], step.get("context_slot")])
            row["slot"] = max(row["context_slots"]) if row["context_slots"] else None
            row["route_labels"] = _unique([*row["route_labels"], label])
            row["route_plan_sources"] = _unique([*row["route_plan_sources"], route.get("allowlist_id")])
    return list(rows_by_key.values())


def build_solana_pool_allowlist(
    *,
    route_path: str | Path = DEFAULT_JUPITER_ROUTE_ALLOWLIST_PATH,
    rpc_url: str | None = None,
) -> dict[str, Any]:
    """Build a Solana pool allowlist from Jupiter route labels and optional RPC state."""
    route_payload = _read_json(Path(route_path))
    rows = _candidate_rows(route_payload)
    effective_rpc_url = rpc_url if rpc_url is not None else os.getenv("SOLANA_RPC_URL", "")
    state_by_pool: dict[str, dict[str, Any]] = {}
    rpc_error = None
    if effective_rpc_url:
        try:
            state_by_pool = _fetch_account_state(effective_rpc_url, [str(row["pool_id"]) for row in rows])
        except (OSError, urllib.error.URLError, RuntimeError, ValueError) as exc:
            rpc_error = f"{type(exc).__name__}: {exc}"

    for row in rows:
        state = state_by_pool.get(str(row["pool_id"]), {})
        if state:
            row["pool_account_state"] = state
            row["slot"] = state.get("slot") or row.get("slot")
            state_field = _state_field_for_venue(str(row["venue"]))
            row[state_field] = {
                "state_kind": "raw_pool_account_hash",
                "slot": state.get("slot"),
                "owner": state.get("owner"),
                "data_sha256": state.get("data_sha256"),
                "data_length": state.get("data_length"),
                "decode_status": "raw_account_captured_pending_pool_decoder",
            }
            row["review_status"] = "pool_account_state_ready_pending_fee_and_tick_state_decode"
        else:
            row["pool_account_state"] = {"status": "not_fetched"}

    by_venue = Counter(str(row["venue"]) for row in rows)
    by_status = Counter(str(row["review_status"]) for row in rows)
    return {
        "product": "rwa_solana_pool_allowlist",
        "generated_at": _utc_now_iso(),
        "summary": {
            "pool_count": len(rows),
            "rpc_state_captured": sum(
                1
                for row in rows
                if (row.get("pool_account_state") or {}).get("status") == "account_state_captured"
            ),
            "missing_rpc_state": sum(
                1
                for row in rows
                if (row.get("pool_account_state") or {}).get("status") != "account_state_captured"
            ),
            "fee_tiers_missing": sum(1 for row in rows if not row.get("fee_tier")),
            "by_venue": dict(sorted(by_venue.items())),
            "by_status": dict(sorted(by_status.items())),
        },
        "inputs": {
            "route_path": str(route_path),
            "solana_rpc_configured": bool(effective_rpc_url),
            "rpc_error": rpc_error,
        },
        "policy": {
            "promotion_rule": "Derived pool IDs and raw account hashes are replay evidence, but production still requires fee/tick/bin decoding plus live quality windows.",
            "raw_payload_policy": "Account data is represented by hashes and metadata only; raw account bytes are not emitted in reports.",
        },
        "pools": sorted(rows, key=lambda row: (str(row["venue"]), str(row["asset_id"]), str(row["pool_id"]))),
    }


def write_solana_pool_allowlist_reports(
    *,
    json_path: str | Path = DEFAULT_SOLANA_POOL_ALLOWLIST_JSON_PATH,
    csv_path: str | Path = DEFAULT_SOLANA_POOL_ALLOWLIST_CSV_PATH,
    route_path: str | Path = DEFAULT_JUPITER_ROUTE_ALLOWLIST_PATH,
    rpc_url: str | None = None,
) -> dict[str, Any]:
    """Write Solana pool allowlist evidence reports."""
    report = build_solana_pool_allowlist(route_path=route_path, rpc_url=rpc_url)
    json_out = Path(json_path)
    csv_out = Path(csv_path)
    json_out.parent.mkdir(parents=True, exist_ok=True)
    csv_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    fieldnames = [
        "allowlist_id",
        "venue",
        "symbol",
        "pool_id",
        "whirlpool_address",
        "base_mint",
        "quote_mint",
        "fee_tier",
        "fee_tier_status",
        "slot",
        "context_slots",
        "review_status",
        "pool_account_state",
        "route_plan_sources",
    ]
    with csv_out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in report["pools"]:
            writer.writerow(
                {
                    key: json.dumps(row[key], sort_keys=True) if isinstance(row.get(key), (list, dict)) else row.get(key)
                    for key in fieldnames
                }
            )
    return report
