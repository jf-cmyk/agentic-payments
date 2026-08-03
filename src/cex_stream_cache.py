"""Sequence-aware caches for public centralized-exchange market-data streams."""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Callable

logger = logging.getLogger(__name__)


class CEXBookUnavailable(ValueError):
    """Raised when a streamed book is absent, stale, or known to have a gap."""


@dataclass
class CEXBook:
    symbol: str
    bids: dict[float, float] = field(default_factory=dict)
    asks: dict[float, float] = field(default_factory=dict)
    sequence: int | None = None
    exchange_timestamp: str | None = None
    received_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    valid: bool = False


class CEXBookCache:
    """Maintain normalized L2 books and reject stale or sequence-gapped state."""

    def __init__(self, *, ttl_seconds: float = 10.0) -> None:
        self.ttl_seconds = ttl_seconds
        self._books: dict[tuple[str, str], CEXBook] = {}
        self._trades: dict[tuple[str, str], list[dict[str, Any]]] = {}

    @staticmethod
    def _key(venue: str, symbol: str) -> tuple[str, str]:
        return venue.lower(), symbol.replace("-", "/").upper()

    def apply_snapshot(
        self,
        venue: str,
        symbol: str,
        *,
        bids: list[dict[str, float]],
        asks: list[dict[str, float]],
        sequence: int | None = None,
        exchange_timestamp: str | None = None,
    ) -> None:
        book = CEXBook(
            symbol=symbol.replace("-", "/").upper(),
            bids=self._levels(bids),
            asks=self._levels(asks),
            sequence=sequence,
            exchange_timestamp=exchange_timestamp,
            valid=True,
        )
        self._books[self._key(venue, symbol)] = book

    def apply_update(
        self,
        venue: str,
        symbol: str,
        *,
        bids: list[dict[str, float]],
        asks: list[dict[str, float]],
        sequence: int | None = None,
        exchange_timestamp: str | None = None,
    ) -> None:
        key = self._key(venue, symbol)
        book = self._books.get(key)
        if book is None or not book.valid:
            raise CEXBookUnavailable(f"{venue} {symbol} requires a fresh snapshot")
        if sequence is not None and book.sequence is not None and sequence != book.sequence + 1:
            book.valid = False
            raise CEXBookUnavailable(
                f"{venue} {symbol} sequence gap: expected {book.sequence + 1}, received {sequence}"
            )
        self._merge(book.bids, bids)
        self._merge(book.asks, asks)
        book.sequence = sequence if sequence is not None else book.sequence
        book.exchange_timestamp = exchange_timestamp or book.exchange_timestamp
        book.received_at = datetime.now(UTC)

    def get(self, venue: str, symbol: str) -> dict[str, Any]:
        book = self._books.get(self._key(venue, symbol))
        if book is None or not book.valid:
            raise CEXBookUnavailable(f"{venue} {symbol} has no valid streamed book")
        age = (datetime.now(UTC) - book.received_at).total_seconds()
        if age > self.ttl_seconds:
            raise CEXBookUnavailable(f"{venue} {symbol} streamed book is stale ({age:.3f}s)")
        bids = [{"price": price, "size": size} for price, size in sorted(book.bids.items(), reverse=True)]
        asks = [{"price": price, "size": size} for price, size in sorted(book.asks.items())]
        if bids and asks and bids[0]["price"] >= asks[0]["price"]:
            raise CEXBookUnavailable(f"{venue} {symbol} streamed book is crossed")
        return {
            "symbol": book.symbol,
            "bids": bids,
            "asks": asks,
            "sequence": book.sequence,
            "exchange_timestamp": book.exchange_timestamp,
            "received_at": book.received_at.isoformat(),
            "age_ms": round(age * 1000, 3),
        }

    def add_trades(self, venue: str, symbol: str, trades: list[dict[str, Any]]) -> None:
        key = self._key(venue, symbol)
        target = self._trades.setdefault(key, [])
        received_at = datetime.now(UTC).isoformat()
        for row in trades:
            price, size = float(row["price"]), float(row["size"])
            if price > 0 and size > 0:
                target.append({
                    "price": price,
                    "size": size,
                    "side": row.get("side"),
                    "timestamp": row.get("timestamp"),
                    "trade_id": row.get("trade_id"),
                    "received_at": received_at,
                })
        del target[:-5000]

    def trade_vwap(self, venue: str, symbol: str, *, max_age_seconds: float = 60.0) -> dict[str, Any]:
        now = datetime.now(UTC)
        rows = []
        for row in self._trades.get(self._key(venue, symbol), []):
            raw = row.get("timestamp") or row.get("received_at")
            try:
                timestamp = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            except (TypeError, ValueError):
                continue
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=UTC)
            if (now - timestamp).total_seconds() <= max_age_seconds:
                rows.append(row)
        volume = sum(row["size"] for row in rows)
        if not rows or volume <= 0:
            raise CEXBookUnavailable(f"{venue} {symbol} has no fresh trades")
        return {
            "symbol": symbol.replace("-", "/").upper(),
            "venue": venue,
            "vwap": sum(row["price"] * row["size"] for row in rows) / volume,
            "base_volume": volume,
            "trade_count": len(rows),
            "window_seconds": max_age_seconds,
            "latest_timestamp": max(str(row.get("timestamp") or row["received_at"]) for row in rows),
        }

    @staticmethod
    def _levels(rows: list[dict[str, float]]) -> dict[float, float]:
        result: dict[float, float] = {}
        CEXBookCache._merge(result, rows)
        return result

    @staticmethod
    def _merge(target: dict[float, float], rows: list[dict[str, float]]) -> None:
        for row in rows:
            price = float(row["price"])
            size = float(row["size"])
            if price <= 0 or size < 0:
                raise ValueError("CEX book levels require positive prices and non-negative sizes")
            if size == 0:
                target.pop(price, None)
            else:
                target[price] = size


class KrakenV2BookStream:
    """Consume Kraken WebSocket v2 book snapshots and updates into a cache."""

    venue_id = "kraken_xstocks"

    def __init__(
        self,
        cache: CEXBookCache,
        *,
        symbols: list[str],
        venue_id: str = "kraken_xstocks",
        ws_url: str = "wss://ws.kraken.com/v2",
        depth: int = 100,
        reconnect_seconds: float = 2.0,
        connect: Callable[..., Any] | None = None,
    ) -> None:
        self.cache = cache
        self.venue_id = venue_id
        self.symbols = symbols
        self.ws_url = ws_url
        self.depth = depth
        self.reconnect_seconds = reconnect_seconds
        self._connect = connect
        self._task: asyncio.Task[None] | None = None

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="kraken-v2-book-stream")

    async def stop(self) -> None:
        if self._task is None:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    async def _run(self) -> None:
        while True:
            try:
                await self._consume_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Kraken public book stream disconnected")
                await asyncio.sleep(self.reconnect_seconds)

    async def _consume_once(self) -> None:
        connect = self._connect
        if connect is None:
            import websockets

            connect = websockets.connect
        async with connect(self.ws_url) as ws:
            await ws.send(json.dumps({
                "method": "subscribe",
                "params": {"channel": "book", "symbol": self.symbols, "depth": self.depth, "snapshot": True},
            }))
            await ws.send(json.dumps({
                "method": "subscribe",
                "params": {"channel": "trade", "symbol": self.symbols, "snapshot": True},
            }))
            async for raw in ws:
                self.handle_message(json.loads(raw))

    def handle_message(self, message: dict[str, Any]) -> None:
        if not isinstance(message.get("data"), list):
            return
        if message.get("channel") == "trade":
            by_symbol: dict[str, list[dict[str, Any]]] = {}
            for row in message["data"]:
                if not isinstance(row, dict) or not row.get("symbol"):
                    continue
                by_symbol.setdefault(str(row["symbol"]), []).append({
                    "price": row.get("price"),
                    "size": row.get("qty"),
                    "side": row.get("side"),
                    "timestamp": row.get("timestamp"),
                    "trade_id": row.get("trade_id"),
                })
            for symbol, rows in by_symbol.items():
                self.cache.add_trades(self.venue_id, symbol, rows)
            return
        if message.get("channel") != "book":
            return
        for row in message["data"]:
            if not isinstance(row, dict) or not row.get("symbol"):
                continue
            bids = self._kraken_levels(row.get("bids"))
            asks = self._kraken_levels(row.get("asks"))
            kwargs = {
                "bids": bids,
                "asks": asks,
                "sequence": self._sequence(row),
                "exchange_timestamp": row.get("timestamp"),
            }
            if message.get("type") == "snapshot":
                self.cache.apply_snapshot(self.venue_id, str(row["symbol"]), **kwargs)
            elif message.get("type") == "update":
                self.cache.apply_update(self.venue_id, str(row["symbol"]), **kwargs)

    @staticmethod
    def _kraken_levels(value: Any) -> list[dict[str, float]]:
        if not isinstance(value, list):
            return []
        return [
            {"price": float(row["price"]), "size": float(row["qty"])}
            for row in value
            if isinstance(row, dict) and row.get("price") is not None and row.get("qty") is not None
        ]

    @staticmethod
    def _sequence(row: dict[str, Any]) -> int | None:
        value = row.get("sequence") or row.get("seq")
        return int(value) if value is not None else None
