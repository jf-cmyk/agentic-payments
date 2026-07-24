"""Source-readiness checklist for RWA market-data expansion.

This layer turns external prerequisites into machine-readable status rows:
API keys, RPC/indexer access, token/pool identifiers, exchange licenses,
issuer access, futures inputs, and production operations controls.
"""

from __future__ import annotations

import csv
import json
import os
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

from src.rwa_dex_allowlist import build_dex_allowlist
from src.rwa_provider_catalog import build_provider_catalog
from src.rwa_rights_clearance import env_or_clearance_ack
from src.rwa_xyz_monitor import RWA_XYZ_READINESS_DEPENDENCY, RWA_XYZ_VENUE_ID


READINESS_CATEGORIES: dict[str, str] = {
    "dex_liquidity": "DEX routes, pools, RPC, and token identifiers",
    "oracle_reference": "Oracle/reference-stream access and catalog identifiers",
    "issuer_nav_reserve": "Issuer, NAV, reserve, attestation, and redemption sources",
    "licensed_exchange": "Direct exchange and consolidated real-time market data",
    "market_data_vendor": "Commercial market-data vendors and security-master feeds",
    "futures_fair_value": "Futures, curves, contract specs, and fair-value components",
    "blocksize_benchmark": "Blocksize benchmark access for feed comparison",
    "production_ops": "Scheduler, storage, secrets, alerting, and promotion controls",
    "legal_policy": "Redistribution, entitlement, and usage-policy decisions",
}

SUPPORTED_READINESS_CATEGORIES = {"all", *READINESS_CATEGORIES}
SUPPORTED_READINESS_STATUSES = {
    "all",
    "configured",
    "missing_required_config",
    "missing_identifier_mapping",
    "blocked_by_license_or_contract",
    "blocked_by_partner_or_whitelist",
    "blocked_by_legal_policy",
}


SOURCE_DEPENDENCIES: list[dict[str, Any]] = [
    RWA_XYZ_READINESS_DEPENDENCY,
    {
        "dependency_id": "jupiter_api_key",
        "category": "dex_liquidity",
        "priority": "P0",
        "name": "Jupiter API key",
        "required_env": ["JUPITER_API_KEY"],
        "optional_env": ["JUPITER_BASE_URL"],
        "missing_status": "missing_required_config",
        "required_for": ["Jupiter router quote sweeps", "Solana xStocks routes", "EURC/USD route quotes"],
        "unblocks": ["jupiter_router", "jupiter_xstocks", "14 Jupiter route allowlist candidates"],
        "next_action": "Provision a Jupiter API key or keep the adapter in mocked/catalog-only mode.",
        "quality_gate": "routePlan, contextSlot, priceImpactPct, and timestamp must be recorded for every quote.",
    },
    {
        "dependency_id": "solana_token_mint_registry",
        "category": "dex_liquidity",
        "priority": "P0",
        "name": "Verified Solana token mint registry",
        "required_env": ["RWA_SOLANA_TOKEN_MINTS_PATH"],
        "optional_env": ["JUPITER_TOKEN_SEARCH_API_KEY"],
        "artifact_paths": ["reports/rwa_solana_token_mints.json"],
        "missing_status": "missing_identifier_mapping",
        "required_for": ["xStock token identity", "EURC/USD", "USDY/OUSG/Treasury-fund route validation"],
        "unblocks": ["jupiter_router", "raydium_clmm", "orca_whirlpool", "meteora_dlmm"],
        "next_action": "Load a reviewed mint registry with symbol, mint, decimals, issuer, and underlying identity.",
        "quality_gate": "no DEX quote is usable until base and quote mints map to canonical assets.",
    },
    {
        "dependency_id": "jupiter_route_allowlist",
        "category": "dex_liquidity",
        "priority": "P0",
        "name": "Jupiter route allowlist evidence",
        "required_env": ["RWA_JUPITER_ROUTE_ALLOWLIST_PATH"],
        "optional_env": [],
        "artifact_paths": ["reports/rwa_jupiter_route_allowlist.json"],
        "missing_status": "missing_identifier_mapping",
        "required_for": ["Jupiter route promotion", "quote-sweep provenance", "route-plan review"],
        "unblocks": ["jupiter_router", "jupiter_xstocks"],
        "next_action": "Run live Jupiter route discovery and review route labels, AMM keys, price impact, and context slots.",
        "quality_gate": "route evidence must include routePlan, contextSlot, priceImpactPct, token mints, and benchmark checks.",
    },
    {
        "dependency_id": "solana_rpc_indexer",
        "category": "dex_liquidity",
        "priority": "P0",
        "name": "Solana RPC and indexer access",
        "required_env": ["SOLANA_RPC_URL"],
        "optional_env": ["SOLANA_INDEXER_URL", "SOLANA_GRPC_URL"],
        "missing_status": "missing_required_config",
        "required_for": ["slot freshness", "pool account reads", "trade/event backfills"],
        "unblocks": ["raydium_clmm", "orca_whirlpool", "meteora_dlmm", "kamino_rwa_routes"],
        "next_action": "Provision low-latency Solana RPC plus indexer/GRPC access for pool-state adapters.",
        "quality_gate": "slot lag and account timestamp must pass the real-time freshness gate.",
    },
    {
        "dependency_id": "solana_pool_allowlist",
        "category": "dex_liquidity",
        "priority": "P0",
        "name": "Solana DEX pool allowlist",
        "required_env": ["RWA_SOLANA_POOL_ALLOWLIST_PATH"],
        "optional_env": ["METEORA_POOL_CATALOG_URL", "RAYDIUM_POOL_CATALOG_URL", "ORCA_WHIRLPOOL_CATALOG_URL"],
        "artifact_paths": ["reports/rwa_solana_pool_allowlist.json"],
        "missing_status": "missing_identifier_mapping",
        "required_for": ["Meteora DLMM", "Raydium CLMM", "Orca Whirlpool"],
        "unblocks": ["29 Solana pool-state candidates"],
        "next_action": "Attach pool IDs, whirlpool addresses, fee tiers, tick/bin arrays, and canonical asset mapping.",
        "quality_gate": "liquidity, price impact, organic volume, and concentration must pass before promotion.",
    },
    {
        "dependency_id": "evm_rpc_multichain",
        "category": "dex_liquidity",
        "priority": "P1",
        "name": "EVM RPC access for RWA pools",
        "required_env": ["EVM_RPC_ETHEREUM_URL", "EVM_RPC_BASE_URL"],
        "optional_env": ["EVM_RPC_ARBITRUM_URL", "EVM_RPC_POLYGON_URL", "EVM_RPC_OPTIMISM_URL"],
        "missing_status": "missing_required_config",
        "required_for": ["Uniswap", "Curve", "Balancer", "Aerodrome"],
        "unblocks": ["EVM pool-state and swap-simulation adapters"],
        "next_action": "Provision archive-capable EVM RPC URLs for chains where RWA pools are allowlisted.",
        "quality_gate": "block number, pool state, token decimals, and swap simulation must be replayable.",
    },
    {
        "dependency_id": "evm_pool_allowlist",
        "category": "dex_liquidity",
        "priority": "P1",
        "name": "EVM pool and token allowlist",
        "required_env": ["RWA_EVM_POOL_ALLOWLIST_PATH"],
        "optional_env": ["THE_GRAPH_API_KEY", "GOLDSKY_API_KEY"],
        "artifact_paths": ["reports/rwa_evm_pool_allowlist.json"],
        "missing_status": "missing_identifier_mapping",
        "required_for": ["Uniswap V3/V4", "Curve", "Balancer", "Aerodrome"],
        "unblocks": ["10 EVM/Base DEX candidates"],
        "next_action": "Load pool addresses, fee tiers, token contracts, chain IDs, and subgraph/indexer mappings.",
        "quality_gate": "pool IDs and token contracts must be reviewed before quote or pool-state ingestion.",
    },
    {
        "dependency_id": "pyth_access",
        "category": "oracle_reference",
        "priority": "P0",
        "name": "Pyth catalog and API access",
        "required_env": ["PYTH_API_KEY"],
        "optional_env": ["PYTH_HERMES_URL", "PYTH_FEED_MAP_PATH"],
        "missing_status": "blocked_by_license_or_contract",
        "required_for": ["Pyth parity", "oracle confidence checks", "RWA reference catalog"],
        "unblocks": ["Pyth equities, FX, metals, rates, crypto, and NAV-like references"],
        "next_action": "Confirm Pyth product tier and redistribution terms, then load feed-id mappings.",
        "quality_gate": "publish time, confidence interval, feed id, and license scope must be retained.",
    },
    {
        "dependency_id": "chainlink_data_streams",
        "category": "oracle_reference",
        "priority": "P0",
        "name": "Chainlink Data Streams access",
        "required_env": ["CHAINLINK_DATA_STREAMS_API_KEY", "CHAINLINK_DATA_STREAMS_API_SECRET"],
        "optional_env": ["CHAINLINK_FEED_REGISTRY_PATH"],
        "missing_status": "blocked_by_license_or_contract",
        "required_for": ["Chainlink parity", "low-latency report verification", "NAV/PoR references"],
        "unblocks": ["Chainlink RWA, FX, commodities, proof-of-reserve, and NAV sources"],
        "next_action": "Get Data Streams credentials and feed registry exports for targeted RWA products.",
        "quality_gate": "report timestamp, feed id, observations timestamp, and verification metadata must be recorded.",
    },
    {
        "dependency_id": "redstone_access",
        "category": "oracle_reference",
        "priority": "P1",
        "name": "RedStone API and feed catalog",
        "required_env": ["REDSTONE_API_KEY"],
        "optional_env": ["REDSTONE_FEED_MAP_PATH"],
        "missing_status": "blocked_by_license_or_contract",
        "required_for": ["supplemental oracle consensus", "RWA reference cross-checks"],
        "unblocks": ["RedStone equities, FX, commodities, and tokenized assets where available"],
        "next_action": "Confirm commercial terms and map feed IDs into canonical symbols.",
        "quality_gate": "timestamp, signer/report provenance, and source-family label must be retained.",
    },
    {
        "dependency_id": "dia_api_access",
        "category": "oracle_reference",
        "priority": "P1",
        "name": "DIA API access",
        "required_env": ["DIA_API_KEY"],
        "optional_env": ["DIA_FEED_MAP_PATH"],
        "missing_status": "blocked_by_license_or_contract",
        "required_for": ["additional oracle/reference diversity", "RWA feed discovery"],
        "unblocks": ["DIA RWA, FX, commodity, and crypto reference rows"],
        "next_action": "Confirm DIA access tier and load relevant feed mappings.",
        "quality_gate": "source timestamp and benchmark/source-family labeling are required.",
    },
    {
        "dependency_id": "api3_switchboard_access",
        "category": "oracle_reference",
        "priority": "P2",
        "name": "API3 and Switchboard access",
        "required_env": ["API3_API_KEY", "SWITCHBOARD_API_KEY"],
        "optional_env": ["API3_FEED_MAP_PATH", "SWITCHBOARD_FEED_MAP_PATH"],
        "missing_status": "blocked_by_license_or_contract",
        "required_for": ["oracle diversity", "long-tail FX/commodity/reference checks"],
        "unblocks": ["API3 and Switchboard supplemental consensus legs"],
        "next_action": "Confirm feed availability and license rights before adding adapters.",
        "quality_gate": "provider, feed id, heartbeat, and deviation policy must be visible.",
    },
    {
        "dependency_id": "backed_xstocks_issuer",
        "category": "issuer_nav_reserve",
        "priority": "P0",
        "name": "Backed xStocks issuer/reference access",
        "required_env": ["BACKED_API_KEY"],
        "optional_env": ["BACKED_PRODUCT_CATALOG_PATH", "BACKED_ATTESTATION_URL"],
        "missing_status": "blocked_by_partner_or_whitelist",
        "required_for": ["xStock token identity", "underlying mapping", "attestations"],
        "unblocks": ["Backed xStocks issuer reference adapter"],
        "next_action": "Secure issuer catalog/API access or load reviewed issuer exports.",
        "quality_gate": "issuer, underlying, token contract, and reserve/attestation clock must align.",
    },
    {
        "dependency_id": "dinari_access",
        "category": "issuer_nav_reserve",
        "priority": "P0",
        "name": "Dinari dShares access",
        "required_env": ["DINARI_API_KEY"],
        "optional_env": ["DINARI_PRODUCT_CATALOG_PATH"],
        "missing_status": "blocked_by_partner_or_whitelist",
        "required_for": ["Dinari dShares catalog", "brokerage quote/reference surface"],
        "unblocks": ["Dinari quote and issuer-reference adapters"],
        "next_action": "Obtain partner/API access and confirm redistribution rights.",
        "quality_gate": "whitelist, symbol identity, quote timestamp, and legal rights must pass.",
    },
    {
        "dependency_id": "ondo_access",
        "category": "issuer_nav_reserve",
        "priority": "P0",
        "name": "Ondo Global Markets and fund source access",
        "required_env": ["ONDO_API_KEY"],
        "optional_env": ["ONDO_PRODUCT_CATALOG_PATH", "ONDO_NAV_URL"],
        "missing_status": "blocked_by_partner_or_whitelist",
        "required_for": ["Ondo Stocks", "OUSG/USDY/NAV validation"],
        "unblocks": ["Ondo quote, NAV, and attestation adapters"],
        "next_action": "Confirm whitelist/API access and issuer NAV publication terms.",
        "quality_gate": "NAV references remain supplemental unless paired with executable market data.",
    },
    {
        "dependency_id": "securitize_buidl_access",
        "category": "issuer_nav_reserve",
        "priority": "P0",
        "name": "Securitize/BUIDL fund access",
        "required_env": ["SECURITIZE_API_KEY"],
        "optional_env": ["BUIDL_NAV_URL", "BUIDL_TOKEN_CONTRACTS_PATH"],
        "missing_status": "blocked_by_partner_or_whitelist",
        "required_for": ["BUIDL identity", "transfer-agent/NAV/reference validation"],
        "unblocks": ["BUIDL reference, NAV, and fund identity checks"],
        "next_action": "Secure transfer-agent or issuer-approved reference/NAV access.",
        "quality_gate": "BUIDL is a tokenized fund; never classify it as a direct Treasury instrument.",
    },
    {
        "dependency_id": "treasury_issuer_pack",
        "category": "issuer_nav_reserve",
        "priority": "P1",
        "name": "Treasury issuer/NAV source pack",
        "required_env": ["OPENEDEN_API_KEY", "SUPERSTATE_API_KEY", "MATRIXDOCK_API_KEY"],
        "optional_env": ["OPENEDEN_NAV_URL", "SUPERSTATE_NAV_URL", "MATRIXDOCK_NAV_URL"],
        "missing_status": "blocked_by_partner_or_whitelist",
        "required_for": ["TBILL", "USTB", "STBT", "tokenized treasury funds"],
        "unblocks": ["issuer NAV/reserve consensus legs for treasury-fund assets"],
        "next_action": "Confirm issuer endpoints, NAV clocks, reserve proofs, and redistribution permissions.",
        "quality_gate": "NAV/reference rows cannot be promoted as tick-by-tick market data.",
    },
    {
        "dependency_id": "precious_metals_issuer_pack",
        "category": "issuer_nav_reserve",
        "priority": "P1",
        "name": "Precious-metals issuer and reserve access",
        "required_env": ["PAXOS_API_KEY", "TETHER_GOLD_API_KEY"],
        "optional_env": ["PAXG_RESERVE_URL", "XAUT_RESERVE_URL"],
        "missing_status": "blocked_by_partner_or_whitelist",
        "required_for": ["PAXG", "XAUT", "tokenized gold reserve/reference checks"],
        "unblocks": ["metal reserve and issuer reference adapters"],
        "next_action": "Confirm issuer APIs or public reserve sources and retention rights.",
        "quality_gate": "reserve/NAV evidence supplements executable exchange/DEX liquidity.",
    },
    {
        "dependency_id": "us_equity_realtime_license",
        "category": "licensed_exchange",
        "priority": "P0",
        "name": "U.S. equity consolidated/direct-feed license",
        "required_env": ["CTA_UTP_LICENSE_ACK", "NYSE_MARKET_DATA_LICENSE_ACK", "NASDAQ_MARKET_DATA_LICENSE_ACK"],
        "optional_env": ["CBOE_MARKET_DATA_LICENSE_ACK", "IEX_CLOUD_API_KEY"],
        "missing_status": "blocked_by_license_or_contract",
        "required_for": ["full S&P 500 NBBO", "U.S. equity trade VWAP", "corporate-action-adjusted identity"],
        "unblocks": ["us_equity_consolidated_feed", "S&P 500 replacement-grade bid/ask and VWAP"],
        "next_action": "Complete exchange/vendor licensing and entitlement review for real-time U.S. equity redistribution.",
        "quality_gate": "sale/quote condition filtering and entitlement scope must be enforced.",
    },
    {
        "dependency_id": "apac_equity_realtime_licenses",
        "category": "licensed_exchange",
        "priority": "P1",
        "name": "APAC equity exchange licenses",
        "required_env": ["HKEX_MARKET_DATA_LICENSE_ACK", "SSE_SZSE_MARKET_DATA_LICENSE_ACK", "JPX_MARKET_DATA_LICENSE_ACK", "KRX_MARKET_DATA_LICENSE_ACK"],
        "optional_env": ["TWSE_MARKET_DATA_LICENSE_ACK", "NSE_BSE_MARKET_DATA_LICENSE_ACK", "SGX_MARKET_DATA_LICENSE_ACK"],
        "missing_status": "blocked_by_license_or_contract",
        "required_for": ["Hong Kong", "China A-shares", "Japan", "South Korea", "Taiwan", "India", "Singapore equities"],
        "unblocks": ["APAC equity universe VWAP and bid/ask feeds"],
        "next_action": "Select direct exchange feeds or a licensed vendor package and confirm redistribution rights.",
        "quality_gate": "local trading calendar, auction/session states, and corporate actions must be normalized.",
    },
    {
        "dependency_id": "europe_equity_realtime_licenses",
        "category": "licensed_exchange",
        "priority": "P1",
        "name": "UK and Europe equity exchange licenses",
        "required_env": ["LSE_MARKET_DATA_LICENSE_ACK", "EURONEXT_MARKET_DATA_LICENSE_ACK", "XETRA_MARKET_DATA_LICENSE_ACK"],
        "optional_env": ["SIX_MARKET_DATA_LICENSE_ACK", "BME_MARKET_DATA_LICENSE_ACK"],
        "missing_status": "blocked_by_license_or_contract",
        "required_for": ["LSE", "Euronext", "Deutsche Boerse/Xetra", "continental Europe equities"],
        "unblocks": ["Europe equity universe VWAP and bid/ask feeds"],
        "next_action": "Acquire direct/vendor entitlements and security-master/corporate-action data.",
        "quality_gate": "MIC, currency, session status, and local holiday rules must be attached.",
    },
    {
        "dependency_id": "americas_ex_us_equity_licenses",
        "category": "licensed_exchange",
        "priority": "P1",
        "name": "Canada and LatAm equity licenses",
        "required_env": ["TSX_MARKET_DATA_LICENSE_ACK"],
        "optional_env": ["BMV_MARKET_DATA_LICENSE_ACK", "B3_MARKET_DATA_LICENSE_ACK"],
        "missing_status": "blocked_by_license_or_contract",
        "required_for": ["Canada equities", "LatAm expansion candidates"],
        "unblocks": ["Canadian and Americas ex-U.S. equity feed expansion"],
        "next_action": "Prioritize TSX/TMX real-time access, then evaluate LatAm venues by RWA demand.",
        "quality_gate": "security master, currency, board lot, and corporate-action rules must be normalized.",
    },
    {
        "dependency_id": "databento_vendor_access",
        "category": "market_data_vendor",
        "priority": "P0",
        "name": "Databento or equivalent tick-data vendor access",
        "required_env": ["DATABENTO_API_KEY"],
        "optional_env": ["DATABENTO_DATASET_ALLOWLIST"],
        "missing_status": "blocked_by_license_or_contract",
        "required_for": ["U.S. equities", "futures", "historical tick benchmarks"],
        "unblocks": ["trade VWAP backtests", "latency/frequency benchmarking", "replacement feed validation"],
        "next_action": "Confirm datasets, real-time entitlements, historical retention, and redistribution terms.",
        "quality_gate": "schema, sale conditions, and exchange timestamps must be retained in receipts.",
    },
    {
        "dependency_id": "polygon_vendor_access",
        "category": "market_data_vendor",
        "priority": "P1",
        "name": "Polygon.io or equivalent reference vendor access",
        "required_env": ["POLYGON_API_KEY"],
        "optional_env": ["POLYGON_ENTITLEMENTS_PATH"],
        "missing_status": "blocked_by_license_or_contract",
        "required_for": ["equity/ETF reference checks", "corporate actions", "secondary benchmark comparisons"],
        "unblocks": ["benchmark and security-master coverage for U.S. listed assets"],
        "next_action": "Confirm plan covers real-time quotes/trades and redistribution needs.",
        "quality_gate": "vendor latency and official exchange timestamp must be separated.",
    },
    {
        "dependency_id": "enterprise_vendor_access",
        "category": "market_data_vendor",
        "priority": "P1",
        "name": "Enterprise vendor access",
        "required_env": ["LSEG_API_KEY", "BLOOMBERG_BLPAPI_HOST"],
        "optional_env": ["ICE_CONNECTIVITY_ACK", "FACTSET_API_KEY"],
        "missing_status": "blocked_by_license_or_contract",
        "required_for": ["global security master", "corporate actions", "licensed global equities and reference data"],
        "unblocks": ["global tradfi replacement-grade coverage"],
        "next_action": "Choose the enterprise vendor lane and map entitlement scope into source policies.",
        "quality_gate": "redistribution rights and exchange contributor codes must be visible per observation.",
    },
    {
        "dependency_id": "futures_exchange_licenses",
        "category": "futures_fair_value",
        "priority": "P0",
        "name": "Futures exchange real-time licenses",
        "required_env": ["CME_MARKET_DATA_LICENSE_ACK", "ICE_MARKET_DATA_LICENSE_ACK"],
        "optional_env": ["EUREX_MARKET_DATA_LICENSE_ACK", "HKEX_DERIVATIVES_LICENSE_ACK", "JPX_OSE_LICENSE_ACK", "KRX_DERIVATIVES_LICENSE_ACK", "LME_MARKET_DATA_LICENSE_ACK"],
        "missing_status": "blocked_by_license_or_contract",
        "required_for": ["index futures", "FX futures", "commodities", "rates", "metals"],
        "unblocks": ["futures fair-value consensus layer"],
        "next_action": "License the venues needed for target underlyings and map contracts to canonical assets.",
        "quality_gate": "contract month, volume/open interest, roll rule, and exchange timestamp must be retained.",
    },
    {
        "dependency_id": "futures_contract_specs",
        "category": "futures_fair_value",
        "priority": "P0",
        "name": "Futures contract-specification registry",
        "required_env": ["FUTURES_CONTRACT_SPECS_PATH"],
        "optional_env": ["FUTURES_ROLL_RULES_PATH"],
        "missing_status": "missing_identifier_mapping",
        "required_for": ["fair-value derivation", "roll selection", "contract-to-underlying mapping"],
        "unblocks": ["index, FX, metals, energy, rates, and ETF fair-value models"],
        "next_action": "Load contract specs, calendars, multipliers, ticks, settlement rules, and roll policy.",
        "quality_gate": "model receipts must include contract, roll, calendar, and version identifiers.",
    },
    {
        "dependency_id": "fair_value_component_inputs",
        "category": "futures_fair_value",
        "priority": "P0",
        "name": "Fair-value component inputs",
        "required_env": ["FUTURES_CURVE_INPUTS_PATH"],
        "optional_env": ["DIVIDEND_FORECASTS_PATH", "RATES_CURVE_PATH", "BORROW_COSTS_PATH", "STORAGE_COSTS_PATH", "FX_BASIS_CURVE_PATH"],
        "missing_status": "missing_required_config",
        "required_for": ["cash-futures basis", "index fair value", "FX basis", "commodity carry"],
        "unblocks": ["derived supplemental price data for non-tokenized assets"],
        "next_action": "Configure rates, dividend, funding, borrow, storage, convenience-yield, and FX basis inputs.",
        "quality_gate": "every component must be versioned with source timestamp and model clock.",
    },
    {
        "dependency_id": "derivative_venue_catalogs",
        "category": "futures_fair_value",
        "priority": "P1",
        "name": "On-chain derivative venue catalogs and replay access",
        "required_env": [],
        "optional_env": ["SOLANA_RPC_URL", "EVM_RPC_ETHEREUM_URL", "INJECTIVE_INDEXER_URL"],
        "artifact_paths": ["reports/rwa_derivative_venue_discovery.json"],
        "missing_status": "missing_identifier_mapping",
        "required_for": ["Aster", "Lighter", "Drift", "dYdX", "Orderly", "Aevo", "ApeX Omni", "Pendle", "Derive"],
        "unblocks": ["public perp/futures/yield market catalogs", "venue market-id mapping", "derivative fair-value probes"],
        "next_action": "Run derivative venue discovery, then capture market ids, contract specs, funding, mark/index, books, and replay payload hashes.",
        "quality_gate": "raw derivative observations must remain labeled and require funding/basis/carry adjustment before spot use.",
    },
    {
        "dependency_id": "blocksize_benchmark_access",
        "category": "blocksize_benchmark",
        "priority": "P0",
        "name": "Blocksize benchmark API access",
        "required_env": ["BLOCKSIZE_API_KEY"],
        "optional_env": ["BLOCKSIZE_BASE_URL"],
        "missing_status": "missing_required_config",
        "required_for": ["feed-by-feed comparison", "agentic payments workflow benchmarking"],
        "unblocks": ["30-minute freshness, latency, tick-frequency, deviation, and alignment tests"],
        "next_action": "Configure Blocksize API credentials for live comparison before replacement decisions.",
        "quality_gate": "benchmarks must record Blocksize timestamp, service, symbol, basis bps, and drift status.",
    },
    {
        "dependency_id": "observation_storage",
        "category": "production_ops",
        "priority": "P0",
        "name": "Observation storage",
        "required_env": ["RWA_OBSERVATION_DB_URL"],
        "optional_env": ["RWA_OBSERVATION_RETENTION_DAYS"],
        "missing_status": "missing_required_config",
        "required_for": ["replayable receipts", "benchmark windows", "audit trails"],
        "unblocks": ["continuous 1m/5m/30m feed windows and post-trade investigations"],
        "next_action": "Configure production database storage for normalized observations and raw payload hashes.",
        "quality_gate": "raw payload hash, normalized hash, quality outputs, and source metadata must be persisted.",
    },
    {
        "dependency_id": "scheduler_runtime",
        "category": "production_ops",
        "priority": "P0",
        "name": "Scheduler/runtime supervisor",
        "required_env": ["RWA_SCHEDULER_ENABLED"],
        "optional_env": ["RWA_POLLING_INTERVAL_MS", "RWA_WEBSOCKET_SUPERVISOR_ENABLED"],
        "missing_status": "missing_required_config",
        "required_for": ["continuous sourcing", "freshness monitoring", "tick-frequency tests"],
        "unblocks": ["continuous feed aggregation"],
        "next_action": "Enable the scheduler and set per-source poll/websocket cadence budgets.",
        "quality_gate": "stale sources must be excluded automatically from real-time consensus.",
    },
    {
        "dependency_id": "secrets_backend",
        "category": "production_ops",
        "priority": "P0",
        "name": "Production secrets backend",
        "required_env": ["RWA_SECRETS_BACKEND"],
        "optional_env": ["AWS_SECRETS_MANAGER_REGION", "GCP_SECRET_PROJECT", "VAULT_ADDR"],
        "missing_status": "missing_required_config",
        "required_for": ["API keys", "license credentials", "partner tokens"],
        "unblocks": ["secure production adapters"],
        "next_action": "Choose and configure a secrets backend before loading external provider credentials.",
        "quality_gate": "no secret values may be emitted in source-readiness or quality receipts.",
    },
    {
        "dependency_id": "alerts_and_slos",
        "category": "production_ops",
        "priority": "P1",
        "name": "Alerts and SLO monitors",
        "required_env": ["RWA_ALERT_WEBHOOK_URL"],
        "optional_env": ["RWA_SLO_CONFIG_PATH", "RWA_PAGERDUTY_SERVICE_KEY"],
        "missing_status": "missing_required_config",
        "required_for": ["staleness alerts", "basis divergence alerts", "venue outage monitoring"],
        "unblocks": ["production feed operations"],
        "next_action": "Configure alert destinations and per-asset freshness/basis SLOs.",
        "quality_gate": "feed status must degrade before stale or divergent observations reach consumers.",
    },
    {
        "dependency_id": "redistribution_policy",
        "category": "legal_policy",
        "priority": "P0",
        "name": "Market-data redistribution policy",
        "required_env": ["RWA_MARKET_DATA_POLICY_ACK"],
        "optional_env": ["RWA_REDISPLAY_POLICY_PATH", "RWA_ENTITLEMENT_MATRIX_PATH"],
        "missing_status": "blocked_by_legal_policy",
        "required_for": ["licensed exchange feeds", "vendor feeds", "oracle commercial feeds"],
        "unblocks": ["production promotion beyond internal benchmarking"],
        "next_action": "Document which data may be stored, displayed, redistributed, sold, or used only internally.",
        "quality_gate": "every production source must carry entitlement and redistribution metadata.",
    },
    {
        "dependency_id": "promotion_signoff",
        "category": "legal_policy",
        "priority": "P0",
        "name": "Promotion signoff workflow",
        "required_env": ["RWA_PROMOTION_SIGNOFF_PATH"],
        "optional_env": ["RWA_RELEASE_APPROVER_GROUP"],
        "missing_status": "missing_required_config",
        "required_for": ["feed replacement decisions", "provider deprecation", "customer-facing source changes"],
        "unblocks": ["controlled replacement of existing data providers"],
        "next_action": "Define signoff owners and required evidence for each source type and asset class.",
        "quality_gate": "replacement requires legal rights, uptime, replayability, consensus legs, and benchmark alignment.",
    },
]


ADAPTER_DEPENDENCIES: dict[str, list[str]] = {
    "jupiter_router": ["jupiter_api_key", "solana_token_mint_registry", "jupiter_route_allowlist"],
    "raydium_clmm": ["solana_rpc_indexer", "solana_token_mint_registry", "solana_pool_allowlist"],
    "orca_whirlpool": ["solana_rpc_indexer", "solana_token_mint_registry", "solana_pool_allowlist"],
    "meteora_dlmm": ["solana_rpc_indexer", "solana_token_mint_registry", "solana_pool_allowlist"],
    "uniswap_v3_v4": ["evm_rpc_multichain", "evm_pool_allowlist"],
    "curve_stableswap": ["evm_rpc_multichain", "evm_pool_allowlist"],
    "balancer_pools": ["evm_rpc_multichain", "evm_pool_allowlist"],
    "aerodrome_slipstream": ["evm_rpc_multichain", "evm_pool_allowlist"],
    "pyth": ["pyth_access"],
    "chainlink": ["chainlink_data_streams"],
    "redstone": ["redstone_access"],
    "dia": ["dia_api_access"],
    "backed_xstocks_issuer": ["backed_xstocks_issuer"],
    "dinari_dshares": ["dinari_access"],
    "ondo_global_markets": ["ondo_access"],
    "blackrock_buidl_securitize": ["securitize_buidl_access"],
    "us_equity_consolidated_feed": ["us_equity_realtime_license", "databento_vendor_access", "redistribution_policy"],
    "apac_equity_feeds": ["apac_equity_realtime_licenses", "enterprise_vendor_access", "redistribution_policy"],
    "europe_equity_feeds": ["europe_equity_realtime_licenses", "enterprise_vendor_access", "redistribution_policy"],
    "futures_fair_value": ["futures_exchange_licenses", "futures_contract_specs", "fair_value_component_inputs"],
    "derivative_venue_public_catalogs": ["derivative_venue_catalogs", "observation_storage", "redistribution_policy"],
    RWA_XYZ_VENUE_ID: ["rwa_xyz_monitor_catalog", "redistribution_policy"],
    "blocksize_benchmark": ["blocksize_benchmark_access"],
    "production_runtime": ["observation_storage", "scheduler_runtime", "secrets_backend", "alerts_and_slos"],
}


def _env_is_set(name: str) -> bool:
    if name.endswith("_ACK") or name.endswith("_POLICY_ACK"):
        return env_or_clearance_ack(name)
    return bool(os.getenv(name, "").strip())


def _artifact_exists(path: str) -> bool:
    return Path(path).expanduser().exists()


def _dependency_status(row: dict[str, Any]) -> dict[str, Any]:
    required_env = [str(name) for name in row.get("required_env", [])]
    optional_env = [str(name) for name in row.get("optional_env", [])]
    artifact_paths = [str(path) for path in row.get("artifact_paths", [])]
    configured_required_env = [name for name in required_env if _env_is_set(name)]
    missing_required_env = [name for name in required_env if name not in configured_required_env]
    configured_optional_env = [name for name in optional_env if _env_is_set(name)]
    missing_optional_env = [name for name in optional_env if name not in configured_optional_env]
    configured_artifact_paths = [path for path in artifact_paths if _artifact_exists(path)]
    missing_artifact_paths = [path for path in artifact_paths if path not in configured_artifact_paths]
    status = (
        "configured"
        if not missing_required_env or configured_artifact_paths
        else str(row["missing_status"])
    )
    result = deepcopy(row)
    result.update(
        {
            "status": status,
            "configured": status == "configured",
            "configured_required_env": configured_required_env,
            "missing_required_env": missing_required_env,
            "configured_optional_env": configured_optional_env,
            "missing_optional_env": missing_optional_env,
            "configured_artifact_paths": configured_artifact_paths,
            "missing_artifact_paths": missing_artifact_paths,
            "secret_safe": True,
        }
    )
    return result


def _adapter_status(dependency_rows: dict[str, dict[str, Any]], dependency_ids: list[str]) -> str:
    statuses = [str(dependency_rows[item]["status"]) for item in dependency_ids if item in dependency_rows]
    if statuses and all(status == "configured" for status in statuses):
        return "ready_to_probe"
    if any(status == "blocked_by_license_or_contract" for status in statuses):
        return "blocked_by_license_or_contract"
    if any(status == "blocked_by_partner_or_whitelist" for status in statuses):
        return "blocked_by_partner_or_whitelist"
    if any(status == "blocked_by_legal_policy" for status in statuses):
        return "blocked_by_legal_policy"
    if any(status == "missing_identifier_mapping" for status in statuses):
        return "missing_identifier_mapping"
    return "missing_required_config"


def _build_adapter_readiness(dependencies: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows_by_id = {str(row["dependency_id"]): row for row in dependencies}
    adapter_rows = []
    for adapter_id, dependency_ids in ADAPTER_DEPENDENCIES.items():
        status = _adapter_status(rows_by_id, dependency_ids)
        missing_dependency_ids = [
            dependency_id
            for dependency_id in dependency_ids
            if rows_by_id.get(dependency_id, {}).get("status") != "configured"
        ]
        adapter_rows.append(
            {
                "adapter_or_source": adapter_id,
                "status": status,
                "ready_to_probe": status == "ready_to_probe",
                "dependency_ids": dependency_ids,
                "missing_dependency_ids": missing_dependency_ids,
                "next_action": (
                    "All required dependencies are configured."
                    if status == "ready_to_probe"
                    else "Resolve missing dependency rows before live ingestion or promotion."
                ),
            }
        )
    return sorted(adapter_rows, key=lambda row: (str(row["status"]), str(row["adapter_or_source"])))


def build_source_readiness(
    *,
    category: str = "all",
    status: str = "all",
) -> dict[str, Any]:
    """Return dependency readiness for sourcing and promoting RWA feeds."""
    category_filter = category.strip().lower()
    status_filter = status.strip().lower()
    if category_filter not in SUPPORTED_READINESS_CATEGORIES:
        raise ValueError(f"Unsupported RWA source-readiness category: {category}")
    if status_filter not in SUPPORTED_READINESS_STATUSES:
        raise ValueError(f"Unsupported RWA source-readiness status: {status}")

    all_dependencies = [_dependency_status(row) for row in SOURCE_DEPENDENCIES]
    dependencies = [
        row
        for row in all_dependencies
        if (category_filter == "all" or row["category"] == category_filter)
        and (status_filter == "all" or row["status"] == status_filter)
    ]

    all_by_status = Counter(str(row["status"]) for row in all_dependencies)
    all_by_category = Counter(str(row["category"]) for row in all_dependencies)
    filtered_by_status = Counter(str(row["status"]) for row in dependencies)
    provider_catalog = build_provider_catalog()
    dex_allowlist = build_dex_allowlist()
    adapter_readiness = _build_adapter_readiness(all_dependencies)

    blocked_next_actions = [
        row["next_action"]
        for row in sorted(
            all_dependencies,
            key=lambda item: (str(item["priority"]), str(item["dependency_id"])),
        )
        if row["status"] != "configured"
    ][:10]

    return {
        "summary": {
            "dependency_count": len(all_dependencies),
            "filtered_dependency_count": len(dependencies),
            "configured": all_by_status.get("configured", 0),
            "missing_required_config": all_by_status.get("missing_required_config", 0),
            "missing_identifier_mapping": all_by_status.get("missing_identifier_mapping", 0),
            "blocked_by_license_or_contract": all_by_status.get("blocked_by_license_or_contract", 0),
            "blocked_by_partner_or_whitelist": all_by_status.get("blocked_by_partner_or_whitelist", 0),
            "blocked_by_legal_policy": all_by_status.get("blocked_by_legal_policy", 0),
            "by_category": dict(sorted(all_by_category.items())),
            "filtered_by_status": dict(sorted(filtered_by_status.items())),
            "provider_catalog_count": provider_catalog["summary"]["provider_count"],
            "provider_catalog_blocked_by_auth_or_license": provider_catalog["summary"]["blocked_by_auth_or_license"],
            "dex_allowlist_candidates": dex_allowlist["summary"]["candidate_count"],
            "dex_allowlist_blocked_by_auth_or_rpc": dex_allowlist["summary"]["by_status"].get(
                "blocked_by_auth_or_rpc",
                0,
            ),
            "adapter_or_source_count": len(adapter_readiness),
            "ready_to_probe_adapter_or_source_count": sum(
                1 for row in adapter_readiness if row["status"] == "ready_to_probe"
            ),
        },
        "filters": {"category": category, "status": status},
        "categories": deepcopy(READINESS_CATEGORIES),
        "dependencies": sorted(dependencies, key=lambda row: (str(row["priority"]), str(row["category"]), str(row["dependency_id"]))),
        "adapter_readiness": adapter_readiness,
        "blocked_next_actions": blocked_next_actions,
        "quality_policy": {
            "secret_policy": "report env var names and presence only; never emit secret values",
            "promotion_policy": "a source cannot be promoted until required config, identifiers, rights, freshness, replayability, and benchmark gates pass",
            "real_time_policy": "missing source timestamps or stale tick cadence exclude observations from real-time consensus",
            "replacement_policy": "replacement-grade feeds require legal rights plus at least two independent fresh replayable consensus legs",
        },
        "execution_order": [
            "Configure production secrets, observation storage, scheduler, and Blocksize benchmark access.",
            "Load verified token mints, pool IDs, contract IDs, and oracle feed IDs into canonical mapping files.",
            "Provision public/API-keyed DEX and oracle credentials for sources that do not require exchange licenses.",
            "Complete issuer, exchange, vendor, futures, and redistribution contracts before production promotion.",
            "Run /v1/rwa/sourcing/probe and /v1/rwa/benchmark/blocksize for every now-ready adapter or source.",
        ],
    }


def write_source_readiness_reports(
    *,
    json_path: str | Path,
    csv_path: str | Path,
) -> dict[str, Any]:
    """Write source-readiness reports as JSON plus a compact dependency CSV."""
    readiness = build_source_readiness()
    json_out = Path(json_path)
    csv_out = Path(csv_path)
    json_out.parent.mkdir(parents=True, exist_ok=True)
    csv_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(readiness, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with csv_out.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = [
            "dependency_id",
            "category",
            "priority",
            "name",
            "status",
            "required_env",
            "missing_required_env",
            "configured_artifact_paths",
            "missing_artifact_paths",
            "optional_env",
            "unblocks",
            "next_action",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in readiness["dependencies"]:
            writer.writerow(
                {
                    "dependency_id": row["dependency_id"],
                    "category": row["category"],
                    "priority": row["priority"],
                    "name": row["name"],
                    "status": row["status"],
                    "required_env": json.dumps(row["required_env"], sort_keys=True),
                    "missing_required_env": json.dumps(row["missing_required_env"], sort_keys=True),
                    "configured_artifact_paths": json.dumps(row["configured_artifact_paths"], sort_keys=True),
                    "missing_artifact_paths": json.dumps(row["missing_artifact_paths"], sort_keys=True),
                    "optional_env": json.dumps(row["optional_env"], sort_keys=True),
                    "unblocks": json.dumps(row["unblocks"], sort_keys=True),
                    "next_action": row["next_action"],
                }
            )
    return readiness
