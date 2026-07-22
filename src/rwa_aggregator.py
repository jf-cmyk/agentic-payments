"""RWA aggregator orchestration and operational roadmap."""

from __future__ import annotations

from collections import defaultdict
from statistics import median
from typing import Any

from src.rwa_adapters import RWA_ADAPTER_REGISTRY, RWAAdapterRegistry
from src.rwa_pricing import (
    calculate_bidask,
    calculate_block_vwap,
    calculate_executable_composite,
    calculate_reference_composite,
    detect_outliers,
)


AGGREGATOR_TODOS: list[dict[str, Any]] = [
    {
        "id": "adapter-contract",
        "status": "complete",
        "priority": "P0",
        "title": "Define normalized adapter contract",
        "done_when": "Every venue returns the same bid/ask, order-book, quality, and metadata fields.",
    },
    {
        "id": "registry",
        "status": "complete",
        "priority": "P0",
        "title": "Create central venue registry",
        "done_when": "Discovery endpoints can list implemented and planned feed adapters.",
    },
    {
        "id": "kraken-xstocks-rest",
        "status": "complete",
        "priority": "P0",
        "title": "Implement Kraken xStocks REST adapter",
        "done_when": "Ticker and depth responses normalize into aggregator input shapes.",
    },
    {
        "id": "aggregation-policy",
        "status": "complete",
        "priority": "P0",
        "title": "Implement source-independent aggregation policy",
        "done_when": "Submitted observations can be scored, filtered, and consolidated without venue-specific logic.",
    },
    {
        "id": "ostium-adapter",
        "status": "planned",
        "priority": "P1",
        "title": "Implement Ostium live adapter",
        "done_when": "Builder API bid/mid/ask and simulated-depth payloads feed the same RWA calculation endpoints.",
    },
    {
        "id": "gains-adapter",
        "status": "planned",
        "priority": "P1",
        "title": "Implement Gains live adapter",
        "done_when": "Price stream and recent trades produce mark/trade observations with no-book source labels.",
    },
    {
        "id": "hyperliquid-paxg-adapter",
        "status": "complete",
        "priority": "P1",
        "title": "Implement Hyperliquid PAXG adapter",
        "done_when": "Public info endpoint l2Book data produces PAXG bid/ask and block-size VWAP observations.",
    },
    {
        "id": "jupiter-xstocks-adapter",
        "status": "implemented_blocked_on_token_catalog_or_api_key",
        "priority": "P2",
        "title": "Implement Jupiter quote-sweep adapter",
        "done_when": "Jupiter /swap/v1/quote maps configured token mints to executable route-derived VWAP with route-plan provenance.",
    },
    {
        "id": "dex-pool-allowlist",
        "status": "complete",
        "priority": "P0",
        "title": "Create high-quality DEX pool and token allowlist",
        "done_when": "Every DEX route/pool maps to verified token contracts, minimum liquidity, volume, price-impact, and manipulation-risk gates.",
    },
    {
        "id": "solana-dex-adapters",
        "status": "partial",
        "priority": "P1",
        "title": "Implement Solana DEX route and pool-state adapters",
        "done_when": "Jupiter quote-sweep is implemented; Raydium, Orca, and Meteora still need pool-state observations with slot freshness and replay payloads.",
    },
    {
        "id": "evm-dex-adapters",
        "status": "planned",
        "priority": "P1",
        "title": "Implement EVM DEX pool/indexer adapters",
        "done_when": "Uniswap, Curve, Balancer, and Aerodrome produce pool-state observations with block freshness and indexer health checks.",
    },
    {
        "id": "ondo-stocks-adapter",
        "status": "planned",
        "priority": "P2",
        "title": "Implement Ondo Stocks adapter",
        "done_when": "Whitelisted quote/price stream and product catalog produce quote-stream observations.",
    },
    {
        "id": "bybit-xstocks-adapter",
        "status": "blocked_on_access",
        "priority": "P2",
        "title": "Implement Bybit xStocks adapter",
        "done_when": "Regional/API availability is confirmed and instrument lists can be fetched reliably.",
    },
    {
        "id": "backed-xstocks-issuer-reference",
        "status": "complete",
        "priority": "P2",
        "title": "Implement Backed/xStocks issuer metadata ingestion",
        "done_when": "Public issuer catalog, token contracts, and reference prices validate xStock symbol mapping; executable depth remains separate.",
    },
    {
        "id": "tradfi-benchmark-reference",
        "status": "planned",
        "priority": "P1",
        "title": "Implement regulated benchmark reference adapter",
        "done_when": "Licensed NBBO/trade/reference data can benchmark RWA prices and corporate actions.",
    },
    {
        "id": "global-listed-equity-feeds",
        "status": "planned",
        "priority": "P0",
        "title": "Implement licensed global listed-equity feeds",
        "done_when": "U.S., HKEX, China A-share, KRX, JPX, TWSE/TPEx, NSE/BSE, LSE/LSEG, Euronext/Xetra, TSX, ASX, and SGX feeds produce bid/ask and trade-VWAP observations.",
    },
    {
        "id": "pyth-oracle-reference",
        "status": "planned",
        "priority": "P1",
        "title": "Implement Pyth oracle parity adapter",
        "done_when": "Pyth catalog, prices, confidence intervals, rates, macro, and NAV targets can be reconciled against our coverage.",
    },
    {
        "id": "chainlink-oracle-reference",
        "status": "planned",
        "priority": "P1",
        "title": "Implement Chainlink oracle parity adapter",
        "done_when": "Chainlink feed catalog, heartbeat/deviation metadata, NAV, PoR, and tokenized-asset categories can be reconciled against our coverage.",
    },
    {
        "id": "persistence",
        "status": "planned",
        "priority": "P1",
        "title": "Persist raw observations and quality receipts",
        "done_when": "Every consolidated price can be replayed from stored venue payload hashes and normalized rows.",
    },
    {
        "id": "scheduler",
        "status": "planned",
        "priority": "P1",
        "title": "Add continuous polling and websocket supervisors",
        "done_when": "Adapters have per-venue cadence, backoff, freshness SLAs, and health metrics.",
    },
    {
        "id": "promotion-gates",
        "status": "complete",
        "priority": "P0",
        "title": "Add feed promotion workflow",
        "done_when": "No feed can move from supplemental to benchmark/replacement without quality, legal, and backtest signoff.",
    },
    {
        "id": "consensus-source-plan",
        "status": "complete",
        "priority": "P0",
        "title": "Build consensus source plan",
        "done_when": "Primary, oracle, benchmark, futures, NAV and issuer sources are grouped into one sourcing plan.",
    },
    {
        "id": "consensus-metric",
        "status": "complete",
        "priority": "P0",
        "title": "Implement quality-weighted consensus metric",
        "done_when": "Submitted observations produce a consensus value, reliability score, source basis, and inclusion/exclusion flags.",
    },
    {
        "id": "provider-catalog-ingestion",
        "status": "planned",
        "priority": "P0",
        "title": "Ingest provider catalogs into canonical registry",
        "done_when": "Pyth, Chainlink, RedStone, DIA, exchange, futures, DEX and issuer catalogs map to canonical assets and feed ids.",
    },
    {
        "id": "consensus-window-supervisor",
        "status": "planned",
        "priority": "P0",
        "title": "Run continuous consensus windows",
        "done_when": "Every promoted feed has rolling 1m/5m/30m consensus receipts with freshness, latency, tick frequency, deviation and alignment metrics.",
    },
]


def build_aggregator_status(
    *,
    registry: RWAAdapterRegistry = RWA_ADAPTER_REGISTRY,
) -> dict[str, Any]:
    """Return registry and build-readiness status."""
    adapters = registry.list_metadata()
    status_counts: dict[str, int] = defaultdict(int)
    for adapter in adapters:
        status_counts[str(adapter["status"])] += 1
    todo_counts: dict[str, int] = defaultdict(int)
    for todo in AGGREGATOR_TODOS:
        todo_counts[str(todo["status"])] += 1
    return {
        "adapter_count": len(adapters),
        "adapter_status_counts": dict(sorted(status_counts.items())),
        "todo_status_counts": dict(sorted(todo_counts.items())),
        "adapters": adapters,
        "todos": AGGREGATOR_TODOS,
        "operating_model": {
            "add_new_feed_steps": [
                "Create an adapter implementing metadata, fetch_bidask, and fetch_order_book.",
                "Register it in build_default_registry.",
                "Map raw venue fields into normalized source_type and asset_class values.",
                "Add mocked HTTP tests using representative payloads.",
                "Add live probe only after legal/API access is confirmed.",
                "Promote the feed from planned to supplemental after freshness, spread, and outlier checks pass.",
            ],
            "quality_gates": [
                "source_type must be explicit",
                "timestamps must be present for real-time consolidation",
                "wide spreads downgrade quality",
                "partial fills remain visible and are not extrapolated",
                "MAD outliers and severe benchmark drift are excluded from consolidated value",
            ],
        },
    }


def aggregate_submitted_observations(payload: dict[str, Any]) -> dict[str, Any]:
    """Aggregate caller-supplied normalized RWA observations."""
    symbol = str(payload.get("symbol") or "").strip().upper()
    if not symbol:
        raise ValueError("symbol is required")
    asset_class = str(payload.get("asset_class") or "equity").strip().lower()
    benchmark_price = payload.get("benchmark_price")
    bidask_rows = payload.get("bidask") or []
    order_books = payload.get("order_books") or []
    block_size_usd = payload.get("block_size_usd")
    side = str(payload.get("side") or "buy").strip().lower()

    normalized_bidask: list[dict[str, Any]] = []
    outlier_observations: list[dict[str, Any]] = []
    for row in bidask_rows:
        if not isinstance(row, dict):
            continue
        bidask_payload = {
            "symbol": row.get("symbol") or symbol,
            "asset_class": row.get("asset_class") or asset_class,
            "benchmark_price": benchmark_price,
            **row,
        }
        normalized = calculate_bidask(bidask_payload)
        normalized_bidask.append(normalized)
        outlier_observations.append(
            {
                "symbol": symbol,
                "venue": normalized["venue"],
                "value": normalized["mid"],
                "source_type": normalized["source_type"],
                "timestamp": row.get("timestamp"),
            }
        )

    normalized_vwaps: list[dict[str, Any]] = []
    if block_size_usd is not None:
        for row in order_books:
            if not isinstance(row, dict):
                continue
            vwap_payload = {
                "symbol": row.get("symbol") or symbol,
                "asset_class": row.get("asset_class") or asset_class,
                "block_size_usd": block_size_usd,
                "side": row.get("side") or side,
                "benchmark_price": benchmark_price,
                **row,
            }
            normalized = calculate_block_vwap(vwap_payload)
            normalized_vwaps.append(normalized)
            outlier_observations.append(
                {
                    "symbol": symbol,
                    "venue": normalized["venue"],
                    "value": normalized["vwap"],
                    "source_type": normalized["source_type"],
                    "timestamp": row.get("timestamp"),
                }
            )

    quality = None
    if outlier_observations:
        quality = detect_outliers(
            {
                "symbol": symbol,
                "asset_class": asset_class,
                "benchmark_price": benchmark_price,
                "observations": outlier_observations,
            }
        )

    included_values = [
        row["value"]
        for row in (quality or {}).get("observations", [])
        if row.get("include_in_consolidated")
    ]
    return {
        "symbol": symbol,
        "asset_class": asset_class,
        "block_size_usd": block_size_usd,
        "side": side,
        "bidask": normalized_bidask,
        "block_vwaps": normalized_vwaps,
        "quality": quality,
        "consolidated": {
            "price_type": "quality_gated_observation_median",
            "is_executable": False,
            "value": round(median(included_values), 10) if included_values else None,
            "included_observations": len(included_values),
            "total_observations": len(outlier_observations),
        },
    }


def build_blocksize_oracle_snapshot(payload: dict[str, Any]) -> dict[str, Any]:
    """Build separate two-sided executable and robust-reference composites.

    No reference observation can fill the executable route, and neither output
    silently falls back to the other. Callers decide which independently valid
    product is appropriate for a downstream use case.
    """
    common = {
        key: payload[key]
        for key in (
            "canonical_asset_id",
            "quote_currency",
            "request_kind",
            "requested_amount",
            "now",
            "max_age_ms",
            "min_reliability",
            "max_venue_share",
            "min_venues",
            "allowed_rights_statuses",
        )
        if payload.get(key) is not None
    }
    buy = calculate_executable_composite({**common, "side": "buy", "books": payload.get("buy_books")})
    sell = calculate_executable_composite({**common, "side": "sell", "books": payload.get("sell_books")})

    executable_flags = sorted(set([*buy["quality_flags"], *sell["quality_flags"]]))
    buy_price = buy.get("effective_vwap")
    sell_price = sell.get("effective_vwap")
    mid = (buy_price + sell_price) / 2 if buy_price is not None and sell_price is not None else None
    spread = buy_price - sell_price if buy_price is not None and sell_price is not None else None
    spread_bps = spread / mid * 10_000 if mid and spread is not None else None
    if spread is not None and spread < 0:
        executable_flags.append("crossed_composite")
    executable_status = "valid_executable"
    if buy["status"] != "full_fill" or sell["status"] != "full_fill":
        executable_status = "not_fully_executable"
    if "crossed_composite" in executable_flags:
        executable_status = "halt_crossed_composite"

    reference = None
    reference_observations = payload.get("reference_observations")
    if isinstance(reference_observations, list) and reference_observations:
        reference = calculate_reference_composite(
            {
                "canonical_asset_id": payload.get("canonical_asset_id"),
                "quote_currency": payload.get("quote_currency"),
                "composite_id": payload.get("composite_id"),
                "now": payload.get("now"),
                "max_age_ms": payload.get("reference_max_age_ms", payload.get("max_age_ms")),
                "min_independent_sources": payload.get("min_independent_sources", 2),
                "max_source_weight": payload.get("max_source_weight", 0.5),
                "mad_z_limit": payload.get("mad_z_limit", 3.5),
                "allowed_rights_statuses": payload.get("allowed_rights_statuses"),
                "observations": reference_observations,
            }
        )

    return {
        "composite_id": str(payload.get("composite_id") or ""),
        "canonical_asset_id": buy["canonical_asset_id"],
        "quote_currency": buy["quote_currency"],
        "executable": {
            "price_type": "two_sided_executable_block_vwap",
            "status": executable_status,
            "buy_vwap": buy_price,
            "sell_vwap": sell_price,
            "mid": round(mid, 12) if mid is not None else None,
            "spread": round(spread, 12) if spread is not None else None,
            "spread_bps": round(spread_bps, 12) if spread_bps is not None else None,
            "quality_flags": sorted(set(executable_flags)),
            "buy": buy,
            "sell": sell,
        },
        "reference": reference,
        "separation_policy": "reference observations never fill executable routes",
    }


def evaluate_feed_promotion(payload: dict[str, Any]) -> dict[str, Any]:
    """Evaluate whether a feed can be promoted to a stronger trust tier."""
    venue = str(payload.get("venue") or "").strip().lower()
    if not venue:
        raise ValueError("venue is required")
    current_tier = str(payload.get("current_tier") or "planned").strip().lower()
    target_tier = str(payload.get("target_tier") or "supplemental").strip().lower()
    backtest_days = int(payload.get("backtest_days") or 0)
    uptime_pct = float(payload.get("uptime_pct") or 0)
    observation_count = int(payload.get("observation_count") or 0)
    excluded_observation_pct = float(payload.get("excluded_observation_pct") or 100)
    median_abs_benchmark_drift_bps = float(payload.get("median_abs_benchmark_drift_bps") or 10_000)
    legal_approved = bool(payload.get("legal_approved"))
    source_type_locked = bool(payload.get("source_type_locked"))
    replayable_receipts = bool(payload.get("replayable_receipts"))

    required_days = 7 if target_tier == "supplemental" else 30
    required_uptime = 98.0 if target_tier == "supplemental" else 99.5
    required_observations = 1_000 if target_tier == "supplemental" else 10_000
    max_excluded_pct = 10.0 if target_tier == "supplemental" else 2.0
    max_drift_bps = 75.0 if target_tier == "supplemental" else 25.0

    checks = [
        {
            "name": "backtest_window",
            "passed": backtest_days >= required_days,
            "actual": backtest_days,
            "required": required_days,
        },
        {
            "name": "uptime",
            "passed": uptime_pct >= required_uptime,
            "actual": uptime_pct,
            "required": required_uptime,
        },
        {
            "name": "observation_count",
            "passed": observation_count >= required_observations,
            "actual": observation_count,
            "required": required_observations,
        },
        {
            "name": "excluded_observation_pct",
            "passed": excluded_observation_pct <= max_excluded_pct,
            "actual": excluded_observation_pct,
            "required": max_excluded_pct,
        },
        {
            "name": "benchmark_drift",
            "passed": median_abs_benchmark_drift_bps <= max_drift_bps,
            "actual": median_abs_benchmark_drift_bps,
            "required": max_drift_bps,
        },
        {
            "name": "legal_approved",
            "passed": legal_approved,
            "actual": legal_approved,
            "required": True,
        },
        {
            "name": "source_type_locked",
            "passed": source_type_locked,
            "actual": source_type_locked,
            "required": True,
        },
        {
            "name": "replayable_receipts",
            "passed": replayable_receipts,
            "actual": replayable_receipts,
            "required": True,
        },
    ]
    failed = [check for check in checks if not check["passed"]]
    return {
        "venue": venue,
        "current_tier": current_tier,
        "target_tier": target_tier,
        "decision": "promote" if not failed else "hold",
        "failed_checks": [check["name"] for check in failed],
        "checks": checks,
    }
