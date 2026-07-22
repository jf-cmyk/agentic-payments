"""Mitigation roadmap for RWA feed discovery and promotion blockers."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from src.rwa_feed_discovery import (
    DEFAULT_REPORTS_DIR,
    build_feed_discovery_audit,
)
from src.rwa_source_readiness import build_source_readiness


DEFAULT_MITIGATION_JSON_PATH = DEFAULT_REPORTS_DIR / "rwa_discovery_mitigation_plan.json"
DEFAULT_MITIGATION_CSV_PATH = DEFAULT_REPORTS_DIR / "rwa_discovery_mitigation_plan.csv"

RESEARCH_BASIS: list[dict[str, str]] = [
    {
        "source": "Jupiter Swap API",
        "url": "https://dev.jup.ag/docs/swap/v1/get-quote",
        "applies_to": "jupiter_router route discovery, quote sweeps, price impact, route replay",
        "relevant_fields": "inputMint, outputMint, amount, slippageBps, priceImpactPct, routePlan, contextSlot, mostReliableAmmsQuoteReport",
    },
    {
        "source": "Hyperliquid Info API",
        "url": "https://hyperliquid.gitbook.io/hyperliquid-docs/for-developers/api/info-endpoint",
        "applies_to": "Hyperliquid RWA spot and PAXG depth, candles, trades, spot index notation",
        "relevant_fields": "spotMeta, allMids, l2Book, candleSnapshot, recentTrades, @{spot_index}",
    },
    {
        "source": "Uniswap v3 SDK quoting guide",
        "url": "https://developers.uniswap.org/docs/sdks/v3/guides/swapping/quoting",
        "applies_to": "EVM CLMM pool discovery, pool identifiers, quote simulation",
        "relevant_fields": "token0, token1, fee, pool address, liquidity, slot0, QuoterV2 amountOut, initializedTicksCrossed, sqrtPriceX96After",
    },
    {
        "source": "Pyth Hermes",
        "url": "https://docs.pyth.network/price-feeds/core/api-instances-and-providers/hermes",
        "applies_to": "oracle benchmark and supplemental real-time price confidence",
        "relevant_fields": "REST updates, streaming updates, rate limits, node-provider recommendation",
    },
    {
        "source": "Chainlink Data Streams",
        "url": "https://docs.chain.link/data-streams",
        "applies_to": "RWA oracle/reference layer, low-latency streams, LWBA-style liquidity metrics, report verification",
        "relevant_fields": "REST, WebSocket, SDK, RWA report schemas, mid, bid/ask, liquidity metrics, onchain verification",
    },
]


GATE_MITIGATIONS: dict[str, dict[str, Any]] = {
    "canonical_identity": {
        "priority": "P0",
        "severity": "critical",
        "root_cause": "Symbols are venue-specific strings and can refer to tokenized, derivative, synthetic, NAV, or issuer-specific instruments.",
        "target_state": "Every feed maps to one canonical asset, asset class, underlying exposure, quote asset, instrument type, issuer where relevant, and transfer/restriction notes.",
        "mitigation_steps": [
            "Resolve each venue symbol through /v1/rwa/resolve and the identity audit.",
            "Attach issuer or exchange metadata for tokenized securities, funds, and synthetic venues.",
            "Reject ambiguous identities until a manual review row confirms the underlying exposure.",
        ],
        "evidence_required": [
            "canonical asset_id",
            "asset_classes",
            "underlying exposure",
            "instrument type",
            "issuer or venue identity note",
        ],
        "acceptance_criteria": [
            "No feed has unverified_identity status.",
            "Ticker aliases resolve to one canonical asset unless explicitly multi-listed.",
        ],
    },
    "venue_identifier": {
        "priority": "P0",
        "severity": "critical",
        "root_cause": "A display symbol is not enough to fetch executable data; each venue needs its native market id, coin, pool, contract, feed id, or route key.",
        "target_state": "Every source row has stable native identifiers and replay-safe request parameters.",
        "mitigation_steps": [
            "For Hyperliquid, store spot index notation such as @268 and verify against spotMeta/allMids.",
            "For Jupiter, store base/quote mint addresses and route allowlist ids.",
            "For EVM pools, store chain id, token contracts, pool address or pool id, fee tier, and block number.",
            "For oracle/reference providers, store feed id, report schema, network, heartbeat/deviation or report freshness rules.",
        ],
        "evidence_required": [
            "native venue identifier",
            "quote asset identifier",
            "adapter request payload",
            "timestamped discovery artifact",
        ],
        "acceptance_criteria": [
            "Adapter can fetch one normalized observation without symbol guessing.",
            "Discovery artifact records enough fields to replay the request.",
        ],
    },
    "token_or_contract_discovery": {
        "priority": "P0",
        "severity": "critical",
        "root_cause": "Onchain tickers can be wrappers or spoofed symbols; token mint/contract identity must be verified before routing.",
        "target_state": "Every onchain token has a verified mint/contract, decimals, issuer/source metadata, holder/pool concentration snapshot, and transfer restriction status.",
        "mitigation_steps": [
            "Run Solana token discovery with Jupiter/Helius inputs and persist mint, decimals, verification, liquidity, organic score, and first pool.",
            "Run EVM token discovery from issuer registries, token lists, verified contracts, and pool registries.",
            "Block tokens with unverifiable issuer metadata, missing decimals, or suspicious holder concentration.",
        ],
        "evidence_required": [
            "mint or contract",
            "decimals",
            "verification status",
            "issuer or registry source",
            "holder/pool concentration review",
        ],
        "acceptance_criteria": [
            "Token identity gate is passed for all route/pool feeds before quote collection affects consensus.",
        ],
    },
    "route_or_pool_discovery": {
        "priority": "P0",
        "severity": "critical",
        "root_cause": "Seed coverage lists candidate symbols, but production needs the actual route, pool, fee tier, or state path.",
        "target_state": "Each DEX feed has an allowlisted route or pool with replayable state and quote evidence.",
        "mitigation_steps": [
            "For Jupiter, request /swap/v1/quote over configured block sizes and persist routePlan, contextSlot, priceImpactPct, AMM labels, and mostReliableAmmsQuoteReport.",
            "For Uniswap v3/v4, compute or discover pool addresses from token pair plus fee, fetch token0/token1/fee/liquidity/slot0 together, and quote with QuoterV2 offchain.",
            "For Curve/Balancer, persist pool address/id, balances, amplification/weights, virtual price where applicable, and simulated swap outputs.",
            "Reject direct routes or single-pool quotes that are illiquid or cannot fill minimum block sizes.",
        ],
        "evidence_required": [
            "routePlan or pool address/id",
            "fee tier or curve parameters",
            "context slot/block number",
            "quote output by block size",
            "raw payload hash",
        ],
        "acceptance_criteria": [
            "Route/pool can be replayed and produces deterministic normalized VWAP/bidask inputs.",
        ],
    },
    "state_instrument_confirmation": {
        "priority": "P0",
        "severity": "critical",
        "root_cause": "Blocksize state rows are references only unless state_instruments confirms matching pool/instrument coverage.",
        "target_state": "State-reference symbols either have confirmed state_instruments/state_pool coverage or are removed from production consideration.",
        "mitigation_steps": [
            "Run scripts/run_rwa_blocksize_state_discovery.py against live Blocksize state_instruments.",
            "For unmatched RWA rows, keep coverage_status as blocked_missing_state_instruments_coverage.",
            "For matched rows, run state_pool freshness, stale-value, and issuer/NAV alignment checks before allowing benchmark use.",
        ],
        "evidence_required": [
            "state_symbol",
            "matched state_instruments rows",
            "state_pool payload",
            "state timestamp/freshness",
        ],
        "acceptance_criteria": [
            "Matched state instrument count is greater than zero and state_pool data is fresh within the configured threshold.",
        ],
    },
    "liquidity_depth_volume": {
        "priority": "P0",
        "severity": "critical",
        "root_cause": "Current evidence is mostly route, symbol, or point-in-time book evidence, not sustained fillability or organic volume.",
        "target_state": "Every live-liquidity feed has block-size VWAP, fill ratio, slippage, top-of-book spread, order-book/pool depth, and organic 24h volume.",
        "mitigation_steps": [
            "Run block-size sweeps for each asset class and side using venue-native order books, route quotes, or pool simulations.",
            "Measure fill ratio and slippage for target notionals; exclude feeds that cannot fill minimum blocks.",
            "Measure 24h volume from venue trades/candles or pool swap events and compare with liquidity minimums.",
            "Persist depth snapshots and quote payload hashes for replay.",
        ],
        "evidence_required": [
            "bid/ask or depth snapshot",
            "VWAP by block size",
            "fill ratio",
            "slippage bps",
            "24h organic volume",
            "depth timestamp",
        ],
        "acceptance_criteria": [
            "Minimum fill ratio is 1.0 for required block size or feed is marked indicative only.",
            "Spread, slippage, and volume clear asset-class thresholds.",
        ],
    },
    "freshness_cadence": {
        "priority": "P0",
        "severity": "critical",
        "root_cause": "A single quote or payload does not prove real-time data quality.",
        "target_state": "Each feed has a continuous benchmark window measuring freshness, latency, tick frequency, gaps, uptime, and benchmark drift.",
        "mitigation_steps": [
            "Run a 30-minute feed-quality window for each ready adapter and Blocksize comparable feed.",
            "Record p50/p95 latency, max age, tick intervals, missing ticks, stale windows, and market-hours behavior.",
            "Use WebSocket where available; REST polling must meet per-asset target cadence and rate limits.",
        ],
        "evidence_required": [
            "window start/end",
            "sample count",
            "tick interval stats",
            "latency stats",
            "age stats",
            "stale/gap flags",
        ],
        "acceptance_criteria": [
            "Feed meets target cadence for the asset class over the full test window.",
            "No unexplained stale or gap periods exceed quality thresholds.",
        ],
    },
    "manipulation_concentration": {
        "priority": "P0",
        "severity": "critical",
        "root_cause": "Thin pools, single routes, synthetic marks, or issuer-priced references can be manipulated or stale without obvious spread changes.",
        "target_state": "Each source has concentration, route diversity, outlier, stale-value, and cross-source deviation checks.",
        "mitigation_steps": [
            "Compute MAD/z-score outlier checks across venue observations and Blocksize/oracle/futures references.",
            "Measure pool liquidity concentration, largest LP/holder where possible, AMM route diversity, and single-venue dependency.",
            "Detect quote stuffing, wash-volume signatures, sudden route changes, and stale NAV/state references.",
            "Exclude or downweight sources that fail manipulation controls.",
        ],
        "evidence_required": [
            "outlier test result",
            "concentration metrics",
            "route diversity",
            "cross-source deviation bps",
            "exclusion/downweight decision",
        ],
        "acceptance_criteria": [
            "No source enters consensus without documented manipulation/concentration pass or explicit downweighting.",
        ],
    },
    "issuer_nav_alignment": {
        "priority": "P0",
        "severity": "critical",
        "root_cause": "Treasury and tokenized funds trade around NAV and redemption constraints; market quotes alone are not enough.",
        "target_state": "Treasury/tokenized fund prices reconcile to issuer NAV, administrator data, reserves, attestations, or redemption economics.",
        "mitigation_steps": [
            "Collect issuer NAV/admin references and timestamps for BUIDL/OUSG/USDY/TBILL/USTB/USCC-like assets.",
            "Compare market mid/VWAP to NAV and redemption value after fees, lockups, and settlement delays.",
            "Block feeds when NAV is stale, unavailable, or deviates beyond asset-class tolerance without explanation.",
        ],
        "evidence_required": [
            "issuer/admin NAV",
            "NAV timestamp",
            "attestation/reserve source",
            "fee/redemption adjustment",
            "market-vs-NAV drift bps",
        ],
        "acceptance_criteria": [
            "NAV/reference source is fresh and market/NAV drift is within tolerance or explicitly explained.",
        ],
    },
    "blocksize_benchmark_alignment": {
        "priority": "P0",
        "severity": "critical",
        "root_cause": "Replacement-grade feeds must be benchmarked against existing Blocksize data and accepted supplemental references.",
        "target_state": "Every comparable observation has drift, freshness, and alignment status versus Blocksize, oracle, NAV, or futures fair value.",
        "mitigation_steps": [
            "Run /v1/rwa/benchmark/blocksize for comparable bidask, fx, metal, vwap, and state references.",
            "Persist benchmark drift bps, pass/warn/exclude outcome, and raw normalized observation hash.",
            "Use Blocksize state only after state_instrument_confirmation passes.",
        ],
        "evidence_required": [
            "benchmark service",
            "benchmark symbol",
            "observed value",
            "reference value",
            "drift bps",
            "benchmark decision",
        ],
        "acceptance_criteria": [
            "Drift remains below warning/exclusion thresholds over the required freshness window.",
        ],
    },
    "rights_and_redistribution": {
        "priority": "P0",
        "severity": "critical",
        "root_cause": "Open API access does not imply production redistribution rights, especially for traditional assets.",
        "target_state": "Every provider, issuer, and venue has documented access tier, redistribution rights, attribution, and production limits.",
        "mitigation_steps": [
            "Map each provider through /v1/rwa/source-readiness and provider catalog legal fields.",
            "Attach commercial plan, license, ToS review, data retention limits, and redistribution status.",
            "Block production promotion until legal/commercial signoff is recorded.",
        ],
        "evidence_required": [
            "license status",
            "redistribution status",
            "allowed usage",
            "rate limits",
            "data retention rules",
            "signoff owner/date",
        ],
        "acceptance_criteria": [
            "Provider is cleared for the intended Blocksize product surface and storage/redistribution pattern.",
        ],
    },
    "replayable_payload": {
        "priority": "P0",
        "severity": "high",
        "root_cause": "Without raw payloads and request metadata, we cannot audit, replay, or debug feed quality decisions.",
        "target_state": "Every observation has raw payload hash, normalized payload hash, adapter version, request metadata, and storage pointer.",
        "mitigation_steps": [
            "Persist request parameters, response payload, source timestamp, receipt timestamp, adapter version, and hashes.",
            "Store normalized observations through /v1/rwa/observations/store.",
            "Retain enough payload detail to replay VWAP, bid/ask, quality, benchmark, and promotion decisions.",
        ],
        "evidence_required": [
            "raw payload hash",
            "normalized payload hash",
            "adapter version",
            "request metadata",
            "storage receipt id",
        ],
        "acceptance_criteria": [
            "A sampled observation can be re-run through quality, benchmark, and promotion checks deterministically.",
        ],
    },
}


VENUE_PLAYBOOKS: list[dict[str, Any]] = [
    {
        "venue_family": "jupiter_router",
        "primary_blockers": ["token_or_contract_discovery", "route_or_pool_discovery", "liquidity_depth_volume", "freshness_cadence"],
        "solution": "Run Solana mint discovery, then quote-sweep each mint pair with /swap/v1/quote across block sizes, recording routePlan, contextSlot, priceImpactPct, AMM labels, and route replay payloads.",
        "implementation": [
            "Use scripts/run_rwa_solana_discovery.py after JUPITER_API_KEY and SOLANA_RPC_URL are configured.",
            "Reject routes with no quote, excessive price impact, non-verified base token, or no replayable route plan.",
            "Run a 30-minute quote-sweep window for the route allowlist before promotion.",
        ],
        "research_source": "Jupiter Swap API",
    },
    {
        "venue_family": "hyperliquid",
        "primary_blockers": ["freshness_cadence", "liquidity_depth_volume", "blocksize_benchmark_alignment", "canonical_identity"],
        "solution": "Use spotMeta/index notation for identity, l2Book for depth, candles/trades for volume, and a timed window for latency and tick cadence.",
        "implementation": [
            "Verify spot pair index and UI remappings before treating the ticker as a canonical traditional asset.",
            "Capture l2Book at full precision, allMids, candles, and recent trades for each sourced RWA candidate.",
            "Benchmark PAXG/equity-like rows against Blocksize metal/bidask or licensed benchmarks where comparable.",
        ],
        "research_source": "Hyperliquid Info API",
    },
    {
        "venue_family": "evm_clmm_and_stableswap",
        "primary_blockers": ["token_or_contract_discovery", "route_or_pool_discovery", "liquidity_depth_volume", "manipulation_concentration"],
        "solution": "Resolve contracts, pool ids, fee tiers, balances, ticks, and quote simulations from onchain state and verified registries.",
        "implementation": [
            "For Uniswap CLMM pools, fetch token0/token1/fee/liquidity/slot0 in one block-consistent batch.",
            "Use QuoterV2 offchain for block-size simulations and persist initializedTicksCrossed/sqrtPriceX96After.",
            "For Curve/Balancer, capture balances/weights/amplification and replay swap math for block-size VWAP.",
        ],
        "research_source": "Uniswap v3 SDK quoting guide",
    },
    {
        "venue_family": "blocksize_state_reference",
        "primary_blockers": ["state_instrument_confirmation", "liquidity_depth_volume", "issuer_nav_alignment", "freshness_cadence"],
        "solution": "Keep as supplemental unless state_instruments and state_pool confirm the symbol; current live result is 0/7 matched.",
        "implementation": [
            "Run scripts/run_rwa_blocksize_state_discovery.py after Blocksize credentials are loaded.",
            "For unmatched state rows, keep them blocked and remove them from replacement-readiness counts.",
            "For matched rows, require state_pool freshness and issuer/NAV alignment before benchmark use.",
        ],
        "research_source": "Blocksize state_instruments/state_pool",
    },
    {
        "venue_family": "oracle_reference",
        "primary_blockers": ["blocksize_benchmark_alignment", "freshness_cadence", "rights_and_redistribution"],
        "solution": "Use Pyth/Chainlink as supplemental references with confidence/freshness metadata, not executable liquidity.",
        "implementation": [
            "For Pyth, use Hermes REST/streaming updates and respect public rate limits or use a production node provider.",
            "For Chainlink, use Data Streams REST/WebSocket/SDK and capture report schema, verification, and RWA fields.",
            "Never classify oracle-only prices as DEX/venue liquidity.",
        ],
        "research_source": "Pyth Hermes and Chainlink Data Streams",
    },
    {
        "venue_family": "issuer_nav_and_tokenized_funds",
        "primary_blockers": ["issuer_nav_alignment", "rights_and_redistribution", "manipulation_concentration"],
        "solution": "Attach issuer/admin NAV, reserve/attestation, redemption fees, restrictions, and stale-value rules before using fund-like feeds.",
        "implementation": [
            "Fetch issuer/admin NAV with timestamp and terms for each treasury/tokenized fund.",
            "Compare market VWAP/mid to NAV after fees, lockups, and redemption friction.",
            "Block promotion when NAV source is stale, inaccessible, or legally restricted.",
        ],
        "research_source": "Issuer/admin disclosures and provider contracts",
    },
]


EXECUTION_PHASES: list[dict[str, Any]] = [
    {
        "phase": "P0_hold_the_line",
        "status": "implemented",
        "objective": "Prevent candidate/supplemental coverage from being treated as production liquidity.",
        "actions": [
            "Keep /v1/rwa/discovery production_promoted at 0 until gates pass.",
            "Use reports/rwa_feed_discovery.json as the current promotion source of truth.",
        ],
    },
    {
        "phase": "P1_identifier_and_pool_discovery",
        "status": "ready_to_execute",
        "objective": "Resolve venue-native identifiers, token contracts/mints, routes, pools, and state instruments.",
        "actions": [
            "Run scripts/run_rwa_solana_discovery.py.",
            "Run scripts/run_rwa_blocksize_state_discovery.py.",
            "Create EVM pool discovery adapters for Uniswap, Curve, Balancer, and Aerodrome.",
        ],
    },
    {
        "phase": "P2_live_quality_windows",
        "status": "ready_after_P1",
        "objective": "Measure freshness, latency, tick frequency, spread, and block-size fillability over continuous windows.",
        "actions": [
            "Extend scripts/run_feed_quality_window.py to consume /v1/rwa/discovery ready rows.",
            "Run 30-minute windows per venue/source type with persistence enabled.",
            "Fail rows with stale, sparse, or high-latency behavior.",
        ],
    },
    {
        "phase": "P3_manipulation_and_consensus",
        "status": "ready_after_P2",
        "objective": "Run outlier, concentration, route diversity, and cross-source consensus checks.",
        "actions": [
            "Apply MAD and benchmark drift gates before consensus inclusion.",
            "Downweight thin or single-route sources.",
            "Persist exclusion reasons and raw payload hashes.",
        ],
    },
    {
        "phase": "P4_issuer_legal_and_nav",
        "status": "rights_cleared_nav_and_access_pending",
        "objective": "Maintain rights evidence and complete issuer/NAV alignment plus provider access for traditional/RWA redistribution.",
        "actions": [
            "Keep rights-clearance evidence attached to source-readiness and discovery reports.",
            "Acquire production data plans where required.",
            "Attach issuer NAV/admin/reserve evidence for tokenized funds.",
        ],
    },
    {
        "phase": "P5_promotion",
        "status": "blocked_until_all_required_gates_pass",
        "objective": "Promote only feeds with every required gate passed.",
        "actions": [
            "Run /v1/rwa/feeds/promotion-check.",
            "Run /v1/rwa/benchmark/blocksize.",
            "Store final promotion receipts.",
        ],
    },
]


def _affected_count(summary: dict[str, Any], gate_id: str) -> int:
    return int((summary.get("by_missing_gate") or {}).get(gate_id, 0))


def _required_count(summary: dict[str, Any], gate_id: str) -> int:
    return int((summary.get("by_required_gate") or {}).get(gate_id, 0))


def _build_issue_rows(audit: dict[str, Any]) -> list[dict[str, Any]]:
    summary = audit["summary"]
    rows: list[dict[str, Any]] = []
    for gate_id, plan in GATE_MITIGATIONS.items():
        affected = _affected_count(summary, gate_id)
        required = _required_count(summary, gate_id)
        rows.append(
            {
                "issue_id": gate_id,
                "priority": plan["priority"],
                "severity": plan["severity"],
                "required_feed_count": required,
                "affected_feed_count": affected,
                "clear_feed_count": max(0, required - affected),
                "current_status": "blocked" if affected else "clear_or_not_required",
                "root_cause": plan["root_cause"],
                "target_state": plan["target_state"],
                "mitigation_steps": plan["mitigation_steps"],
                "evidence_required": plan["evidence_required"],
                "acceptance_criteria": plan["acceptance_criteria"],
            }
        )
    return sorted(rows, key=lambda row: (row["priority"], -row["affected_feed_count"], row["issue_id"]))


def _immediate_actions(audit: dict[str, Any], readiness: dict[str, Any]) -> list[dict[str, Any]]:
    summary = audit["summary"]
    dependencies = {
        row["dependency_id"]: row
        for row in readiness.get("dependencies", [])
        if isinstance(row, dict) and row.get("dependency_id")
    }
    return [
        {
            "action_id": "refresh_blocksize_state_discovery",
            "priority": "P0",
            "status": "ready",
            "command": "source .env && source .venv/bin/activate && python3 scripts/run_rwa_blocksize_state_discovery.py",
            "unblocks": ["state_instrument_confirmation"],
            "current_blocked_feeds": _affected_count(summary, "state_instrument_confirmation"),
            "note": "The latest run should remain the authority for whether state rows are references or matched state instruments.",
        },
        {
            "action_id": "refresh_solana_token_and_route_discovery",
            "priority": "P0",
            "status": dependencies.get("jupiter_route_allowlist", {}).get("status", "unknown"),
            "command": "source .env && source .venv/bin/activate && python3 scripts/run_rwa_solana_discovery.py",
            "unblocks": ["token_or_contract_discovery", "route_or_pool_discovery", "replayable_payload"],
            "current_blocked_feeds": _affected_count(summary, "token_or_contract_discovery")
            + _affected_count(summary, "route_or_pool_discovery"),
            "note": "Requires Jupiter/Solana access and should record token verification, context slots, route labels, and quote payloads.",
        },
        {
            "action_id": "run_30_minute_quality_windows",
            "priority": "P0",
            "status": "ready_after_identifier_discovery",
            "command": "source .venv/bin/activate && python3 scripts/run_feed_quality_window.py --duration-seconds 1800",
            "unblocks": ["freshness_cadence", "liquidity_depth_volume", "blocksize_benchmark_alignment"],
            "current_blocked_feeds": _affected_count(summary, "freshness_cadence")
            + _affected_count(summary, "liquidity_depth_volume"),
            "note": "Extend the script feed list from the discovery report before running the full universe.",
        },
        {
            "action_id": "legal_and_nav_clearance",
            "priority": "P0",
            "status": "rights_cleared_nav_and_access_pending",
            "command": "GET /v1/rwa/source-readiness and update provider access/NAV evidence records",
            "unblocks": ["rights_and_redistribution", "issuer_nav_alignment"],
            "current_blocked_feeds": _affected_count(summary, "rights_and_redistribution")
            + _affected_count(summary, "issuer_nav_alignment"),
            "note": "Rights are recorded separately; this action now tracks issuer NAV, reserve, entitlement metadata, and provider access evidence.",
        },
        {
            "action_id": "manipulation_outlier_controls",
            "priority": "P0",
            "status": "ready_after_live_payloads",
            "command": "POST /v1/rwa/quality/check and POST /v1/rwa/consensus/calculate for replayed windows",
            "unblocks": ["manipulation_concentration"],
            "current_blocked_feeds": _affected_count(summary, "manipulation_concentration"),
            "note": "Requires multi-source observations and pool/route concentration evidence.",
        },
    ]


def build_discovery_mitigation_plan(
    *,
    exclude_tokenized_stocks: bool = False,
    reports_dir: str | Path = DEFAULT_REPORTS_DIR,
) -> dict[str, Any]:
    """Build a research-backed mitigation plan for all current discovery blockers."""
    audit = build_feed_discovery_audit(
        exclude_tokenized_stocks=exclude_tokenized_stocks,
        include_feed_details=False,
        reports_dir=reports_dir,
    )
    readiness = build_source_readiness()
    issues = _build_issue_rows(audit)
    immediate = _immediate_actions(audit, readiness)
    summary = audit["summary"]
    return {
        "summary": {
            "feed_count": summary["feed_count"],
            "production_promoted": summary["production_promoted"],
            "blocked_from_production": summary["blocked_from_production"],
            "open_issue_count": sum(1 for row in issues if row["affected_feed_count"] > 0),
            "critical_open_issue_count": sum(
                1 for row in issues if row["severity"] == "critical" and row["affected_feed_count"] > 0
            ),
            "ready_to_execute_action_count": sum(
                1 for row in immediate if str(row["status"]).startswith(("ready", "configured"))
            ),
            "by_missing_gate": summary.get("by_missing_gate", {}),
            "by_status": summary.get("by_status", {}),
            "by_venue": summary.get("by_venue", {}),
        },
        "policy": {
            "promotion_rule": "A feed is production-ready only when every required discovery gate is passed, not merely evidenced.",
            "oracle_rule": "Oracle, NAV, and state rows can improve consensus but are not executable liquidity.",
            "legal_rule": "No traditional/RWA feed may be redistributed until rights and usage are cleared.",
        },
        "research_basis": RESEARCH_BASIS,
        "issues": issues,
        "venue_playbooks": VENUE_PLAYBOOKS,
        "execution_phases": EXECUTION_PHASES,
        "immediate_actions": immediate,
    }


def write_discovery_mitigation_reports(
    *,
    json_path: str | Path = DEFAULT_MITIGATION_JSON_PATH,
    csv_path: str | Path = DEFAULT_MITIGATION_CSV_PATH,
    exclude_tokenized_stocks: bool = False,
    reports_dir: str | Path = DEFAULT_REPORTS_DIR,
) -> dict[str, Any]:
    """Write JSON and CSV mitigation reports."""
    plan = build_discovery_mitigation_plan(
        exclude_tokenized_stocks=exclude_tokenized_stocks,
        reports_dir=reports_dir,
    )
    json_out = Path(json_path)
    csv_out = Path(csv_path)
    json_out.parent.mkdir(parents=True, exist_ok=True)
    csv_out.parent.mkdir(parents=True, exist_ok=True)
    json_out.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    fieldnames = [
        "issue_id",
        "priority",
        "severity",
        "required_feed_count",
        "affected_feed_count",
        "clear_feed_count",
        "current_status",
        "root_cause",
        "target_state",
        "mitigation_steps",
        "evidence_required",
        "acceptance_criteria",
    ]
    with csv_out.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in plan["issues"]:
            writer.writerow(
                {
                    key: json.dumps(row[key]) if isinstance(row.get(key), (list, dict)) else row.get(key)
                    for key in fieldnames
                }
            )
    return plan
