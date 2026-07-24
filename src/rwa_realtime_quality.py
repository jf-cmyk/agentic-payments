"""Real-time quality gates for RWA market-data observations."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from src.rwa_coverage import QUALITY_ALIGNMENT
from src.rwa_derivative_venues import DERIVATIVE_VENUE_CONFIGS
from src.rwa_xyz_monitor import RWA_XYZ_VENUE_ID


VENUE_REALTIME_PROFILES: dict[str, dict[str, Any]] = {
    "kraken_xstocks": {
        "mode": "event_driven_exchange_ws",
        "target_tick_ms": 1_000,
        "max_tick_gap_ms": 10_000,
        "max_age_ms": 10_000,
        "quality_note": "Native exchange book/trade/ticker streams; gaps may reflect quiet markets but freshness must stay below the asset threshold.",
    },
    "ostium": {
        "mode": "synthetic_price_stream",
        "target_tick_ms": 1_000,
        "max_tick_gap_ms": 15_000,
        "max_age_ms": 15_000,
        "quality_note": "Synthetic bid/mid/ask and simulated-depth source; keep source_type visible.",
    },
    "gains": {
        "mode": "high_frequency_mark_stream",
        "target_tick_ms": 25,
        "max_tick_gap_ms": 500,
        "max_age_ms": 1_000,
        "quality_note": "Fast mark stream; no native book, so cadence can be real-time while depth quality remains limited.",
    },
    "jupiter_xstocks": {
        "mode": "quote_sweep",
        "target_tick_ms": 2_000,
        "max_tick_gap_ms": 10_000,
        "max_age_ms": 10_000,
        "quality_note": "Quote-derived route snapshots; quality depends on requested size and route plan.",
    },
    "jupiter_router": {
        "mode": "dex_quote_router",
        "target_tick_ms": 2_000,
        "max_tick_gap_ms": 10_000,
        "max_age_ms": 10_000,
        "quality_note": "Executable route quote; require route plan, context slot, price impact, and token allowlist checks.",
    },
    "raydium_clmm": {
        "mode": "onchain_pool_state",
        "target_tick_ms": 1_000,
        "max_tick_gap_ms": 10_000,
        "max_age_ms": 10_000,
        "quality_note": "Use SDK/gRPC/RPC pool state for real-time monitoring; REST data alone is not tick-by-tick.",
    },
    "orca_whirlpool": {
        "mode": "onchain_pool_state",
        "target_tick_ms": 1_000,
        "max_tick_gap_ms": 10_000,
        "max_age_ms": 10_000,
        "quality_note": "Whirlpool tick/liquidity state can support pool-implied quotes after slot freshness checks.",
    },
    "meteora_dlmm": {
        "mode": "onchain_pool_state",
        "target_tick_ms": 1_000,
        "max_tick_gap_ms": 10_000,
        "max_age_ms": 10_000,
        "quality_note": "DLMM bin and dynamic-fee state can support pool-implied quotes after allowlist and freshness checks.",
    },
    "uniswap_v3_v4": {
        "mode": "indexed_pool_plus_rpc",
        "target_tick_ms": 12_000,
        "max_tick_gap_ms": 36_000,
        "max_age_ms": 36_000,
        "quality_note": "Subgraphs are indexers; production real-time use needs RPC block checks or managed indexing.",
    },
    "curve_stableswap": {
        "mode": "onchain_stableswap_pool",
        "target_tick_ms": 12_000,
        "max_tick_gap_ms": 36_000,
        "max_age_ms": 36_000,
        "quality_note": "Stable/NAV pool source; require balance/imbalance and stale-block checks.",
    },
    "balancer_pools": {
        "mode": "onchain_weighted_or_stable_pool",
        "target_tick_ms": 12_000,
        "max_tick_gap_ms": 36_000,
        "max_age_ms": 36_000,
        "quality_note": "Weighted/stable pool source; require balance, weight, and stale-block checks.",
    },
    "aerodrome_slipstream": {
        "mode": "base_onchain_pool_state",
        "target_tick_ms": 2_000,
        "max_tick_gap_ms": 12_000,
        "max_age_ms": 12_000,
        "quality_note": "Base CLMM/route source for tokenized funds and stables; keep supplemental until benchmarked.",
    },
    "bybit_xstocks": {
        "mode": "event_driven_exchange_ws",
        "target_tick_ms": 1_000,
        "max_tick_gap_ms": 10_000,
        "max_age_ms": 10_000,
        "quality_note": "Native exchange stream once regional/API access is confirmed.",
    },
    "ondo_stocks": {
        "mode": "api_keyed_quote_stream",
        "target_tick_ms": 1_000,
        "max_tick_gap_ms": 10_000,
        "max_age_ms": 10_000,
        "quality_note": "Quote or stream source after whitelist/API access; not a native lit order book.",
    },
    "hyperliquid_paxg": {
        "mode": "event_driven_perp_ws",
        "target_tick_ms": 1_000,
        "max_tick_gap_ms": 5_000,
        "max_age_ms": 10_000,
        "quality_note": "Native perp L2/market stream for PAXG only in current RWA scope.",
    },
    "hyperliquid_rwa_spot": {
        "mode": "event_driven_spot_book",
        "target_tick_ms": 1_000,
        "max_tick_gap_ms": 5_000,
        "max_age_ms": 10_000,
        "quality_note": "Native spot L2 source for tokenized RWA/traditional candidates; promotion still requires identity, liquidity, and benchmark validation.",
    },
    "treasury_nav": {
        "mode": "nav_reference",
        "target_tick_ms": 86_400_000,
        "max_tick_gap_ms": 86_400_000,
        "max_age_ms": 86_400_000,
        "quality_note": "Reference/NAV source, not tick-by-tick market data.",
    },
    "backed_xstocks_issuer": {
        "mode": "issuer_reference",
        "target_tick_ms": 86_400_000,
        "max_tick_gap_ms": 86_400_000,
        "max_age_ms": 86_400_000,
        "quality_note": "Issuer/product metadata and attestation; not tick-by-tick market data.",
    },
    "polygon_tradfi_reference": {
        "mode": "benchmark_reference",
        "target_tick_ms": 1_000,
        "max_tick_gap_ms": 10_000,
        "max_age_ms": 10_000,
        "quality_note": "Licensed benchmark/reference feed for quality alignment and corporate-action context.",
    },
    "blocksize_state": {
        "mode": "pool_state_reference",
        "target_tick_ms": 1_000,
        "max_tick_gap_ms": 10_000,
        "max_age_ms": 10_000,
        "quality_note": "Blocksize state_subscribe or state_pool-derived reference price; supplemental only and not executable liquidity.",
    },
    "us_equity_consolidated_tape": {
        "mode": "licensed_consolidated_equity_feed",
        "target_tick_ms": 1_000,
        "max_tick_gap_ms": 10_000,
        "max_age_ms": 10_000,
        "quality_note": "Licensed U.S. consolidated/direct feed; require NBBO/trade timestamps, condition codes, and exchange session calendars.",
    },
    "hkex_licensed_equities": {
        "mode": "licensed_exchange_feed",
        "target_tick_ms": 1_000,
        "max_tick_gap_ms": 10_000,
        "max_age_ms": 10_000,
        "quality_note": "Licensed HKEX or vendor feed; require exchange timestamps, board-lot handling, auction/session state, and holiday calendars.",
    },
    "china_a_share_licensed_equities": {
        "mode": "licensed_exchange_feed",
        "target_tick_ms": 1_000,
        "max_tick_gap_ms": 10_000,
        "max_age_ms": 10_000,
        "quality_note": "Licensed SSE/SZSE/China Connect vendor feed; require session state, price-limit handling, and mainland holiday calendars.",
    },
    "krx_licensed_equities": {
        "mode": "licensed_exchange_feed",
        "target_tick_ms": 1_000,
        "max_tick_gap_ms": 10_000,
        "max_age_ms": 10_000,
        "quality_note": "Licensed KRX or vendor feed; require KOSPI/KOSDAQ session calendars, price-limit handling, and exchange timestamps.",
    },
    "jpx_licensed_equities": {
        "mode": "licensed_exchange_feed",
        "target_tick_ms": 1_000,
        "max_tick_gap_ms": 10_000,
        "max_age_ms": 10_000,
        "quality_note": "Licensed JPX/TSE or vendor feed; require local security-code mapping, session calendars, and exchange timestamps.",
    },
    "twse_licensed_equities": {
        "mode": "licensed_exchange_feed",
        "target_tick_ms": 1_000,
        "max_tick_gap_ms": 10_000,
        "max_age_ms": 10_000,
        "quality_note": "Licensed TWSE/TPEx or vendor feed; require exchange timestamps, price-limit handling, and local holiday calendars.",
    },
    "india_nse_bse_licensed_equities": {
        "mode": "licensed_exchange_feed",
        "target_tick_ms": 1_000,
        "max_tick_gap_ms": 10_000,
        "max_age_ms": 10_000,
        "quality_note": "Licensed NSE/BSE or vendor feed; require series/security master mapping, session calendars, and exchange timestamps.",
    },
    "lse_lseg_licensed_equities": {
        "mode": "licensed_exchange_feed",
        "target_tick_ms": 1_000,
        "max_tick_gap_ms": 10_000,
        "max_age_ms": 10_000,
        "quality_note": "Licensed LSE/LSEG or vendor feed; require GBX/GBP price scaling, auction state, and exchange timestamps.",
    },
    "euronext_licensed_equities": {
        "mode": "licensed_exchange_feed",
        "target_tick_ms": 1_000,
        "max_tick_gap_ms": 10_000,
        "max_age_ms": 10_000,
        "quality_note": "Licensed Euronext or vendor feed; require MIC/currency handling, local sessions, and exchange timestamps.",
    },
    "deutsche_boerse_xetra_licensed_equities": {
        "mode": "licensed_exchange_feed",
        "target_tick_ms": 1_000,
        "max_tick_gap_ms": 10_000,
        "max_age_ms": 10_000,
        "quality_note": "Licensed Deutsche Boerse/Xetra or vendor feed; require Xetra session calendars, auction state, and exchange timestamps.",
    },
    "tsx_licensed_equities": {
        "mode": "licensed_exchange_feed",
        "target_tick_ms": 1_000,
        "max_tick_gap_ms": 10_000,
        "max_age_ms": 10_000,
        "quality_note": "Licensed TSX/TSXV/TMX or vendor feed; require interlisted symbol mapping, currency handling, and exchange timestamps.",
    },
    "asx_licensed_equities": {
        "mode": "licensed_exchange_feed",
        "target_tick_ms": 1_000,
        "max_tick_gap_ms": 10_000,
        "max_age_ms": 10_000,
        "quality_note": "Licensed ASX or vendor feed; require local security master, session calendars, and exchange timestamps.",
    },
    "sgx_licensed_equities": {
        "mode": "licensed_exchange_feed",
        "target_tick_ms": 1_000,
        "max_tick_gap_ms": 10_000,
        "max_age_ms": 10_000,
        "quality_note": "Licensed SGX or vendor feed; require local security master, session calendars, and exchange timestamps.",
    },
    "pyth_oracle_reference": {
        "mode": "oracle_reference",
        "target_tick_ms": 10_000,
        "max_tick_gap_ms": 10_000,
        "max_age_ms": 10_000,
        "quality_note": "Oracle price/confidence reference for parity checks; not a venue book.",
    },
    "chainlink_oracle_reference": {
        "mode": "oracle_reference",
        "target_tick_ms": 10_000,
        "max_tick_gap_ms": 10_000,
        "max_age_ms": 10_000,
        "quality_note": "Oracle feed answer plus heartbeat/deviation metadata for parity checks; not a venue book.",
    },
    RWA_XYZ_VENUE_ID: {
        "mode": "platform_catalog_reference",
        "target_tick_ms": 86_400_000,
        "max_tick_gap_ms": 86_400_000,
        "max_age_ms": 86_400_000,
        "quality_note": "RWA.xyz monitor rows are product/token catalog evidence, not tick-by-tick executable market data; real-time prices must come from discovered pools, routes, issuer quote streams, or order books.",
    },
}

for _derivative_config in DERIVATIVE_VENUE_CONFIGS:
    _venue_id = str(_derivative_config["venue_id"])
    if _venue_id in VENUE_REALTIME_PROFILES:
        continue
    _source_tier = str(_derivative_config["source_tier"])
    if _source_tier == "onchain_yield_market":
        VENUE_REALTIME_PROFILES[_venue_id] = {
            "mode": "onchain_yield_market_state",
            "target_tick_ms": 12_000,
            "max_tick_gap_ms": 36_000,
            "max_age_ms": 36_000,
            "quality_note": "Yield-token market state is supplemental until pool/account state, implied APY, liquidity, and replay checks pass.",
        }
    elif _derivative_config["instrument_type"] in {"chain_ecosystem", "not_market_operator"}:
        VENUE_REALTIME_PROFILES[_venue_id] = {
            "mode": "ecosystem_or_non_venue_reference",
            "target_tick_ms": 86_400_000,
            "max_tick_gap_ms": 86_400_000,
            "max_age_ms": 86_400_000,
            "quality_note": "Not a standalone market-data venue; source concrete markets, pools, protocols, or licensed feeds instead.",
        }
    elif bool(_derivative_config.get("requires_auth")):
        VENUE_REALTIME_PROFILES[_venue_id] = {
            "mode": "rpc_or_partner_indexer_required",
            "target_tick_ms": 1_000,
            "max_tick_gap_ms": 10_000,
            "max_age_ms": 10_000,
            "quality_note": "Real-time use requires configured venue/API/RPC access, replayable payloads, and basis/funding context.",
        }
    else:
        VENUE_REALTIME_PROFILES[_venue_id] = {
            "mode": "event_driven_derivative_book",
            "target_tick_ms": 1_000,
            "max_tick_gap_ms": 5_000,
            "max_age_ms": 10_000,
            "quality_note": "Native derivative venue book; use as derivative liquidity and derive spot/fair value only after funding/basis adjustment.",
        }


def _parse_timestamp(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    if isinstance(value, (int, float)):
        raw = float(value)
        if raw > 10_000_000_000:
            raw = raw / 1000
        return datetime.fromtimestamp(raw, tz=UTC)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    raise ValueError("timestamp must be ISO-8601, unix seconds, unix milliseconds, or omitted")


def _age_ms(timestamp_value: Any, *, now: datetime) -> int | None:
    timestamp = _parse_timestamp(timestamp_value)
    if timestamp is None:
        return None
    return max(0, int((now - timestamp).total_seconds() * 1000))


def _asset_max_age_ms(asset_class: str) -> int:
    thresholds = QUALITY_ALIGNMENT["thresholds"]["max_age_ms"]
    normalized = asset_class.strip().lower()
    if normalized == "equity":
        return int(thresholds["tokenized_equity"])
    if normalized == "etf":
        return int(thresholds["tokenized_etf"])
    if normalized == "index":
        return int(thresholds["synthetic_etf_index"])
    if normalized in {"commodity", "metal"}:
        return int(thresholds["metal_commodity"])
    if normalized in {"treasury", "treasury_fund", "tokenized_fund"}:
        return int(thresholds["treasury_nav"])
    return int(thresholds.get(normalized, 60_000))


def _tick_interval_ms(row: dict[str, Any]) -> int | None:
    explicit = row.get("tick_interval_ms")
    if explicit is not None:
        return max(0, int(float(explicit)))
    timestamp = _parse_timestamp(row.get("timestamp"))
    previous = _parse_timestamp(row.get("previous_timestamp"))
    if timestamp is None or previous is None:
        return None
    return max(0, int((timestamp - previous).total_seconds() * 1000))


def build_realtime_requirements() -> dict[str, Any]:
    """Return real-time freshness and cadence requirements."""
    return {
        "venue_profiles": VENUE_REALTIME_PROFILES,
        "asset_freshness_ms": {
            "equity": _asset_max_age_ms("equity"),
            "etf": _asset_max_age_ms("etf"),
            "index": _asset_max_age_ms("index"),
            "fx": _asset_max_age_ms("fx"),
            "commodity": _asset_max_age_ms("commodity"),
            "metal": _asset_max_age_ms("metal"),
            "treasury": _asset_max_age_ms("treasury"),
            "treasury_fund": _asset_max_age_ms("treasury_fund"),
            "tokenized_fund": _asset_max_age_ms("tokenized_fund"),
        },
        "requirements": [
            "Every real-time observation must include a venue timestamp.",
            "Every streaming adapter should record previous_timestamp or tick_interval_ms for cadence checks.",
            "A venue-symbol is not usable for real-time consolidation when timestamp freshness or tick cadence fails.",
            "NAV/reference feeds are valid only in reference mode and must not be labeled tick-by-tick.",
        ],
    }


def evaluate_realtime_quality(payload: dict[str, Any]) -> dict[str, Any]:
    """Evaluate live freshness and cadence for normalized observations."""
    observations = payload.get("observations")
    if not isinstance(observations, list) or not observations:
        raise ValueError("observations must include at least one row")
    now = _parse_timestamp(payload.get("now")) or datetime.now(UTC)

    rows: list[dict[str, Any]] = []
    for raw in observations:
        if not isinstance(raw, dict):
            continue
        venue = str(raw.get("venue") or "unknown").strip().lower()
        symbol = str(raw.get("symbol") or payload.get("symbol") or "").strip().upper()
        asset_class = str(raw.get("asset_class") or payload.get("asset_class") or "equity").strip().lower()
        profile = VENUE_REALTIME_PROFILES.get(
            venue,
            {
                "mode": "unknown",
                "target_tick_ms": _asset_max_age_ms(asset_class),
                "max_tick_gap_ms": _asset_max_age_ms(asset_class),
                "max_age_ms": _asset_max_age_ms(asset_class),
                "quality_note": "Unknown venue profile; using asset-class freshness only.",
            },
        )
        age = _age_ms(raw.get("timestamp"), now=now)
        interval = _tick_interval_ms(raw)
        asset_max_age = _asset_max_age_ms(asset_class)
        venue_max_age = int(profile["max_age_ms"])
        max_age = min(asset_max_age, venue_max_age)
        max_tick_gap = int(profile["max_tick_gap_ms"])

        flags: list[str] = []
        score = 100
        if age is None:
            flags.append("missing_timestamp")
            score -= 50
        elif age > max_age:
            flags.append("stale")
            score -= 60 if age > max_age * 3 else 35
        elif age > max_age * 0.5:
            flags.append("freshness_warning")
            score -= 10

        if interval is None:
            flags.append("cadence_unmeasured")
            score -= 10
        elif interval > max_tick_gap:
            flags.append("tick_gap_exceeded")
            score -= 45
        elif interval > int(profile["target_tick_ms"]) * 3:
            flags.append("tick_gap_warning")
            score -= 10

        if profile["mode"] in {"nav_reference", "issuer_reference", "platform_catalog_reference"}:
            flags.append("reference_mode_not_tick_by_tick")

        usable = not {"missing_timestamp", "stale", "tick_gap_exceeded", "reference_mode_not_tick_by_tick"}.intersection(flags)
        rows.append(
            {
                "symbol": symbol,
                "venue": venue,
                "asset_class": asset_class,
                "source_type": str(raw.get("source_type") or "unknown"),
                "mode": profile["mode"],
                "age_ms": age,
                "max_age_ms": max_age,
                "tick_interval_ms": interval,
                "target_tick_ms": int(profile["target_tick_ms"]),
                "max_tick_gap_ms": max_tick_gap,
                "usable_for_realtime": usable,
                "status": "live" if usable else "not_realtime_usable",
                "quality": {
                    "score": max(0, min(100, score)),
                    "flags": flags,
                },
            }
        )

    if not rows:
        raise ValueError("observations must include object rows")
    usable_count = sum(1 for row in rows if row["usable_for_realtime"])
    if usable_count == len(rows):
        aggregate_status = "live"
    elif usable_count:
        aggregate_status = "degraded"
    else:
        aggregate_status = "not_realtime_usable"
    return {
        "as_of": now.isoformat(),
        "aggregate_status": aggregate_status,
        "usable_observations": usable_count,
        "total_observations": len(rows),
        "observations": rows,
    }
