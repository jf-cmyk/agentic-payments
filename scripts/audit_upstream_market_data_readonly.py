#!/usr/bin/env python3
"""Read a small multi-asset sample directly from the configured Blocksize API."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from src.blocksize_client import BlocksizeClient
from src.config import settings


def _timestamp_age_seconds(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0.0, (datetime.now(timezone.utc) - parsed).total_seconds())


def _summary(value: Any) -> dict[str, Any]:
    fields = {}
    for name in (
        "pair",
        "ticker",
        "vwap",
        "bid",
        "ask",
        "price",
        "rate",
        "mid",
        "currency",
        "source",
    ):
        field_value = getattr(value, name, None)
        if field_value is not None:
            fields[name] = field_value
    timestamp = getattr(value, "timestamp", None)
    fields["timestamp_present"] = timestamp is not None
    fields["timestamp_age_seconds"] = _timestamp_age_seconds(timestamp)
    numeric_values = [
        item
        for key, item in fields.items()
        if key in {"vwap", "bid", "ask", "price", "rate", "mid"}
        and isinstance(item, (int, float))
    ]
    fields["positive_numeric_value"] = any(item > 0 for item in numeric_values)
    return fields


async def _run_check(
    call: Callable[[], Awaitable[Any]],
) -> dict[str, Any]:
    try:
        value = await call()
    except Exception as exc:  # bounded audit output; never include credentials
        return {"status": "error", "error_type": type(exc).__name__}
    return {"status": "ok", "result": _summary(value)}


async def audit() -> dict[str, Any]:
    client = BlocksizeClient(api_key=settings.blocksize.api_key)
    try:
        checks = {
            "crypto_vwap": await _run_check(lambda: client.get_vwap_latest("BTCUSD")),
            "crypto_bidask": await _run_check(
                lambda: client.get_bidask_snapshot("BTCUSD")
            ),
            "equity": await _run_check(lambda: client.get_equity_snapshot("AAPL")),
            "fx": await _run_check(lambda: client.get_fx_rate("EURUSD")),
            "metal": await _run_check(lambda: client.get_metal_price("XAUUSD")),
        }
    finally:
        await client.close()
    return {
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "checks": checks,
        "successful_checks": sum(row["status"] == "ok" for row in checks.values()),
        "total_checks": len(checks),
    }


def main() -> None:
    print(json.dumps(asyncio.run(audit()), indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
