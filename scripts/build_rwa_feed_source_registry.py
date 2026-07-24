#!/usr/bin/env python3
"""Build a per-feed RWA source registry from current coverage artifacts."""

from __future__ import annotations

import csv
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


REPORTS_DIR = Path("reports")
ALL_TICKERS_PATH = REPORTS_DIR / "rwa_all_tickers_token_contracts_sourceability_2026-07-16.csv"
SOURCEABLE_PAIRS_PATH = REPORTS_DIR / "rwa_sourceable_pairs_with_contracts_2026-07-16.csv"
DERIVATIVE_VENUES_PATH = REPORTS_DIR / "rwa_derivative_venue_discovery.csv"
OUT_CSV = REPORTS_DIR / "rwa_feed_source_registry_2026-07-16.csv"
OUT_JSON = REPORTS_DIR / "rwa_feed_source_registry_2026-07-16.json"
OUT_MD = REPORTS_DIR / "rwa_feed_source_registry_2026-07-16.md"


PAIR_RE = re.compile(r"^(?P<venue>[^:]+):(?P<symbol>[^\[]+)\[(?P<kind>[^\]]+)\]$")

FIELDNAMES = [
    "registry_scope",
    "feed_status",
    "canonical_ticker",
    "token_ticker",
    "rwa_symbol",
    "asset_class",
    "issuer_platform",
    "network",
    "token_contract_address",
    "venue",
    "venue_symbol",
    "venue_market_id",
    "market_type",
    "price_source_type",
    "price_source_lane",
    "latest_price",
    "bid",
    "ask",
    "vwap",
    "source_timestamp_present",
    "production_grade",
    "candidate_source_count",
    "candidate_venues",
    "source_reference_url",
    "spot_anchor_required",
    "premium_discount_guard",
    "basis_warning_bps",
    "basis_exclude_bps",
    "raw_perp_allowed_in_spot_vwap",
    "max_spot_composite_weight",
    "basis_inputs_required",
    "raw_data_to_capture",
    "composite_role",
    "normalization_rule",
    "quality_gates",
    "next_action",
]


RAW_DATA_BY_TYPE = {
    "BidAsk": "best_bid,best_ask,mid,depth_ladder,timestamp,block_or_slot_or_sequence",
    "VWAP": "trade_price,trade_size,quote_notional,timestamp,block_or_slot_or_sequence,trade_id_or_tx_hash",
    "perp": "l2_book,trades,mark_price,index_or_oracle_price,funding_rate,open_interest,timestamp,market_id",
    "synthetic_perp": "execution_bid,execution_ask,mark_or_oracle_price,open_interest,rollover_or_funding,timestamp,market_id",
    "issuer_reference": "issuer_nav,redemption_price,total_supply,reserve_attestation,timestamp,terms_url",
    "pending": "token_contract,issuer_metadata,token_supply,holders,pool_or_venue_discovery,issuer_nav",
}


BASIS_THRESHOLDS_BY_ASSET_CLASS = {
    "crypto": ("75", "200"),
    "equity": ("35", "100"),
    "etf": ("30", "75"),
    "fx": ("5", "20"),
    "metal": ("25", "75"),
    "commodity": ("35", "100"),
    "tokenized_fund": ("15", "50"),
    "treasury_fund": ("5", "15"),
}


SOURCE_REFERENCE_URLS = {
    "aevo": "https://api-docs.aevo.xyz/",
    "apex_omni": "https://api-docs.pro.apex.exchange/",
    "aster": "https://github.com/asterdex/api-docs",
    "balancer_pools": "https://docs.balancer.fi/",
    "dinari": "https://docs.dinari.com/docs/what-is-dshare",
    "drift": "https://docs.drift.trade/protocol/trading/market-specs",
    "dydx": "https://docs.dydx.xyz/",
    "gains": "https://docs.gains.trade/developer/integrators/price-feed",
    "hyperliquid_perps": "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint/perpetuals",
    "hyperliquid_rwa_spot": "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions",
    "hyperliquid_spot": "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/websocket/subscriptions",
    "jupiter_router": "https://station.jup.ag/docs/apis",
    "lighter": "https://docs.lighter.xyz/perpetual-futures/api",
    "meteora_dlmm": "https://docs.meteora.ag/",
    "ondo": "https://docs.ondo.finance/api-reference/assets/get-metadata-for-all-supported-assets",
    "orca_whirlpool": "https://orca-so.github.io/whirlpools/",
    "orderly": "https://orderly.network/docs/build-on-omnichain/introduction",
    "ostium": "https://ostium-labs.gitbook.io/ostium-docs/developer/api-and-sdk",
    "securitize": "https://securitize.io/",
    "swarm": "https://www.swarm.com/",
    "uniswap_v3_v4": "https://docs.uniswap.org/",
    "xstocks": "https://xstocks.com/",
}


def source_reference_url(venue: str, issuer_platform: str = "") -> str:
    venue_key = (venue or "").lower()
    issuer_key = (issuer_platform or "").lower()
    if venue_key in SOURCE_REFERENCE_URLS:
        return SOURCE_REFERENCE_URLS[venue_key]
    if issuer_key in SOURCE_REFERENCE_URLS:
        return SOURCE_REFERENCE_URLS[issuer_key]
    return ""


def basis_thresholds(asset_class: str) -> tuple[str, str]:
    normalized = (asset_class or "").strip().lower()
    return BASIS_THRESHOLDS_BY_ASSET_CLASS.get(normalized, ("35", "100"))


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def split_candidate_pairs(raw: str) -> list[dict[str, str]]:
    pairs: list[dict[str, str]] = []
    for piece in (raw or "").split("|"):
        value = piece.strip()
        if not value:
            continue
        match = PAIR_RE.match(value)
        if not match:
            pairs.append({"venue": "", "symbol": value, "kind": "unknown"})
            continue
        pairs.append(match.groupdict())
    return pairs


def canonical_ticker(row: dict[str, str]) -> str:
    return (
        row.get("feed_key")
        or row.get("asset_id")
        or row.get("rwa_ticker")
        or row.get("symbol", "").split("/", 1)[0]
    ).strip()


def infer_market_type(source_lane: str, venue: str) -> str:
    lane = (source_lane or "").lower()
    venue_l = venue.lower()
    if venue_l in {
        "aevo",
        "apex_omni",
        "aster",
        "dydx",
        "drift",
        "gains",
        "lighter",
        "orderly",
        "ostium",
        "synthetix",
        "gmx",
        "hyperliquid_perps",
    }:
        return "perp_or_synthetic_perp"
    if venue_l in {
        "balancer_pools",
        "hyperliquid_paxg",
        "hyperliquid_rwa_spot",
        "hyperliquid_spot",
        "jupiter_router",
        "meteora_dlmm",
        "orca_whirlpool",
        "uniswap_v3_v4",
    }:
        return "tokenized_spot_or_dex_pool"
    if not venue_l and ("issuer_nav" in lane or "transfer_agent" in lane):
        return "issuer_nav_or_transfer_agent"
    if "dex_pool" in lane:
        return "tokenized_spot_or_dex_pool"
    if "issuer_nav" in lane or "transfer_agent" in lane:
        return "issuer_nav_or_transfer_agent"
    return "venue_orderbook_or_spot_reference"


def composite_role(market_type: str, price_source_type: str) -> str:
    if "issuer" in market_type:
        return "NAV/redemption anchor; never executable liquidity"
    if "perp" in market_type:
        return "derivative liquidity/reference leg; basis-adjust before spot composite"
    if price_source_type == "VWAP":
        return "executable trade-derived spot leg"
    if price_source_type == "BidAsk":
        return "executable top-of-book/depth leg"
    return "supplemental discovery/reference leg"


def normalization_rule(market_type: str, price_source_type: str) -> str:
    if "perp" in market_type:
        return "Normalize mark/index/trades separately; adjust for funding, premium/discount, and basis before any spot proxy use."
    if "issuer" in market_type:
        return "Normalize NAV/redemption to USD per token with timestamp and redemption terms; use as anchor, not VWAP."
    if price_source_type == "VWAP":
        return "Normalize quote notional to USD and compute sum(notional_usd)/sum(base_amount) per block/time bucket."
    if price_source_type == "BidAsk":
        return "Normalize bid/ask to USD, compute mid and executable depth by target block size."
    return "Normalize as candidate reference until adapter-specific schema is available."


def quality_gates(market_type: str) -> str:
    gates = [
        "freshness",
        "replayable_raw_payload",
        "rights_clearance",
        "benchmark_alignment",
    ]
    if "perp" in market_type:
        gates.extend(["funding_basis_model", "open_interest", "mark_index_trade_separation"])
    elif "issuer" in market_type:
        gates.extend(["issuer_timestamp", "redemption_terms", "reserve_or_nav_attestation"])
    else:
        gates.extend(["depth_or_liquidity", "outlier_filter", "manipulation_checks"])
    return ",".join(gates)


def basis_policy_fields(asset_class: str, market_type: str, price_source_type: str) -> dict[str, str]:
    warning_bps, exclude_bps = basis_thresholds(asset_class)
    is_perp = "perp" in (market_type or "").lower() or price_source_type in {"perp", "synthetic_perp"}
    if is_perp:
        return {
            "spot_anchor_required": "true",
            "premium_discount_guard": (
                "raw_perp_excluded_from_spot_vwap; compare mark/index/trade VWAP to independent "
                "spot/NAV/benchmark anchor; only basis-adjusted fair-value rows may enter, capped."
            ),
            "basis_warning_bps": warning_bps,
            "basis_exclude_bps": exclude_bps,
            "raw_perp_allowed_in_spot_vwap": "false",
            "max_spot_composite_weight": "0.15_after_basis_adjustment_pass",
            "basis_inputs_required": (
                "spot_anchor_price,perp_mark,perp_index,perp_trade_vwap,funding_rate,"
                "open_interest,basis_model_version,residual_basis_bps"
            ),
        }
    if "issuer" in (market_type or "").lower():
        return {
            "spot_anchor_required": "false",
            "premium_discount_guard": "issuer/NAV anchor; do not treat as executable VWAP.",
            "basis_warning_bps": "",
            "basis_exclude_bps": "",
            "raw_perp_allowed_in_spot_vwap": "",
            "max_spot_composite_weight": "reference_only",
            "basis_inputs_required": "issuer_nav,redemption_terms,reserve_or_nav_attestation,timestamp",
        }
    return {
        "spot_anchor_required": "false",
        "premium_discount_guard": "spot/DEX venue leg; still run cross-source outlier and manipulation checks.",
        "basis_warning_bps": "",
        "basis_exclude_bps": "",
        "raw_perp_allowed_in_spot_vwap": "",
        "max_spot_composite_weight": "liquidity_quality_weighted",
        "basis_inputs_required": "",
    }


def base_record(row: dict[str, str]) -> dict[str, str]:
    return {
        "canonical_ticker": canonical_ticker(row),
        "token_ticker": row.get("rwa_ticker", ""),
        "rwa_symbol": row.get("rwa_symbol", ""),
        "asset_class": row.get("asset_class", ""),
        "issuer_platform": row.get("platform", ""),
        "network": row.get("network", ""),
        "token_contract_address": row.get("token_contract_address", ""),
        "price_source_lane": row.get("price_source_lane", ""),
        "source_timestamp_present": row.get("source_timestamp_present", ""),
        "production_grade": row.get("production_grade", "false"),
        "candidate_source_count": row.get("candidate_source_count") or row.get("sourceable_pair_count", ""),
        "candidate_venues": row.get("candidate_venues") or row.get("sourceable_venues", ""),
        "source_reference_url": source_reference_url(row.get("best_venue", ""), row.get("platform", "")),
        "next_action": row.get("next_action", ""),
    }


def build_token_feed_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for row in rows:
        base = base_record(row)
        candidates = split_candidate_pairs(row.get("currently_sourceable_pairs", ""))
        if candidates:
            for candidate in candidates:
                price_type = candidate["kind"]
                venue = candidate["venue"]
                market_type = infer_market_type(base["price_source_lane"], venue)
                basis_policy = basis_policy_fields(base["asset_class"], market_type, price_type)
                is_best = (
                    venue == row.get("best_venue", "")
                    and candidate["symbol"] == row.get("best_source_symbol", "")
                    and price_type == row.get("best_price_type", "")
                )
                out.append(
                    {
                        **{name: "" for name in FIELDNAMES},
                        **base,
                        "registry_scope": "token_contract_feed_source",
                        "feed_status": "sourced_candidate",
                        "venue": venue,
                        "venue_symbol": candidate["symbol"],
                        "venue_market_id": "",
                        "market_type": market_type,
                        "price_source_type": price_type,
                        **basis_policy,
                        "source_reference_url": source_reference_url(venue, base["issuer_platform"]),
                        "latest_price": row.get("best_price", "") if is_best else "",
                        "bid": row.get("bid", "") if is_best else "",
                        "ask": row.get("ask", "") if is_best else "",
                        "vwap": row.get("vwap", "") if is_best or price_type == "VWAP" else "",
                        "raw_data_to_capture": RAW_DATA_BY_TYPE.get(price_type, RAW_DATA_BY_TYPE["pending"]),
                        "composite_role": composite_role(market_type, price_type),
                        "normalization_rule": normalization_rule(market_type, price_type),
                        "quality_gates": quality_gates(market_type),
                    }
                )
        else:
            market_type = infer_market_type(base["price_source_lane"], "")
            basis_policy = basis_policy_fields(base["asset_class"], market_type, "pending")
            out.append(
                {
                    **{name: "" for name in FIELDNAMES},
                    **base,
                    "registry_scope": "token_contract_pending_source",
                    "feed_status": row.get("sourceability_status", "pending_adapter_or_access"),
                    "venue": "",
                    "venue_symbol": "",
                    "venue_market_id": "",
                    "market_type": market_type,
                    "price_source_type": "pending",
                    **basis_policy,
                    "source_reference_url": source_reference_url("", base["issuer_platform"]),
                    "raw_data_to_capture": RAW_DATA_BY_TYPE["pending"],
                    "composite_role": composite_role(market_type, "pending"),
                    "normalization_rule": normalization_rule(market_type, "pending"),
                    "quality_gates": quality_gates(market_type),
                }
            )
    return out


def is_derivative_candidate(row: dict[str, str]) -> bool:
    market_type = (row.get("market_type") or "").lower()
    asset_class = (row.get("asset_class") or "").lower()
    source_type = (row.get("source_type") or "").lower()
    symbol = (row.get("symbol") or "").upper()
    if "option" in market_type or asset_class == "option":
        return False
    return (
        "perp" in market_type
        or "future" in market_type
        or "perp" in source_type
        or "future" in source_type
        or "FUT" in symbol
    )


def build_derivative_rows(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for row in rows:
        if not is_derivative_candidate(row):
            continue
        market_type = row.get("market_type", "perp")
        price_source_type = "synthetic_perp" if row.get("venue") in {"ostium", "gains"} else "perp"
        basis_policy = basis_policy_fields(row.get("asset_class", ""), market_type, price_source_type)
        out.append(
            {
                **{name: "" for name in FIELDNAMES},
                "registry_scope": "derivative_market_candidate",
                "feed_status": row.get("coverage_status", "derivative_candidate"),
                "canonical_ticker": row.get("asset_id", ""),
                "token_ticker": "",
                "rwa_symbol": row.get("symbol", ""),
                "asset_class": row.get("asset_class", ""),
                "issuer_platform": "",
                "network": row.get("venue", ""),
                "token_contract_address": "",
                "venue": row.get("venue", ""),
                "venue_symbol": row.get("venue_symbol") or row.get("symbol", ""),
                "venue_market_id": row.get("venue_market_id", ""),
                "market_type": market_type,
                "price_source_type": price_source_type,
                "price_source_lane": row.get("source_type", ""),
                **basis_policy,
                "source_reference_url": source_reference_url(row.get("venue", ""), ""),
                "production_grade": "false",
                "raw_data_to_capture": RAW_DATA_BY_TYPE[price_source_type],
                "composite_role": composite_role(market_type, price_source_type),
                "normalization_rule": normalization_rule(market_type, price_source_type),
                "quality_gates": quality_gates(market_type),
                "next_action": "Capture book/trades/mark/index/funding/OI; build basis/fair-value model before use in canonical spot price.",
            }
        )
    return out


def summarize(rows: list[dict[str, str]]) -> dict[str, Any]:
    by_scope = Counter(row["registry_scope"] for row in rows)
    by_status = Counter(row["feed_status"] for row in rows)
    by_venue = Counter(row["venue"] or "pending" for row in rows)
    by_asset_class = Counter(row["asset_class"] or "unknown" for row in rows)
    by_source_type = Counter(row["price_source_type"] for row in rows)
    token_contracts = {
        row["token_contract_address"]
        for row in rows
        if row.get("token_contract_address")
    }
    sourced_tickers = {
        row["canonical_ticker"]
        for row in rows
        if row["registry_scope"] == "token_contract_feed_source"
    }
    derivative_tickers = {
        row["canonical_ticker"]
        for row in rows
        if row["registry_scope"] == "derivative_market_candidate"
    }
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "row_count": len(rows),
        "unique_token_contracts": len(token_contracts),
        "sourced_token_tickers": len(sourced_tickers),
        "derivative_candidate_tickers": len(derivative_tickers),
        "by_scope": dict(sorted(by_scope.items())),
        "by_status_top": dict(by_status.most_common(25)),
        "by_venue_top": dict(by_venue.most_common(30)),
        "by_asset_class": dict(sorted(by_asset_class.items())),
        "by_source_type": dict(sorted(by_source_type.items())),
    }


def write_outputs(rows: list[dict[str, str]], summary: dict[str, Any]) -> None:
    OUT_CSV.parent.mkdir(parents=True, exist_ok=True)
    with OUT_CSV.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(rows)
    OUT_JSON.write_text(
        json.dumps({"summary": summary, "rows": rows}, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    top_venues = "\n".join(
        f"- `{venue}`: {count}"
        for venue, count in Counter(row["venue"] or "pending" for row in rows).most_common(15)
    )
    top_source_types = "\n".join(
        f"- `{source_type}`: {count}"
        for source_type, count in Counter(row["price_source_type"] for row in rows).most_common()
    )
    sample_rows = sorted(
        [
            row
            for row in rows
            if row["registry_scope"] == "token_contract_feed_source"
        ],
        key=lambda item: (item["canonical_ticker"], item["venue"], item["price_source_type"]),
    )[:40]
    sample_table = "\n".join(
        "| "
        + " | ".join(
            [
                row["canonical_ticker"],
                row["token_ticker"],
                row["asset_class"],
                row["issuer_platform"],
                row["network"],
                row["token_contract_address"],
                row["venue"],
                row["venue_symbol"],
                row["price_source_type"],
                row["market_type"],
            ]
        )
        + " |"
        for row in sample_rows
    )
    OUT_MD.write_text(
        "\n".join(
            [
                "# RWA Feed Source Registry",
                "",
                f"Generated: `{summary['generated_at']}`",
                "",
                "This registry expands token contracts into one row per sourceable feed, and appends derivative/perp markets as separate candidate reference legs. Perp rows are not spot prices until basis/funding/fair-value adjustment passes.",
                "",
                "## Premium/Discount Guard",
                "",
                "- Raw perp/futures marks, mids, and VWAPs are excluded from spot VWAP.",
                "- Every perp/futures leg must be compared to an independent spot, NAV, or benchmark anchor before composite use.",
                "- Premium/discount is measured as `(derivative_price - spot_anchor_price) / spot_anchor_price * 10_000`.",
                "- Only explicitly basis-adjusted fair-value rows may enter the spot composite, and their weight is capped.",
                "- Rows expose `basis_warning_bps`, `basis_exclude_bps`, `raw_perp_allowed_in_spot_vwap`, and `max_spot_composite_weight`.",
                "",
                "## Summary",
                "",
                f"- Rows: `{summary['row_count']}`",
                f"- Unique token contracts: `{summary['unique_token_contracts']}`",
                f"- Sourced token tickers: `{summary['sourced_token_tickers']}`",
                f"- Derivative candidate tickers: `{summary['derivative_candidate_tickers']}`",
                f"- Scope counts: `{summary['by_scope']}`",
                "",
                "## Top Venues",
                "",
                top_venues,
                "",
                "## Source Types",
                "",
                top_source_types,
                "",
                "## Sample Feed Rows",
                "",
                "| canonical_ticker | token_ticker | asset_class | issuer_platform | network | token_contract_address | venue | venue_symbol | price_source_type | market_type |",
                "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
                sample_table,
                "",
                "## Files",
                "",
                f"- CSV: `{OUT_CSV}`",
                f"- JSON: `{OUT_JSON}`",
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def main() -> None:
    detailed_rows = read_csv(SOURCEABLE_PAIRS_PATH)
    all_rows = read_csv(ALL_TICKERS_PATH)
    detailed_keys = {
        (
            row.get("rwa_ticker", ""),
            row.get("network", ""),
            row.get("token_contract_address", ""),
        )
        for row in detailed_rows
    }
    pending_rows = [
        row
        for row in all_rows
        if (
            row.get("rwa_ticker", ""),
            row.get("network", ""),
            row.get("token_contract_address", ""),
        )
        not in detailed_keys
    ]
    token_rows = build_token_feed_rows(detailed_rows + pending_rows)
    derivative_rows = build_derivative_rows(read_csv(DERIVATIVE_VENUES_PATH))
    all_feed_rows = sorted(
        [*token_rows, *derivative_rows],
        key=lambda row: (
            row["canonical_ticker"],
            row["token_ticker"],
            row["network"],
            row["venue"],
            row["price_source_type"],
            row["venue_symbol"],
        ),
    )
    summary = summarize(all_feed_rows)
    write_outputs(all_feed_rows, summary)
    print(json.dumps(summary, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
