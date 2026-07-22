#!/usr/bin/env python3
"""Measure observed Tiingo WebSocket event cadence without exposing credentials."""

from __future__ import annotations

import argparse
import asyncio
import json
import math
from collections import Counter, defaultdict
from datetime import UTC, datetime
from pathlib import Path
from statistics import median
from time import monotonic
from typing import Any

import websockets

from src.config import settings


PROFILES: dict[str, dict[str, Any]] = {
    "iex_reference": {
        "url": "wss://api.tiingo.com/iex",
        "threshold": 6,
        "tickers": ["LCID", "AAPL", "NVDA", "TSLA", "SPY", "QQQ", "AMD", "MSFT"],
    },
    "iex_full": {
        "url": "wss://api.tiingo.com/iex",
        "threshold": 0,
        "tickers": ["LCID", "AAPL", "NVDA", "TSLA", "SPY", "QQQ", "AMD", "MSFT"],
    },
    "consolidated_reference": {
        "url": "wss://api.tiingo.com/equity/intraday",
        "threshold": 6,
        "tickers": ["LCID", "AAPL", "NVDA", "TSLA", "SPY", "QQQ", "AMD", "MSFT"],
    },
    "consolidated_liquidity": {
        "url": "wss://api.tiingo.com/equity/intraday",
        "threshold": 4,
        "tickers": ["LCID", "AAPL", "NVDA", "TSLA", "SPY", "QQQ", "AMD", "MSFT"],
    },
    "crypto_top_trade": {
        "url": "wss://api.tiingo.com/crypto",
        "threshold": 2,
        "tickers": ["btcusd", "ethusd", "solusd"],
    },
    "crypto_trade": {
        "url": "wss://api.tiingo.com/crypto",
        "threshold": 5,
        "tickers": ["btcusd", "ethusd", "solusd"],
    },
    "fx_top": {
        "url": "wss://api.tiingo.com/fx",
        "threshold": 5,
        "tickers": ["eurusd", "usdjpy", "gbpusd"],
    },
}


def _percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    position = (len(ordered) - 1) * fraction
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    return ordered[lower] * (upper - position) + ordered[upper] * (position - lower)


def _stats(values: list[float]) -> dict[str, float | int | None]:
    if not values:
        return {"count": 0, "min": None, "median": None, "p95": None, "max": None}
    return {
        "count": len(values),
        "min": round(min(values), 6),
        "median": round(median(values), 6),
        "p95": round(_percentile(values, 0.95) or 0.0, 6),
        "max": round(max(values), 6),
    }


def _event_identity(profile: str, data: list[Any]) -> tuple[str | None, str | None, str | None]:
    """Return ticker, event type, and provider timestamp for supported payload shapes."""
    if profile in {"iex_reference", "consolidated_reference"}:
        return (
            str(data[1]).upper() if len(data) > 1 else None,
            "reference",
            str(data[0]) if data else None,
        )
    if profile == "consolidated_liquidity":
        return (
            str(data[1]).upper() if len(data) > 1 else None,
            "liquidity",
            str(data[0]) if data else None,
        )
    if profile == "iex_full":
        return (
            str(data[3]).upper() if len(data) > 3 else None,
            str(data[0]) if data else None,
            str(data[1]) if len(data) > 1 else None,
        )
    if profile.startswith("crypto_"):
        return (
            str(data[1]).upper() if len(data) > 1 else None,
            str(data[0]) if data else None,
            str(data[2]) if len(data) > 2 else None,
        )
    if profile == "fx_top":
        return (
            str(data[1]).upper() if len(data) > 1 else None,
            str(data[0]) if data else "quote",
            str(data[2]) if len(data) > 2 else None,
        )
    return None, None, None


async def benchmark(profile_name: str, duration_seconds: float, tickers: list[str]) -> dict[str, Any]:
    profile = PROFILES[profile_name]
    api_key = settings.tiingo.api_key.strip()
    if not api_key:
        raise RuntimeError("TIINGO_API_KEY is not configured")

    subscription = {
        "eventName": "subscribe",
        "authorization": api_key,
        "eventData": {
            "thresholdLevel": profile["threshold"],
            "tickers": tickers,
        },
    }
    started_at = datetime.now(UTC)
    start_clock = monotonic()
    event_times: dict[str, list[float]] = defaultdict(list)
    provider_timestamps: dict[str, set[str]] = defaultdict(set)
    event_types: Counter[str] = Counter()
    control_messages: Counter[str] = Counter()
    control_samples: list[dict[str, Any]] = []
    errors: list[str] = []
    samples: list[dict[str, Any]] = []

    async with websockets.connect(profile["url"], open_timeout=10, close_timeout=5) as socket:
        await socket.send(json.dumps(subscription))
        deadline = start_clock + duration_seconds
        while monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(socket.recv(), timeout=min(5.0, deadline - monotonic()))
            except asyncio.TimeoutError:
                continue
            except websockets.ConnectionClosed as exc:
                errors.append(f"connection_closed:{exc.code}:{exc.reason or 'no_reason'}")
                break
            received_at = datetime.now(UTC)
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                errors.append("non_json_message")
                continue
            message_type = str(message.get("messageType", "unknown"))
            if message_type != "A":
                control_messages[message_type] += 1
                if len(control_samples) < 10:
                    control_samples.append(
                        {
                            "message_type": message_type,
                            "data": message.get("data"),
                            "message": message.get("message"),
                        }
                    )
                if message_type in {"E", "error"}:
                    errors.append(str(message.get("data") or message.get("message") or "feed_error"))
                continue
            data = message.get("data")
            if not isinstance(data, list):
                errors.append("data_message_without_array")
                continue
            ticker, event_type, provider_timestamp = _event_identity(profile_name, data)
            ticker = ticker or "UNKNOWN"
            event_times[ticker].append(monotonic())
            if provider_timestamp:
                provider_timestamps[ticker].add(provider_timestamp)
            if event_type:
                event_types[event_type] += 1
            if len(samples) < 20:
                samples.append(
                    {
                        "received_at": received_at.isoformat(),
                        "ticker": ticker,
                        "event_type": event_type,
                        "provider_timestamp": provider_timestamp,
                    }
                )

    elapsed = max(monotonic() - start_clock, 0.001)
    ticker_rows: list[dict[str, Any]] = []
    for ticker in tickers:
        normalized = ticker.upper()
        times = event_times.get(normalized, [])
        gaps = [right - left for left, right in zip(times, times[1:])]
        ticker_rows.append(
            {
                "ticker": normalized,
                "events": len(times),
                "events_per_second": round(len(times) / elapsed, 6),
                "events_per_minute": round(len(times) / elapsed * 60, 3),
                "distinct_provider_timestamps": len(provider_timestamps.get(normalized, set())),
                "interarrival_seconds": _stats(gaps),
                "share_gaps_under_500ms": (
                    round(sum(gap <= 0.5 for gap in gaps) / len(gaps), 6) if gaps else None
                ),
            }
        )

    total_events = sum(len(times) for times in event_times.values())
    return {
        "profile": profile_name,
        "url": profile["url"],
        "threshold_level": profile["threshold"],
        "started_at": started_at.isoformat(),
        "ended_at": datetime.now(UTC).isoformat(),
        "duration_seconds": round(elapsed, 6),
        "tickers_requested": [ticker.upper() for ticker in tickers],
        "total_events": total_events,
        "aggregate_events_per_second": round(total_events / elapsed, 6),
        "event_types": dict(sorted(event_types.items())),
        "control_messages": dict(sorted(control_messages.items())),
        "control_samples": control_samples,
        "errors": errors,
        "ticker_frequency": ticker_rows,
        "sample_event_metadata": samples,
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile", choices=sorted(PROFILES))
    parser.add_argument("--duration-seconds", type=float, default=30.0)
    parser.add_argument("--tickers", nargs="*")
    parser.add_argument("--output", type=Path)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    tickers = args.tickers or list(PROFILES[args.profile]["tickers"])
    result = asyncio.run(benchmark(args.profile, args.duration_seconds, tickers))
    rendered = json.dumps(result, indent=2)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)


if __name__ == "__main__":
    main()
