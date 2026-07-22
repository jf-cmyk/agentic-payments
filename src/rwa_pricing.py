"""RWA VWAP, bid/ask, and quality calculations.

These helpers keep venue adapters thin: adapters fetch and normalize raw venue
data, while this module owns calculation, fill, and outlier semantics.
"""

from __future__ import annotations

from datetime import UTC, datetime
from statistics import median
from typing import Any

from src.rwa_coverage import QUALITY_ALIGNMENT


SOURCE_TYPE_PENALTIES = {
    "native_l2": 0,
    "native_l1": 2,
    "synthetic_depth": 10,
    "quote_sweep": 12,
    "onchain_clmm_pool": 14,
    "onchain_stableswap_pool": 12,
    "dex_indexer_reference": 22,
    "onchain_pool_mid": 18,
    "synthetic_l1": 15,
    "price_stream_no_book": 25,
    "quote_stream": 12,
    "issuer_reference": 35,
    "benchmark_reference": 5,
    "licensed_consolidated_tape": 2,
    "licensed_exchange_feed": 3,
    "oracle_reference": 10,
    "macro_reference": 20,
    "proof_of_reserve": 15,
    "nav_reference": 30,
}

PERP_BASIS_GUARD_THRESHOLDS_BPS = {
    "crypto": {"warning": 75.0, "exclude": 200.0},
    "equity": {"warning": 35.0, "exclude": 100.0},
    "etf": {"warning": 30.0, "exclude": 75.0},
    "fx": {"warning": 5.0, "exclude": 20.0},
    "metal": {"warning": 25.0, "exclude": 75.0},
    "commodity": {"warning": 35.0, "exclude": 100.0},
    "tokenized_fund": {"warning": 15.0, "exclude": 50.0},
    "treasury_fund": {"warning": 5.0, "exclude": 15.0},
    "default": {"warning": 35.0, "exclude": 100.0},
}


def _as_float(value: Any, field_name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{field_name} must be numeric") from exc
    if result < 0:
        raise ValueError(f"{field_name} must be non-negative")
    return result


def _parse_timestamp(value: Any) -> datetime | None:
    if value is None or value == "":
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


def _age_ms(timestamp_value: Any, *, now: datetime | None = None) -> int | None:
    timestamp = _parse_timestamp(timestamp_value)
    if timestamp is None:
        return None
    now = now or datetime.now(UTC)
    return max(0, int((now - timestamp).total_seconds() * 1000))


def _required_text(payload: dict[str, Any], field_name: str) -> str:
    value = str(payload.get(field_name) or "").strip()
    if not value:
        raise ValueError(f"{field_name} is required")
    return value


def _normalized_identity(value: Any) -> str:
    return str(value or "").strip().upper()


def _validate_exact_identity(
    row: dict[str, Any],
    *,
    canonical_asset_id: str,
    quote_currency: str,
) -> tuple[str, str]:
    """Require explicit identity fields; a matching display symbol is never enough."""
    row_asset_id = _normalized_identity(row.get("canonical_asset_id"))
    row_quote = _normalized_identity(row.get("quote_currency"))
    instrument_id = str(row.get("instrument_id") or "").strip()
    if not row_asset_id or not row_quote or not instrument_id:
        raise ValueError(
            "each observation requires canonical_asset_id, quote_currency, and instrument_id; "
            "symbol-only joins are not allowed"
        )
    if row_asset_id != canonical_asset_id or row_quote != quote_currency:
        raise ValueError(
            f"identity mismatch for instrument_id={instrument_id}: expected "
            f"{canonical_asset_id}/{quote_currency}, got {row_asset_id}/{row_quote}"
        )
    return instrument_id, row_asset_id


def _parse_now(payload: dict[str, Any]) -> datetime:
    now = _parse_timestamp(payload.get("now"))
    if now is None:
        raise ValueError("now is required for deterministic freshness evaluation")
    return now.astimezone(UTC)


def _allowed_rights_statuses(payload: dict[str, Any]) -> set[str]:
    raw = payload.get("allowed_rights_statuses")
    if not isinstance(raw, list) or not raw:
        raise ValueError("allowed_rights_statuses must include at least one explicitly approved status")
    statuses = {str(value).strip().lower() for value in raw if str(value).strip()}
    if not statuses:
        raise ValueError("allowed_rights_statuses must include at least one explicitly approved status")
    return statuses


def _threshold(asset_class: str, key: str, default: float) -> float:
    thresholds = QUALITY_ALIGNMENT["thresholds"].get(key, {})
    normalized = asset_class.lower().strip()
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


def _source_type_penalty(source_type: str) -> int:
    return SOURCE_TYPE_PENALTIES.get(source_type, 20)


def _basis_thresholds(asset_class: str) -> dict[str, float]:
    normalized = (asset_class or "").strip().lower()
    if normalized in PERP_BASIS_GUARD_THRESHOLDS_BPS:
        return PERP_BASIS_GUARD_THRESHOLDS_BPS[normalized]
    if normalized in {"tokenized_equity", "synthetic_equity"}:
        return PERP_BASIS_GUARD_THRESHOLDS_BPS["equity"]
    if normalized in {"tokenized_etf", "synthetic_etf_index"}:
        return PERP_BASIS_GUARD_THRESHOLDS_BPS["etf"]
    if normalized in {"metal_commodity"}:
        return PERP_BASIS_GUARD_THRESHOLDS_BPS["commodity"]
    if normalized in {"treasury_nav", "treasury"}:
        return PERP_BASIS_GUARD_THRESHOLDS_BPS["treasury_fund"]
    return PERP_BASIS_GUARD_THRESHOLDS_BPS["default"]


def _first_positive(payload: dict[str, Any], keys: tuple[str, ...]) -> tuple[str | None, float | None]:
    for key in keys:
        candidate_value = payload.get(key)
        if candidate_value is None or candidate_value == "":
            continue
        value = _as_float(candidate_value, key)
        if value > 0:
            return key, value
    return None, None


def _payload_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y", "pass", "passed"}
    return bool(value)


def calculate_perp_basis_guard(payload: dict[str, Any]) -> dict[str, Any]:
    """Gate raw perp/futures observations before they can affect spot VWAP.

    Raw derivative prices are never treated as spot. A derivative observation can
    only enter the spot composite if it is explicitly marked as basis-adjusted
    fair value and the residual premium/discount versus an independent spot
    anchor stays inside the configured guardrail.
    """
    symbol = str(payload.get("symbol") or "").strip().upper()
    if not symbol:
        raise ValueError("symbol is required")
    asset_class = str(payload.get("asset_class") or "equity").strip().lower()
    venue = str(payload.get("venue") or "unknown").strip().lower()
    derivative_key, derivative_price = _first_positive(
        payload,
        (
            "basis_adjusted_price",
            "fair_value",
            "spot_estimate",
            "mark_price",
            "perp_price",
            "vwap",
            "mid",
            "price",
            "value",
        ),
    )
    if derivative_price is None:
        raise ValueError("payload must include a positive perp/futures price")

    anchor_key, anchor_price = _first_positive(
        payload,
        (
            "spot_anchor_price",
            "spot_vwap",
            "spot_mid",
            "issuer_nav",
            "nav",
            "benchmark_price",
            "index_price",
        ),
    )
    thresholds = _basis_thresholds(asset_class)
    basis_adjusted = _payload_bool(payload.get("basis_adjusted")) or _payload_bool(payload.get("fair_value_adjusted"))
    flags: list[str] = ["raw_perp_not_spot"]
    status = "blocked_missing_spot_anchor"
    basis_bps = None
    abs_basis_bps = None
    direction = "unknown"
    include_in_spot_vwap = False
    max_spot_composite_weight = 0.0
    quality_penalty = 50

    if anchor_price is None:
        flags.append("missing_spot_anchor")
    else:
        basis_bps = (derivative_price - anchor_price) / anchor_price * 10_000
        abs_basis_bps = abs(basis_bps)
        direction = "premium" if basis_bps > 0 else "discount" if basis_bps < 0 else "flat"
        if abs_basis_bps >= thresholds["exclude"]:
            status = f"exclude_derivative_{direction}"
            flags.extend(["perp_basis_exclude", f"perp_{direction}"])
            quality_penalty = 70
        elif abs_basis_bps >= thresholds["warning"]:
            flags.extend(["perp_basis_warning", f"perp_{direction}"])
            quality_penalty = 35
            if basis_adjusted:
                status = "basis_adjusted_warning_cap_weight"
                include_in_spot_vwap = True
                max_spot_composite_weight = 0.05
            else:
                status = "reference_only_basis_warning"
        else:
            flags.append("perp_basis_pass")
            quality_penalty = 10 if basis_adjusted else 25
            if basis_adjusted:
                status = "basis_adjusted_pass_cap_weight"
                include_in_spot_vwap = True
                max_spot_composite_weight = 0.15
            else:
                status = "reference_only_until_basis_adjusted"

    return {
        "symbol": symbol,
        "venue": venue,
        "asset_class": asset_class,
        "derivative_price": round(derivative_price, 10),
        "derivative_price_field": derivative_key,
        "spot_anchor_price": round(anchor_price, 10) if anchor_price is not None else None,
        "spot_anchor_field": anchor_key,
        "basis_bps": round(basis_bps, 6) if basis_bps is not None else None,
        "abs_basis_bps": round(abs_basis_bps, 6) if abs_basis_bps is not None else None,
        "basis_direction": direction,
        "basis_warning_bps": thresholds["warning"],
        "basis_exclude_bps": thresholds["exclude"],
        "basis_adjusted": basis_adjusted,
        "raw_perp_allowed_in_spot_vwap": False,
        "include_in_spot_vwap": include_in_spot_vwap,
        "max_spot_composite_weight": max_spot_composite_weight,
        "status": status,
        "quality_penalty": quality_penalty,
        "flags": sorted(set(flags)),
    }


def _quality_score(
    *,
    asset_class: str,
    source_type: str,
    age_ms: int | None,
    spread_bps: float | None = None,
    fill_ratio: float | None = None,
    benchmark_drift_bps: float | None = None,
) -> tuple[int, list[str]]:
    score = 100 - _source_type_penalty(source_type)
    flags: list[str] = []
    max_age_ms = _threshold(asset_class, "max_age_ms", 60_000)
    max_spread_bps = _threshold(asset_class, "max_spread_bps", 75)
    drift_thresholds = QUALITY_ALIGNMENT["thresholds"]["benchmark_drift_bps"]

    if age_ms is None:
        score -= 10
        flags.append("missing_timestamp")
    elif age_ms > max_age_ms:
        score -= 35
        flags.append("stale")

    if spread_bps is not None and spread_bps > max_spread_bps:
        score -= 30
        flags.append("wide_spread")

    if fill_ratio is not None and fill_ratio < 1:
        score -= 25 if fill_ratio < 0.5 else 15
        flags.append("partial_fill")

    if benchmark_drift_bps is not None:
        abs_drift = abs(benchmark_drift_bps)
        if abs_drift >= float(drift_thresholds["exclude"]):
            score -= 40
            flags.append("benchmark_drift_exclude")
        elif abs_drift >= float(drift_thresholds["warning"]):
            score -= 15
            flags.append("benchmark_drift_warning")

    return max(0, min(100, round(score))), flags


def _level_notional(level: dict[str, Any]) -> tuple[float, float]:
    price = _as_float(level.get("price"), "level.price")
    if price <= 0:
        raise ValueError("level.price must be greater than zero")
    raw_notional = level.get("notional_usd")
    if raw_notional is not None:
        notional = _as_float(raw_notional, "level.notional_usd")
        size = notional / price
    else:
        size_value = (
            level.get("size")
            if level.get("size") is not None
            else level.get("quantity")
            if level.get("quantity") is not None
            else level.get("base_size")
        )
        size = _as_float(size_value, "level.size")
        notional = price * size
    return price, notional


def calculate_block_vwap(payload: dict[str, Any]) -> dict[str, Any]:
    """Calculate block-size VWAP from normalized L2 levels."""
    symbol = str(payload.get("symbol") or "").strip().upper()
    if not symbol:
        raise ValueError("symbol is required")
    venue = str(payload.get("venue") or "unknown").strip().lower()
    asset_class = str(payload.get("asset_class") or "equity").strip().lower()
    source_type = str(payload.get("source_type") or "native_l2").strip().lower()
    side = str(payload.get("side") or "buy").strip().lower()
    if side not in {"buy", "sell"}:
        raise ValueError("side must be buy or sell")
    block_size_usd = _as_float(payload.get("block_size_usd"), "block_size_usd")
    if block_size_usd <= 0:
        raise ValueError("block_size_usd must be greater than zero")
    levels = payload.get("levels")
    if not isinstance(levels, list) or not levels:
        raise ValueError("levels must include at least one order-book level")

    parsed_levels = [_level_notional(level) for level in levels if isinstance(level, dict)]
    if not parsed_levels:
        raise ValueError("levels must include object rows with price and size or notional_usd")
    parsed_levels.sort(key=lambda item: item[0], reverse=side == "sell")

    top_price = parsed_levels[0][0]
    remaining = block_size_usd
    filled_notional = 0.0
    filled_size = 0.0
    consumed_levels: list[dict[str, float]] = []

    for price, available_notional in parsed_levels:
        if remaining <= 0:
            break
        consumed_notional = min(remaining, available_notional)
        consumed_size = consumed_notional / price
        filled_notional += consumed_notional
        filled_size += consumed_size
        remaining -= consumed_notional
        consumed_levels.append(
            {
                "price": price,
                "consumed_notional_usd": round(consumed_notional, 8),
                "consumed_size": round(consumed_size, 12),
            }
        )

    if filled_size <= 0:
        raise ValueError("levels do not contain fillable depth")

    vwap = filled_notional / filled_size
    if side == "buy":
        slippage_bps = (vwap - top_price) / top_price * 10_000
    else:
        slippage_bps = (top_price - vwap) / top_price * 10_000
    fill_ratio = min(1.0, filled_notional / block_size_usd)
    benchmark = payload.get("benchmark_price")
    benchmark_drift_bps = None
    if benchmark is not None:
        benchmark_price = _as_float(benchmark, "benchmark_price")
        if benchmark_price > 0:
            benchmark_drift_bps = (vwap - benchmark_price) / benchmark_price * 10_000
    age = _age_ms(payload.get("timestamp"))
    score, flags = _quality_score(
        asset_class=asset_class,
        source_type=source_type,
        age_ms=age,
        fill_ratio=fill_ratio,
        benchmark_drift_bps=benchmark_drift_bps,
    )

    return {
        "symbol": symbol,
        "venue": venue,
        "asset_class": asset_class,
        "side": side,
        "source_type": source_type,
        "block_size_usd": block_size_usd,
        "vwap": round(vwap, 10),
        "top_price": top_price,
        "fillable_notional_usd": round(filled_notional, 8),
        "fill_ratio": round(fill_ratio, 6),
        "slippage_bps": round(slippage_bps, 6),
        "benchmark_drift_bps": round(benchmark_drift_bps, 6) if benchmark_drift_bps is not None else None,
        "status": "full_fill" if fill_ratio >= 1 else "partial_fill",
        "quality": {"score": score, "flags": flags, "age_ms": age},
        "consumed_levels": consumed_levels,
    }


def calculate_executable_composite(payload: dict[str, Any]) -> dict[str, Any]:
    """Route a base quantity or quote notional across fresh, eligible L2 books.

    This is deliberately narrower than a general smart-order router. It accepts
    only side-specific executable L2 levels with explicit canonical identity.
    AMM/router quotes and reference prices must use separate calculation paths.
    """
    canonical_asset_id = _normalized_identity(_required_text(payload, "canonical_asset_id"))
    quote_currency = _normalized_identity(_required_text(payload, "quote_currency"))
    side = str(payload.get("side") or "").strip().lower()
    if side not in {"buy", "sell"}:
        raise ValueError("side must be buy or sell")
    request_kind = str(payload.get("request_kind") or "").strip().lower()
    if request_kind not in {"base_quantity", "quote_notional"}:
        raise ValueError("request_kind must be base_quantity or quote_notional")
    requested_amount = _as_float(payload.get("requested_amount"), "requested_amount")
    if requested_amount <= 0:
        raise ValueError("requested_amount must be greater than zero")

    now = _parse_now(payload)
    max_age_ms = int(_as_float(payload.get("max_age_ms"), "max_age_ms"))
    min_reliability = _as_float(payload.get("min_reliability", 0), "min_reliability")
    max_venue_share = _as_float(payload.get("max_venue_share", 1), "max_venue_share")
    min_venues = int(_as_float(payload.get("min_venues", 1), "min_venues"))
    if max_age_ms <= 0:
        raise ValueError("max_age_ms must be greater than zero")
    if not 0 <= min_reliability <= 1:
        raise ValueError("min_reliability must be between zero and one")
    if not 0 < max_venue_share <= 1:
        raise ValueError("max_venue_share must be greater than zero and at most one")
    if min_venues < 1:
        raise ValueError("min_venues must be at least one")
    allowed_rights = _allowed_rights_statuses(payload)

    books = payload.get("books")
    if not isinstance(books, list) or not books:
        raise ValueError("books must include at least one executable L2 book")

    chunks: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    seen_instruments: set[tuple[str, str]] = set()
    for row in books:
        if not isinstance(row, dict):
            continue
        instrument_id, _ = _validate_exact_identity(
            row,
            canonical_asset_id=canonical_asset_id,
            quote_currency=quote_currency,
        )
        venue = _required_text(row, "venue").lower()
        dedupe_key = (venue, instrument_id)
        if dedupe_key in seen_instruments:
            raise ValueError(f"duplicate executable book for venue/instrument: {venue}/{instrument_id}")
        seen_instruments.add(dedupe_key)

        source_kind = str(row.get("source_kind") or "").strip().lower()
        if source_kind != "executable_l2":
            excluded.append({"venue": venue, "instrument_id": instrument_id, "reason": "not_executable_l2"})
            continue
        row_side = str(row.get("side") or "").strip().lower()
        if row_side != side:
            excluded.append({"venue": venue, "instrument_id": instrument_id, "reason": "side_mismatch"})
            continue
        rights_status = str(row.get("rights_status") or "").strip().lower()
        if rights_status not in allowed_rights:
            excluded.append({"venue": venue, "instrument_id": instrument_id, "reason": "rights_not_allowed"})
            continue
        event_time = row.get("event_time")
        age_ms = _age_ms(event_time, now=now)
        if age_ms is None:
            excluded.append({"venue": venue, "instrument_id": instrument_id, "reason": "missing_event_time"})
            continue
        if age_ms > max_age_ms:
            excluded.append(
                {"venue": venue, "instrument_id": instrument_id, "reason": "stale", "age_ms": age_ms}
            )
            continue
        reliability = _as_float(row.get("reliability", 1), "reliability")
        if reliability < min_reliability or reliability > 1:
            excluded.append(
                {
                    "venue": venue,
                    "instrument_id": instrument_id,
                    "reason": "reliability_below_minimum" if reliability < min_reliability else "invalid_reliability",
                }
            )
            continue
        fee_bps = _as_float(row.get("taker_fee_bps", 0), "taker_fee_bps")
        if fee_bps >= 10_000:
            raise ValueError("taker_fee_bps must be less than 10000")
        fee_rate = fee_bps / 10_000

        levels = row.get("levels")
        if not isinstance(levels, list) or not levels:
            excluded.append({"venue": venue, "instrument_id": instrument_id, "reason": "missing_levels"})
            continue
        parsed = [_level_notional(level) for level in levels if isinstance(level, dict)]
        if not parsed:
            excluded.append({"venue": venue, "instrument_id": instrument_id, "reason": "missing_levels"})
            continue
        parsed.sort(key=lambda item: item[0], reverse=side == "sell")

        cap_base = _as_float(row.get("venue_cap_base", float("inf")), "venue_cap_base")
        cap_quote = _as_float(row.get("venue_cap_quote", float("inf")), "venue_cap_quote")
        concentration_cap = requested_amount * max_venue_share
        remaining_cap_base = cap_base
        remaining_cap_quote = cap_quote
        if request_kind == "base_quantity":
            remaining_cap_base = min(remaining_cap_base, concentration_cap)
        else:
            remaining_cap_quote = min(remaining_cap_quote, concentration_cap)

        for level_index, (price, level_quote) in enumerate(parsed):
            if remaining_cap_base <= 0 or remaining_cap_quote <= 0:
                break
            level_base = level_quote / price
            available_base = min(level_base, remaining_cap_base, remaining_cap_quote / price)
            if available_base <= 0:
                continue
            available_quote = available_base * price
            effective_price = price * (1 + fee_rate if side == "buy" else 1 - fee_rate)
            chunks.append(
                {
                    "venue": venue,
                    "instrument_id": instrument_id,
                    "rights_status": rights_status,
                    "event_time": _parse_timestamp(event_time).astimezone(UTC).isoformat(),
                    "age_ms": age_ms,
                    "block": row.get("block"),
                    "slot": row.get("slot"),
                    "sequence": row.get("sequence"),
                    "level_index": level_index,
                    "price": price,
                    "effective_price": effective_price,
                    "fee_rate": fee_rate,
                    "available_base": available_base,
                    "available_quote": available_quote,
                }
            )
            remaining_cap_base -= available_base
            remaining_cap_quote -= available_quote

    chunks.sort(key=lambda row: row["effective_price"], reverse=side == "sell")
    remaining = requested_amount
    filled_base = 0.0
    gross_quote = 0.0
    fees_quote = 0.0
    route: list[dict[str, Any]] = []
    venue_request_fills: dict[str, float] = {}
    for chunk in chunks:
        if remaining <= 1e-15:
            break
        venue_remaining = max(0.0, requested_amount * max_venue_share - venue_request_fills.get(chunk["venue"], 0.0))
        if venue_remaining <= 1e-15:
            continue
        if request_kind == "base_quantity":
            take_base = min(remaining, chunk["available_base"], venue_remaining)
            request_fill = take_base
        else:
            take_quote = min(remaining, chunk["available_quote"], venue_remaining)
            take_base = take_quote / chunk["price"]
            request_fill = take_quote
        take_quote = take_base * chunk["price"]
        fee_quote = take_quote * chunk["fee_rate"]
        filled_base += take_base
        gross_quote += take_quote
        fees_quote += fee_quote
        remaining = max(0.0, remaining - request_fill)
        venue_request_fills[chunk["venue"]] = venue_request_fills.get(chunk["venue"], 0.0) + request_fill
        route.append(
            {
                "venue": chunk["venue"],
                "instrument_id": chunk["instrument_id"],
                "level_index": chunk["level_index"],
                "price": round(chunk["price"], 12),
                "effective_price": round(chunk["effective_price"], 12),
                "filled_base": round(take_base, 12),
                "gross_quote": round(take_quote, 12),
                "fee_quote": round(fee_quote, 12),
                "event_time": chunk["event_time"],
                "block": chunk["block"],
                "slot": chunk["slot"],
                "sequence": chunk["sequence"],
                "rights_status": chunk["rights_status"],
            }
        )

    filled_request_amount = requested_amount - remaining
    fill_ratio = filled_request_amount / requested_amount
    venues_used = sorted(venue_request_fills)
    raw_vwap = gross_quote / filled_base if filled_base else None
    net_quote = gross_quote + fees_quote if side == "buy" else gross_quote - fees_quote
    effective_vwap = net_quote / filled_base if filled_base else None
    quality_flags: list[str] = []
    if fill_ratio < 1 - 1e-12:
        quality_flags.append("partial_fill")
    if len(venues_used) < min_venues:
        quality_flags.append("insufficient_venues")
    if venue_request_fills and max(venue_request_fills.values()) / requested_amount > max_venue_share + 1e-12:
        quality_flags.append("venue_concentration_limit_breached")
    status = "full_fill"
    if fill_ratio < 1 - 1e-12:
        status = "partial_fill" if filled_request_amount > 0 else "no_fill"
    if len(venues_used) < min_venues:
        status = "insufficient_venues"

    return {
        "canonical_asset_id": canonical_asset_id,
        "quote_currency": quote_currency,
        "price_type": "executable_block_vwap",
        "side": side,
        "request_kind": request_kind,
        "requested_amount": requested_amount,
        "filled_request_amount": round(filled_request_amount, 12),
        "unfilled_request_amount": round(remaining, 12),
        "filled_base": round(filled_base, 12),
        "gross_quote": round(gross_quote, 12),
        "fees_quote": round(fees_quote, 12),
        "net_quote": round(net_quote, 12),
        "vwap": round(raw_vwap, 12) if raw_vwap is not None else None,
        "effective_vwap": round(effective_vwap, 12) if effective_vwap is not None else None,
        "fill_ratio": round(fill_ratio, 12),
        "venue_count": len(venues_used),
        "venues_used": venues_used,
        "venue_request_fills": {venue: round(value, 12) for venue, value in sorted(venue_request_fills.items())},
        "status": status,
        "quality_flags": quality_flags,
        "event_time_min": min((row["event_time"] for row in route), default=None),
        "event_time_max": max((row["event_time"] for row in route), default=None),
        "receive_time": now.isoformat(),
        "rights_statuses": sorted({row["rights_status"] for row in route}),
        "route": route,
        "excluded_books": excluded,
    }


def _weighted_median(rows: list[dict[str, Any]]) -> float:
    ordered = sorted(rows, key=lambda row: row["value"])
    total_weight = sum(row.get("normalized_weight", row["weight"]) for row in ordered)
    threshold = total_weight / 2
    cumulative = 0.0
    for row in ordered:
        cumulative += row.get("normalized_weight", row["weight"])
        if cumulative >= threshold:
            return float(row["value"])
    return float(ordered[-1]["value"])


def _capped_normalized_weights(raw_weights: list[float], cap: float) -> list[float]:
    """Normalize positive quality weights while enforcing a hard share cap."""
    if not raw_weights:
        return []
    if cap * len(raw_weights) < 1 - 1e-12:
        raise ValueError("max_source_weight is infeasible for the included independent source count")
    remaining_indices = set(range(len(raw_weights)))
    result = [0.0] * len(raw_weights)
    remaining_mass = 1.0
    while remaining_indices:
        raw_total = sum(raw_weights[index] for index in remaining_indices)
        if raw_total <= 0:
            equal = remaining_mass / len(remaining_indices)
            for index in remaining_indices:
                result[index] = equal
            break
        over_cap = [
            index
            for index in remaining_indices
            if remaining_mass * raw_weights[index] / raw_total > cap + 1e-15
        ]
        if not over_cap:
            for index in remaining_indices:
                result[index] = remaining_mass * raw_weights[index] / raw_total
            break
        for index in over_cap:
            result[index] = cap
            remaining_indices.remove(index)
            remaining_mass -= cap
    return result


def calculate_reference_composite(payload: dict[str, Any]) -> dict[str, Any]:
    """Build a robust, lineage-deduplicated reference that is never executable."""
    canonical_asset_id = _normalized_identity(_required_text(payload, "canonical_asset_id"))
    quote_currency = _normalized_identity(_required_text(payload, "quote_currency"))
    composite_id = _required_text(payload, "composite_id")
    now = _parse_now(payload)
    max_age_ms = int(_as_float(payload.get("max_age_ms"), "max_age_ms"))
    min_independent_sources = int(
        _as_float(payload.get("min_independent_sources", 2), "min_independent_sources")
    )
    max_source_weight = _as_float(payload.get("max_source_weight", 0.5), "max_source_weight")
    mad_z_limit = _as_float(payload.get("mad_z_limit", 3.5), "mad_z_limit")
    if max_age_ms <= 0 or min_independent_sources < 1:
        raise ValueError("max_age_ms and min_independent_sources must be positive")
    if not 0 < max_source_weight <= 1:
        raise ValueError("max_source_weight must be greater than zero and at most one")
    if max_source_weight * min_independent_sources < 1 - 1e-12:
        raise ValueError("max_source_weight is infeasible for min_independent_sources")
    allowed_rights = _allowed_rights_statuses(payload)
    allowed_kinds = {
        "oracle_reference",
        "benchmark_reference",
        "issuer_reference",
        "nav_reference",
        "index_reference",
        "mark_reference",
        "redemption_rate_reference",
    }
    observations = payload.get("observations")
    if not isinstance(observations, list) or not observations:
        raise ValueError("observations must include at least one reference observation")

    eligible: list[dict[str, Any]] = []
    excluded: list[dict[str, Any]] = []
    for row in observations:
        if not isinstance(row, dict):
            continue
        instrument_id, _ = _validate_exact_identity(
            row,
            canonical_asset_id=canonical_asset_id,
            quote_currency=quote_currency,
        )
        source_id = _required_text(row, "source_id")
        lineage_group = _required_text(row, "lineage_group")
        source_kind = str(row.get("source_kind") or "").strip().lower()
        reason = None
        if source_kind not in allowed_kinds:
            reason = "not_reference_semantics"
        elif str(row.get("rights_status") or "").strip().lower() not in allowed_rights:
            reason = "rights_not_allowed"
        elif composite_id in {str(value) for value in row.get("upstream_composite_ids") or []}:
            reason = "circular_dependency"
        age_ms = _age_ms(row.get("event_time"), now=now)
        if reason is None and age_ms is None:
            reason = "missing_event_time"
        elif reason is None and age_ms > max_age_ms:
            reason = "stale"
        if reason:
            excluded.append(
                {
                    "source_id": source_id,
                    "instrument_id": instrument_id,
                    "lineage_group": lineage_group,
                    "reason": reason,
                }
            )
            continue
        value = _as_float(row.get("value"), "value")
        weight = _as_float(row.get("quality_weight", 1), "quality_weight")
        if value <= 0 or weight <= 0:
            raise ValueError("reference value and quality_weight must be greater than zero")
        eligible.append(
            {
                "source_id": source_id,
                "instrument_id": instrument_id,
                "lineage_group": lineage_group,
                "source_kind": source_kind,
                "value": value,
                "weight": weight,
                "event_time": _parse_timestamp(row["event_time"]).astimezone(UTC).isoformat(),
                "age_ms": age_ms,
                "rights_status": str(row.get("rights_status")).strip().lower(),
            }
        )

    by_lineage: dict[str, list[dict[str, Any]]] = {}
    for row in eligible:
        by_lineage.setdefault(row["lineage_group"], []).append(row)
    lineage_rows: list[dict[str, Any]] = []
    for lineage_group, rows in sorted(by_lineage.items()):
        lineage_rows.append(
            {
                "lineage_group": lineage_group,
                "source_ids": sorted({row["source_id"] for row in rows}),
                "value": median(row["value"] for row in rows),
                "weight": max(row["weight"] for row in rows),
                "event_time": max(row["event_time"] for row in rows),
                "rights_statuses": sorted({row["rights_status"] for row in rows}),
            }
        )

    center = median([row["value"] for row in lineage_rows]) if lineage_rows else None
    mad = median([abs(row["value"] - center) for row in lineage_rows]) if lineage_rows else None
    included: list[dict[str, Any]] = []
    for row in lineage_rows:
        robust_z = 0.0 if not mad else 0.6745 * (row["value"] - center) / mad
        row["robust_z"] = round(robust_z, 12)
        row["included"] = abs(robust_z) <= mad_z_limit
        if row["included"]:
            included.append(row)
        else:
            excluded.append({"lineage_group": row["lineage_group"], "reason": "mad_outlier"})

    independent_count = len(included)
    status = "valid_reference" if independent_count >= min_independent_sources else "insufficient_independent_sources"
    normalized_weights = (
        _capped_normalized_weights([row["weight"] for row in included], max_source_weight)
        if status == "valid_reference"
        else []
    )
    for row, normalized_weight in zip(included, normalized_weights, strict=True):
        row["normalized_weight"] = normalized_weight
    reference_price = _weighted_median(included) if status == "valid_reference" else None
    included_mad = (
        median([abs(row["value"] - reference_price) for row in included]) if reference_price is not None else None
    )
    confidence_abs = 1.4826 * included_mad if included_mad is not None else None
    confidence_bps = (
        confidence_abs / reference_price * 10_000 if reference_price and confidence_abs is not None else None
    )
    for row in lineage_rows:
        row["normalized_weight"] = round(row.get("normalized_weight", 0.0), 12)

    return {
        "composite_id": composite_id,
        "canonical_asset_id": canonical_asset_id,
        "quote_currency": quote_currency,
        "price_type": "robust_reference",
        "reference_price": round(reference_price, 12) if reference_price is not None else None,
        "confidence_abs": round(confidence_abs, 12) if confidence_abs is not None else None,
        "confidence_bps": round(confidence_bps, 12) if confidence_bps is not None else None,
        "status": status,
        "independent_source_count": independent_count,
        "required_independent_source_count": min_independent_sources,
        "median": round(center, 12) if center is not None else None,
        "mad": round(mad, 12) if mad is not None else None,
        "event_time_min": min((row["event_time"] for row in included), default=None),
        "event_time_max": max((row["event_time"] for row in included), default=None),
        "receive_time": now.isoformat(),
        "lineage_observations": lineage_rows,
        "excluded_observations": excluded,
    }


def calculate_bidask(payload: dict[str, Any]) -> dict[str, Any]:
    """Normalize and score a bid/ask observation."""
    symbol = str(payload.get("symbol") or "").strip().upper()
    if not symbol:
        raise ValueError("symbol is required")
    venue = str(payload.get("venue") or "unknown").strip().lower()
    asset_class = str(payload.get("asset_class") or "equity").strip().lower()
    source_type = str(payload.get("source_type") or "native_l1").strip().lower()
    bid = _as_float(payload.get("bid"), "bid")
    ask = _as_float(payload.get("ask"), "ask")
    if bid <= 0 or ask <= 0:
        raise ValueError("bid and ask must be greater than zero")
    if ask < bid:
        raise ValueError("ask must be greater than or equal to bid")
    mid = (bid + ask) / 2
    spread = ask - bid
    spread_bps = spread / mid * 10_000 if mid else None
    benchmark = payload.get("benchmark_price")
    benchmark_drift_bps = None
    if benchmark is not None:
        benchmark_price = _as_float(benchmark, "benchmark_price")
        if benchmark_price > 0:
            benchmark_drift_bps = (mid - benchmark_price) / benchmark_price * 10_000
    age = _age_ms(payload.get("timestamp"))
    score, flags = _quality_score(
        asset_class=asset_class,
        source_type=source_type,
        age_ms=age,
        spread_bps=spread_bps,
        benchmark_drift_bps=benchmark_drift_bps,
    )
    return {
        "symbol": symbol,
        "venue": venue,
        "asset_class": asset_class,
        "source_type": source_type,
        "bid": bid,
        "ask": ask,
        "mid": round(mid, 10),
        "spread": round(spread, 10),
        "spread_bps": round(spread_bps or 0.0, 6),
        "benchmark_drift_bps": round(benchmark_drift_bps, 6) if benchmark_drift_bps is not None else None,
        "quality": {"score": score, "flags": flags, "age_ms": age},
    }


def detect_outliers(payload: dict[str, Any]) -> dict[str, Any]:
    """Score a set of venue observations with MAD and benchmark gates."""
    observations = payload.get("observations")
    if not isinstance(observations, list) or not observations:
        raise ValueError("observations must include at least one row")
    asset_class = str(payload.get("asset_class") or "equity").strip().lower()
    benchmark = payload.get("benchmark_price")
    benchmark_price = _as_float(benchmark, "benchmark_price") if benchmark is not None else None
    values: list[float] = []
    clean_rows: list[dict[str, Any]] = []
    for row in observations:
        if not isinstance(row, dict):
            continue
        value = _as_float(row.get("value"), "observation.value")
        if value <= 0:
            raise ValueError("observation.value must be greater than zero")
        clean = dict(row)
        clean["value"] = value
        clean_rows.append(clean)
        values.append(value)
    if not clean_rows:
        raise ValueError("observations must include object rows")

    center = median(values)
    absolute_deviations = [abs(value - center) for value in values]
    mad = median(absolute_deviations)
    exclude_drift = float(QUALITY_ALIGNMENT["thresholds"]["benchmark_drift_bps"]["exclude"])
    warning_drift = float(QUALITY_ALIGNMENT["thresholds"]["benchmark_drift_bps"]["warning"])

    results = []
    included_values = []
    for row in clean_rows:
        value = row["value"]
        robust_z = 0.0 if mad == 0 else 0.6745 * (value - center) / mad
        benchmark_drift_bps = None
        flags: list[str] = []
        if abs(robust_z) > 3.5:
            flags.append("mad_outlier")
        if benchmark_price:
            benchmark_drift_bps = (value - benchmark_price) / benchmark_price * 10_000
            if abs(benchmark_drift_bps) >= exclude_drift:
                flags.append("benchmark_drift_exclude")
            elif abs(benchmark_drift_bps) >= warning_drift:
                flags.append("benchmark_drift_warning")
        age = _age_ms(row.get("timestamp"))
        score, quality_flags = _quality_score(
            asset_class=asset_class,
            source_type=str(row.get("source_type") or "unknown"),
            age_ms=age,
            benchmark_drift_bps=benchmark_drift_bps,
        )
        flags.extend(flag for flag in quality_flags if flag not in flags)
        include = not {"mad_outlier", "benchmark_drift_exclude", "stale"}.intersection(flags)
        if include:
            included_values.append(value)
        results.append(
            {
                "symbol": str(row.get("symbol") or payload.get("symbol") or "").upper(),
                "venue": str(row.get("venue") or "unknown").lower(),
                "value": value,
                "source_type": str(row.get("source_type") or "unknown"),
                "robust_z": round(robust_z, 6),
                "benchmark_drift_bps": round(benchmark_drift_bps, 6) if benchmark_drift_bps is not None else None,
                "include_in_consolidated": include,
                "quality": {"score": score, "flags": flags, "age_ms": age},
            }
        )

    return {
        "asset_class": asset_class,
        "benchmark_price": benchmark_price,
        "median": round(center, 10),
        "mad": round(mad, 10),
        "included_count": len(included_values),
        "excluded_count": len(results) - len(included_values),
        "consolidated_value": round(median(included_values), 10) if included_values else None,
        "observations": results,
    }
