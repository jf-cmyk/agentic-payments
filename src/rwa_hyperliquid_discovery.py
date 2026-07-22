"""Live Hyperliquid tradeable feed discovery and coverage normalization."""

from __future__ import annotations

import csv
import json
import urllib.error
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.rwa_hyperliquid import (
    HYPERLIQUID_RWA_SPOT_SYMBOLS,
    hyperliquid_is_unverified,
    hyperliquid_normalized_asset_class,
)


DEFAULT_REPORTS_DIR = Path("reports")
DEFAULT_HYPERLIQUID_TRADEABLE_FEEDS_JSON_PATH = (
    DEFAULT_REPORTS_DIR / "hyperliquid_tradeable_feeds.json"
)
DEFAULT_HYPERLIQUID_TRADEABLE_FEEDS_CSV_PATH = (
    DEFAULT_REPORTS_DIR / "hyperliquid_tradeable_feeds.csv"
)
HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"

HYPERLIQUID_PERPS_VENUE_ID = "hyperliquid_perps"
HYPERLIQUID_SPOT_VENUE_ID = "hyperliquid_spot"

TOKENIZED_EQUITY_SYMBOLS = {
    "AAPL",
    "AMZN",
    "AVGO",
    "CRCL",
    "GOOGL",
    "HOOD",
    "META",
    "MSFT",
    "MU",
    "ORCL",
    "SPCX",
    "SPCXD",
    "TSLA",
}
TOKENIZED_ETF_SYMBOLS = {"GLD", "QQQ", "QQQM", "SLV", "SPY", "USPYX"}
TOKENIZED_GOLD_SYMBOLS = {"PAXG", "XAUT0", "XAUM"}
FIAT_TOKEN_SYMBOLS = {"BRLA", "RUBT", "WARS", "WBRL"}
STABLE_OR_CASH_SYMBOLS = {"DIME", "JUNO", "USDH", "USDHL", "USDT0", "USDE", "USDV", "USR"}
TREASURY_FUND_SYMBOLS = {"THBILL"}


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _post_info(body: dict[str, Any]) -> dict[str, Any]:
    request = urllib.request.Request(
        HYPERLIQUID_INFO_URL,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "accept": "application/json",
            "content-type": "application/json",
            "user-agent": "BlocksizeHyperliquidCoverageDiscovery/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    return payload if isinstance(payload, dict) else {}


def fetch_hyperliquid_meta() -> dict[str, Any]:
    """Fetch live Hyperliquid perpetual metadata."""
    return _post_info({"type": "meta"})


def fetch_hyperliquid_spot_meta() -> dict[str, Any]:
    """Fetch live Hyperliquid spot metadata."""
    return _post_info({"type": "spotMeta"})


def _asset_id(symbol: str) -> str:
    return symbol.replace("-", "/").split("/", 1)[0].upper()


def _evm_contract_address(token: dict[str, Any]) -> str | None:
    evm_contract = token.get("evmContract")
    if isinstance(evm_contract, dict):
        address = evm_contract.get("address")
        return str(address) if address else None
    return None


def _token_maps(spot_meta: dict[str, Any]) -> tuple[dict[int, dict[str, Any]], dict[str, dict[str, Any]]]:
    token_by_index: dict[int, dict[str, Any]] = {}
    token_by_name: dict[str, dict[str, Any]] = {}
    for token in spot_meta.get("tokens") or []:
        if not isinstance(token, dict):
            continue
        try:
            token_by_index[int(token["index"])] = token
        except (KeyError, TypeError, ValueError):
            continue
        name = str(token.get("name") or "").upper()
        if name:
            token_by_name[name] = token
    return token_by_index, token_by_name


def _curated_rwa_by_pair_index() -> dict[int, dict[str, Any]]:
    rows: dict[int, dict[str, Any]] = {}
    for row in HYPERLIQUID_RWA_SPOT_SYMBOLS:
        try:
            rows[int(row["pair_index"])] = row
        except (KeyError, TypeError, ValueError):
            continue
    return rows


def _classify_traditional_symbol(symbol: str, full_name: str | None = None) -> tuple[str, str, str]:
    upper = symbol.upper()
    full = (full_name or "").lower()
    if upper in TOKENIZED_EQUITY_SYMBOLS or "space exploration" in full:
        return "equity", "rwa_or_traditional", "tokenized_equity_or_private_market"
    if upper in TOKENIZED_ETF_SYMBOLS or "sp500" in full or "s&p" in full:
        return "etf" if upper != "USPYX" else "index", "rwa_or_traditional", "tokenized_etf_or_index"
    if upper in TOKENIZED_GOLD_SYMBOLS or "gold" in full:
        return "metal", "rwa_or_traditional", "tokenized_gold_or_gold_reference"
    if upper in FIAT_TOKEN_SYMBOLS or "wrapped brazilian real" in full or "peso argentino" in full:
        return "fx", "rwa_or_traditional", "fiat_or_fx_token"
    if upper in TREASURY_FUND_SYMBOLS:
        return "treasury_fund", "rwa_or_traditional", "tokenized_treasury_fund"
    if upper in STABLE_OR_CASH_SYMBOLS or "cash" in full:
        return "fx", "rwa_or_traditional", "stable_or_cash_like_token"
    return "crypto", "crypto", "crypto_asset"


def _coverage_status(asset_class: str, market_type: str) -> str:
    if asset_class == "crypto":
        return f"hyperliquid_live_{market_type}_candidate_requires_depth_freshness_and_benchmark_validation"
    return f"hyperliquid_live_{market_type}_rwa_candidate_requires_identity_liquidity_and_benchmark_validation"


def _compare_status(
    row: dict[str, Any],
    existing_exact_keys: set[tuple[str, str]],
    existing_assets: set[str],
) -> str:
    exact_key = (str(row["venue"]), str(row["symbol"]).upper())
    if exact_key in existing_exact_keys:
        return "already_covered_exact_venue_symbol"
    if str(row["asset_id"]).upper() in existing_assets:
        return "new_hyperliquid_venue_for_existing_asset"
    return "new_asset_and_venue_coverage"


def _perp_row(
    market: dict[str, Any],
    existing_exact_keys: set[tuple[str, str]],
    existing_assets: set[str],
) -> dict[str, Any]:
    name = str(market.get("name") or "").upper()
    asset_class, family, subtype = _classify_traditional_symbol(name)
    symbol = f"{name}/USD"
    row = {
        "symbol": symbol,
        "asset_id": name,
        "asset_class": asset_class,
        "asset_family": family,
        "venue": HYPERLIQUID_PERPS_VENUE_ID,
        "source_type": "native_l2",
        "coverage_status": _coverage_status(asset_class, "perp"),
        "vwap_support": "native_l2_block_vwap",
        "bidask_support": "native_top_of_book",
        "metadata": {
            "market_type": "perp",
            "hyperliquid_coin": name,
            "hyperliquid_asset_subtype": subtype,
            "sz_decimals": market.get("szDecimals"),
            "max_leverage": market.get("maxLeverage"),
            "margin_table_id": market.get("marginTableId"),
            "only_isolated": bool(market.get("onlyIsolated")),
            "is_delisted": bool(market.get("isDelisted")),
        },
    }
    row["coverage_delta"] = _compare_status(row, existing_exact_keys, existing_assets)
    return row


def _spot_row(
    pair: dict[str, Any],
    token_by_index: dict[int, dict[str, Any]],
    curated_by_pair_index: dict[int, dict[str, Any]],
    existing_exact_keys: set[tuple[str, str]],
    existing_assets: set[str],
) -> dict[str, Any] | None:
    token_indexes = pair.get("tokens") if isinstance(pair.get("tokens"), list) else []
    if len(token_indexes) != 2:
        return None
    base_token = token_by_index.get(int(token_indexes[0]))
    quote_token = token_by_index.get(int(token_indexes[1]))
    if not base_token or not quote_token:
        return None
    base = str(base_token.get("name") or "").upper()
    quote = str(quote_token.get("name") or "").upper()
    if not base or not quote:
        return None
    pair_index = int(pair.get("index"))
    curated = curated_by_pair_index.get(pair_index)
    if curated:
        asset_class = hyperliquid_normalized_asset_class(curated)
        family = "rwa_or_traditional"
        subtype = str(curated.get("asset_class") or "curated_rwa_spot")
        identity_note = curated.get("identity_note")
        use_case = curated.get("use_case")
        unverified = hyperliquid_is_unverified(curated)
    else:
        asset_class, family, subtype = _classify_traditional_symbol(base, base_token.get("fullName"))
        identity_note = base_token.get("fullName")
        use_case = (
            "crypto_spot_reference"
            if asset_class == "crypto"
            else "rwa_or_traditional_spot_candidate_needs_identity_validation"
        )
        unverified = False
    symbol = f"{base}/{quote}"
    row = {
        "symbol": symbol,
        "asset_id": _asset_id(symbol),
        "asset_class": asset_class,
        "asset_family": family,
        "venue": HYPERLIQUID_SPOT_VENUE_ID,
        "source_type": "native_l2",
        "coverage_status": (
            "hyperliquid_live_spot_unverified_identity_requires_manual_review"
            if unverified
            else _coverage_status(asset_class, "spot")
        ),
        "vwap_support": "requires_identity_verification" if unverified else "native_l2_block_vwap",
        "bidask_support": "requires_identity_verification" if unverified else "native_top_of_book",
        "metadata": {
            "market_type": "spot",
            "hyperliquid_coin": f"@{pair_index}",
            "pair_index": pair_index,
            "pair_name": pair.get("name"),
            "is_canonical_pair": bool(pair.get("isCanonical")),
            "hyperliquid_asset_subtype": subtype,
            "base_token_index": base_token.get("index"),
            "quote_token_index": quote_token.get("index"),
            "base_token_id": base_token.get("tokenId"),
            "quote_token_id": quote_token.get("tokenId"),
            "base_evm_contract": _evm_contract_address(base_token),
            "quote_evm_contract": _evm_contract_address(quote_token),
            "base_full_name": base_token.get("fullName"),
            "identity_note": identity_note,
            "use_case": use_case,
        },
    }
    row["coverage_delta"] = _compare_status(row, existing_exact_keys, existing_assets)
    return row


def build_hyperliquid_tradeable_feed_discovery(
    *,
    meta: dict[str, Any] | None = None,
    spot_meta: dict[str, Any] | None = None,
    existing_rows: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build a live Hyperliquid tradable feed coverage report."""
    meta_payload = meta if meta is not None else fetch_hyperliquid_meta()
    spot_payload = spot_meta if spot_meta is not None else fetch_hyperliquid_spot_meta()
    existing_exact_keys = {
        (str(row.get("venue")), str(row.get("symbol")).upper())
        for row in (existing_rows or [])
        if row.get("venue") and row.get("symbol")
    }
    existing_assets = {
        str(row.get("asset_id")).upper()
        for row in (existing_rows or [])
        if row.get("asset_id")
    }

    perp_rows = [
        _perp_row(row, existing_exact_keys, existing_assets)
        for row in meta_payload.get("universe") or []
        if isinstance(row, dict) and row.get("name")
    ]
    active_perp_rows = [row for row in perp_rows if not row["metadata"].get("is_delisted")]
    token_by_index, _token_by_name = _token_maps(spot_payload)
    curated = _curated_rwa_by_pair_index()
    spot_rows = [
        row
        for row in (
            _spot_row(pair, token_by_index, curated, existing_exact_keys, existing_assets)
            for pair in spot_payload.get("universe") or []
            if isinstance(pair, dict)
        )
        if row is not None
    ]
    coverage_rows = [
        *active_perp_rows,
        *spot_rows,
    ]
    by_family = Counter(row["asset_family"] for row in coverage_rows)
    by_asset_class = Counter(row["asset_class"] for row in coverage_rows)
    by_delta = Counter(row["coverage_delta"] for row in coverage_rows)
    by_venue = Counter(row["venue"] for row in coverage_rows)
    return {
        "product": "hyperliquid_tradeable_feed_discovery",
        "generated_at": _utc_now_iso(),
        "summary": {
            "perp_market_count": len(perp_rows),
            "active_perp_market_count": len(active_perp_rows),
            "delisted_perp_market_count": len(perp_rows) - len(active_perp_rows),
            "spot_pair_count": len(spot_rows),
            "coverage_row_count": len(coverage_rows),
            "crypto_coverage_rows": by_family.get("crypto", 0),
            "rwa_or_traditional_coverage_rows": by_family.get("rwa_or_traditional", 0),
            "new_asset_and_venue_coverage": by_delta.get("new_asset_and_venue_coverage", 0),
            "new_hyperliquid_venue_for_existing_asset": by_delta.get(
                "new_hyperliquid_venue_for_existing_asset", 0
            ),
            "already_covered_exact_venue_symbol": by_delta.get("already_covered_exact_venue_symbol", 0),
            "by_venue": dict(sorted(by_venue.items())),
            "by_asset_class": dict(sorted(by_asset_class.items())),
            "by_family": dict(sorted(by_family.items())),
            "by_coverage_delta": dict(sorted(by_delta.items())),
        },
        "policy": {
            "promotion_rule": "Hyperliquid rows are sourceable candidates only; production promotion still requires freshness, depth, manipulation, benchmark, and rights checks.",
            "rwa_rule": "RWA/traditional spot rows require issuer or identity validation before they can be used as replacement market data.",
            "delisted_rule": "Delisted perp markets are retained in the report but excluded from coverage rows.",
        },
        "coverage_rows": sorted(
            coverage_rows,
            key=lambda row: (str(row["venue"]), str(row["asset_class"]), str(row["asset_id"]), str(row["symbol"])),
        ),
        "perp_markets": sorted(perp_rows, key=lambda row: str(row["asset_id"])),
        "spot_pairs": sorted(spot_rows, key=lambda row: (str(row["asset_id"]), str(row["symbol"]))),
    }


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def load_hyperliquid_tradeable_coverage_rows(
    path: str | Path = DEFAULT_HYPERLIQUID_TRADEABLE_FEEDS_JSON_PATH,
) -> list[dict[str, Any]]:
    """Load generated Hyperliquid coverage rows if the discovery report exists."""
    payload = _read_json(Path(path))
    rows = payload.get("coverage_rows") if isinstance(payload.get("coverage_rows"), list) else []
    return [row for row in rows if isinstance(row, dict)]


def write_hyperliquid_tradeable_feed_reports(
    *,
    existing_rows: list[dict[str, Any]] | None = None,
    json_path: str | Path = DEFAULT_HYPERLIQUID_TRADEABLE_FEEDS_JSON_PATH,
    csv_path: str | Path = DEFAULT_HYPERLIQUID_TRADEABLE_FEEDS_CSV_PATH,
) -> dict[str, Any]:
    """Fetch live Hyperliquid metadata and write coverage reports."""
    report = build_hyperliquid_tradeable_feed_discovery(existing_rows=existing_rows)
    json_out = Path(json_path)
    csv_out = Path(csv_path)
    json_out.parent.mkdir(parents=True, exist_ok=True)
    csv_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    fieldnames = [
        "venue",
        "symbol",
        "asset_id",
        "asset_class",
        "asset_family",
        "source_type",
        "coverage_status",
        "vwap_support",
        "bidask_support",
        "coverage_delta",
    ]
    with csv_out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in report["coverage_rows"]:
            writer.writerow({key: row.get(key) for key in fieldnames})
    return report
