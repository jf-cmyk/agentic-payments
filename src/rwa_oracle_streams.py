"""Oracle-streamed RWA/traditional price feed coverage.

These feeds are distinct from executable venues and DEX pools. They provide
published oracle prices, confidence/heartbeat metadata, proof-of-reserve, NAV,
or reference values that can become benchmark and consensus legs for the RWA
aggregator after licensing and feed-level validation.
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any


ORACLE_STREAM_PROVIDERS: list[dict[str, Any]] = [
    {
        "provider_id": "pyth",
        "name": "Pyth Network / Pyth Pro",
        "source_url": "https://www.pyth.network/price-feeds",
        "published_feed_count": "3,059+",
        "published_network_count": None,
        "asset_classes": [
            "commodities",
            "crypto",
            "crypto_indices",
            "crypto_redemption_rates",
            "economic_data",
            "equities",
            "fx",
            "metals",
            "nav",
            "rates",
        ],
        "stream_model": "pull_or_terminal_stream_with_plan_based_update_frequency",
        "latency_or_frequency": {
            "free": "10s update frequency, view-only, no API or redistribution rights",
            "starter": "crypto at up to 1s update frequency",
            "pro": "all data at up to 1ms update frequency with display/non-display and limited redistribution rights",
        },
        "rwa_relevance": [
            "broadest currently identified oracle-stream RWA coverage",
            "direct overlap with our equities, FX, metals, commodities, rates, NAV and macro targets",
            "best candidate for a licensed oracle reference leg",
        ],
        "integration_status": "catalog_adapter_and_data_plan_required",
        "counting_rule": "count each Pyth symbol once per feed id; provider may publish same base asset across multiple chains or products",
    },
    {
        "provider_id": "chainlink",
        "name": "Chainlink Data Feeds / Data Streams / SmartData",
        "source_url": "https://data.chain.link/feeds",
        "published_feed_count": "1,683 entries",
        "published_network_count": 30,
        "published_category_count": 21,
        "asset_classes": [
            "asset_token",
            "commodity",
            "crypto",
            "equity",
            "etf",
            "fiat",
            "fixed_income",
            "index",
            "index_fund",
            "macroeconomics",
            "money_market_fund",
            "nav",
            "private_credit_fund",
            "proof_of_reserve",
            "stablecoin",
            "stablecoin_stability_assessment",
            "tokenized_asset",
            "tokenized_commodities",
            "tokenized_fund",
            "tokenized_treasury_fund",
            "us_treasuries",
        ],
        "stream_model": "onchain_aggregator_feeds_and_low_latency_data_streams",
        "latency_or_frequency": {
            "data_feeds": "feed-level heartbeat and deviation threshold",
            "data_streams": "low-latency pull-based reports where product access is available",
        },
        "rwa_relevance": [
            "strongest onchain registry for feed-level heartbeat/deviation metadata",
            "important for NAV, PoR, tokenized funds, tokenized treasury funds, commodities, FX and macro categories",
            "can provide benchmark and proof-of-reserve legs, not executable VWAP liquidity",
        ],
        "integration_status": "catalog_adapter_required",
        "counting_rule": "count each feed-network deployment separately, then de-duplicate by canonical symbol for asset coverage",
    },
    {
        "provider_id": "redstone",
        "name": "RedStone Push/Pull/Hybrid Feeds",
        "source_url": "https://app.redstone.finance/push-feeds",
        "docs_url": "https://docs.redstone.finance/docs/introduction",
        "published_feed_count": "801 push feeds",
        "published_network_count": "70+",
        "asset_classes": [
            "crypto",
            "stablecoin",
            "liquid_staking",
            "liquid_restaking",
            "btcfi",
            "rwa",
            "specialized_defi_assets",
        ],
        "stream_model": "push_pull_or_hybrid_oracle_feeds",
        "latency_or_frequency": {
            "push_feeds": "feed-level deviation and heartbeat, visible per feed",
            "pull_feeds": "fresh signed data packages fetched by consumers",
            "hybrid": "ERC-7412-style push plus pull availability",
        },
        "rwa_relevance": [
            "valuable for RWA and specialized DeFi assets that do not have deep exchange liquidity",
            "good candidate for multi-oracle consensus checks against Chainlink/Pyth and DEX pools",
        ],
        "integration_status": "catalog_adapter_required",
        "counting_rule": "count push-feed contract deployments separately; also maintain symbol-level de-duplicated feed set",
    },
    {
        "provider_id": "dia",
        "name": "DIA Oracles",
        "source_url": "https://www.diadata.org/app/price/",
        "published_feed_count": "3,000+ token price feeds",
        "published_network_count": None,
        "asset_classes": ["digital_assets", "rwa_data_feeds", "fundamental_feeds", "randomness"],
        "stream_model": "verifiable_oracle_feeds_and_api_backed_onchain_data",
        "latency_or_frequency": {"feeds": "feed-specific source and chain configuration"},
        "rwa_relevance": [
            "useful long-tail token and RWA oracle source",
            "source-count metadata can help with confidence and provenance scoring",
        ],
        "integration_status": "catalog_adapter_required",
        "counting_rule": "count DIA assets separately from chain deployments; verify RWA subset before replacement use",
    },
    {
        "provider_id": "chronicle",
        "name": "Chronicle Protocol",
        "source_url": "https://chroniclelabs.org/dashboard/oracles",
        "published_feed_count": "dashboard_catalog_available_count_not_yet_imported",
        "published_network_count": None,
        "asset_classes": ["crypto", "stablecoin", "rwa", "proof_of_asset"],
        "stream_model": "onchain_oracle_contracts",
        "latency_or_frequency": {"feeds": "feed-level oracle configuration to import"},
        "rwa_relevance": [
            "important for Maker/Sky-adjacent oracle validation and proof-of-asset style checks",
        ],
        "integration_status": "catalog_adapter_required",
        "counting_rule": "import dashboard/contracts and count per canonical oracle id",
    },
    {
        "provider_id": "api3",
        "name": "API3 Market / dAPIs",
        "source_url": "https://market.api3.org/",
        "published_feed_count": "market_catalog_available_count_not_yet_imported",
        "published_network_count": None,
        "asset_classes": ["crypto", "fx", "commodity", "rwa_candidate"],
        "stream_model": "first_party_oracle_dapi",
        "latency_or_frequency": {"feeds": "feed and chain specific"},
        "rwa_relevance": [
            "first-party oracle model can be useful as an independent reference source",
        ],
        "integration_status": "catalog_adapter_required",
        "counting_rule": "count dAPI per chain deployment, then de-duplicate by canonical symbol",
    },
    {
        "provider_id": "switchboard",
        "name": "Switchboard",
        "source_url": "https://app.switchboard.xyz/feeds",
        "published_feed_count": "catalog_available_count_not_yet_imported",
        "published_network_count": None,
        "asset_classes": ["crypto", "fx_candidate", "rwa_candidate", "custom_feeds"],
        "stream_model": "oracle_network_and_verifiable_feed_jobs",
        "latency_or_frequency": {"feeds": "job and chain specific"},
        "rwa_relevance": [
            "useful for Solana/SVM and custom-feed oracle validation alongside Pyth and RedStone",
        ],
        "integration_status": "catalog_adapter_required",
        "counting_rule": "count feed jobs/deployments and normalize to canonical symbols",
    },
    {
        "provider_id": "band",
        "name": "Band Protocol",
        "source_url": "https://bandprotocol.com/",
        "published_feed_count": "not_yet_imported",
        "published_network_count": None,
        "asset_classes": ["crypto", "fx_candidate", "commodity_candidate", "custom_feeds"],
        "stream_model": "oracle_network_data_requests",
        "latency_or_frequency": {"feeds": "request and chain specific"},
        "rwa_relevance": [
            "potential additional independent oracle consensus source after catalog import",
        ],
        "integration_status": "candidate_catalog_research_required",
        "counting_rule": "count active price feeds after supported catalog import",
    },
    {
        "provider_id": "tellor",
        "name": "Tellor",
        "source_url": "https://tellor.io/",
        "published_feed_count": "not_yet_imported",
        "published_network_count": None,
        "asset_classes": ["crypto", "custom_feeds", "rwa_candidate"],
        "stream_model": "permissionless_oracle_reporting",
        "latency_or_frequency": {"feeds": "query and reporter dependent"},
        "rwa_relevance": [
            "candidate fallback oracle source for custom RWA feeds with stricter quality gates",
        ],
        "integration_status": "candidate_catalog_research_required",
        "counting_rule": "count active query ids after registry import",
    },
]


ORACLE_ASSET_CLASS_TO_RWA_BUCKET: dict[str, str] = {
    "asset_token": "tokenized_asset",
    "commodity": "commodity",
    "commodities": "commodity",
    "digital_assets": "crypto_or_tokenized_asset",
    "economic_data": "macro",
    "equities": "equity",
    "equity": "equity",
    "etf": "etf",
    "fiat": "fx",
    "fixed_income": "rates_or_fixed_income",
    "fundamental_feeds": "fundamental_reference",
    "fx": "fx",
    "index": "index",
    "index_fund": "fund",
    "macroeconomics": "macro",
    "metals": "metal",
    "money_market_fund": "fund",
    "nav": "nav",
    "private_credit_fund": "private_credit",
    "proof_of_asset": "proof_of_reserve",
    "proof_of_reserve": "proof_of_reserve",
    "rates": "rates",
    "rwa": "rwa_general",
    "rwa_data_feeds": "rwa_general",
    "stablecoin": "stablecoin",
    "stablecoin_stability_assessment": "stablecoin",
    "tokenized_asset": "tokenized_asset",
    "tokenized_commodities": "tokenized_commodity",
    "tokenized_fund": "tokenized_fund",
    "tokenized_treasury_fund": "treasury_fund",
    "us_treasuries": "treasury",
}


def _numeric_count(raw: Any) -> int | None:
    if isinstance(raw, int):
        return raw
    text = str(raw or "")
    digits = "".join(ch for ch in text if ch.isdigit())
    if not digits:
        return None
    return int(digits)


def build_oracle_stream_coverage() -> dict[str, Any]:
    """Return oracle-stream providers and how they map to RWA coverage."""
    provider_rows = []
    by_bucket = Counter()
    imported_count_lower_bound = 0
    known_provider_count = 0
    for provider in ORACLE_STREAM_PROVIDERS:
        count = _numeric_count(provider.get("published_feed_count"))
        if count is not None:
            imported_count_lower_bound += count
            known_provider_count += 1
        buckets = sorted(
            {
                ORACLE_ASSET_CLASS_TO_RWA_BUCKET.get(str(asset_class), str(asset_class))
                for asset_class in provider["asset_classes"]
            }
        )
        for bucket in buckets:
            by_bucket[bucket] += 1
        provider_rows.append(
            {
                **provider,
                "rwa_buckets": buckets,
                "numeric_feed_count": count,
                "usable_as": [
                    "benchmark_reference",
                    "cross_oracle_consensus",
                    "staleness_and_deviation_monitor",
                    "fallback_reference_after_license",
                ],
                "not_usable_as": [
                    "executable_liquidity",
                    "native_order_book",
                    "block_size_fill_source",
                ],
                "promotion_requirements": [
                    "import complete provider catalog and canonical symbol mapping",
                    "record heartbeat/deviation/confidence metadata per feed",
                    "verify license, display, non-display, and redistribution rights",
                    "compare against Blocksize, exchange, DEX, and futures-derived benchmarks",
                    "exclude stale feeds and feeds whose update policy is not real-time enough for the asset class",
                ],
            }
        )
    return {
        "summary": {
            "provider_count": len(provider_rows),
            "providers_with_public_counts": known_provider_count,
            "known_feed_entries_lower_bound": imported_count_lower_bound,
            "known_count_note": (
                "Lower bound sums public provider counts where available: Pyth 3,059+, "
                "Chainlink 1,683, RedStone 801, and DIA 3,000+. It does not include "
                "Chronicle, API3, Switchboard, Band, or Tellor until their catalogs are imported."
            ),
            "by_rwa_bucket": dict(sorted(by_bucket.items())),
        },
        "providers": provider_rows,
        "integration_order": [
            "Pyth first for broad equities, FX, metals, commodities, rates, NAV and macro coverage.",
            "Chainlink second for onchain heartbeat/deviation metadata, NAV, PoR, tokenized funds and RWA categories.",
            "RedStone third for push/pull specialized DeFi, LST/LRT, BTCFi and RWA feeds across 70+ chains.",
            "DIA fourth for long-tail tokens and RWA/fundamental feeds with source-count metadata.",
            "Chronicle, API3, Switchboard, Band and Tellor as independent consensus/fallback sources after catalog import.",
        ],
        "counting_methodology": [
            "Do not merge oracle-stream feeds into DEX feed counts; they are reference streams, not liquidity.",
            "Maintain two counts: provider feed deployments and canonical asset symbols.",
            "Use provider feed deployments for operational monitoring and canonical symbols for coverage parity.",
            "For RWA replacement, require at least two independent oracle/reference sources or one oracle plus one executable/source-of-truth venue.",
        ],
    }


def write_oracle_stream_reports(
    *,
    json_path: str | Path,
    csv_path: str | Path,
) -> dict[str, Any]:
    """Write oracle-stream coverage as JSON and CSV."""
    coverage = build_oracle_stream_coverage()
    json_output = Path(json_path)
    csv_output = Path(csv_path)
    json_output.parent.mkdir(parents=True, exist_ok=True)
    csv_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(json.dumps(coverage, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    fieldnames = [
        "provider_id",
        "name",
        "published_feed_count",
        "published_network_count",
        "published_category_count",
        "asset_classes",
        "rwa_buckets",
        "stream_model",
        "integration_status",
        "numeric_feed_count",
        "source_url",
    ]
    with csv_output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for provider in coverage["providers"]:
            writer.writerow(
                {
                    key: json.dumps(provider[key], sort_keys=True)
                    if isinstance(provider.get(key), (list, dict))
                    else provider.get(key, "")
                    for key in fieldnames
                }
            )
    return coverage
