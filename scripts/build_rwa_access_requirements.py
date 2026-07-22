#!/usr/bin/env python3
"""Build ticker, network, and platform access requirements for full RWA coverage."""

from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any


EVM_NETWORKS: dict[str, tuple[str, str, str]] = {
    "Ethereum": ("EVM_RPC_ETHEREUM_URL", "https://dashboard.alchemy.com/", "Create a production Ethereum HTTPS/WebSocket endpoint with archive/log capacity."),
    "Base": ("EVM_RPC_BASE_URL", "https://dashboard.alchemy.com/", "Create a Base endpoint with WebSocket and historical log capacity."),
    "Arbitrum": ("EVM_RPC_ARBITRUM_URL", "https://dashboard.alchemy.com/", "Create an Arbitrum One endpoint with WebSocket and archive/log access."),
    "Polygon": ("EVM_RPC_POLYGON_URL", "https://dashboard.alchemy.com/", "Create a Polygon PoS endpoint with WebSocket and archive/log access."),
    "Optimism": ("EVM_RPC_OPTIMISM_URL", "https://dashboard.alchemy.com/", "Create an Optimism endpoint with WebSocket and historical logs."),
    "BNB Chain": ("EVM_RPC_BSC_URL", "https://www.quicknode.com/", "Create a BNB Smart Chain endpoint with WebSocket and historical logs."),
    "Avalanche C-Chain": ("EVM_RPC_AVALANCHE_URL", "https://dashboard.alchemy.com/", "Create an Avalanche C-Chain endpoint with WebSocket and historical logs."),
    "Mantle": ("EVM_RPC_MANTLE_URL", "https://www.quicknode.com/", "Create a Mantle endpoint with WebSocket and historical logs."),
    "Gnosis": ("EVM_RPC_GNOSIS_URL", "https://www.quicknode.com/", "Create a Gnosis endpoint with WebSocket and historical logs."),
    "Celo": ("EVM_RPC_CELO_URL", "https://www.quicknode.com/", "Create a Celo endpoint with WebSocket and historical logs."),
    "HyperEVM": ("EVM_RPC_HYPEREVM_URL", "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/hyperevm/json-rpc", "Use the public RPC for discovery; procure an archive/indexed endpoint for replay because the default endpoint only supports latest-state calls."),
    "Ink": ("EVM_RPC_INK_URL", "https://docs.inkonchain.com/tools/rpc", "Use an Ink public endpoint for discovery or create a private Alchemy/QuickNode/Gelato endpoint for production."),
    "Plume": ("EVM_RPC_PLUME_URL", "https://docs.plume.org/plume/developers/tools-and-services/node-rpc-providers", "Use the public RPC for discovery or create a Conduit/dRPC/Uniblock/Tatum key for production."),
    "Plasma": ("EVM_RPC_PLASMA_URL", "https://docs.plasma.to/", "Create a dedicated Plasma EVM endpoint with WebSocket and historical logs."),
    "Monad": ("EVM_RPC_MONAD_URL", "https://docs.monad.xyz/reference/json-rpc/api", "Create a Monad EVM endpoint with WebSocket and historical logs."),
    "SEI": ("EVM_RPC_SEI_URL", "https://docs.sei.io/evm", "Use the Sei EVM public RPC for discovery or a dedicated provider for production."),
    "XDC": ("EVM_RPC_XDC_URL", "https://docs.xdc.network/", "Create an XDC EVM endpoint with WebSocket and historical logs."),
    "Pharos": ("EVM_RPC_PHAROS_URL", "https://docs.pharosnetwork.xyz/", "Create a Pharos EVM endpoint and confirm mainnet contract availability."),
    "ZKsync Era": ("EVM_RPC_ZKSYNC_URL", "https://docs.zksync.io/zksync-network/environment/connecting-to-zksync", "Create a zkSync Era endpoint with WebSocket and historical logs."),
}

NON_EVM_NETWORKS: dict[str, tuple[str, str, str, str]] = {
    "Solana": ("SOLANA_RPC_URL", "solana_rpc", "https://dashboard.helius.dev/", "Use the configured RPC for account state; add WebSocket/enhanced transaction capacity and direct Raydium, Orca, and Meteora decoders."),
    "Stellar": ("STELLAR_HORIZON_URL", "stellar_horizon", "https://developers.stellar.org/docs/data/apis/horizon", "Use public Horizon for discovery; procure or operate a production Horizon endpoint for replay and SLA."),
    "Hedera": ("HEDERA_MIRROR_NODE_URL", "hedera_mirror", "https://docs.hedera.com/hedera/sdks-and-apis/rest-api", "Use a mirror node REST endpoint for token, contract, and transaction history."),
    "XRP Ledger": ("XRPL_RPC_URL", "xrpl_clio", "https://xrpl.org/docs/infrastructure/data-apis/api-v2/get-started", "Use public APIs for discovery; procure a dedicated Clio/provider endpoint for production replay."),
    "Aptos": ("APTOS_RPC_URL", "aptos_fullnode", "https://aptos.dev/network/nodes/full-node", "Use a dedicated fullnode REST endpoint and indexer for events and pool state."),
    "Sui": ("SUI_RPC_URL", "sui_rpc", "https://docs.sui.io/guides/developer/getting-started/connect", "Use a dedicated Sui RPC/gRPC endpoint and indexer for object and event replay."),
    "NEAR": ("NEAR_RPC_URL", "near_rpc", "https://docs.near.org/api/rpc/introduction", "Use a dedicated archival RPC or indexer for contract state and pool events."),
    "TRON": ("TRON_RPC_URL", "tron_rpc", "https://www.trongrid.io/", "Create a TronGrid API key or operate a full node for contract and event replay."),
    "Liquid Network": ("LIQUID_RPC_URL", "liquid_elements_rpc", "https://docs.liquid.net/docs/technical-overview", "Operate an Elements/Liquid node or procure an indexed API; confidential amounts can prevent public pool pricing."),
    "Provenance": ("PROVENANCE_RPC_URL", "provenance_rpc", "https://docs.provenance.io/", "Use Provenance chain APIs for state and request issuer/Figure terms for authoritative quotes."),
    "MANTRA": ("MANTRA_RPC_URL", "cosmos_rpc", "https://docs.mantrachain.io/", "Use a dedicated Cosmos RPC/indexer and identify any DEX pool or oracle state."),
    "Noble": ("NOBLE_RPC_URL", "cosmos_rpc", "https://docs.noble.xyz/", "Use a dedicated Cosmos RPC/indexer; Noble issuance state is not itself a market price."),
    "Robinhood": ("ROBINHOOD_STOCK_TOKEN_API_KEY", "partner_api", "https://docs.robinhood.com/chain/contracts/", "Contract metadata is public, but stock-token quotes require a Robinhood product/partner entitlement; the public crypto API does not cover stock tokens."),
}

PLATFORM_OVERRIDES: dict[str, tuple[str, str, str]] = {
    "Ondo": ("ondo_global_markets_api", "https://docs.ondo.finance/api-reference/overview", "Email onboarding@ondo.finance for API credentials, real-time/historical prices, token metadata, and redistribution terms."),
    "xStocks": ("xstocks_api", "https://docs.xstocks.fi/developers", "Use public market-data/token-metadata endpoints where sufficient; generate an API key under Settings > API for authenticated access and confirm redistribution rights."),
    "Dinari": ("dinari_api", "https://docs.dinari.com/", "Complete business onboarding/KYB, obtain a Dinari API key and market-data license; real-time NBBO is a separately licensed product."),
    "Robinhood": ("robinhood_stock_token_partner", "https://docs.robinhood.com/chain/contracts/", "Request stock-token catalog/quote access and redistribution permission through Robinhood; the documented retail API is crypto-only."),
    "Swarm": ("swarm_subgraph_and_partner", "https://docs.swarm.com/reference/apis", "Use the public subgraph/onchain pools for state; contact Swarm for production onboarding/API keys and regulated-market data terms."),
    "WisdomTree": ("wisdomtree_connect_partner", "https://www.wisdomtreeconnect.com/", "Request WisdomTree Connect institutional API access, fund NAV clock, token metadata, and redistribution rights."),
    "Securitize": ("securitize_connect_api", "https://sec-connect-api-docs.securitize.io/accessing-the-apis", "Create a Securitize Connect integration/issuer relationship and request NAV, transfer-agent, webhook, and redistribution access."),
    "Spiko": ("spiko_partner_api", "https://www.spiko.io/use-cases/web3", "Book a partner integration, obtain API credentials, NAV/share-price cadence, and redistribution terms."),
    "Backed Finance": ("backed_or_xstocks_api", "https://docs.xstocks.fi/developers", "Use xStocks public/authenticated market data for listed products and request issuer NAV for fund products."),
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ticker-input", default="reports/rwa_unique_ticker_sourceability_opportunity_2026-07-16.csv")
    parser.add_argument("--token-input", default="reports/rwa_master_all_token_contracts_sourceability_source_all_2026-07-16.csv")
    parser.add_argument("--pool-input", default="reports/rwa_contract_pool_sources_combined_2026-07-16.json")
    parser.add_argument("--xstocks-input", default="reports/rwa_xstocks_public_price_discovery_2026-07-16.json")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--ticker-output", default="reports/rwa_ticker_access_requirements_2026-07-16.csv")
    parser.add_argument("--network-output", default="reports/rwa_network_rpc_access_requirements_2026-07-16.csv")
    parser.add_argument("--platform-output", default="reports/rwa_platform_access_requirements_2026-07-16.csv")
    parser.add_argument("--summary-output", default="reports/rwa_access_requirements_summary_2026-07-16.json")
    return parser.parse_args()


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def env_names(path: Path) -> set[str]:
    if not path.exists():
        return set()
    names: set[str] = set()
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        names.add(line.split("=", 1)[0].strip())
    return names


def pipe(values: set[str]) -> str:
    return "|".join(sorted((value for value in values if value), key=str.casefold))


def network_requirement(network: str) -> tuple[str, str, str, str]:
    if network in EVM_NETWORKS:
        env_name, url, method = EVM_NETWORKS[network]
        return env_name, "evm_rpc", url, method
    return NON_EVM_NETWORKS.get(
        network,
        (f"{network.upper().replace(' ', '_')}_API_URL", "chain_or_partner", "", "Identify the official chain/indexer endpoint and issuer source before integration."),
    )


def platform_requirement(platform: str, classes: set[str], website: str) -> tuple[str, str, str]:
    if platform in PLATFORM_OVERRIDES:
        return PLATFORM_OVERRIDES[platform]
    if classes <= {"equity", "etf"}:
        return (
            "venue_or_platform_market_data",
            website,
            "Request a documented quote/order-book API and redistribution rights, or identify a verified liquid onchain pool.",
        )
    return (
        "issuer_nav_or_transfer_agent",
        website,
        "Request timestamped NAV/share-price or transfer-agent data, raw-payload retention, and redistribution rights; add a pool only where liquid.",
    )


def main() -> None:
    args = parse_args()
    ticker_rows = read_csv(Path(args.ticker_input))
    token_rows = read_csv(Path(args.token_input))
    configured = env_names(Path(args.env_file))
    pool_payload = json.loads(Path(args.pool_input).read_text(encoding="utf-8"))
    known_pool_tickers = {
        str(row.get("rwa_ticker") or "").upper()
        for row in pool_payload.get("pools", [])
        if isinstance(row, dict) and row.get("rwa_ticker")
    }
    xstocks_path = Path(args.xstocks_input)
    xstocks_payload = json.loads(xstocks_path.read_text(encoding="utf-8")) if xstocks_path.exists() else {}
    public_reference_tickers = {
        str(row.get("rwa_xyz_ticker") or "").upper()
        for row in xstocks_payload.get("rows", [])
        if isinstance(row, dict)
        and isinstance(row.get("quote"), (int, float))
        and float(row["quote"]) > 0
    }

    ticker_access_rows: list[dict[str, Any]] = []
    for row in ticker_rows:
        ticker = row["rwa_xyz_ticker"]
        networks = {value for value in row["networks"].split("|") if value}
        platforms = {value for value in row["platforms"].split("|") if value}
        requirements = [network_requirement(network) for network in networks]
        rpc_envs = {item[0] for item in requirements if item[1] != "partner_api"}
        missing_rpc_envs = rpc_envs - configured
        was_current = row["currently_candidate_sourceable"] == "True"
        public_reference = ticker.upper() in public_reference_tickers
        current = was_current or public_reference
        known_pool = ticker.upper() in known_pool_tickers

        if current:
            access_bucket = "candidate_now_needs_production_quality"
            rpc_role = (
                "verified_pool_needed_for_executable_liquidity; reference_price_already_public"
                if public_reference and not was_current
                else "supplemental_or_existing_source"
            )
            primary_requirement = (
                "Public xStocks reference price is live without a key; add venue/pool depth, source-timestamp handling, rights, quality windows, benchmark alignment, and consensus."
                if public_reference and not was_current
                else "Continuous quality windows, replay, depth, benchmark, consensus, and rights gates."
            )
        elif known_pool:
            access_bucket = "near_term_rpc_pool_unlock"
            rpc_role = "primary_market_price_possible_from_known_pool"
            primary_requirement = "Configure the chain RPC, complete the pool invariant/tick decoder, and validate liquidity and manipulation resistance."
        elif row["current_status"] == "additional_market_price_candidate":
            access_bucket = "venue_api_or_new_pool_required"
            rpc_role = "conditional_only_if_liquid_pool_is_discovered"
            primary_requirement = "Obtain platform/venue market data or discover and verify a liquid pool; an RPC endpoint alone does not provide an equity price."
        else:
            access_bucket = "issuer_nav_or_onchain_rate_required"
            rpc_role = "secondary_market_or_onchain_rate_only"
            primary_requirement = "Obtain issuer NAV/share-price or identify an audited onchain exchange-rate/oracle contract; a token balance alone is not a price."

        ticker_access_rows.append(
            {
                "rwa_xyz_ticker": ticker,
                "asset_classes": row["asset_classes"],
                "platforms": row["platforms"],
                "networks": row["networks"],
                "token_contract_count": row["token_contract_count"],
                "token_contract_addresses": row["token_contract_addresses"],
                "current_status": row["current_status"],
                "access_bucket": access_bucket,
                "new_public_reference_price": str(public_reference and not was_current),
                "public_reference_source": "xstocks_public" if public_reference else "",
                "known_exact_pool_candidate": str(known_pool),
                "rpc_role": rpc_role,
                "required_rpc_envs": pipe(rpc_envs),
                "missing_rpc_envs": pipe(missing_rpc_envs),
                "platform_access_packages": pipe({platform_requirement(p, set(row["asset_classes"].split("|")), "")[0] for p in platforms}),
                "intended_use_case": row["intended_use_case"],
                "primary_requirement": primary_requirement,
                "production_grade": "False",
            }
        )

    token_by_network: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in token_rows:
        token_by_network[row["network"]].append(row)
    network_rows: list[dict[str, Any]] = []
    for network, rows in sorted(token_by_network.items(), key=lambda item: (-len(item[1]), item[0])):
        env_name, access_type, url, method = network_requirement(network)
        blocked = [
            row for row in rows
            if row["currently_sourceable"] != "True"
            and row["rwa_xyz_ticker"].upper() not in public_reference_tickers
        ]
        network_rows.append(
            {
                "network": network,
                "access_type": access_type,
                "required_env": env_name,
                "configured_in_env": str(env_name in configured),
                "catalog_token_rows": len(rows),
                "blocked_token_rows": len(blocked),
                "blocked_unique_tickers": len({row["rwa_xyz_ticker"].upper() for row in blocked}),
                "access_url": url,
                "method": method,
                "rpc_limitation": "RPC yields a price only from a verified pool, oracle, or exchange-rate contract; token metadata and balances are not prices.",
            }
        )

    token_by_platform: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in token_rows:
        token_by_platform[row["platform"]].append(row)
    platform_rows: list[dict[str, Any]] = []
    for platform, rows in sorted(token_by_platform.items(), key=lambda item: (-len(item[1]), item[0])):
        blocked = [
            row for row in rows
            if row["currently_sourceable"] != "True"
            and row["rwa_xyz_ticker"].upper() not in public_reference_tickers
        ]
        classes = {row["asset_class"] for row in rows}
        websites = {row["platform_website"] for row in rows if row["platform_website"]}
        package, url, method = platform_requirement(platform, classes, sorted(websites)[0] if websites else "")
        platform_rows.append(
            {
                "platform": platform,
                "access_package": package,
                "catalog_token_rows": len(rows),
                "blocked_token_rows": len(blocked),
                "blocked_unique_tickers": len({row["rwa_xyz_ticker"].upper() for row in blocked}),
                "asset_classes": pipe(classes),
                "networks": pipe({row["network"] for row in rows}),
                "access_url": url,
                "method": method,
                "rpc_alternative": "Use RPC pool/oracle/rate state where verified; otherwise this platform/issuer access remains required.",
                "production_grade": "False",
            }
        )

    bucket_counts = Counter(row["access_bucket"] for row in ticker_access_rows)
    summary = {
        "unique_rwa_tickers": len(ticker_access_rows),
        "candidate_sourceable_now": bucket_counts["candidate_now_needs_production_quality"],
        "xstocks_public_positive_reference_tickers": len(public_reference_tickers),
        "xstocks_public_new_reference_tickers": sum(row["new_public_reference_price"] == "True" for row in ticker_access_rows),
        "near_term_rpc_pool_unlocks": bucket_counts["near_term_rpc_pool_unlock"],
        "venue_api_or_new_pool_required": bucket_counts["venue_api_or_new_pool_required"],
        "issuer_nav_or_onchain_rate_required": bucket_counts["issuer_nav_or_onchain_rate_required"],
        "production_grade": 0,
        "configured_access_env_names": sorted(configured & {row["required_env"] for row in network_rows}),
        "missing_network_access_env_names": sorted(
            {
                row["required_env"]
                for row in network_rows
                if row["configured_in_env"] == "False" and int(row["blocked_token_rows"]) > 0
            }
        ),
        "access_bucket_counts": dict(sorted(bucket_counts.items())),
    }

    write_csv(Path(args.ticker_output), ticker_access_rows)
    write_csv(Path(args.network_output), network_rows)
    write_csv(Path(args.platform_output), platform_rows)
    Path(args.summary_output).write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
