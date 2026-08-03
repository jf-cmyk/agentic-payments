#!/usr/bin/env python3
"""Probe configured public CEX feeds and write replayable freshness evidence."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from src.cex_stream_cache import CEXBookCache, CEXBookUnavailable, KrakenV2BookStream
from src.rwa_adapters import KrakenSpotAdapter, RevolutXAdapter


def _hash(payload: Any) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


async def _kraken_probe(symbols: list[str], seconds: float) -> dict[str, Any]:
    cache = CEXBookCache(ttl_seconds=max(10, seconds + 2))
    stream = KrakenV2BookStream(cache, symbols=symbols, venue_id="kraken_spot")
    started = datetime.now(UTC)
    await stream.start()
    rows = []
    try:
        await asyncio.sleep(seconds)
        for symbol in symbols:
            try:
                book = cache.get("kraken_spot", symbol)
                try:
                    trades = cache.trade_vwap(
                        "kraken_spot",
                        symbol,
                        max_age_seconds=max(60, seconds + 5),
                    )
                except CEXBookUnavailable as exc:
                    trades = {"status": "unavailable", "reason": str(exc)}
                rows.append({
                    "symbol": symbol,
                    "status": "ok",
                    "book": book,
                    "trade_vwap": trades,
                    "raw_payload_hash": _hash({"book": book, "trade_vwap": trades}),
                })
            except Exception as exc:
                rows.append({"symbol": symbol, "status": "error", "error": str(exc)})
    finally:
        await stream.stop()
    async with httpx.AsyncClient(timeout=20) as client:
        adapter = KrakenSpotAdapter(client=client)
        catalog = {}
        for symbol in symbols:
            try:
                api_pair, wsname = await adapter.resolve_pair(symbol)
                catalog[symbol] = {"api_pair": api_pair, "wsname": wsname, "listed": True}
            except Exception as exc:
                catalog[symbol] = {"listed": False, "error": str(exc)}
        xstocks = {}
        for symbol in ("AAPL/USD", "TSLA/USD", "NVDA/USD", "MSFT/USD"):
            try:
                payload = await adapter._get("/0/public/AssetPairs", {"pair": f"{symbol.split('/')[0]}XUSD"})
                xstocks[symbol] = bool(payload.get("result"))
            except Exception:
                xstocks[symbol] = False
    return {
        "started_at": started.isoformat(),
        "completed_at": datetime.now(UTC).isoformat(),
        "probe_seconds": seconds,
        "catalog": catalog,
        "xstocks_listed": xstocks,
        "observations": rows,
    }


async def _revolut_probe() -> dict[str, Any]:
    adapter = RevolutXAdapter()
    if not adapter.api_key:
        return {"status": "blocked", "reason": "REVOLUT_X_API_KEY is not configured"}
    try:
        pairs = await adapter.discover_pairs()
        pair_names = sorted(pairs)[:25]
        result: dict[str, Any] = {
            "status": "catalog_ok",
            "pair_count": len(pairs),
            "sample_pairs": pair_names,
            "catalog_hash": _hash(pairs),
        }
        if adapter.private_key_pem and pair_names:
            result["book"] = await adapter.fetch_bidask(pair_names[0])
        else:
            result["book_status"] = "blocked_without_REVOLUT_X_PRIVATE_KEY_PEM"
        return result
    except Exception as exc:
        return {"status": "error", "error": str(exc)}


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--symbols", default="BTC/USD,ETH/USD")
    parser.add_argument("--seconds", type=float, default=8.0)
    parser.add_argument("--out", default="reports/cex_public_feed_probe.json")
    args = parser.parse_args()
    symbols = [item.strip() for item in args.symbols.split(",") if item.strip()]
    report = {
        "product": "cex_public_feed_probe",
        "generated_at": datetime.now(UTC).isoformat(),
        "promotion_boundary": "supplemental shadow evidence only until quality and data-rights gates pass",
        "kraken": await _kraken_probe(symbols, args.seconds),
        "revolut_x": await _revolut_probe(),
    }
    output = Path(args.out)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "report": str(output),
        "kraken_ok": sum(row["status"] == "ok" for row in report["kraken"]["observations"]),
        "revolut_x": report["revolut_x"]["status"],
    }))


if __name__ == "__main__":
    asyncio.run(main())
