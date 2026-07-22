#!/usr/bin/env python3
"""Run a timed feed-quality window against Blocksize and live RWA adapters."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from time import perf_counter
from typing import Any, Awaitable, Callable

from src.blocksize_client import BlocksizeAPIError, BlocksizeClient
from src.rwa_adapters import KrakenXStocksAdapter
from src.rwa_blocksize_benchmark import compare_observation_to_blocksize
from src.rwa_coverage import QUALITY_ALIGNMENT
from src.rwa_realtime_quality import evaluate_realtime_quality


@dataclass(frozen=True)
class FeedSpec:
    feed_id: str
    source: str
    kind: str
    symbol: str
    benchmark_service: str | None = None
    benchmark_symbol: str | None = None


DEFAULT_FEEDS = [
    FeedSpec("blocksize:vwap:BTCUSD", "blocksize", "vwap", "BTCUSD", "bidask", "BTCUSD"),
    FeedSpec("blocksize:vwap:ETHUSD", "blocksize", "vwap", "ETHUSD", "bidask", "ETHUSD"),
    FeedSpec("blocksize:bidask:BTCUSD", "blocksize", "bidask", "BTCUSD"),
    FeedSpec("blocksize:bidask:ETHUSD", "blocksize", "bidask", "ETHUSD"),
    FeedSpec("blocksize:bidask:AAPL", "blocksize", "bidask", "AAPL"),
    FeedSpec("blocksize:bidask:NVDA", "blocksize", "bidask", "NVDA"),
    FeedSpec("blocksize:fx:EURUSD", "blocksize", "fx", "EURUSD"),
    FeedSpec("blocksize:metal:XAUUSD", "blocksize", "metal", "XAUUSD"),
    FeedSpec("rwa:kraken_xstocks:bidask:AAPL", "kraken_xstocks", "bidask", "AAPL/USD", "bidask", "AAPL"),
    FeedSpec("rwa:kraken_xstocks:vwap:AAPL", "kraken_xstocks", "vwap", "AAPL/USD", "bidask", "AAPL"),
]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _iso_now() -> str:
    return _utc_now().isoformat()


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
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    return None


def _age_ms(timestamp: datetime | None, *, now: datetime) -> float | None:
    if timestamp is None:
        return None
    return max(0.0, (now - timestamp).total_seconds() * 1000)


def _percentile(values: list[float], pct: float) -> float | None:
    clean = sorted(value for value in values if math.isfinite(value))
    if not clean:
        return None
    if len(clean) == 1:
        return round(clean[0], 6)
    index = (len(clean) - 1) * pct
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return round(clean[lower], 6)
    weight = index - lower
    return round(clean[lower] * (1 - weight) + clean[upper] * weight, 6)


def _stats(values: list[float]) -> dict[str, Any]:
    clean = [value for value in values if value is not None and math.isfinite(value)]
    if not clean:
        return {"count": 0, "min": None, "median": None, "p95": None, "max": None}
    return {
        "count": len(clean),
        "min": round(min(clean), 6),
        "median": round(median(clean), 6),
        "p95": _percentile(clean, 0.95),
        "max": round(max(clean), 6),
    }


def _mid_from_bidask(row: dict[str, Any]) -> float | None:
    if row.get("mid") is not None:
        return float(row["mid"])
    if row.get("bid") is not None and row.get("ask") is not None:
        return (float(row["bid"]) + float(row["ask"])) / 2
    return None


def _value_from_observation(row: dict[str, Any]) -> float | None:
    for key in ("value", "mid", "vwap", "price", "last"):
        if row.get(key) is not None:
            return float(row[key])
    return _mid_from_bidask(row)


def _to_snapshot(service: str, symbol: str, row: dict[str, Any]) -> dict[str, Any]:
    value = _value_from_observation(row)
    data = {"value": value}
    if row.get("bid") is not None:
        data["bid"] = row["bid"]
    if row.get("ask") is not None:
        data["ask"] = row["ask"]
    if row.get("mid") is not None:
        data["mid"] = row["mid"]
    if row.get("vwap") is not None:
        data["vwap"] = row["vwap"]
    return {
        "service": service,
        "symbol": symbol,
        "endpoint": row.get("endpoint"),
        "timestamp": row.get("timestamp"),
        "value": value,
        "data": data,
    }


async def _timed(call: Callable[[], Awaitable[dict[str, Any]]]) -> tuple[dict[str, Any], float]:
    start = perf_counter()
    row = await call()
    return row, (perf_counter() - start) * 1000


async def _fetch_blocksize(client: BlocksizeClient, spec: FeedSpec) -> dict[str, Any]:
    if spec.kind == "vwap":
        item = await client.get_vwap_latest(spec.symbol)
        return {
            "symbol": item.pair,
            "venue": "blocksize",
            "source_type": "blocksize_vwap",
            "asset_class": "crypto",
            "kind": "vwap",
            "vwap": item.vwap,
            "value": item.vwap,
            "timestamp": item.timestamp.isoformat(),
            "endpoint": "vwap_latest",
        }
    if spec.kind == "bidask":
        item = await client.get_bidask_snapshot(spec.symbol)
        mid = (item.bid + item.ask) / 2 if item.ask > 0 else 0.0
        return {
            "symbol": item.pair,
            "venue": "blocksize",
            "source_type": "blocksize_bidask",
            "asset_class": "multi_asset",
            "kind": "bidask",
            "bid": item.bid,
            "ask": item.ask,
            "mid": mid,
            "value": mid,
            "spread": item.spread,
            "spread_pct": item.spread_pct,
            "timestamp": item.timestamp.isoformat(),
            "endpoint": "bidask_getSnapshot",
        }
    if spec.kind == "fx":
        item = await client.get_fx_rate(spec.symbol)
        return {
            "symbol": item.pair,
            "venue": "blocksize",
            "source_type": "blocksize_fx",
            "asset_class": "fx",
            "kind": "bidask",
            "bid": item.bid,
            "ask": item.ask,
            "mid": item.mid,
            "value": item.mid,
            "timestamp": item.timestamp.isoformat(),
            "endpoint": "bidask_getSnapshot",
        }
    if spec.kind == "metal":
        item = await client.get_metal_price(spec.symbol)
        return {
            "symbol": item.ticker,
            "venue": "blocksize",
            "source_type": "blocksize_metal",
            "asset_class": "metal",
            "kind": "bidask",
            "price": item.price,
            "value": item.price,
            "timestamp": item.timestamp.isoformat(),
            "endpoint": "bidask_getSnapshot",
        }
    raise ValueError(f"unsupported Blocksize feed kind: {spec.kind}")


async def _fetch_kraken(adapter: KrakenXStocksAdapter, spec: FeedSpec) -> dict[str, Any]:
    if spec.kind == "bidask":
        row = await adapter.fetch_bidask(spec.symbol)
        row["kind"] = "bidask"
        row["mid"] = _mid_from_bidask(row)
        row["value"] = row["mid"]
        row["receipt_timestamp"] = _iso_now()
        row["endpoint"] = "Kraken /0/public/Ticker"
        return row
    if spec.kind == "vwap":
        order_book = await adapter.fetch_order_book(spec.symbol, side="buy", depth=100)
        from src.rwa_pricing import calculate_block_vwap

        vwap = calculate_block_vwap({**order_book, "block_size_usd": 10_000})
        return {
            **order_book,
            "kind": "vwap",
            "vwap": vwap.get("vwap"),
            "value": vwap.get("vwap"),
            "fill_status": vwap.get("status"),
            "fillable_notional_usd": vwap.get("fillable_notional_usd"),
            "receipt_timestamp": _iso_now(),
            "endpoint": "Kraken /0/public/Depth",
        }
    raise ValueError(f"unsupported Kraken feed kind: {spec.kind}")


async def _fetch_spec(
    spec: FeedSpec,
    *,
    blocksize: BlocksizeClient,
    kraken: KrakenXStocksAdapter,
) -> tuple[dict[str, Any], float]:
    if spec.source == "blocksize":
        return await _timed(lambda: _fetch_blocksize(blocksize, spec))
    if spec.source == "kraken_xstocks":
        return await _timed(lambda: _fetch_kraken(kraken, spec))
    raise ValueError(f"unsupported source: {spec.source}")


async def _run_once(
    specs: list[FeedSpec],
    *,
    blocksize: BlocksizeClient,
    kraken: KrakenXStocksAdapter,
) -> list[dict[str, Any]]:
    sampled_at = _utc_now()
    tasks = [_fetch_spec(spec, blocksize=blocksize, kraken=kraken) for spec in specs]
    raw_results = await asyncio.gather(*tasks, return_exceptions=True)
    rows: list[dict[str, Any]] = []
    for spec, result in zip(specs, raw_results):
        if isinstance(result, Exception):
            rows.append(
                {
                    "feed_id": spec.feed_id,
                    "source": spec.source,
                    "kind": spec.kind,
                    "symbol": spec.symbol,
                    "sampled_at": sampled_at.isoformat(),
                    "status": "error",
                    "error_type": type(result).__name__,
                    "error": str(result),
                }
            )
            continue
        observation, latency_ms = result
        timestamp = _parse_timestamp(observation.get("timestamp"))
        freshness_ms = _age_ms(timestamp, now=_utc_now())
        quality = None
        if spec.source != "blocksize":
            try:
                quality = evaluate_realtime_quality(
                    {"now": _iso_now(), "observations": [observation]}
                )["observations"][0]
            except Exception as exc:
                quality = {"usable_for_realtime": False, "flags": [f"quality_error:{exc}"]}
        rows.append(
            {
                "feed_id": spec.feed_id,
                "source": spec.source,
                "kind": spec.kind,
                "symbol": spec.symbol,
                "sampled_at": sampled_at.isoformat(),
                "status": "ok",
                "latency_ms": round(latency_ms, 6),
                "freshness_ms": round(freshness_ms, 6) if freshness_ms is not None else None,
                "source_timestamp": timestamp.isoformat() if timestamp else None,
                "observation": observation,
                "realtime_quality": quality,
            }
        )
    return rows


def _build_comparisons(samples: list[dict[str, Any]], specs: list[FeedSpec]) -> list[dict[str, Any]]:
    spec_by_id = {spec.feed_id: spec for spec in specs}
    by_sample_time: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
    for row in samples:
        if row.get("status") == "ok":
            by_sample_time[str(row["sampled_at"])][str(row["feed_id"])] = row

    comparisons: list[dict[str, Any]] = []
    for sampled_at, rows in by_sample_time.items():
        benchmark_rows: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows.values():
            spec = spec_by_id[str(row["feed_id"])]
            service = spec.kind if spec.source == "blocksize" else None
            if spec.kind in {"fx", "metal"}:
                service = spec.kind
            if spec.source == "blocksize" and service:
                benchmark_rows[(service, spec.symbol.upper())] = row
                if spec.kind == "bidask":
                    benchmark_rows[("bidask", spec.symbol.upper())] = row

        for row in rows.values():
            spec = spec_by_id[str(row["feed_id"])]
            if not spec.benchmark_service or not spec.benchmark_symbol:
                continue
            benchmark = benchmark_rows.get((spec.benchmark_service, spec.benchmark_symbol.upper()))
            if benchmark is None:
                continue
            try:
                comparison = compare_observation_to_blocksize(
                    row["observation"],
                    _to_snapshot(
                        spec.benchmark_service,
                        spec.benchmark_symbol,
                        benchmark["observation"],
                    ),
                )
                comparisons.append(
                    {
                        "sampled_at": sampled_at,
                        "feed_id": row["feed_id"],
                        "benchmark_feed_id": benchmark["feed_id"],
                        **comparison,
                    }
                )
            except (BlocksizeAPIError, ValueError) as exc:
                comparisons.append(
                    {
                        "sampled_at": sampled_at,
                        "feed_id": row["feed_id"],
                        "benchmark_feed_id": benchmark["feed_id"],
                        "status": "error",
                        "error": str(exc),
                    }
                )
    return comparisons


def _summarize(samples: list[dict[str, Any]], comparisons: list[dict[str, Any]], started_at: datetime, ended_at: datetime) -> dict[str, Any]:
    by_feed: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for sample in samples:
        by_feed[str(sample["feed_id"])].append(sample)

    comparison_by_feed: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in comparisons:
        if item.get("basis_bps") is not None:
            comparison_by_feed[str(item["feed_id"])].append(item)

    feed_summaries: dict[str, Any] = {}
    for feed_id, rows in sorted(by_feed.items()):
        ok_rows = [row for row in rows if row.get("status") == "ok"]
        timestamps = [
            _parse_timestamp(row.get("source_timestamp"))
            for row in ok_rows
            if row.get("source_timestamp")
        ]
        distinct_timestamps = sorted({ts for ts in timestamps if ts is not None})
        intervals_ms = [
            (right - left).total_seconds() * 1000
            for left, right in zip(distinct_timestamps, distinct_timestamps[1:])
        ]
        realtime_flags = Counter(
            flag
            for row in ok_rows
            for flag in ((row.get("realtime_quality") or {}).get("flags") or [])
        )
        comp_rows = comparison_by_feed.get(feed_id, [])
        decisions = Counter(str(row.get("decision")) for row in comp_rows if row.get("decision"))
        feed_summaries[feed_id] = {
            "attempts": len(rows),
            "ok": len(ok_rows),
            "errors": len(rows) - len(ok_rows),
            "success_rate": round(len(ok_rows) / len(rows), 6) if rows else 0,
            "latency_ms": _stats([float(row["latency_ms"]) for row in ok_rows if row.get("latency_ms") is not None]),
            "freshness_ms": _stats([float(row["freshness_ms"]) for row in ok_rows if row.get("freshness_ms") is not None]),
            "source_tick_updates": max(0, len(distinct_timestamps) - 1),
            "source_tick_interval_ms": _stats(intervals_ms),
            "observed_tick_frequency_hz": round(
                max(0, len(distinct_timestamps) - 1) / max(1.0, (ended_at - started_at).total_seconds()),
                9,
            ),
            "deviation_bps": _stats([float(row["basis_bps"]) for row in comp_rows if row.get("basis_bps") is not None]),
            "abs_deviation_bps": _stats([float(row["abs_basis_bps"]) for row in comp_rows if row.get("abs_basis_bps") is not None]),
            "alignment_decisions": dict(sorted(decisions.items())),
            "realtime_flags": dict(sorted(realtime_flags.items())),
            "last_error": next((row.get("error") for row in reversed(rows) if row.get("status") == "error"), None),
        }

    thresholds = QUALITY_ALIGNMENT["thresholds"]["benchmark_drift_bps"]
    return {
        "window": {
            "started_at": started_at.isoformat(),
            "ended_at": ended_at.isoformat(),
            "duration_seconds": round((ended_at - started_at).total_seconds(), 6),
        },
        "thresholds": {
            "benchmark_warning_bps": thresholds["warning"],
            "benchmark_exclude_bps": thresholds["exclude"],
        },
        "summary": {
            "feed_count": len(by_feed),
            "sample_count": len(samples),
            "comparison_count": len(comparisons),
            "ok_samples": len([row for row in samples if row.get("status") == "ok"]),
            "error_samples": len([row for row in samples if row.get("status") != "ok"]),
            "alignment_decisions": dict(
                sorted(Counter(str(row.get("decision")) for row in comparisons if row.get("decision")).items())
            ),
        },
        "feeds": feed_summaries,
    }


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


async def run(args: argparse.Namespace) -> dict[str, Any]:
    specs = DEFAULT_FEEDS
    started_at = _utc_now()
    ended_at = started_at
    samples: list[dict[str, Any]] = []
    blocksize = BlocksizeClient(timeout=args.timeout)
    kraken = KrakenXStocksAdapter()
    output = Path(args.output)
    try:
        iteration = 0
        while True:
            iteration += 1
            rows = await _run_once(specs, blocksize=blocksize, kraken=kraken)
            samples.extend(rows)
            ended_at = _utc_now()
            comparisons = _build_comparisons(samples, specs)
            report = {
                "product": "feed_quality_window",
                "generated_at": ended_at.isoformat(),
                "config": {
                    "duration_seconds": args.duration_seconds,
                    "interval_seconds": args.interval_seconds,
                    "timeout_seconds": args.timeout,
                    "feeds": [asdict(spec) for spec in specs],
                },
                **_summarize(samples, comparisons, started_at, ended_at),
                "samples": samples if args.include_samples else [],
                "comparisons": comparisons if args.include_samples else comparisons[-100:],
            }
            _write_report(output, report)
            print(
                json.dumps(
                    {
                        "iteration": iteration,
                        "elapsed_seconds": round((ended_at - started_at).total_seconds(), 3),
                        "ok": report["summary"]["ok_samples"],
                        "errors": report["summary"]["error_samples"],
                        "comparisons": report["summary"]["comparison_count"],
                        "alignment": report["summary"]["alignment_decisions"],
                        "output": str(output),
                    },
                    sort_keys=True,
                ),
                flush=True,
            )
            if (ended_at - started_at).total_seconds() >= args.duration_seconds:
                return report
            await asyncio.sleep(args.interval_seconds)
    finally:
        await blocksize.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-seconds", type=int, default=1800)
    parser.add_argument("--interval-seconds", type=float, default=30.0)
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument(
        "--output",
        default=f"reports/feed_quality_window_{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json",
    )
    parser.add_argument("--include-samples", action="store_true")
    return parser.parse_args()


def main() -> None:
    asyncio.run(run(parse_args()))


if __name__ == "__main__":
    main()
