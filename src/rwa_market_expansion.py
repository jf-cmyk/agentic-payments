"""Expanded RWA/traditional-asset sourcing and futures-derived pricing plans."""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from src.rwa_coverage import build_rwa_asset_matrix
from src.rwa_equity_universes import EQUITY_UNIVERSES, build_equity_universe_sourcing_plan


EXPANDED_SOURCE_VENUES: list[dict[str, Any]] = [
    {
        "venue_id": "nyse_direct_feed",
        "name": "NYSE Integrated / OpenBook direct feeds",
        "venue_type": "listed_exchange_direct_feed",
        "region": "United States",
        "asset_classes": ["equity", "etf", "closed_end_fund", "reit"],
        "ticker_scope": "all NYSE-listed securities covered by licensed security master",
        "example_tickers": ["BRK.B", "LLY", "JPM", "V", "XOM", "UNH", "MA", "HD"],
        "data_needed": ["security_master", "trades", "quotes", "depth", "halts", "corporate_actions"],
        "status": "license_required",
        "priority": "P0",
    },
    {
        "venue_id": "nasdaq_totalview",
        "name": "Nasdaq TotalView / UTP direct feeds",
        "venue_type": "listed_exchange_direct_feed",
        "region": "United States",
        "asset_classes": ["equity", "etf", "reit"],
        "ticker_scope": "all Nasdaq-listed securities covered by licensed security master",
        "example_tickers": ["AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA", "AVGO"],
        "data_needed": ["security_master", "trades", "quotes", "depth", "halts", "corporate_actions"],
        "status": "license_required",
        "priority": "P0",
    },
    {
        "venue_id": "cboe_us_equities",
        "name": "Cboe U.S. Equities",
        "venue_type": "listed_exchange_direct_feed",
        "region": "United States",
        "asset_classes": ["equity", "etf"],
        "ticker_scope": "Cboe trade/quote/depth coverage for U.S. listed equities and ETFs",
        "example_tickers": ["SPY", "QQQ", "IWM", "DIA", "TLT", "HYG", "GLD", "SLV"],
        "data_needed": ["trades", "quotes", "depth", "auction", "halts"],
        "status": "license_required",
        "priority": "P0",
    },
    {
        "venue_id": "cta_utp_sip",
        "name": "CTA/UTP SIP consolidated U.S. tape",
        "venue_type": "consolidated_tape",
        "region": "United States",
        "asset_classes": ["equity", "etf", "reit", "closed_end_fund"],
        "ticker_scope": "all NMS securities in the consolidated tape",
        "example_tickers": ["AAPL", "SPY", "QQQ", "IWM", "TLT", "GLD", "USO", "UNG"],
        "data_needed": ["consolidated_trades", "nbbo", "halts", "sale_conditions", "quote_conditions"],
        "status": "license_required",
        "priority": "P0",
    },
    {
        "venue_id": "hkex_omd",
        "name": "HKEX OMD-C securities feed",
        "venue_type": "listed_exchange_direct_feed",
        "region": "Hong Kong",
        "asset_classes": ["equity", "etf", "reit", "warrant", "structured_product"],
        "ticker_scope": "all HKEX-listed securities under licensed HKEX master data",
        "example_tickers": ["0700.HK", "9988.HK", "3690.HK", "1299.HK", "0388.HK", "2800.HK", "2828.HK"],
        "data_needed": ["securities_master", "trades", "quotes", "depth", "board_lots", "corporate_actions"],
        "status": "license_required",
        "priority": "P0",
    },
    {
        "venue_id": "sse_szse_china_connect",
        "name": "SSE/SZSE / Stock Connect licensed feeds",
        "venue_type": "listed_exchange_direct_feed",
        "region": "China",
        "asset_classes": ["equity", "etf", "fund"],
        "ticker_scope": "Shanghai, Shenzhen, Beijing and Stock Connect eligible securities by licensed master",
        "example_tickers": ["600519.SS", "601318.SS", "600036.SS", "000858.SZ", "002594.SZ", "300750.SZ", "510300.SS"],
        "data_needed": ["security_master", "trades", "quotes", "depth", "price_limits", "corporate_actions"],
        "status": "license_required",
        "priority": "P0",
    },
    {
        "venue_id": "jpx_arrowhead",
        "name": "JPX/TSE arrowhead market data",
        "venue_type": "listed_exchange_direct_feed",
        "region": "Japan",
        "asset_classes": ["equity", "etf", "reit"],
        "ticker_scope": "all TSE listed securities by licensed JPX master",
        "example_tickers": ["7203.T", "6758.T", "8306.T", "9984.T", "6861.T", "1321.T", "1306.T"],
        "data_needed": ["security_master", "trades", "quotes", "depth", "corporate_actions", "tick_size_table"],
        "status": "license_required",
        "priority": "P0",
    },
    {
        "venue_id": "krx_market_data",
        "name": "KRX real-time equities feed",
        "venue_type": "listed_exchange_direct_feed",
        "region": "South Korea",
        "asset_classes": ["equity", "etf", "reit"],
        "ticker_scope": "all KOSPI/KOSDAQ/KONEX securities by licensed KRX master",
        "example_tickers": ["005930.KS", "000660.KS", "035420.KS", "005380.KS", "051910.KS", "069500.KS"],
        "data_needed": ["security_master", "trades", "quotes", "depth", "price_limits", "corporate_actions"],
        "status": "license_required",
        "priority": "P0",
    },
    {
        "venue_id": "lse_lseg_realtime",
        "name": "LSE/LSEG real-time exchange data",
        "venue_type": "listed_exchange_direct_feed",
        "region": "Europe",
        "asset_classes": ["equity", "etf", "reit", "fund"],
        "ticker_scope": "LSE listed securities plus LSEG reference master",
        "example_tickers": ["AZN.L", "SHEL.L", "HSBA.L", "ULVR.L", "BP.L", "VUSA.L", "ISF.L"],
        "data_needed": ["security_master", "trades", "quotes", "depth", "currency_scaling", "corporate_actions"],
        "status": "license_required",
        "priority": "P0",
    },
    {
        "venue_id": "euronext_optiq",
        "name": "Euronext Optiq market data",
        "venue_type": "listed_exchange_direct_feed",
        "region": "Europe",
        "asset_classes": ["equity", "etf", "fund", "warrant"],
        "ticker_scope": "Euronext Paris, Amsterdam, Brussels, Lisbon, Milan and related markets",
        "example_tickers": ["MC.PA", "OR.PA", "AIR.PA", "SAN.PA", "TTE.PA", "ASML.AS", "ABI.BR"],
        "data_needed": ["security_master", "trades", "quotes", "depth", "mic", "corporate_actions"],
        "status": "license_required",
        "priority": "P0",
    },
    {
        "venue_id": "xetra_deutsche_boerse",
        "name": "Deutsche Boerse Xetra",
        "venue_type": "listed_exchange_direct_feed",
        "region": "Europe",
        "asset_classes": ["equity", "etf", "fund"],
        "ticker_scope": "Xetra listed equities and ETFs by licensed instrument master",
        "example_tickers": ["SAP.DE", "SIE.DE", "ALV.DE", "DTE.DE", "BAS.DE", "EXS1.DE", "DAXEX.DE"],
        "data_needed": ["security_master", "trades", "quotes", "depth", "corporate_actions"],
        "status": "license_required",
        "priority": "P0",
    },
    {
        "venue_id": "kraken_xstocks_dynamic",
        "name": "Kraken xStocks dynamic catalog",
        "venue_type": "tokenized_security_exchange",
        "region": "Global ex-U.S.",
        "asset_classes": ["tokenized_equity", "tokenized_etf", "tokenized_fund"],
        "ticker_scope": "50+ U.S. stocks and ETFs, to be verified from current exchange/issuer catalog",
        "example_tickers": ["AAPLx", "NVDAx", "TSLAx", "SPYx", "QQQx", "GLDx"],
        "data_needed": ["instrument_catalog", "l1", "l2", "trades", "issuer_mapping", "redemption_terms"],
        "status": "catalog_probe_required",
        "priority": "P0",
    },
    {
        "venue_id": "backed_xstocks",
        "name": "Backed xStocks issuer and token contracts",
        "venue_type": "tokenized_security_issuer",
        "region": "On-chain",
        "asset_classes": ["tokenized_equity", "tokenized_etf", "tokenized_fund"],
        "ticker_scope": "xStocks token catalog, contract addresses and backing attestations",
        "example_tickers": ["AAPLx", "AMZNx", "MSFTx", "NVDAx", "TSLAx", "SPYx", "QQQx", "VOOx", "SGOVx"],
        "data_needed": ["issuer_catalog", "contract_addresses", "attestations", "underlying_isin", "redemption_terms"],
        "status": "issuer_catalog_required",
        "priority": "P0",
    },
    {
        "venue_id": "ondo_global_markets",
        "name": "Ondo Stocks / Global Markets",
        "venue_type": "tokenized_security_platform",
        "region": "Global ex-U.S.",
        "asset_classes": ["tokenized_equity", "tokenized_etf", "tokenized_fixed_income_etf"],
        "ticker_scope": "100+ tokenized stocks and ETFs today, designed to scale to thousands",
        "example_tickers": ["TSLA", "NVDA", "FIG", "QQQ", "SPY", "TLT", "TIP", "AGG"],
        "data_needed": ["available_assets", "quote_stream", "mint_redeem", "brokerage_account_cash", "USDon_conversion"],
        "status": "api_access_required",
        "priority": "P0",
    },
    {
        "venue_id": "pyth_market_data",
        "name": "Pyth institutional market data",
        "venue_type": "oracle_market_data_network",
        "region": "Global",
        "asset_classes": ["equity", "fx", "commodity", "metal", "rate", "nav", "crypto_index"],
        "ticker_scope": "Pyth catalog across 3,000+ price feeds and 1,000+ partners where licensed",
        "example_tickers": ["AAPL/USD", "AMZN/USD", "MSFT/USD", "XAU/USD", "EUR/USD", "US2Y", "US10Y", "ACRED/USD"],
        "data_needed": ["catalog", "price", "confidence", "publish_time", "publisher_metadata", "license_tier"],
        "status": "data_plan_required_for_api_and_redistribution",
        "priority": "P0",
    },
    {
        "venue_id": "chainlink_data_feeds",
        "name": "Chainlink Data Feeds / SmartData",
        "venue_type": "oracle_market_data_network",
        "region": "Global",
        "asset_classes": ["fx", "commodity", "metal", "nav", "tokenized_fund", "proof_of_reserve"],
        "ticker_scope": "Chainlink feed explorer catalog and SmartData/NAV/PoR feeds",
        "example_tickers": ["XAU/USD", "EUR/USD", "NAV feeds", "proof_of_reserve", "tokenized_fund_reserves"],
        "data_needed": ["feed_registry", "answer", "heartbeat", "deviation", "decimals", "contract_addresses"],
        "status": "catalog_probe_required",
        "priority": "P1",
    },
    {
        "venue_id": "jupiter_solana_rwa_routes",
        "name": "Jupiter Solana RWA routes",
        "venue_type": "dex_router",
        "region": "On-chain Solana",
        "asset_classes": ["tokenized_equity", "tokenized_etf", "stablecoin_fx", "tokenized_fund"],
        "ticker_scope": "allowlisted xStocks, EURC/USDC, tokenized Treasury/fund routes",
        "example_tickers": ["AAPLx/USDC", "NVDAx/USDC", "SPYx/USDC", "QQQx/USDC", "EURC/USDC", "USDY/USDC"],
        "data_needed": ["quote", "route_plan", "price_impact", "context_slot", "dex_labels", "token_mints"],
        "status": "api_key_and_pool_allowlist_required",
        "priority": "P0",
    },
    {
        "venue_id": "evm_rwa_pools",
        "name": "EVM DEX RWA pools",
        "venue_type": "dex_pool_family",
        "region": "Ethereum/Base/EVM",
        "asset_classes": ["tokenized_fund", "tokenized_treasury_fund", "tokenized_gold", "stablecoin_fx"],
        "ticker_scope": "Uniswap, Curve, Balancer and Aerodrome pools for PAXG, EURC, BUIDL, OUSG, USDY and similar assets",
        "example_tickers": ["PAXG/USDC", "EURC/USDC", "BUIDL/USDC", "OUSG/USDC", "USDY/USDC"],
        "data_needed": ["pool_state", "ticks_or_balances", "swaps", "liquidity_usd", "block_timestamp", "token_contracts"],
        "status": "pool_allowlist_and_rpc_required",
        "priority": "P0",
    },
]


INDEX_AND_FUND_TARGETS: list[dict[str, Any]] = [
    {
        "symbol": "SPYx/USDC",
        "name": "SPDR S&P 500 ETF tokenized route",
        "asset_class": "tokenized_etf",
        "underlying": "S&P 500 ETF",
        "source_candidates": ["backed_xstocks", "jupiter_solana_rwa_routes", "kraken_xstocks_dynamic", "ondo_global_markets"],
    },
    {
        "symbol": "QQQx/USDC",
        "name": "Invesco QQQ tokenized route",
        "asset_class": "tokenized_etf",
        "underlying": "Nasdaq-100 ETF",
        "source_candidates": ["backed_xstocks", "jupiter_solana_rwa_routes", "kraken_xstocks_dynamic", "ondo_global_markets"],
    },
    {
        "symbol": "VOOx/USDC",
        "name": "Vanguard S&P 500 ETF tokenized route",
        "asset_class": "tokenized_etf",
        "underlying": "S&P 500 ETF",
        "source_candidates": ["backed_xstocks", "jupiter_solana_rwa_routes", "kraken_xstocks_dynamic"],
    },
    {
        "symbol": "SGOVx/USDC",
        "name": "iShares 0-3 Month Treasury Bond ETF tokenized route",
        "asset_class": "tokenized_etf",
        "underlying": "short-term U.S. Treasury ETF",
        "source_candidates": ["backed_xstocks", "jupiter_solana_rwa_routes", "kraken_xstocks_dynamic"],
    },
    {
        "symbol": "US500/USD",
        "name": "U.S. 500 synthetic index",
        "asset_class": "equity_index",
        "underlying": "S&P 500 / U.S. large-cap basket",
        "source_candidates": ["ostium", "cme_equity_index_futures", "pyth_market_data"],
    },
    {
        "symbol": "US100/USD",
        "name": "U.S. 100 synthetic index",
        "asset_class": "equity_index",
        "underlying": "Nasdaq-100",
        "source_candidates": ["ostium", "cme_equity_index_futures", "pyth_market_data"],
    },
    {
        "symbol": "US30/USD",
        "name": "U.S. 30 synthetic index",
        "asset_class": "equity_index",
        "underlying": "Dow Jones Industrial Average",
        "source_candidates": ["ostium", "cme_equity_index_futures", "pyth_market_data"],
    },
    {
        "symbol": "HK50/HKD",
        "name": "Hong Kong 50 synthetic index",
        "asset_class": "equity_index",
        "underlying": "Hang Seng / HK large-cap basket",
        "source_candidates": ["ostium", "hkex_derivatives", "hkex_omd"],
    },
    {
        "symbol": "JP225/JPY",
        "name": "Japan 225 synthetic index",
        "asset_class": "equity_index",
        "underlying": "Nikkei 225",
        "source_candidates": ["ostium", "cme_equity_index_futures", "ose_jpx_derivatives", "jpx_arrowhead"],
    },
    {
        "symbol": "GER40/EUR",
        "name": "Germany 40 synthetic index",
        "asset_class": "equity_index",
        "underlying": "DAX / Germany large-cap basket",
        "source_candidates": ["ostium", "eurex_index_futures", "xetra_deutsche_boerse"],
    },
    {
        "symbol": "UK100/GBP",
        "name": "UK 100 synthetic index",
        "asset_class": "equity_index",
        "underlying": "FTSE 100",
        "source_candidates": ["ostium", "ice_liffe_index_futures", "lse_lseg_realtime"],
    },
    {
        "symbol": "EUROSTOXX50/EUR",
        "name": "Euro STOXX 50 futures-derived index",
        "asset_class": "equity_index",
        "underlying": "Euro STOXX 50",
        "source_candidates": ["eurex_index_futures", "euronext_optiq", "xetra_deutsche_boerse"],
    },
]


FUTURES_VENUES: list[dict[str, Any]] = [
    {
        "venue_id": "cme_equity_index_futures",
        "name": "CME equity index futures",
        "asset_classes": ["equity_index", "dividend_index", "total_return_index"],
        "example_contracts": ["ES", "MES", "NQ", "MNQ", "YM", "MYM", "RTY", "M2K", "NKD"],
        "underlyings": ["S&P 500", "Nasdaq-100", "Dow Jones Industrial Average", "Russell 2000", "Nikkei 225"],
        "data_needed": ["real_time_l1_l2", "trades", "settlements", "contract_specs", "expiry_calendar", "roll_calendar", "dividend_futures"],
        "status": "data_plan_required",
    },
    {
        "venue_id": "cme_fx_futures",
        "name": "CME FX futures and FX Link",
        "asset_classes": ["fx"],
        "example_contracts": ["6E", "6B", "6J", "6A", "6C", "6S", "6M", "6N", "CNH", "KRW"],
        "underlyings": ["EUR/USD", "GBP/USD", "USD/JPY", "AUD/USD", "USD/CAD", "USD/CHF", "USD/MXN", "USD/CNH", "USD/KRW"],
        "data_needed": ["real_time_l1_l2", "trades", "settlements", "contract_specs", "delivery_calendar", "ois_curves", "cross_currency_basis"],
        "status": "data_plan_required",
    },
    {
        "venue_id": "cme_metals_futures",
        "name": "COMEX/NYMEX metals futures",
        "asset_classes": ["metal", "commodity"],
        "example_contracts": ["GC", "MGC", "SI", "SIL", "HG", "MHG", "PL", "PA"],
        "underlyings": ["gold", "silver", "copper", "platinum", "palladium"],
        "data_needed": ["real_time_l1_l2", "trades", "settlements", "warehouse_stocks", "contract_specs", "expiry_calendar", "lease_rates"],
        "status": "data_plan_required",
    },
    {
        "venue_id": "cme_energy_futures",
        "name": "NYMEX/CME energy futures",
        "asset_classes": ["commodity", "energy"],
        "example_contracts": ["CL", "MCL", "BZ", "QM", "NG", "MNG", "RB", "HO"],
        "underlyings": ["WTI crude", "Brent crude", "Henry Hub natural gas", "RBOB gasoline", "ULSD/heating oil"],
        "data_needed": ["real_time_l1_l2", "trades", "settlements", "storage", "inventory", "contract_specs", "delivery_calendar"],
        "status": "data_plan_required",
    },
    {
        "venue_id": "eurex_index_futures",
        "name": "Eurex European index futures",
        "asset_classes": ["equity_index"],
        "example_contracts": ["FESX", "FDAX", "FDXM", "FSTX", "FSMI"],
        "underlyings": ["Euro STOXX 50", "DAX", "STOXX Europe 600", "SMI"],
        "data_needed": ["real_time_l1_l2", "trades", "settlements", "contract_specs", "dividend_forecasts", "expiry_calendar"],
        "status": "data_plan_required",
    },
    {
        "venue_id": "hkex_derivatives",
        "name": "HKEX index futures",
        "asset_classes": ["equity_index"],
        "example_contracts": ["HSI", "MHI", "HHI", "MCH", "HTI"],
        "underlyings": ["Hang Seng Index", "Hang Seng China Enterprises Index", "Hang Seng TECH Index"],
        "data_needed": ["real_time_l1_l2", "trades", "settlements", "contract_specs", "dividend_forecasts", "expiry_calendar"],
        "status": "data_plan_required",
    },
    {
        "venue_id": "ose_jpx_derivatives",
        "name": "Osaka Exchange / JPX derivatives",
        "asset_classes": ["equity_index"],
        "example_contracts": ["Nikkei 225 futures", "TOPIX futures", "JPX-Nikkei 400 futures"],
        "underlyings": ["Nikkei 225", "TOPIX", "JPX-Nikkei 400"],
        "data_needed": ["real_time_l1_l2", "trades", "settlements", "contract_specs", "dividend_forecasts", "expiry_calendar"],
        "status": "data_plan_required",
    },
    {
        "venue_id": "krx_derivatives",
        "name": "KRX derivatives",
        "asset_classes": ["equity_index"],
        "example_contracts": ["KOSPI 200 futures", "Mini KOSPI 200 futures", "KOSDAQ 150 futures"],
        "underlyings": ["KOSPI 200", "KOSDAQ 150"],
        "data_needed": ["real_time_l1_l2", "trades", "settlements", "contract_specs", "dividend_forecasts", "expiry_calendar"],
        "status": "data_plan_required",
    },
    {
        "venue_id": "ice_futures",
        "name": "ICE futures markets",
        "asset_classes": ["commodity", "energy", "equity_index", "fx"],
        "example_contracts": ["Brent", "Gasoil", "FTSE 100", "MSCI index futures", "soft commodities"],
        "underlyings": ["Brent crude", "FTSE 100", "MSCI regional indexes", "sugar", "coffee", "cocoa"],
        "data_needed": ["real_time_l1_l2", "trades", "settlements", "contract_specs", "delivery_calendar", "fees"],
        "status": "data_plan_required",
    },
    {
        "venue_id": "lme_metals",
        "name": "London Metal Exchange",
        "asset_classes": ["metal", "commodity"],
        "example_contracts": ["LME Copper", "LME Aluminum", "LME Zinc", "LME Nickel", "LME Lead", "LME Tin"],
        "underlyings": ["base metals"],
        "data_needed": ["real_time_l1_l2", "trades", "official_prices", "warehouse_stocks", "cash_to_3m_curve", "warrants"],
        "status": "data_plan_required",
    },
]


FUTURES_PRICING_METHODS: list[dict[str, Any]] = [
    {
        "asset_class": "equity_index",
        "formula": "spot_estimate = futures_price * exp(-r*T) + present_value_expected_dividends + basis_adjustment",
        "required_components": ["futures mid/bid/ask", "expiry", "risk_free_curve", "dividend_curve", "borrow/funding basis", "calendar", "roll state"],
        "premium_or_discount_terms": ["fair_value_basis", "dividend_uncertainty", "repo/funding basis", "tax withholding where relevant", "roll liquidity"],
        "quality_notes": "Use front and second contracts, dividend futures where available, and reject stale/wide contracts.",
    },
    {
        "asset_class": "single_equity",
        "formula": "spot_estimate = single_stock_future * exp(-r*T) + present_value_expected_dividends + borrow_adjustment",
        "required_components": ["single stock futures or listed total-return derivative", "corporate actions", "dividend schedule", "stock borrow", "financing curve"],
        "premium_or_discount_terms": ["hard-to-borrow premium", "special dividends", "corporate actions", "single-name liquidity"],
        "quality_notes": "Only promote when direct single-stock futures or reliable listed derivatives exist; index beta decomposition is a benchmark, not exact price.",
    },
    {
        "asset_class": "fx",
        "formula": "spot_estimate = futures_price * exp(-(r_domestic - r_foreign + cross_currency_basis)*T), adjusted for quote convention",
        "required_components": ["FX futures mid/bid/ask", "contract quote convention", "domestic and foreign OIS curves", "cross_currency_basis", "settlement calendar"],
        "premium_or_discount_terms": ["interest-rate differential", "cross-currency basis", "holiday/settlement mismatch", "roll spread"],
        "quality_notes": "Normalize CME inverse/direct quote conventions before comparing to spot FX.",
    },
    {
        "asset_class": "metal",
        "formula": "spot_estimate = futures_price * exp(-(r + storage + insurance - convenience_yield - lease_rate)*T)",
        "required_components": ["metals futures curve", "warehouse/location data", "lease rates", "storage and insurance assumptions", "contract specs"],
        "premium_or_discount_terms": ["storage", "insurance", "lease rate", "convenience yield", "loco/location basis"],
        "quality_notes": "Gold/silver can be robust with deep COMEX curves; base metals need LME cash-to-3m and warehouse context.",
    },
    {
        "asset_class": "energy_commodity",
        "formula": "spot_estimate = futures_price * exp(-(r + storage + transport - convenience_yield)*T) + location_quality_basis",
        "required_components": ["energy futures curve", "storage/inventory", "delivery location", "grade/spec", "transport basis", "seasonality"],
        "premium_or_discount_terms": ["storage", "transport", "quality/location differential", "seasonal convenience yield", "inventory shocks"],
        "quality_notes": "WTI/Brent/natural gas spot derivation must preserve delivery hub and grade; do not blend hubs without basis model.",
    },
    {
        "asset_class": "rates_and_treasuries",
        "formula": "derive implied yield/price via CTD: invoice = futures_price * conversion_factor + accrued_interest; solve implied repo/yield curve",
        "required_components": ["Treasury futures", "deliverable basket", "conversion factors", "accrued interest", "CTD selection", "repo curve"],
        "premium_or_discount_terms": ["cheapest-to-deliver option", "implied repo", "delivery optionality", "special repo", "coupon/roll-down"],
        "quality_notes": "Treasury futures are excellent rate benchmarks, but CTD optionality means they are not direct cash Treasury prices.",
    },
    {
        "asset_class": "etf_or_fund",
        "formula": "estimate NAV from underlying futures/basket, then adjust for expense ratio, tracking error, creation/redemption spread, and fund premium/discount",
        "required_components": ["underlying futures", "fund holdings", "NAV/iNAV", "expense ratio", "creation units", "primary-market spread"],
        "premium_or_discount_terms": ["ETF premium/discount", "creation/redemption fees", "expense accrual", "tracking error", "withholding taxes"],
        "quality_notes": "Use futures-derived fair value as benchmark; actual ETF bid/ask still needs exchange quote data.",
    },
]


def _current_asset_ids() -> set[str]:
    return {str(asset["asset_id"]) for asset in build_rwa_asset_matrix()["assets"]}


def _coverage_for_examples(examples: list[str]) -> dict[str, Any]:
    current = _current_asset_ids()
    covered = []
    missing = []
    for symbol in examples:
        base = symbol.split("/", 1)[0].split(".", 1)[0].replace("x", "").replace("X", "").replace("_1", "")
        if base.upper() in current:
            covered.append(symbol)
        else:
            missing.append(symbol)
    return {"covered_examples": covered, "missing_examples": missing}


def build_market_expansion_plan() -> dict[str, Any]:
    """Return venues, ticker scopes, and missing catalog work for expanded RWA coverage."""
    current = build_rwa_asset_matrix()
    equity_universes = build_equity_universe_sourcing_plan()
    by_region = Counter(str(venue["region"]) for venue in EXPANDED_SOURCE_VENUES)
    by_status = Counter(str(venue["status"]) for venue in EXPANDED_SOURCE_VENUES)
    by_asset_class = Counter(
        asset_class
        for venue in EXPANDED_SOURCE_VENUES
        for asset_class in venue["asset_classes"]
    )
    venues = []
    for venue in EXPANDED_SOURCE_VENUES:
        examples = [str(item) for item in venue["example_tickers"]]
        venues.append({**venue, **_coverage_for_examples(examples)})
    universe_rows = []
    for universe_id, universe in EQUITY_UNIVERSES.items():
        overlap = next(
            (
                row["current_registry_overlap"]
                for row in equity_universes["universes"]
                if row["universe_id"] == universe_id
            ),
            {},
        )
        universe_rows.append(
            {
                "universe_id": universe_id,
                "name": universe["name"],
                "region": universe["region"],
                "asset_class": universe["asset_class"],
                "ticker_scope": universe["constituent_note"],
                "sample_symbols": universe["sample_symbols"],
                "primary_source_venues": universe["primary_source_venues"],
                "sourceability": universe["sourceability"],
                "missing_sample_symbols": overlap.get("missing_sample_symbols", universe["sample_symbols"]),
                "required_setup": universe["required_setup"],
            }
        )
    return {
        "summary": {
            "current_registry_assets": current["summary"]["asset_count"],
            "expanded_venue_count": len(EXPANDED_SOURCE_VENUES),
            "expanded_index_and_fund_targets": len(INDEX_AND_FUND_TARGETS),
            "equity_universe_count": len(universe_rows),
            "by_region": dict(sorted(by_region.items())),
            "by_asset_class": dict(sorted(by_asset_class.items())),
            "by_status": dict(sorted(by_status.items())),
        },
        "venues": sorted(venues, key=lambda item: (str(item["priority"]), str(item["venue_id"]))),
        "equity_universes": sorted(universe_rows, key=lambda item: str(item["universe_id"])),
        "index_and_fund_targets": sorted(INDEX_AND_FUND_TARGETS, key=lambda item: str(item["symbol"])),
        "execution_order": [
            "License or connect U.S. CTA/UTP plus NYSE/Nasdaq/Cboe direct data first for S&P 500, Nasdaq-100 and ETF replacement coverage.",
            "Probe xStocks/Ondo/Backed catalogs and DEX routes for tokenized U.S. equity/ETF overlap; promote only after issuer-token verification and liquidity checks.",
            "License HKEX, SSE/SZSE, JPX, KRX and European exchange/vendor feeds for regional equity universes.",
            "Add Pyth/Chainlink catalog probes as oracle/reference comparisons, not sole replacement feeds where redistribution rights are missing.",
            "Add futures-derived fair-value benchmarks for indexes, FX, metals, energy, rates and ETFs.",
        ],
    }


def build_futures_data_plan() -> dict[str, Any]:
    """Return futures venues, instruments and methodology for deriving spot/fair values."""
    by_asset_class = Counter(
        asset_class
        for venue in FUTURES_VENUES
        for asset_class in venue["asset_classes"]
    )
    required_components = sorted(
        {
            component
            for method in FUTURES_PRICING_METHODS
            for component in method["required_components"]
        }
    )
    jobs = []
    for venue in FUTURES_VENUES:
        for underlying in venue["underlyings"]:
            jobs.append(
                {
                    "job_id": f"futures:{venue['venue_id']}:{underlying.lower().replace(' ', '_').replace('/', '_')}",
                    "venue": venue["venue_id"],
                    "underlying": underlying,
                    "asset_classes": venue["asset_classes"],
                    "status": venue["status"],
                    "data_needed": venue["data_needed"],
                }
            )
    return {
        "summary": {
            "futures_venue_count": len(FUTURES_VENUES),
            "futures_underlying_jobs": len(jobs),
            "method_count": len(FUTURES_PRICING_METHODS),
            "by_asset_class": dict(sorted(by_asset_class.items())),
            "required_component_count": len(required_components),
        },
        "venues": FUTURES_VENUES,
        "pricing_methods": FUTURES_PRICING_METHODS,
        "required_components": required_components,
        "jobs": jobs,
        "promotion_gates": [
            "contract specification and tick-value verified",
            "front/next contract selected by liquidity and expiry rules",
            "calendar, settlement and holiday conventions loaded",
            "fair-value model explains futures basis versus independent cash/reference feed within tolerance",
            "premiums, funding, dividends, storage, delivery, and fee assumptions recorded with versioned provenance",
            "derived value remains a benchmark leg until live spot/exchange venue validation passes",
        ],
        "data_plan_recommendation": (
            "Acquire real-time and historical futures data for CME, ICE, Eurex, HKEX, JPX/OSE, KRX and LME, "
            "plus curve/dividend/storage/reference datasets needed to invert futures into spot or fair value."
        ),
    }


def write_market_expansion_reports(
    *,
    expansion_json_path: str | Path,
    futures_json_path: str | Path,
    venue_csv_path: str | Path,
    futures_csv_path: str | Path,
) -> dict[str, Any]:
    """Write JSON and CSV artifacts for the expanded sourcing and futures plans."""
    expansion = build_market_expansion_plan()
    futures = build_futures_data_plan()
    expansion_json = Path(expansion_json_path)
    futures_json = Path(futures_json_path)
    venue_csv = Path(venue_csv_path)
    futures_csv = Path(futures_csv_path)
    for path in (expansion_json, futures_json, venue_csv, futures_csv):
        path.parent.mkdir(parents=True, exist_ok=True)
    expansion_json.write_text(json.dumps(expansion, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    futures_json.write_text(json.dumps(futures, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    with venue_csv.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = [
            "venue_id",
            "name",
            "venue_type",
            "region",
            "asset_classes",
            "ticker_scope",
            "example_tickers",
            "status",
            "priority",
            "missing_examples",
        ]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for venue in expansion["venues"]:
            writer.writerow(
                {
                    "venue_id": venue["venue_id"],
                    "name": venue["name"],
                    "venue_type": venue["venue_type"],
                    "region": venue["region"],
                    "asset_classes": json.dumps(venue["asset_classes"]),
                    "ticker_scope": venue["ticker_scope"],
                    "example_tickers": json.dumps(venue["example_tickers"]),
                    "status": venue["status"],
                    "priority": venue["priority"],
                    "missing_examples": json.dumps(venue["missing_examples"]),
                }
            )

    with futures_csv.open("w", newline="", encoding="utf-8") as handle:
        fieldnames = ["job_id", "venue", "underlying", "asset_classes", "status", "data_needed"]
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for job in futures["jobs"]:
            writer.writerow(
                {
                    "job_id": job["job_id"],
                    "venue": job["venue"],
                    "underlying": job["underlying"],
                    "asset_classes": json.dumps(job["asset_classes"]),
                    "status": job["status"],
                    "data_needed": json.dumps(job["data_needed"]),
                }
            )

    return {"market_expansion": expansion, "futures_data_plan": futures}
