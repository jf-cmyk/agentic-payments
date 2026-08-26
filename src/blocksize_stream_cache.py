"""
Websocket-backed cache for Blocksize subscription-only feeds.

The public docs expose 24-hour fixed VWAP and aggregate AMM state price as
websocket subscriptions. This cache turns those live streams into fast, ready
HTTP reads for the paid resource server without inventing undocumented RPC
methods.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.blocksize_client import BlocksizeAPIError, BlocksizeClient
from src.config import settings
from src.models import StatePriceData, VWAP24HrData

logger = logging.getLogger(__name__)


@dataclass
class _CacheItem:
    payload: dict[str, Any]
    cached_at: datetime


class BlocksizeStreamCache:
    """Maintain local reads for websocket-only Blocksize feeds."""

    def __init__(
        self,
        *,
        api_key: str | None = None,
        ws_url: str | None = None,
        rest_client: BlocksizeClient | None = None,
        enabled: bool | None = None,
        fixed_vwap_tickers: list[str] | None = None,
        state_tickers: list[str] | None = None,
        state_mode: str | None = None,
        max_state_tickers: int | None = None,
        ttl_seconds: int | None = None,
        reconnect_seconds: float | None = None,
    ) -> None:
        self.api_key = api_key or settings.blocksize.api_key
        self.ws_url = ws_url or settings.blocksize.ws_url
        self.rest_client = rest_client
        self.enabled = settings.blocksize.stream_cache_enabled if enabled is None else enabled
        self.fixed_vwap_tickers = fixed_vwap_tickers or settings.blocksize.fixed_vwap_ticker_list
        self.state_tickers = state_tickers or settings.blocksize.state_cache_ticker_list
        self.state_mode = (state_mode or settings.blocksize.state_cache_mode).lower()
        self.max_state_tickers = max_state_tickers or settings.blocksize.state_cache_max_tickers
        self.ttl_seconds = ttl_seconds or settings.blocksize.stream_cache_ttl_seconds
        self.reconnect_seconds = reconnect_seconds or settings.blocksize.stream_cache_reconnect_seconds
        self._vwap24h: dict[str, _CacheItem] = {}
        self._state: dict[str, _CacheItem] = {}
        self._task: asyncio.Task[None] | None = None
        self._ready = asyncio.Event()

    async def start(self) -> None:
        if not self.enabled:
            return
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._run(), name="blocksize-stream-cache")

    async def stop(self) -> None:
        if not self._task:
            return
        self._task.cancel()
        try:
            await self._task
        except asyncio.CancelledError:
            pass
        self._task = None

    def status(self) -> dict[str, Any]:
        configured_fixed_vwap = {_normalize(item) for item in self.fixed_vwap_tickers}
        fresh_fixed_vwap = sum(
            self._is_fresh(self._vwap24h, ticker)
            for ticker in configured_fixed_vwap
        )
        return {
            "enabled": self.enabled,
            "ready": self._ready.is_set(),
            "ws_url": self.ws_url,
            "fixed_vwap_tickers": len(configured_fixed_vwap),
            "state_tickers": len(self.state_tickers),
            "state_mode": self.state_mode,
            "cached_24h_vwap": len(self._vwap24h),
            "fresh_configured_24h_vwap": fresh_fixed_vwap,
            "missing_configured_24h_vwap": max(
                0,
                len(configured_fixed_vwap) - fresh_fixed_vwap,
            ),
            "cached_state": len(self._state),
            "ttl_seconds": self.ttl_seconds,
        }

    def has_vwap_24h(self, pair: str) -> bool:
        return self._is_fresh(self._vwap24h, _normalize(pair))

    def has_state_price(self, pair: str) -> bool:
        return self._is_fresh(self._state, _normalize(pair))

    async def get_vwap_24h(self, pair: str) -> VWAP24HrData:
        item = self._get_fresh(self._vwap24h, _normalize(pair), "24-hour fixed VWAP")
        data = item.payload
        return VWAP24HrData(
            pair=str(data.get("ticker") or pair).upper(),
            vwap=float(data.get("price", data.get("vwap", 0))),
            volume=float(data.get("volume", 0) or 0),
            timestamp=_parse_ts(data.get("timestamp", data.get("ts"))),
            source="blocksize:fixedvwap_subscribe_cache",
        )

    async def get_state_price(self, pair: str) -> StatePriceData:
        item = self._get_fresh(self._state, _normalize(pair), "aggregate state price")
        data = item.payload
        base = str(data.get("base_symbol") or "")
        quote = str(data.get("quote_symbol") or "")
        return StatePriceData(
            pair=(base + quote).upper() or pair.upper(),
            price=float(
                data.get("aggregated_state_price")
                or data.get("state_price_usd")
                or data.get("price")
                or 0
            ),
            timestamp=_parse_ts(data.get("timestamp", data.get("ts"))),
            source="blocksize:state_subscribe_cache",
        )

    def _get_fresh(
        self,
        cache: dict[str, _CacheItem],
        key: str,
        label: str,
    ) -> _CacheItem:
        item = cache.get(key)
        if item is None:
            raise BlocksizeAPIError(-32004, f"{label} cache does not contain {key}")
        age = (datetime.now(timezone.utc) - item.cached_at).total_seconds()
        if age > self.ttl_seconds:
            raise BlocksizeAPIError(-32004, f"{label} cache entry for {key} is stale ({age:.0f}s)")
        return item

    def _is_fresh(self, cache: dict[str, _CacheItem], key: str) -> bool:
        item = cache.get(key)
        if item is None:
            return False
        age = (datetime.now(timezone.utc) - item.cached_at).total_seconds()
        return age <= self.ttl_seconds

    async def _run(self) -> None:
        while True:
            try:
                await self._resolve_state_tickers()
                await self._consume_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Blocksize stream cache disconnected")
                self._ready.clear()
                await asyncio.sleep(self.reconnect_seconds)

    async def _resolve_state_tickers(self) -> None:
        if self.state_mode != "all":
            return
        if self.rest_client is None:
            logger.warning("Cannot resolve all state tickers without REST client; using configured list")
            return
        instruments = await self.rest_client.list_state_instruments()
        tickers = [
            str(item.get("symbol", "")).upper()
            for item in instruments
            if isinstance(item, dict) and item.get("symbol")
        ]
        self.state_tickers = tickers[: self.max_state_tickers]

    async def _consume_once(self) -> None:
        try:
            import websockets
        except ImportError as exc:  # pragma: no cover - dependency guard
            raise RuntimeError("Install websockets to enable Blocksize stream cache") from exc

        async with websockets.connect(self.ws_url) as ws:
            await self._ws_call(ws, "authentication_logon", {"api_key": self.api_key, "token": ""})
            if self.fixed_vwap_tickers:
                result = await self._ws_call(
                    ws,
                    "fixedvwap_subscribe",
                    {"tickers": self.fixed_vwap_tickers},
                )
                self._apply_fixed_vwap_snapshot(result)
            if self.state_tickers:
                result = await self._ws_call(
                    ws,
                    "state_subscribe",
                    {"tickers": self.state_tickers},
                )
                self._apply_state_snapshot(result)
            self._ready.set()

            async for message in ws:
                self._handle_message(json.loads(message))

    async def _ws_call(self, ws: Any, method: str, params: dict[str, Any]) -> Any:
        request_id = str(uuid.uuid4())
        await ws.send(json.dumps({
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": params,
        }))
        while True:
            message = json.loads(await ws.recv())
            if message.get("id") == request_id:
                if "error" in message:
                    err = message["error"]
                    raise BlocksizeAPIError(
                        err.get("code", -1),
                        err.get("message", f"{method} failed"),
                        err.get("data"),
                    )
                return message.get("result")
            self._handle_message(message)

    def _handle_message(self, message: dict[str, Any]) -> None:
        method = message.get("method")
        params = message.get("params")
        if isinstance(params, str):
            try:
                params = json.loads(params)
            except json.JSONDecodeError:
                return
        if not isinstance(params, dict):
            return
        if method == "fixedvwap":
            self._apply_fixed_vwap_snapshot(params)
        elif method == "state":
            self._apply_state_snapshot(params)

    def _apply_fixed_vwap_snapshot(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        rows = payload.get("snapshot") or payload.get("updates") or []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            ticker = row.get("ticker")
            if ticker:
                self._vwap24h[_normalize(str(ticker))] = _CacheItem(row, datetime.now(timezone.utc))

    def _apply_state_snapshot(self, payload: Any) -> None:
        if not isinstance(payload, dict):
            return
        rows = payload.get("snapshot") or payload.get("states") or []
        for row in rows if isinstance(rows, list) else []:
            if not isinstance(row, dict):
                continue
            base = row.get("base_symbol")
            quote = row.get("quote_symbol")
            if base and quote:
                self._state[_normalize(f"{base}{quote}")] = _CacheItem(row, datetime.now(timezone.utc))


def _normalize(value: str) -> str:
    return value.replace("-", "").replace("/", "").replace("_", "").upper()


def _parse_ts(value: Any) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, (int, float)):
        numeric = float(value)
        if numeric > 10_000_000_000_000:
            numeric = numeric / 1_000_000
        elif numeric > 10_000_000_000:
            numeric = numeric / 1_000
        return datetime.fromtimestamp(numeric, tz=timezone.utc)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            pass
    return datetime.now(timezone.utc)
