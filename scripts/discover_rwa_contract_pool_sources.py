#!/usr/bin/env python3
"""Discover sourceable DEX pools for unfetched RWA token contract rows.

This is intentionally contract-address first. It does not use off-chain equity
vendors as a price source; a row is only made probeable when public pair
metadata shows the exact token contract in a DEX pool.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.rwa_evm_pool_discovery import _enrich_pair_row_with_evm_state  # noqa: E402


DEXSCREENER_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search/"
STABLE_QUOTES = {"USDC", "USDT", "USDBC", "DAI", "USD0", "USD1", "PYUSD", "FRAX"}
CHAIN_BY_NETWORK = {
    "Ethereum": "ethereum",
    "Base": "base",
    "Arbitrum": "arbitrum",
    "Polygon": "polygon",
    "Optimism": "optimism",
    "BNB Chain": "bsc",
    "Avalanche C-Chain": "avalanche",
    "Mantle": "mantle",
    "Ink": "ink",
    "Plume": "plume",
    "Sonic": "sonic",
    "Gnosis": "gnosischain",
    "Celo": "celo",
}
VENUE_BY_DEX = (
    ("aerodrome", "aerodrome_slipstream"),
    ("uniswap", "uniswap_v3_v4"),
    ("pancakeswap", "uniswap_v3_v4"),
    ("sushiswap", "uniswap_v3_v4"),
    ("curve", "curve_stableswap"),
    ("balancer", "balancer_pools"),
)


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _is_evm_address(value: Any) -> bool:
    text = str(value or "")
    return (
        text.startswith("0x")
        and len(text) == 42
        and all(char in "0123456789abcdefABCDEF" for char in text[2:])
    )


def _normal(value: Any) -> str:
    return str(value or "").strip().upper().replace("-", "").replace("_", "")


def _venue_for_pair(pair: dict[str, Any]) -> str | None:
    dex_id = str(pair.get("dexId") or "").lower()
    labels = " ".join(str(label).lower() for label in pair.get("labels") or [])
    haystack = f"{dex_id} {labels}"
    for needle, venue in VENUE_BY_DEX:
        if needle in haystack:
            return venue
    return None


def _liquidity_usd(pair: dict[str, Any]) -> float:
    liquidity = pair.get("liquidity") if isinstance(pair.get("liquidity"), dict) else {}
    try:
        return float(liquidity.get("usd") or 0)
    except (TypeError, ValueError):
        return 0.0


def _volume_h24(pair: dict[str, Any]) -> float:
    volume = pair.get("volume") if isinstance(pair.get("volume"), dict) else {}
    try:
        return float(volume.get("h24") or 0)
    except (TypeError, ValueError):
        return 0.0


def _fetch_search(query: str, *, timeout_seconds: float) -> tuple[dict[str, Any], str | None]:
    url = f"{DEXSCREENER_SEARCH_URL}?{urllib.parse.urlencode({'q': query})}"
    request = urllib.request.Request(
        url,
        headers={
            "accept": "application/json",
            "user-agent": "BlocksizeRWAContractPoolDiscovery/1.0",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            payload = json.loads(response.read().decode("utf-8"))
        return payload if isinstance(payload, dict) else {}, None
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return {}, f"{type(exc).__name__}: {exc}"


def _pair_for_token(row: dict[str, Any], pairs: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, str]:
    address = str(row.get("token_contract_address") or row.get("token_address") or "").lower()
    expected_chain = CHAIN_BY_NETWORK.get(str(row.get("network") or ""))
    candidates: list[dict[str, Any]] = []
    rejected = Counter()
    for pair in pairs:
        if not isinstance(pair, dict):
            continue
        if expected_chain and str(pair.get("chainId") or "").lower() != expected_chain:
            rejected["wrong_chain"] += 1
            continue
        venue = _venue_for_pair(pair)
        if not venue:
            rejected["unsupported_dex"] += 1
            continue
        base_token = pair.get("baseToken") if isinstance(pair.get("baseToken"), dict) else {}
        quote_token = pair.get("quoteToken") if isinstance(pair.get("quoteToken"), dict) else {}
        base_address = str(base_token.get("address") or "").lower()
        quote_address = str(quote_token.get("address") or "").lower()
        base_symbol = _normal(base_token.get("symbol"))
        quote_symbol = _normal(quote_token.get("symbol"))
        token_is_base = base_address == address and quote_symbol in STABLE_QUOTES
        token_is_quote = quote_address == address and base_symbol in STABLE_QUOTES
        if not token_is_base and not token_is_quote:
            rejected["not_stable_quoted_exact_contract"] += 1
            continue
        enriched = dict(pair)
        enriched["_venue"] = venue
        enriched["_token_is_base"] = token_is_base
        candidates.append(enriched)
    if not candidates:
        reason = ",".join(f"{key}:{value}" for key, value in sorted(rejected.items())) or "no_pairs"
        return None, reason
    candidates.sort(key=lambda item: (_liquidity_usd(item), _volume_h24(item)), reverse=True)
    return candidates[0], "matched"


def _pool_row(token_row: dict[str, Any], pair: dict[str, Any]) -> dict[str, Any]:
    token_address = str(token_row["token_contract_address"]).lower()
    base_token = pair.get("baseToken") if isinstance(pair.get("baseToken"), dict) else {}
    quote_token = pair.get("quoteToken") if isinstance(pair.get("quoteToken"), dict) else {}
    token_is_base = bool(pair.get("_token_is_base"))
    if token_is_base:
        base_address = base_token.get("address")
        quote_address = quote_token.get("address")
        base_symbol = token_row.get("asset_id") or base_token.get("symbol")
        quote_symbol = quote_token.get("symbol") or "USDC"
    else:
        base_address = quote_token.get("address")
        quote_address = base_token.get("address")
        base_symbol = token_row.get("asset_id") or quote_token.get("symbol")
        quote_symbol = base_token.get("symbol") or "USDC"
    venue = str(pair["_venue"])
    symbol = f"{base_symbol}/{quote_symbol}"
    pair_address = pair.get("pairAddress")
    return {
        "allowlist_id": f"rwa_contract:{venue}:{token_address}:{quote_symbol}",
        "venue": venue,
        "symbol": symbol,
        "asset_id": token_row.get("asset_id"),
        "token_row_id": token_row.get("token_row_id"),
        "rwa_ticker": token_row.get("rwa_ticker"),
        "asset_class": token_row.get("asset_class"),
        "platform": token_row.get("platform"),
        "network": token_row.get("network"),
        "chain": str(pair.get("chainId") or "").lower(),
        "chain_id": str(pair.get("chainId") or "").lower(),
        "dex_id": pair.get("dexId"),
        "pool_address": pair_address,
        "pool_id": pair_address,
        "base_token": str(base_address or "").lower(),
        "quote_token": str(quote_address or "").lower(),
        "base_symbol": base_symbol,
        "quote_symbol": quote_symbol,
        "fee_tier": None,
        "fee_tier_status": "not_exposed_by_public_pair_search",
        "block_number": None,
        "tick_state": None,
        "balances": None,
        "weights": None,
        "amplification": None,
        "price_usd": pair.get("priceUsd"),
        "liquidity_usd": (pair.get("liquidity") or {}).get("usd")
        if isinstance(pair.get("liquidity"), dict)
        else None,
        "volume_h24": (pair.get("volume") or {}).get("h24")
        if isinstance(pair.get("volume"), dict)
        else None,
        "pair_created_at": pair.get("pairCreatedAt"),
        "url": pair.get("url"),
        "source": "dexscreener_public_pair_search_by_token_contract",
        "review_status": "contract_pool_identity_ready_pending_evm_rpc_state",
    }


def discover_contract_pools(
    *,
    input_csv: Path,
    min_liquidity_usd: float,
    sleep_seconds: float,
    timeout_seconds: float,
    offset: int,
    max_rows: int | None,
    progress_every: int,
) -> dict[str, Any]:
    rows = list(csv.DictReader(input_csv.open()))
    targets = [
        row for row in rows
        if row.get("token_contract_address")
        and row.get("sourceability_status") != "candidate_price_fetched"
        and _is_evm_address(row.get("token_contract_address"))
    ]
    total_targets = len(targets)
    targets = targets[max(0, offset):]
    if max_rows is not None:
        targets = targets[:max_rows]
    cache: dict[str, tuple[dict[str, Any], str | None]] = {}
    pools: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for index, row in enumerate(targets, start=1):
        address = str(row["token_contract_address"]).lower()
        if address not in cache:
            cache[address] = _fetch_search(address, timeout_seconds=timeout_seconds)
            if sleep_seconds:
                time.sleep(sleep_seconds)
        if progress_every and index % progress_every == 0:
            print(
                json.dumps(
                    {
                        "progress": index,
                        "batch_targets": len(targets),
                        "global_offset": offset,
                        "pools": len(pools),
                        "errors": len(errors),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
        payload, fetch_error = cache[address]
        if fetch_error:
            errors.append({**row, "error": fetch_error, "error_category": "dexscreener_fetch_error"})
            continue
        pairs = payload.get("pairs") if isinstance(payload.get("pairs"), list) else []
        pair, reason = _pair_for_token(row, pairs)
        if not pair:
            errors.append({**row, "error": reason, "error_category": "no_contract_stable_pool"})
            continue
        if _liquidity_usd(pair) < min_liquidity_usd:
            errors.append(
                {
                    **row,
                    "error": f"liquidity_below_threshold:{_liquidity_usd(pair):.2f}",
                    "error_category": "liquidity_below_threshold",
                }
            )
            continue
        pools.append(_enrich_pair_row_with_evm_state(_pool_row(row, pair)))

    return {
        "product": "rwa_contract_pool_sources",
        "generated_at": _utc_now_iso(),
        "inputs": {
            "input_csv": str(input_csv),
            "min_liquidity_usd": min_liquidity_usd,
            "timeout_seconds": timeout_seconds,
            "offset": offset,
            "max_rows": max_rows,
            "total_target_count_before_offset": total_targets,
            "target_count": len(targets),
        },
        "summary": {
            "target_count": len(targets),
            "pool_count": len(pools),
            "error_count": len(errors),
            "by_venue": dict(sorted(Counter(str(row.get("venue")) for row in pools).items())),
            "by_chain": dict(sorted(Counter(str(row.get("chain")) for row in pools).items())),
            "by_error_category": dict(sorted(Counter(str(row.get("error_category")) for row in errors).items())),
        },
        "pools": sorted(pools, key=lambda row: (str(row.get("venue")), str(row.get("asset_id")), str(row.get("base_token")))),
        "errors": sorted(errors, key=lambda row: (str(row.get("asset_id")), str(row.get("token_contract_address")))),
    }


def write_outputs(report: dict[str, Any], *, json_out: Path, csv_out: Path, probe_csv_out: Path) -> None:
    json_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    pool_fields = [
        "allowlist_id",
        "venue",
        "symbol",
        "asset_id",
        "token_row_id",
        "rwa_ticker",
        "asset_class",
        "platform",
        "network",
        "chain",
        "dex_id",
        "pool_address",
        "base_token",
        "quote_token",
        "liquidity_usd",
        "volume_h24",
        "block_number",
        "review_status",
        "url",
    ]
    with csv_out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=pool_fields)
        writer.writeheader()
        for row in report["pools"]:
            writer.writerow({key: row.get(key) for key in pool_fields})
    probe_fields = [
        "feed_id",
        "kind",
        "asset_id",
        "asset_classes",
        "symbol",
        "venue",
        "source_type",
        "promotion_status",
        "production_promoted",
    ]
    with probe_csv_out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=probe_fields)
        writer.writeheader()
        for row in report["pools"]:
            source_type = "onchain_clmm_pool" if row.get("venue") in {"uniswap_v3_v4", "aerodrome_slipstream"} else "onchain_stableswap_pool"
            for kind in ("bidask", "vwap"):
                writer.writerow(
                    {
                        "feed_id": f"rwa_contract_{kind}:{row.get('venue')}:{row.get('token_row_id')}:{row.get('base_token')}",
                        "kind": kind,
                        "asset_id": row.get("asset_id"),
                        "asset_classes": json.dumps([row.get("asset_class") or "tokenized_asset"]),
                        "symbol": row.get("base_token"),
                        "venue": row.get("venue"),
                        "source_type": source_type,
                        "promotion_status": "production_blocked_missing_discovery",
                        "production_promoted": "False",
                    }
                )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-csv", type=Path, default=Path("reports/rwa_all_tickers_token_contracts_sourceability_2026-07-16.csv"))
    parser.add_argument("--json-out", type=Path, default=Path("reports/rwa_contract_pool_sources_2026-07-16.json"))
    parser.add_argument("--csv-out", type=Path, default=Path("reports/rwa_contract_pool_sources_2026-07-16.csv"))
    parser.add_argument("--probe-csv-out", type=Path, default=Path("reports/rwa_contract_pool_probe_input_2026-07-16.csv"))
    parser.add_argument("--min-liquidity-usd", type=float, default=0.0)
    parser.add_argument("--sleep-seconds", type=float, default=0.05)
    parser.add_argument("--timeout-seconds", type=float, default=6.0)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--max-rows", type=int)
    parser.add_argument("--progress-every", type=int, default=100)
    args = parser.parse_args()
    report = discover_contract_pools(
        input_csv=args.input_csv,
        min_liquidity_usd=args.min_liquidity_usd,
        sleep_seconds=args.sleep_seconds,
        timeout_seconds=args.timeout_seconds,
        offset=args.offset,
        max_rows=args.max_rows,
        progress_every=args.progress_every,
    )
    write_outputs(report, json_out=args.json_out, csv_out=args.csv_out, probe_csv_out=args.probe_csv_out)
    print(json.dumps(report["summary"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
