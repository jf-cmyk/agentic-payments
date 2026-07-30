"""Derivative, perp, and yield-venue discovery for RWA coverage expansion.

The report built here is intentionally conservative. Public market catalogs are
sourceable candidates, while derived spot/fair-value use still requires basis,
funding, expiry, liquidity, and replay checks before production promotion.
"""

from __future__ import annotations

import csv
import json
import re
import urllib.error
import urllib.request
from collections import Counter
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from src.rwa_asset_semantics import normalize_instrument_semantics


DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_DERIVATIVE_VENUE_DISCOVERY_JSON_PATH = (
    DEFAULT_REPORTS_DIR / "rwa_derivative_venue_discovery.json"
)
DEFAULT_DERIVATIVE_VENUE_DISCOVERY_CSV_PATH = (
    DEFAULT_REPORTS_DIR / "rwa_derivative_venue_discovery.csv"
)

DRIFT_PERP_MARKETS_URL = (
    "https://raw.githubusercontent.com/drift-labs/protocol-v2/master/sdk/src/constants/perpMarkets.ts"
)
DRIFT_SPOT_MARKETS_URL = (
    "https://raw.githubusercontent.com/drift-labs/protocol-v2/master/sdk/src/constants/spotMarkets.ts"
)

EQUITY_BASES = {
    "AAPL",
    "AAOI",
    "AMD",
    "AMZN",
    "AVGO",
    "COIN",
    "CRCL",
    "GOOG",
    "GOOGL",
    "HOOD",
    "META",
    "MSFT",
    "MSTR",
    "MU",
    "NFLX",
    "NVDA",
    "ORCL",
    "PLTR",
    "QQQ",
    "SMCI",
    "SPCX",
    "TSLA",
    "TSM",
}
ETF_BASES = {"DIA", "GLD", "HYG", "IWM", "QQQ", "QQQM", "SPY", "TLT", "VOO"}
METAL_BASES = {"GOLD", "PAXG", "XAG", "XAU", "XAUT", "XAUT0", "SILVER"}
COMMODITY_BASES = {"BRENT", "CL", "COPPER", "HG", "NATGAS", "NG", "OIL", "WTI", "XCU"}
TREASURY_BASES = {"BUIDL", "MINT", "MTBILL", "OUSG", "SACRED", "TBILL", "USTB", "USDY"}
YIELD_BASE_HINTS = {"APY", "PT-", "YT-", "SY-", "SUSDE", "SUSN", "SYRUP", "YN", "YIELD"}
FIAT_OR_STABLE_BASES = {
    "AUSD",
    "CASH",
    "EURC",
    "EUR",
    "PYUSD",
    "USDC",
    "USDE",
    "USDS",
    "USDT",
    "USD1",
}

DERIVATIVE_FAIR_VALUE_METHODOLOGY: dict[str, Any] = {
    "rule": "Never treat a perp or futures mark as a spot price without explicit fair-value adjustment.",
    "perpetuals": [
        "Ingest native order-book top and depth, venue index/oracle, mark price, funding rate, open interest, and volume.",
        "For an executable derivative VWAP, walk the native L2 book and label it as derivative liquidity.",
        "For a spot proxy, adjust the derivative mid by observed premium/discount and expected funding carry over the intended horizon.",
        "Keep the venue index/oracle as a separate benchmark leg; do not blend it with executable book depth.",
    ],
    "dated_futures": [
        "Map contract id, multiplier, tick size, quote currency, expiry timestamp, settlement type, and delivery/settlement calendar.",
        "Derive spot fair value from futures using cost of carry: spot = futures * exp(-(risk_free_rate + storage - convenience_yield + funding_basis - expected_income) * time_to_expiry).",
        "For equity/index futures, include dividend yield, financing curve, borrow/stock-loan costs, and contract multiplier.",
        "For commodities/metals, include storage, insurance, delivery optionality, convenience yield, and roll calendar.",
        "For FX futures, use covered-interest parity with domestic/foreign rates and settlement calendar.",
    ],
    "quality_gates": [
        "venue market id and contract specs captured",
        "book, mark, index/oracle, funding, and open-interest timestamps captured",
        "raw response payload hash is replayable",
        "30-minute freshness/tick-frequency/latency benchmark passes",
        "basis model inputs are rights-cleared and timestamp-aligned",
        "Blocksize or regulated benchmark deviation remains within configured limits",
    ],
}


DERIVATIVE_VENUE_CONFIGS: list[dict[str, Any]] = [
    {
        "venue_id": "ostium",
        "name": "Ostium",
        "region": "On-chain / EVM",
        "category": "tokenized_security",
        "market_type": "synthetic_perp",
        "instrument_type": "synthetic_perp",
        "source_tier": "synthetic_depth",
        "coverage_mode": "already_integrated_static_market_list",
        "ingestion_status": "planned_adapter",
        "access_model": "public_or_builder_api",
        "requires_auth": False,
        "requires_license": False,
        "priority": "P0",
        "endpoint_url": None,
        "parser": None,
        "notes": ["Already covered by the Ostium seed list in src.rwa_coverage."],
    },
    {
        "venue_id": "aster",
        "name": "Aster",
        "region": "On-chain / BNB Chain",
        "category": "dex_liquidity",
        "market_type": "perp",
        "instrument_type": "perp",
        "source_tier": "native_l2",
        "coverage_mode": "public_binance_compatible_exchange_info",
        "ingestion_status": "ready_to_probe",
        "access_model": "public_rest",
        "requires_auth": False,
        "requires_license": False,
        "priority": "P1",
        "endpoint_url": "https://fapi.asterdex.com/fapi/v1/exchangeInfo",
        "parser": "aster_exchange_info",
    },
    {
        "venue_id": "lighter",
        "name": "Lighter",
        "region": "On-chain / L2",
        "category": "dex_liquidity",
        "market_type": "perp_and_spot",
        "instrument_type": "perp",
        "source_tier": "native_l2",
        "coverage_mode": "public_order_book_details",
        "ingestion_status": "ready_to_probe",
        "access_model": "public_rest",
        "requires_auth": False,
        "requires_license": False,
        "priority": "P1",
        "endpoint_url": "https://mainnet.zklighter.elliot.ai/api/v1/orderBookDetails",
        "parser": "lighter_order_book_details",
    },
    {
        "venue_id": "drift",
        "name": "Drift",
        "region": "Solana",
        "category": "dex_liquidity",
        "market_type": "perp_and_spot",
        "instrument_type": "solana_perp_spot",
        "source_tier": "native_l2",
        "coverage_mode": "protocol_constants_plus_solana_rpc_live_state",
        "ingestion_status": "planned_adapter",
        "access_model": "public_constants_plus_rpc",
        "requires_auth": True,
        "requires_license": False,
        "priority": "P1",
        "endpoint_url": DRIFT_PERP_MARKETS_URL,
        "secondary_endpoint_url": DRIFT_SPOT_MARKETS_URL,
        "parser": "drift_protocol_constants",
    },
    {
        "venue_id": "grvt",
        "name": "GRVT",
        "region": "ZK / Hybrid exchange",
        "category": "dex_liquidity",
        "market_type": "perp_and_options",
        "instrument_type": "perp",
        "source_tier": "native_l2",
        "coverage_mode": "partner_or_docs_endpoint_required",
        "ingestion_status": "blocked_by_auth_or_license",
        "access_model": "partner_or_api_keyed",
        "requires_auth": True,
        "requires_license": False,
        "priority": "P2",
        "endpoint_url": None,
        "parser": None,
    },
    {
        "venue_id": "dydx",
        "name": "dYdX v4",
        "region": "dYdX Chain",
        "category": "dex_liquidity",
        "market_type": "perp",
        "instrument_type": "perp",
        "source_tier": "native_l2",
        "coverage_mode": "public_indexer_perpetual_markets",
        "ingestion_status": "ready_to_probe",
        "access_model": "public_rest",
        "requires_auth": False,
        "requires_license": False,
        "priority": "P1",
        "endpoint_url": "https://indexer.dydx.trade/v4/perpetualMarkets",
        "parser": "dydx_perpetual_markets",
    },
    {
        "venue_id": "extended",
        "name": "Extended",
        "region": "StarkEx / Hybrid exchange",
        "category": "dex_liquidity",
        "market_type": "perp",
        "instrument_type": "perp",
        "source_tier": "native_l2",
        "coverage_mode": "catalog_probe_required",
        "ingestion_status": "blocked_by_auth_or_license",
        "access_model": "api_endpoint_unconfirmed",
        "requires_auth": True,
        "requires_license": False,
        "priority": "P2",
        "endpoint_url": None,
        "parser": None,
    },
    {
        "venue_id": "pacifica",
        "name": "Pacifica",
        "region": "On-chain / Hybrid exchange",
        "category": "dex_liquidity",
        "market_type": "perp",
        "instrument_type": "perp",
        "source_tier": "native_l2",
        "coverage_mode": "catalog_probe_required",
        "ingestion_status": "blocked_by_auth_or_license",
        "access_model": "api_endpoint_unconfirmed",
        "requires_auth": True,
        "requires_license": False,
        "priority": "P2",
        "endpoint_url": None,
        "parser": None,
    },
    {
        "venue_id": "apex_omni",
        "name": "ApeX Omni",
        "region": "On-chain / Multi-chain",
        "category": "dex_liquidity",
        "market_type": "perp_stock_prediction",
        "instrument_type": "perp",
        "source_tier": "native_l2",
        "coverage_mode": "public_symbols_catalog",
        "ingestion_status": "ready_to_probe",
        "access_model": "public_rest",
        "requires_auth": False,
        "requires_license": False,
        "priority": "P1",
        "endpoint_url": "https://omni.apex.exchange/api/v3/symbols",
        "parser": "apex_omni_symbols",
    },
    {
        "venue_id": "vest_exchange",
        "name": "Vest Exchange",
        "region": "On-chain",
        "category": "dex_liquidity",
        "market_type": "perp",
        "instrument_type": "perp",
        "source_tier": "native_l2",
        "coverage_mode": "partner_or_docs_endpoint_required",
        "ingestion_status": "blocked_by_auth_or_license",
        "access_model": "partner_or_api_keyed",
        "requires_auth": True,
        "requires_license": False,
        "priority": "P2",
        "endpoint_url": None,
        "parser": None,
    },
    {
        "venue_id": "helix",
        "name": "Helix / Injective",
        "region": "Injective",
        "category": "dex_liquidity",
        "market_type": "perp_and_spot",
        "instrument_type": "injective_derivative_market",
        "source_tier": "native_l2",
        "coverage_mode": "grpc_or_indexer_endpoint_required",
        "ingestion_status": "planned_adapter",
        "access_model": "public_grpc_gateway_or_indexer",
        "requires_auth": True,
        "requires_license": False,
        "priority": "P2",
        "endpoint_url": None,
        "parser": None,
    },
    {
        "venue_id": "enclavex",
        "name": "EnclaveX",
        "region": "Confidential / Hybrid exchange",
        "category": "dex_liquidity",
        "market_type": "perp",
        "instrument_type": "perp",
        "source_tier": "native_l2",
        "coverage_mode": "partner_or_docs_endpoint_required",
        "ingestion_status": "blocked_by_auth_or_license",
        "access_model": "partner_or_api_keyed",
        "requires_auth": True,
        "requires_license": False,
        "priority": "P2",
        "endpoint_url": None,
        "parser": None,
    },
    {
        "venue_id": "synfutures",
        "name": "SynFutures",
        "region": "On-chain / EVM",
        "category": "dex_liquidity",
        "market_type": "perp_and_futures",
        "instrument_type": "perp",
        "source_tier": "native_l2",
        "coverage_mode": "subgraph_or_api_endpoint_required",
        "ingestion_status": "planned_adapter",
        "access_model": "public_subgraph_or_partner_api",
        "requires_auth": True,
        "requires_license": False,
        "priority": "P2",
        "endpoint_url": None,
        "parser": None,
    },
    {
        "venue_id": "myx",
        "name": "MYX",
        "region": "On-chain",
        "category": "dex_liquidity",
        "market_type": "perp",
        "instrument_type": "perp",
        "source_tier": "native_l2",
        "coverage_mode": "partner_or_docs_endpoint_required",
        "ingestion_status": "blocked_by_auth_or_license",
        "access_model": "partner_or_api_keyed",
        "requires_auth": True,
        "requires_license": False,
        "priority": "P2",
        "endpoint_url": None,
        "parser": None,
    },
    {
        "venue_id": "orderly",
        "name": "Orderly Network",
        "region": "Omnichain",
        "category": "dex_liquidity",
        "market_type": "perp",
        "instrument_type": "perp",
        "source_tier": "native_l2",
        "coverage_mode": "public_info_rows",
        "ingestion_status": "ready_to_probe",
        "access_model": "public_rest",
        "requires_auth": False,
        "requires_license": False,
        "priority": "P1",
        "endpoint_url": "https://api.orderly.org/v1/public/info",
        "parser": "orderly_public_info",
    },
    {
        "venue_id": "derive",
        "name": "Derive / Lyra",
        "region": "EVM / Options and perps",
        "category": "dex_liquidity",
        "market_type": "options_perps_spot_reference",
        "instrument_type": "options_perps",
        "source_tier": "price_stream_no_book",
        "coverage_mode": "public_currency_catalog",
        "ingestion_status": "ready_to_probe",
        "access_model": "public_rest",
        "requires_auth": False,
        "requires_license": False,
        "priority": "P1",
        "endpoint_url": "https://api.lyra.finance/public/get_all_currencies",
        "parser": "derive_currencies",
    },
    {
        "venue_id": "aevo",
        "name": "Aevo",
        "region": "On-chain / Options and perps",
        "category": "dex_liquidity",
        "market_type": "options_and_perps",
        "instrument_type": "options_perps",
        "source_tier": "native_l2",
        "coverage_mode": "public_markets_catalog",
        "ingestion_status": "ready_to_probe",
        "access_model": "public_rest",
        "requires_auth": False,
        "requires_license": False,
        "priority": "P1",
        "endpoint_url": "https://api.aevo.xyz/markets",
        "parser": "aevo_markets",
    },
    {
        "venue_id": "plume",
        "name": "Plume",
        "region": "RWA chain / ecosystem",
        "category": "dex_liquidity",
        "market_type": "rwa_ecosystem",
        "instrument_type": "chain_ecosystem",
        "source_tier": "onchain_clmm_pool",
        "coverage_mode": "ecosystem_pool_and_token_discovery_required",
        "ingestion_status": "planned_adapter",
        "access_model": "public_rpc_indexer_plus_protocol_apis",
        "requires_auth": True,
        "requires_license": False,
        "priority": "P2",
        "endpoint_url": None,
        "parser": None,
    },
    {
        "venue_id": "coinbase_ventures",
        "name": "Coinbase Ventures",
        "region": "Global",
        "category": "market_data_vendor",
        "market_type": "not_a_tradeable_venue",
        "instrument_type": "not_market_operator",
        "source_tier": "benchmark_reference",
        "coverage_mode": "not_standalone_market_data_venue",
        "ingestion_status": "blocked_by_auth_or_license",
        "access_model": "not_applicable_use_coinbase_exchange_or_derivatives_instead",
        "requires_auth": True,
        "requires_license": True,
        "priority": "P3",
        "endpoint_url": None,
        "parser": None,
    },
    {
        "venue_id": "solana",
        "name": "Solana ecosystem",
        "region": "Solana",
        "category": "dex_liquidity",
        "market_type": "chain_ecosystem",
        "instrument_type": "chain_ecosystem",
        "source_tier": "onchain_clmm_pool",
        "coverage_mode": "covered_through_jupiter_raydium_orca_meteora_drift_pendle_like_protocols",
        "ingestion_status": "planned_adapter",
        "access_model": "public_rpc_indexer",
        "requires_auth": True,
        "requires_license": False,
        "priority": "P1",
        "endpoint_url": None,
        "parser": None,
    },
    {
        "venue_id": "pendle",
        "name": "Pendle",
        "region": "Ethereum / EVM",
        "category": "dex_liquidity",
        "market_type": "yield_token_market",
        "instrument_type": "yield_pool",
        "source_tier": "onchain_yield_market",
        "coverage_mode": "public_active_markets_catalog",
        "ingestion_status": "ready_to_probe",
        "access_model": "public_rest_plus_rpc",
        "requires_auth": False,
        "requires_license": False,
        "priority": "P2",
        "endpoint_url": "https://api-v2.pendle.finance/core/v1/1/markets/active",
        "parser": "pendle_active_markets",
    },
    {
        "venue_id": "tradible",
        "name": "Tradible",
        "region": "Tokenized RWA",
        "category": "issuer_nav_reserve",
        "market_type": "tokenized_rwa_market",
        "instrument_type": "rwa_primary_secondary",
        "source_tier": "issuer_reference",
        "coverage_mode": "partner_or_issuer_endpoint_required",
        "ingestion_status": "blocked_by_auth_or_license",
        "access_model": "partner_or_api_keyed",
        "requires_auth": True,
        "requires_license": True,
        "priority": "P2",
        "endpoint_url": None,
        "parser": None,
    },
    {
        "venue_id": "cork",
        "name": "Cork Protocol",
        "region": "On-chain / Yield derivatives",
        "category": "dex_liquidity",
        "market_type": "yield_derivative_market",
        "instrument_type": "yield_derivative",
        "source_tier": "onchain_yield_market",
        "coverage_mode": "subgraph_or_protocol_endpoint_required",
        "ingestion_status": "planned_adapter",
        "access_model": "public_rpc_or_subgraph",
        "requires_auth": True,
        "requires_license": False,
        "priority": "P2",
        "endpoint_url": None,
        "parser": None,
    },
]


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _clean_base(value: Any) -> str:
    raw = str(value or "").strip().upper()
    raw = raw.replace("-PERP", "").replace("_PERP", "")
    raw = raw.removeprefix("PERP_")
    return raw.replace("/", "").replace(" ", "")


def _asset_id(symbol: str) -> str:
    return symbol.replace("-", "/").split("/", 1)[0].upper()


def _fx_like(base: str) -> bool:
    return bool(re.fullmatch(r"[A-Z]{6}", base)) and (
        base.startswith(("USD", "EUR", "GBP", "AUD", "NZD"))
        or base.endswith(("USD", "JPY", "CAD", "CHF", "CNH", "KRW", "MXN"))
    )


def _classify_asset(base: str, *, name: str = "", market_type: str = "", instrument_type: str = "") -> tuple[str, str, str]:
    clean = _clean_base(base)
    lower_name = name.lower()
    lower_market = market_type.lower()
    lower_instrument = instrument_type.lower()
    if "prediction" in lower_market or "prediction" in lower_instrument or clean.endswith("-BET"):
        return "prediction", "derivative", "prediction_market"
    if clean in ETF_BASES or " etf" in f" {lower_name}":
        return "etf", "rwa_or_traditional", "etf_or_index_derivative"
    if clean in METAL_BASES or "gold" in lower_name or "silver" in lower_name:
        return "metal", "rwa_or_traditional", "metal_or_tokenized_metal"
    if clean in COMMODITY_BASES or any(word in lower_name for word in ("crude", "natural gas", "copper", "oil")):
        return "commodity", "rwa_or_traditional", "commodity_derivative"
    if clean in EQUITY_BASES or lower_market == "equity" or lower_instrument == "stock":
        subtype = "private_market_or_equity_derivative" if clean == "SPCX" else "equity_derivative"
        return "equity", "rwa_or_traditional", subtype
    if clean in TREASURY_BASES or "tbill" in clean or "treasury" in lower_name:
        return "treasury_fund", "rwa_or_traditional", "tokenized_treasury_or_fund_yield"
    if clean in YIELD_BASE_HINTS or clean.startswith(("PT-", "YT-", "SY-")) or lower_market == "yield":
        return "yield_token", "rwa_or_traditional", "yield_token_or_principal_token"
    if clean in FIAT_OR_STABLE_BASES or _fx_like(clean):
        return "fx", "rwa_or_traditional", "stablecoin_or_fx_reference"
    if lower_market in {"commodity", "equity"}:
        return lower_market, "rwa_or_traditional", f"{lower_market}_derivative"
    return "crypto", "crypto", "crypto_derivative_or_spot"


def _market_record(
    *,
    venue: str,
    symbol: str,
    venue_symbol: str,
    market_type: str,
    status: str,
    is_active: bool,
    source_type: str = "native_l2",
    venue_market_id: Any = None,
    name: str = "",
    quote: str = "USD",
    instrument_type: str = "",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    base = _asset_id(symbol)
    asset_class, family, subtype = _classify_asset(
        base,
        name=name,
        market_type=market_type,
        instrument_type=instrument_type,
    )
    derivative_like = market_type not in {"spot", "yield_token_market"} or "perp" in instrument_type.lower()
    option_contract = "option" in instrument_type.lower() or market_type == "option"
    if source_type == "onchain_yield_market":
        vwap_support = "not_vwap_suitable_until_yield_pool_state_adapter"
        bidask_support = "not_bidask_suitable_until_yield_pool_state_adapter"
    elif option_contract:
        vwap_support = "not_spot_vwap_suitable_option_book_only"
        bidask_support = "option_top_of_book_not_spot_bidask"
    elif source_type == "price_stream_no_book":
        vwap_support = "not_l2_vwap_suitable"
        bidask_support = "mark_or_spot_reference"
    else:
        vwap_support = (
            "native_l2_derivative_block_vwap_requires_basis_adjustment"
            if derivative_like
            else "native_l2_block_vwap"
        )
        bidask_support = (
            "native_derivative_top_of_book_requires_basis_adjustment"
            if derivative_like
            else "native_top_of_book"
        )
    coverage_status = (
        f"{venue}_{market_type}_active_candidate_requires_freshness_liquidity_basis_and_benchmark_validation"
        if is_active
        else f"{venue}_{market_type}_inactive_or_settled_not_sourceable"
    )
    row_metadata = {
        "venue_symbol": venue_symbol,
        "venue_market_id": venue_market_id,
        "market_type": market_type,
        "instrument_type": instrument_type or market_type,
        "status": status,
        "quote": quote,
        "asset_subtype": subtype,
        "pricing_methodology": "perp_or_futures_fair_value_required" if derivative_like else "native_spot_or_pool_price",
        "fair_value_policy": "raw derivative price is supplemental until basis/funding/carry adjustment passes",
    }
    if metadata:
        row_metadata.update(metadata)
    return normalize_instrument_semantics({
        "symbol": symbol,
        "asset_id": base,
        "asset_class": asset_class,
        "asset_family": family,
        "venue": venue,
        "source_type": source_type,
        "coverage_status": coverage_status,
        "vwap_support": vwap_support,
        "bidask_support": bidask_support,
        "metadata": row_metadata,
        "is_active": is_active,
    })


def _fetch_url(url: str) -> tuple[Any, dict[str, Any]]:
    request = urllib.request.Request(
        url,
        headers={
            "accept": "application/json,text/plain,*/*",
            "user-agent": "BlocksizeRWADerivativeVenueDiscovery/1.0",
        },
        method="GET",
    )
    started = _utc_now_iso()
    try:
        with urllib.request.urlopen(request, timeout=35) as response:
            raw = response.read()
            status_code = int(response.status)
            content_type = str(response.headers.get("content-type") or "")
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        return None, {
            "fetch_status": "failed",
            "status_code": getattr(exc, "code", None),
            "error": str(exc),
            "started_at": started,
            "completed_at": _utc_now_iso(),
            "url": url,
        }
    text = raw.decode("utf-8", errors="replace")
    payload: Any = text
    if "json" in content_type or text.strip().startswith(("{", "[")):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            payload = text
    return payload, {
        "fetch_status": "ok",
        "status_code": status_code,
        "bytes": len(raw),
        "content_type": content_type,
        "started_at": started,
        "completed_at": _utc_now_iso(),
        "url": url,
    }


def _parse_dydx(payload: Any, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    markets = payload.get("markets") if isinstance(payload, dict) else {}
    rows = []
    for ticker, market in (markets or {}).items():
        if not isinstance(market, dict):
            continue
        display = str(market.get("ticker") or ticker).replace("-", "/").upper()
        status = str(market.get("status") or "")
        rows.append(
            _market_record(
                venue=cfg["venue_id"],
                symbol=display,
                venue_symbol=str(market.get("ticker") or ticker),
                venue_market_id=market.get("clobPairId"),
                market_type="perp",
                instrument_type="perpetual",
                status=status,
                is_active=status == "ACTIVE",
                metadata={
                    "oracle_price": market.get("oraclePrice"),
                    "next_funding_rate": market.get("nextFundingRate"),
                    "open_interest": market.get("openInterest"),
                    "trades_24h": market.get("trades24H"),
                    "volume_24h": market.get("volume24H"),
                },
            )
        )
    return rows


def _parse_orderly(payload: Any, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else {}
    rows = data.get("rows") if isinstance(data, dict) else []
    parsed = []
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        raw_symbol = str(item.get("symbol") or "")
        parts = raw_symbol.removeprefix("PERP_").split("_")
        quote = "USDC"
        if "USDC" in parts:
            quote_index = parts.index("USDC")
            base = "_".join(parts[:quote_index])
        else:
            base = str(item.get("display_symbol_name") or parts[0] or raw_symbol)
        display = f"{base.upper()}/{quote}"
        status = str(item.get("status") or "ACTIVE")
        parsed.append(
            _market_record(
                venue=cfg["venue_id"],
                symbol=display,
                venue_symbol=raw_symbol,
                venue_market_id=raw_symbol,
                market_type="perp",
                instrument_type="perpetual",
                status=status,
                is_active=status == "ACTIVE",
                quote=quote,
                metadata={
                    "display_symbol_name": item.get("display_symbol_name"),
                    "funding_period": item.get("funding_period"),
                    "cap_funding": item.get("cap_funding"),
                    "floor_funding": item.get("floor_funding"),
                    "price_range": item.get("price_range"),
                },
            )
        )
    return parsed


def _parse_aevo(payload: Any, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload if isinstance(payload, list) else []
    parsed = []
    for item in rows:
        if not isinstance(item, dict):
            continue
        base = str(item.get("underlying_asset") or item.get("instrument_name") or "").upper()
        quote = str(item.get("quote_asset") or "USDC").upper()
        instrument_type = str(item.get("instrument_type") or "")
        market_type = "option" if instrument_type == "OPTION" else "perp"
        parsed.append(
            _market_record(
                venue=cfg["venue_id"],
                symbol=f"{base}/{quote}",
                venue_symbol=str(item.get("instrument_name") or base),
                venue_market_id=item.get("instrument_id"),
                market_type=market_type,
                instrument_type=instrument_type,
                status="ACTIVE" if item.get("is_active") else "INACTIVE",
                is_active=bool(item.get("is_active")),
                quote=quote,
                metadata={
                    "is_rwa": bool(item.get("is_rwa")),
                    "aevo_market_type": item.get("market_type"),
                    "mark_price": item.get("mark_price"),
                    "index_price": item.get("index_price"),
                    "max_leverage": item.get("max_leverage"),
                    "expiry": item.get("expiry"),
                },
            )
        )
    return parsed


def _split_compact_pair(symbol: str, quote_candidates: tuple[str, ...] = ("USDT", "USDC", "USD")) -> tuple[str, str]:
    upper = symbol.upper()
    for quote in quote_candidates:
        if upper.endswith(quote) and len(upper) > len(quote):
            return upper[: -len(quote)], quote
    return upper, "USD"


def _parse_aster(payload: Any, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("symbols") if isinstance(payload, dict) else []
    parsed = []
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        base = str(item.get("baseAsset") or "")
        quote = str(item.get("quoteAsset") or item.get("marginAsset") or "")
        if not base or not quote:
            base, quote = _split_compact_pair(str(item.get("symbol") or ""))
        status = str(item.get("status") or "")
        contract_type = str(item.get("contractType") or "PERPETUAL")
        parsed.append(
            _market_record(
                venue=cfg["venue_id"],
                symbol=f"{base.upper()}/{quote.upper()}",
                venue_symbol=str(item.get("symbol") or f"{base}{quote}"),
                venue_market_id=item.get("pair") or item.get("symbol"),
                market_type="perp",
                instrument_type=contract_type,
                status=status,
                is_active=status == "TRADING",
                quote=quote.upper(),
                name=str(item.get("name") or ""),
                metadata={
                    "contract_type": contract_type,
                    "delivery_date": item.get("deliveryDate"),
                    "onboard_date": item.get("onboardDate"),
                    "underlying_type": item.get("underlyingType"),
                    "maint_margin_percent": item.get("maintMarginPercent"),
                },
            )
        )
    return parsed


def _parse_lighter(payload: Any, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    source_rows = [
        *((payload.get("order_book_details") or [])),
        *((payload.get("spot_order_book_details") or [])),
    ]
    parsed = []
    for item in source_rows:
        if not isinstance(item, dict):
            continue
        base = str(item.get("symbol") or "").upper()
        market_type = str(item.get("market_type") or "perp").lower()
        quote = "USDC" if market_type == "spot" else "USD"
        status = str(item.get("status") or "")
        parsed.append(
            _market_record(
                venue=cfg["venue_id"],
                symbol=f"{base}/{quote}",
                venue_symbol=base,
                venue_market_id=item.get("market_id"),
                market_type=market_type,
                instrument_type=market_type,
                status=status,
                is_active=status == "active",
                quote=quote,
                metadata={
                    "index_price": item.get("index_price"),
                    "mark_price": item.get("mark_price"),
                    "taker_fee": item.get("taker_fee"),
                    "maker_fee": item.get("maker_fee"),
                    "min_quote_amount": item.get("min_quote_amount"),
                },
            )
        )
    return parsed


def _parse_apex(payload: Any, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    data = payload.get("data") if isinstance(payload, dict) else {}
    contract = data.get("contractConfig") if isinstance(data, dict) else {}
    parsed = []
    buckets = [
        ("perp", "perpetual", contract.get("perpetualContract") if isinstance(contract, dict) else []),
        ("stock", "stock", contract.get("stockContract") if isinstance(contract, dict) else []),
        ("prediction", "prediction", contract.get("predictionContract") if isinstance(contract, dict) else []),
    ]
    for market_type, instrument_type, rows in buckets:
        for item in rows or []:
            if not isinstance(item, dict):
                continue
            base = str(item.get("baseTokenId") or "").upper()
            quote = str(item.get("settleAssetId") or "USDT").upper()
            enabled = bool(item.get("enableTrade")) and bool(item.get("enableDisplay"))
            parsed.append(
                _market_record(
                    venue=cfg["venue_id"],
                    symbol=f"{base}/{quote}",
                    venue_symbol=str(item.get("symbol") or f"{base}-{quote}"),
                    venue_market_id=item.get("l2PairId") or item.get("crossSymbolId"),
                    market_type=market_type,
                    instrument_type=instrument_type,
                    status="ACTIVE" if enabled else "INACTIVE",
                    is_active=enabled,
                    quote=quote,
                    name=str(item.get("tokenName") or ""),
                    metadata={
                        "cross_symbol_name": item.get("crossSymbolName"),
                        "token_name": item.get("tokenName"),
                        "category": item.get("category"),
                        "display_max_leverage": item.get("displayMaxLeverage"),
                        "funding_interest_rate": item.get("fundingInterestRate"),
                        "contract_type": item.get("contractType"),
                    },
                )
            )
    return parsed


def _parse_pendle(payload: Any, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("markets") if isinstance(payload, dict) else []
    parsed = []
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").upper()
        details = item.get("details") if isinstance(item.get("details"), dict) else {}
        parsed.append(
            _market_record(
                venue=cfg["venue_id"],
                symbol=f"{name}/PT",
                venue_symbol=name,
                venue_market_id=item.get("address"),
                market_type="yield_token_market",
                instrument_type="pendle_pt_yt_market",
                status="ACTIVE",
                is_active=True,
                source_type="onchain_yield_market",
                quote="PT",
                name=name,
                metadata={
                    "market_address": item.get("address"),
                    "expiry": item.get("expiry"),
                    "pt": item.get("pt"),
                    "yt": item.get("yt"),
                    "sy": item.get("sy"),
                    "underlying_asset": item.get("underlyingAsset"),
                    "liquidity": details.get("liquidity"),
                    "implied_apy": details.get("impliedApy"),
                    "pendle_apy": details.get("pendleApy"),
                    "fee_rate": details.get("feeRate"),
                    "category_ids": item.get("categoryIds"),
                },
            )
        )
    return parsed


def _parse_derive(payload: Any, cfg: dict[str, Any]) -> list[dict[str, Any]]:
    rows = payload.get("result") if isinstance(payload, dict) else []
    parsed = []
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        currency = str(item.get("currency") or "").upper()
        if not currency:
            continue
        instrument_types = item.get("instrument_types") if isinstance(item.get("instrument_types"), list) else []
        market_type = "options_perps_spot_reference" if instrument_types else "currency_reference"
        parsed.append(
            _market_record(
                venue=cfg["venue_id"],
                symbol=f"{currency}/USDC",
                venue_symbol=currency,
                venue_market_id=currency,
                market_type=market_type,
                instrument_type=",".join(str(value) for value in instrument_types),
                status="ACTIVE",
                is_active=True,
                source_type="price_stream_no_book",
                quote="USDC",
                metadata={
                    "instrument_types": instrument_types,
                    "derive_market_type": item.get("market_type"),
                    "spot_price": item.get("spot_price"),
                    "spot_price_24h": item.get("spot_price_24h"),
                    "borrow_apy": item.get("borrow_apy"),
                    "supply_apy": item.get("supply_apy"),
                },
            )
        )
    return parsed


def _section(text: str, marker: str) -> str:
    start = text.find(marker)
    if start < 0:
        return ""
    after = text.find("[", start)
    end = text.find("\n];", after)
    if after < 0 or end < 0:
        return ""
    return text[after:end]


def _ts_objects(section: str) -> list[str]:
    return [match.group(1) for match in re.finditer(r"\{\s*(.*?)\n\s*\},", section, re.S)]


def _ts_string(block: str, key: str) -> str | None:
    match = re.search(rf"{re.escape(key)}:\s*'([^']+)'", block)
    return match.group(1) if match else None


def _ts_number(block: str, key: str) -> int | None:
    match = re.search(rf"{re.escape(key)}:\s*([0-9]+)", block)
    return int(match.group(1)) if match else None


def _parse_drift_protocol_constants(payload: Any, cfg: dict[str, Any], secondary_payload: Any = None) -> list[dict[str, Any]]:
    perp_text = payload if isinstance(payload, str) else ""
    spot_text = secondary_payload if isinstance(secondary_payload, str) else ""
    parsed = []
    for block in _ts_objects(_section(perp_text, "MainnetPerpMarkets")):
        symbol = _ts_string(block, "symbol") or ""
        base = _ts_string(block, "baseAssetSymbol") or symbol.replace("-PERP", "")
        index = _ts_number(block, "marketIndex")
        status = _ts_string(block, "marketStatus") or "ACTIVE"
        parsed.append(
            _market_record(
                venue=cfg["venue_id"],
                symbol=f"{base.upper()}/USD",
                venue_symbol=symbol,
                venue_market_id=index,
                market_type="perp",
                instrument_type="perpetual",
                status=status,
                is_active=status == "ACTIVE",
                name=_ts_string(block, "fullName") or base,
                metadata={
                    "market_index": index,
                    "base_asset_symbol": base,
                    "pyth_feed_id": _ts_string(block, "pythFeedId"),
                    "live_state_requirement": "solana_rpc_dlob_or_market_account_replay",
                },
            )
        )
    for block in _ts_objects(_section(spot_text, "MainnetSpotMarkets")):
        symbol = _ts_string(block, "symbol") or ""
        if not symbol:
            continue
        index = _ts_number(block, "marketIndex")
        status = _ts_string(block, "marketStatus") or "ACTIVE"
        parsed.append(
            _market_record(
                venue=cfg["venue_id"],
                symbol=f"{symbol.upper()}/USDC",
                venue_symbol=symbol,
                venue_market_id=index,
                market_type="spot",
                instrument_type="spot_market_or_collateral",
                status=status,
                is_active=status == "ACTIVE",
                quote="USDC",
                metadata={
                    "market_index": index,
                    "pyth_feed_id": _ts_string(block, "pythFeedId"),
                    "mint": _ts_string(block, "mint"),
                    "live_state_requirement": "solana_rpc_spot_market_account_and_oracle_replay",
                },
            )
        )
    return parsed


PARSERS: dict[str, Callable[..., list[dict[str, Any]]]] = {
    "dydx_perpetual_markets": _parse_dydx,
    "orderly_public_info": _parse_orderly,
    "aevo_markets": _parse_aevo,
    "aster_exchange_info": _parse_aster,
    "lighter_order_book_details": _parse_lighter,
    "apex_omni_symbols": _parse_apex,
    "pendle_active_markets": _parse_pendle,
    "derive_currencies": _parse_derive,
    "drift_protocol_constants": _parse_drift_protocol_constants,
}


def _descriptor(config: dict[str, Any], index: int) -> dict[str, Any]:
    return {
        "id": config["venue_id"],
        "name": config["name"],
        "priority": 40 + index,
        "status": "derivatives_expansion",
        "instrument_type": config["instrument_type"],
        "source_tier": config["source_tier"],
        "data": [
            "market_catalog",
            "l1_bid_ask",
            "l2_order_book",
            "trades",
            "mark",
            "index_or_oracle",
            "funding",
            "open_interest",
            "contract_specs",
        ],
        "vwap_method": "walk native derivative L2 book for derivative VWAP; derive spot/fair value only after funding, basis, and contract-spec adjustment",
        "bidask_method": "native derivative top of book, explicitly labeled as derivative until fair-value adjustment passes",
        "coverage_mode": config["coverage_mode"],
        "requires_auth": bool(config["requires_auth"]),
        "requires_license": bool(config["requires_license"]),
        "legal_note": "Derivative venues are supplemental candidates until source rights, replay payloads, freshness, liquidity, manipulation, and fair-value methodology gates pass.",
    }


DERIVATIVE_VENUE_DESCRIPTORS: list[dict[str, Any]] = [
    _descriptor(config, index) for index, config in enumerate(DERIVATIVE_VENUE_CONFIGS, start=1)
]


DERIVATIVE_PROVIDER_ROWS: list[dict[str, Any]] = [
    {
        "provider_id": config["venue_id"],
        "name": config["name"],
        "category": config["category"],
        "region": config["region"],
        "asset_classes": ["crypto", "equity", "etf", "fx", "commodity", "metal", "treasury_fund", "yield_token", "option"],
        "coverage_scope": config["coverage_mode"],
        "example_symbols": [],
        "target_source_types": [config["source_tier"], "futures_fair_value"],
        "endpoint_families": ["market_catalog", "order_book", "trades", "mark", "index_oracle", "funding", "contract_specs"],
        "access_model": config["access_model"],
        "requires_auth": bool(config["requires_auth"]),
        "requires_license": bool(config["requires_license"]),
        "ingestion_status": config["ingestion_status"],
        "priority": config["priority"],
        "adapter_lane": "derivative_venue_catalog_and_market_data_adapter",
        "promotion_gate": "market id, contract specs, book replay, funding/basis model, freshness, liquidity, manipulation, and rights gates",
    }
    for config in DERIVATIVE_VENUE_CONFIGS
]


DERIVATIVE_SOURCE_RIGHTS_OVERRIDES: dict[str, dict[str, Any]] = {
    config["venue_id"]: {
        "category": config["category"],
        "required_policy_env": ["RWA_MARKET_DATA_POLICY_ACK"],
        "required_dependency_ids": ["redistribution_policy"],
        "rights_status_if_configured": "internal_benchmark_allowed_pending_redistribution",
        "production_requirement": "venue API/data-use terms, derivative-data labeling, derived-data policy, and redistribution signoff",
    }
    for config in DERIVATIVE_VENUE_CONFIGS
}


def build_derivative_venue_discovery() -> dict[str, Any]:
    """Fetch public catalogs where available and normalize derivative coverage rows."""
    venues: list[dict[str, Any]] = []
    all_market_rows: list[dict[str, Any]] = []
    coverage_rows: list[dict[str, Any]] = []
    for config in DERIVATIVE_VENUE_CONFIGS:
        payload = None
        secondary_payload = None
        fetches: list[dict[str, Any]] = []
        parser_name = config.get("parser")
        if config.get("endpoint_url"):
            payload, fetch_status = _fetch_url(str(config["endpoint_url"]))
            fetches.append(fetch_status)
        if config.get("secondary_endpoint_url"):
            secondary_payload, fetch_status = _fetch_url(str(config["secondary_endpoint_url"]))
            fetches.append(fetch_status)
        rows: list[dict[str, Any]] = []
        parse_error = None
        if parser_name and payload is not None and all(fetch.get("fetch_status") == "ok" for fetch in fetches):
            try:
                parser = PARSERS[str(parser_name)]
                if parser_name == "drift_protocol_constants":
                    rows = parser(payload, config, secondary_payload)
                else:
                    rows = parser(payload, config)
            except Exception as exc:  # pragma: no cover - defensive report path
                parse_error = str(exc)
        active_rows = [row for row in rows if row.get("is_active")]
        venue_status = (
            "catalog_fetched"
            if rows
            else "fetch_failed"
            if fetches and any(fetch.get("fetch_status") != "ok" for fetch in fetches)
            else "parse_failed"
            if parse_error
            else "no_public_catalog_configured"
        )
        venue_row = {
            "venue_id": config["venue_id"],
            "name": config["name"],
            "region": config["region"],
            "category": config["category"],
            "market_type": config["market_type"],
            "instrument_type": config["instrument_type"],
            "source_tier": config["source_tier"],
            "coverage_mode": config["coverage_mode"],
            "ingestion_status": config["ingestion_status"],
            "access_model": config["access_model"],
            "requires_auth": bool(config["requires_auth"]),
            "requires_license": bool(config["requires_license"]),
            "endpoint_url": config.get("endpoint_url"),
            "secondary_endpoint_url": config.get("secondary_endpoint_url"),
            "parser": parser_name,
            "fetches": fetches,
            "parse_error": parse_error,
            "discovery_status": venue_status,
            "market_row_count": len(rows),
            "active_market_row_count": len(active_rows),
            "sourceable_now": config["ingestion_status"] == "ready_to_probe" and bool(active_rows),
            "next_action": _venue_next_action(config, venue_status, active_rows),
        }
        venues.append(venue_row)
        all_market_rows.extend(rows)
        coverage_rows.extend(row for row in active_rows if config["venue_id"] != "ostium")

    by_venue = Counter(str(row["venue"]) for row in coverage_rows)
    by_asset_class = Counter(str(row["asset_class"]) for row in coverage_rows)
    by_family = Counter(str(row["asset_family"]) for row in coverage_rows)
    by_source_type = Counter(str(row["source_type"]) for row in coverage_rows)
    by_market_type = Counter(str(row.get("metadata", {}).get("market_type")) for row in coverage_rows)
    by_discovery_status = Counter(str(row["discovery_status"]) for row in venues)
    return reclassify_derivative_venue_discovery_report({
        "product": "rwa_derivative_venue_discovery",
        "generated_at": _utc_now_iso(),
        "summary": {
            "venue_count": len(venues),
            "coverage_row_count": len(coverage_rows),
            "market_row_count": len(all_market_rows),
            "sourceable_venue_count": sum(1 for row in venues if row["sourceable_now"]),
            "blocked_or_gated_venue_count": sum(1 for row in venues if not row["sourceable_now"]),
            "rwa_or_traditional_coverage_rows": by_family.get("rwa_or_traditional", 0),
            "crypto_coverage_rows": by_family.get("crypto", 0),
            "derivative_only_rows": by_family.get("derivative", 0),
            "by_venue": dict(sorted(by_venue.items())),
            "by_asset_class": dict(sorted(by_asset_class.items())),
            "by_family": dict(sorted(by_family.items())),
            "by_source_type": dict(sorted(by_source_type.items())),
            "by_market_type": dict(sorted(by_market_type.items())),
            "by_discovery_status": dict(sorted(by_discovery_status.items())),
        },
        "policy": {
            "promotion_rule": "Rows are candidates only; production promotion requires all feed discovery gates plus fair-value validation for derivative-derived prices.",
            "spot_derivation_rule": DERIVATIVE_FAIR_VALUE_METHODOLOGY["rule"],
            "blocked_venue_rule": "Venue registry rows with no confirmed public catalog remain access or catalog-probe blockers, not sourced coverage.",
        },
        "fair_value_methodology": deepcopy(DERIVATIVE_FAIR_VALUE_METHODOLOGY),
        "venues": venues,
        "coverage_rows": sorted(
            [
                {key: value for key, value in row.items() if key != "is_active"}
                for row in coverage_rows
            ],
            key=lambda row: (str(row["venue"]), str(row["asset_class"]), str(row["asset_id"]), str(row["symbol"])),
        ),
        "market_rows": sorted(
            [
                {key: value for key, value in row.items() if key != "is_active"}
                for row in all_market_rows
            ],
            key=lambda row: (str(row["venue"]), str(row["asset_id"]), str(row["symbol"])),
        ),
    })


def _venue_next_action(config: dict[str, Any], status: str, active_rows: list[dict[str, Any]]) -> str:
    if active_rows and config["ingestion_status"] == "ready_to_probe":
        return "Run order-book/trade/funding probes, capture replay payloads, then benchmark 30-minute freshness and basis."
    if status == "fetch_failed":
        return "Confirm the current public API host/path or provision partner/API access before sourcing."
    if config["venue_id"] in {"coinbase_ventures", "solana", "plume"}:
        return "Treat as ecosystem/company context; source data through concrete venues, pools, protocols, or licensed feeds."
    if config["requires_auth"]:
        return "Provision API/RPC/indexer access and attach venue terms evidence before live sourcing."
    return "Add a confirmed catalog endpoint or protocol subgraph, then rerun discovery."


def _derivative_identity_quality(
    rows: list[dict[str, Any]],
) -> dict[str, Any]:
    raw_classes: dict[str, set[str]] = {}
    canonical_classes: dict[str, set[str]] = {}
    decision_flags: dict[str, list[bool]] = {}
    statuses: dict[str, set[str]] = {}
    for row in rows:
        raw_id = str(row.get("raw_source_asset_id") or row.get("asset_id"))
        canonical_id = str(row.get("asset_id") or "")
        raw_classes.setdefault(raw_id, set()).add(
            str(row.get("raw_source_asset_class") or "unknown")
        )
        canonical_classes.setdefault(canonical_id, set()).add(
            str(row.get("underlying_asset_class") or row.get("asset_class"))
        )
        decision_flags.setdefault(canonical_id, []).append(
            bool(row.get("decision_grade"))
        )
        statuses.setdefault(canonical_id, set()).add(
            str(row.get("identity_status") or "unknown")
        )
    decision_grade_candidate_ids = {
        asset_id
        for asset_id, flags in decision_flags.items()
        if flags and all(flags)
    }
    raw_mixed_ids = sorted(
        asset_id
        for asset_id, classes in raw_classes.items()
        if len(classes) > 1
    )
    canonical_mixed_ids = sorted(
        asset_id
        for asset_id, classes in canonical_classes.items()
        if len(classes) > 1
    )
    decision_grade_mixed_ids = sorted(
        asset_id
        for asset_id in decision_grade_candidate_ids
        if len(canonical_classes[asset_id]) > 1
    )
    decision_grade_ids = decision_grade_candidate_ids - set(
        decision_grade_mixed_ids
    )
    ambiguous_ids = sorted(
        asset_id
        for asset_id, values in statuses.items()
        if "source_scoped_ambiguous" in values
    )
    return {
        "raw_mixed_class_asset_id_count": len(raw_mixed_ids),
        "canonical_mixed_class_asset_id_count": len(canonical_mixed_ids),
        "decision_grade_mixed_class_asset_id_count": len(
            decision_grade_mixed_ids
        ),
        "decision_grade_asset_count": len(decision_grade_ids),
        "manual_verification_asset_count": (
            len(canonical_classes) - len(decision_grade_ids)
        ),
        "ambiguous_source_scoped_asset_count": len(ambiguous_ids),
        "raw_mixed_class_asset_ids": raw_mixed_ids,
        "canonical_mixed_class_asset_ids": canonical_mixed_ids,
        "decision_grade_mixed_class_asset_ids": decision_grade_mixed_ids,
        "ambiguous_source_scoped_asset_ids": ambiguous_ids,
        "acceptance": {
            "status": (
                "pass"
                if not canonical_mixed_ids and not decision_grade_mixed_ids
                else "blocked"
            ),
            "criterion": (
                "zero unresolved mixed-class canonical ids, including zero "
                "mixed ids in decision-grade counts"
            ),
        },
    }


def reclassify_derivative_venue_discovery_report(
    report: dict[str, Any],
) -> dict[str, Any]:
    """Re-normalize a captured report without refetching or retimestamping it."""
    updated = deepcopy(report)
    coverage_rows = [
        normalize_instrument_semantics(row)
        for row in updated.get("coverage_rows", [])
        if isinstance(row, dict)
    ]
    market_rows = [
        normalize_instrument_semantics(row)
        for row in updated.get("market_rows", [])
        if isinstance(row, dict)
    ]
    coverage_rows.sort(
        key=lambda row: (
            str(row.get("venue")),
            str(row.get("asset_class")),
            str(row.get("asset_id")),
            str(row.get("symbol")),
            str((row.get("metadata") or {}).get("venue_market_id")),
        )
    )
    market_rows.sort(
        key=lambda row: (
            str(row.get("venue")),
            str(row.get("asset_id")),
            str(row.get("symbol")),
            str((row.get("metadata") or {}).get("venue_market_id")),
        )
    )
    updated["coverage_rows"] = coverage_rows
    updated["market_rows"] = market_rows

    summary = deepcopy(updated.get("summary") or {})
    by_venue = Counter(str(row.get("venue")) for row in coverage_rows)
    by_asset_class = Counter(
        str(row.get("asset_class")) for row in coverage_rows
    )
    by_raw_asset_class = Counter(
        str(row.get("raw_source_asset_class")) for row in coverage_rows
    )
    by_family = Counter(str(row.get("asset_family")) for row in coverage_rows)
    by_source_type = Counter(
        str(row.get("source_type")) for row in coverage_rows
    )
    by_market_type = Counter(
        str((row.get("metadata") or {}).get("market_type"))
        for row in coverage_rows
    )
    by_contract_type = Counter(
        str(row.get("contract_type")) for row in coverage_rows
    )
    summary.update(
        {
            "coverage_row_count": len(coverage_rows),
            "market_row_count": len(market_rows),
            "rwa_or_traditional_coverage_rows": by_family.get(
                "rwa_or_traditional", 0
            ),
            "crypto_coverage_rows": by_family.get("crypto", 0),
            # Compatibility metric based on the captured source family. New
            # consumers should use by_contract_type.
            "derivative_only_rows": sum(
                1
                for row in coverage_rows
                if row.get("raw_source_asset_family") == "derivative"
            ),
            "by_venue": dict(sorted(by_venue.items())),
            "by_asset_class": dict(sorted(by_asset_class.items())),
            "by_raw_source_asset_class": dict(
                sorted(by_raw_asset_class.items())
            ),
            "by_family": dict(sorted(by_family.items())),
            "by_source_type": dict(sorted(by_source_type.items())),
            "by_market_type": dict(sorted(by_market_type.items())),
            "by_contract_type": dict(sorted(by_contract_type.items())),
            "identity_quality": _derivative_identity_quality(coverage_rows),
        }
    )
    updated["summary"] = summary
    updated["identity_semantics"] = {
        "version": 1,
        "normalization_mode": "offline_deterministic_no_refetch",
        "generated_at_preserved": True,
        "raw_class_field": "raw_source_asset_class",
        "underlying_class_field": "underlying_asset_class",
        "contract_type_field": "contract_type",
        "fail_closed_rule": (
            "ambiguous bare tickers are source-scoped and excluded from "
            "decision-grade counts"
        ),
    }
    return updated


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def load_derivative_coverage_rows(
    path: str | Path = DEFAULT_DERIVATIVE_VENUE_DISCOVERY_JSON_PATH,
) -> list[dict[str, Any]]:
    """Load generated derivative venue coverage rows when the report exists."""
    payload = _read_json(Path(path))
    rows = payload.get("coverage_rows") if isinstance(payload.get("coverage_rows"), list) else []
    return [
        normalize_instrument_semantics(row)
        for row in rows
        if isinstance(row, dict)
    ]


def load_derivative_venue_rows(
    path: str | Path = DEFAULT_DERIVATIVE_VENUE_DISCOVERY_JSON_PATH,
) -> list[dict[str, Any]]:
    """Load generated venue access/probe rows when the report exists."""
    payload = _read_json(Path(path))
    rows = payload.get("venues") if isinstance(payload.get("venues"), list) else []
    return [row for row in rows if isinstance(row, dict)]


def load_derivative_venue_discovery_report(
    path: str | Path = DEFAULT_DERIVATIVE_VENUE_DISCOVERY_JSON_PATH,
) -> dict[str, Any]:
    """Load the generated derivative venue discovery report without network access."""
    payload = _read_json(Path(path))
    if payload:
        return reclassify_derivative_venue_discovery_report(payload)
    return {
        "product": "rwa_derivative_venue_discovery",
        "generated_at": None,
        "summary": {
            "venue_count": len(DERIVATIVE_VENUE_CONFIGS),
            "coverage_row_count": 0,
            "market_row_count": 0,
            "sourceable_venue_count": 0,
            "blocked_or_gated_venue_count": len(DERIVATIVE_VENUE_CONFIGS),
        },
        "policy": {
            "promotion_rule": "Run scripts/run_rwa_derivative_venue_discovery.py to generate live public catalog evidence.",
            "spot_derivation_rule": DERIVATIVE_FAIR_VALUE_METHODOLOGY["rule"],
        },
        "fair_value_methodology": deepcopy(DERIVATIVE_FAIR_VALUE_METHODOLOGY),
        "venues": [
            {
                "venue_id": config["venue_id"],
                "name": config["name"],
                "ingestion_status": config["ingestion_status"],
                "discovery_status": "report_not_generated",
                "sourceable_now": False,
                "next_action": "Run derivative venue discovery.",
            }
            for config in DERIVATIVE_VENUE_CONFIGS
        ],
        "coverage_rows": [],
        "market_rows": [],
    }


def build_derivative_venue_report(
    *,
    venue: str = "all",
    asset_class: str = "all",
    status: str = "all",
    include_market_rows: bool = False,
) -> dict[str, Any]:
    """Return filtered derivative venue discovery rows from the generated report."""
    report = load_derivative_venue_discovery_report()
    venue_filter = venue.strip().lower()
    asset_filter = asset_class.strip().lower()
    status_filter = status.strip().lower()
    venues = [
        row
        for row in report.get("venues", [])
        if isinstance(row, dict)
        and (venue_filter == "all" or str(row.get("venue_id")).lower() == venue_filter)
        and (
            status_filter == "all"
            or str(row.get("discovery_status")).lower() == status_filter
            or str(row.get("ingestion_status")).lower() == status_filter
        )
    ]
    coverage_rows = [
        row
        for row in report.get("coverage_rows", [])
        if isinstance(row, dict)
        and (venue_filter == "all" or str(row.get("venue")).lower() == venue_filter)
        and (asset_filter == "all" or str(row.get("asset_class")).lower() == asset_filter)
    ]
    response = {
        "source_report": str(DEFAULT_DERIVATIVE_VENUE_DISCOVERY_JSON_PATH),
        "generated_at": report.get("generated_at"),
        "summary": {
            **(report.get("summary") or {}),
            "returned_venues": len(venues),
            "returned_coverage_rows": len(coverage_rows),
        },
        "filters": {
            "venue": venue,
            "asset_class": asset_class,
            "status": status,
            "include_market_rows": include_market_rows,
        },
        "policy": report.get("policy") or {},
        "fair_value_methodology": report.get("fair_value_methodology") or DERIVATIVE_FAIR_VALUE_METHODOLOGY,
        "venues": venues,
        "coverage_rows": coverage_rows,
    }
    if include_market_rows:
        response["market_rows"] = [
            row
            for row in report.get("market_rows", [])
            if isinstance(row, dict)
            and (venue_filter == "all" or str(row.get("venue")).lower() == venue_filter)
            and (asset_filter == "all" or str(row.get("asset_class")).lower() == asset_filter)
        ]
    return response


def write_derivative_venue_discovery_reports(
    *,
    json_path: str | Path = DEFAULT_DERIVATIVE_VENUE_DISCOVERY_JSON_PATH,
    csv_path: str | Path = DEFAULT_DERIVATIVE_VENUE_DISCOVERY_CSV_PATH,
) -> dict[str, Any]:
    """Fetch, normalize, and write derivative venue discovery reports."""
    report = build_derivative_venue_discovery()
    _write_derivative_venue_discovery_report_files(
        report,
        json_path=Path(json_path),
        csv_path=Path(csv_path),
    )
    return report


def write_reclassified_derivative_venue_discovery_reports(
    *,
    input_json_path: str | Path = DEFAULT_DERIVATIVE_VENUE_DISCOVERY_JSON_PATH,
    json_path: str | Path = DEFAULT_DERIVATIVE_VENUE_DISCOVERY_JSON_PATH,
    csv_path: str | Path = DEFAULT_DERIVATIVE_VENUE_DISCOVERY_CSV_PATH,
) -> dict[str, Any]:
    """Reclassify a captured report offline and preserve its source timestamp."""
    captured = _read_json(Path(input_json_path))
    if not captured:
        raise ValueError(f"Derivative discovery report is missing: {input_json_path}")
    report = reclassify_derivative_venue_discovery_report(captured)
    _write_derivative_venue_discovery_report_files(
        report,
        json_path=Path(json_path),
        csv_path=Path(csv_path),
    )
    return report


def _write_derivative_venue_discovery_report_files(
    report: dict[str, Any],
    *,
    json_path: Path,
    csv_path: Path,
) -> None:
    """Atomically write one already-built report and its review CSV."""
    json_out = Path(json_path)
    csv_out = Path(csv_path)
    json_out.parent.mkdir(parents=True, exist_ok=True)
    csv_out.parent.mkdir(parents=True, exist_ok=True)
    json_temp = json_out.with_suffix(f"{json_out.suffix}.tmp")
    csv_temp = csv_out.with_suffix(f"{csv_out.suffix}.tmp")
    json_temp.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    fieldnames = [
        "venue",
        "symbol",
        "raw_source_asset_id",
        "asset_id",
        "raw_source_asset_class",
        "asset_class",
        "underlying_asset_class",
        "asset_family",
        "contract_type",
        "identity_status",
        "decision_grade",
        "manual_verification_required",
        "identity_evidence",
        "source_type",
        "coverage_status",
        "vwap_support",
        "bidask_support",
        "market_type",
        "venue_symbol",
        "venue_market_id",
        "pricing_methodology",
    ]
    with csv_temp.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in report["coverage_rows"]:
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            writer.writerow(
                {
                    "venue": row.get("venue"),
                    "symbol": row.get("symbol"),
                    "raw_source_asset_id": row.get("raw_source_asset_id"),
                    "asset_id": row.get("asset_id"),
                    "raw_source_asset_class": row.get(
                        "raw_source_asset_class"
                    ),
                    "asset_class": row.get("asset_class"),
                    "underlying_asset_class": row.get(
                        "underlying_asset_class"
                    ),
                    "asset_family": row.get("asset_family"),
                    "contract_type": row.get("contract_type"),
                    "identity_status": row.get("identity_status"),
                    "decision_grade": row.get("decision_grade"),
                    "manual_verification_required": row.get(
                        "manual_verification_required"
                    ),
                    "identity_evidence": row.get("identity_evidence"),
                    "source_type": row.get("source_type"),
                    "coverage_status": row.get("coverage_status"),
                    "vwap_support": row.get("vwap_support"),
                    "bidask_support": row.get("bidask_support"),
                    "market_type": metadata.get("market_type"),
                    "venue_symbol": metadata.get("venue_symbol"),
                    "venue_market_id": metadata.get("venue_market_id"),
                    "pricing_methodology": metadata.get("pricing_methodology"),
                }
            )
    json_temp.replace(json_out)
    csv_temp.replace(csv_out)
