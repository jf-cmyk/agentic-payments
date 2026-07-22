#!/usr/bin/env python3
"""Compare Blocksize and Tiingo equity snapshots over the same timed window."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any, Awaitable, Callable

from src.blocksize_client import BlocksizeClient
from src.tiingo_client import TiingoClient


def _now() -> datetime:
    return datetime.now(UTC)


def _parse_timestamp(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * fraction
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return ordered[lower]
    weight = index - lower
    return ordered[lower] * (1 - weight) + ordered[upper] * weight


def _stats(values: list[float]) -> dict[str, float | int | None]:
    clean = [value for value in values if math.isfinite(value)]
    if not clean:
        return {"count": 0, "min": None, "median": None, "p95": None, "max": None}
    return {
        "count": len(clean),
        "min": round(min(clean), 6),
        "median": round(median(clean), 6),
        "p95": round(_percentile(clean, 0.95) or 0.0, 6),
        "max": round(max(clean), 6),
    }


async def _timed(call: Callable[[], Awaitable[dict[str, Any]]]) -> dict[str, Any]:
    started = perf_counter()
    try:
        observation = await call()
    except Exception as exc:
        return {
            "status": "error",
            "error_type": type(exc).__name__,
            "error": str(exc),
            "latency_ms": round((perf_counter() - started) * 1000, 6),
            "received_at": _now().isoformat(),
        }
    return {
        "status": "ok",
        "latency_ms": round((perf_counter() - started) * 1000, 6),
        "received_at": _now().isoformat(),
        "observation": observation,
    }


async def _fetch_blocksize(client: BlocksizeClient, symbol: str) -> dict[str, Any]:
    item = await client.get_bidask_snapshot(symbol)
    mid = (item.bid + item.ask) / 2 if item.bid > 0 and item.ask > 0 else None
    return {
        "ticker": item.pair,
        "bid": item.bid,
        "ask": item.ask,
        "mid": mid,
        "reference_price": mid,
        "reference_price_field": "bid_ask_mid",
        "timestamp": item.timestamp.isoformat(),
        "spread": item.spread,
        "spread_pct": item.spread_pct,
        "source": "blocksize",
    }


async def _collect_once(
    sequence: int,
    scheduled_offset_seconds: float,
    *,
    blocksize: BlocksizeClient,
    tiingo: TiingoClient,
    blocksize_symbol: str,
    tiingo_symbol: str,
) -> dict[str, Any]:
    blocksize_row, tiingo_iex_row, tiingo_consolidated_row = await asyncio.gather(
        _timed(lambda: _fetch_blocksize(blocksize, blocksize_symbol)),
        _timed(lambda: tiingo.get_equity_snapshot(tiingo_symbol)),
        _timed(lambda: tiingo.get_consolidated_equity_snapshot(tiingo_symbol)),
    )
    return {
        "sequence": sequence,
        "scheduled_offset_seconds": round(scheduled_offset_seconds, 6),
        "blocksize": blocksize_row,
        "tiingo_iex": tiingo_iex_row,
        "tiingo_consolidated": tiingo_consolidated_row,
    }


def _signature(provider: str, observation: dict[str, Any]) -> tuple[Any, ...]:
    if provider == "blocksize":
        return observation.get("bid"), observation.get("ask"), observation.get("mid")
    return (
        observation.get("bid"),
        observation.get("ask"),
        observation.get("reference_price"),
        observation.get("reference_price_field"),
    )


def _summarize_provider(
    samples: list[dict[str, Any]], provider: str, duration_seconds: float
) -> dict[str, Any]:
    rows = [sample[provider] for sample in samples]
    ok = [row for row in rows if row.get("status") == "ok"]
    timestamp_values: list[datetime] = []
    freshness_ms: list[float] = []
    for row in ok:
        observation = row["observation"]
        timestamp = _parse_timestamp(observation.get("timestamp"))
        received = _parse_timestamp(row.get("received_at"))
        if timestamp is not None:
            timestamp_values.append(timestamp)
            if received is not None:
                freshness_ms.append((received - timestamp).total_seconds() * 1000)

    distinct_timestamps = sorted(set(timestamp_values))
    timestamp_intervals_ms = [
        (right - left).total_seconds() * 1000
        for left, right in zip(distinct_timestamps, distinct_timestamps[1:])
    ]
    quote_changes = sum(
        1
        for left, right in zip(ok, ok[1:])
        if _signature(provider, left["observation"])
        != _signature(provider, right["observation"])
    )
    updates = max(0, len(distinct_timestamps) - 1)
    return {
        "attempts": len(rows),
        "ok": len(ok),
        "errors": len(rows) - len(ok),
        "success_rate": round(len(ok) / len(rows), 6) if rows else 0.0,
        "distinct_source_timestamps": len(distinct_timestamps),
        "source_timestamp_updates": updates,
        "quote_value_changes": quote_changes,
        "observed_source_update_hz": round(updates / max(duration_seconds, 1.0), 9),
        "duplicate_timestamp_samples": len(ok) - len(distinct_timestamps),
        "source_update_interval_ms": _stats(timestamp_intervals_ms),
        "freshness_ms": _stats(freshness_ms),
        "latency_ms": _stats([float(row["latency_ms"]) for row in ok]),
        "reference_price_fields": sorted(
            {
                str(row["observation"].get("reference_price_field"))
                for row in ok
                if row["observation"].get("reference_price_field")
            }
        ),
        "last_error": next(
            (row.get("error") for row in reversed(rows) if row.get("status") == "error"),
            None,
        ),
    }


def _build_comparison(samples: list[dict[str, Any]]) -> dict[str, Any]:
    paired: list[dict[str, Any]] = []
    blocksize_iex_basis_bps: list[float] = []
    blocksize_consolidated_basis_bps: list[float] = []
    iex_consolidated_basis_bps: list[float] = []
    blocksize_consolidated_time_delta_ms: list[float] = []
    bid_offsets_bps: list[float] = []
    ask_offsets_bps: list[float] = []
    consolidated_spread_bps: list[float] = []
    for sample in samples:
        blocksize = sample["blocksize"]
        tiingo_iex = sample["tiingo_iex"]
        tiingo_consolidated = sample["tiingo_consolidated"]
        if any(
            row.get("status") != "ok"
            for row in (blocksize, tiingo_iex, tiingo_consolidated)
        ):
            continue
        block_observation = blocksize["observation"]
        iex_observation = tiingo_iex["observation"]
        consolidated_observation = tiingo_consolidated["observation"]
        block_price = block_observation.get("reference_price")
        iex_price = iex_observation.get("reference_price")
        consolidated_price = consolidated_observation.get("reference_price")
        if (
            block_price is None
            or iex_price in {None, 0}
            or consolidated_price in {None, 0}
        ):
            continue
        block_iex_basis = (
            (float(block_price) - float(iex_price)) / float(iex_price) * 10_000
        )
        block_consolidated_basis = (
            (float(block_price) - float(consolidated_price))
            / float(consolidated_price)
            * 10_000
        )
        iex_consolidated_basis = (
            (float(iex_price) - float(consolidated_price))
            / float(consolidated_price)
            * 10_000
        )
        blocksize_iex_basis_bps.append(block_iex_basis)
        blocksize_consolidated_basis_bps.append(block_consolidated_basis)
        iex_consolidated_basis_bps.append(iex_consolidated_basis)

        block_timestamp = _parse_timestamp(block_observation.get("timestamp"))
        consolidated_timestamp = _parse_timestamp(consolidated_observation.get("timestamp"))
        timestamp_delta = None
        if block_timestamp is not None and consolidated_timestamp is not None:
            timestamp_delta = (
                (block_timestamp - consolidated_timestamp).total_seconds() * 1000
            )
            blocksize_consolidated_time_delta_ms.append(timestamp_delta)

        block_mid = block_observation.get("mid")
        block_bid = block_observation.get("bid")
        block_ask = block_observation.get("ask")
        if block_mid not in {None, 0} and block_bid is not None and block_ask is not None:
            bid_offsets_bps.append((float(block_mid) - float(block_bid)) / float(block_mid) * 10_000)
            ask_offsets_bps.append((float(block_ask) - float(block_mid)) / float(block_mid) * 10_000)

        consolidated_bid = consolidated_observation.get("bid")
        consolidated_ask = consolidated_observation.get("ask")
        if consolidated_bid is not None and consolidated_ask is not None:
            consolidated_spread_bps.append(
                (float(consolidated_ask) - float(consolidated_bid))
                / float(consolidated_price)
                * 10_000
            )

        paired.append(
            {
                "sequence": sample["sequence"],
                "blocksize_timestamp": block_observation.get("timestamp"),
                "tiingo_iex_timestamp": iex_observation.get("timestamp"),
                "tiingo_consolidated_timestamp": consolidated_observation.get("timestamp"),
                "blocksize_mid": block_price,
                "tiingo_iex_reference_price": iex_price,
                "tiingo_consolidated_reference_price": consolidated_price,
                "tiingo_consolidated_bid": consolidated_bid,
                "tiingo_consolidated_ask": consolidated_ask,
                "blocksize_minus_tiingo_iex_basis_bps": round(block_iex_basis, 6),
                "blocksize_minus_tiingo_consolidated_basis_bps": round(
                    block_consolidated_basis, 6
                ),
                "tiingo_iex_minus_consolidated_basis_bps": round(
                    iex_consolidated_basis, 6
                ),
                "blocksize_minus_consolidated_timestamp_ms": (
                    round(timestamp_delta, 6) if timestamp_delta is not None else None
                ),
            }
        )

    return {
        "paired_samples": len(paired),
        "blocksize_minus_tiingo_iex_basis_bps": _stats(blocksize_iex_basis_bps),
        "blocksize_minus_tiingo_consolidated_basis_bps": _stats(
            blocksize_consolidated_basis_bps
        ),
        "tiingo_iex_minus_consolidated_basis_bps": _stats(
            iex_consolidated_basis_bps
        ),
        "blocksize_minus_consolidated_source_timestamp_ms": _stats(
            blocksize_consolidated_time_delta_ms
        ),
        "blocksize_bid_offset_from_mid_bps": _stats(bid_offsets_bps),
        "blocksize_ask_offset_from_mid_bps": _stats(ask_offsets_bps),
        "tiingo_consolidated_liquidity_spread_bps": _stats(consolidated_spread_bps),
        "paired_observations": paired,
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    tiingo = TiingoClient(timeout=args.timeout)
    blocksize = BlocksizeClient(timeout=args.timeout)
    started_at = _now()
    start_clock = perf_counter()
    iteration_count = max(1, math.ceil(args.duration_seconds / args.interval_seconds))
    tasks: list[asyncio.Task[dict[str, Any]]] = []
    try:
        for sequence in range(iteration_count):
            target = start_clock + sequence * args.interval_seconds
            await asyncio.sleep(max(0.0, target - perf_counter()))
            tasks.append(
                asyncio.create_task(
                    _collect_once(
                        sequence,
                        perf_counter() - start_clock,
                        blocksize=blocksize,
                        tiingo=tiingo,
                        blocksize_symbol=args.blocksize_symbol,
                        tiingo_symbol=args.symbol,
                    )
                )
            )
        samples = await asyncio.gather(*tasks)
    finally:
        await asyncio.gather(blocksize.close(), tiingo.close())

    ended_at = _now()
    observed_duration = max(args.duration_seconds, (ended_at - started_at).total_seconds())
    report = {
        "product": "blocksize_tiingo_iex_consolidated_equity_comparison",
        "generated_at": ended_at.isoformat(),
        "config": {
            "symbol": args.symbol,
            "blocksize_symbol": args.blocksize_symbol,
            "duration_seconds": args.duration_seconds,
            "interval_seconds": args.interval_seconds,
            "timeout_seconds": args.timeout,
            "nbbo_status": (
                "not_available_from_entitled_fields; tiingo consolidated liquidity bid/ask "
                "is used as a labeled proxy, not as verified SIP NBBO"
            ),
        },
        "window": {
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "scheduled_duration_seconds": args.duration_seconds,
            "observed_duration_seconds": round((ended_at - started_at).total_seconds(), 6),
        },
        "feeds": {
            "blocksize": _summarize_provider(samples, "blocksize", observed_duration),
            "tiingo_iex": _summarize_provider(samples, "tiingo_iex", observed_duration),
            "tiingo_consolidated": _summarize_provider(
                samples, "tiingo_consolidated", observed_duration
            ),
        },
        "comparison": _build_comparison(samples),
        "samples": samples,
    }
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(output), "feeds": report["feeds"], "comparison": {key: value for key, value in report["comparison"].items() if key != "paired_observations"}}, indent=2))
    return report


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--symbol", default="LCID", help="Tiingo equity ticker")
    parser.add_argument(
        "--blocksize-symbol",
        default="LCIDUSD",
        help="Blocksize shared bid/ask symbol",
    )
    parser.add_argument("--duration-seconds", type=float, default=60.0)
    parser.add_argument("--interval-seconds", type=float, default=1.0)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--output")
    args = parser.parse_args()
    if args.duration_seconds <= 0 or args.interval_seconds <= 0:
        parser.error("duration and interval must both be positive")
    if args.output is None:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        args.output = f"reports/blocksize_tiingo_{args.symbol.upper()}_{stamp}.json"
    return args


def main() -> None:
    try:
        asyncio.run(run(parse_args()))
    except KeyboardInterrupt:
        raise SystemExit(130) from None


if __name__ == "__main__":
    main()
