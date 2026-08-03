"""Discover EVM RWA pool candidates from public pair metadata."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from src.rwa_dex_allowlist import build_dex_allowlist
from src.rwa_feed_discovery import DEFAULT_REPORTS_DIR
from src.runtime_data import resolve_required_rwa_report_path


DEFAULT_EVM_POOL_ALLOWLIST_JSON_PATH = resolve_required_rwa_report_path(
    "rwa_evm_pool_allowlist.json"
)
DEFAULT_EVM_POOL_ALLOWLIST_CSV_PATH = DEFAULT_REPORTS_DIR / "rwa_evm_pool_allowlist.csv"
DEFAULT_TOKEN_ADDRESS_MASTER_PATH = DEFAULT_REPORTS_DIR / "token_address_master_2026-07-15.csv"
DEXSCREENER_SEARCH_URL = "https://api.dexscreener.com/latest/dex/search/"

EVM_RPC_ENV_BY_CHAIN = {
    "ethereum": "EVM_RPC_ETHEREUM_URL",
    "base": "EVM_RPC_BASE_URL",
    "arbitrum": "EVM_RPC_ARBITRUM_URL",
    "polygon": "EVM_RPC_POLYGON_URL",
    "optimism": "EVM_RPC_OPTIMISM_URL",
}

PUBLIC_EVM_RPC_FALLBACKS = {
    "ethereum": [
        ("ethereum-rpc.publicnode.com", "https://ethereum-rpc.publicnode.com"),
    ],
    "base": [
        ("mainnet.base.org", "https://mainnet.base.org"),
        ("base-rpc.publicnode.com", "https://base-rpc.publicnode.com"),
    ],
}

EVM_POOL_CALL_SELECTORS = {
    "token0": "0x0dfe1681",
    "token1": "0xd21220a7",
    "fee": "0xddca3f43",
    "tick_spacing": "0xd0c93a7c",
    "liquidity": "0x1a686502",
    "slot0": "0x3850c7bd",
}

VENUE_DEX_MATCHERS = {
    "uniswap_v3_v4": ("uniswap",),
    "curve_stableswap": ("curve",),
    "balancer_pools": ("balancer",),
    "aerodrome_slipstream": ("aerodrome",),
}

VENUE_CHAIN_MATCHERS = {
    "aerodrome_slipstream": ("base",),
    "uniswap_v3_v4": ("ethereum", "base", "arbitrum", "optimism", "polygon"),
    "curve_stableswap": ("ethereum", "base", "arbitrum", "optimism", "polygon"),
    "balancer_pools": ("ethereum", "base", "arbitrum", "optimism", "polygon"),
}


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _fetch_search(query: str) -> dict[str, Any]:
    url = f"{DEXSCREENER_SEARCH_URL}?{urllib.parse.urlencode({'q': query})}"
    request = urllib.request.Request(
        url,
        headers={
            "accept": "application/json",
            "user-agent": "BlocksizeRWAFeedDiscovery/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _safe_fetch_search(query: str) -> tuple[dict[str, Any], str | None]:
    try:
        return _fetch_search(query), None
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return {}, f"{type(exc).__name__}: {exc}"


def _rpc_candidates(chain: str) -> list[tuple[str, str]]:
    chain_key = str(chain or "").lower()
    candidates: list[tuple[str, str]] = []
    env_name = EVM_RPC_ENV_BY_CHAIN.get(chain_key)
    if env_name:
        rpc_url = os.getenv(env_name)
        if rpc_url:
            candidates.append((f"env:{env_name}", rpc_url))
    if os.getenv("RWA_EVM_DISABLE_PUBLIC_RPC_FALLBACKS", "").strip().lower() not in {"1", "true", "yes"}:
        candidates.extend(
            (f"public_fallback:{label}", url)
            for label, url in PUBLIC_EVM_RPC_FALLBACKS.get(chain_key, [])
        )
    return candidates


def _json_rpc(url: str, method: str, params: list[Any]) -> Any:
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": method, "params": params}).encode(
        "utf-8"
    )
    request = urllib.request.Request(
        url,
        data=body,
        headers={
            "accept": "application/json",
            "content-type": "application/json",
            "user-agent": "BlocksizeRWAFeedDiscovery/1.0",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if isinstance(payload, dict) and payload.get("error"):
        raise RuntimeError(json.dumps(payload["error"], sort_keys=True))
    return payload.get("result") if isinstance(payload, dict) else None


def _safe_json_rpc(url: str, method: str, params: list[Any]) -> tuple[Any, str | None]:
    try:
        return _json_rpc(url, method, params), None
    except (OSError, RuntimeError, urllib.error.URLError, json.JSONDecodeError) as exc:
        return None, f"{type(exc).__name__}: {exc}"


def _hex_to_int(value: Any) -> int | None:
    if not isinstance(value, str) or not value.startswith("0x"):
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None


def _hex_to_address(value: Any) -> str | None:
    if not isinstance(value, str) or not value.startswith("0x"):
        return None
    encoded = value[2:]
    if len(encoded) < 40:
        return None
    address = encoded[-40:]
    if not address or set(address) == {"0"}:
        return None
    return f"0x{address.lower()}"


def _is_evm_address(value: Any) -> bool:
    text = str(value or "")
    if not text.startswith("0x") or len(text) != 42:
        return False
    return all(char in "0123456789abcdefABCDEF" for char in text[2:])


def _chain_network_names(chain: str) -> set[str]:
    chain_key = str(chain or "").lower()
    if chain_key == "base":
        return {"base"}
    if chain_key in {"ethereum", "ethereum_or_evm"}:
        return {"ethereum", "base", "arbitrum", "polygon", "optimism"}
    return {chain_key}


def _load_token_address_lookup(path: Path = DEFAULT_TOKEN_ADDRESS_MASTER_PATH) -> dict[str, list[dict[str, str]]]:
    if not path.exists():
        return {}
    lookup: dict[str, list[dict[str, str]]] = {}
    try:
        with path.open("r", newline="", encoding="utf-8") as handle:
            rows = list(csv.DictReader(handle))
    except OSError:
        return {}
    for row in rows:
        address = str(row.get("token_address") or "").strip()
        if not _is_evm_address(address):
            continue
        record = {
            "address": address.lower(),
            "network": str(row.get("network") or "").strip().lower(),
            "source": str(row.get("source") or ""),
            "confidence": str(row.get("confidence") or ""),
        }
        keys = {
            _normal(row.get("lookup_key")),
            _normal(row.get("display_key")),
            _normal(row.get("asset_id")),
            _base_quote(str(row.get("display_key") or ""))[0] if row.get("display_key") else "",
        }
        for key in {item for item in keys if item}:
            lookup.setdefault(key, []).append(record)
    return lookup


def _candidate_token_addresses(
    candidate: dict[str, Any],
    *,
    token_lookup: dict[str, list[dict[str, str]]],
) -> list[str]:
    chain = str(candidate.get("chain") or "").lower()
    allowed_networks = _chain_network_names(chain)
    base, _ = _base_quote(str(candidate.get("symbol") or ""))
    keys = {_normal(candidate.get("asset_id")), base, _normal(candidate.get("symbol"))}
    addresses: list[str] = []
    seen: set[str] = set()
    for key in {item for item in keys if item}:
        for record in token_lookup.get(key, []):
            network = str(record.get("network") or "").lower()
            if network and network not in allowed_networks:
                continue
            address = str(record.get("address") or "").lower()
            if address and address not in seen:
                seen.add(address)
                addresses.append(address)
    return addresses


def _pool_contract_address(pool_address: Any) -> str | None:
    text = str(pool_address or "").strip()
    if _is_evm_address(text):
        return text
    first_segment = text.split("-", 1)[0]
    if _is_evm_address(first_segment):
        return first_segment
    return None


def _state_hash(value: Any) -> str | None:
    if not value:
        return None
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _token_contract_status(row: dict[str, Any], token0: str | None, token1: str | None) -> str:
    if not token0 or not token1:
        return "token_contracts_not_returned"
    expected = {
        str(row.get("base_token") or "").lower(),
        str(row.get("quote_token") or "").lower(),
    }
    observed = {token0.lower(), token1.lower()}
    return "token_contracts_match_pair_metadata" if expected == observed else "token_contract_mismatch"


def _fetch_evm_pool_state(row: dict[str, Any]) -> dict[str, Any]:
    chain = str(row.get("chain") or row.get("chain_id") or "").lower()
    contract = _pool_contract_address(row.get("pool_address"))
    if not contract:
        return {
            "status": "missing_evm_contract_address",
            "errors": [{"scope": "pool_address", "error": "not_a_single_evm_contract_address"}],
        }

    candidates = _rpc_candidates(chain)
    if not candidates:
        return {
            "status": "missing_evm_rpc_url",
            "pool_contract_address": contract,
            "errors": [{"scope": "rpc", "error": f"no_rpc_candidate_for_chain:{chain}"}],
        }

    attempted: list[dict[str, Any]] = []
    for rpc_source, rpc_url in candidates:
        block_hex, block_error = _safe_json_rpc(rpc_url, "eth_blockNumber", [])
        if block_error:
            attempted.append({"rpc_source": rpc_source, "scope": "eth_blockNumber", "error": block_error})
            continue
        block_number = _hex_to_int(block_hex)
        block_tag = hex(block_number) if block_number is not None else "latest"
        raw_calls: dict[str, Any] = {}
        call_errors: dict[str, str] = {}
        for call_name, selector in EVM_POOL_CALL_SELECTORS.items():
            result, error = _safe_json_rpc(
                rpc_url,
                "eth_call",
                [{"to": contract, "data": selector}, block_tag],
            )
            if error:
                call_errors[call_name] = error
            elif result not in {None, "0x"}:
                raw_calls[call_name] = result

        token0 = _hex_to_address(raw_calls.get("token0"))
        token1 = _hex_to_address(raw_calls.get("token1"))
        fee_tier = _hex_to_int(raw_calls.get("fee"))
        tick_spacing = _hex_to_int(raw_calls.get("tick_spacing"))
        liquidity = _hex_to_int(raw_calls.get("liquidity"))
        slot0_raw = raw_calls.get("slot0")
        pool_state_available = bool(token0 or token1 or fee_tier is not None or tick_spacing is not None or slot0_raw)
        state = {
            "status": "pool_state_captured" if pool_state_available else "block_number_only",
            "rpc_source": rpc_source,
            "pool_contract_address": contract,
            "block_number": block_number,
            "block_tag": block_tag,
            "token0": token0,
            "token1": token1,
            "fee_tier": fee_tier,
            "tick_spacing": tick_spacing,
            "liquidity": liquidity,
            "slot0_raw": slot0_raw,
            "slot0_sha256": _state_hash(slot0_raw),
            "raw_call_sha256": _state_hash(json.dumps(raw_calls, sort_keys=True)),
            "token_contract_status": _token_contract_status(row, token0, token1),
            "call_errors": call_errors,
            "attempted_rpc": attempted,
        }
        if pool_state_available or block_number is not None:
            return state

        attempted.append({"rpc_source": rpc_source, "scope": "eth_call", "error": call_errors})

    return {
        "status": "evm_rpc_state_failed",
        "pool_contract_address": contract,
        "errors": attempted,
    }


def _normal(value: Any) -> str:
    return str(value or "").strip().upper().replace("-", "").replace("_", "")


def _unique(values: list[Any]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _base_quote(symbol: str) -> tuple[str, str]:
    if "/" in symbol:
        base, quote = symbol.split("/", 1)
    else:
        base, quote = symbol, "USDC"
    return _normal(base), _normal(quote)


def _pair_liquidity_usd(pair: dict[str, Any]) -> float:
    liquidity = pair.get("liquidity") if isinstance(pair.get("liquidity"), dict) else {}
    try:
        return float(liquidity.get("usd") or 0)
    except (TypeError, ValueError):
        return 0.0


def _dex_matches(venue: str, pair: dict[str, Any]) -> bool:
    dex_id = str(pair.get("dexId") or "").lower()
    labels = " ".join(str(label).lower() for label in pair.get("labels") or [])
    haystack = f"{dex_id} {labels}"
    return any(item in haystack for item in VENUE_DEX_MATCHERS.get(venue, ()))


def _chain_matches(venue: str, pair: dict[str, Any]) -> bool:
    chain_id = str(pair.get("chainId") or "").lower()
    return chain_id in VENUE_CHAIN_MATCHERS.get(venue, ())


def _token_matches(symbol: str, pair: dict[str, Any], base_token_addresses: set[str] | None = None) -> bool:
    base, quote = _base_quote(symbol)
    base_token = pair.get("baseToken") if isinstance(pair.get("baseToken"), dict) else {}
    quote_token = pair.get("quoteToken") if isinstance(pair.get("quoteToken"), dict) else {}
    pair_base = _normal(base_token.get("symbol"))
    pair_quote = _normal(quote_token.get("symbol"))
    base_address = str(base_token.get("address") or "").lower()
    base_matches = pair_base == base or bool(base_token_addresses and base_address in base_token_addresses)
    return base_matches and pair_quote in {quote, "USDC", "USDBC"}


def _best_pair(
    venue: str,
    symbol: str,
    pairs: list[dict[str, Any]],
    *,
    base_token_addresses: set[str] | None = None,
) -> dict[str, Any] | None:
    matches = [
        pair
        for pair in pairs
        if isinstance(pair, dict)
        and _dex_matches(venue, pair)
        and _chain_matches(venue, pair)
        and _token_matches(symbol, pair, base_token_addresses)
        and pair.get("pairAddress")
    ]
    if not matches:
        return None
    return sorted(matches, key=_pair_liquidity_usd, reverse=True)[0]


def _pair_row(candidate: dict[str, Any], pair: dict[str, Any]) -> dict[str, Any]:
    base_token = pair.get("baseToken") if isinstance(pair.get("baseToken"), dict) else {}
    quote_token = pair.get("quoteToken") if isinstance(pair.get("quoteToken"), dict) else {}
    payload = json.dumps(pair, sort_keys=True)
    pair_address = pair.get("pairAddress")
    return {
        "allowlist_id": candidate["allowlist_id"],
        "venue": candidate["venue"],
        "symbol": candidate["symbol"],
        "asset_id": candidate["asset_id"],
        "chain": pair.get("chainId") or candidate["chain"],
        "chain_id": pair.get("chainId") or candidate["chain_id"],
        "dex_id": pair.get("dexId"),
        "pool_address": pair_address,
        "pool_id": pair_address,
        "base_token": base_token.get("address"),
        "quote_token": quote_token.get("address"),
        "base_symbol": base_token.get("symbol"),
        "quote_symbol": quote_token.get("symbol"),
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
        "raw_payload_sha256": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "raw_payload_artifact": "reports/rwa_evm_pool_allowlist.json",
        "source": "dexscreener_public_pair_search",
        "review_status": "pool_identity_ready_pending_evm_rpc_state",
    }


def _enrich_pair_row_with_evm_state(row: dict[str, Any]) -> dict[str, Any]:
    state = _fetch_evm_pool_state(row)
    enriched = dict(row)
    enriched["evm_rpc_state"] = state
    block_number = state.get("block_number")
    if block_number:
        enriched["block_number"] = block_number
    if state.get("token0"):
        enriched["token0"] = state["token0"]
    if state.get("token1"):
        enriched["token1"] = state["token1"]
    if state.get("fee_tier") is not None:
        enriched["fee_tier"] = state["fee_tier"]
        enriched["fee_tier_status"] = "fee_tier_read_from_pool_contract"
    if state.get("status") == "pool_state_captured" and (
        state.get("slot0_raw") or state.get("tick_spacing") is not None or state.get("liquidity") is not None
    ):
        enriched["tick_state"] = {
            "block_number": state.get("block_number"),
            "block_tag": state.get("block_tag"),
            "liquidity": state.get("liquidity"),
            "tick_spacing": state.get("tick_spacing"),
            "slot0_sha256": state.get("slot0_sha256"),
            "slot0_raw": state.get("slot0_raw"),
            "rpc_source": state.get("rpc_source"),
        }
        enriched["review_status"] = "pool_block_state_ready_pending_live_quality"
    elif block_number:
        enriched["review_status"] = "pool_block_number_ready_missing_pool_state"
    return enriched


def build_evm_pool_allowlist() -> dict[str, Any]:
    """Build EVM pool allowlist candidates from public pair search metadata."""
    allowlist = build_dex_allowlist()
    token_lookup = _load_token_address_lookup()
    candidates = [
        row
        for row in allowlist["candidates"]
        if row["chain"] in {"ethereum_or_evm", "base"} and row["venue"] in VENUE_DEX_MATCHERS
    ]
    search_cache: dict[str, tuple[dict[str, Any], str | None]] = {}
    rows: list[dict[str, Any]] = []
    errors: list[dict[str, Any]] = []
    for candidate in candidates:
        base, quote = _base_quote(str(candidate["symbol"]))
        token_addresses = _candidate_token_addresses(candidate, token_lookup=token_lookup)
        queries = _unique([f"{base} {quote}", *token_addresses, str(candidate.get("asset_id") or "")])
        collected_pairs: list[dict[str, Any]] = []
        query_errors: list[dict[str, str]] = []
        for query in queries:
            if query not in search_cache:
                search_cache[query] = _safe_fetch_search(query)
            payload, error = search_cache[query]
            if error:
                query_errors.append({"query": query, "error": error})
                continue
            pairs = payload.get("pairs") if isinstance(payload.get("pairs"), list) else []
            collected_pairs.extend(pair for pair in pairs if isinstance(pair, dict))
        pair = _best_pair(
            str(candidate["venue"]),
            str(candidate["symbol"]),
            collected_pairs,
            base_token_addresses=set(token_addresses),
        )
        if pair:
            rows.append(_enrich_pair_row_with_evm_state(_pair_row(candidate, pair)))
        else:
            errors.append(
                {
                    "allowlist_id": candidate["allowlist_id"],
                    "query": "|".join(queries),
                    "error": "no_matching_pair_found",
                    "query_errors": query_errors,
                    "token_address_candidates": token_addresses,
                }
            )

    by_venue = Counter(str(row["venue"]) for row in rows)
    by_chain = Counter(str(row["chain"]) for row in rows)
    block_state_captured = sum(
        1
        for row in rows
        if row.get("block_number")
        and (row.get("tick_state") or row.get("balances") or row.get("weights") or row.get("amplification"))
    )
    return {
        "product": "rwa_evm_pool_allowlist",
        "generated_at": _utc_now_iso(),
        "summary": {
            "candidate_count": len(candidates),
            "pool_count": len(rows),
            "missing_pair_count": len(errors),
            "block_state_captured": block_state_captured,
            "fee_tiers_missing": sum(1 for row in rows if not row.get("fee_tier")),
            "by_venue": dict(sorted(by_venue.items())),
            "by_chain": dict(sorted(by_chain.items())),
        },
        "policy": {
            "promotion_rule": "Pair search plus block-tagged RPC state resolves replay evidence only; live liquidity, manipulation, benchmark, and production RPC gates are still required before promotion.",
            "source_limit": "Public RPC fallback receipts are discovery evidence and must be replaced by monitored production RPC/indexer access for promoted feeds.",
        },
        "pools": sorted(rows, key=lambda row: (str(row["venue"]), str(row["asset_id"]), str(row["pool_address"]))),
        "errors": sorted(errors, key=lambda row: str(row["allowlist_id"])),
    }


def write_evm_pool_allowlist_reports(
    *,
    json_path: str | Path = DEFAULT_EVM_POOL_ALLOWLIST_JSON_PATH,
    csv_path: str | Path = DEFAULT_EVM_POOL_ALLOWLIST_CSV_PATH,
) -> dict[str, Any]:
    """Write EVM pool allowlist reports."""
    report = build_evm_pool_allowlist()
    json_out = Path(json_path)
    csv_out = Path(csv_path)
    json_out.parent.mkdir(parents=True, exist_ok=True)
    csv_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    fieldnames = [
        "allowlist_id",
        "venue",
        "symbol",
        "chain",
        "dex_id",
        "pool_address",
        "base_token",
        "quote_token",
        "fee_tier",
        "block_number",
        "liquidity_usd",
        "volume_h24",
        "review_status",
        "url",
    ]
    with csv_out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in report["pools"]:
            writer.writerow({key: row.get(key) for key in fieldnames})
    return report
