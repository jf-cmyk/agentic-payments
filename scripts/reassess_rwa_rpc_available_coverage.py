#!/usr/bin/env python3
"""Reassess RWA price coverage from the latest live probe.

This script intentionally treats direct pool/RPC pricing separately from
derivative/reference venue prices. It produces token-row price candidates and a
recomputed Blocksize workbook coverage table using the latest probe output.
"""

from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


TOKEN_OR_POOL_VENUES = {
    "aerodrome_slipstream",
    "balancer_pools",
    "curve_stableswap",
    "jupiter_router",
    "meteora_dlmm",
    "orca_whirlpool",
    "raydium_clmm",
    "uniswap_v3_v4",
}
SPOT_OR_TOKENIZED_MARKET_VENUES = {
    "hyperliquid_paxg",
    "hyperliquid_rwa_spot",
    "hyperliquid_spot",
}
SYNTHETIC_OR_DERIVATIVE_VENUES = {
    "aevo",
    "apex_omni",
    "aster",
    "derive",
    "drift",
    "dydx",
    "gains",
    "hyperliquid_perps",
    "lighter",
    "orderly",
    "ostium",
}
ROLE_PRIORITY = {
    "token_or_pool": 0,
    "venue_spot_or_tokenized_market": 1,
    "synthetic_or_derivative_benchmark": 2,
    "reference_only": 3,
    "unknown": 4,
}


def normalize(value: Any) -> str:
    text = str(value or "").upper().strip()
    return re.sub(r"[^A-Z0-9]", "", text)


def symbol_variants(value: Any) -> set[str]:
    text = str(value or "").upper().strip()
    variants: set[str] = set()
    if not text:
        return variants
    variants.add(normalize(text))
    for sep in ("/", "-", "_", ":", " "):
        if sep in text:
            base = text.split(sep, 1)[0]
            quote = text.split(sep, 1)[1]
            variants.add(normalize(base))
            variants.add(normalize(base + quote))
            break
    compact = normalize(text)
    for suffix in ("USD", "USDC", "USDT", "PERP"):
        if compact.endswith(suffix) and len(compact) > len(suffix):
            variants.add(compact[: -len(suffix)])
    if compact.endswith("X") and len(compact) > 2:
        variants.add(compact[:-1])
    generic_or_system_keys = {"RWA", "XYZ", "RWAXYZ"}
    return {v for v in variants if v and v not in generic_or_system_keys}


def row_keys(row: dict[str, Any], fields: list[str]) -> set[str]:
    keys: set[str] = set()
    for field in fields:
        keys.update(symbol_variants(row.get(field)))
    return keys


def source_role(row: dict[str, Any]) -> str:
    venue = str(row.get("venue") or "")
    source_type = str(row.get("source_type") or "")
    if venue in TOKEN_OR_POOL_VENUES or "pool" in source_type or "route" in source_type:
        return "token_or_pool"
    if venue in SPOT_OR_TOKENIZED_MARKET_VENUES or "spot" in source_type:
        return "venue_spot_or_tokenized_market"
    if venue in SYNTHETIC_OR_DERIVATIVE_VENUES:
        return "synthetic_or_derivative_benchmark"
    if row.get("reference_only") in {True, "True", "true"}:
        return "reference_only"
    return "unknown"


def price_type(row: dict[str, Any]) -> str:
    if row.get("kind") == "vwap" or row.get("vwap") not in {None, ""}:
        return "VWAP"
    if row.get("kind") == "bidask" or row.get("bid") not in {None, ""} or row.get("ask") not in {None, ""}:
        return "BidAsk"
    return "State Data"


def best_value(row: dict[str, Any]) -> Any:
    for key in ("vwap", "value", "mid", "price", "last"):
        if row.get(key) not in {None, ""}:
            return row.get(key)
    bid = row.get("bid")
    ask = row.get("ask")
    try:
        if bid not in {None, ""} and ask not in {None, ""}:
            return (float(bid) + float(ask)) / 2
    except (TypeError, ValueError):
        return ""
    return ""


def price_lane(row: dict[str, Any]) -> str:
    asset_class = str(row.get("asset_class") or "").lower()
    platform = str(row.get("platform") or "").lower()
    network = str(row.get("network") or "").lower()
    if any(term in asset_class for term in ("equity", "stock", "etf")):
        return "venue_orderbook_or_tokenized_spot + dex_pool_discovery"
    if any(term in asset_class for term in ("treasury", "fund", "credit", "bond")):
        return "issuer_nav_or_transfer_agent + dex_pool_discovery"
    if any(term in asset_class for term in ("metal", "commodity")):
        return "venue_orderbook_or_spot + issuer_reserve_reference"
    if "fx" in asset_class or "currency" in asset_class:
        return "venue_fx_quote + stablecoin_dex_pool"
    if "real estate" in asset_class or "real_estate" in asset_class:
        return "issuer_nav_or_appraisal_reference"
    if platform or network:
        return "token_or_pool_price_source_lane"
    return "source_lane_not_classified"


def next_action_for(row: dict[str, Any]) -> str:
    lane = price_lane(row)
    if "issuer_nav" in lane:
        return "Request issuer NAV/share-price endpoint with timestamp and redistribution rights; pair with pool or venue liquidity where available."
    if "dex_pool" in lane or "stablecoin_dex_pool" in lane:
        return "Configure direct RPC/pool-state adapter and benchmark against independent venue/oracle sources."
    if "venue" in lane:
        return "Add direct venue order-book/quote feed, rights evidence, and 5m/30m/24h quality windows."
    return "Map ticker to a reviewed price source lane, then add replayable source payload capture."


def load_probe(path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("results", data if isinstance(data, list) else [])
    if not isinstance(rows, list):
        raise ValueError(f"probe results not found in {path}")
    return data, rows


def write_csv(path: Path, fieldnames: list[str], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def build_price_lane(args: argparse.Namespace, ok_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    tokens = list(csv.DictReader(args.tokens_csv.open("r", newline="", encoding="utf-8")))
    candidates_by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in ok_rows:
        for key in row_keys(row, ["asset_id", "symbol"]):
            candidates_by_key[key].append(row)

    full_rows: list[dict[str, Any]] = []
    best_rows: list[dict[str, Any]] = []
    for token in tokens:
        keys = row_keys(
            token,
            ["asset_id", "symbol", "rwa_xyz_ticker", "canonical_underlying_candidate", "token_name", "address"],
        )
        matches_by_id = {
            match.get("feed_id") or f"{match.get('venue')}:{match.get('symbol')}": match
            for key in keys
            for match in candidates_by_key.get(key, [])
        }
        matches = list(matches_by_id.values())
        matches.sort(
            key=lambda r: (
                ROLE_PRIORITY.get(source_role(r), 9),
                0 if r.get("source_timestamp_present") in {True, "True", "true"} else 1,
                str(r.get("venue") or ""),
                str(r.get("symbol") or ""),
            )
        )
        common = {
            "token_row_id": token.get("token_row_id", ""),
            "ticker": token.get("rwa_xyz_ticker") or token.get("asset_id") or token.get("symbol", ""),
            "symbol": token.get("symbol", ""),
            "asset_id": token.get("asset_id", ""),
            "asset_class": token.get("asset_class", ""),
            "platform": token.get("platform", ""),
            "network": token.get("network", ""),
            "token_address": token.get("address", ""),
            "price_source_lane": price_lane(token),
        }
        if not matches:
            full_rows.append(
                {
                    **common,
                    "price_status": "not_fetched_lane_requires_access_or_adapter",
                    "price_type": "",
                    "source_role": "no_live_price_source_yet",
                    "venue": "",
                    "source_symbol": "",
                    "feed_id": "",
                    "bid": "",
                    "ask": "",
                    "mid_price": "",
                    "vwap": "",
                    "value": "",
                    "price_timestamp": "",
                    "source_timestamp_present": False,
                    "freshness_ms": "",
                    "latency_ms": "",
                    "production_grade": "false",
                    "production_blockers": "",
                    "raw_payload_replayable": False,
                    "reference_only": False,
                    "method": "",
                    "next_action": next_action_for(token),
                }
            )
            best_rows.append(
                {
                    **common,
                    "best_price_status": "not_fetched_lane_requires_access_or_adapter",
                    "best_price": "",
                    "best_price_type": "",
                    "best_source_role": "no_live_price_source_yet",
                    "best_venue": "",
                    "best_source_symbol": "",
                    "bid": "",
                    "ask": "",
                    "mid_price": "",
                    "vwap": "",
                    "price_timestamp": "",
                    "source_timestamp_present": False,
                    "production_grade": "false",
                    "candidate_source_count": 0,
                    "candidate_venues": "",
                    "next_action": next_action_for(token),
                }
            )
            continue

        for match in matches:
            full_rows.append(
                {
                    **common,
                    "price_status": "candidate_price_fetched",
                    "price_type": price_type(match),
                    "source_role": source_role(match),
                    "venue": match.get("venue", ""),
                    "source_symbol": match.get("symbol", ""),
                    "feed_id": match.get("feed_id", ""),
                    "bid": match.get("bid", ""),
                    "ask": match.get("ask", ""),
                    "mid_price": match.get("value", ""),
                    "vwap": match.get("vwap", ""),
                    "value": best_value(match),
                    "price_timestamp": match.get("tested_at", ""),
                    "source_timestamp_present": match.get("source_timestamp_present", False),
                    "freshness_ms": match.get("freshness_ms", ""),
                    "latency_ms": match.get("latency_ms", ""),
                    "production_grade": "false",
                    "production_blockers": match.get("production_blockers", ""),
                    "raw_payload_replayable": match.get("raw_payload_replayable", False),
                    "reference_only": match.get("reference_only", False),
                    "method": "live_probe_candidate_match_by_symbol_or_underlying",
                    "next_action": "Run benchmark alignment, replay capture, rights, manipulation/depth checks, and continuous windows before promotion.",
                }
            )

        best = matches[0]
        best_rows.append(
            {
                **common,
                "best_price_status": "candidate_price_fetched",
                "best_price": best_value(best),
                "best_price_type": price_type(best),
                "best_source_role": source_role(best),
                "best_venue": best.get("venue", ""),
                "best_source_symbol": best.get("symbol", ""),
                "bid": best.get("bid", ""),
                "ask": best.get("ask", ""),
                "mid_price": best.get("value", ""),
                "vwap": best.get("vwap", ""),
                "price_timestamp": best.get("tested_at", ""),
                "source_timestamp_present": best.get("source_timestamp_present", False),
                "production_grade": "false",
                "candidate_source_count": len(matches),
                "candidate_venues": "|".join(sorted({str(m.get("venue") or "") for m in matches if m.get("venue")})),
                "next_action": "Run benchmark alignment, replay capture, rights, manipulation/depth checks, and continuous windows before promotion.",
            }
        )
    return full_rows, best_rows


def build_coverage(args: argparse.Namespace, ok_rows: list[dict[str, Any]], best_rows: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    source_keys: set[str] = set()
    for row in ok_rows:
        source_keys.update(row_keys(row, ["asset_id", "symbol"]))
    for row in best_rows:
        if row.get("best_price_status") == "candidate_price_fetched":
            source_keys.update(row_keys(row, ["ticker", "symbol", "asset_id", "best_source_symbol"]))

    detail_rows = list(csv.DictReader(args.coverage_detail_template.open("r", newline="", encoding="utf-8")))
    updated: list[dict[str, Any]] = []
    grouped: dict[str, dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    for row in detail_rows:
        keys = row_keys(row, ["match_key", "ticker", "name"])
        sourced = bool(source_keys.intersection(keys))
        row = {**row, "our_sourced_candidate": str(sourced)}
        updated.append(row)
        category = row.get("category", "")
        ident = str(row.get("match_key") or row.get("ticker") or row.get("name") or "").strip()
        if not ident:
            continue
        grouped[category]["target"].add(ident)
        if sourced:
            grouped[category]["our"].add(ident)
        if row.get("pyth_match") == "True":
            grouped[category]["pyth"].add(ident)
        if row.get("chainlink_public_match") == "True":
            grouped[category]["chainlink"].add(ident)

    summary: list[dict[str, Any]] = []
    for category in sorted(grouped):
        target = len(grouped[category]["target"])
        our = len(grouped[category]["our"])
        pyth = len(grouped[category]["pyth"])
        chainlink = len(grouped[category]["chainlink"])
        summary.append(
            {
                "category": category,
                "target_unique_tickers_or_pairs": target,
                "our_sourced_candidate_count": our,
                "our_sourced_candidate_pct": (our / target) if target else 0,
                "pyth_match_count": pyth,
                "pyth_match_pct": (pyth / target) if target else 0,
                "chainlink_public_match_count": chainlink,
                "chainlink_public_match_pct": (chainlink / target) if target else 0,
            }
        )
    return updated, summary


def pct(value: float) -> str:
    return f"{value * 100:.1f}%"


def write_markdown(args: argparse.Namespace, probe: dict[str, Any], full_rows: list[dict[str, Any]], best_rows: list[dict[str, Any]], coverage: list[dict[str, Any]]) -> None:
    token_total = len(best_rows)
    priced = sum(1 for r in best_rows if r.get("best_price_status") == "candidate_price_fetched")
    role_counts = Counter(r.get("best_source_role") for r in best_rows if r.get("best_price_status") == "candidate_price_fetched")
    venue_counts = Counter(r.get("best_venue") for r in best_rows if r.get("best_price_status") == "candidate_price_fetched")
    pool_priced = sum(1 for r in best_rows if r.get("best_source_role") == "token_or_pool")
    production = sum(1 for r in best_rows if r.get("production_grade") == "true")
    summary = probe.get("summary", {})

    lines = [
        "# RWA RPC-Available Price Coverage Reassessment",
        "",
        f"Probe used: `{args.probe_json}`",
        f"Generated at: `{summary.get('generated_at', '')}`",
        "",
        "## Live Probe Result",
        "",
        f"- Feed catalog rows: {summary.get('total_feed_rows', 0):,}",
        f"- Live attempted rows: {summary.get('live_attempted_rows', 0):,}",
        f"- OK price rows: {summary.get('ok_rows', 0):,}",
        f"- Error rows: {summary.get('probe_status_counts', {}).get('error', 0):,}",
        f"- Not-live-wired rows: {summary.get('probe_status_counts', {}).get('not_live_wired', 0):,}",
        f"- Unique OK symbols: {summary.get('unique_ok_symbols', 0):,}",
        f"- Unique OK venues: {summary.get('unique_ok_venues', 0):,}",
        f"- Production-promoted rows: {summary.get('production_promoted_rows', 0):,}",
        "",
        "## Token-Row Price Lane",
        "",
        f"- RWA.xyz token rows assessed: {token_total:,}",
        f"- Token rows with at least one candidate price: {priced:,} ({pct(priced / token_total if token_total else 0)})",
        f"- Token rows still requiring access/adapter/source work: {token_total - priced:,} ({pct((token_total - priced) / token_total if token_total else 0)})",
        f"- Token/pool lane priced rows: {pool_priced:,}",
        f"- Production-grade token rows: {production:,}",
        "",
        "Best-source roles:",
    ]
    for role, count in role_counts.most_common():
        lines.append(f"- {role}: {count:,}")
    lines.extend(["", "Top best-source venues:"])
    for venue, count in venue_counts.most_common(15):
        lines.append(f"- {venue}: {count:,}")
    lines.extend(["", "## Workbook Coverage", ""])
    lines.append("| Category | Target | Ours | Ours % | Pyth % | Chainlink % |")
    lines.append("|---|---:|---:|---:|---:|---:|")
    for row in coverage:
        lines.append(
            "| {category} | {target_unique_tickers_or_pairs:,} | {our_sourced_candidate_count:,} | {our_pct} | {pyth_pct} | {chainlink_pct} |".format(
                category=row["category"],
                target_unique_tickers_or_pairs=row["target_unique_tickers_or_pairs"],
                our_sourced_candidate_count=row["our_sourced_candidate_count"],
                our_pct=pct(float(row["our_sourced_candidate_pct"])),
                pyth_pct=pct(float(row["pyth_match_pct"])),
                chainlink_pct=pct(float(row["chainlink_public_match_pct"])),
            )
        )
    lines.extend(
        [
            "",
            "## Quality Read",
            "",
            "- These are candidate prices, not production-grade feeds.",
            "- Solana RPC account-state capture is available when `SOLANA_RPC_URL` is exported, but pool decoders, replayable L2 depth, and manipulation/depth checks are still required for production.",
            "- EVM pool coverage is partially live for allowlisted Uniswap/Aerodrome CLMM pools and Balancer pair-metadata candidates; Curve discovery and full invariant/tick-range replay remain gated by additional adapters and replay/depth checks.",
            "- Jupiter/Raydium quote-derived rows are executable quote snapshots, not direct pool-state replay.",
            "- Production promotion still requires replayable raw payloads, source timestamps, 5m/30m/24h windows, benchmark alignment, manipulation/depth checks, rights clearance, and multi-source consensus where possible.",
            "",
            "## Outputs",
            "",
            f"- Full token candidate file: `{args.full_out}`",
            f"- Best token candidate file: `{args.best_out}`",
            f"- Coverage summary CSV: `{args.coverage_summary_out}`",
            f"- Coverage detail CSV: `{args.coverage_detail_out}`",
        ]
    )
    args.markdown_out.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--probe-json", type=Path, required=True)
    parser.add_argument("--tokens-csv", type=Path, default=Path("reports/rwa_xyz_new_asset_monitor_tokens.csv"))
    parser.add_argument("--coverage-detail-template", type=Path, default=Path("reports/blocksize_feed_coverage_detail_2026-07-15.csv"))
    parser.add_argument("--full-out", type=Path, required=True)
    parser.add_argument("--best-out", type=Path, required=True)
    parser.add_argument("--coverage-detail-out", type=Path, required=True)
    parser.add_argument("--coverage-summary-out", type=Path, required=True)
    parser.add_argument("--markdown-out", type=Path, required=True)
    args = parser.parse_args()

    probe, rows = load_probe(args.probe_json)
    ok_rows = [row for row in rows if row.get("probe_status") == "ok"]
    full_rows, best_rows = build_price_lane(args, ok_rows)

    full_fields = [
        "token_row_id",
        "ticker",
        "symbol",
        "asset_id",
        "asset_class",
        "platform",
        "network",
        "token_address",
        "price_source_lane",
        "price_status",
        "price_type",
        "source_role",
        "venue",
        "source_symbol",
        "feed_id",
        "bid",
        "ask",
        "mid_price",
        "vwap",
        "value",
        "price_timestamp",
        "source_timestamp_present",
        "freshness_ms",
        "latency_ms",
        "production_grade",
        "production_blockers",
        "raw_payload_replayable",
        "reference_only",
        "method",
        "next_action",
    ]
    best_fields = [
        "token_row_id",
        "ticker",
        "symbol",
        "asset_id",
        "asset_class",
        "platform",
        "network",
        "token_address",
        "best_price_status",
        "best_price",
        "best_price_type",
        "best_source_role",
        "best_venue",
        "best_source_symbol",
        "bid",
        "ask",
        "mid_price",
        "vwap",
        "price_timestamp",
        "source_timestamp_present",
        "production_grade",
        "price_source_lane",
        "candidate_source_count",
        "candidate_venues",
        "next_action",
    ]
    write_csv(args.full_out, full_fields, full_rows)
    write_csv(args.best_out, best_fields, best_rows)

    detail_rows, coverage_rows = build_coverage(args, ok_rows, best_rows)
    write_csv(args.coverage_detail_out, list(detail_rows[0].keys()), detail_rows)
    coverage_fields = [
        "category",
        "target_unique_tickers_or_pairs",
        "our_sourced_candidate_count",
        "our_sourced_candidate_pct",
        "pyth_match_count",
        "pyth_match_pct",
        "chainlink_public_match_count",
        "chainlink_public_match_pct",
    ]
    write_csv(args.coverage_summary_out, coverage_fields, coverage_rows)
    write_markdown(args, probe, full_rows, best_rows, coverage_rows)

    priced = sum(1 for r in best_rows if r.get("best_price_status") == "candidate_price_fetched")
    print(
        json.dumps(
            {
                "ok_probe_rows": len(ok_rows),
                "token_rows": len(best_rows),
                "token_rows_with_candidate_price": priced,
                "coverage_categories": len(coverage_rows),
                "outputs": {
                    "full": str(args.full_out),
                    "best": str(args.best_out),
                    "coverage_summary": str(args.coverage_summary_out),
                    "coverage_detail": str(args.coverage_detail_out),
                    "markdown": str(args.markdown_out),
                },
            },
            indent=2,
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
