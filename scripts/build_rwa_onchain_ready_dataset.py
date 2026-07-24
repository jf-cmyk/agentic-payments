#!/usr/bin/env python3
"""Build workbook-ready RWA contract, ticker, feed, and liquidity datasets.

The controlling universe is the 3,407-row RWA master.  Candidate source rows
are deduplicated into contributor feeds.  Exact network+contract identity is
preferred; symbol fallback is accepted only when it resolves to one economic
asset id.  Missing measurements receive explicit operational states.
"""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


ROOT = Path("reports")
MASTER = ROOT / "rwa_master_all_token_contracts_sourceability_2026-07-16.csv"
INVENTORY = ROOT / "rwa_onchain_contract_feed_inventory_2026-07-22.csv"
REGISTRY = ROOT / "rwa_feed_source_registry_2026-07-16.csv"
RPC_MATRIX = ROOT / "rwa_network_rpc_access_requirements_2026-07-16.csv"
POOL_REFRESH = ROOT / "rwa_pool_liquidity_refresh_2026-07-22.csv"
OUTPUT = ROOT / "rwa_onchain_ready_workbook_data_2026-07-22.json"


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def clean(value: Any) -> str:
    return "" if value is None else str(value).strip()


def norm_network(value: str) -> str:
    return clean(value).lower().replace(" ", "_").replace("-", "_")


def norm_address(value: str) -> str:
    return clean(value).lower()


def as_float(value: Any) -> float | None:
    try:
        return None if value in {None, ""} else float(value)
    except (TypeError, ValueError):
        return None


def safe_status(value: Any) -> Any:
    if not isinstance(value, str):
        return value
    return value.replace("not_fetched_lane_requires_access_or_adapter", "access_or_adapter_required").replace("not fetched", "access required")


def join(values: set[str] | list[str], limit: int = 100) -> str:
    items = sorted({clean(v) for v in values if clean(v)})
    if len(items) > limit:
        return " | ".join(items[:limit]) + f" | +{len(items) - limit} more"
    return " | ".join(items)


def liquidity_state(current_rows: list[dict[str, Any]], has_candidate: bool) -> dict[str, Any]:
    values = [as_float(r.get("liquidity_value_usd")) for r in current_rows]
    values = [v for v in values if v is not None]
    tested = [as_float(r.get("tested_executable_notional_usd")) for r in current_rows]
    tested = [v for v in tested if v is not None]
    pool_rows = [r for r in current_rows if r.get("liquidity_metric") == "pool_tvl_usd"]
    route_rows = [r for r in current_rows if r.get("price_semantics") == "executable_router_quote_snapshot"]
    rate_rows = [r for r in current_rows if r.get("price_semantics") == "contract_exchange_rate_reference"]
    if route_rows and tested:
        status = "tested_executable_route_notional_and_liquidity_snapshot"
        reason = "At least one exact-input router quote filled at the tested notional; this is point-in-time route evidence, not guaranteed capacity."
    elif route_rows and values:
        status = "observed_router_liquidity_snapshot_test_notional_not_fully_validated"
        reason = "Router/token liquidity metadata was observed, but the configured block-size quote did not produce a fully validated fill result."
    elif pool_rows and values:
        status = "observed_pool_tvl_reference_only"
        reason = "Verified pool has observed USD TVL; exact block-size executable depth still requires invariant/tick replay."
    elif pool_rows:
        status = "verified_pool_state_liquidity_value_unavailable"
        reason = "Exact pool state was observed, but the public USD liquidity snapshot was unavailable; refresh or calculate reserves with verified quote valuation."
    elif rate_rows:
        status = "contract_exchange_rate_only_no_market_liquidity"
        reason = "Vault/share rate was observed, but no exact executable pool, route, or book liquidity was verified."
    elif current_rows:
        status = "verified_price_source_liquidity_value_unavailable"
        reason = "An exact source observation exists, but no normalized USD liquidity or tested executable notional is available."
    elif has_candidate:
        status = "candidate_market_exists_depth_not_captured"
        reason = "A catalog/venue candidate exists, but no exact current depth or route-liquidity observation is linked to this ticker."
    else:
        status = "no_verified_price_bearing_market"
        reason = "No exact pool, route, order book, oracle, or audited NAV/rate source with market liquidity is currently verified."
    return {
        "liquidity_status": status,
        "liquidity_value_usd": max(values) if values else None,
        "liquidity_value_semantics": "maximum observed single-source liquidity; sources are not summed" if values else "not available",
        "known_liquidity_source_count": len({r["contributor_id"] for r in current_rows if as_float(r.get("liquidity_value_usd")) is not None}),
        "tested_executable_notional_usd_max": max(tested) if tested else None,
        "liquidity_observed_at": max((clean(r.get("liquidity_observed_at")) for r in current_rows), default=""),
        "liquidity_reason": reason,
    }


def main() -> None:
    master = read_csv(MASTER)
    inventory = read_csv(INVENTORY)
    registry = read_csv(REGISTRY)
    rpc_rows = read_csv(RPC_MATRIX)
    refresh = read_csv(POOL_REFRESH)

    inv_by_id = {r["token_row_id"]: r for r in inventory}
    rpc_by_network = {r["network"]: r for r in rpc_rows}
    pool_refresh = {norm_address(r["pool_address"]): r for r in refresh if r.get("status") == "observed_public_pool_snapshot"}

    exact_assets: dict[tuple[str, str], set[str]] = defaultdict(set)
    alias_assets: dict[str, set[str]] = defaultdict(set)
    asset_master_rows: dict[str, list[dict[str, str]]] = defaultdict(list)
    ticker_master_rows: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in master:
        asset_id = row["asset_id"]
        exact_assets[(norm_network(row["network"]), norm_address(row["address"]))].add(asset_id)
        for alias in (row["rwa_xyz_ticker"], row["asset_id"], row["canonical_underlying_candidate"], row["symbol"].split("/")[0]):
            if clean(alias):
                alias_assets[clean(alias).upper()].add(asset_id)
        asset_master_rows[asset_id].append(row)
        ticker_master_rows[row["rwa_xyz_ticker"]].append(row)

    grouped_registry: dict[tuple[str, ...], dict[str, Any]] = {}
    unresolved_registry = 0
    for row in registry:
        address = norm_address(row.get("token_contract_address", ""))
        exact = exact_assets.get((norm_network(row.get("network", "")), address), set()) if address else set()
        mapping_status = "exact_network_contract" if len(exact) == 1 else ""
        assets = exact
        if len(assets) != 1:
            assets = alias_assets.get(clean(row.get("canonical_ticker", "")).upper(), set())
            mapping_status = "unique_canonical_symbol" if len(assets) == 1 else "unresolved_not_counted"
        asset_id = next(iter(assets)) if len(assets) == 1 else ""
        if not asset_id:
            unresolved_registry += 1
        scope = row.get("registry_scope", "")
        if scope == "derivative_market_candidate":
            status = "derivative_candidate_basis_gated"
        elif row.get("feed_status") == "sourced_candidate":
            status = "catalog_candidate"
        else:
            status = "access_or_adapter_required"
        instrument = clean(row.get("venue_market_id")) or clean(row.get("venue_symbol")) or address or clean(row.get("rwa_symbol"))
        key = (
            asset_id or f"UNRESOLVED:{clean(row.get('canonical_ticker'))}",
            clean(row.get("venue")), instrument, clean(row.get("market_type")),
            norm_network(row.get("network", "")), address, status,
        )
        group = grouped_registry.setdefault(key, {
            "canonical_feed_id": asset_id,
            "canonical_ticker": clean(row.get("canonical_ticker")),
            "contributor_id": "registry|" + "|".join(key),
            "contributor_status": status,
            "mapping_status": mapping_status,
            "venue": clean(row.get("venue")),
            "instrument_id": instrument,
            "market_type": clean(row.get("market_type")),
            "network": clean(row.get("network")),
            "token_contract_address": clean(row.get("token_contract_address")),
            "price_source_contract_or_route": "",
            "price_products": set(),
            "price_semantics": "derivative_reference_basis_gated" if status.startswith("derivative") else ("candidate_price_source_not_currently_observed" if status == "catalog_candidate" else "no_current_price_observation"),
            "current_price_or_reference": None,
            "current_bid": None,
            "current_ask": None,
            "block_size_vwap_10000": None,
            "tested_executable_notional_usd": None,
            "liquidity_metric": "not_measured",
            "liquidity_value_usd": None,
            "liquidity_observed_at": "",
            "volume_h24_usd": None,
            "block_or_slot": "",
            "liveness": "candidate only; current payload/depth not captured",
            "production_status": "not_production_promoted",
            "rights_status": "commercial redistribution and onchain-publication rights require source-specific review",
            "source_reference_url": clean(row.get("source_reference_url")),
            "lineage_group": clean(row.get("venue")) + "|" + instrument,
            "lineage_status": "venue/instrument proxy; upstream independence not proven",
            "quality_or_access_gate": safe_status(row.get("quality_gates") or row.get("next_action")),
        })
        group["price_products"].add(clean(row.get("price_source_type")))

    source_rows: list[dict[str, Any]] = []
    for group in grouped_registry.values():
        group["price_products"] = join(group["price_products"])
        source_rows.append(group)

    current_sources: list[dict[str, Any]] = []
    for row in inventory:
        status = row["onchain_price_path_status"]
        if not status.startswith("live_"):
            continue
        mechanism = row.get("price_mechanism", "")
        source_contract = clean(row.get("price_source_contract_or_route"))
        venue = clean(row.get("price_source_venue"))
        liquidity = as_float(row.get("liquidity_usd_discovery_snapshot"))
        volume = as_float(row.get("volume_h24_usd_discovery_snapshot"))
        liquidity_at = clean(row.get("observed_at"))
        liquidity_metric = "not_available"
        semantics = "contract_exchange_rate_reference"
        tested = None
        if mechanism == "evm_pool_state":
            semantics = "block_state_pool_reference_not_executable_depth"
            liquidity_metric = "pool_tvl_usd"
            refreshed = pool_refresh.get(norm_address(source_contract))
            if refreshed:
                liquidity = as_float(refreshed.get("liquidity_usd"))
                volume = as_float(refreshed.get("volume_h24_usd"))
                liquidity_at = clean(refreshed.get("observed_at"))
        elif mechanism == "jupiter_router_quote":
            semantics = "executable_router_quote_snapshot"
            liquidity_metric = "router_registry_token_liquidity_usd"
            tested = 10000.0 if clean(row.get("vwap_fill_status")).lower() in {"full", "filled", "ok"} or as_float(row.get("block_size_vwap_10000")) is not None else None
        contributor_id = f"current|{row['asset_id']}|{venue}|{source_contract}|{mechanism}|{row['network']}"
        current_sources.append({
            "canonical_feed_id": row["asset_id"],
            "canonical_ticker": row["rwa_ticker"],
            "contributor_id": contributor_id,
            "contributor_status": "current_exact_observed",
            "mapping_status": "exact_network_contract",
            "venue": venue,
            "instrument_id": source_contract or row["token_contract_address"],
            "market_type": "amm_route" if mechanism == "jupiter_router_quote" else "amm_pool" if mechanism == "evm_pool_state" else "vault_rate",
            "network": row["network"],
            "token_contract_address": row["token_contract_address"],
            "price_source_contract_or_route": source_contract,
            "price_products": "BidAsk | block-size VWAP" if mechanism == "jupiter_router_quote" else "pool reference" if mechanism == "evm_pool_state" else "assets-per-share",
            "price_semantics": semantics,
            "current_price_or_reference": as_float(row.get("current_mid_or_reference")) or as_float(row.get("erc4626_assets_per_share")),
            "current_bid": as_float(row.get("current_bid")),
            "current_ask": as_float(row.get("current_ask")),
            "block_size_vwap_10000": as_float(row.get("block_size_vwap_10000")),
            "tested_executable_notional_usd": tested,
            "liquidity_metric": liquidity_metric,
            "liquidity_value_usd": liquidity,
            "liquidity_observed_at": liquidity_at,
            "volume_h24_usd": volume,
            "block_or_slot": row.get("block_or_slot", ""),
            "liveness": row.get("liveness", ""),
            "production_status": "candidate_only_not_production_promoted",
            "rights_status": row.get("rights_status", ""),
            "source_reference_url": "",
            "lineage_group": venue + "|" + (source_contract or row["token_contract_address"]),
            "lineage_status": "exact observed venue/source; upstream independence still requires review",
            "quality_or_access_gate": row.get("required_gates", ""),
        })
    source_rows.extend(current_sources)

    sources_by_asset: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in source_rows:
        if row["canonical_feed_id"]:
            sources_by_asset[row["canonical_feed_id"]].append(row)

    def source_counts(asset_ids: set[str]) -> dict[str, int]:
        rows = [r for aid in asset_ids for r in sources_by_asset.get(aid, [])]
        def count(status: str) -> int:
            return len({r["contributor_id"] for r in rows if r["contributor_status"] == status})
        eligible = {r["contributor_id"] for r in rows if r["contributor_status"] in {"current_exact_observed", "catalog_candidate"}}
        return {
            "canonical_component_feed_count": len(eligible),
            "current_exact_observed_feed_count": count("current_exact_observed"),
            "catalog_candidate_feed_count": count("catalog_candidate"),
            "derivative_candidate_feed_count": count("derivative_candidate_basis_gated"),
            "access_or_adapter_required_feed_count": count("access_or_adapter_required"),
            "production_promoted_feed_count": 0,
            "lineage_group_count_proxy": len({r["lineage_group"] for r in rows if r["contributor_status"] in {"current_exact_observed", "catalog_candidate"}}),
        }

    ticker_rows: list[dict[str, Any]] = []
    liquidity_rows: list[dict[str, Any]] = []
    for ticker, rows in sorted(ticker_master_rows.items()):
        assets = {r["asset_id"] for r in rows}
        counts = source_counts(assets)
        current = [r for aid in assets for r in sources_by_asset.get(aid, []) if r["contributor_status"] == "current_exact_observed" and r["canonical_ticker"] == ticker]
        counts["current_exact_observed_feed_count"] = len({r["contributor_id"] for r in current})
        liquidity = liquidity_state(current, counts["catalog_candidate_feed_count"] > 0 or counts["derivative_candidate_feed_count"] > 0)
        canonical_labels = {r["canonical_underlying_candidate"] for r in rows}
        ticker_row = {
            "rwa_ticker": ticker,
            "rwa_symbol_examples": join({r["symbol"] for r in rows}, 8),
            "canonical_feed_ids": join(assets),
            "canonical_underlyings": join(canonical_labels),
            "asset_class": join({r["asset_class"] for r in rows}),
            "asset_names": join({r["asset_name"] for r in rows}, 5),
            "deployment_count": len(rows),
            "network_count": len({r["network"] for r in rows}),
            "networks": join({r["network"] for r in rows}),
            "contract_count": len({(r["network"], r["address"].lower()) for r in rows}),
            **counts,
            **liquidity,
            "current_price_semantics": join({r["price_semantics"] for r in current}),
            "current_venues": join({r["venue"] for r in current}),
            "identity_mapping_statuses": join({r["identity_mapping_status"] for r in rows}),
            "feed_fit_now": "candidate source inventory; zero production-promoted feeds" if counts["canonical_component_feed_count"] else "contract/state catalog only; no verified price component",
            "next_gate": liquidity["liquidity_reason"],
        }
        ticker_rows.append(ticker_row)
        liquidity_rows.append({
            "rwa_ticker": ticker,
            "canonical_feed_ids": ticker_row["canonical_feed_ids"],
            "asset_class": ticker_row["asset_class"],
            "deployment_count": len(rows),
            "current_exact_observed_feed_count": counts["current_exact_observed_feed_count"],
            "catalog_candidate_feed_count": counts["catalog_candidate_feed_count"],
            **liquidity,
            "venues_with_current_observation": ticker_row["current_venues"],
            "price_semantics": ticker_row["current_price_semantics"],
            "production_status": "not_production_promoted",
        })

    canonical_rows: list[dict[str, Any]] = []
    ticker_by_asset: dict[str, set[str]] = defaultdict(set)
    for row in master:
        ticker_by_asset[row["asset_id"]].add(row["rwa_xyz_ticker"])
    for asset_id, rows in sorted(asset_master_rows.items()):
        counts = source_counts({asset_id})
        current = [r for r in sources_by_asset.get(asset_id, []) if r["contributor_status"] == "current_exact_observed"]
        liquidity = liquidity_state(current, counts["catalog_candidate_feed_count"] > 0 or counts["derivative_candidate_feed_count"] > 0)
        candidates = [r for r in sources_by_asset.get(asset_id, []) if r["contributor_status"] in {"current_exact_observed", "catalog_candidate"}]
        canonical_rows.append({
            "canonical_feed_id": asset_id,
            "canonical_feed_label": join({r["canonical_underlying_candidate"] for r in rows}),
            "economic_asset_names": join({r["asset_name"] for r in rows}, 5),
            "asset_class": join({r["asset_class"] for r in rows}),
            "ticker_count": len(ticker_by_asset[asset_id]),
            "tickers": join(ticker_by_asset[asset_id], 20),
            "deployment_count": len(rows),
            "network_count": len({r["network"] for r in rows}),
            "networks": join({r["network"] for r in rows}),
            **counts,
            "component_venues": join({r["venue"] for r in candidates}),
            "component_price_semantics": join({r["price_semantics"] for r in candidates}),
            **liquidity,
            "production_status": "not_production_promoted",
            "canonical_feed_readiness": "current candidate components exist; quality, independence, rights, and continuous monitoring gates remain" if counts["current_exact_observed_feed_count"] else "candidate/pending components only; no current exact onchain observation",
        })

    canonical_count_by_asset = {r["canonical_feed_id"]: r["canonical_component_feed_count"] for r in canonical_rows}
    contract_rows: list[dict[str, Any]] = []
    for row in master:
        inv = inv_by_id[row["token_row_id"]]
        current = [r for r in sources_by_asset.get(row["asset_id"], []) if r["contributor_status"] == "current_exact_observed" and norm_address(r["token_contract_address"]) == norm_address(row["address"]) and norm_network(r["network"]) == norm_network(row["network"])]
        liquidity = liquidity_state(current, bool(row.get("currently_sourceable", "").lower() == "true"))
        rpc = rpc_by_network.get(row["network"], {})
        raw_status = inv["onchain_price_path_status"]
        operational = {
            "token_contract_state_only_no_verified_price_path": "no_verified_price_bearing_contract_or_market",
            "offchain_or_symbol_mapped_price_candidate_not_exact_onchain": "separate_candidate_exists_exact_onchain_path_absent",
            "contract_rate_candidate_unverified_or_blocked": "vault_interface_or_rpc_verification_required",
        }.get(raw_status, raw_status)
        contract_rows.append({
            "token_row_id": row["token_row_id"],
            "rwa_ticker": row["rwa_xyz_ticker"],
            "rwa_symbol": row["symbol"],
            "canonical_feed_id": row["asset_id"],
            "canonical_underlying": row["canonical_underlying_candidate"],
            "canonical_component_feed_count": canonical_count_by_asset[row["asset_id"]],
            "asset_name": row["asset_name"],
            "asset_class": row["asset_class"],
            "issuer_name": row["issuer_name"],
            "platform": row["platform"],
            "network": row["network"],
            "token_contract_address": row["address"],
            "standards": row["standards"],
            "identity_mapping_status": row["identity_mapping_status"],
            "operational_price_path_status": operational,
            "price_mechanism": inv["price_mechanism"],
            "price_source_venue": inv["price_source_venue"],
            "price_source_contract_or_route": inv["price_source_contract_or_route"],
            "quote_or_underlying_contract": inv["quote_or_underlying_contract"],
            "current_mid_or_reference": as_float(inv["current_mid_or_reference"]),
            "current_bid": as_float(inv["current_bid"]),
            "current_ask": as_float(inv["current_ask"]),
            "block_size_vwap_10000": as_float(inv["block_size_vwap_10000"]),
            "vwap_fill_status": inv["vwap_fill_status"],
            "erc4626_assets_per_share": as_float(inv["erc4626_assets_per_share"]),
            "block_or_slot": inv["block_or_slot"],
            "observed_at": inv["observed_at"],
            "liveness": inv["liveness"],
            "economic_activity_liveness": inv["economic_activity_liveness"],
            **liquidity,
            "volume_h24_usd": max([as_float(r.get("volume_h24_usd")) for r in current if as_float(r.get("volume_h24_usd")) is not None], default=None),
            "token_contract_data_available": inv["token_contract_data_available"],
            "price_data_from_token_contract": inv["price_data_from_token_contract"],
            "price_semantics": inv["price_semantics"],
            "rpc_access_type": rpc.get("access_type", "unclassified_network_access"),
            "rpc_configured": rpc.get("configured_in_env", "False"),
            "rpc_or_access_requirement": rpc.get("required_env", "") or rpc.get("method", ""),
            "blocksize_feed_fit_now": inv["blocksize_feed_fit_now"],
            "blocksize_feed_fit_after_gates": inv["blocksize_feed_fit_after_gates"],
            "production_status": inv["production_status"],
            "required_gates": inv["required_gates"],
            "rights_status": inv["rights_status"],
        })

    status_order = {"current_exact_observed": 0, "catalog_candidate": 1, "derivative_candidate_basis_gated": 2, "access_or_adapter_required": 3}
    source_rows.sort(key=lambda r: (
        not bool(r.get("canonical_feed_id", "")),
        status_order.get(r.get("contributor_status", ""), 9),
        r.get("canonical_feed_id", ""), r.get("venue", ""), r.get("instrument_id", ""),
    ))
    all_payload = {
        "generated_at": datetime.now(UTC).isoformat(),
        "basis": str(MASTER),
        "definitions": {
            "canonical_component_feed_count": "Distinct current exact or catalog candidate contributors after deduping BidAsk/VWAP rows for the same venue instrument; derivatives and access-required rows are separate.",
            "liquidity_value_usd": "Maximum observed single-source liquidity for the ticker/canonical feed. Values are not summed across correlated wrappers, routes, or venues.",
            "tested_executable_notional_usd_max": "Largest configured quote notional that returned a full point-in-time route result. It is not guaranteed capacity.",
            "production_promoted": "Zero until continuous quality, replay, rights, manipulation, and independent-consensus gates pass.",
        },
        "summary": {
            "contract_deployments": len(contract_rows),
            "tickers": len(ticker_rows),
            "canonical_feeds": len(canonical_rows),
            "source_feed_rows": len(source_rows),
            "current_exact_source_feeds": sum(r["contributor_status"] == "current_exact_observed" for r in source_rows),
            "tickers_with_liquidity_value": sum(r["liquidity_value_usd"] is not None for r in liquidity_rows),
            "tickers_with_tested_executable_notional": sum(r["tested_executable_notional_usd_max"] is not None for r in liquidity_rows),
            "production_promoted_feeds": 0,
            "unresolved_registry_records_after_dedupe_input": unresolved_registry,
            "liquidity_status_counts": dict(Counter(r["liquidity_status"] for r in liquidity_rows)),
        },
        "canonical_feeds": canonical_rows,
        "ticker_inventory": ticker_rows,
        "contract_inventory": contract_rows,
        "source_feeds": source_rows,
        "liquidity": liquidity_rows,
        "data_dictionary": [
            {"field": "canonical_feed_id", "definition": "Controlling economic asset id from the RWA master; wrappers/deployments remain separate on the Contract Inventory tab."},
            {"field": "canonical_component_feed_count", "definition": "Distinct current exact or catalog candidate contributor feeds; raw BidAsk/VWAP registry row duplication removed."},
            {"field": "current_exact_observed_feed_count", "definition": "Exact network+contract source with a successful current pool/router/rate observation."},
            {"field": "catalog_candidate_feed_count", "definition": "Known candidate source in the source registry; not necessarily live, rights-cleared, or depth-tested."},
            {"field": "derivative_candidate_feed_count", "definition": "Perpetual/derivative candidates retained as basis-gated references, excluded from raw spot VWAP."},
            {"field": "liquidity_value_usd", "definition": "Maximum single-source observed liquidity. Pool TVL or router registry liquidity according to liquidity metric; not additive."},
            {"field": "tested_executable_notional_usd_max", "definition": "Maximum configured quote sweep with a full route result; a point observation, not guaranteed capacity."},
            {"field": "lineage_group_count_proxy", "definition": "Distinct venue/instrument lineage proxies; upstream licensing and independence are not proven by this count."},
            {"field": "production_promoted_feed_count", "definition": "Feeds admitted to production composite after all quality/rights gates. Current value is zero."},
            {"field": "rpc_configured", "definition": "Whether the required RPC environment entry was configured during the access audit. RPC state alone is not a price."},
        ],
    }
    def sanitize(obj: Any) -> Any:
        if isinstance(obj, dict):
            return {k: sanitize(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [sanitize(v) for v in obj]
        return safe_status(obj)
    OUTPUT.write_text(json.dumps(sanitize(all_payload), indent=2), encoding="utf-8")
    print(json.dumps(all_payload["summary"], indent=2))


if __name__ == "__main__":
    main()
