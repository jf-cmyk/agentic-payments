"""Equity universe sourcing plans for U.S., APAC, and global listed markets."""

from __future__ import annotations

from typing import Any

from src.rwa_symbol_registry import build_rwa_symbol_registry


ASIA_REGIONS = {"China", "Hong Kong", "South Korea", "Japan", "Taiwan", "India", "Singapore"}
EUROPE_REGIONS = {"United Kingdom", "Europe"}
NORTH_AMERICA_REGIONS = {"United States", "Canada"}


EQUITY_UNIVERSES: dict[str, dict[str, Any]] = {
    "sp500": {
        "name": "S&P 500",
        "region": "United States",
        "asset_class": "equity",
        "universe_size": 503,
        "constituent_note": "503 common stocks representing 500 companies because some companies have multiple share classes.",
        "sample_symbols": [
            "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "GOOG", "BRK.B", "LLY", "AVGO",
            "TSLA", "JPM", "V", "XOM", "UNH", "MA", "COST", "HD", "PG", "NFLX",
        ],
        "primary_source_venues": ["us_equity_consolidated_tape", "polygon_tradfi_reference"],
        "sourceability": "coverable_with_licensed_us_equity_feed",
        "vwap_support": "yes_trade_vwap_from_tick_trades_or_aggregate_bars",
        "bidask_support": "yes_nbbo_or_direct_feed_top_of_book",
        "benchmark_support": "Blocksize bidask/service check per ticker plus licensed vendor cross-check.",
        "coverage_decision": "yes_cover_whole_universe_after_constituent_loader_and_us_market_data_license",
        "required_setup": [
            "license U.S. equity trades/quotes/NBBO with redistribution terms",
            "load current S&P 500 constituents from licensed S&P DJI or approved reference provider",
            "generate one bid/ask and one VWAP feed per constituent share class",
            "compare sampled symbols against existing Blocksize bidask where available",
            "persist normalized trade/quote receipts and benchmark decisions",
        ],
    },
    "china_a_shares": {
        "name": "China A-shares",
        "region": "China",
        "asset_class": "equity",
        "universe_size": None,
        "constituent_note": "SSE/SZSE/Beijing listed equities; exact active universe should be loaded from licensed exchange/vendor master data.",
        "sample_symbols": [
            "600519.SS", "601318.SS", "600036.SS", "601398.SS", "601288.SS",
            "000858.SZ", "000333.SZ", "002594.SZ", "300750.SZ", "300760.SZ",
        ],
        "primary_source_venues": ["china_a_share_licensed_equities"],
        "sourceability": "coverable_with_mainland_exchange_or_vendor_license",
        "vwap_support": "yes_trade_vwap_from_licensed_trade_feed",
        "bidask_support": "yes_exchange_top_of_book_or_depth_feed",
        "benchmark_support": "requires licensed benchmark/vendor comparison; current Blocksize equity coverage is U.S.-centric.",
        "coverage_decision": "yes_sourceable_but_license_blocked_for_real_time",
        "required_setup": [
            "license SSE/SZSE or China Connect real-time trade/quote data",
            "load active security master and corporate-action mapping",
            "normalize .SS/.SZ symbols into canonical asset ids",
            "apply mainland market session, currency, and holiday calendars",
            "benchmark against licensed vendor/reference feed before consolidation",
        ],
    },
    "hong_kong_equities": {
        "name": "Hong Kong equities",
        "region": "Hong Kong",
        "asset_class": "equity",
        "universe_size": None,
        "constituent_note": "HKEX-listed equities and eligible China/H-share listings; active universe should be loaded from HKEX/licensed vendor master data.",
        "sample_symbols": [
            "0700.HK", "9988.HK", "3690.HK", "1299.HK", "0005.HK",
            "0941.HK", "0388.HK", "2318.HK", "1810.HK", "9618.HK",
        ],
        "primary_source_venues": ["hkex_licensed_equities"],
        "sourceability": "coverable_with_hkex_market_data_license",
        "vwap_support": "yes_trade_vwap_from_hkex_trade_feed",
        "bidask_support": "yes_hkex_top_of_book_or_depth_feed",
        "benchmark_support": "requires HKEX/licensed vendor comparison; current Blocksize equity coverage is U.S.-centric.",
        "coverage_decision": "yes_sourceable_but_license_blocked_for_real_time",
        "required_setup": [
            "license HKEX OMD-C or vendor equivalent",
            "load HKEX securities master, board lot, currency, and corporate-action data",
            "normalize numeric HK tickers and .HK aliases",
            "apply Hong Kong trading sessions, auctions, holidays, and lot-size rules",
            "benchmark against licensed HK reference feed before consolidation",
        ],
    },
    "south_korea_equities": {
        "name": "South Korean equities",
        "region": "South Korea",
        "asset_class": "equity",
        "universe_size": None,
        "constituent_note": "KRX KOSPI/KOSDAQ equities; active universe should be loaded from KRX/licensed vendor master data.",
        "sample_symbols": [
            "005930.KS", "000660.KS", "035420.KS", "005380.KS", "051910.KS",
            "068270.KS", "035720.KS", "373220.KS", "207940.KS", "012450.KS",
        ],
        "primary_source_venues": ["krx_licensed_equities"],
        "sourceability": "coverable_with_krx_market_data_license",
        "vwap_support": "yes_trade_vwap_from_krx_trade_feed",
        "bidask_support": "yes_krx_top_of_book_or_depth_feed",
        "benchmark_support": "requires KRX/licensed vendor comparison; current Blocksize equity coverage is U.S.-centric.",
        "coverage_decision": "yes_sourceable_but_license_blocked_for_real_time",
        "required_setup": [
            "license KRX real-time trade/quote data or vendor equivalent",
            "load KOSPI/KOSDAQ security master, currency, and corporate-action data",
            "normalize six-digit .KS/.KQ ticker aliases",
            "apply Korean trading sessions, holidays, price limits, and board rules",
            "benchmark against licensed KRX/vendor reference feed before consolidation",
        ],
    },
    "japan_equities": {
        "name": "Japan equities",
        "region": "Japan",
        "asset_class": "equity",
        "universe_size": None,
        "constituent_note": "JPX/TSE listed equities; TOPIX/Nikkei coverage requires licensed constituent and exchange data.",
        "sample_symbols": ["7203.T", "6758.T", "8306.T", "9984.T", "6861.T", "6098.T", "8035.T", "9432.T", "4063.T", "6501.T"],
        "primary_source_venues": ["jpx_licensed_equities"],
        "sourceability": "coverable_with_jpx_or_vendor_market_data_license",
        "vwap_support": "yes_trade_vwap_from_jpx_trade_feed",
        "bidask_support": "yes_jpx_top_of_book_or_depth_feed",
        "benchmark_support": "requires JPX/licensed vendor comparison; Blocksize does not currently provide broad Japan equity benchmarks.",
        "coverage_decision": "yes_sourceable_but_license_blocked_for_real_time",
        "required_setup": [
            "license JPX/TSE real-time trade/quote data or vendor equivalent",
            "load Japanese security master, ISIN, currency, board, and corporate-action data",
            "normalize .T ticker aliases and local security codes",
            "apply Japan trading sessions, lunch break rules where applicable, holidays, and currency handling",
            "benchmark against licensed JPX/vendor reference feed before consolidation",
        ],
    },
    "taiwan_equities": {
        "name": "Taiwan equities",
        "region": "Taiwan",
        "asset_class": "equity",
        "universe_size": None,
        "constituent_note": "TWSE/TPEx listed equities; TAIEX and semiconductor coverage require licensed exchange/vendor data.",
        "sample_symbols": ["2330.TW", "2317.TW", "2454.TW", "2308.TW", "2881.TW", "2382.TW", "2412.TW", "2303.TW", "2891.TW", "3711.TW"],
        "primary_source_venues": ["twse_licensed_equities"],
        "sourceability": "coverable_with_twse_tpex_or_vendor_market_data_license",
        "vwap_support": "yes_trade_vwap_from_twse_tpex_trade_feed",
        "bidask_support": "yes_twse_tpex_top_of_book_or_depth_feed",
        "benchmark_support": "requires TWSE/TPEx/licensed vendor comparison; Blocksize does not currently provide broad Taiwan equity benchmarks.",
        "coverage_decision": "yes_sourceable_but_license_blocked_for_real_time",
        "required_setup": [
            "license TWSE/TPEx real-time trade/quote data or vendor equivalent",
            "load Taiwan security master, ISIN, currency, and corporate actions",
            "normalize .TW/.TWO aliases and local numeric tickers",
            "apply Taiwan market sessions, holidays, and price limit rules",
            "benchmark against licensed Taiwan vendor/reference feed before consolidation",
        ],
    },
    "india_equities": {
        "name": "India equities",
        "region": "India",
        "asset_class": "equity",
        "universe_size": None,
        "constituent_note": "NSE/BSE listed equities; NIFTY/SENSEX coverage requires licensed constituent and exchange data.",
        "sample_symbols": ["RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "ICICIBANK.NS", "BHARTIARTL.NS", "SBIN.NS", "LT.NS", "ITC.NS", "AXISBANK.NS"],
        "primary_source_venues": ["india_nse_bse_licensed_equities"],
        "sourceability": "coverable_with_nse_bse_or_vendor_market_data_license",
        "vwap_support": "yes_trade_vwap_from_nse_bse_trade_feed",
        "bidask_support": "yes_nse_bse_top_of_book_or_depth_feed",
        "benchmark_support": "requires NSE/BSE/licensed vendor comparison; Blocksize does not currently provide broad India equity benchmarks.",
        "coverage_decision": "yes_sourceable_but_license_blocked_for_real_time",
        "required_setup": [
            "license NSE/BSE real-time trade/quote data or vendor equivalent",
            "load Indian security master, ISIN, series, currency, and corporate-action data",
            "normalize .NS/.BO aliases and local exchange symbols",
            "apply Indian market sessions, holidays, and corporate-action adjustments",
            "benchmark against licensed Indian vendor/reference feed before consolidation",
        ],
    },
    "uk_equities": {
        "name": "UK equities",
        "region": "United Kingdom",
        "asset_class": "equity",
        "universe_size": None,
        "constituent_note": "LSE listed equities; FTSE 100/250/350 coverage requires licensed LSE/LSEG/FTSE Russell data.",
        "sample_symbols": ["AZN.L", "SHEL.L", "HSBA.L", "ULVR.L", "BP.L", "GSK.L", "RIO.L", "BATS.L", "LSEG.L", "REL.L"],
        "primary_source_venues": ["lse_lseg_licensed_equities"],
        "sourceability": "coverable_with_lse_lseg_or_vendor_market_data_license",
        "vwap_support": "yes_trade_vwap_from_lse_trade_feed",
        "bidask_support": "yes_lse_top_of_book_or_depth_feed",
        "benchmark_support": "requires LSE/LSEG/licensed vendor comparison; Blocksize does not currently provide broad UK equity benchmarks.",
        "coverage_decision": "yes_sourceable_but_license_blocked_for_real_time",
        "required_setup": [
            "license LSE real-time trade/quote data or LSEG/vendor equivalent",
            "load UK security master, ISIN, currency, tick size, and corporate-action data",
            "normalize .L ticker aliases and GBX/GBP price conventions",
            "apply UK market sessions, auctions, holidays, and currency scaling",
            "benchmark against licensed UK vendor/reference feed before consolidation",
        ],
    },
    "europe_equities": {
        "name": "Continental Europe equities",
        "region": "Europe",
        "asset_class": "equity",
        "universe_size": None,
        "constituent_note": "Euronext/Xetra listed equities; STOXX/CAC/DAX/AEX coverage requires licensed constituents and exchange data.",
        "sample_symbols": ["ASML.AS", "MC.PA", "SAP.DE", "SIE.DE", "OR.PA", "AIR.PA", "SAN.PA", "TTE.PA", "SU.PA", "ALV.DE"],
        "primary_source_venues": ["euronext_licensed_equities", "deutsche_boerse_xetra_licensed_equities"],
        "sourceability": "coverable_with_euronext_xetra_or_vendor_market_data_license",
        "vwap_support": "yes_trade_vwap_from_euronext_or_xetra_trade_feeds",
        "bidask_support": "yes_exchange_top_of_book_or_depth_feed",
        "benchmark_support": "requires Euronext/Xetra/licensed vendor comparison; Blocksize does not currently provide broad European equity benchmarks.",
        "coverage_decision": "yes_sourceable_but_license_blocked_for_real_time",
        "required_setup": [
            "license Euronext and Deutsche Boerse/Xetra real-time trade/quote data or vendor equivalents",
            "load European security master, MIC, ISIN, currency, and corporate-action data",
            "normalize .PA/.AS/.BR/.MI/.DE aliases and multi-listing mappings",
            "apply local sessions, holidays, currencies, and tick-size regimes",
            "benchmark against licensed European vendor/reference feed before consolidation",
        ],
    },
    "canada_equities": {
        "name": "Canadian equities",
        "region": "Canada",
        "asset_class": "equity",
        "universe_size": None,
        "constituent_note": "TSX/TSXV listed equities; S&P/TSX index coverage requires licensed constituents and exchange data.",
        "sample_symbols": ["RY.TO", "TD.TO", "SHOP.TO", "ENB.TO", "BN.TO", "BNS.TO", "CNQ.TO", "BMO.TO", "CP.TO", "TRI.TO"],
        "primary_source_venues": ["tsx_licensed_equities"],
        "sourceability": "coverable_with_tsx_tmx_or_vendor_market_data_license",
        "vwap_support": "yes_trade_vwap_from_tsx_trade_feed",
        "bidask_support": "yes_tsx_top_of_book_or_depth_feed",
        "benchmark_support": "requires TSX/licensed vendor comparison; Blocksize does not currently provide broad Canadian equity benchmarks.",
        "coverage_decision": "yes_sourceable_but_license_blocked_for_real_time",
        "required_setup": [
            "license TSX/TSXV/TMX real-time trade/quote data or vendor equivalent",
            "load Canadian security master, ISIN, currency, and corporate-action data",
            "normalize .TO/.V aliases and interlisted U.S./Canada mappings",
            "apply Canadian market sessions, holidays, and currency handling",
            "benchmark against licensed Canadian vendor/reference feed before consolidation",
        ],
    },
    "australia_equities": {
        "name": "Australian equities",
        "region": "Australia",
        "asset_class": "equity",
        "universe_size": None,
        "constituent_note": "ASX listed equities; ASX 200 coverage requires licensed constituents and exchange data.",
        "sample_symbols": ["BHP.AX", "CBA.AX", "CSL.AX", "NAB.AX", "WBC.AX", "ANZ.AX", "MQG.AX", "WES.AX", "FMG.AX", "WOW.AX"],
        "primary_source_venues": ["asx_licensed_equities"],
        "sourceability": "coverable_with_asx_or_vendor_market_data_license",
        "vwap_support": "yes_trade_vwap_from_asx_trade_feed",
        "bidask_support": "yes_asx_top_of_book_or_depth_feed",
        "benchmark_support": "requires ASX/licensed vendor comparison; Blocksize does not currently provide broad Australian equity benchmarks.",
        "coverage_decision": "yes_sourceable_but_license_blocked_for_real_time",
        "required_setup": [
            "license ASX real-time trade/quote data or vendor equivalent",
            "load Australian security master, ISIN, currency, and corporate-action data",
            "normalize .AX aliases and local ticker conventions",
            "apply Australian market sessions, holidays, and currency handling",
            "benchmark against licensed ASX/vendor reference feed before consolidation",
        ],
    },
    "singapore_equities": {
        "name": "Singapore equities",
        "region": "Singapore",
        "asset_class": "equity",
        "universe_size": None,
        "constituent_note": "SGX listed equities; Straits Times Index coverage requires licensed constituents and exchange data.",
        "sample_symbols": ["D05.SI", "O39.SI", "U11.SI", "Z74.SI", "C6L.SI", "S68.SI", "C09.SI", "A17U.SI", "BN4.SI", "F34.SI"],
        "primary_source_venues": ["sgx_licensed_equities"],
        "sourceability": "coverable_with_sgx_or_vendor_market_data_license",
        "vwap_support": "yes_trade_vwap_from_sgx_trade_feed",
        "bidask_support": "yes_sgx_top_of_book_or_depth_feed",
        "benchmark_support": "requires SGX/licensed vendor comparison; Blocksize does not currently provide broad Singapore equity benchmarks.",
        "coverage_decision": "yes_sourceable_but_license_blocked_for_real_time",
        "required_setup": [
            "license SGX real-time trade/quote data or vendor equivalent",
            "load Singapore security master, ISIN, currency, and corporate-action data",
            "normalize .SI aliases and local ticker conventions",
            "apply Singapore market sessions, holidays, and currency handling",
            "benchmark against licensed SGX/vendor reference feed before consolidation",
        ],
    },
}


def _current_overlap(sample_symbols: list[str]) -> dict[str, Any]:
    registry = build_rwa_symbol_registry()
    alias_index = registry["alias_index"]
    matched: list[str] = []
    missing: list[str] = []
    for symbol in sample_symbols:
        compact = "".join(ch for ch in symbol.upper() if ch.isalnum())
        base = compact
        for suffix in (
            "HK",
            "KS",
            "KQ",
            "SS",
            "SZ",
            "T",
            "TW",
            "TWO",
            "NS",
            "BO",
            "L",
            "PA",
            "AS",
            "BR",
            "MI",
            "DE",
            "TO",
            "V",
            "AX",
            "SI",
        ):
            if base.endswith(suffix) and len(base) > len(suffix):
                base = base[: -len(suffix)]
        if compact in alias_index or base in alias_index:
            matched.append(symbol)
        else:
            missing.append(symbol)
    return {
        "sample_count": len(sample_symbols),
        "matched_sample_count": len(matched),
        "missing_sample_count": len(missing),
        "matched_samples": matched,
        "missing_samples": missing,
    }


def _feed_shapes(universe_id: str, universe: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {
            "kind": "bidask",
            "feed_id_template": f"rwa_bidask:{universe_id}:{{symbol}}",
            "source_type": (
                "licensed_consolidated_tape"
                if universe_id == "sp500"
                else "licensed_exchange_feed"
            ),
            "required_fields": ["symbol", "bid", "ask", "bid_size", "ask_size", "timestamp", "exchange_or_venue"],
            "comparison": universe["benchmark_support"],
        },
        {
            "kind": "vwap",
            "feed_id_template": f"rwa_vwap:{universe_id}:{{symbol}}:{{block_size_usd}}",
            "source_type": (
                "licensed_consolidated_tape"
                if universe_id == "sp500"
                else "licensed_exchange_feed"
            ),
            "required_fields": ["symbol", "trades", "price", "size", "timestamp", "conditions"],
            "comparison": universe["benchmark_support"],
        },
    ]


def build_equity_universe_sourcing_plan(
    *,
    universe: str | None = None,
    region: str | None = None,
) -> dict[str, Any]:
    """Return sourceability and feed-build plan for major equity universes."""
    selected = []
    clean_universe = (universe or "all").strip().lower()
    clean_region = (region or "all").strip().lower()
    for universe_id, data in EQUITY_UNIVERSES.items():
        if clean_universe != "all" and universe_id != clean_universe:
            continue
        region_name = str(data["region"])
        if clean_region != "all" and not _region_matches(region_name, clean_region):
            continue
        row = {
            "universe_id": universe_id,
            **data,
            "current_registry_overlap": _current_overlap(data["sample_symbols"]),
            "feed_shapes": _feed_shapes(universe_id, data),
        }
        selected.append(row)
    if clean_universe != "all" and not selected:
        raise ValueError(f"Unsupported equity universe: {universe}")
    return {
        "summary": {
            "universe_count": len(selected),
            "s_and_p_500_coverable": any(row["universe_id"] == "sp500" for row in selected),
            "asia_universe_count": len([row for row in selected if row["region"] in ASIA_REGIONS]),
            "licensed_venue_count": len(
                {
                    venue
                    for row in selected
                    for venue in row["primary_source_venues"]
                    if "licensed" in venue or venue == "us_equity_consolidated_tape"
                }
            ),
            "licensed_source_required": True,
            "current_status": "sourceable_after_market_data_license_and_adapter_build",
        },
        "universes": selected,
        "implementation_order": [
            "Add licensed U.S. equity consolidated feed first to cover the full S&P 500 and benchmark U.S. equity RWA rows.",
            "Add APAC exchange/vendor feeds for Hong Kong, China A-shares, South Korea, Japan, Taiwan, India, and Singapore.",
            "Add Europe/UK feeds through LSE/LSEG, Euronext, and Deutsche Boerse/Xetra.",
            "Add Canada and Australia feeds through TSX/TMX and ASX vendor access.",
            "Generate bid/ask and trade-VWAP feeds from the security master, then compare against Blocksize or licensed benchmark references.",
        ],
    }


def _region_matches(region_name: str, clean_region: str) -> bool:
    normalized = clean_region.replace("_", " ").replace("-", " ")
    if normalized == "asia":
        return region_name in ASIA_REGIONS
    if normalized in {"europe", "uk", "united kingdom", "emea"}:
        return region_name in EUROPE_REGIONS
    if normalized in {"north america", "northamerica"}:
        return region_name in NORTH_AMERICA_REGIONS
    return normalized in region_name.lower()
