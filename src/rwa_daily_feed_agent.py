"""Daily RWA feed discovery agent for newly listed tokenized products."""

from __future__ import annotations

import csv
import json
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
    write_rwa_xyz_monitor_reports,
)


DEFAULT_DAILY_AGENT_JSON_PATH = Path("reports/rwa_daily_feed_agent.json")
DEFAULT_DAILY_AGENT_CSV_PATH = Path("reports/rwa_daily_new_tokens.csv")
DEFAULT_DAILY_AGENT_HISTORY_DIR = Path("reports/rwa_daily_feed_agent_history")

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


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc_now_iso() -> str:
    return _utc_now().isoformat()


def _token_key(row: dict[str, Any]) -> str:
    network = str(row.get("network_slug") or row.get("network") or "unknown").strip().lower()
    address = str(row.get("address") or "").strip().lower()
    if network and address:
        return f"{network}:{address}"
    return str(row.get("token_row_id") or row.get("rwa_xyz_token_id") or "")


def _asset_key(row: dict[str, Any]) -> str:
    return str(row.get("rwa_xyz_asset_id") or row.get("asset_id") or row.get("symbol") or "")


def _by_key(rows: list[dict[str, Any]], key_fn) -> dict[str, dict[str, Any]]:
    keyed: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = key_fn(row)
        if key:
            keyed[key] = row
    return keyed


def _priority_for_token(row: dict[str, Any]) -> str:
    asset_class = str(row.get("asset_class") or "").lower()
    network = str(row.get("network_slug") or "").lower()
    if asset_class in P0_ASSET_CLASSES and network in SOLANA_NETWORKS | EVM_NETWORKS:
        return "P0"
    if asset_class in P0_ASSET_CLASSES:
        return "P1"
    return "P2"


def _discovery_lane(row: dict[str, Any]) -> str:
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


def _token_action(row: dict[str, Any]) -> dict[str, Any]:
    lane = _discovery_lane(row)
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


def _counter(rows: list[dict[str, Any]], field: str) -> dict[str, int]:
    return dict(sorted(Counter(str(row.get(field) or "unknown") for row in rows).items()))


def build_daily_feed_agent_report(
    *,
    previous_report: dict[str, Any],
    current_report: dict[str, Any],
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Compare the current RWA.xyz monitor with the previous saved report."""
    previous_assets = previous_report.get("asset_rows") if isinstance(previous_report.get("asset_rows"), list) else []
    current_assets = current_report.get("asset_rows") if isinstance(current_report.get("asset_rows"), list) else []
    previous_tokens = previous_report.get("token_rows") if isinstance(previous_report.get("token_rows"), list) else []
    current_tokens = current_report.get("token_rows") if isinstance(current_report.get("token_rows"), list) else []

    previous_asset_map = _by_key(previous_assets, _asset_key)
    current_asset_map = _by_key(current_assets, _asset_key)
    previous_token_map = _by_key(previous_tokens, _token_key)
    current_token_map = _by_key(current_tokens, _token_key)

    baseline_created = not previous_asset_map and not previous_token_map
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

    alert_level = (
        "baseline_created"
        if baseline_created
        else "new_p0_tokens"
        if p0_actions
        else "new_tokens"
        if new_tokens
        else "no_new_feeds"
    )

    return {
        "product": "rwa_daily_feed_discovery_agent",
        "generated_at": generated_at or _utc_now_iso(),
        "source": {
            "venue": RWA_XYZ_VENUE_ID,
            "current_report": str(DEFAULT_RWA_XYZ_REPORT_JSON_PATH),
            "method": "daily diff of normalized RWA.xyz New Asset Monitor assets and token contracts",
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
            "If new_token_count is zero, no sourcing expansion is needed beyond the normal scheduled quality probes.",
            "For P0 new tokens, immediately run token/pool/route discovery by lane and attach replayable evidence.",
            "Refresh /v1/rwa/sourcing/jobs after every daily run so the new token contracts are visible in the backlog.",
            "Do not promote any new catalog row into VWAP, bid/ask, or consensus until the promotion_required_gates pass.",
        ],
    }


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
    """Refresh RWA.xyz, diff against previous report, and write daily outputs."""
    previous_report = load_rwa_xyz_monitor_report(refresh_json_path)
    if input_path:
        payload, metadata = load_payload_from_file(input_path)
    else:
        payload, metadata = fetch_rwa_xyz_monitor_payload(timeout=timeout)
    current_report = write_rwa_xyz_monitor_reports(
        json_path=refresh_json_path,
        asset_csv_path=refresh_asset_csv_path,
        token_csv_path=refresh_token_csv_path,
        payload=payload,
        fetch_metadata=metadata,
    )
    report = build_daily_feed_agent_report(
        previous_report=previous_report,
        current_report=current_report,
    )

    json_out = Path(json_path)
    csv_out = Path(csv_path)
    history_out = Path(history_dir) / f"{_utc_now().date().isoformat()}.json"
    for path in (json_out, csv_out, history_out):
        path.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    history_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _write_new_token_csv(csv_out, report["new_tokens"], report["sourcing_actions"])
    return report


def load_daily_feed_agent_report(
    path: str | Path = DEFAULT_DAILY_AGENT_JSON_PATH,
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
                "new_asset_count": 0,
                "new_token_count": 0,
                "new_p0_token_count": 0,
            },
            "new_assets": [],
            "new_tokens": [],
            "sourcing_actions": [],
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
        }
    return payload if isinstance(payload, dict) else {}


def build_daily_feed_agent_view(
    *,
    include_rows: bool = False,
    row_limit: int = 100,
    path: str | Path = DEFAULT_DAILY_AGENT_JSON_PATH,
) -> dict[str, Any]:
    report = load_daily_feed_agent_report(path)
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
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for token in token_rows:
            action = actions_by_key.get(f"{token.get('network_slug')}:{str(token.get('address') or '').lower()}", {})
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
