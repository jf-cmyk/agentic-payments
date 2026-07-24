"""DEX route and pool allowlist planning for RWA market-data expansion."""

from __future__ import annotations

import csv
import json
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

from src.rwa_coverage import DEX_QUALITY_REQUIREMENTS, DEX_RWA_SYMBOLS
from src.rwa_provider_catalog import build_provider_catalog


DEX_VENUE_EXECUTION: dict[str, dict[str, Any]] = {
    "jupiter_router": {
        "chain": "solana",
        "chain_id": "solana-mainnet",
        "candidate_kind": "router_route",
        "source_type": "quote_sweep",
        "adapter_lane": "dex_quote_router_adapter",
        "status": "blocked_by_auth_or_rpc",
        "endpoint_families": ["quote", "route_plan", "price_impact", "context_slot", "dex_labels"],
        "required_identifiers": ["base_mint", "quote_mint", "route_plan", "context_slot"],
    },
    "raydium_clmm": {
        "chain": "solana",
        "chain_id": "solana-mainnet",
        "candidate_kind": "pool_state",
        "source_type": "onchain_clmm_pool",
        "adapter_lane": "solana_pool_state_adapter",
        "status": "planned_adapter",
        "endpoint_families": ["pool_catalog", "pool_state", "sdk_quote", "grpc_monitoring"],
        "required_identifiers": ["pool_id", "base_mint", "quote_mint", "tick_or_curve_state", "slot"],
    },
    "orca_whirlpool": {
        "chain": "solana",
        "chain_id": "solana-mainnet",
        "candidate_kind": "pool_state",
        "source_type": "onchain_clmm_pool",
        "adapter_lane": "solana_pool_state_adapter",
        "status": "planned_adapter",
        "endpoint_families": ["whirlpool_state", "ticks", "sdk_quote", "rpc"],
        "required_identifiers": ["whirlpool_address", "base_mint", "quote_mint", "tick_arrays", "slot"],
    },
    "meteora_dlmm": {
        "chain": "solana",
        "chain_id": "solana-mainnet",
        "candidate_kind": "pool_state",
        "source_type": "onchain_clmm_pool",
        "adapter_lane": "solana_pool_state_adapter",
        "status": "planned_adapter",
        "endpoint_families": ["pool_catalog", "dlmm_bins", "sdk_quote", "rpc"],
        "required_identifiers": ["pool_id", "base_mint", "quote_mint", "active_bin", "slot"],
    },
    "uniswap_v3_v4": {
        "chain": "ethereum_or_evm",
        "chain_id": "evm-multi",
        "candidate_kind": "pool_state",
        "source_type": "onchain_clmm_pool",
        "adapter_lane": "evm_pool_state_adapter",
        "status": "blocked_by_auth_or_rpc",
        "endpoint_families": ["pool_state", "ticks", "swaps", "subgraph", "rpc"],
        "required_identifiers": ["chain_id", "pool_address", "base_token", "quote_token", "fee_tier", "block_number"],
    },
    "curve_stableswap": {
        "chain": "ethereum_or_evm",
        "chain_id": "evm-multi",
        "candidate_kind": "stableswap_pool",
        "source_type": "onchain_stableswap_pool",
        "adapter_lane": "evm_stableswap_adapter",
        "status": "planned_adapter",
        "endpoint_families": ["pool_registry", "balances", "virtual_price", "swap_simulation", "rpc"],
        "required_identifiers": ["chain_id", "pool_address", "base_token", "quote_token", "balances", "block_number"],
    },
    "balancer_pools": {
        "chain": "ethereum_or_evm",
        "chain_id": "evm-multi",
        "candidate_kind": "weighted_or_stable_pool",
        "source_type": "onchain_stableswap_pool",
        "adapter_lane": "evm_weighted_pool_adapter",
        "status": "blocked_by_auth_or_rpc",
        "endpoint_families": ["pool_state", "balances", "weights", "subgraph", "rpc"],
        "required_identifiers": ["chain_id", "pool_id", "base_token", "quote_token", "weights_or_amplification", "block_number"],
    },
    "aerodrome_slipstream": {
        "chain": "base",
        "chain_id": "base-mainnet",
        "candidate_kind": "pool_state",
        "source_type": "onchain_clmm_pool",
        "adapter_lane": "evm_pool_state_adapter",
        "status": "planned_adapter",
        "endpoint_families": ["pool_state", "ticks", "route_quote", "rpc"],
        "required_identifiers": ["pool_address", "base_token", "quote_token", "tick_state", "block_number"],
    },
}


ASSET_CLASS_MINIMUMS: dict[str, dict[str, Any]] = {
    "equity": {
        "min_liquidity_usd": 250_000,
        "min_24h_volume_usd": 50_000,
        "max_price_impact_bps_by_block": {"1000": 75, "5000": 150, "10000": 250},
        "benchmark_sources": ["Blocksize equity/tokenized benchmark", "licensed U.S. equity benchmark", "Pyth/Chainlink where available"],
    },
    "etf": {
        "min_liquidity_usd": 500_000,
        "min_24h_volume_usd": 100_000,
        "max_price_impact_bps_by_block": {"5000": 50, "25000": 125, "100000": 250},
        "benchmark_sources": ["Blocksize ETF benchmark", "licensed ETF NBBO/trades", "Pyth/Chainlink where available"],
    },
    "fx": {
        "min_liquidity_usd": 1_000_000,
        "min_24h_volume_usd": 250_000,
        "max_price_impact_bps_by_block": {"10000": 10, "50000": 25, "100000": 50},
        "benchmark_sources": ["Blocksize FX feed", "institutional FX benchmark", "Pyth/Chainlink/API3 where available"],
    },
    "treasury_fund": {
        "min_liquidity_usd": 250_000,
        "min_24h_volume_usd": 25_000,
        "max_price_impact_bps_by_block": {"10000": 25, "100000": 75, "500000": 150},
        "benchmark_sources": ["issuer NAV", "Chainlink/Pyth NAV where available", "licensed Treasury/fund benchmark"],
    },
    "tokenized_fund": {
        "min_liquidity_usd": 250_000,
        "min_24h_volume_usd": 25_000,
        "max_price_impact_bps_by_block": {"10000": 50, "100000": 100, "500000": 200},
        "benchmark_sources": [
            "issuer NAV or fund administrator reference",
            "Blocksize state reference where state_instruments coverage exists",
            "Chainlink/Pyth NAV where available",
        ],
    },
    "metal": {
        "min_liquidity_usd": 250_000,
        "min_24h_volume_usd": 50_000,
        "max_price_impact_bps_by_block": {"5000": 50, "25000": 125, "100000": 250},
        "benchmark_sources": ["Blocksize metal feed", "issuer reserve/NAV", "COMEX/LME fair-value benchmark"],
    },
}


SUPPORTED_ALLOWLIST_STATUSES = {"all", "planned_adapter", "blocked_by_auth_or_rpc", "ready_for_live_probe"}


def _symbol_parts(symbol: str) -> tuple[str, str]:
    if "/" in symbol:
        base, quote = symbol.split("/", 1)
        return base, quote
    return symbol, "USD"


def _minimums_for(asset_class: str) -> dict[str, Any]:
    return deepcopy(ASSET_CLASS_MINIMUMS.get(asset_class, ASSET_CLASS_MINIMUMS["equity"]))


def _blockers_for(row: dict[str, Any], asset_class: str) -> list[str]:
    blockers = [
        "token_contract_or_mint_verification",
        "pool_or_route_identifier_discovery",
        "live_liquidity_and_24h_volume_measurement",
        "block_or_slot_freshness_validation",
        "price_impact_quote_sweep",
        "manipulation_and_concentration_checks",
        "Blocksize_or_regulated_benchmark_alignment",
    ]
    if row["status"] == "blocked_by_auth_or_rpc":
        blockers.insert(0, "api_key_rpc_or_indexer_access")
    if asset_class in {"equity", "etf"}:
        blockers.append("issuer_underlying_identity_and_transfer_restriction_review")
    if asset_class in {"treasury_fund", "tokenized_fund"}:
        blockers.append("issuer_nav_and_attestation_alignment")
    return blockers


def build_dex_allowlist(
    *,
    venue: str = "all",
    status: str = "all",
) -> dict[str, Any]:
    """Build DEX route/pool candidates and promotion jobs."""
    venue_filter = venue.strip().lower()
    status_filter = status.strip().lower()
    if venue_filter != "all" and venue_filter not in DEX_VENUE_EXECUTION:
        raise ValueError(f"Unsupported DEX venue: {venue}")
    if status_filter not in SUPPORTED_ALLOWLIST_STATUSES:
        raise ValueError(f"Unsupported DEX allowlist status: {status}")

    provider_catalog = build_provider_catalog(category="dex_liquidity")
    provider_by_id = {row["provider_id"]: row for row in provider_catalog["providers"]}
    candidates: list[dict[str, Any]] = []
    for venue_id, by_asset_class in DEX_RWA_SYMBOLS.items():
        if venue_filter != "all" and venue_id != venue_filter:
            continue
        execution = DEX_VENUE_EXECUTION[venue_id]
        provider = provider_by_id.get(venue_id, {})
        for asset_class, symbols in by_asset_class.items():
            minimums = _minimums_for(asset_class)
            for symbol in symbols:
                base, quote = _symbol_parts(symbol)
                candidate = {
                    "allowlist_id": f"dex:{venue_id}:{base.upper()}:{quote.upper()}",
                    "venue": venue_id,
                    "provider_name": provider.get("name", venue_id),
                    "symbol": symbol,
                    "asset_id": base.replace("x", "").replace("X", "").replace("_1", "").upper(),
                    "quote_asset": quote.upper(),
                    "asset_class": asset_class,
                    "chain": execution["chain"],
                    "chain_id": execution["chain_id"],
                    "candidate_kind": execution["candidate_kind"],
                    "source_type": execution["source_type"],
                    "adapter_lane": execution["adapter_lane"],
                    "status": execution["status"],
                    "endpoint_families": execution["endpoint_families"],
                    "required_identifiers": execution["required_identifiers"],
                    "required_observation_fields": DEX_QUALITY_REQUIREMENTS["minimum_fields"],
                    "quality_tier": DEX_QUALITY_REQUIREMENTS["initial_quality_tier"].get(venue_id),
                    "minimums": minimums,
                    "benchmark_sources": minimums["benchmark_sources"],
                    "blockers": _blockers_for(execution, asset_class),
                    "promotion_status": "candidate_requires_pool_and_liquidity_validation",
                    "promotion_gates": DEX_QUALITY_REQUIREMENTS["promotion_gates"],
                }
                if status_filter == "all" or candidate["status"] == status_filter:
                    candidates.append(candidate)

    promotion_jobs = [
        {
            "job_id": f"dex_allowlist:{row['venue']}:{row['asset_id']}:{row['quote_asset']}",
            "venue": row["venue"],
            "symbol": row["symbol"],
            "asset_class": row["asset_class"],
            "chain": row["chain"],
            "source_type": row["source_type"],
            "status": row["status"],
            "adapter_lane": row["adapter_lane"],
            "required_identifiers": row["required_identifiers"],
            "blockers": row["blockers"],
            "promotion_gate": row["promotion_status"],
        }
        for row in candidates
    ]

    by_venue = Counter(str(row["venue"]) for row in candidates)
    by_asset_class = Counter(str(row["asset_class"]) for row in candidates)
    by_status = Counter(str(row["status"]) for row in candidates)
    by_source_type = Counter(str(row["source_type"]) for row in candidates)
    by_chain = Counter(str(row["chain"]) for row in candidates)
    return {
        "summary": {
            "candidate_count": len(candidates),
            "promotion_job_count": len(promotion_jobs),
            "provider_dex_count": provider_catalog["summary"]["provider_count"],
            "by_venue": dict(sorted(by_venue.items())),
            "by_asset_class": dict(sorted(by_asset_class.items())),
            "by_status": dict(sorted(by_status.items())),
            "by_source_type": dict(sorted(by_source_type.items())),
            "by_chain": dict(sorted(by_chain.items())),
        },
        "filters": {"venue": venue, "status": status},
        "quality_requirements": deepcopy(DEX_QUALITY_REQUIREMENTS),
        "venue_execution": deepcopy(DEX_VENUE_EXECUTION),
        "asset_class_minimums": deepcopy(ASSET_CLASS_MINIMUMS),
        "candidates": sorted(candidates, key=lambda row: (str(row["venue"]), str(row["asset_id"]), str(row["quote_asset"]))),
        "promotion_jobs": sorted(promotion_jobs, key=lambda row: str(row["job_id"])),
        "execution_order": [
            "Verify token mint or contract identity before requesting quotes or pool state.",
            "Discover pool, route, fee-tier, tick/bin, balance, and block/slot identifiers.",
            "Measure liquidity, organic volume, price impact, and source timestamp freshness.",
            "Replay route or pool-state payloads through /v1/rwa/vwap/calculate and /v1/rwa/realtime/quality.",
            "Benchmark against Blocksize, oracle, issuer NAV, regulated market, or futures fair-value references.",
            "Promote only candidates that pass manipulation, concentration, freshness, spread/depth, and rights checks.",
        ],
    }


def write_dex_allowlist_reports(
    *,
    json_path: str | Path,
    csv_path: str | Path,
) -> dict[str, Any]:
    """Write DEX allowlist planning reports."""
    allowlist = build_dex_allowlist()
    json_out = Path(json_path)
    csv_out = Path(csv_path)
    json_out.parent.mkdir(parents=True, exist_ok=True)
    csv_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(allowlist, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with csv_out.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = [
            "allowlist_id",
            "venue",
            "symbol",
            "asset_id",
            "quote_asset",
            "asset_class",
            "chain",
            "source_type",
            "status",
            "adapter_lane",
            "minimums",
            "blockers",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in allowlist["candidates"]:
            writer.writerow(
                {
                    "allowlist_id": row["allowlist_id"],
                    "venue": row["venue"],
                    "symbol": row["symbol"],
                    "asset_id": row["asset_id"],
                    "quote_asset": row["quote_asset"],
                    "asset_class": row["asset_class"],
                    "chain": row["chain"],
                    "source_type": row["source_type"],
                    "status": row["status"],
                    "adapter_lane": row["adapter_lane"],
                    "minimums": json.dumps(row["minimums"]),
                    "blockers": json.dumps(row["blockers"]),
                }
            )
    return allowlist
