#!/usr/bin/env python3
"""Capture defensible volume, depth, and manipulation evidence for the RWA pilot.

The Hyperliquid lane uses native L2 plus venue-native rolling volume. EVM CLMM
lanes use block-pinned pool state, decoded Swap logs, and bounded initialized-
tick replay. Synthetic levels are excluded. Nothing here promotes a feed or
completes a sustained manipulation review.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import statistics
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

from scripts.run_rwa_growth_pilot import PILOT_FEEDS, capture_pilot
from src.rwa_adapters import build_default_registry


TARGET_NOTIONALS_USD = (1_000.0, 10_000.0, 50_000.0)
MAX_TOP_LEVEL_CONCENTRATION = 0.80
MAX_SIDE_IMBALANCE = 0.90
MAX_BOOK_TIMESTAMP_GAP_SECONDS = 5.0
MAX_NATIVE_SPREAD_BPS = 75.0
REQUIRED_NATIVE_BLOCK_USD = 10_000.0
MINIMUM_ORGANIC_24H_VOLUME_USD = 100_000.0
MAXIMUM_REQUIRED_BLOCK_SLIPPAGE_BPS = 100.0


def _finite(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _parse_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _spread_bps(observation: dict[str, Any]) -> float | None:
    bid = _finite(observation.get("bid"))
    ask = _finite(observation.get("ask"))
    if bid is None or ask is None or bid <= 0 or ask < bid:
        return None
    mid = (bid + ask) / 2
    return (ask - bid) / mid * 10_000 if mid else None


def _payload_hash(payload: Any) -> str:
    encoded = json.dumps(payload, default=str, sort_keys=True, separators=(",", ":")).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _level_notional(level: dict[str, Any]) -> float | None:
    explicit = _finite(level.get("notional_usd"))
    if explicit is not None and explicit >= 0:
        return explicit
    price = _finite(level.get("price"))
    size = _finite(level.get("size"))
    if price is None or size is None or price <= 0 or size < 0:
        return None
    return price * size


def _walk_book(levels: list[dict[str, Any]], target_usd: float, side: str) -> dict[str, Any]:
    remaining = target_usd
    filled = 0.0
    quantity = 0.0
    top_price = _finite(levels[0].get("price")) if levels else None
    for level in levels:
        price = _finite(level.get("price"))
        notional = _level_notional(level)
        if price is None or notional is None or price <= 0 or notional <= 0:
            continue
        consumed = min(remaining, notional)
        quantity += consumed / price
        filled += consumed
        remaining -= consumed
        if remaining <= 1e-9:
            break
    vwap = filled / quantity if quantity > 0 else None
    slippage_bps = None
    if top_price and vwap:
        slippage_bps = (
            (vwap - top_price) / top_price * 10_000
            if side == "buy"
            else (top_price - vwap) / top_price * 10_000
        )
    return {
        "target_notional_usd": target_usd,
        "filled_notional_usd": filled,
        "fill_ratio": filled / target_usd if target_usd > 0 else None,
        "vwap": vwap,
        "slippage_bps": slippage_bps,
    }


def _side_metrics(book: dict[str, Any], side: str) -> dict[str, Any]:
    levels = [row for row in (book.get("levels") or []) if isinstance(row, dict)]
    notionals = [value for row in levels if (value := _level_notional(row)) is not None]
    total = sum(notionals)
    top = notionals[0] if notionals else None
    return {
        "level_count": len(notionals),
        "visible_notional_usd": total,
        "top_level_notional_usd": top,
        "top_level_concentration": top / total if top is not None and total > 0 else None,
        "top_price": _finite(levels[0].get("price")) if levels else None,
        "target_fills": [_walk_book(levels, target, side) for target in TARGET_NOTIONALS_USD],
    }


def _native_l2_evidence(
    feed: dict[str, Any],
    capture: dict[str, Any],
    books: dict[str, Any],
) -> dict[str, Any]:
    buy_book = books.get("buy") if isinstance(books.get("buy"), dict) else None
    sell_book = books.get("sell") if isinstance(books.get("sell"), dict) else None
    activity = books.get("activity") if isinstance(books.get("activity"), dict) else None
    base = {
        **feed,
        "evidence_class": "native_l2_point_in_time",
        "production_promoted": False,
    }
    if buy_book is None or sell_book is None:
        return {
            **base,
            "status": "error",
            "error": books.get("error") or "native L2 sides unavailable",
            "point_in_time_depth_observed": False,
            "manipulation_review_complete": False,
        }
    buy = _side_metrics(buy_book, "buy")
    sell = _side_metrics(sell_book, "sell")
    ask = buy.get("top_price")
    bid = sell.get("top_price")
    crossed = bid is not None and ask is not None and bid > ask
    buy_ts = _parse_timestamp(buy_book.get("timestamp"))
    sell_ts = _parse_timestamp(sell_book.get("timestamp"))
    timestamp_gap = abs((buy_ts - sell_ts).total_seconds()) if buy_ts and sell_ts else None
    total_visible = buy["visible_notional_usd"] + sell["visible_notional_usd"]
    side_imbalance = (
        abs(buy["visible_notional_usd"] - sell["visible_notional_usd"]) / total_visible
        if total_visible > 0
        else None
    )
    spread_bps = _spread_bps(capture.get("raw_observation") or {})
    organic_volume = _finite((activity or {}).get("notional_volume_usd"))
    required_buy = next(
        row for row in buy["target_fills"] if row["target_notional_usd"] == REQUIRED_NATIVE_BLOCK_USD
    )
    required_sell = next(
        row for row in sell["target_fills"] if row["target_notional_usd"] == REQUIRED_NATIVE_BLOCK_USD
    )
    flags = ["single_venue_dependency", "sustained_depth_window_missing"]
    if activity is None:
        flags.append("trade_volume_window_missing")
    elif organic_volume is None or organic_volume < MINIMUM_ORGANIC_24H_VOLUME_USD:
        flags.append("organic_volume_below_threshold")
    if crossed:
        flags.append("crossed_book")
    if timestamp_gap is None or timestamp_gap > MAX_BOOK_TIMESTAMP_GAP_SECONDS:
        flags.append("book_sides_not_timestamp_aligned")
    if any(
        value is not None and value > MAX_TOP_LEVEL_CONCENTRATION
        for value in (buy["top_level_concentration"], sell["top_level_concentration"])
    ):
        flags.append("top_level_concentration_high")
    if side_imbalance is not None and side_imbalance > MAX_SIDE_IMBALANCE:
        flags.append("side_imbalance_high")
    if spread_bps is None or spread_bps > MAX_NATIVE_SPREAD_BPS:
        flags.append("wide_or_missing_spread")
    if required_buy["fill_ratio"] < 1 or required_sell["fill_ratio"] < 1:
        flags.append("required_block_partial_fill")
    point_in_time = (
        buy["level_count"] > 0
        and sell["level_count"] > 0
        and not crossed
        and timestamp_gap is not None
        and timestamp_gap <= MAX_BOOK_TIMESTAMP_GAP_SECONDS
    )
    point_in_time_quality_pass = (
        point_in_time
        and spread_bps is not None
        and spread_bps <= MAX_NATIVE_SPREAD_BPS
        and required_buy["fill_ratio"] == 1
        and required_sell["fill_ratio"] == 1
        and organic_volume is not None
        and organic_volume >= MINIMUM_ORGANIC_24H_VOLUME_USD
        and not any(flag in flags for flag in ("top_level_concentration_high", "side_imbalance_high"))
    )
    raw_depth_payload = {"buy": buy_book, "sell": sell_book}
    return {
        **base,
        "status": "warn" if point_in_time else "error",
        "point_in_time_depth_observed": point_in_time,
        "point_in_time_quality_pass": point_in_time_quality_pass,
        "quality_decision": "candidate_snapshot_pass" if point_in_time_quality_pass else "indicative_only_exclude",
        "manipulation_review_complete": False,
        "spread_bps": spread_bps,
        "book_timestamp_gap_seconds": timestamp_gap,
        "book_sides_sane": not crossed,
        "visible_depth": {"buy": buy, "sell": sell, "side_imbalance": side_imbalance},
        "organic_volume": activity,
        "replay_evidence": {
            "raw_depth_payload_hash": _payload_hash(raw_depth_payload),
            "raw_depth_payload": raw_depth_payload,
        },
        "risk_flags": sorted(set(flags)),
        "limitations": [
            "One public venue snapshot is not a sustained manipulation review.",
            "Visible depth excludes hidden liquidity and does not prove organic traded volume.",
            "Identity, issuer, independent benchmark, rights, and human approval remain separate gates.",
        ],
    }


def _onchain_state_evidence(
    feed: dict[str, Any],
    capture: dict[str, Any],
    inputs: dict[str, Any],
) -> dict[str, Any]:
    observation = capture.get("raw_observation") if isinstance(capture.get("raw_observation"), dict) else {}
    metadata = observation.get("metadata") if isinstance(observation.get("metadata"), dict) else {}
    required = {
        "pool_contract": bool(metadata.get("pool_contract")),
        "block_number": isinstance(metadata.get("block_number"), int),
        "liquidity": isinstance(metadata.get("liquidity"), int) and metadata.get("liquidity", 0) > 0,
        "tick": isinstance(metadata.get("tick"), int),
        "sqrt_price_x96": bool(metadata.get("sqrt_price_x96")),
        "token_pair": bool(metadata.get("token0")) and bool(metadata.get("token1")),
    }
    state_observed = capture.get("status") == "ok" and all(required.values())
    raw_pool_state = metadata.get("raw_payload") if isinstance(metadata.get("raw_payload"), dict) else {}
    replay = inputs.get("pool_replay") if isinstance(inputs.get("pool_replay"), dict) else None
    replay_error = inputs.get("error")
    fills = replay.get("target_fills") if isinstance(replay, dict) else {}
    buy_fills = fills.get("buy") if isinstance(fills, dict) and isinstance(fills.get("buy"), list) else []
    sell_fills = fills.get("sell") if isinstance(fills, dict) and isinstance(fills.get("sell"), list) else []
    required_buy = next(
        (row for row in buy_fills if row.get("target_notional_usd") == REQUIRED_NATIVE_BLOCK_USD),
        None,
    )
    required_sell = next(
        (row for row in sell_fills if row.get("target_notional_usd") == REQUIRED_NATIVE_BLOCK_USD),
        None,
    )
    tick_replay_observed = bool(
        replay
        and replay.get("initialized_tick_count", 0) > 0
        and required_buy
        and required_sell
    )
    required_block_pass = bool(
        tick_replay_observed
        and required_buy.get("fill_ratio") == 1
        and required_sell.get("fill_ratio") == 1
        and required_buy.get("captured_range_sufficient") is True
        and required_sell.get("captured_range_sufficient") is True
        and _finite(required_buy.get("slippage_bps")) is not None
        and _finite(required_sell.get("slippage_bps")) is not None
        and abs(float(required_buy["slippage_bps"])) <= MAXIMUM_REQUIRED_BLOCK_SLIPPAGE_BPS
        and abs(float(required_sell["slippage_bps"])) <= MAXIMUM_REQUIRED_BLOCK_SLIPPAGE_BPS
    )
    volume_window = replay.get("volume_window") if isinstance(replay, dict) else None
    organic_volume = _finite((volume_window or {}).get("quote_volume_usd"))
    volume_window_observed = bool(
        volume_window
        and volume_window.get("status") == "ok"
        and (volume_window.get("window_coverage_seconds") or 0) >= 82_800
    )
    organic_volume_pass = bool(
        volume_window_observed
        and organic_volume is not None
        and organic_volume >= MINIMUM_ORGANIC_24H_VOLUME_USD
    )
    point_in_time_quality_pass = required_block_pass and organic_volume_pass
    risk_flags = [
        "lp_or_holder_concentration_missing",
        "single_pool_dependency",
        "route_diversity_missing",
        "sustained_depth_window_missing",
    ]
    if not tick_replay_observed:
        risk_flags.append("synthetic_depth_excluded")
        risk_flags.append("tick_bitmap_and_liquidity_replay_missing")
    if not volume_window_observed:
        risk_flags.append("swap_volume_window_missing")
    elif not organic_volume_pass:
        risk_flags.append("organic_volume_below_threshold")
    if tick_replay_observed and not required_block_pass:
        risk_flags.append("required_block_fill_or_slippage_failed")
    return {
        **feed,
        "status": "warn" if state_observed else "error",
        "evidence_class": (
            "block_pinned_clmm_tick_and_swap_replay"
            if tick_replay_observed
            else "block_pinned_pool_state_not_executable_depth"
        ),
        "production_promoted": False,
        "point_in_time_pool_state_observed": state_observed,
        "point_in_time_depth_observed": tick_replay_observed,
        "point_in_time_tick_replay_observed": tick_replay_observed,
        "point_in_time_volume_window_observed": volume_window_observed,
        "point_in_time_quality_pass": point_in_time_quality_pass,
        "quality_decision": (
            "candidate_snapshot_pass"
            if point_in_time_quality_pass
            else "candidate_replay_failed_quality"
            if tick_replay_observed
            else "pool_state_only_executable_depth_unverified"
        ),
        "manipulation_review_complete": False,
        "spread_bps": _spread_bps(observation),
        "pool_state": {
            "chain": metadata.get("chain"),
            "pool_contract": metadata.get("pool_contract"),
            "block_number": metadata.get("block_number"),
            "fee_tier": metadata.get("fee_tier"),
            "tick_spacing": metadata.get("tick_spacing"),
            "active_liquidity_units": metadata.get("liquidity"),
            "discovery_liquidity_usd": metadata.get("discovery_liquidity_usd"),
            "required_fields": required,
        },
        "exact_tick_replay": {
            key: replay.get(key)
            for key in (
                "block_number",
                "tick_word_range",
                "tick_word_count",
                "initialized_tick_count",
                "initialized_ticks_truncated",
                "target_fills",
                "volume_window",
                "semantics",
            )
        } if replay else None,
        "replay_error": replay_error,
        "volume_replay_error": (
            volume_window.get("error")
            if isinstance(volume_window, dict) and volume_window.get("status") == "error"
            else None
        ),
        "replay_evidence": {
            "raw_pool_state_payload_hash": _payload_hash(raw_pool_state) if raw_pool_state else None,
            "raw_pool_state_payload": raw_pool_state,
            "tick_and_swap_payload_hash": (
                _payload_hash(replay.get("replay_payload")) if replay else None
            ),
            "tick_and_swap_payload": replay.get("replay_payload") if replay else None,
        },
        "risk_flags": sorted(set(risk_flags)),
        "limitations": [
            "Current active liquidity is not USD executable depth.",
            "Exact replay is bounded by the captured bitmap range and remains partial beyond it.",
            "Pool and holder concentration require additional onchain indexing and history.",
        ],
    }


def _history_stats(history: list[dict[str, Any]], rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def metric(row: dict[str, Any], name: str) -> float | None:
        if name == "volume":
            native = row.get("organic_volume") if isinstance(row.get("organic_volume"), dict) else {}
            replay = row.get("exact_tick_replay") if isinstance(row.get("exact_tick_replay"), dict) else {}
            window = replay.get("volume_window") if isinstance(replay.get("volume_window"), dict) else {}
            value = native.get("notional_volume_usd")
            if value is None:
                if window.get("status") != "ok":
                    return None
                value = window.get("quote_volume_usd")
            return _finite(value)
        pool = row.get("pool_state") if isinstance(row.get("pool_state"), dict) else {}
        return _finite(pool.get("active_liquidity_units"))

    def robust(values: list[float], current_value: float | None) -> dict[str, Any]:
        median = statistics.median(values) if values else None
        mad = (
            statistics.median(abs(value - median) for value in values)
            if median is not None
            else None
        )
        threshold = max(abs(median or 0) * 0.05, 6 * (mad or 0))
        return {
            "median": median,
            "mad": mad,
            "current": current_value,
            "current_robust_outlier": bool(
                len(values) >= 5
                and current_value is not None
                and median is not None
                and abs(current_value - median) > threshold
            ),
        }

    result = []
    for feed in PILOT_FEEDS:
        pilot_id = str(feed["pilot_id"])
        historic_rows = [
            row
            for report in history
            for row in (report.get("rows") or [])
            if isinstance(row, dict) and row.get("pilot_id") == pilot_id
        ]
        current = next((row for row in rows if row.get("pilot_id") == pilot_id), {})
        all_rows = [*historic_rows, current]
        spreads = [value for row in all_rows if (value := _finite(row.get("spread_bps"))) is not None]
        median = statistics.median(spreads) if spreads else None
        mad = statistics.median(abs(value - median) for value in spreads) if median is not None else None
        current_spread = _finite(current.get("spread_bps"))
        volumes = [value for row in all_rows if (value := metric(row, "volume")) is not None]
        liquidities = [
            value for row in all_rows if (value := metric(row, "liquidity")) is not None
        ]
        result.append(
            {
                "pilot_id": pilot_id,
                "snapshot_count": len(all_rows),
                "successful_snapshot_count": sum(row.get("status") in {"pass", "warn"} for row in all_rows),
                "spread_bps_median": median,
                "spread_bps_mad": mad,
                "current_spread_robust_outlier": bool(
                    len(spreads) >= 5
                    and current_spread is not None
                    and median is not None
                    and mad is not None
                    and current_spread > median + max(1.0, 6 * mad)
                ),
                "organic_volume": robust(volumes, metric(current, "volume")),
                "active_liquidity": robust(
                    liquidities,
                    metric(current, "liquidity"),
                ),
            }
        )
    return result


def evaluate_depth_evidence(
    captures: list[dict[str, Any]],
    books_by_pilot: dict[str, dict[str, Any]],
    *,
    history: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    rows = []
    for feed in PILOT_FEEDS:
        pilot_id = str(feed["pilot_id"])
        capture = next((row for row in captures if row.get("pilot_id") == pilot_id), None) or {}
        if feed["source_lane"] == "venue_api_order_book":
            rows.append(_native_l2_evidence(feed, capture, books_by_pilot.get(pilot_id, {})))
        else:
            rows.append(
                _onchain_state_evidence(
                    feed,
                    capture,
                    books_by_pilot.get(pilot_id, {}),
                )
            )
    native = [row for row in rows if row["evidence_class"] == "native_l2_point_in_time"]
    pool = [row for row in rows if row["source_lane"].endswith("rpc_pool_state")]
    return {
        "product": "rwa_pilot_depth_and_manipulation_evidence",
        "generated_at": datetime.now(UTC).isoformat(),
        "status": "point_in_time_evidence_candidate_only",
        "production_promoted_feed_count": 0,
        "summary": {
            "feeds_attempted": len(rows),
            "native_l2_point_in_time_depth_observed": sum(
                row.get("point_in_time_depth_observed") is True for row in native
            ),
            "pool_state_observed": sum(
                row.get("point_in_time_pool_state_observed") is True for row in pool
            ),
            "point_in_time_tick_replay_observed": sum(
                row.get("point_in_time_tick_replay_observed") is True for row in pool
            ),
            "point_in_time_volume_window_observed": sum(
                row.get("point_in_time_volume_window_observed") is True for row in rows
            ) + sum(
                isinstance(row.get("organic_volume"), dict) for row in native
            ),
            "executable_depth_observed_feed_count": sum(
                row.get("point_in_time_depth_observed") is True for row in rows
            ),
            "point_in_time_quality_pass_feed_count": sum(
                row.get("point_in_time_quality_pass") is True for row in rows
            ),
            "manipulation_review_complete_feed_count": 0,
            "production_promoted_feed_count": 0,
        },
        "gate_assessment": {
            "point_in_time_depth_or_state_evidence_collected": all(
                row.get("point_in_time_depth_observed") is True
                or row.get("point_in_time_pool_state_observed") is True
                for row in rows
            ),
            "sustained_depth_window_complete": False,
            "point_in_time_volume_evidence_collected": all(
                row.get("point_in_time_volume_window_observed") is True
                or isinstance(row.get("organic_volume"), dict)
                for row in rows
            ),
            "organic_volume_review_complete": False,
            "route_and_source_diversity_complete": False,
            "pool_or_holder_concentration_complete": False,
            "point_in_time_tick_liquidity_replay_collected": all(
                row.get("point_in_time_tick_replay_observed") is True for row in pool
            ),
            "tick_liquidity_replay_complete": False,
            "manipulation_and_depth_review_complete": False,
            "production_promotion_allowed": False,
        },
        "thresholds": {
            "target_notionals_usd": list(TARGET_NOTIONALS_USD),
            "maximum_top_level_concentration": MAX_TOP_LEVEL_CONCENTRATION,
            "maximum_side_imbalance": MAX_SIDE_IMBALANCE,
            "maximum_book_timestamp_gap_seconds": MAX_BOOK_TIMESTAMP_GAP_SECONDS,
            "maximum_native_spread_bps": MAX_NATIVE_SPREAD_BPS,
            "required_native_block_usd": REQUIRED_NATIVE_BLOCK_USD,
            "minimum_organic_24h_volume_usd": MINIMUM_ORGANIC_24H_VOLUME_USD,
            "maximum_required_block_slippage_bps": MAXIMUM_REQUIRED_BLOCK_SLIPPAGE_BPS,
            "robust_spread_outlier_rule": "current > median + max(1 bps, 6 * MAD), after 5 samples",
            "robust_volume_liquidity_outlier_rule": (
                "absolute deviation > max(5% of median, 6 * MAD), after 5 samples"
            ),
        },
        "history_statistics": _history_stats(history or [], rows),
        "rows": rows,
        "policy": {
            "automatic_promotion": False,
            "synthetic_depth_counts_as_executable": False,
            "pilot_runtime_tiingo_dependency": False,
        },
    }


async def capture_depth_inputs(
    registry: Any,
    captures: list[dict[str, Any]],
    *,
    timeout_seconds: float = 20.0,
) -> dict[str, dict[str, Any]]:
    """Capture native L2 sides; pool lanes reuse their block-pinned capture."""
    async def capture_feed(feed: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        pilot_id = str(feed["pilot_id"])
        capture = next((row for row in captures if row.get("pilot_id") == pilot_id), None)
        if not capture or capture.get("status") != "ok":
            return pilot_id, {"error": "pilot capture unavailable"}
        try:
            adapter = registry.get(feed["venue"])
            if feed["source_lane"] == "venue_api_order_book":
                buy, sell, activity = await asyncio.gather(
                    asyncio.wait_for(
                        adapter.fetch_order_book(feed["symbol"], side="buy", depth=20),
                        timeout=timeout_seconds,
                    ),
                    asyncio.wait_for(
                        adapter.fetch_order_book(feed["symbol"], side="sell", depth=20),
                        timeout=timeout_seconds,
                    ),
                    asyncio.wait_for(
                        adapter.fetch_market_activity(feed["symbol"]),
                        timeout=timeout_seconds,
                    ),
                )
                return pilot_id, {"buy": buy, "sell": sell, "activity": activity}
            replay = await asyncio.wait_for(
                adapter.fetch_pool_replay_evidence(
                    feed["symbol"],
                    capture["raw_observation"],
                ),
                timeout=max(60.0, timeout_seconds * 4),
            )
            return pilot_id, {"pool_replay": replay}
        except Exception as exc:
            return pilot_id, {"error": f"{type(exc).__name__}: {str(exc)[:1000]}"}

    return dict(await asyncio.gather(*(capture_feed(feed) for feed in PILOT_FEEDS)))


def load_depth_history(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def persist_depth_report(
    report: dict[str, Any],
    *,
    history_path: Path,
    latest_path: Path,
) -> dict[str, str]:
    history_path.parent.mkdir(parents=True, exist_ok=True)
    latest_path.parent.mkdir(parents=True, exist_ok=True)
    with history_path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(report, sort_keys=True, default=str) + "\n")
    latest_path.write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )
    return {"history_path": str(history_path), "latest_path": str(latest_path)}


async def run(timeout: float, history_path: Path) -> dict[str, Any]:
    load_dotenv()
    registry = build_default_registry()
    captures = await capture_pilot(registry, timeout_seconds=timeout)
    books = await capture_depth_inputs(registry, captures, timeout_seconds=timeout)
    return evaluate_depth_evidence(captures, books, history=load_depth_history(history_path))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument(
        "--history",
        type=Path,
        default=Path("reports/agentic_marketing/rwa_pilot_depth_history.jsonl"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("reports/agentic_marketing/rwa_pilot_depth_latest.json"),
    )
    parser.add_argument("--require-point-in-time-evidence", action="store_true")
    args = parser.parse_args()
    report = asyncio.run(run(max(1.0, args.timeout), args.history))
    persist_depth_report(report, history_path=args.history, latest_path=args.output)
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    if args.require_point_in_time_evidence and not report["gate_assessment"][
        "point_in_time_depth_or_state_evidence_collected"
    ]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
