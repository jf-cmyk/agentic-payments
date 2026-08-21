"""Sourcing job planner for oracle-parity RWA coverage."""

from __future__ import annotations

from collections import Counter
from typing import Any

from src.rwa_adapters import RWA_ADAPTER_REGISTRY
from src.rwa_coverage import build_oracle_parity_matrix, build_rwa_asset_matrix
from src.rwa_derivative_venues import (
    DERIVATIVE_VENUE_CONFIGS,
    load_derivative_coverage_rows,
    load_derivative_venue_rows,
)
from src.rwa_hyperliquid import (
    HYPERLIQUID_RWA_SPOT_SYMBOLS,
    HYPERLIQUID_RWA_SPOT_VENUE_ID,
    hyperliquid_is_unverified,
    hyperliquid_normalized_asset_class,
)
from src.rwa_hyperliquid_discovery import (
    HYPERLIQUID_PERPS_VENUE_ID,
    HYPERLIQUID_SPOT_VENUE_ID,
    load_hyperliquid_tradeable_coverage_rows,
)
from src.rwa_daily_feed_agent import (
    build_rwa_xyz_token_action,
    rwa_xyz_token_contract_key,
    rwa_xyz_token_priority,
)
from src.rwa_xyz_monitor import RWA_XYZ_VENUE_ID, load_rwa_xyz_token_rows


ENDPOINT_HINTS: dict[str, dict[str, Any]] = {
    "kraken_xstocks": {
        "job_type": "exchange_catalog_and_l2",
        "endpoint_hint": "Kraken public REST/WS: AssetPairs/instrument, Ticker, Depth, Trades, OHLC",
        "source_mode": "executable_market_data",
    },
    "ostium": {
        "job_type": "synthetic_prices_and_depth",
        "endpoint_hint": "Ostium Builder API: /v1/prices, /v1/prices/stream, /v1/ohlc, simulated orderbook/slippage",
        "source_mode": "synthetic_market_data",
    },
    "gains": {
        "job_type": "mark_stream_and_trade_history",
        "endpoint_hint": "Gains backend pricing WS plus recent trading-history/stats endpoints",
        "source_mode": "synthetic_mark_reference",
    },
    "jupiter_xstocks": {
        "job_type": "quote_sweep",
        "endpoint_hint": "Jupiter quote/Ultra APIs over xStocks token mints and route plans",
        "source_mode": "quote_route",
    },
    "jupiter_router": {
        "job_type": "dex_quote_sweep",
        "endpoint_hint": "Jupiter Swap/Price APIs: quote routePlan, priceImpactPct, contextSlot, DEX labels; API key required for platform endpoint",
        "source_mode": "dex_quote_route",
    },
    "raydium_clmm": {
        "job_type": "dex_pool_state",
        "endpoint_hint": "Raydium API v3 for pool data/routing plus SDK or gRPC for real-time monitoring",
        "source_mode": "onchain_clmm_pool",
    },
    "orca_whirlpool": {
        "job_type": "dex_pool_state",
        "endpoint_hint": "Orca Whirlpool SDK/RPC tick and liquidity state for allowlisted pools",
        "source_mode": "onchain_clmm_pool",
    },
    "meteora_dlmm": {
        "job_type": "dex_pool_state",
        "endpoint_hint": "Meteora DLMM SDK/RPC bin, liquidity, dynamic-fee, and quote simulation state",
        "source_mode": "onchain_clmm_pool",
    },
    "uniswap_v3_v4": {
        "job_type": "dex_pool_indexer_and_rpc",
        "endpoint_hint": "Uniswap v3/v4 subgraphs through The Graph plus RPC tick/pool state for freshness",
        "source_mode": "onchain_clmm_pool",
    },
    "curve_stableswap": {
        "job_type": "dex_stableswap_pool",
        "endpoint_hint": "Curve pool registry/RPC balances, virtual price, and swap simulation for stable/NAV pairs",
        "source_mode": "onchain_stableswap_pool",
    },
    "balancer_pools": {
        "job_type": "dex_weighted_or_stable_pool",
        "endpoint_hint": "Balancer pool state/subgraph plus RPC balances/weights for tokenized funds and stables",
        "source_mode": "onchain_stableswap_pool",
    },
    "aerodrome_slipstream": {
        "job_type": "dex_base_clmm_pool",
        "endpoint_hint": "Aerodrome Slipstream pool state, ticks, route quote, and Base block freshness",
        "source_mode": "onchain_clmm_pool",
    },
    "bybit_xstocks": {
        "job_type": "exchange_catalog_and_l2",
        "endpoint_hint": "Bybit v5 instruments, tickers, orderbook, recent trades, websocket",
        "source_mode": "executable_market_data",
    },
    "ondo_stocks": {
        "job_type": "quote_stream_catalog",
        "endpoint_hint": "Ondo whitelisted product catalog, quotes, price stream, OHLC, attestations",
        "source_mode": "api_keyed_quote_reference",
    },
    "backed_xstocks_issuer": {
        "job_type": "issuer_metadata",
        "endpoint_hint": "Backed/xStocks issuer product catalog, token contracts, attestations",
        "source_mode": "issuer_reference",
    },
    "polygon_tradfi_reference": {
        "job_type": "licensed_benchmark_reference",
        "endpoint_hint": "Licensed benchmark provider: NBBO/trades/OHLC/reference/corporate actions",
        "source_mode": "benchmark_reference",
    },
    "us_equity_consolidated_tape": {
        "job_type": "licensed_us_equity_universe",
        "endpoint_hint": "U.S. consolidated/direct equity feed: security master, NBBO, tick trades, snapshots, corporate actions",
        "source_mode": "licensed_consolidated_tape",
    },
    "hkex_licensed_equities": {
        "job_type": "licensed_hkex_equity_universe",
        "endpoint_hint": "HKEX OMD-C or licensed vendor feed: securities master, real-time quotes, trades, and depth",
        "source_mode": "licensed_exchange_feed",
    },
    "china_a_share_licensed_equities": {
        "job_type": "licensed_china_a_share_universe",
        "endpoint_hint": "SSE/SZSE/China Connect licensed vendor feed: security master, real-time quotes, trades, and depth",
        "source_mode": "licensed_exchange_feed",
    },
    "krx_licensed_equities": {
        "job_type": "licensed_krx_equity_universe",
        "endpoint_hint": "KRX market-data system or licensed vendor feed: KOSPI/KOSDAQ master, quotes, trades, and depth",
        "source_mode": "licensed_exchange_feed",
    },
    "jpx_licensed_equities": {
        "job_type": "licensed_jpx_equity_universe",
        "endpoint_hint": "JPX/TSE licensed vendor feed: security master, quotes, trades, market depth, corporate actions",
        "source_mode": "licensed_exchange_feed",
    },
    "twse_licensed_equities": {
        "job_type": "licensed_twse_tpex_equity_universe",
        "endpoint_hint": "TWSE/TPEx licensed vendor feed: security master, quotes, trades, market depth, corporate actions",
        "source_mode": "licensed_exchange_feed",
    },
    "india_nse_bse_licensed_equities": {
        "job_type": "licensed_india_equity_universe",
        "endpoint_hint": "NSE/BSE licensed vendor feed: security master, quotes, trades, market depth, corporate actions",
        "source_mode": "licensed_exchange_feed",
    },
    "lse_lseg_licensed_equities": {
        "job_type": "licensed_uk_equity_universe",
        "endpoint_hint": "LSE/LSEG licensed vendor feed: security master, quotes, trades, market depth, corporate actions",
        "source_mode": "licensed_exchange_feed",
    },
    "euronext_licensed_equities": {
        "job_type": "licensed_euronext_equity_universe",
        "endpoint_hint": "Euronext licensed vendor feed: security master, quotes, trades, market depth, corporate actions",
        "source_mode": "licensed_exchange_feed",
    },
    "deutsche_boerse_xetra_licensed_equities": {
        "job_type": "licensed_xetra_equity_universe",
        "endpoint_hint": "Deutsche Boerse/Xetra licensed vendor feed: security master, quotes, trades, market depth, corporate actions",
        "source_mode": "licensed_exchange_feed",
    },
    "tsx_licensed_equities": {
        "job_type": "licensed_canada_equity_universe",
        "endpoint_hint": "TSX/TSXV/TMX licensed vendor feed: security master, quotes, trades, market depth, corporate actions",
        "source_mode": "licensed_exchange_feed",
    },
    "asx_licensed_equities": {
        "job_type": "licensed_asx_equity_universe",
        "endpoint_hint": "ASX licensed vendor feed: security master, quotes, trades, market depth, corporate actions",
        "source_mode": "licensed_exchange_feed",
    },
    "sgx_licensed_equities": {
        "job_type": "licensed_sgx_equity_universe",
        "endpoint_hint": "SGX licensed vendor feed: security master, quotes, trades, market depth, corporate actions",
        "source_mode": "licensed_exchange_feed",
    },
    "pyth_oracle_reference": {
        "job_type": "oracle_catalog_reference",
        "endpoint_hint": "Pyth price feed catalog, price/confidence, rates, macro, NAV where licensed/available",
        "source_mode": "oracle_reference",
    },
    "chainlink_oracle_reference": {
        "job_type": "oracle_feed_reference",
        "endpoint_hint": "Chainlink feed explorer/contracts: answer, heartbeat, deviation, NAV, PoR, tokenized-asset categories",
        "source_mode": "oracle_reference",
    },
    "treasury_nav": {
        "job_type": "nav_reference",
        "endpoint_hint": "Issuer NAV/yield/redemption quote and attestation APIs",
        "source_mode": "nav_reference",
    },
    "hyperliquid_paxg": {
        "job_type": "perp_l2_reference",
        "endpoint_hint": "Hyperliquid info REST/WS for PAXG l2Book, mids, candles, trades",
        "source_mode": "executable_market_data",
    },
    HYPERLIQUID_PERPS_VENUE_ID: {
        "job_type": "perp_l2_tradeable_universe",
        "endpoint_hint": "Hyperliquid public meta plus info l2Book/mids/trades/funding for active perp markets",
        "source_mode": "executable_market_data",
    },
    HYPERLIQUID_SPOT_VENUE_ID: {
        "job_type": "spot_l2_tradeable_universe",
        "endpoint_hint": "Hyperliquid public spotMeta plus info l2Book @pair_index for crypto and RWA/traditional spot pairs",
        "source_mode": "executable_market_data",
    },
    HYPERLIQUID_RWA_SPOT_VENUE_ID: {
        "job_type": "spot_l2_rwa_reference",
        "endpoint_hint": "Hyperliquid public spotMeta plus info l2Book @pair_index for tokenized equities, ETFs, fiat/stable, Treasury, gold, and private-market candidates",
        "source_mode": "executable_market_data",
    },
    RWA_XYZ_VENUE_ID: {
        "job_type": "platform_catalog_token_realtime_discovery",
        "endpoint_hint": "RWA.xyz New Asset Monitor via public Next.js data payload; use token address/network/platform to discover executable pools, routes, issuer quotes, and order books",
        "source_mode": "platform_catalog_reference_plus_token_pool_discovery",
    },
}

for _derivative_config in DERIVATIVE_VENUE_CONFIGS:
    ENDPOINT_HINTS.setdefault(
        str(_derivative_config["venue_id"]),
        {
            "job_type": "derivative_catalog_orderbook_and_fair_value_probe",
            "endpoint_hint": (
                str(_derivative_config.get("endpoint_url") or "No confirmed public catalog endpoint")
                + "; capture market ids, contract specs, L1/L2 book, trades, mark/index, funding, open interest, and raw payload hashes"
            ),
            "source_mode": "derivative_market_data_with_fair_value_adjustment",
        },
    )


def _adapter_metadata() -> dict[str, dict[str, Any]]:
    return {
        item["venue_id"]: item
        for item in RWA_ADAPTER_REGISTRY.list_metadata()
    }


def _venue_symbol_lookup() -> dict[tuple[str, str], str]:
    matrix = build_rwa_asset_matrix()
    lookup: dict[tuple[str, str], str] = {}
    for asset in matrix["assets"]:
        asset_id = str(asset["asset_id"]).upper()
        for venue_id, venue_data in (asset.get("venues") or {}).items():
            symbol = venue_data.get("symbol")
            if symbol:
                lookup[(asset_id, str(venue_id))] = str(symbol)
    return lookup


def _job_status(venue: str, metadata: dict[str, Any] | None) -> str:
    if metadata is None:
        return "missing_registry_entry"
    if metadata.get("requires_auth"):
        return "blocked_by_auth_or_license"
    if metadata.get("implementation") != "planned_adapter":
        return "ready_to_probe"
    return "planned_adapter"


def _hyperliquid_spot_sourcing_jobs(adapter_metadata: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    metadata = adapter_metadata.get(HYPERLIQUID_RWA_SPOT_VENUE_ID)
    hint = ENDPOINT_HINTS[HYPERLIQUID_RWA_SPOT_VENUE_ID]
    jobs = []
    for row in HYPERLIQUID_RWA_SPOT_SYMBOLS:
        asset_class = hyperliquid_normalized_asset_class(row)
        unverified = hyperliquid_is_unverified(row)
        jobs.append(
            {
                "job_id": f"hyperliquid_spot:{row['symbol']}:{row['pair_index']}",
                "priority": "P1" if unverified else "P0",
                "category": "hyperliquid_spot_discovered_rwa",
                "asset_class": asset_class,
                "symbol": row["display_pair"],
                "asset_id": row["symbol"],
                "venue": HYPERLIQUID_RWA_SPOT_VENUE_ID,
                "job_type": hint["job_type"],
                "source_mode": hint["source_mode"],
                "endpoint_hint": hint["endpoint_hint"],
                "status": _job_status(HYPERLIQUID_RWA_SPOT_VENUE_ID, metadata),
                "requires_auth": bool((metadata or {}).get("requires_auth")),
                "adapter_implementation": (metadata or {}).get("implementation"),
                "target_status": "unverified_identity_hold" if unverified else "discovered_sourceable",
                "missing_source_types": [],
                "metadata": {
                    "hyperliquid_coin": row["hyperliquid_coin"],
                    "pair_index": row["pair_index"],
                    "hyperliquid_asset_class": row["asset_class"],
                    "identity_note": row["identity_note"],
                    "token_id": row["token_id"],
                    "evm_contract": row["evm_contract"],
                    "promotion_gate": (
                        "manual_identity_review_required"
                        if unverified
                        else "issuer_identity_liquidity_and_benchmark_validation_required"
                    ),
                },
            }
        )
    return jobs


def _hyperliquid_tradeable_sourcing_jobs(adapter_metadata: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    jobs = []
    for row in load_hyperliquid_tradeable_coverage_rows():
        venue = str(row.get("venue") or "")
        if venue not in {HYPERLIQUID_PERPS_VENUE_ID, HYPERLIQUID_SPOT_VENUE_ID}:
            continue
        metadata = adapter_metadata.get(venue)
        hint = ENDPOINT_HINTS[venue]
        row_metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        jobs.append(
            {
                "job_id": f"{venue}:{row.get('asset_id')}:{row_metadata.get('hyperliquid_coin') or row.get('symbol')}",
                "priority": "P0" if row.get("asset_family") == "rwa_or_traditional" else "P1",
                "category": "hyperliquid_tradeable_feed_discovery",
                "asset_class": row.get("asset_class"),
                "symbol": row.get("symbol"),
                "asset_id": row.get("asset_id"),
                "venue": venue,
                "job_type": hint["job_type"],
                "source_mode": hint["source_mode"],
                "endpoint_hint": hint["endpoint_hint"],
                "status": _job_status(venue, metadata),
                "requires_auth": bool((metadata or {}).get("requires_auth")),
                "adapter_implementation": (metadata or {}).get("implementation"),
                "target_status": row.get("coverage_delta"),
                "missing_source_types": [],
                "metadata": {
                    **row_metadata,
                    "asset_family": row.get("asset_family"),
                    "coverage_status": row.get("coverage_status"),
                    "coverage_delta": row.get("coverage_delta"),
                },
            }
        )
    return jobs


def _derivative_venue_sourcing_jobs(adapter_metadata: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    venue_rows = {
        str(row.get("venue_id")): row
        for row in load_derivative_venue_rows()
        if row.get("venue_id")
    }
    jobs = []
    for row in load_derivative_coverage_rows():
        venue = str(row.get("venue") or "")
        if not venue:
            continue
        metadata = adapter_metadata.get(venue)
        hint = ENDPOINT_HINTS.get(
            venue,
            {
                "job_type": "derivative_market_data_probe",
                "endpoint_hint": "Probe venue market catalog, L2 book, trades, mark/index, funding, and contract specs",
                "source_mode": "derivative_market_data_with_fair_value_adjustment",
            },
        )
        venue_status = venue_rows.get(venue, {})
        row_metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        sourceable = bool(venue_status.get("sourceable_now"))
        jobs.append(
            {
                "job_id": f"derivative:{venue}:{row.get('asset_id')}:{row_metadata.get('venue_market_id') or row.get('symbol')}",
                "priority": "P0" if row.get("asset_family") == "rwa_or_traditional" else "P1",
                "category": "derivative_venue_feed_discovery",
                "asset_class": row.get("asset_class"),
                "symbol": row.get("symbol"),
                "asset_id": row.get("asset_id"),
                "venue": venue,
                "job_type": hint["job_type"],
                "source_mode": hint["source_mode"],
                "endpoint_hint": hint["endpoint_hint"],
                "status": "ready_to_probe" if sourceable else _job_status(venue, metadata),
                "requires_auth": bool((metadata or {}).get("requires_auth") or venue_status.get("requires_auth")),
                "adapter_implementation": (metadata or {}).get("implementation"),
                "target_status": row.get("coverage_status"),
                "missing_source_types": [],
                "metadata": {
                    **row_metadata,
                    "asset_family": row.get("asset_family"),
                    "derivative_policy": "basis_funding_carry_adjustment_required_before_spot_use",
                    "venue_discovery_status": venue_status.get("discovery_status"),
                    "sourceable_now": sourceable,
                },
            }
        )
    for venue, venue_status in venue_rows.items():
        if venue_status.get("sourceable_now") or int(venue_status.get("market_row_count") or 0) > 0:
            continue
        hint = ENDPOINT_HINTS.get(
            venue,
            {
                "job_type": "derivative_access_or_catalog_probe",
                "endpoint_hint": "Confirm public API, subgraph, RPC, or partner access",
                "source_mode": "access_discovery",
            },
        )
        jobs.append(
            {
                "job_id": f"derivative_access:{venue}",
                "priority": "P2",
                "category": "derivative_venue_access_blocker",
                "asset_class": "unknown",
                "symbol": "catalog",
                "asset_id": "catalog",
                "venue": venue,
                "job_type": hint["job_type"],
                "source_mode": hint["source_mode"],
                "endpoint_hint": hint["endpoint_hint"],
                "status": "blocked_by_auth_or_license" if venue_status.get("requires_auth") else "planned_adapter",
                "requires_auth": bool(venue_status.get("requires_auth")),
                "adapter_implementation": (adapter_metadata.get(venue) or {}).get("implementation"),
                "target_status": venue_status.get("discovery_status"),
                "missing_source_types": ["market_catalog", "venue_market_id", "replayable_payload"],
                "metadata": {
                    "next_action": venue_status.get("next_action"),
                    "access_model": venue_status.get("access_model"),
                    "coverage_mode": venue_status.get("coverage_mode"),
                },
            }
        )
    return jobs


def _rwa_xyz_monitor_sourcing_jobs(adapter_metadata: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    metadata = adapter_metadata.get(RWA_XYZ_VENUE_ID)
    hint = ENDPOINT_HINTS[RWA_XYZ_VENUE_ID]
    jobs = []
    seen_contracts: set[str] = set()
    for row in load_rwa_xyz_token_rows():
        contract_key = rwa_xyz_token_contract_key(row)
        if contract_key in seen_contracts:
            continue
        seen_contracts.add(contract_key)
        address = str(row.get("address") or "")
        network = str(row.get("network") or "unknown")
        platform = str(row.get("platform") or "unknown")
        asset_class = str(row.get("asset_class") or "tokenized_fund")
        action = build_rwa_xyz_token_action(row)
        priority = rwa_xyz_token_priority(row)
        jobs.append(
            {
                "job_id": f"rwa_xyz_token:{contract_key}",
                "priority": priority,
                "category": "rwa_xyz_monitor_token_realtime_discovery",
                "sourcing_lane": action["lane"],
                "asset_class": asset_class,
                "symbol": row.get("symbol"),
                "asset_id": row.get("asset_id"),
                "venue": RWA_XYZ_VENUE_ID,
                "job_type": hint["job_type"],
                "source_mode": hint["source_mode"],
                "endpoint_hint": hint["endpoint_hint"],
                "status": "ready_to_probe" if address else "missing_identifier_mapping",
                "requires_auth": bool((metadata or {}).get("requires_auth")),
                "adapter_implementation": (metadata or {}).get("implementation") or (metadata or {}).get("adapter_lane"),
                "target_status": "catalog_token_identity_ready_realtime_price_discovery_required",
                "missing_source_types": [
                    "token_identity_and_decimals",
                    "pool_or_route_liquidity",
                    "fee_tiers_and_slot_or_block_state",
                    "replayable_raw_payloads",
                    "issuer_nav_or_primary_market_alignment",
                    "realtime_price_observation",
                    "blocksize_benchmark_alignment",
                    "manipulation_and_concentration_checks",
                ],
                "metadata": {
                    "rwa_xyz_asset_id": row.get("rwa_xyz_asset_id"),
                    "rwa_xyz_token_id": row.get("rwa_xyz_token_id"),
                    "rwa_xyz_ticker": row.get("rwa_xyz_ticker"),
                    "asset_name": row.get("asset_name"),
                    "issuer_name": row.get("issuer_name"),
                    "platform": platform,
                    "platform_slug": row.get("platform_slug"),
                    "network": network,
                    "network_slug": row.get("network_slug"),
                    "address": address,
                    "contract_identity": contract_key,
                    "standards": row.get("standards"),
                    "tokenization_type": row.get("tokenization_type"),
                    "issuance_type": row.get("issuance_type"),
                    "identity_mapping_status": row.get("identity_mapping_status"),
                    "canonical_underlying_candidate": row.get("canonical_underlying_candidate"),
                    "next_action": action["next_action"],
                    "production_eligible": False,
                    "allowed_feed_semantics": ["supplemental_catalog_coverage"],
                    "prohibited_feed_semantics": ["vwap", "bid_ask", "consensus"],
                    "production_boundary": "catalog row only; do not promote to VWAP, bid/ask, or consensus until token identity, executable venue, replay, liquidity, freshness, manipulation, issuer NAV alignment, Blocksize benchmark alignment, and data-rights gates pass",
                },
            }
        )
    return jobs


def build_sourcing_jobs(
    *,
    include_completed_targets: bool = False,
) -> dict[str, Any]:
    """Build executable sourcing jobs from the oracle parity gaps."""
    parity = build_oracle_parity_matrix()
    adapter_metadata = _adapter_metadata()
    venue_symbols = _venue_symbol_lookup()
    jobs: list[dict[str, Any]] = []
    for category in parity["categories"]:
        for target in category["targets"]:
            if target["status"] == "covered" and not include_completed_targets:
                continue
            if include_completed_targets:
                venue_candidates = sorted(
                    set(target["venue_gap"])
                    | (set(target.get("needed_venues") or category["needed_venues"]) & set(target["current_venues"]))
                )
            else:
                venue_candidates = target["venue_gap"] or category["needed_venues"]
            for venue in venue_candidates:
                metadata = adapter_metadata.get(venue)
                hint = ENDPOINT_HINTS.get(
                    venue,
                    {
                        "job_type": "unknown",
                        "endpoint_hint": "No endpoint hint registered",
                        "source_mode": "unknown",
                    },
                )
                jobs.append(
                    {
                        "job_id": f"{category['category']}:{target['asset_id']}:{venue}",
                        "priority": category["priority"],
                        "category": category["category"],
                        "asset_class": category["asset_class"],
                        "symbol": venue_symbols.get(
                            (str(target["asset_id"]).upper(), str(venue)),
                            target["symbol"],
                        ),
                        "asset_id": target["asset_id"],
                        "venue": venue,
                        "job_type": hint["job_type"],
                        "source_mode": hint["source_mode"],
                        "endpoint_hint": hint["endpoint_hint"],
                        "status": _job_status(venue, metadata),
                        "requires_auth": bool((metadata or {}).get("requires_auth")),
                        "adapter_implementation": (metadata or {}).get("implementation"),
                        "target_status": target["status"],
                        "missing_source_types": target["source_type_gap"],
                    }
                )
    jobs.extend(_hyperliquid_spot_sourcing_jobs(adapter_metadata))
    jobs.extend(_hyperliquid_tradeable_sourcing_jobs(adapter_metadata))
    jobs.extend(_derivative_venue_sourcing_jobs(adapter_metadata))
    jobs.extend(_rwa_xyz_monitor_sourcing_jobs(adapter_metadata))
    jobs.sort(
        key=lambda job: (
            str(job["priority"]),
            str(job["status"]),
            str(job["category"]),
            str(job["asset_id"]),
            str(job["venue"]),
        )
    )
    by_status = Counter(str(job["status"]) for job in jobs)
    by_venue = Counter(str(job["venue"]) for job in jobs)
    by_category = Counter(str(job["category"]) for job in jobs)
    return {
        "summary": {
            "job_count": len(jobs),
            "by_status": dict(sorted(by_status.items())),
            "by_venue": dict(sorted(by_venue.items())),
            "by_category": dict(sorted(by_category.items())),
            "ready_to_probe": by_status.get("ready_to_probe", 0),
            "blocked_by_auth_or_license": by_status.get("blocked_by_auth_or_license", 0),
            "planned_adapter": by_status.get("planned_adapter", 0),
        },
        "jobs": jobs,
        "next_execution_order": [
            "Run ready_to_probe jobs first, currently Kraken xStocks seed/parity symbols plus Hyperliquid PAXG and RWA spot l2Book candidates.",
            "Stand up DEX token/pool allowlists and API keys for Jupiter, Uniswap/The Graph, and Balancer where needed.",
            "Implement Solana pool-state adapters for Raydium, Orca, and Meteora before treating their DEX rows as real-time.",
            "Open auth/licensing paths for Polygon/TradFi benchmark, Pyth, Chainlink, Ondo, Jupiter, treasury NAV, and Bybit.",
            "Run derivative venue probes for Aster, Lighter, dYdX, Orderly, Aevo, ApeX Omni, Derive, Pendle, and Drift constants; capture contract specs, funding, mark/index, and basis inputs before using them for spot fair value.",
            "Implement planned non-auth adapters for Ostium, Gains, Curve, and Aerodrome.",
            "Persist raw payload hashes and normalized observations before promoting any source.",
        ],
    }
