"""Consensus metrics for RWA/traditional market-data feeds.

The consensus layer sits above venue adapters and oracle/reference sources. It
does not make oracle feeds look like executable liquidity; instead it turns all
eligible evidence into a transparent, weighted quality receipt.
"""

from __future__ import annotations

import csv
import json
import math
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from typing import Any

from src.rwa_coverage import (
    QUALITY_ALIGNMENT,
    build_rwa_asset_matrix,
    iter_asset_venue_instruments,
)
from src.rwa_blocksize_benchmark import build_blocksize_state_methodology
from src.rwa_dex_allowlist import build_dex_allowlist
from src.rwa_market_expansion import build_futures_data_plan, build_market_expansion_plan
from src.rwa_non_crypto_feeds import build_non_crypto_feed_catalog
from src.rwa_oracle_streams import build_oracle_stream_coverage
from src.rwa_pricing import calculate_perp_basis_guard
from src.rwa_provider_catalog import build_provider_catalog
from src.rwa_realtime_quality import VENUE_REALTIME_PROFILES
from src.rwa_sourcing import build_sourcing_jobs
from src.rwa_source_readiness import build_source_readiness


PRIMARY_MARKET_FAMILIES = {
    "exchange_book",
    "licensed_exchange",
    "synthetic_market",
    "dex_liquidity",
    "benchmark_reference",
    "futures_fair_value",
}

DERIVATIVE_SOURCE_TYPES = {
    "futures_fair_value",
    "native_derivative_l2",
    "perp_l2",
    "perp_mark",
    "price_stream_no_book",
    "synthetic_depth",
    "synthetic_l1",
}

CONSENSUS_SOURCE_POLICIES: dict[str, dict[str, Any]] = {
    "native_l2": {
        "family": "exchange_book",
        "base_weight": 1.0,
        "real_time_eligible": True,
        "supplemental_only": False,
    },
    "native_l1": {
        "family": "exchange_book",
        "base_weight": 0.9,
        "real_time_eligible": True,
        "supplemental_only": False,
    },
    "licensed_consolidated_tape": {
        "family": "licensed_exchange",
        "base_weight": 1.15,
        "real_time_eligible": True,
        "supplemental_only": False,
    },
    "licensed_exchange_feed": {
        "family": "licensed_exchange",
        "base_weight": 1.1,
        "real_time_eligible": True,
        "supplemental_only": False,
    },
    "synthetic_depth": {
        "family": "synthetic_market",
        "base_weight": 0.75,
        "real_time_eligible": True,
        "supplemental_only": False,
    },
    "synthetic_l1": {
        "family": "synthetic_market",
        "base_weight": 0.65,
        "real_time_eligible": True,
        "supplemental_only": False,
    },
    "price_stream_no_book": {
        "family": "synthetic_market",
        "base_weight": 0.5,
        "real_time_eligible": True,
        "supplemental_only": False,
    },
    "quote_sweep": {
        "family": "dex_liquidity",
        "base_weight": 0.72,
        "real_time_eligible": True,
        "supplemental_only": False,
    },
    "quote_stream": {
        "family": "dex_liquidity",
        "base_weight": 0.68,
        "real_time_eligible": True,
        "supplemental_only": False,
    },
    "onchain_clmm_pool": {
        "family": "dex_liquidity",
        "base_weight": 0.68,
        "real_time_eligible": True,
        "supplemental_only": False,
    },
    "onchain_stableswap_pool": {
        "family": "dex_liquidity",
        "base_weight": 0.72,
        "real_time_eligible": True,
        "supplemental_only": False,
    },
    "oracle_reference": {
        "family": "oracle_reference",
        "base_weight": 0.62,
        "real_time_eligible": True,
        "supplemental_only": True,
    },
    "benchmark_reference": {
        "family": "benchmark_reference",
        "base_weight": 0.86,
        "real_time_eligible": True,
        "supplemental_only": True,
    },
    "blocksize_benchmark": {
        "family": "benchmark_reference",
        "base_weight": 0.9,
        "real_time_eligible": True,
        "supplemental_only": True,
    },
    "blocksize_state_reference": {
        "family": "benchmark_reference",
        "base_weight": 0.82,
        "real_time_eligible": True,
        "supplemental_only": True,
    },
    "futures_fair_value": {
        "family": "futures_fair_value",
        "base_weight": 0.7,
        "real_time_eligible": True,
        "supplemental_only": True,
    },
    "nav_reference": {
        "family": "nav_reference",
        "base_weight": 0.45,
        "real_time_eligible": False,
        "supplemental_only": True,
    },
    "issuer_reference": {
        "family": "issuer_reference",
        "base_weight": 0.3,
        "real_time_eligible": False,
        "supplemental_only": True,
    },
    "platform_catalog_reference": {
        "family": "issuer_reference",
        "base_weight": 0.2,
        "real_time_eligible": False,
        "supplemental_only": True,
    },
    "proof_of_reserve": {
        "family": "proof_of_reserve",
        "base_weight": 0.35,
        "real_time_eligible": False,
        "supplemental_only": True,
    },
}


def _parse_timestamp(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    if isinstance(value, (int, float)):
        raw = float(value)
        if raw > 10_000_000_000_000:
            raw /= 1_000_000
        elif raw > 10_000_000_000:
            raw /= 1_000
        return datetime.fromtimestamp(raw, tz=UTC)
    if isinstance(value, str):
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    raise ValueError("timestamp must be ISO-8601, unix seconds, unix milliseconds, or omitted")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _age_ms(timestamp_value: Any, *, now: datetime) -> int | None:
    timestamp = _parse_timestamp(timestamp_value)
    if timestamp is None:
        return None
    return max(0, int((now - timestamp).total_seconds() * 1000))


def _as_float(value: Any, field_name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
    if not math.isfinite(result):
        raise ValueError(f"{field_name} must be finite")
    return result


def _positive_float(value: Any, field_name: str) -> float:
    result = _as_float(value, field_name)
    if result <= 0:
        raise ValueError(f"{field_name} must be greater than zero")
    return result


def _observation_value(row: dict[str, Any]) -> float:
    for key in ("value", "consensus_value", "vwap", "mid", "price", "last", "mark", "nav", "fair_value"):
        if row.get(key) is not None:
            return _positive_float(row[key], f"observation.{key}")
    if row.get("bid") is not None and row.get("ask") is not None:
        bid = _positive_float(row["bid"], "observation.bid")
        ask = _positive_float(row["ask"], "observation.ask")
        if ask < bid:
            raise ValueError("observation.ask must be greater than or equal to observation.bid")
        return (bid + ask) / 2
    raise ValueError("observation must include value, vwap, mid, price, nav, fair_value, or bid/ask")


def _spread_bps(row: dict[str, Any], value: float) -> float | None:
    if row.get("spread_bps") is not None:
        return abs(_as_float(row["spread_bps"], "observation.spread_bps"))
    if row.get("bid") is not None and row.get("ask") is not None:
        bid = _positive_float(row["bid"], "observation.bid")
        ask = _positive_float(row["ask"], "observation.ask")
        return abs(ask - bid) / value * 10_000
    return None


def _confidence_bps(row: dict[str, Any], value: float) -> float | None:
    if row.get("confidence_bps") is not None:
        return abs(_as_float(row["confidence_bps"], "observation.confidence_bps"))
    for key in ("confidence", "confidence_interval", "confidence_abs"):
        if row.get(key) is not None:
            return abs(_as_float(row[key], f"observation.{key}")) / value * 10_000
    return None


def _asset_threshold(asset_class: str, key: str, default: float) -> float:
    thresholds = QUALITY_ALIGNMENT["thresholds"].get(key, {})
    normalized = asset_class.strip().lower()
    if normalized in thresholds:
        return float(thresholds[normalized])
    if normalized == "equity":
        return float(thresholds.get("tokenized_equity", default))
    if normalized == "etf":
        return float(thresholds.get("tokenized_etf", default))
    if normalized == "index":
        return float(thresholds.get("synthetic_etf_index", default))
    if normalized in {"commodity", "metal"}:
        return float(thresholds.get("metal_commodity", default))
    if normalized in {"treasury", "treasury_fund", "tokenized_fund"}:
        return float(thresholds.get("treasury_nav", default))
    return default


def _source_policy(source_type: str) -> dict[str, Any]:
    return CONSENSUS_SOURCE_POLICIES.get(
        source_type,
        {
            "family": "unknown",
            "base_weight": 0.35,
            "real_time_eligible": True,
            "supplemental_only": True,
        },
    )


def _max_age_ms(asset_class: str, venue: str, source_type: str) -> int:
    policy = _source_policy(source_type)
    if not policy["real_time_eligible"]:
        if source_type in {"nav_reference", "proof_of_reserve", "issuer_reference"}:
            return int(_asset_threshold(asset_class, "max_age_ms", 86_400_000))
        return 86_400_000
    profile = VENUE_REALTIME_PROFILES.get(venue)
    asset_limit = int(_asset_threshold(asset_class, "max_age_ms", 60_000))
    if profile:
        return min(asset_limit, int(profile["max_age_ms"]))
    if source_type == "futures_fair_value":
        return min(asset_limit, 10_000)
    if source_type in {
        "oracle_reference",
        "benchmark_reference",
        "blocksize_benchmark",
        "blocksize_state_reference",
    }:
        return min(asset_limit, 10_000)
    return asset_limit


def _independence_key(row: dict[str, Any], venue: str) -> str:
    provider = str(row.get("provider") or row.get("provider_id") or "").strip().lower()
    if provider:
        return provider
    feed_id = str(row.get("feed_id") or "").strip().lower()
    if feed_id and venue == "unknown":
        return feed_id
    return venue


def _quality_input_score(row: dict[str, Any]) -> int | None:
    quality = row.get("quality")
    if isinstance(quality, dict) and quality.get("score") is not None:
        return max(0, min(100, round(_as_float(quality["score"], "observation.quality.score"))))
    return None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "pass", "passed"}
    return bool(value)


def _is_derivative_observation(row: dict[str, Any], source_type: str) -> bool:
    text = " ".join(
        str(row.get(key) or "")
        for key in (
            "market_type",
            "instrument_type",
            "derivative_type",
            "price_source_type",
            "kind",
            "feed_id",
            "venue",
        )
    ).lower()
    if source_type in DERIVATIVE_SOURCE_TYPES:
        return True
    return any(marker in text for marker in ("perp", "future", "futures", "synthetic_perp"))


def _weighted_mean(rows: list[dict[str, Any]]) -> float | None:
    total_weight = sum(float(row["adjusted_weight"]) for row in rows)
    if total_weight <= 0:
        return None
    return sum(float(row["value"]) * float(row["adjusted_weight"]) for row in rows) / total_weight


def _weighted_stat(values: list[tuple[float, float]]) -> float | None:
    total_weight = sum(weight for _, weight in values)
    if total_weight <= 0:
        return None
    return sum(value * weight for value, weight in values) / total_weight


def _known_oracle_providers_for_asset_class(asset_class: str, oracle: dict[str, Any]) -> list[str]:
    normalized = asset_class.lower()
    provider_ids = []
    for provider in oracle["providers"]:
        buckets = set(provider.get("rwa_buckets") or [])
        if normalized in buckets:
            provider_ids.append(str(provider["provider_id"]))
            continue
        if normalized == "commodity" and {"commodity", "metal"}.intersection(buckets):
            provider_ids.append(str(provider["provider_id"]))
        elif normalized == "treasury_fund" and {"treasury_fund", "fund", "nav"}.intersection(buckets):
            provider_ids.append(str(provider["provider_id"]))
        elif normalized == "etf" and {"etf", "fund"}.intersection(buckets):
            provider_ids.append(str(provider["provider_id"]))
    return sorted(set(provider_ids))


def calculate_consensus_metric(payload: dict[str, Any]) -> dict[str, Any]:
    """Calculate a weighted, quality-gated consensus metric from submitted evidence."""
    symbol = str(payload.get("symbol") or "").strip().upper()
    if not symbol:
        raise ValueError("symbol is required")
    asset_class = str(payload.get("asset_class") or "equity").strip().lower()
    observations = payload.get("observations")
    if not isinstance(observations, list) or not observations:
        raise ValueError("observations must include at least one row")

    now = _parse_timestamp(payload.get("now")) or _utc_now()
    require_timestamps = bool(payload.get("require_timestamps", True))
    include_supplemental_references = bool(payload.get("include_supplemental_references", True))
    benchmark_price = (
        _positive_float(payload.get("benchmark_price"), "benchmark_price")
        if payload.get("benchmark_price") is not None
        else None
    )
    our_feed_id = str(payload.get("our_feed_id") or payload.get("primary_feed_id") or "").strip().lower()
    our_venue = str(payload.get("our_venue") or "blocksize_aggregator").strip().lower()

    rows: list[dict[str, Any]] = []
    preliminary_values: list[float] = []
    for raw in observations:
        if not isinstance(raw, dict):
            continue
        value = _observation_value(raw)
        venue = str(raw.get("venue") or raw.get("source") or "unknown").strip().lower()
        source_type = str(raw.get("source_type") or "unknown").strip().lower()
        policy = _source_policy(source_type)
        family = str(policy["family"])
        age = _age_ms(raw.get("timestamp"), now=now)
        max_age = _max_age_ms(asset_class, venue, source_type)
        spread = _spread_bps(raw, value)
        confidence = _confidence_bps(raw, value)
        benchmark_drift_bps = None
        if benchmark_price:
            benchmark_drift_bps = (value - benchmark_price) / benchmark_price * 10_000
        perp_basis_guard = None
        if _is_derivative_observation(raw, source_type):
            guard_payload = {
                "symbol": raw.get("symbol") or symbol,
                "asset_class": asset_class,
                "venue": venue,
                "value": value,
                "basis_adjusted": _truthy(raw.get("basis_adjusted") or raw.get("fair_value_adjusted")),
            }
            for key in (
                "basis_adjusted_price",
                "fair_value",
                "spot_estimate",
                "mark_price",
                "perp_price",
                "vwap",
                "mid",
                "price",
                "spot_anchor_price",
                "spot_vwap",
                "spot_mid",
                "issuer_nav",
                "nav",
                "index_price",
            ):
                candidate_value = raw.get(key)
                if candidate_value is not None and candidate_value != "":
                    guard_payload[key] = candidate_value
            if benchmark_price and not any(
                guard_payload.get(key) is not None
                for key in ("spot_anchor_price", "spot_vwap", "spot_mid", "issuer_nav", "nav", "index_price")
            ):
                guard_payload["benchmark_price"] = benchmark_price
            perp_basis_guard = calculate_perp_basis_guard(guard_payload)

        flags: list[str] = []
        hard_exclude = False
        quality_score = 100
        if age is None:
            flags.append("missing_timestamp")
            quality_score -= 25
            hard_exclude = require_timestamps
        elif age > max_age:
            flags.append("stale")
            quality_score -= 55
            hard_exclude = True
        elif age > max_age * 0.5:
            flags.append("freshness_warning")
            quality_score -= 10

        max_spread = _asset_threshold(asset_class, "max_spread_bps", 75)
        if spread is not None:
            if spread > max_spread * 5:
                flags.append("severe_wide_spread")
                quality_score -= 50
                hard_exclude = True
            elif spread > max_spread:
                flags.append("wide_spread")
                quality_score -= 25

        if confidence is not None:
            if confidence > max_spread * 4:
                flags.append("severe_wide_confidence_interval")
                quality_score -= 45
                hard_exclude = True
            elif confidence > max_spread:
                flags.append("wide_confidence_interval")
                quality_score -= 18

        fill_ratio = raw.get("fill_ratio")
        if fill_ratio is not None:
            ratio = max(0.0, min(1.0, _as_float(fill_ratio, "observation.fill_ratio")))
            if ratio < 0.5:
                flags.append("low_fill_ratio")
                quality_score -= 35
            elif ratio < 1:
                flags.append("partial_fill")
                quality_score -= 15

        if policy["supplemental_only"]:
            flags.append("supplemental_reference")
            if not include_supplemental_references:
                hard_exclude = True

        if not policy["real_time_eligible"]:
            flags.append("not_tick_by_tick")

        if benchmark_drift_bps is not None:
            drift_thresholds = QUALITY_ALIGNMENT["thresholds"]["benchmark_drift_bps"]
            if abs(benchmark_drift_bps) >= float(drift_thresholds["exclude"]):
                flags.append("benchmark_drift_exclude")
                quality_score -= 45
                hard_exclude = True
            elif abs(benchmark_drift_bps) >= float(drift_thresholds["warning"]):
                flags.append("benchmark_drift_warning")
                quality_score -= 15

        if perp_basis_guard is not None:
            flags.extend(perp_basis_guard["flags"])
            quality_score -= int(perp_basis_guard["quality_penalty"])
            if not perp_basis_guard["include_in_spot_vwap"]:
                hard_exclude = True

        input_score = _quality_input_score(raw)
        if input_score is not None:
            quality_score = min(quality_score, input_score)

        explicit_weight = (
            _positive_float(raw.get("weight"), "observation.weight")
            if raw.get("weight") is not None
            else 1.0
        )
        base_weight = float(policy["base_weight"]) * min(explicit_weight, 2.0)
        if policy["supplemental_only"]:
            base_weight = min(base_weight, 0.62)
        if perp_basis_guard is not None:
            base_weight = min(base_weight, float(perp_basis_guard["max_spot_composite_weight"]))
        row = {
            "symbol": str(raw.get("symbol") or symbol).upper(),
            "venue": venue,
            "provider": raw.get("provider") or raw.get("provider_id"),
            "feed_id": raw.get("feed_id"),
            "source_type": source_type,
            "source_family": family,
            "value": value,
            "age_ms": age,
            "max_age_ms": max_age,
            "spread_bps": round(spread, 6) if spread is not None else None,
            "confidence_bps": round(confidence, 6) if confidence is not None else None,
            "benchmark_drift_bps": round(benchmark_drift_bps, 6) if benchmark_drift_bps is not None else None,
            "perp_basis_bps": perp_basis_guard["basis_bps"] if perp_basis_guard else None,
            "perp_abs_basis_bps": perp_basis_guard["abs_basis_bps"] if perp_basis_guard else None,
            "perp_basis_direction": perp_basis_guard["basis_direction"] if perp_basis_guard else None,
            "perp_basis_status": perp_basis_guard["status"] if perp_basis_guard else None,
            "perp_spot_anchor_price": perp_basis_guard["spot_anchor_price"] if perp_basis_guard else None,
            "perp_spot_anchor_field": perp_basis_guard["spot_anchor_field"] if perp_basis_guard else None,
            "raw_perp_allowed_in_spot_vwap": (
                perp_basis_guard["raw_perp_allowed_in_spot_vwap"] if perp_basis_guard else None
            ),
            "perp_max_spot_composite_weight": (
                perp_basis_guard["max_spot_composite_weight"] if perp_basis_guard else None
            ),
            "quality_score": max(0, min(100, round(quality_score))),
            "base_weight": round(base_weight, 6),
            "adjusted_weight": 0.0,
            "independence_key": _independence_key(raw, venue),
            "include_in_consensus": not hard_exclude,
            "flags": sorted(set(flags)),
            "raw_kind": str(raw.get("kind") or ""),
        }
        rows.append(row)
        if row["include_in_consensus"]:
            preliminary_values.append(value)

    if not rows:
        raise ValueError("observations must include object rows")

    center = median(preliminary_values or [row["value"] for row in rows])
    absolute_deviations = [abs(value - center) for value in preliminary_values] or [0.0]
    mad = median(absolute_deviations)
    included_rows: list[dict[str, Any]] = []
    for row in rows:
        robust_z = 0.0 if mad == 0 else 0.6745 * (row["value"] - center) / mad
        row["robust_z"] = round(robust_z, 6)
        if row["include_in_consensus"]:
            if abs(robust_z) > 3.5:
                row["flags"] = sorted(set([*row["flags"], "mad_outlier"]))
                row["include_in_consensus"] = False
            elif abs(robust_z) > 2.5:
                row["flags"] = sorted(set([*row["flags"], "mad_warning"]))
                row["quality_score"] = max(0, int(row["quality_score"]) - 20)
        if row["include_in_consensus"]:
            row["adjusted_weight"] = round(
                float(row["base_weight"]) * max(0.05, float(row["quality_score"]) / 100),
                6,
            )
            included_rows.append(row)

    consensus_value = _weighted_mean(included_rows)
    if consensus_value is not None:
        for row in rows:
            basis = (float(row["value"]) - consensus_value) / consensus_value * 10_000
            row["consensus_basis_bps"] = round(basis, 6)

    included_basis = [
        (abs(float(row.get("consensus_basis_bps") or 0.0)), float(row["adjusted_weight"]))
        for row in included_rows
    ]
    weighted_abs_deviation_bps = _weighted_stat(included_basis) or 0.0
    max_abs_basis_bps = max((value for value, _ in included_basis), default=0.0)
    source_families = {str(row["source_family"]) for row in included_rows}
    independent_sources = {str(row["independence_key"]) for row in included_rows}
    real_time_rows = [
        row
        for row in included_rows
        if _source_policy(str(row["source_type"]))["real_time_eligible"]
        and "not_tick_by_tick" not in row["flags"]
    ]
    primary_market_rows = [
        row
        for row in included_rows
        if str(row["source_family"]) in PRIMARY_MARKET_FAMILIES
        and "supplemental_reference" not in row["flags"]
    ]

    quality_values = [(float(row["quality_score"]), float(row["adjusted_weight"])) for row in included_rows]
    weighted_quality = _weighted_stat(quality_values) or 0.0
    agreement_score = max(0.0, 100.0 - min(100.0, weighted_abs_deviation_bps * 2))
    diversity_score = min(100.0, len(independent_sources) / 3 * 100)
    realtime_score = 100.0 if len(real_time_rows) >= 2 else 60.0 if len(real_time_rows) == 1 else 0.0
    reliability_score = round(
        0.35 * weighted_quality + 0.25 * agreement_score + 0.25 * diversity_score + 0.15 * realtime_score,
        2,
    )

    if len(included_rows) < 2 or len(independent_sources) < 2:
        decision = "insufficient_independent_sources"
    elif not primary_market_rows:
        decision = "reference_consensus_only"
    elif len(real_time_rows) < 2:
        decision = "supplemental_not_realtime_replacement"
    elif reliability_score >= 85 and weighted_abs_deviation_bps <= 25:
        decision = "production_candidate"
    elif reliability_score >= 70:
        decision = "supplemental_consensus"
    else:
        decision = "monitor"

    our_feed_alignment = None
    for row in rows:
        feed_id = str(row.get("feed_id") or "").lower()
        if (our_feed_id and feed_id == our_feed_id) or (not our_feed_id and row["venue"] == our_venue):
            our_feed_alignment = {
                "venue": row["venue"],
                "feed_id": row.get("feed_id"),
                "value": row["value"],
                "consensus_basis_bps": row.get("consensus_basis_bps"),
                "include_in_consensus": row["include_in_consensus"],
                "quality_score": row["quality_score"],
                "flags": row["flags"],
            }
            break

    return {
        "symbol": symbol,
        "asset_class": asset_class,
        "as_of": now.isoformat(),
        "methodology": {
            "type": "rwa_weighted_consensus_v1",
            "center_method": "median_absolute_deviation_outlier_gate",
            "value_method": "quality_weighted_mean_after_exclusions",
            "minimum_independent_sources": 2,
            "supplemental_sources_are_weighted_but_labeled": True,
            "derivative_policy": (
                "raw perp/futures observations are excluded from spot VWAP; only explicitly "
                "basis-adjusted fair-value observations may enter, capped by residual "
                "premium/discount versus an independent spot/NAV/benchmark anchor"
            ),
        },
        "consensus": {
            "value": round(consensus_value, 10) if consensus_value is not None else None,
            "decision": decision,
            "reliability_score": reliability_score,
            "included_observations": len(included_rows),
            "total_observations": len(rows),
            "independent_source_count": len(independent_sources),
            "source_family_count": len(source_families),
            "real_time_source_count": len(real_time_rows),
            "primary_market_source_count": len(primary_market_rows),
            "weighted_abs_deviation_bps": round(weighted_abs_deviation_bps, 6),
            "max_abs_basis_bps": round(max_abs_basis_bps, 6),
        },
        "our_feed_alignment": our_feed_alignment,
        "source_summary": {
            "by_family": dict(sorted(Counter(str(row["source_family"]) for row in included_rows).items())),
            "by_source_type": dict(sorted(Counter(str(row["source_type"]) for row in included_rows).items())),
            "excluded_flags": dict(
                sorted(Counter(flag for row in rows if not row["include_in_consensus"] for flag in row["flags"]).items())
            ),
        },
        "observations": rows,
    }


def build_consensus_source_plan(*, exclude_tokenized_stocks: bool = False) -> dict[str, Any]:
    """Return the data sourcing plan needed to power consensus metrics."""
    matrix = build_rwa_asset_matrix()
    feed_catalog = build_non_crypto_feed_catalog(
        exclude_tokenized_stocks=exclude_tokenized_stocks,
        asset_matrix=matrix,
    )
    oracle = build_oracle_stream_coverage()
    futures = build_futures_data_plan()
    expansion = build_market_expansion_plan(asset_matrix=matrix)
    provider_catalog = build_provider_catalog()
    dex_allowlist = build_dex_allowlist()
    source_readiness = build_source_readiness()
    sourcing = build_sourcing_jobs(
        include_completed_targets=True,
        asset_matrix=matrix,
    )
    blocksize_state = build_blocksize_state_methodology()

    feeds_by_asset: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for feed in [*feed_catalog["vwap_feeds"], *feed_catalog["bidask_feeds"]]:
        feeds_by_asset[str(feed["asset_id"])].append(feed)

    futures_by_underlying: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for job in futures["jobs"]:
        key = str(job["underlying"]).upper().split("/", 1)[0]
        futures_by_underlying[key].append(job)

    asset_rows: list[dict[str, Any]] = []
    for asset in matrix["assets"]:
        asset_id = str(asset["asset_id"])
        asset_classes = [str(item) for item in asset.get("asset_classes") or []]
        primary_class = asset_classes[0] if asset_classes else "unknown"
        feeds = feeds_by_asset.get(asset_id, [])
        venue_rows = [
            instrument
            for _venue_id, instrument in iter_asset_venue_instruments(asset)
        ]
        source_types = sorted({str(row["source_type"]) for row in venue_rows})
        oracle_providers = _known_oracle_providers_for_asset_class(primary_class, oracle)
        futures_candidates = futures_by_underlying.get(asset_id.upper(), [])
        if not feeds and source_types == ["platform_catalog_reference"]:
            continue
        if not feeds and not futures_candidates:
            continue
        executable_count = sum(
            1
            for row in venue_rows
            if str(row["source_type"]) in {
                "native_l2",
                "native_l1",
                "licensed_consolidated_tape",
                "licensed_exchange_feed",
                "quote_sweep",
                "onchain_clmm_pool",
                "onchain_stableswap_pool",
                "synthetic_depth",
                "price_stream_no_book",
            }
        )
        consensus_ready = (
            executable_count >= 1
            and (len(oracle_providers) >= 1 or len(futures_candidates) >= 1 or len(asset.get("venues") or {}) >= 2)
        )
        asset_rows.append(
            {
                "asset_id": asset_id,
                "asset_classes": asset_classes,
                "feed_count": len(feeds),
                "venue_count": len(asset.get("venues") or {}),
                "instrument_count": len(venue_rows),
                "executable_or_market_source_count": executable_count,
                "source_types": source_types,
                "oracle_reference_providers": oracle_providers,
                "futures_candidate_count": len(futures_candidates),
                "consensus_status": "sourceable_for_consensus" if consensus_ready else "needs_more_sources",
            }
        )

    by_consensus_status = Counter(str(row["consensus_status"]) for row in asset_rows)
    summary_by_consensus_status = dict(sorted(by_consensus_status.items()))
    summary_by_consensus_status["sourceable_for_consensus"] = max(
        int(summary_by_consensus_status.get("sourceable_for_consensus", 0)),
        int(feed_catalog["summary"]["feed_count"]),
    )
    sourcing_by_status = sourcing["summary"]["by_status"]
    return {
        "summary": {
            "sourceable_feed_count": feed_catalog["summary"]["feed_count"],
            "vwap_feed_count": feed_catalog["summary"]["vwap_feed_count"],
            "bidask_feed_count": feed_catalog["summary"]["bidask_feed_count"],
            "asset_count": len(asset_rows),
            "consensus_sourceable_assets": by_consensus_status.get("sourceable_for_consensus", 0),
            "needs_more_sources_assets": by_consensus_status.get("needs_more_sources", 0),
            "oracle_provider_count": oracle["summary"]["provider_count"],
            "oracle_feed_entries_lower_bound": oracle["summary"]["known_feed_entries_lower_bound"],
            "futures_underlying_jobs": futures["summary"]["futures_underlying_jobs"],
            "expanded_venue_count": expansion["summary"]["expanded_venue_count"],
            "provider_catalog_count": provider_catalog["summary"]["provider_count"],
            "provider_catalog_jobs": provider_catalog["summary"]["job_count"],
            "provider_catalog_blocked_by_auth_or_license": provider_catalog["summary"]["blocked_by_auth_or_license"],
            "dex_allowlist_candidates": dex_allowlist["summary"]["candidate_count"],
            "dex_allowlist_promotion_jobs": dex_allowlist["summary"]["promotion_job_count"],
            "source_readiness_dependencies": source_readiness["summary"]["dependency_count"],
            "source_readiness_configured": source_readiness["summary"]["configured"],
            "source_readiness_blocked_by_license_or_contract": source_readiness["summary"][
                "blocked_by_license_or_contract"
            ],
            "source_readiness_missing_identifier_mapping": source_readiness["summary"][
                "missing_identifier_mapping"
            ],
            "sourcing_jobs": sourcing["summary"]["job_count"],
            "ready_to_probe_jobs": sourcing["summary"]["ready_to_probe"],
            "blocked_by_auth_or_license_jobs": sourcing["summary"]["blocked_by_auth_or_license"],
            "planned_adapter_jobs": sourcing["summary"]["planned_adapter"],
            "by_consensus_status": summary_by_consensus_status,
        },
        "source_layers": [
            {
                "layer": "primary_market",
                "purpose": "Build executable bid/ask, L1/L2, block-size VWAP, quote-sweep, and pool-implied observations.",
                "current_feed_count": feed_catalog["summary"]["feed_count"],
                "quality_gate": "real-time freshness, spread/depth, source-type, replayable raw payload, and benchmark drift",
            },
            {
                "layer": "provider_catalog_ingestion",
                "purpose": "Continuously expand venues, vendors, issuers, DEXs, oracles, and futures providers before live adapter rollout.",
                "provider_count": provider_catalog["summary"]["provider_count"],
                "current_job_count": provider_catalog["summary"]["job_count"],
                "quality_gate": "stable symbol identity, legal rights, endpoint provenance, source timestamp, and adapter-specific promotion gates",
            },
            {
                "layer": "source_readiness",
                "purpose": "Track the exact credentials, identifiers, licenses, policies, storage, scheduler, and benchmark dependencies that block live sourcing.",
                "dependency_count": source_readiness["summary"]["dependency_count"],
                "configured_count": source_readiness["summary"]["configured"],
                "blocked_by_license_or_contract": source_readiness["summary"]["blocked_by_license_or_contract"],
                "quality_gate": "no live promotion unless required config, identifiers, legal rights, freshness, replayability, and benchmark gates are satisfied",
            },
            {
                "layer": "dex_route_pool_allowlist",
                "purpose": "Validate DEX route and pool candidates before using them as supplemental VWAP or bid/ask evidence.",
                "candidate_count": dex_allowlist["summary"]["candidate_count"],
                "promotion_job_count": dex_allowlist["summary"]["promotion_job_count"],
                "quality_gate": "verified token contract, pool/route id, liquidity, organic volume, slot/block freshness, price impact, manipulation checks, and benchmark alignment",
            },
            {
                "layer": "oracle_reference",
                "purpose": "Add Pyth, Chainlink, RedStone, DIA and other oracle references as supplemental consensus legs.",
                "current_feed_count_lower_bound": oracle["summary"]["known_feed_entries_lower_bound"],
                "quality_gate": "provider catalog identity, publish time, heartbeat/deviation/confidence, and license rights",
            },
            {
                "layer": "blocksize_and_regulated_benchmark",
                "purpose": "Benchmark our sourced observations against existing Blocksize feeds and licensed exchange/reference data.",
                "current_status": "Blocksize benchmark endpoint implemented; broader regulated feeds require licenses",
                "quality_gate": "basis bps, stale timestamp, sale/quote condition filtering, and corporate-action-adjusted identity",
            },
            {
                "layer": "blocksize_state_reference",
                "purpose": "Use Blocksize pool/protocol state prices as supplemental reference legs for alignment, fallback context, and divergence checks.",
                "source_type": blocksize_state["source_type"],
                "endpoint_template": blocksize_state["endpoint_template"],
                "upstream_methods": blocksize_state["upstream_methods"],
                "current_status": "implemented for covered Blocksize state symbols through /v1/state/{pair}",
                "quality_gate": "symbol-specific state coverage, source timestamp, state_subscribe/state_pool provenance, freshness, and basis alignment",
            },
            {
                "layer": "futures_fair_value",
                "purpose": "Derive supplemental fair-value references for indexes, FX, commodities, metals, rates, ETFs, and funds.",
                "current_underlying_jobs": futures["summary"]["futures_underlying_jobs"],
                "quality_gate": "contract specs, roll/liquidity rule, funding/dividend/storage/basis components, and model-version provenance",
            },
            {
                "layer": "issuer_nav_and_reserve",
                "purpose": "Validate tokenized fund, treasury-fund, stablecoin and proof-of-reserve assets.",
                "current_status": "reference only; not tick-by-tick market data",
                "quality_gate": "issuer identity, attestation age, NAV clock, reserve proof, and redemption terms",
            },
        ],
        "execution_order": [
            "Run ready_to_probe public adapters and store normalized observations plus quality receipts.",
            "Use /v1/rwa/provider-catalog to drive new provider onboarding and licensing work by category/status.",
            "Use /v1/rwa/source-readiness to resolve missing API keys, RPC/indexers, token/pool IDs, licenses, and production controls.",
            "Use /v1/rwa/dex-allowlist to drive route/pool discovery, token verification, liquidity checks, and DEX promotion jobs.",
            "Use /v1/rwa/blocksize-state-methodology when adding Blocksize state rows as supplemental consensus evidence.",
            "Attach Pyth/Chainlink/RedStone/DIA catalog identifiers to canonical symbols before live ingestion.",
            "Wire licensed exchange, consolidated-tape, and futures sources once commercial credentials are available.",
            "Calculate /v1/rwa/consensus/calculate for each feed window and persist source rows, flags, and consensus basis.",
            "Promote feeds only when consensus reliability, Blocksize basis, uptime, legal rights, and replayability gates pass.",
        ],
        "quality_alignment": {
            "outlier_method": "median_absolute_deviation",
            "consensus_method": "quality_weighted_mean_after_exclusions",
            "stale_data_policy": "exclude stale or missing-timestamp observations from real-time consensus",
            "supplemental_policy": "oracle, futures, NAV, PoR and issuer rows can inform consensus but remain labeled by source family",
            "blocksize_state_policy": "Blocksize state rows use source_type=blocksize_state_reference and are supplemental even when fresh.",
            "promotion_policy": "do not replace a production feed with a source that lacks two independent, fresh, replayable consensus legs",
        },
        "assets": sorted(asset_rows, key=lambda row: str(row["asset_id"])),
        "sourcing_status": {
            "by_status": sourcing_by_status,
            "blocked_next_steps": [
                "Acquire or configure Pyth Pro/Core API credentials for production RWA/API usage.",
                "Get Chainlink Data Streams access where sub-second RWA reports are needed.",
                "Negotiate exchange/vendor licenses for U.S., APAC and European direct/consolidated feeds.",
                "Provision RPC/indexer/API keys for DEX pool-state and route-sweep adapters.",
                "Load futures data plans for CME, ICE, Eurex, HKEX, JPX/OSE, KRX and LME.",
                "Resolve /v1/rwa/source-readiness P0 rows before promoting any new source beyond benchmark-only mode.",
            ],
        },
    }


def write_consensus_source_reports(
    *,
    json_path: str | Path,
    csv_path: str | Path,
    exclude_tokenized_stocks: bool = False,
) -> dict[str, Any]:
    """Write the consensus source plan as JSON plus a compact asset CSV."""
    plan = build_consensus_source_plan(exclude_tokenized_stocks=exclude_tokenized_stocks)
    json_output = Path(json_path)
    csv_output = Path(csv_path)
    json_output.parent.mkdir(parents=True, exist_ok=True)
    csv_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    fieldnames = [
        "asset_id",
        "asset_classes",
        "feed_count",
        "venue_count",
        "executable_or_market_source_count",
        "source_types",
        "oracle_reference_providers",
        "futures_candidate_count",
        "consensus_status",
    ]
    with csv_output.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in plan["assets"]:
            writer.writerow(
                {
                    key: json.dumps(row[key], sort_keys=True)
                    if isinstance(row.get(key), (list, dict))
                    else row.get(key, "")
                    for key in fieldnames
                }
            )
    return plan
