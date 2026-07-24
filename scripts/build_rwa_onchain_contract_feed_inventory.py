#!/usr/bin/env python3
"""Build a contract-grain onchain sourcing inventory from the RWA master.

The master deployment table remains the controlling universe.  Price-bearing
pool, router, and ERC-4626 observations are joined only by exact network and
contract identity; symbol-only joins are intentionally rejected.
"""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


DEFAULT_MASTER = Path("reports/rwa_master_all_token_contracts_sourceability_2026-07-16.csv")
DEFAULT_EVM_PROBE = Path("reports/feed_quality_contract_pool_probe_2026-07-21.csv")
DEFAULT_EVM_POOLS = Path("reports/rwa_contract_pool_sources_combined_2026-07-16.json")
DEFAULT_JUPITER_PROBE = Path("reports/feed_quality_jupiter_router_probe_2026-07-21.csv")
DEFAULT_ERC4626_PROBE = Path("reports/rwa_erc4626_probe_2026-07-21.json")
DEFAULT_OUTPUT = Path("reports/rwa_onchain_contract_feed_inventory_2026-07-21.csv")
DEFAULT_SOURCEABLE = Path("reports/rwa_onchain_price_sourceable_contracts_2026-07-21.csv")
DEFAULT_SUMMARY = Path("reports/rwa_onchain_contract_feed_inventory_summary_2026-07-21.json")


NETWORK_TO_CHAIN = {
    "Ethereum": "ethereum",
    "Base": "base",
    "Arbitrum": "arbitrum",
    "Avalanche C-Chain": "avalanche",
    "BNB Chain": "bsc",
    "Polygon": "polygon",
    "Optimism": "optimism",
    "HyperEVM": "hyperevm",
    "Ink": "ink",
    "Mantle": "mantle",
    "Monad": "monad",
    "Plume": "plume",
    "Plasma": "plasma",
    "SEI": "sei",
    "XDC": "xdc",
    "ZKsync Era": "zksync-era",
    "Solana": "solana",
}


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return list(csv.DictReader(handle))


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _json_dict(value: str) -> dict[str, Any]:
    if not value:
        return {}
    parsed = json.loads(value)
    return parsed if isinstance(parsed, dict) else {}


def _clean_address(value: str) -> str:
    return value.strip().lower()


def _identity_key(network: str, address: str) -> tuple[str, str]:
    return NETWORK_TO_CHAIN.get(network, network.strip().lower()), _clean_address(address)


def _as_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _standards(value: str) -> list[str]:
    try:
        parsed = json.loads(value or "[]")
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _token_contract_capabilities(network: str, standards_value: str) -> str:
    standards = set(_standards(standards_value))
    capabilities: list[str] = []
    if standards & {"ERC-20", "BEP-20", "XRC-20", "TRC-20", "NEP-141"}:
        capabilities.extend(
            ["name/symbol/decimals", "total supply", "balances", "transfers/mint/burn logs", "allowances"]
        )
    if "ERC-4626" in standards:
        capabilities.extend(
            ["underlying asset", "total assets", "assets-per-share", "deposit/redeem previews and limits"]
        )
    if "ERC-7540" in standards:
        capabilities.extend(
            ["asynchronous deposit/redeem requests", "claimable request state", "vault asset/share state"]
        )
    if standards & {"ERC-1400", "ERC-3643"}:
        capabilities.extend(["partition/compliance state", "transfer restrictions and events"])
    if standards & {"SPL", "token-2022"}:
        capabilities.extend(
            ["mint decimals/supply", "mint/freeze authorities", "token accounts/balances", "transfers", "mint extensions"]
        )
    if "HTS" in standards:
        capabilities.extend(
            ["token info/supply", "treasury and KYC/freeze/wipe/pause state", "balances/transfers"]
        )
    if "XRPL-Token" in standards:
        capabilities.extend(
            ["issuer and currency code", "trustlines/balances", "payments/transactions", "ledger DEX offers queried separately"]
        )
    if standards & {"Stellar Asset Contract (SAC)", "SEP-41"}:
        capabilities.extend(
            ["asset/issuer identity", "balances/trustlines", "payments/transfers", "Horizon order books/trades queried separately"]
        )
    if standards & {"Aptos-FA", "Aptos-Coin"}:
        capabilities.extend(["asset metadata", "supply", "balances", "transfer events"])
    if "Sui Coin" in standards:
        capabilities.extend(["coin metadata", "supply", "owned balances", "transaction events"])
    if "Liquid AMP" in standards:
        capabilities.extend(["asset id/issuance", "balances", "transfers", "authorization state"])
    if not capabilities:
        if network == "Robinhood":
            capabilities.append("catalog identifier only; no public chain-state interface established")
        else:
            capabilities.append("network-native identity, balances and transaction/event state where an RPC/indexer exists")
    return "; ".join(dict.fromkeys(capabilities))


def _pool_activity(volume_h24: float | None) -> str:
    if volume_h24 is None:
        return "24h activity not measured"
    if volume_h24 >= 25_000:
        return "active: >=$25k observed 24h volume"
    if volume_h24 > 0:
        return "thin: positive but <$25k observed 24h volume"
    return "idle: zero observed 24h volume"


def _route_contracts(metadata: dict[str, Any]) -> tuple[str, str, int | None, float | None]:
    amm_rows: list[str] = []
    context_slots: list[int] = []
    impacts: list[float] = []
    quote_mint = ""
    for side in ("bid_quote", "ask_quote"):
        quote = metadata.get(side) if isinstance(metadata.get(side), dict) else {}
        if quote.get("context_slot") is not None:
            context_slots.append(int(quote["context_slot"]))
        impact = _as_float(quote.get("price_impact_pct"))
        if impact is not None:
            impacts.append(impact)
        for leg in quote.get("route_plan") or []:
            swap = leg.get("swapInfo") if isinstance(leg, dict) else {}
            if isinstance(swap, dict) and swap.get("ammKey"):
                label = str(swap.get("label") or "AMM")
                amm_rows.append(f"{label}:{swap['ammKey']}")
        if side == "ask_quote":
            quote_mint = str(quote.get("input_mint") or "")
        elif not quote_mint:
            quote_mint = str(quote.get("output_mint") or "")
    return (
        "|".join(dict.fromkeys(amm_rows)),
        quote_mint,
        max(context_slots) if context_slots else None,
        max(impacts) if impacts else None,
    )


def _build_evm_observations(
    probe_rows: list[dict[str, str]], pool_payload: dict[str, Any]
) -> dict[tuple[str, str], dict[str, Any]]:
    pool_by_contract = {
        _clean_address(str(pool.get("pool_address") or pool.get("pool_id") or "")): pool
        for pool in pool_payload.get("pools") or []
        if str(pool.get("pool_address") or pool.get("pool_id") or "").startswith("0x")
        and len(str(pool.get("pool_address") or pool.get("pool_id") or "")) == 42
    }
    by_feed = {(row["venue"], row["symbol"].lower(), row["kind"]): row for row in probe_rows}
    observations: dict[tuple[str, str], dict[str, Any]] = {}
    for row in probe_rows:
        if row.get("kind") != "bidask" or row.get("probe_status") != "ok":
            continue
        metadata = _json_dict(row.get("source_metadata", ""))
        chain = str(metadata.get("chain") or "").lower()
        token_contract = _clean_address(row.get("symbol", ""))
        pool_contract = _clean_address(str(metadata.get("pool_contract") or ""))
        pool = pool_by_contract.get(pool_contract, {})
        vwap = by_feed.get((row["venue"], row["symbol"].lower(), "vwap"), {})
        token0 = _clean_address(str(metadata.get("token0") or ""))
        token1 = _clean_address(str(metadata.get("token1") or ""))
        quote_contract = token1 if token0 == token_contract else token0
        depth_semantics = str(metadata.get("depth_semantics") or "")
        observations[(chain, token_contract)] = {
            "mechanism": "evm_pool_state",
            "venue": row["venue"],
            "price_source_contract": pool_contract,
            "quote_contract": quote_contract,
            "block_or_slot": metadata.get("block_number"),
            "tested_at": row.get("tested_at"),
            "latency_ms": _as_float(row.get("latency_ms")),
            "capture_freshness_ms": _as_float(row.get("freshness_ms")),
            "mid": _as_float(row.get("value")),
            "bid": _as_float(row.get("bid")),
            "ask": _as_float(row.get("ask")),
            "vwap": _as_float(vwap.get("vwap")),
            "vwap_fill_status": vwap.get("fill_status", ""),
            "liquidity_usd": _as_float(metadata.get("discovery_liquidity_usd") or pool.get("liquidity_usd")),
            "volume_h24_usd": _as_float(pool.get("volume_h24")),
            "depth_semantics": depth_semantics,
        }
    return observations


def _build_jupiter_observations(probe_rows: list[dict[str, str]]) -> dict[tuple[str, str], dict[str, Any]]:
    by_feed = {(row["symbol"], row["kind"]): row for row in probe_rows}
    observations: dict[tuple[str, str], dict[str, Any]] = {}
    for row in probe_rows:
        if row.get("kind") != "bidask" or row.get("probe_status") != "ok":
            continue
        metadata = _json_dict(row.get("source_metadata", ""))
        base_token = metadata.get("base_token") if isinstance(metadata.get("base_token"), dict) else {}
        mint = _clean_address(str(base_token.get("mint") or ""))
        if not mint:
            continue
        vwap = by_feed.get((row["symbol"], "vwap"), {})
        route_contracts, quote_contract, context_slot, impact = _route_contracts(metadata)
        observations[("solana", mint)] = {
            "mechanism": "jupiter_router_quote",
            "venue": "jupiter_router",
            "price_source_contract": route_contracts,
            "quote_contract": quote_contract,
            "block_or_slot": context_slot,
            "tested_at": row.get("tested_at"),
            "latency_ms": _as_float(row.get("latency_ms")),
            "capture_freshness_ms": _as_float(row.get("freshness_ms")),
            "mid": _as_float(row.get("value")),
            "bid": _as_float(row.get("bid")),
            "ask": _as_float(row.get("ask")),
            "vwap": _as_float(vwap.get("vwap")) if vwap.get("probe_status") == "ok" else None,
            "vwap_fill_status": vwap.get("fill_status", "") if vwap.get("probe_status") == "ok" else "",
            "liquidity_usd": _as_float(base_token.get("liquidity")),
            "volume_h24_usd": None,
            "price_impact_pct": impact,
            "depth_semantics": "executable route quote snapshots; no native L2 order book",
        }
    return observations


def _build_rate_observations(payload: dict[str, Any]) -> dict[tuple[str, str], dict[str, Any]]:
    observations: dict[tuple[str, str], dict[str, Any]] = {}
    for row in payload.get("results") or []:
        if not row.get("supports_exchange_rate"):
            continue
        observations[_identity_key(str(row["network"]), str(row["token_contract"]))] = row
    return observations


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = list(rows[0]) if rows else []
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def build(args: argparse.Namespace) -> dict[str, Any]:
    master = _read_csv(args.master)
    evm = _build_evm_observations(_read_csv(args.evm_probe), _read_json(args.evm_pools))
    jupiter = _build_jupiter_observations(_read_csv(args.jupiter_probe))
    rates = _build_rate_observations(_read_json(args.erc4626_probe))
    price_observations = {**evm, **jupiter}
    output: list[dict[str, Any]] = []

    for row in master:
        key = _identity_key(row["network"], row["address"])
        price = price_observations.get(key)
        rate = rates.get(key)
        standards = _standards(row.get("standards", ""))
        master_candidate = str(row.get("currently_sourceable", "")).lower() == "true"

        if price and price["mechanism"] == "jupiter_router_quote":
            status = "live_onchain_route_quote_candidate"
            liveness = "live on request at observed Solana context slot"
            update_model = "new exact-input route quote per request; route may change every slot"
            direct_price = "No: price comes from routed AMM contracts, not the token mint"
            fit_now = "BidAsk candidate at probe notional; block-size VWAP candidate where the configured quote sweep filled"
            fit_after = "Eligible executable route leg after continuous quality, route replay, manipulation, benchmark, rights, and consensus gates"
            semantics = "Executable Jupiter route snapshots; not native L2 and not a time-window trade VWAP"
            activity = "route returned for current point probe; 24h economic activity not measured"
        elif price:
            status = "live_onchain_pool_state_reference_candidate"
            if rate:
                status = "live_pool_state_plus_exchange_rate_candidate"
            liveness = "live block-tagged state at current EVM point probe"
            update_model = "pool state changes on swaps/liquidity events; poll or subscribe to logs/blocks"
            direct_price = "No: pool contract supplies the price-bearing state; token contract supplies identity/state"
            fit_now = "RWA reference candidate only; synthetic spread/depth must not be published as executable BidAsk or VWAP"
            fit_after = "Exact block-size VWAP/BidAsk only after full tick/invariant replay and exact-input simulation"
            semantics = str(price.get("depth_semantics") or "block-tagged pool state")
            activity = _pool_activity(price.get("volume_h24_usd"))
        elif rate:
            status = "live_contract_exchange_rate_reference_candidate"
            liveness = "live ERC-4626 view call at current EVM block"
            update_model = "read at each block or when vault accounting state changes"
            direct_price = "Assets-per-share exchange rate only; requires an independent underlying-asset price for USD"
            fit_now = "RWA exchange-rate/NAV reference; never BidAsk or executable VWAP"
            fit_after = "Reference composite input after underlying identity, valuation, rights, freshness, and anti-circularity checks"
            semantics = "ERC-4626 convertToAssets share rate"
            activity = "vault view functions responded; transaction activity not measured"
        elif "ERC-4626" in standards or "ERC-7540" in standards:
            status = "contract_rate_candidate_unverified_or_blocked"
            liveness = "not currently observed"
            update_model = "potential vault state reads per block after RPC/interface verification"
            direct_price = "Potential exchange-rate state only; no successful exact contract probe in this run"
            fit_now = "No price feed now; contract-state monitoring only"
            fit_after = "Possible RWA exchange-rate/NAV reference after interface and underlying valuation verification"
            semantics = "catalog standard tag, not a verified price observation"
            activity = "not measured"
        elif master_candidate:
            status = "offchain_or_symbol_mapped_price_candidate_not_exact_onchain"
            liveness = "onchain token price not observed in current exact-identity probes"
            update_model = "token state is event-driven; existing price candidate follows its separate venue/source clock"
            direct_price = "No verified exact pool/oracle/rate contract in this run"
            fit_now = "Keep existing venue/reference candidate in its non-onchain lane; token state is supplemental"
            fit_after = "Onchain price only after exact pool/oracle/rate contract discovery and successful probe"
            semantics = "master candidate source is not proof that this deployment contract is price-bearing"
            activity = "not measured"
        else:
            status = "token_contract_state_only_no_verified_price_path"
            liveness = "price liveness not applicable; token activity not measured"
            update_model = "metadata is mostly static; balances/supply/transfers update on chain events"
            direct_price = "No verified price-bearing function or linked market contract"
            fit_now = "Token/state feed only; exclude from BidAsk, VWAP, NAV, and oracle price composites"
            fit_after = "Requires exact pool, oracle, audited rate/NAV contract, or issuer/venue price source"
            semantics = "token metadata/state is not price"
            activity = "not measured"

        if rate:
            rate_text = (
                f"{rate['assets_per_share']} units of underlying per 1 share at block {rate['block_number']}"
            )
            underlying = str(rate.get("underlying_asset_contract") or "")
        else:
            rate_text = ""
            underlying = ""

        output.append(
            {
                "token_row_id": row["token_row_id"],
                "rwa_ticker": row["rwa_xyz_ticker"],
                "rwa_symbol": row["symbol"],
                "asset_id": row["asset_id"],
                "canonical_underlying_candidate": row["canonical_underlying_candidate"],
                "asset_name": row["asset_name"],
                "asset_class": row["asset_class"],
                "issuer_name": row["issuer_name"],
                "platform": row["platform"],
                "network": row["network"],
                "token_contract_address": row["address"],
                "standards": row["standards"],
                "identity_mapping_status": row["identity_mapping_status"],
                "master_sourceability_status": row["sourceability_status"],
                "master_best_venue": row["best_venue"],
                "master_best_price_type": row["best_price_type"],
                "onchain_price_path_status": status,
                "price_mechanism": price.get("mechanism", "") if price else "erc4626_exchange_rate" if rate else "",
                "price_source_venue": price.get("venue", "token_contract") if price else ("token_contract" if rate else ""),
                "price_source_contract_or_route": price.get("price_source_contract", "") if price else row["address"] if rate else "",
                "quote_or_underlying_contract": price.get("quote_contract", "") if price else underlying,
                "current_mid_or_reference": price.get("mid", "") if price else "",
                "current_bid": price.get("bid", "") if price else "",
                "current_ask": price.get("ask", "") if price else "",
                "block_size_vwap_10000": price.get("vwap", "") if price else "",
                "vwap_fill_status": price.get("vwap_fill_status", "") if price else "",
                "erc4626_assets_per_share": rate.get("assets_per_share", "") if rate else "",
                "erc4626_rate_observation": rate_text,
                "block_or_slot": price.get("block_or_slot", "") if price else rate.get("block_number", "") if rate else "",
                "observed_at": price.get("tested_at", "") if price else args.erc4626_probe_date if rate else "",
                "receive_latency_ms": price.get("latency_ms", "") if price else "",
                "capture_freshness_ms": price.get("capture_freshness_ms", "") if price else "",
                "liveness": liveness,
                "update_model": update_model,
                "economic_activity_liveness": activity,
                "liquidity_usd_discovery_snapshot": price.get("liquidity_usd", "") if price else "",
                "volume_h24_usd_discovery_snapshot": price.get("volume_h24_usd", "") if price else "",
                "max_router_price_impact_pct": price.get("price_impact_pct", "") if price else "",
                "token_contract_data_available": _token_contract_capabilities(row["network"], row["standards"]),
                "price_data_from_token_contract": direct_price,
                "price_semantics": semantics,
                "blocksize_feed_fit_now": fit_now,
                "blocksize_feed_fit_after_gates": fit_after,
                "production_status": "candidate_only_not_production_promoted" if price or rate else "not_price_source",
                "required_gates": (
                    "exact identity; replayable raw payload; native block/slot and event/receive times; continuous 5m/30m/24h windows; "
                    "benchmark alignment; depth/liquidity/manipulation/depeg checks; rights clearance; independent-source consensus"
                    if price or rate
                    else "exact price-bearing contract/source discovery before price-feed quality gates"
                ),
                "rights_status": "onchain/public access does not by itself prove commercial redistribution or onchain-publication rights; review source and router/RPC terms",
            }
        )

    sourceable_statuses = {
        "live_onchain_route_quote_candidate",
        "live_onchain_pool_state_reference_candidate",
        "live_pool_state_plus_exchange_rate_candidate",
        "live_contract_exchange_rate_reference_candidate",
    }
    sourceable = [row for row in output if row["onchain_price_path_status"] in sourceable_statuses]
    _write_csv(args.output, output)
    _write_csv(args.sourceable_output, sourceable)

    status_counts = Counter(row["onchain_price_path_status"] for row in output)
    sourceable_tickers = {row["rwa_ticker"] for row in sourceable}
    summary = {
        "generated_at": datetime.now(UTC).isoformat(),
        "basis": str(args.master),
        "master_deployment_rows": len(output),
        "unique_tickers": len({row["rwa_ticker"] for row in output}),
        "unique_economic_asset_ids": len({row["asset_id"] for row in output}),
        "exact_current_onchain_price_relevant_deployments": len(sourceable),
        "exact_current_onchain_price_relevant_tickers": len(sourceable_tickers),
        "current_router_quote_deployments": sum(row["price_mechanism"] == "jupiter_router_quote" for row in sourceable),
        "current_pool_state_deployments": sum(row["price_mechanism"] == "evm_pool_state" for row in sourceable),
        "current_exchange_rate_deployments": sum(bool(row["erc4626_assets_per_share"] != "") for row in sourceable),
        "production_promoted_deployments": 0,
        "status_counts": dict(sorted(status_counts.items())),
        "quality_notes": [
            "Exact network plus contract identity is required; repeated addresses on other networks are not inherited.",
            "EVM pool adapter bid/ask and depth are synthetic from pool state and liquidity, not executable L2 or exact block-size VWAP.",
            "Jupiter observations are executable route snapshots returned by an API and tied to Solana context slots, not direct RPC pool replay.",
            "ERC-4626 assets-per-share is an exchange rate in underlying units, not a USD price.",
            "Production-promoted count remains zero.",
        ],
        "outputs": {"full_inventory": str(args.output), "sourceable_contracts": str(args.sourceable_output)},
    }
    args.summary_output.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--master", type=Path, default=DEFAULT_MASTER)
    parser.add_argument("--evm-probe", type=Path, default=DEFAULT_EVM_PROBE)
    parser.add_argument("--evm-pools", type=Path, default=DEFAULT_EVM_POOLS)
    parser.add_argument("--jupiter-probe", type=Path, default=DEFAULT_JUPITER_PROBE)
    parser.add_argument("--erc4626-probe", type=Path, default=DEFAULT_ERC4626_PROBE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--sourceable-output", type=Path, default=DEFAULT_SOURCEABLE)
    parser.add_argument("--summary-output", type=Path, default=DEFAULT_SUMMARY)
    parser.add_argument("--erc4626-probe-date", default="2026-07-21T23:28:00Z")
    return parser.parse_args()


def main() -> None:
    print(json.dumps(build(parse_args()), indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
