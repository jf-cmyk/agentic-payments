"""Benchmark sourced RWA observations against live Blocksize feeds."""

from __future__ import annotations

from typing import Any

from src.blocksize_client import FIAT_CURRENCIES, METAL_TICKERS
from src.rwa_coverage import QUALITY_ALIGNMENT


BLOCKSIZE_STATE_SOURCE_TYPE = "blocksize_state_reference"
BLOCKSIZE_STATE_VENUE = "blocksize_state"
STATE_REFERENCE_SOURCE_TYPES = {
    BLOCKSIZE_STATE_SOURCE_TYPE,
    "state_reference",
    "amm_state_reference",
    "pool_state_reference",
}


def build_blocksize_state_methodology() -> dict[str, Any]:
    """Return the Blocksize state-reference contract for RWA consensus."""
    return {
        "source_type": BLOCKSIZE_STATE_SOURCE_TYPE,
        "venue": BLOCKSIZE_STATE_VENUE,
        "service": "state",
        "endpoint_template": "/v1/state/{pair}",
        "upstream_methods": ["state_subscribe", "state_instruments", "state_pool"],
        "source_contract": {
            "role": "supplemental_reference",
            "value_semantics": "pool-derived state/reference price",
            "not_executable_liquidity": True,
            "not_a_bidask_or_order_book": True,
            "eligible_for_consensus": True,
            "eligible_for_feed_replacement_alone": False,
        },
        "quality_gates": [
            "Resolve the requested symbol through Blocksize state coverage before use.",
            "Prefer state_subscribe cache snapshots when available; otherwise use state_instruments plus state_pool.",
            "Require a source timestamp and the same real-time freshness gates as other supplemental references.",
            "Attach upstream method provenance so cached stream and pool-derived HTTP values are distinguishable.",
            "Compare against primary market observations in basis points and flag divergence before consensus inclusion.",
        ],
        "consensus_rules": [
            "Normalize the value into source_type=blocksize_state_reference and venue=blocksize_state.",
            "Include it as a supplemental benchmark-reference leg, never as primary executable liquidity.",
            "Exclude stale or missing-timestamp state rows when real-time consensus is required.",
            "Use MAD and benchmark-drift gates alongside spread/depth checks from primary venues.",
        ],
        "coverage_notes": [
            "State coverage is symbol-specific and strongest for Blocksize pool/protocol symbols such as MSOLUSD or JUPSOLUSD.",
            "Plain spot symbols and RWA tickers only qualify when Blocksize state_instruments has matching pool coverage.",
            "For tokenized RWA symbols, maintain an explicit benchmark_symbol/state_symbol mapping instead of guessing.",
        ],
        "observation_shape": {
            "symbol": "MSOLUSD",
            "venue": BLOCKSIZE_STATE_VENUE,
            "provider": "blocksize",
            "source_type": BLOCKSIZE_STATE_SOURCE_TYPE,
            "value": 215.0,
            "timestamp": "ISO-8601 source timestamp",
            "benchmark_service": "state",
            "benchmark_symbol": "MSOLUSD",
        },
    }


def normalize_blocksize_state_reference(
    state_snapshot: dict[str, Any],
    *,
    symbol: str | None = None,
) -> dict[str, Any]:
    """Normalize a Blocksize state snapshot into a supplemental observation row."""
    data = state_snapshot.get("data") if isinstance(state_snapshot.get("data"), dict) else {}
    resolved_symbol = str(
        symbol
        or data.get("pair")
        or data.get("ticker")
        or state_snapshot.get("symbol")
        or ""
    ).upper()
    value = state_snapshot.get("value")
    if value is None:
        value = data.get("price") or data.get("state_price") or data.get("reference_price")
    if value is None:
        raise ValueError("state snapshot does not include a state/reference price")
    timestamp = state_snapshot.get("timestamp") or data.get("timestamp")
    return {
        "symbol": resolved_symbol,
        "venue": BLOCKSIZE_STATE_VENUE,
        "provider": "blocksize",
        "source_type": BLOCKSIZE_STATE_SOURCE_TYPE,
        "value": float(value),
        "timestamp": timestamp,
        "benchmark_service": "state",
        "benchmark_symbol": resolved_symbol,
        "metadata": {
            "service": state_snapshot.get("service", "state"),
            "endpoint": state_snapshot.get("endpoint"),
            "source": data.get("source", "blocksize"),
            "source_method": state_snapshot.get("source_method"),
        },
    }


def _clean_part(value: str) -> str:
    return value.strip().replace("-", "").replace("/", "").replace("_", "").upper()


def _base_quote(symbol: str) -> tuple[str, str]:
    raw = symbol.strip()
    if "/" in raw:
        base, quote = raw.split("/", 1)
        return _clean_part(base), _clean_part(quote)
    clean = _clean_part(raw)
    for quote in ("USDT", "USDC", "USD", "EUR", "GBP", "JPY"):
        if clean.endswith(quote) and len(clean) > len(quote):
            return clean[: -len(quote)], quote
    return clean, ""


def _strip_tokenized_suffix(base: str) -> str:
    return base[:-1] if base.endswith("X") and len(base) > 1 else base


def resolve_blocksize_benchmark(observation: dict[str, Any]) -> dict[str, str]:
    """Resolve an RWA observation to the closest Blocksize benchmark service."""
    explicit_symbol = str(observation.get("benchmark_symbol") or observation.get("state_symbol") or "").strip()
    explicit_service = str(observation.get("benchmark_service") or "").strip().lower()
    if explicit_symbol and explicit_service:
        return {"service": explicit_service, "symbol": _clean_part(explicit_symbol)}

    symbol = str(observation.get("symbol") or "").strip()
    asset_class = str(observation.get("asset_class") or "").strip().lower()
    source_type = str(observation.get("source_type") or "").strip().lower()
    venue = str(observation.get("venue") or "").strip().lower()
    base, quote = _base_quote(symbol)
    base_no_x = _strip_tokenized_suffix(base)
    compact = f"{base_no_x}{quote}" if quote else base_no_x

    if explicit_symbol:
        compact = _clean_part(explicit_symbol)

    if explicit_service:
        if explicit_service == "state":
            return {"service": explicit_service, "symbol": _clean_part(explicit_symbol or symbol)}
        return {"service": explicit_service, "symbol": compact}

    if source_type in STATE_REFERENCE_SOURCE_TYPES or venue == BLOCKSIZE_STATE_VENUE:
        return {"service": "state", "symbol": _clean_part(explicit_symbol or symbol)}
    if asset_class in {"equity", "etf", "index"} or source_type in {"native_l2", "quote_stream"}:
        return {"service": "bidask", "symbol": base_no_x}
    if base in FIAT_CURRENCIES and quote in FIAT_CURRENCIES:
        return {"service": "fx", "symbol": compact}
    if compact in METAL_TICKERS:
        return {"service": "metal", "symbol": compact}
    return {"service": "vwap", "symbol": compact or base_no_x}


def _observation_value(observation: dict[str, Any]) -> float:
    if observation.get("value") is not None:
        return float(observation["value"])
    bid = observation.get("bid")
    ask = observation.get("ask")
    if bid is not None and ask is not None:
        return (float(bid) + float(ask)) / 2
    if observation.get("mid") is not None:
        return float(observation["mid"])
    if observation.get("vwap") is not None:
        return float(observation["vwap"])
    raise ValueError("observation must include value, mid, vwap, or bid+ask")


def _benchmark_value(snapshot: dict[str, Any]) -> float:
    value = snapshot.get("value")
    if value is not None:
        return float(value)
    data = snapshot.get("data") if isinstance(snapshot.get("data"), dict) else {}
    for key in ("mid", "vwap", "price", "last"):
        if data.get(key) is not None:
            return float(data[key])
    bid = data.get("bid")
    ask = data.get("ask")
    if bid is not None and ask is not None:
        return (float(bid) + float(ask)) / 2
    raise ValueError("benchmark snapshot does not include a comparable value")


def compare_observation_to_blocksize(
    observation: dict[str, Any],
    benchmark_snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Compare one normalized observation against one Blocksize snapshot."""
    observation_value = _observation_value(observation)
    benchmark_value = _benchmark_value(benchmark_snapshot)
    if observation_value <= 0 or benchmark_value <= 0:
        raise ValueError("observation and benchmark values must be positive")
    drift_bps = (observation_value - benchmark_value) / benchmark_value * 10_000
    abs_drift_bps = abs(drift_bps)
    thresholds = QUALITY_ALIGNMENT["thresholds"]["benchmark_drift_bps"]
    if abs_drift_bps >= float(thresholds["exclude"]):
        decision = "exclude"
    elif abs_drift_bps >= float(thresholds["warning"]):
        decision = "warn"
    else:
        decision = "pass"
    return {
        "symbol": str(observation.get("symbol") or "").upper(),
        "venue": str(observation.get("venue") or "unknown").lower(),
        "source_type": str(observation.get("source_type") or "unknown"),
        "observation_value": observation_value,
        "blocksize_value": benchmark_value,
        "basis_bps": round(drift_bps, 6),
        "abs_basis_bps": round(abs_drift_bps, 6),
        "decision": decision,
        "benchmark": {
            "service": benchmark_snapshot.get("service"),
            "symbol": benchmark_snapshot.get("symbol"),
            "endpoint": benchmark_snapshot.get("endpoint"),
            "timestamp": benchmark_snapshot.get("timestamp"),
        },
    }
