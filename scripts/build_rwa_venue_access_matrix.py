#!/usr/bin/env python3
"""Write the current venue/API/key access matrix for RWA price sourcing."""

from __future__ import annotations

import csv
import json
from pathlib import Path


OUTPUT = Path("reports/rwa_venue_market_data_access_requirements_2026-07-16.csv")


ROWS = [
    ("xstocks_public", "P0", "xStocks public API", "public_no_key", "", "https://docs.xstocks.fi/developers/quickstart", "Use /public/assets/{symbol} and /price-data; optional protected API key is generated in app.backed.fi under Settings > API.", "74 exact reference prices live; 47 newly covered tickers", "No; RPC can add pool prices but does not reproduce the issuer quote.", "reference_price"),
    ("jupiter_router", "P0", "Jupiter", "api_key_configured", "JUPITER_API_KEY|SOLANA_RPC_URL", "https://portal.jup.ag/", "Create a production API key and quota in the Jupiter portal; keep the configured Solana RPC for token and slot checks.", "Executable route snapshots for verified Solana mints", "Partly; direct Raydium/Orca/Meteora pool decoders are required for replayable pool state.", "quote_snapshot"),
    ("raydium_clmm", "P0", "Raydium", "rpc_configured_adapter_incomplete", "SOLANA_RPC_URL", "https://docs.raydium.io/raydium/protocol/developers", "Use the configured Solana RPC/WebSocket and Raydium SDK; discover and allowlist exact pools.", "Direct CLMM/CPMM pool-state price and block-size simulation", "Yes, with verified pools and a complete pool/tick decoder.", "pool_state_vwap"),
    ("orca_whirlpool", "P0", "Orca", "rpc_configured_adapter_incomplete", "SOLANA_RPC_URL", "https://dev.orca.so/", "Use the configured Solana RPC/WebSocket and Whirlpool SDK; discover and allowlist exact pools.", "Direct Whirlpool price and block-size simulation", "Yes, with verified pools and tick-array replay.", "pool_state_vwap"),
    ("meteora_dlmm", "P0", "Meteora", "rpc_configured_adapter_incomplete", "SOLANA_RPC_URL", "https://docs.meteora.ag/", "Use the configured Solana RPC/WebSocket and DLMM SDK; discover and allowlist exact pools.", "Direct bin-state price and block-size simulation", "Yes, with verified pools and bin replay.", "pool_state_vwap"),
    ("drift", "P0", "Drift", "rpc_configured_adapter_missing", "SOLANA_RPC_URL", "https://docs.drift.trade/developers/drift-sdk/dlob", "Use the configured low-latency Solana RPC/WebSocket and Drift SDK to construct the DLOB locally.", "L2 best bid/ask and depth for listed spot/perp markets", "Yes for DLOB construction; the RPC must expose timely account updates and history.", "l2_bidask_vwap"),
    ("ostium", "P0", "Ostium", "public_no_key_live", "", "https://ostium-labs.gitbook.io/ostium-docs/developer/api-and-sdk", "Use the public builder REST endpoints; add Arbitrum RPC only for contract-state replay.", "Public bid/mid/ask and candidate simulated depth", "Partly; RPC covers contract/oracle state, not necessarily the builder quote semantics.", "bidask_reference"),
    ("gains", "P0", "Gains Network", "public_no_key_live", "", "https://docs.gains.trade/developer/integrators/price-feed", "Use the documented public rate-limited REST/WebSocket price stream; add chain RPC/subgraph for parameters and replay.", "High-frequency mark/index-style prices", "Partly; RPC can reconstruct protocol state, but the official stream is the simplest live source.", "price_stream_reference"),
    ("orderly", "P0", "Orderly Network", "public_no_key_l2_adapter_incomplete", "ORDERLY_ORDERBOOK_PATH_TEMPLATE", "https://orderly.network/docs/build-on-omnichain/introduction", "Use public WebSocket snapshot/update endpoints; configure the confirmed L2 path and sequence replay. Private account APIs need account keys, public market data does not.", "Mark/index already available; L2 would add BidAsk/VWAP", "No; Orderly's offchain order book is not recoverable from a generic chain RPC.", "l2_bidask_vwap"),
    ("kraken_xstocks", "P1", "Kraken xStocks", "public_no_key_catalog_limited", "", "https://docs.kraken.com/api/docs/rest-api/get-ticker-information", "Use public AssetPairs/Ticker/Depth for listed xStocks; no key fixes instruments absent from the public catalog.", "Native exchange BidAsk/VWAP where listed", "No; exchange order books require the venue feed.", "l2_bidask_vwap"),
    ("uniswap_v3_v4", "P0", "Uniswap v3/v4", "ethereum_rpc_configured_decoder_partial", "EVM_RPC_ETHEREUM_URL", "https://docs.uniswap.org/", "Use the configured Ethereum RPC; add archive/log capacity and verified pool mappings, then replay slot0, ticks, liquidity and swaps.", "Direct EVM pool-state spot and simulated block-size VWAP", "Yes for exact verified pools; token contracts alone are insufficient.", "pool_state_vwap"),
    ("curve_stableswap", "P1", "Curve", "ethereum_rpc_configured_mapping_missing", "EVM_RPC_ETHEREUM_URL", "https://docs.curve.finance/", "Use registry discovery plus the configured Ethereum RPC; verify pool addresses and decode balances, rates and virtual price.", "StableSwap marginal price and block-size simulation", "Yes for verified pools; fund NAV still needs issuer data.", "pool_state_vwap"),
    ("balancer_pools", "P1", "Balancer", "ethereum_rpc_configured_mapping_missing", "EVM_RPC_ETHEREUM_URL", "https://docs.balancer.fi/", "Use public pool discovery/subgraph plus the configured Ethereum RPC; verify pool IDs, balances, weights and pool type.", "Weighted/stable pool price and block-size simulation", "Yes for verified pools; use an indexer/archive endpoint for replay.", "pool_state_vwap"),
    ("aerodrome_slipstream", "P1", "Aerodrome", "base_rpc_missing", "EVM_RPC_BASE_URL", "https://aerodrome.finance/docs", "Create a Base RPC/WebSocket endpoint with logs, then verify pools and decode Slipstream ticks/liquidity.", "Base pool-state price and block-size simulation", "Yes after the Base RPC and exact pool mapping exist.", "pool_state_vwap"),
    ("hyperliquid", "P0", "Hyperliquid", "public_no_key_live", "", "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint", "Use public info/WebSocket endpoints for market metadata, L2 books and trades; no key is required for public market data.", "Native L2 BidAsk/VWAP for listed RWA/perp/spot markets", "No; Hyperliquid order books come from venue APIs, while HyperEVM RPC covers only EVM state.", "l2_bidask_vwap"),
    ("pyth_pro", "P1", "Pyth Pro", "commercial_key_and_rights_missing", "PYTH_API_KEY", "https://www.pyth.network/price-feeds/pyth-pro", "Request Pyth Pro credentials, product catalog, confidence/publisher metadata and redistribution terms.", "Independent oracle benchmark and possible reference feed", "Public onchain Pyth accounts can be read by RPC where deployed, but Pro delivery/rights are not replaced.", "oracle_reference"),
    ("chainlink_data_streams", "P1", "Chainlink Data Streams / SmartData", "commercial_credentials_and_rights_missing", "CHAINLINK_CLIENT_ID|CHAINLINK_CLIENT_SECRET", "https://data.chain.link/streams", "Request Data Streams credentials and terms; public Data Feed contracts can be read directly where a matching feed exists.", "Independent benchmark, NAV/PoR and low-latency reference data", "Partly; RPC reads public feed contracts but cannot replace Data Streams credentials or rights.", "oracle_reference"),
    ("ondo_global_markets", "P0", "Ondo Global Markets", "partner_api_missing", "ONDO_API_KEY", "https://docs.ondo.finance/api-reference/overview", "Email onboarding@ondo.finance for credentials, price history, metadata and redistribution approval.", "Up to 362 currently blocked unique tickers in the catalog", "No for issuer quote/history; RPC only helps when a separate liquid pool or oracle exists.", "issuer_quote_reference"),
    ("dinari", "P0", "Dinari", "kyb_api_and_nbbo_license_missing", "DINARI_API_KEY", "https://docs.dinari.com/docs/us", "Complete business onboarding/KYB, obtain API credentials, and separately license real-time NBBO if required.", "Up to 73 currently blocked unique tickers", "No for NBBO; RPC only prices a specific liquid dShare pool or onchain rate.", "issuer_or_nbbo_reference"),
    ("robinhood_stock_tokens", "P1", "Robinhood stock tokens", "partner_entitlement_missing", "ROBINHOOD_STOCK_TOKEN_API_KEY", "https://docs.robinhood.com/chain/contracts/", "Request partner product-catalog, quote and redistribution access; public contract metadata is not a quote feed.", "95 currently blocked unique stock-token tickers", "No unless exact tokens gain liquid public pools; the documented public API is crypto-focused.", "issuer_or_venue_reference"),
    ("issuer_nav_reserve", "P0", "RWA issuers and transfer agents", "bilateral_access_and_rights_missing", "ISSUER_SPECIFIC_CREDENTIALS", "reports/rwa_platform_access_requirements_2026-07-16.csv", "Use the platform matrix for all 106 issuers/platforms; request timestamped NAV/share price, redemption terms, raw payload retention and redistribution rights.", "376 unique tickers still require issuer NAV or onchain rate", "Only when an audited onchain exchange-rate/NAV contract exists; balances and supply are not prices.", "nav_or_exchange_rate_reference"),
    ("regulated_us_equity_data", "P1", "CTA/UTP SIP or licensed vendor", "commercial_license_missing", "US_EQUITY_MARKET_DATA_CREDENTIALS", "https://www.ctaplan.com/", "Execute market-data agreements or contract with a normalized vendor for real-time NBBO/trades/depth and redistribution.", "Benchmark tokenized U.S. equities and replace current equity subscription", "No; exchange/SIP data is offchain and licensed.", "benchmark_bidask_trade"),
    ("institutional_fx_data", "P1", "Institutional FX venue/vendor", "commercial_license_missing", "FX_MARKET_DATA_CREDENTIALS", "https://www.lseg.com/en/data-analytics/financial-data/pricing-and-market-data", "Contract for executable FX quotes/trades/depth and redistribution rights.", "Benchmark FX and synthetic perpetual feeds", "No; FX venue liquidity is offchain.", "benchmark_bidask_trade"),
    ("futures_fair_value_data", "P1", "CME/ICE/Eurex or normalized vendor", "commercial_license_missing", "FUTURES_MARKET_DATA_CREDENTIALS", "https://www.cmegroup.com/market-data.html", "License real-time futures L1/L2/trades/settlements and instrument reference data.", "Benchmark commodities, metals, rates and index synthetic feeds", "No; exchange futures books are offchain and licensed.", "benchmark_bidask_trade"),
]


def main() -> None:
    fields = [
        "access_package", "priority", "provider", "current_access_status", "required_env",
        "access_url", "acquisition_method", "coverage_unlocked", "can_rpc_replace", "data_use_case",
    ]
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(fields)
        writer.writerows(ROWS)
    print(json.dumps({"rows": len(ROWS), "output": str(OUTPUT)}, indent=2))


if __name__ == "__main__":
    main()
