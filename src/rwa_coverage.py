"""RWA market-data coverage and build-plan metadata.

The catalog is intentionally conservative: documented or live-checked symbols
are explicit, while dynamic exchange catalogs are represented as adapter work.
"""

from __future__ import annotations

from collections import defaultdict
from copy import deepcopy
from typing import Any

from src.rwa_derivative_venues import (
    DERIVATIVE_VENUE_DESCRIPTORS,
    load_derivative_coverage_rows,
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
from src.rwa_xyz_monitor import (
    RWA_XYZ_VENUE_DESCRIPTOR,
    load_rwa_xyz_coverage_rows,
)


FEASIBILITY_REPORT_PATH = "reports/rwa_market_data_feasibility.md"

ORACLE_PARITY_SOURCES: dict[str, Any] = {
    "pyth": {
        "source_url": "https://www.pyth.network/price-feeds",
        "coverage_note": (
            "Pyth advertises coverage across commodities, crypto, crypto indices, "
            "crypto redemption rates, economic data, equities, FX, metals, NAV, and rates."
        ),
        "performance_note": "Free tier: 10s update frequency. Pro tier: up to 1ms update frequency.",
    },
    "chainlink": {
        "source_url": "https://data.chain.link/feeds",
        "coverage_note": (
            "Chainlink feed categories include commodity, equity, ETF, fiat, fixed income, index, "
            "macroeconomics, NAV, proof of reserve, tokenized assets, tokenized funds, tokenized "
            "treasury funds, and U.S. Treasuries."
        ),
        "performance_note": "Chainlink feeds expose heartbeat/deviation metadata by feed and network.",
    },
}

ORACLE_PARITY_TARGETS: dict[str, dict[str, Any]] = {
    "equity": {
        "asset_class": "equity",
        "priority": "P0",
        "target_symbols": ["AAPL/USD", "AMZN/USD", "MSFT/USD", "NVDA/USD", "TSLA/USD", "META/USD"],
        "required_source_types": ["native_l2", "synthetic_depth", "price_stream_no_book", "quote_sweep", "onchain_clmm_pool", "benchmark_reference"],
        "needed_venues": ["kraken_xstocks", "ostium", "gains", "jupiter_router", "meteora_dlmm", "polygon_tradfi_reference"],
    },
    "etf": {
        "asset_class": "etf",
        "priority": "P0",
        "target_symbols": ["SPY/USD", "QQQ/USD", "VOO/USD", "TLT/USD", "HYG/USD", "SGOV/USD"],
        "required_source_types": ["native_l2", "synthetic_depth", "price_stream_no_book", "quote_sweep", "onchain_clmm_pool", "benchmark_reference"],
        "needed_venues": ["kraken_xstocks", "ostium", "gains", "jupiter_router", "meteora_dlmm", "polygon_tradfi_reference"],
    },
    "fx": {
        "asset_class": "fx",
        "priority": "P0",
        "target_symbols": ["EUR/USD", "GBP/USD", "AUD/USD", "USD/JPY", "USD/CAD", "USD/CHF", "USD/CNH", "USD/MXN", "USD/KRW"],
        "required_source_types": ["synthetic_depth", "price_stream_no_book", "onchain_stableswap_pool", "benchmark_reference"],
        "needed_venues": ["ostium", "gains", "curve_stableswap", "aerodrome_slipstream", "polygon_tradfi_reference"],
    },
    "metals": {
        "asset_class": "commodity",
        "priority": "P0",
        "target_symbols": ["XAU/USD", "XAG/USD", "XPD/USD", "XPT/USD", "PAXG/USD"],
        "required_source_types": ["native_l2", "synthetic_depth", "price_stream_no_book", "onchain_clmm_pool", "benchmark_reference"],
        "needed_venues": ["ostium", "gains", "uniswap_v3_v4", "balancer_pools", "polygon_tradfi_reference"],
        "target_venue_overrides": {
            "PAXG/USD": ["hyperliquid_paxg", "uniswap_v3_v4", "balancer_pools", "polygon_tradfi_reference"],
        },
    },
    "energy": {
        "asset_class": "commodity",
        "priority": "P1",
        "target_symbols": ["WTI/USD", "BRENT/USD", "USOILSPOT/USD", "UKOILSPOT/USD"],
        "required_source_types": ["synthetic_depth", "price_stream_no_book", "benchmark_reference"],
        "needed_venues": ["ostium", "gains", "polygon_tradfi_reference"],
    },
    "rates": {
        "asset_class": "rate",
        "priority": "P1",
        "target_symbols": ["US2Y", "US10Y", "US30Y"],
        "required_source_types": ["benchmark_reference", "oracle_reference"],
        "needed_venues": ["pyth_oracle_reference", "chainlink_oracle_reference", "polygon_tradfi_reference"],
    },
    "macro": {
        "asset_class": "macro",
        "priority": "P2",
        "target_symbols": ["GDP", "WAGEGROWTH", "CPIINDEX"],
        "required_source_types": ["macro_reference", "oracle_reference"],
        "needed_venues": ["pyth_oracle_reference", "chainlink_oracle_reference"],
    },
    "nav": {
        "asset_class": "treasury_fund",
        "priority": "P1",
        "target_symbols": ["ACRED/USD", "OUSG", "USDY", "TBILL", "USTB"],
        "required_source_types": ["nav_reference", "oracle_reference", "onchain_stableswap_pool"],
        "needed_venues": ["treasury_nav", "curve_stableswap", "balancer_pools", "chainlink_oracle_reference", "pyth_oracle_reference"],
    },
    "proof_of_reserve": {
        "asset_class": "reserve",
        "priority": "P2",
        "target_symbols": ["stablecoin_reserves", "wrapped_asset_reserves", "tokenized_fund_reserves"],
        "required_source_types": ["proof_of_reserve", "issuer_reference", "oracle_reference"],
        "needed_venues": ["chainlink_oracle_reference", "backed_xstocks_issuer", "treasury_nav"],
    },
}

BLOCK_SIZES_USD: dict[str, list[int]] = {
    "crypto": [1_000, 5_000, 10_000, 25_000, 100_000, 500_000, 1_000_000],
    "tokenized_equity": [1_000, 5_000, 10_000, 25_000, 50_000, 100_000],
    "tokenized_etf": [5_000, 25_000, 100_000, 250_000, 500_000],
    "synthetic_equity": [1_000, 5_000, 10_000, 25_000, 50_000],
    "synthetic_etf_index": [5_000, 25_000, 100_000, 250_000, 500_000],
    "fx": [10_000, 50_000, 100_000, 500_000, 1_000_000],
    "metal_commodity": [5_000, 25_000, 100_000, 250_000],
    "treasury_nav": [10_000, 100_000, 500_000, 1_000_000],
}

QUALITY_ALIGNMENT: dict[str, Any] = {
    "target_shape": {
        "vwap": {
            "symbol": "AAPLUSD",
            "venue": "kraken_xstocks",
            "block_size_usd": 10_000,
            "vwap": 0.0,
            "fillable_notional_usd": 0.0,
            "slippage_bps": 0.0,
            "source_type": "l2_book|trade_prints|quote_sweep|onchain_clmm_pool|onchain_stableswap_pool|synthetic_depth|nav_reference",
            "timestamp": "ISO-8601",
        },
        "bidask": {
            "symbol": "AAPLUSD",
            "venue": "kraken_xstocks",
            "bid": 0.0,
            "ask": 0.0,
            "mid": 0.0,
            "spread_bps": 0.0,
            "source_type": "native_l1|synthetic_l1|quote_response|onchain_pool_mid|nav_reference",
            "timestamp": "ISO-8601",
        },
    },
    "quality_score_weights": {
        "freshness": 0.25,
        "spread": 0.20,
        "depth_fill": 0.20,
        "cross_venue_agreement": 0.20,
        "source_tier": 0.15,
    },
    "outlier_detection": {
        "primary_method": "median_absolute_deviation",
        "fallback_method": "benchmark_basis_bps",
        "minimum_independent_sources_for_consensus": 2,
        "actions": [
            "exclude stale real-time observations from consolidated VWAP",
            "downgrade wide-spread observations but keep them visible",
            "return partial-fill VWAP when block size cannot be filled",
            "flag source-type mismatches instead of blending true L2 with NAV or synthetic depth",
            "require manual promotion before replacing any existing regulated vendor feed",
        ],
    },
    "thresholds": {
        "max_age_ms": {
        "tokenized_equity": 10_000,
        "tokenized_etf": 10_000,
        "synthetic_equity": 15_000,
        "synthetic_etf_index": 15_000,
        "fx": 10_000,
        "metal_commodity": 10_000,
        "treasury_nav": 86_400_000,
        "treasury_fund": 86_400_000,
        "tokenized_fund": 86_400_000,
        },
        "max_spread_bps": {
            "tokenized_equity": 75,
            "tokenized_etf": 50,
        "synthetic_equity": 100,
        "synthetic_etf_index": 75,
        "fx": 10,
        "metal_commodity": 50,
        "treasury_nav": 25,
        "treasury_fund": 25,
        "tokenized_fund": 50,
        },
        "benchmark_drift_bps": {
            "warning": 50,
            "exclude": 150,
            "treasury_nav_warning": 10,
        },
    },
}

DEX_QUALITY_REQUIREMENTS: dict[str, Any] = {
    "minimum_fields": [
        "chain_id",
        "pool_or_route_id",
        "base_mint_or_token",
        "quote_mint_or_token",
        "slot_or_block_number",
        "timestamp",
        "liquidity_usd",
        "price",
        "source_type",
    ],
    "source_type_rules": {
        "quote_sweep": "Executable router quote by notional size; must include route plan, price impact, and context slot/block.",
        "onchain_clmm_pool": "Pool-state derived concentrated-liquidity price/depth; must include tick/bin/liquidity state and block or slot.",
        "onchain_stableswap_pool": "Pool-state derived stable-curve price/depth; only suitable for stablecoin/NAV-like assets after imbalance checks.",
        "dex_indexer_reference": "Indexed swaps/pools for analytics and backfill; not sufficient alone for tick-by-tick consolidation.",
    },
    "promotion_gates": [
        "pool allowlist with token-contract verification",
        "minimum liquidity and minimum 24h organic volume thresholds",
        "stale slot/block detection",
        "route or pool price-impact ceiling by block size",
        "cross-check against Blocksize/Pyth/Chainlink or regulated benchmark where available",
        "sandwich/manipulation and single-pool concentration flags",
        "replayable raw route or pool-state payload",
    ],
    "initial_quality_tier": {
        "jupiter_router": "supplemental_quote_route_after_api_key",
        "raydium_clmm": "planned_pool_state_source",
        "orca_whirlpool": "planned_pool_state_source",
        "meteora_dlmm": "planned_pool_state_source",
        "uniswap_v3_v4": "planned_indexed_pool_source",
        "curve_stableswap": "planned_stableswap_source",
        "balancer_pools": "planned_weighted_or_stable_pool_source",
        "aerodrome_slipstream": "planned_base_clmm_source",
    },
}

VENUES: list[dict[str, Any]] = [
    {
        "id": "kraken_xstocks",
        "name": "Kraken xStocks",
        "priority": 1,
        "status": "first_wave",
        "instrument_type": "tokenized_spot",
        "source_tier": "native_l2",
        "data": ["l1_bid_ask", "l2_order_book", "trades", "ohlc", "ticker"],
        "vwap_method": "walk native L2 book by block size; use trades for print VWAP",
        "bidask_method": "native ticker or top of book",
        "coverage_mode": "dynamic_product_catalog_plus_targeted_endpoint_check",
        "legal_note": "Tokenized securities restrictions apply; do not market as official consolidated equity data.",
    },
    {
        "id": "ostium",
        "name": "Ostium",
        "priority": 2,
        "status": "first_wave",
        "instrument_type": "synthetic_perp",
        "source_tier": "synthetic_depth",
        "data": ["bid", "mid", "ask", "candles", "fills", "simulated_orderbook", "simulated_slippage"],
        "vwap_method": "use simulated orderbook and slippage endpoints; label depth as synthetic",
        "bidask_method": "builder API bid/mid/ask",
        "coverage_mode": "documented_market_list",
        "legal_note": "Restricted jurisdictions include the United States; gate production usage accordingly.",
    },
    {
        "id": "gains",
        "name": "Gains gTrade",
        "priority": 3,
        "status": "first_wave",
        "instrument_type": "synthetic_leveraged_market",
        "source_tier": "price_stream_no_book",
        "data": ["price_stream", "recent_trades", "stats", "current_ohlc"],
        "vwap_method": "trade VWAP only; mark depth VWAP unsupported unless price-impact parameters are integrated",
        "bidask_method": "not a true book; use price stream as synthetic mark/reference",
        "coverage_mode": "documented_pair_list",
        "legal_note": "Supplemental benchmark only; no lit order book.",
    },
    {
        "id": "jupiter_xstocks",
        "name": "Jupiter xStocks routes",
        "priority": 4,
        "status": "phase_two",
        "instrument_type": "onchain_quote_route",
        "source_tier": "quote_sweep",
        "data": ["quote", "route_plan", "price_impact", "swap_metadata"],
        "vwap_method": "sweep quote sizes and record route plan/price impact",
        "bidask_method": "quote response by side and size, not native book",
        "coverage_mode": "dynamic_token_catalog_and_quote_probe",
        "legal_note": "Quote API may require keying; liquidity is fragmented across routes.",
    },
    {
        "id": "jupiter_router",
        "name": "Jupiter Solana router",
        "priority": 5,
        "status": "dex_expansion",
        "instrument_type": "onchain_quote_router",
        "source_tier": "quote_sweep",
        "data": ["quote", "route_plan", "price_impact", "context_slot", "dex_labels"],
        "vwap_method": "sweep quote sizes through Jupiter and preserve routePlan, contextSlot, and priceImpactPct",
        "bidask_method": "derive side-specific executable quotes by direction and size; not a lit book",
        "coverage_mode": "api_keyed_dynamic_router",
        "legal_note": "Use as executable Solana route evidence; route quality depends on token allowlists, liquidity, and API access.",
    },
    {
        "id": "raydium_clmm",
        "name": "Raydium CLMM / CPMM",
        "priority": 6,
        "status": "dex_expansion",
        "instrument_type": "onchain_pool",
        "source_tier": "onchain_clmm_pool",
        "data": ["pool_state", "pool_liquidity", "token_info", "swap_routing", "grpc_monitoring"],
        "vwap_method": "derive pool-state depth or use SDK/gRPC for real-time pool monitoring; do not rely on REST for tick-by-tick",
        "bidask_method": "pool-implied side quotes by size after liquidity and imbalance checks",
        "coverage_mode": "dynamic_pool_catalog_and_sdk_probe",
        "legal_note": "DEX pool data is executable context, not consolidated market data; filter manipulated or shallow pools.",
    },
    {
        "id": "orca_whirlpool",
        "name": "Orca Whirlpool",
        "priority": 7,
        "status": "dex_expansion",
        "instrument_type": "onchain_pool",
        "source_tier": "onchain_clmm_pool",
        "data": ["whirlpool_state", "ticks", "liquidity", "swap_quote", "position_data"],
        "vwap_method": "derive concentrated-liquidity depth from Whirlpool tick state or SDK quote simulation",
        "bidask_method": "pool-implied side quotes by size after liquidity and stale-slot checks",
        "coverage_mode": "dynamic_pool_catalog_and_sdk_probe",
        "legal_note": "Use only allowlisted pools with verified token mints and adequate liquidity.",
    },
    {
        "id": "meteora_dlmm",
        "name": "Meteora DLMM",
        "priority": 8,
        "status": "dex_expansion",
        "instrument_type": "onchain_pool",
        "source_tier": "onchain_clmm_pool",
        "data": ["dlmm_bins", "pool_state", "liquidity", "dynamic_fees", "swap_quote"],
        "vwap_method": "walk DLMM bins or SDK quote simulation by notional and record bin/liquidity state",
        "bidask_method": "pool-implied side quotes by size with dynamic-fee and bin-state provenance",
        "coverage_mode": "dynamic_pool_catalog_and_sdk_probe",
        "legal_note": "Token-2022 support is useful for tokenized assets, but pool allowlists and transfer-hook checks are required.",
    },
    {
        "id": "uniswap_v3_v4",
        "name": "Uniswap v3/v4 pools",
        "priority": 9,
        "status": "dex_expansion",
        "instrument_type": "onchain_pool_indexed",
        "source_tier": "onchain_clmm_pool",
        "data": ["pool_state", "ticks", "swaps", "subgraph", "the_graph_indexing_status"],
        "vwap_method": "derive pool-state depth from ticks and use subgraphs for backfill/indexed swaps",
        "bidask_method": "pool-implied side quotes by size after indexer and block freshness checks",
        "coverage_mode": "api_keyed_subgraph_plus_rpc_probe",
        "legal_note": "Public subgraph deployments must be verified for indexing health; production should manage own subgraph/RPC.",
    },
    {
        "id": "curve_stableswap",
        "name": "Curve StableSwap",
        "priority": 10,
        "status": "dex_expansion",
        "instrument_type": "onchain_stableswap_pool",
        "source_tier": "onchain_stableswap_pool",
        "data": ["pool_state", "virtual_price", "balances", "swaps", "gauge_metadata"],
        "vwap_method": "simulate stable-curve swap sizes and reject imbalanced or stale pools",
        "bidask_method": "pool-implied side quotes for stablecoin, FX proxy, and NAV-like pairs only",
        "coverage_mode": "dynamic_pool_catalog_and_rpc_probe",
        "legal_note": "High quality for stable/NAV assets when pools are deep and balanced; not an equity price source.",
    },
    {
        "id": "balancer_pools",
        "name": "Balancer weighted/stable pools",
        "priority": 11,
        "status": "dex_expansion",
        "instrument_type": "onchain_weighted_or_stable_pool",
        "source_tier": "onchain_stableswap_pool",
        "data": ["pool_state", "balances", "weights", "stable_pool_state", "swaps"],
        "vwap_method": "simulate weighted/stable pool swaps by notional with pool balance provenance",
        "bidask_method": "pool-implied side quotes after concentration and stale-block checks",
        "coverage_mode": "api_keyed_subgraph_plus_rpc_probe",
        "legal_note": "Useful for tokenized funds and stable assets after pool allowlist and imbalance checks.",
    },
    {
        "id": "aerodrome_slipstream",
        "name": "Aerodrome Slipstream",
        "priority": 12,
        "status": "dex_expansion",
        "instrument_type": "base_onchain_pool",
        "source_tier": "onchain_clmm_pool",
        "data": ["pool_state", "ticks", "swaps", "route_quote"],
        "vwap_method": "derive Base CLMM depth or route quotes for tokenized assets and stable routes",
        "bidask_method": "pool-implied side quotes by size with Base block freshness checks",
        "coverage_mode": "dynamic_pool_catalog_and_rpc_probe",
        "legal_note": "Base liquidity can be strategic for tokenized funds/stables; keep as supplemental until benchmarked.",
    },
    {
        "id": "bybit_xstocks",
        "name": "Bybit xStocks",
        "priority": 13,
        "status": "phase_two",
        "instrument_type": "tokenized_spot",
        "source_tier": "native_l2",
        "data": ["l1_bid_ask", "l2_order_book", "recent_trades", "ticker"],
        "vwap_method": "walk native L2 book where symbols are listed",
        "bidask_method": "native ticker or top of book",
        "coverage_mode": "dynamic_instrument_fetch",
        "legal_note": "Public API access may be region blocked; confirm legal availability.",
    },
    {
        "id": "ondo_stocks",
        "name": "Ondo Stocks",
        "priority": 14,
        "status": "phase_two",
        "instrument_type": "tokenized_spot_api",
        "source_tier": "quote_stream",
        "data": ["quote", "price_stream", "ohlc", "mint_redeem_attestation"],
        "vwap_method": "quote-sweep by notional where API access permits; no native lit book",
        "bidask_method": "soft quote or real-time price stream, subject to whitelist/API access",
        "coverage_mode": "api_keyed_dynamic_catalog",
        "legal_note": "Whitelisted/non-US product surface; use as strategic tokenized-stock source only after access review.",
    },
    {
        "id": "hyperliquid_paxg",
        "name": "Hyperliquid PAXG",
        "priority": 15,
        "status": "phase_two",
        "instrument_type": "perp",
        "source_tier": "native_l2",
        "data": ["l2_order_book", "trades", "mark", "oracle"],
        "vwap_method": "walk native L2 book and compare mark/oracle divergence",
        "bidask_method": "native top of book",
        "coverage_mode": "single_documented_market",
        "legal_note": "Useful for gold-token overlap; not broad RWA coverage.",
    },
    {
        "id": HYPERLIQUID_RWA_SPOT_VENUE_ID,
        "name": "Hyperliquid RWA spot",
        "priority": 16,
        "status": "phase_two",
        "instrument_type": "tokenized_spot",
        "source_tier": "native_l2",
        "data": ["l1_bid_ask", "l2_order_book", "trades", "spot_meta", "ticker"],
        "vwap_method": "walk public l2Book levels by Hyperliquid @spot index; cap depth to available 20 levels per side",
        "bidask_method": "native top of book from public l2Book",
        "coverage_mode": "dynamic_spot_meta_plus_targeted_l2book_check",
        "legal_note": "Supplemental tokenized-asset liquidity only until identity, issuer, liquidity, and benchmark checks pass.",
    },
    {
        "id": HYPERLIQUID_PERPS_VENUE_ID,
        "name": "Hyperliquid perps",
        "priority": 16,
        "status": "phase_two",
        "instrument_type": "perp",
        "source_tier": "native_l2",
        "data": ["l1_bid_ask", "l2_order_book", "trades", "funding", "mark", "oracle", "meta"],
        "vwap_method": "walk native L2 book by coin and compare mark/oracle/funding context before use",
        "bidask_method": "native top of book from public l2Book",
        "coverage_mode": "dynamic_meta_tradeable_universe",
        "legal_note": "Crypto perp liquidity is sourceable, but replacement use still needs freshness, depth, and manipulation checks.",
    },
    {
        "id": HYPERLIQUID_SPOT_VENUE_ID,
        "name": "Hyperliquid spot",
        "priority": 16,
        "status": "phase_two",
        "instrument_type": "spot",
        "source_tier": "native_l2",
        "data": ["l1_bid_ask", "l2_order_book", "trades", "spot_meta", "token_metadata"],
        "vwap_method": "walk public l2Book levels by @spot index; retain token and pair metadata from spotMeta",
        "bidask_method": "native top of book from public l2Book",
        "coverage_mode": "dynamic_spot_meta_tradeable_universe",
        "legal_note": "Crypto spot rows are sourceable candidates; RWA-like spot rows require issuer and identity validation.",
    },
    {
        "id": "treasury_nav",
        "name": "Tokenized Treasury NAV sources",
        "priority": 17,
        "status": "benchmark_only",
        "instrument_type": "nav_reference",
        "source_tier": "nav_reference",
        "data": ["nav", "redemption_quote", "yield", "attestation"],
        "vwap_method": "not VWAP-suitable except where secondary DEX pools are integrated",
        "bidask_method": "issuer NAV or soft quote only",
        "coverage_mode": "issuer_api_or_attestation",
        "legal_note": "Reference/NAV mode only; avoid presenting as real-time rates.",
    },
    {
        "id": "backed_xstocks_issuer",
        "name": "Backed / xStocks issuer metadata",
        "priority": 18,
        "status": "benchmark_only",
        "instrument_type": "issuer_reference",
        "source_tier": "issuer_reference",
        "data": ["product_catalog", "issuer_metadata", "attestation", "token_contracts"],
        "vwap_method": "not a trading VWAP source",
        "bidask_method": "not a bid/ask source",
        "coverage_mode": "issuer_catalog_and_attestation",
        "legal_note": "Use to validate product mapping, token contracts, and issuer metadata, not as market microstructure.",
    },
    {
        "id": "polygon_tradfi_reference",
        "name": "Polygon.io / TradFi benchmark reference",
        "priority": 19,
        "status": "benchmark_only",
        "instrument_type": "regulated_market_data_reference",
        "source_tier": "benchmark_reference",
        "data": ["nbbo", "trades", "ohlc", "reference_data", "corporate_actions"],
        "vwap_method": "benchmark trade/NBBO VWAP for quality alignment, subject to licensing",
        "bidask_method": "benchmark NBBO/reference bid/ask, subject to licensing",
        "coverage_mode": "licensed_dynamic_catalog",
        "legal_note": "Use as regulated benchmark/comparison feed; redistribution depends on vendor license.",
    },
    {
        "id": "us_equity_consolidated_tape",
        "name": "U.S. consolidated equity feed",
        "priority": 20,
        "status": "licensed_expansion",
        "instrument_type": "listed_equity_consolidated_feed",
        "source_tier": "licensed_consolidated_tape",
        "data": ["tick_trades", "nbbo", "last_quote", "snapshots", "corporate_actions", "constituents"],
        "vwap_method": "construct trade VWAP from eligible exchange/SIP trades or vendor aggregate bars",
        "bidask_method": "NBBO or direct-feed best bid/ask, subject to vendor license",
        "coverage_mode": "licensed_dynamic_constituent_catalog",
        "legal_note": "Required for full S&P 500 coverage; redistribution and display use depend on market-data license.",
    },
    {
        "id": "hkex_licensed_equities",
        "name": "HKEX licensed equities",
        "priority": 21,
        "status": "licensed_expansion",
        "instrument_type": "listed_equity_exchange_feed",
        "source_tier": "licensed_exchange_feed",
        "data": ["real_time_quotes", "trades", "market_depth", "securities_master", "corporate_actions"],
        "vwap_method": "construct trade VWAP from HKEX trade feed or licensed vendor aggregates",
        "bidask_method": "HKEX best bid/ask or market-depth top of book",
        "coverage_mode": "licensed_dynamic_exchange_catalog",
        "legal_note": "HKEX real-time data requires market-data agreement and redistribution controls.",
    },
    {
        "id": "china_a_share_licensed_equities",
        "name": "China A-share licensed equities",
        "priority": 22,
        "status": "licensed_expansion",
        "instrument_type": "listed_equity_exchange_feed",
        "source_tier": "licensed_exchange_feed",
        "data": ["real_time_quotes", "trades", "market_depth", "securities_master", "northbound_connect_mapping"],
        "vwap_method": "construct trade VWAP from SSE/SZSE/China Connect licensed trade feeds",
        "bidask_method": "licensed exchange best bid/ask or market-depth top of book",
        "coverage_mode": "licensed_dynamic_exchange_catalog",
        "legal_note": "Mainland China market data and China Connect redistribution require licensed vendor/exchange terms.",
    },
    {
        "id": "krx_licensed_equities",
        "name": "KRX licensed equities",
        "priority": 23,
        "status": "licensed_expansion",
        "instrument_type": "listed_equity_exchange_feed",
        "source_tier": "licensed_exchange_feed",
        "data": ["real_time_quotes", "trades", "market_depth", "securities_master", "kospi_kosdaq_constituents"],
        "vwap_method": "construct trade VWAP from KRX trade feed or licensed vendor aggregates",
        "bidask_method": "KRX best bid/ask or market-depth top of book",
        "coverage_mode": "licensed_dynamic_exchange_catalog",
        "legal_note": "KRX real-time data requires exchange/vendor license and redistribution controls.",
    },
    {
        "id": "jpx_licensed_equities",
        "name": "JPX licensed equities",
        "priority": 24,
        "status": "licensed_expansion",
        "instrument_type": "listed_equity_exchange_feed",
        "source_tier": "licensed_exchange_feed",
        "data": ["real_time_quotes", "trades", "market_depth", "securities_master", "corporate_actions"],
        "vwap_method": "construct trade VWAP from JPX/TSE trade feed or licensed vendor aggregates",
        "bidask_method": "JPX/TSE best bid/ask or market-depth top of book",
        "coverage_mode": "licensed_dynamic_exchange_catalog",
        "legal_note": "JPX/TSE real-time data requires exchange/vendor license and redistribution controls.",
    },
    {
        "id": "twse_licensed_equities",
        "name": "TWSE/TPEx licensed equities",
        "priority": 25,
        "status": "licensed_expansion",
        "instrument_type": "listed_equity_exchange_feed",
        "source_tier": "licensed_exchange_feed",
        "data": ["real_time_quotes", "trades", "market_depth", "securities_master", "corporate_actions"],
        "vwap_method": "construct trade VWAP from TWSE/TPEx trade feed or licensed vendor aggregates",
        "bidask_method": "TWSE/TPEx best bid/ask or market-depth top of book",
        "coverage_mode": "licensed_dynamic_exchange_catalog",
        "legal_note": "Taiwan exchange data requires licensed vendor/exchange terms and redistribution controls.",
    },
    {
        "id": "india_nse_bse_licensed_equities",
        "name": "India NSE/BSE licensed equities",
        "priority": 26,
        "status": "licensed_expansion",
        "instrument_type": "listed_equity_exchange_feed",
        "source_tier": "licensed_exchange_feed",
        "data": ["real_time_quotes", "trades", "market_depth", "securities_master", "corporate_actions"],
        "vwap_method": "construct trade VWAP from NSE/BSE trade feed or licensed vendor aggregates",
        "bidask_method": "NSE/BSE best bid/ask or market-depth top of book",
        "coverage_mode": "licensed_dynamic_exchange_catalog",
        "legal_note": "NSE/BSE real-time data requires exchange/vendor license and redistribution controls.",
    },
    {
        "id": "lse_lseg_licensed_equities",
        "name": "LSE/LSEG licensed equities",
        "priority": 27,
        "status": "licensed_expansion",
        "instrument_type": "listed_equity_exchange_feed",
        "source_tier": "licensed_exchange_feed",
        "data": ["real_time_quotes", "trades", "market_depth", "securities_master", "corporate_actions"],
        "vwap_method": "construct trade VWAP from LSE trade feed or LSEG/vendor aggregates",
        "bidask_method": "LSE best bid/ask or market-depth top of book",
        "coverage_mode": "licensed_dynamic_exchange_catalog",
        "legal_note": "UK/LSE real-time market data requires licensed terms and redistribution controls.",
    },
    {
        "id": "euronext_licensed_equities",
        "name": "Euronext licensed equities",
        "priority": 28,
        "status": "licensed_expansion",
        "instrument_type": "listed_equity_exchange_feed",
        "source_tier": "licensed_exchange_feed",
        "data": ["real_time_quotes", "trades", "market_depth", "securities_master", "corporate_actions"],
        "vwap_method": "construct trade VWAP from Euronext trade feed or licensed vendor aggregates",
        "bidask_method": "Euronext best bid/ask or market-depth top of book",
        "coverage_mode": "licensed_dynamic_exchange_catalog",
        "legal_note": "Euronext real-time data requires market-data license and redistribution controls.",
    },
    {
        "id": "deutsche_boerse_xetra_licensed_equities",
        "name": "Deutsche Boerse/Xetra licensed equities",
        "priority": 29,
        "status": "licensed_expansion",
        "instrument_type": "listed_equity_exchange_feed",
        "source_tier": "licensed_exchange_feed",
        "data": ["real_time_quotes", "trades", "market_depth", "securities_master", "corporate_actions"],
        "vwap_method": "construct trade VWAP from Xetra trade feed or licensed vendor aggregates",
        "bidask_method": "Xetra best bid/ask or market-depth top of book",
        "coverage_mode": "licensed_dynamic_exchange_catalog",
        "legal_note": "Deutsche Boerse/Xetra data requires licensed vendor/exchange terms.",
    },
    {
        "id": "tsx_licensed_equities",
        "name": "TSX/TSXV licensed equities",
        "priority": 30,
        "status": "licensed_expansion",
        "instrument_type": "listed_equity_exchange_feed",
        "source_tier": "licensed_exchange_feed",
        "data": ["real_time_quotes", "trades", "market_depth", "securities_master", "corporate_actions"],
        "vwap_method": "construct trade VWAP from TSX/TSXV trade feed or licensed vendor aggregates",
        "bidask_method": "TSX/TSXV best bid/ask or market-depth top of book",
        "coverage_mode": "licensed_dynamic_exchange_catalog",
        "legal_note": "Canadian exchange data requires licensed vendor/exchange terms and redistribution controls.",
    },
    {
        "id": "asx_licensed_equities",
        "name": "ASX licensed equities",
        "priority": 31,
        "status": "licensed_expansion",
        "instrument_type": "listed_equity_exchange_feed",
        "source_tier": "licensed_exchange_feed",
        "data": ["real_time_quotes", "trades", "market_depth", "securities_master", "corporate_actions"],
        "vwap_method": "construct trade VWAP from ASX trade feed or licensed vendor aggregates",
        "bidask_method": "ASX best bid/ask or market-depth top of book",
        "coverage_mode": "licensed_dynamic_exchange_catalog",
        "legal_note": "ASX real-time market data requires licensed terms and redistribution controls.",
    },
    {
        "id": "sgx_licensed_equities",
        "name": "SGX licensed equities",
        "priority": 32,
        "status": "licensed_expansion",
        "instrument_type": "listed_equity_exchange_feed",
        "source_tier": "licensed_exchange_feed",
        "data": ["real_time_quotes", "trades", "market_depth", "securities_master", "corporate_actions"],
        "vwap_method": "construct trade VWAP from SGX trade feed or licensed vendor aggregates",
        "bidask_method": "SGX best bid/ask or market-depth top of book",
        "coverage_mode": "licensed_dynamic_exchange_catalog",
        "legal_note": "SGX real-time market data requires licensed terms and redistribution controls.",
    },
    {
        "id": "pyth_oracle_reference",
        "name": "Pyth oracle reference",
        "priority": 33,
        "status": "benchmark_only",
        "instrument_type": "oracle_reference",
        "source_tier": "oracle_reference",
        "data": ["oracle_price", "confidence_interval", "asset_catalog", "rates", "macro", "nav"],
        "vwap_method": "not a trading VWAP source; use as parity target and oracle benchmark",
        "bidask_method": "not a bid/ask source; oracle price/confidence only",
        "coverage_mode": "oracle_catalog_and_price_feeds",
        "legal_note": "Use as oracle parity/benchmark reference subject to Pyth distributor terms.",
    },
    {
        "id": "chainlink_oracle_reference",
        "name": "Chainlink oracle reference",
        "priority": 34,
        "status": "benchmark_only",
        "instrument_type": "oracle_reference",
        "source_tier": "oracle_reference",
        "data": ["data_feeds", "data_streams", "heartbeat", "deviation_threshold", "proof_of_reserve", "nav"],
        "vwap_method": "not a trading VWAP source; use as parity target and oracle benchmark",
        "bidask_method": "not a bid/ask source; feed answer plus heartbeat/deviation metadata",
        "coverage_mode": "oracle_feed_catalog",
        "legal_note": "Use as oracle parity/benchmark reference subject to Chainlink/feed terms.",
    },
    {
        "id": "blocksize_state",
        "name": "Blocksize state reference",
        "priority": 35,
        "status": "benchmark_only",
        "instrument_type": "pool_state_reference",
        "source_tier": "benchmark_reference",
        "data": ["state_price", "state_instruments", "state_pool", "state_subscribe"],
        "vwap_method": "not a trading VWAP source; use state/reference price as supplemental consensus evidence",
        "bidask_method": "not executable bid/ask; use state price as a supplemental mid/reference only",
        "coverage_mode": "state_instruments_probe",
        "legal_note": "Blocksize state values are supplemental references and must not be treated as executable liquidity.",
    },
]

_EXISTING_VENUE_IDS = {str(venue["id"]) for venue in VENUES}
VENUES.extend(
    deepcopy(venue)
    for venue in DERIVATIVE_VENUE_DESCRIPTORS
    if str(venue["id"]) not in _EXISTING_VENUE_IDS
)
if str(RWA_XYZ_VENUE_DESCRIPTOR["id"]) not in {str(venue["id"]) for venue in VENUES}:
    VENUES.append(deepcopy(RWA_XYZ_VENUE_DESCRIPTOR))

OSTIUM_SYMBOLS: dict[str, list[str]] = {
    "equity": [
        "AAPL/USD", "AMD/USD", "AMZN/USD", "ARM/USD", "ASML/USD", "AVGO/USD", "BB/USD",
        "BMNR/USD", "CAT/USD", "COIN/USD", "COST/USD", "CRCL/USD", "CVX/USD", "GEV/USD",
        "GLXY/USD", "GOOG/USD", "HOOD/USD", "INTC/USD", "META/USD", "MP/USD", "MSFT/USD",
        "MSTR/USD", "MU/USD", "NFLX/USD", "NVDA/USD", "ORCL/USD", "PLTR/USD", "RIVN/USD",
        "SBET/USD", "SHEL/USD", "SMCI/USD", "SNDK/USD", "TSLA/USD", "TSM/USD", "XOM/USD",
    ],
    "etf": ["DRAM/USD", "HYG/USD", "KR2550/USD", "REMX/USD", "TLT/USD", "UNG/USD", "URA/USD", "XLE/USD"],
    "commodity": ["XAU/USD", "WTI/USD", "BRENT/USD", "XCU/USD", "XAG/USD", "XPT/USD", "XPD/USD"],
    "index": ["US500/USD", "US100/USD", "US30/USD", "GER40/EUR", "UK100/GBP", "JP225/JPY", "HK50/HKD"],
    "fx": ["AUD/USD", "EUR/USD", "GBP/USD", "NZD/USD", "USD/CAD", "USD/CHF", "USD/JPY", "USD/MXN", "USD/KRW"],
}

GAINS_SYMBOLS: dict[str, list[str]] = {
    "fx": ["EUR/USD", "USD/JPY", "GBP/USD", "USD/CAD", "USD/CNH", "USD/SGD", "EUR/AUD", "GBP/CAD", "GBP/JPY"],
    "equity": [
        "AAPL/USD", "MSFT/USD", "SNAP/USD", "NVDA/USD", "PYPL/USD", "MCD/USD", "META/USD",
        "GOOGL_1/USD", "GME_1/USD", "AMZN_1/USD", "TSLA_1/USD", "COIN/USD", "HOOD/USD",
        "MSTR/USD", "CRCL/USD", "PLTR/USD", "LMT/USD", "RIOT/USD", "MARA/USD", "NFLX_1/USD",
        "WPM/USD", "SPCX/USD", "AVGO/USD", "SNDK/USD", "MU/USD", "MRVL/USD", "SAMSUNG/USD",
        "SKHYNIX/USD", "BOT/USD", "BB/USD", "LPTH/USD", "ABCL/USD", "IOVA/USD", "BRUN/USD",
        "WYFI/USD", "SHAZ/USD", "BE/USD", "NBIS/USD", "CRWV/USD", "IREN/USD",
    ],
    "etf_index": ["SPY/USD", "QQQ/USD", "IWM/USD", "DIA/USD", "GDX/USD", "URA/USD", "URNM/USD"],
    "commodity": ["XAU/USD", "XAG/USD", "WTI/USD", "XPT/USD", "HG/USD"],
}

KRAKEN_XSTOCKS_EXAMPLES: dict[str, list[str]] = {
    "tokenized_equity": ["AAPLx/USD", "NVDAx/USD", "TSLAx/USD"],
    "tokenized_etf": ["SPYx/USD", "QQQx/USD", "VOOx/USD", "SGOVx/USD", "TBLLx/USD"],
}

DEX_RWA_SYMBOLS: dict[str, dict[str, list[str]]] = {
    "jupiter_router": {
        "equity": ["AAPLx/USD", "AMZNx/USD", "MSFTx/USD", "NVDAx/USD", "TSLAx/USD", "METAx/USD"],
        "etf": ["SPYx/USD", "QQQx/USD", "VOOx/USD", "SGOVx/USD", "TBLLx/USD"],
        "fx": ["EURC/USDC"],
        "treasury_fund": ["USDY/USDC", "OUSG/USDC"],
    },
    "raydium_clmm": {
        "equity": ["AAPLx/USD", "NVDAx/USD", "TSLAx/USD"],
        "etf": ["SPYx/USD", "QQQx/USD"],
        "fx": ["EURC/USDC"],
        "treasury_fund": ["USDY/USDC"],
    },
    "orca_whirlpool": {
        "equity": ["AAPLx/USD", "NVDAx/USD", "TSLAx/USD"],
        "etf": ["SPYx/USD", "QQQx/USD"],
        "fx": ["EURC/USDC"],
    },
    "meteora_dlmm": {
        "equity": ["AAPLx/USD", "AMZNx/USD", "NVDAx/USD", "TSLAx/USD"],
        "etf": ["SPYx/USD", "QQQx/USD", "SGOVx/USD"],
        "fx": ["EURC/USDC"],
        "treasury_fund": ["USDY/USDC"],
    },
    "uniswap_v3_v4": {
        "metal": ["PAXG/USDC"],
        "treasury_fund": ["OUSG/USDC", "USDY/USDC", "BUIDL/USDC", "TBILL/USDC", "USTB/USDC"],
        "tokenized_fund": ["USCC/USDC"],
    },
    "curve_stableswap": {
        "fx": ["EURC/USDC"],
        "treasury_fund": ["USDY/USDC", "OUSG/USDC", "TBILL/USDC", "USTB/USDC"],
        "tokenized_fund": ["USCC/USDC"],
    },
    "balancer_pools": {
        "metal": ["PAXG/USDC"],
        "treasury_fund": ["BUIDL/USDC", "OUSG/USDC", "TBILL/USDC", "USTB/USDC"],
        "tokenized_fund": ["USCC/USDC"],
    },
    "aerodrome_slipstream": {
        "fx": ["EURC/USDC"],
        "treasury_fund": ["USDY/USDC", "BUIDL/USDC", "USTB/USDC"],
        "tokenized_fund": ["USCC/USDC"],
    },
}

BLOCKSIZE_STATE_REFERENCE_SYMBOLS: dict[str, list[str]] = {
    "treasury_fund": ["BUIDL/USD", "OUSG/USD", "USDY/USD", "TBILL/USD", "USTB/USD"],
    "tokenized_fund": ["USCC/USD"],
    "metal": ["PAXG/USD"],
}

REFERENCE_SYMBOL_CLASSES: dict[str, str] = {
    "OUSG": "treasury_fund",
    "USDY": "treasury_fund",
    "TBILL": "treasury_fund",
    "USTB": "treasury_fund",
    "USCC": "tokenized_fund",
    "SGOVx/USD": "etf",
    "TBLLx/USD": "etf",
}

SUPPORTED_RWA_ASSET_CLASS_FILTERS = {
    "all",
    "equity",
    "etf",
    "index",
    "fx",
    "commodity",
    "metal",
    "treasury",
    "treasury_fund",
    "tokenized_fund",
    "crypto",
    "yield_token",
    "option",
    "prediction",
}


def _symbol_key(symbol: str) -> str:
    return symbol.replace("_1", "").replace("x/", "/").upper()


def _add_symbol(
    rows: list[dict[str, Any]],
    *,
    symbol: str,
    asset_class: str,
    venue: str,
    source_type: str,
    coverage_status: str,
    vwap_support: str,
    bidask_support: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    row = {
        "symbol": symbol,
        "asset_id": _symbol_key(symbol).split("/")[0],
        "asset_class": asset_class,
        "venue": venue,
        "source_type": source_type,
        "coverage_status": coverage_status,
        "vwap_support": vwap_support,
        "bidask_support": bidask_support,
        "block_sizes_usd": _block_sizes_for_asset_class(asset_class),
    }
    if metadata:
        row["metadata"] = metadata
    rows.append(row)


def _block_sizes_for_asset_class(asset_class: str) -> list[int]:
    if asset_class == "crypto":
        return BLOCK_SIZES_USD["crypto"]
    if asset_class in {"equity", "tokenized_equity"}:
        return BLOCK_SIZES_USD["tokenized_equity"]
    if asset_class in {"etf", "tokenized_etf"}:
        return BLOCK_SIZES_USD["tokenized_etf"]
    if asset_class in {"index", "etf_index"}:
        return BLOCK_SIZES_USD["synthetic_etf_index"]
    if asset_class == "fx":
        return BLOCK_SIZES_USD["fx"]
    if asset_class in {"commodity", "metal"}:
        return BLOCK_SIZES_USD["metal_commodity"]
    if asset_class in {"treasury", "treasury_nav", "treasury_fund", "tokenized_fund"}:
        return BLOCK_SIZES_USD["treasury_nav"]
    return BLOCK_SIZES_USD["synthetic_equity"]


def _dex_source_type(venue: str) -> str:
    if venue == "jupiter_router":
        return "quote_sweep"
    if venue in {"curve_stableswap", "balancer_pools"}:
        return "onchain_stableswap_pool"
    return "onchain_clmm_pool"


def _dex_vwap_support(source_type: str) -> str:
    if source_type == "quote_sweep":
        return "route_quote_sweep_vwap"
    if source_type == "onchain_stableswap_pool":
        return "pool_simulated_stableswap_vwap"
    return "pool_simulated_clmm_vwap"


def _coverage_rows() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for asset_class, symbols in OSTIUM_SYMBOLS.items():
        for symbol in symbols:
            _add_symbol(
                rows,
                symbol=symbol,
                asset_class=asset_class,
                venue="ostium",
                source_type="synthetic_depth",
                coverage_status="documented",
                vwap_support="synthetic_block_vwap",
                bidask_support="native_synthetic_bid_mid_ask",
            )
    for asset_class, symbols in GAINS_SYMBOLS.items():
        normalized_class = "index" if asset_class == "etf_index" else asset_class
        for symbol in symbols:
            _add_symbol(
                rows,
                symbol=symbol,
                asset_class=normalized_class,
                venue="gains",
                source_type="price_stream_no_book",
                coverage_status="documented",
                vwap_support="trade_vwap_only",
                bidask_support="mark_price_reference",
            )
    for asset_class, symbols in KRAKEN_XSTOCKS_EXAMPLES.items():
        normalized_class = "equity" if asset_class == "tokenized_equity" else "etf"
        for symbol in symbols:
            _add_symbol(
                rows,
                symbol=symbol,
                asset_class=normalized_class,
                venue="kraken_xstocks",
                source_type="native_l2",
                coverage_status="catalog_unconfirmed_public_pair_rejected",
                vwap_support="requires_dynamic_catalog_confirmation",
                bidask_support="requires_dynamic_catalog_confirmation",
            )
    for venue, by_asset_class in DEX_RWA_SYMBOLS.items():
        source_type = _dex_source_type(venue)
        for asset_class, symbols in by_asset_class.items():
            for symbol in symbols:
                _add_symbol(
                    rows,
                    symbol=symbol,
                    asset_class=asset_class,
                    venue=venue,
                    source_type=source_type,
                    coverage_status="candidate_requires_pool_and_liquidity_validation",
                    vwap_support=_dex_vwap_support(source_type),
                    bidask_support="side_specific_pool_or_route_quote",
                )
    for spot_row in HYPERLIQUID_RWA_SPOT_SYMBOLS:
        unverified = hyperliquid_is_unverified(spot_row)
        _add_symbol(
            rows,
            symbol=str(spot_row["display_pair"]),
            asset_class=hyperliquid_normalized_asset_class(spot_row),
            venue=HYPERLIQUID_RWA_SPOT_VENUE_ID,
            source_type="native_l2",
            coverage_status=(
                "hyperliquid_spot_unverified_identity_requires_manual_review"
                if unverified
                else "hyperliquid_spot_candidate_requires_identity_liquidity_and_benchmark_validation"
            ),
            vwap_support="requires_identity_verification" if unverified else "native_l2_block_vwap",
            bidask_support="requires_identity_verification" if unverified else "native_top_of_book",
            metadata={
                "hyperliquid_coin": spot_row["hyperliquid_coin"],
                "pair_index": spot_row["pair_index"],
                "hyperliquid_asset_class": spot_row["asset_class"],
                "identity_note": spot_row["identity_note"],
                "token_id": spot_row["token_id"],
                "evm_contract": spot_row["evm_contract"],
                "use_case": spot_row["use_case"],
                "promotion_gate": (
                    "manual_identity_review_required"
                    if unverified
                    else "issuer_identity_liquidity_and_benchmark_validation_required"
                ),
            },
        )
    _add_symbol(
        rows,
        symbol="PAXG/USD",
        asset_class="metal",
        venue="hyperliquid_paxg",
        source_type="native_l2",
        coverage_status="documented_single_market",
        vwap_support="native_l2_block_vwap",
        bidask_support="native_top_of_book",
    )
    existing_hyperliquid_keys = {
        (str(row["venue"]), str(row["symbol"]).upper())
        for row in rows
        if str(row.get("venue", "")).startswith("hyperliquid")
    }
    for live_row in load_hyperliquid_tradeable_coverage_rows():
        venue = str(live_row.get("venue") or "")
        symbol = str(live_row.get("symbol") or "")
        if not venue or not symbol or (venue, symbol.upper()) in existing_hyperliquid_keys:
            continue
        _add_symbol(
            rows,
            symbol=symbol,
            asset_class=str(live_row.get("asset_class") or "crypto"),
            venue=venue,
            source_type=str(live_row.get("source_type") or "native_l2"),
            coverage_status=str(live_row.get("coverage_status") or "hyperliquid_live_candidate"),
            vwap_support=str(live_row.get("vwap_support") or "native_l2_block_vwap"),
            bidask_support=str(live_row.get("bidask_support") or "native_top_of_book"),
            metadata=live_row.get("metadata") if isinstance(live_row.get("metadata"), dict) else None,
        )
        existing_hyperliquid_keys.add((venue, symbol.upper()))
    existing_derivative_keys = {
        (
            str(row.get("venue")),
            str(row.get("symbol")).upper(),
            (
                row.get("metadata", {}).get("venue_market_id")
                if isinstance(row.get("metadata"), dict)
                else None
            ),
        )
        for row in rows
        if row.get("venue") and row.get("symbol")
    }
    for derivative_row in load_derivative_coverage_rows():
        venue = str(derivative_row.get("venue") or "")
        symbol = str(derivative_row.get("symbol") or "")
        derivative_metadata = (
            derivative_row.get("metadata")
            if isinstance(derivative_row.get("metadata"), dict)
            else {}
        )
        derivative_key = (venue, symbol.upper(), derivative_metadata.get("venue_market_id"))
        if not venue or not symbol or derivative_key in existing_derivative_keys:
            continue
        _add_symbol(
            rows,
            symbol=symbol,
            asset_class=str(derivative_row.get("asset_class") or "crypto"),
            venue=venue,
            source_type=str(derivative_row.get("source_type") or "native_l2"),
            coverage_status=str(derivative_row.get("coverage_status") or "derivative_venue_candidate"),
            vwap_support=str(derivative_row.get("vwap_support") or "native_l2_derivative_block_vwap_requires_basis_adjustment"),
            bidask_support=str(derivative_row.get("bidask_support") or "native_derivative_top_of_book_requires_basis_adjustment"),
            metadata=derivative_metadata or None,
        )
        existing_derivative_keys.add(derivative_key)
    for symbol, asset_class in REFERENCE_SYMBOL_CLASSES.items():
        _add_symbol(
            rows,
            symbol=symbol,
            asset_class=asset_class,
            venue="treasury_nav",
            source_type="nav_reference",
            coverage_status="benchmark_only",
            vwap_support="not_vwap_suitable",
            bidask_support="nav_or_soft_quote_reference",
        )
    for asset_class, symbols in BLOCKSIZE_STATE_REFERENCE_SYMBOLS.items():
        for symbol in symbols:
            _add_symbol(
                rows,
                symbol=symbol,
                asset_class=asset_class,
                venue="blocksize_state",
                source_type="blocksize_state_reference",
                coverage_status="candidate_requires_state_instrument_probe",
                vwap_support="not_vwap_suitable",
                bidask_support="state_reference_mid",
                metadata={
                    "benchmark_service": "state",
                    "state_symbol": symbol.replace("/", ""),
                    "promotion_gate": "state_instruments_pool_coverage_and_freshness_required",
                },
            )
    existing_rwa_xyz_keys = {
        (
            str(row.get("venue")),
            str(row.get("symbol")).upper(),
            str((row.get("metadata") or {}).get("rwa_xyz_asset_id")),
        )
        for row in rows
        if row.get("venue") == RWA_XYZ_VENUE_DESCRIPTOR["id"]
    }
    for rwa_xyz_row in load_rwa_xyz_coverage_rows():
        venue = str(rwa_xyz_row.get("venue") or "")
        symbol = str(rwa_xyz_row.get("symbol") or "")
        metadata = rwa_xyz_row.get("metadata") if isinstance(rwa_xyz_row.get("metadata"), dict) else {}
        key = (venue, symbol.upper(), str(metadata.get("rwa_xyz_asset_id")))
        if not venue or not symbol or key in existing_rwa_xyz_keys:
            continue
        row = deepcopy(rwa_xyz_row)
        asset_class = str(row.get("asset_class") or "tokenized_fund")
        row["block_sizes_usd"] = _block_sizes_for_asset_class(asset_class)
        rows.append(row)
        existing_rwa_xyz_keys.add(key)
    return rows


def _normalize_filter(value: str | None) -> str:
    return (value or "all").strip().lower().replace("-", "_")


def _filter_rows(
    rows: list[dict[str, Any]],
    *,
    asset_class: str | None,
    venue: str | None,
) -> list[dict[str, Any]]:
    asset_filter = _normalize_filter(asset_class)
    venue_filter = _normalize_filter(venue)
    if asset_filter not in SUPPORTED_RWA_ASSET_CLASS_FILTERS:
        raise ValueError(f"Unsupported asset_class: {asset_class}")
    venue_ids = {item["id"] for item in VENUES}
    if venue_filter != "all" and venue_filter not in venue_ids:
        raise ValueError(f"Unsupported venue: {venue}")
    return [
        row
        for row in rows
        if (asset_filter == "all" or row["asset_class"] == asset_filter)
        and (venue_filter == "all" or row["venue"] == venue_filter)
    ]


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_asset_class: dict[str, set[str]] = defaultdict(set)
    by_venue: dict[str, set[str]] = defaultdict(set)
    source_types: dict[str, int] = defaultdict(int)
    for row in rows:
        by_asset_class[row["asset_class"]].add(row["asset_id"])
        by_venue[row["venue"]].add(row["asset_id"])
        source_types[row["source_type"]] += 1
    return {
        "rows": len(rows),
        "unique_assets": len({row["asset_id"] for row in rows}),
        "by_asset_class": {key: len(value) for key, value in sorted(by_asset_class.items())},
        "by_venue": {key: len(value) for key, value in sorted(by_venue.items())},
        "by_source_type": dict(sorted(source_types.items())),
    }


def build_rwa_coverage_overview(
    *,
    asset_class: str | None = None,
    venue: str | None = None,
    include_symbols: bool = True,
) -> dict[str, Any]:
    """Return filtered RWA coverage that can back discovery and planning APIs."""
    rows = _filter_rows(_coverage_rows(), asset_class=asset_class, venue=venue)
    response: dict[str, Any] = {
        "source_report": FEASIBILITY_REPORT_PATH,
        "coverage_summary": _summarize(rows),
        "coverage_notes": [
            "Kraken xStocks coverage must be enumerated dynamically from public or authenticated venue instruments; current public AssetPairs checks reject the seed xStock aliases, so those rows are not promoted to feeds.",
            "Ostium and Gains lists are documented non-crypto markets from the feasibility study.",
            "Hyperliquid RWA spot rows come from public spotMeta/l2Book candidates and require identity, liquidity, and benchmark validation before promotion.",
            "Hyperliquid live tradeable rows are loaded from reports/hyperliquid_tradeable_feeds.json when present; rerun the discovery script to refresh meta and spotMeta coverage.",
            "Derivative venue rows are loaded from reports/rwa_derivative_venue_discovery.json when present; raw perp/futures prices require basis, funding, expiry, and benchmark adjustment before they can support spot/fair-value replacement feeds.",
            "Tokenized Treasury or liquidity-fund products are benchmark/NAV references, not real-time VWAP or bid/ask replacements.",
        ],
        "venues": deepcopy(VENUES),
        "quality_alignment": deepcopy(QUALITY_ALIGNMENT),
    }
    if include_symbols:
        response["symbols"] = rows
    return response


def build_rwa_asset_matrix(
    *,
    asset_class: str | None = None,
    venue: str | None = None,
) -> dict[str, Any]:
    """Return assets grouped across venues with sourcing gaps and next actions."""
    rows = _filter_rows(_coverage_rows(), asset_class=asset_class, venue=venue)
    grouped: dict[str, dict[str, Any]] = {}
    for row in rows:
        asset_id = str(row["asset_id"])
        item = grouped.setdefault(
            asset_id,
            {
                "asset_id": asset_id,
                "asset_classes": set(),
                "symbols": set(),
                "venues": {},
                "source_types": set(),
                "block_sizes_usd": row["block_sizes_usd"],
            },
        )
        item["asset_classes"].add(row["asset_class"])
        item["symbols"].add(row["symbol"])
        item["source_types"].add(row["source_type"])
        item["venues"][row["venue"]] = {
            "symbol": row["symbol"],
            "source_type": row["source_type"],
            "coverage_status": row["coverage_status"],
            "vwap_support": row["vwap_support"],
            "bidask_support": row["bidask_support"],
        }
        if row.get("metadata"):
            item["venues"][row["venue"]]["metadata"] = row["metadata"]

    assets = []
    all_registry_venues = {str(venue_item["id"]) for venue_item in VENUES}
    for item in grouped.values():
        venue_ids = set(item["venues"])
        source_types = sorted(item["source_types"])
        asset_classes = sorted(item["asset_classes"])
        symbols = sorted(item["symbols"])
        executable_venues = [
            venue_id
            for venue_id, venue_data in item["venues"].items()
            if venue_data["source_type"] in {
                "native_l2",
                "synthetic_depth",
                "quote_sweep",
                "quote_stream",
                "onchain_clmm_pool",
                "onchain_stableswap_pool",
            }
        ]
        reference_venues = [
            venue_id
            for venue_id, venue_data in item["venues"].items()
            if venue_data["source_type"] in {
                "nav_reference",
                "issuer_reference",
                "benchmark_reference",
                "blocksize_state_reference",
                "platform_catalog_reference",
            }
        ]
        missing_registry_venues = sorted(all_registry_venues - venue_ids)
        assets.append(
            {
                "asset_id": item["asset_id"],
                "asset_classes": asset_classes,
                "symbols": symbols,
                "venue_count": len(venue_ids),
                "venues": item["venues"],
                "source_types": source_types,
                "executable_venues": sorted(executable_venues),
                "reference_venues": sorted(reference_venues),
                "missing_registry_venues": missing_registry_venues,
                "block_sizes_usd": item["block_sizes_usd"],
                "sourcing_status": (
                    "multi_venue"
                    if len(executable_venues) >= 2
                    else "single_venue"
                    if executable_venues
                    else "reference_only"
                    if reference_venues
                    else "coverage_gap"
                ),
            }
        )

    assets.sort(key=lambda item: (-int(item["venue_count"]), str(item["asset_id"])))
    multi_venue = [item for item in assets if item["sourcing_status"] == "multi_venue"]
    single_venue = [item for item in assets if item["sourcing_status"] == "single_venue"]
    reference_only = [item for item in assets if item["sourcing_status"] == "reference_only"]
    dynamic_registry_venues = [
        {
            "venue": venue_item["id"],
            "name": venue_item["name"],
            "status": venue_item["status"],
            "coverage_mode": venue_item["coverage_mode"],
            "data": venue_item["data"],
            "next_action": "fetch_dynamic_catalog_or_confirm_vendor_license",
        }
        for venue_item in VENUES
        if str(venue_item["coverage_mode"]).startswith(("dynamic", "api_keyed", "licensed", "issuer", "next_static_props"))
        or str(venue_item["coverage_mode"]) in {"issuer_catalog_and_attestation", "licensed_dynamic_catalog"}
    ]
    return {
        "source_report": FEASIBILITY_REPORT_PATH,
        "summary": {
            "asset_count": len(assets),
            "coverage_rows": len(rows),
            "multi_venue_assets": len(multi_venue),
            "single_venue_assets": len(single_venue),
            "reference_only_assets": len(reference_only),
            "registry_venue_count": len(VENUES),
        },
        "assets": assets,
        "dynamic_registry_venues": dynamic_registry_venues,
        "sourcing_priorities": [
            "Fetch dynamic product catalogs for Kraken xStocks, Bybit xStocks, Jupiter xStocks, Ondo Stocks, Backed issuer metadata, and high-quality DEX pools/routes.",
            "Prioritize multi-venue assets such as AAPL, NVDA, TSLA, MSFT, AMZN, COIN, MSTR, XAU, XAG, WTI, SPY, QQQ, and major FX pairs.",
            "Promote DEX observations only after pool allowlists, liquidity thresholds, slot/block freshness, price-impact checks, and route/pool replay receipts pass.",
            "Use derivative venue observations as native derivative liquidity first; derive spot/fair value only after contract specs, funding/carry inputs, and Blocksize benchmark alignment pass.",
            "Use Polygon/TradFi benchmark reference only for quality alignment and regulated reference data subject to license.",
            "Keep Treasury/NAV products reference-only unless secondary executable pools are added.",
            "Use RWA.xyz New Asset Monitor rows as token/product discovery coverage only; executable real-time prices still require venue, pool, route, liquidity, freshness, and issuer alignment.",
        ],
    }


def _target_symbol_key(symbol: str) -> str:
    return symbol.replace("_1", "").replace("x/", "/").split("/")[0].upper()


def build_oracle_parity_matrix() -> dict[str, Any]:
    """Compare current sourcing coverage to Pyth/Chainlink-style oracle breadth."""
    current = build_rwa_asset_matrix()
    current_assets = {str(asset["asset_id"]): asset for asset in current["assets"]}
    categories: list[dict[str, Any]] = []
    total_targets = 0
    covered_targets = 0
    partial_targets = 0

    for category_id, target in ORACLE_PARITY_TARGETS.items():
        target_rows = []
        for symbol in target["target_symbols"]:
            total_targets += 1
            asset_key = _target_symbol_key(symbol)
            current_asset = current_assets.get(asset_key)
            present_venues = set((current_asset or {}).get("venues", {}))
            present_source_types = set((current_asset or {}).get("source_types", []))
            target_venue_overrides = target.get("target_venue_overrides") or {}
            needed_venues = set(target_venue_overrides.get(symbol, target["needed_venues"]))
            required_source_types = set(target["required_source_types"])
            venue_gap = sorted(needed_venues - present_venues)
            source_type_gap = sorted(required_source_types - present_source_types)
            if current_asset and not venue_gap:
                status = "covered"
                covered_targets += 1
            elif current_asset:
                status = "partial"
                partial_targets += 1
            else:
                status = "missing"
            target_rows.append(
                {
                    "symbol": symbol,
                    "asset_id": asset_key,
                    "status": status,
                    "needed_venues": sorted(needed_venues),
                    "current_venues": sorted(present_venues),
                    "current_source_types": sorted(present_source_types),
                    "venue_gap": venue_gap,
                    "source_type_gap": source_type_gap,
                }
            )
        categories.append(
            {
                "category": category_id,
                "asset_class": target["asset_class"],
                "priority": target["priority"],
                "target_count": len(target["target_symbols"]),
                "covered": len([row for row in target_rows if row["status"] == "covered"]),
                "partial": len([row for row in target_rows if row["status"] == "partial"]),
                "missing": len([row for row in target_rows if row["status"] == "missing"]),
                "needed_venues": target["needed_venues"],
                "required_source_types": target["required_source_types"],
                "targets": target_rows,
            }
        )

    return {
        "source_report": FEASIBILITY_REPORT_PATH,
        "oracle_sources": ORACLE_PARITY_SOURCES,
        "summary": {
            "target_categories": len(ORACLE_PARITY_TARGETS),
            "target_symbols": total_targets,
            "covered_targets": covered_targets,
            "partial_targets": partial_targets,
            "missing_targets": total_targets - covered_targets - partial_targets,
            "current_asset_count": current["summary"]["asset_count"],
            "registry_venue_count": len(VENUES),
        },
        "categories": categories,
        "sourcing_backlog": [
            {
                "priority": "P0",
                "action": "Fetch and reconcile dynamic xStocks catalogs from Kraken, Backed issuer metadata, Jupiter, Bybit, Ondo, and DEX token/pool catalogs.",
                "reason": "Needed to expand equity/ETF parity beyond the current seed symbols.",
            },
            {
                "priority": "P0",
                "action": "Add DEX route/pool adapters for Jupiter, Raydium, Orca, Meteora, Uniswap, Curve, Balancer, and Aerodrome.",
                "reason": "High-quality DEX liquidity is needed for tokenized equity/ETF, stablecoin/FX proxy, tokenized Treasury-fund, and PAXG overlap coverage.",
            },
            {
                "priority": "P0",
                "action": "License or integrate benchmark reference data for equities, ETFs, FX, commodities, rates, and corporate actions.",
                "reason": "Needed for Chainlink/Pyth-like benchmark alignment and source-of-truth validation.",
            },
            {
                "priority": "P1",
                "action": "Add oracle-reference adapters for Pyth and Chainlink catalog/metadata checks.",
                "reason": "Needed to track parity coverage, feed heartbeat/deviation metadata, NAV, rates, macro, and proof-of-reserve categories.",
            },
            {
                "priority": "P1",
                "action": "Add rates and macro reference ingestion for US2Y, US10Y, US30Y, GDP, wage growth, and CPI index.",
                "reason": "These are explicit Pyth/Chainlink-style categories not covered by current RWA venue feeds.",
            },
            {
                "priority": "P2",
                "action": "Add proof-of-reserve and issuer attestation ingestion for stablecoins, wrapped assets, tokenized funds, and tokenized treasuries.",
                "reason": "Required for Chainlink-style PoR/tokenized-asset coverage parity.",
            },
        ],
    }


def build_dex_venue_quality_plan() -> dict[str, Any]:
    """Return DEX venues, source semantics, and promotion gates."""
    dex_ids = set(DEX_QUALITY_REQUIREMENTS["initial_quality_tier"])
    dex_venues = [venue for venue in deepcopy(VENUES) if venue["id"] in dex_ids]
    rows = [row for row in _coverage_rows() if row["venue"] in dex_ids]
    by_venue: dict[str, set[str]] = defaultdict(set)
    by_asset_class: dict[str, set[str]] = defaultdict(set)
    by_source_type: dict[str, int] = defaultdict(int)
    for row in rows:
        by_venue[row["venue"]].add(row["asset_id"])
        by_asset_class[row["asset_class"]].add(row["asset_id"])
        by_source_type[row["source_type"]] += 1
    return {
        "summary": {
            "dex_venue_count": len(dex_venues),
            "seed_rows": len(rows),
            "seed_assets": len({row["asset_id"] for row in rows}),
            "by_venue": {key: len(value) for key, value in sorted(by_venue.items())},
            "by_asset_class": {key: len(value) for key, value in sorted(by_asset_class.items())},
            "by_source_type": dict(sorted(by_source_type.items())),
        },
        "quality_requirements": deepcopy(DEX_QUALITY_REQUIREMENTS),
        "venues": dex_venues,
        "seed_coverage": rows,
        "execution_order": [
            "Jupiter router quote sweeps for xStocks and Solana tokenized assets after API-key setup.",
            "Meteora/Raydium/Orca pool-state probes for Solana xStocks, EURC/USDC, and tokenized Treasury-fund pools.",
            "Uniswap/Balancer/Curve/Aerodrome probes for PAXG, stablecoin/FX proxy, and tokenized-fund pools.",
            "Promote only pools/routes that pass liquidity, price-impact, freshness, manipulation, and benchmark checks.",
        ],
    }


def build_rwa_build_plan() -> dict[str, Any]:
    """Return the recommended build sequence for RWA VWAP and bid/ask coverage."""
    coverage = build_rwa_coverage_overview(include_symbols=False)
    return {
        "source_report": FEASIBILITY_REPORT_PATH,
        "recommendation": (
            "Build a supplemental RWA market-data layer first. Keep existing Blocksize/vendor "
            "feeds as benchmarks until every venue observation passes freshness, spread, depth, "
            "source-type, and cross-venue agreement checks."
        ),
        "target_endpoints": [
            "/v1/rwa/vwap/{symbol}?block_size_usd=10000&venue=kraken_xstocks",
            "/v1/rwa/bidask/{symbol}?venue=ostium",
            "/v1/rwa/coverage",
            "/v1/rwa/build-plan",
        ],
        "phases": [
            {
                "phase": 1,
                "duration": "2 weeks",
                "deliverable": "Adapter contracts and first-wave ingestion",
                "work": [
                    "Implement Kraken xStocks REST/WS adapter for ticker, trades, OHLC, and L2 books.",
                    "Implement Ostium bid/mid/ask, candle, fill, and simulated-depth adapter.",
                    "Implement Gains price stream and recent-trade adapter as benchmark/trade-VWAP source.",
                    "Normalize every observation into Blocksize VWAP and bid/ask response shapes.",
                ],
            },
            {
                "phase": 2,
                "duration": "2 weeks",
                "deliverable": "Block-size VWAP engine and sparse-liquidity handling",
                "work": [
                    "Walk true L2 books by notional for Kraken and later Bybit.",
                    "Sweep quote sizes for Jupiter routes and label them quote-derived.",
                    "Use Ostium simulated depth only with explicit synthetic-depth metadata.",
                    "Return fillable_notional_usd and partial-fill status instead of extrapolating.",
                ],
            },
            {
                "phase": 3,
                "duration": "1-2 weeks",
                "deliverable": "Outlier detection and data-quality gates",
                "work": [
                    "Run median absolute deviation checks across independent observations.",
                    "Compare each RWA venue against existing Blocksize/vendor benchmark symbols.",
                    "Exclude stale and severe benchmark-drift observations from consolidated prices.",
                    "Persist quality flags, source receipts, and replayable raw observations.",
                ],
            },
            {
                "phase": 4,
                "duration": "ongoing",
                "deliverable": "Venue expansion",
                "work": [
                    "Add Jupiter xStocks route quotes for Solana liquidity.",
                    "Add high-quality DEX routes and pools for xStocks, EURC/USDC, PAXG, tokenized treasuries, and stable assets.",
                    "Add Bybit xStocks where legally and technically available.",
                    "Add Hyperliquid PAXG as gold overlap.",
                    "Add tokenized Treasury NAV sources as benchmark/reference feeds only.",
                ],
            },
        ],
        "block_sizes_usd": deepcopy(BLOCK_SIZES_USD),
        "coverage_summary": coverage["coverage_summary"],
        "first_wave_venues": [venue for venue in deepcopy(VENUES) if venue["status"] == "first_wave"],
        "expansion_venues": [venue for venue in deepcopy(VENUES) if venue["status"] != "first_wave"],
        "quality_alignment": deepcopy(QUALITY_ALIGNMENT),
    }
