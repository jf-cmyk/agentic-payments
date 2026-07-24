"""Minimal async client for Tiingo real-time U.S. equity snapshots."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from typing import Any

import httpx

from src.config import settings


class TiingoAPIError(Exception):
    """Raised when Tiingo cannot return a usable equity snapshot."""


class TiingoClient:
    """Fetch Tiingo IEX and consolidated equity data without logging credentials."""

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        equity_base_url: str | None = None,
        timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._api_key = (api_key if api_key is not None else settings.tiingo.api_key).strip()
        self._base_url = (base_url or settings.tiingo.base_url).rstrip("/")
        self._equity_base_url = (
            equity_base_url or settings.tiingo.equity_base_url
        ).rstrip("/")
        self._timeout = timeout
        self._client = client
        self._owns_client = client is None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self._timeout)
            self._owns_client = True
        return self._client

    async def close(self) -> None:
        if self._owns_client and self._client is not None and not self._client.is_closed:
            await self._client.aclose()

    async def get_equity_snapshot(self, ticker: str) -> dict[str, Any]:
        """Return the Tiingo IEX/derived equity snapshot."""
        symbol = ticker.strip().upper()
        item = await self._request_item(f"{self._base_url}/{symbol}", symbol)

        bid = _finite_float(item.get("bidPrice"))
        ask = _finite_float(item.get("askPrice"))
        provider_mid = _finite_float(item.get("mid"))
        tngo_last = _finite_float(item.get("tngoLast"))
        last = _finite_float(item.get("last"))

        if provider_mid is not None:
            reference_price = provider_mid
            reference_price_field = "mid"
        elif bid is not None and ask is not None:
            reference_price = (bid + ask) / 2
            reference_price_field = "bid_ask_mid"
        elif tngo_last is not None:
            reference_price = tngo_last
            reference_price_field = "tngoLast"
        else:
            reference_price = last
            reference_price_field = "last" if last is not None else None

        if reference_price is None:
            raise TiingoAPIError(f"Tiingo returned no usable price for {symbol}")

        quote_timestamp = _iso_timestamp(item.get("quoteTimestamp"))
        last_sale_timestamp = _iso_timestamp(item.get("lastSaleTimestamp"))
        refresh_timestamp = _iso_timestamp(item.get("timestamp"))
        if bid is not None and ask is not None and quote_timestamp:
            source_timestamp = quote_timestamp
        elif reference_price_field in {"last", "tngoLast"} and last_sale_timestamp:
            source_timestamp = last_sale_timestamp
        else:
            source_timestamp = refresh_timestamp or quote_timestamp or last_sale_timestamp

        return {
            "ticker": str(item.get("ticker", symbol)).upper(),
            "bid": bid,
            "ask": ask,
            "mid": provider_mid,
            "reference_price": reference_price,
            "reference_price_field": reference_price_field,
            "timestamp": source_timestamp,
            "refresh_timestamp": refresh_timestamp,
            "quote_timestamp": quote_timestamp,
            "last_sale_timestamp": last_sale_timestamp,
            "last": last,
            "tngo_last": tngo_last,
            "volume": _finite_float(item.get("volume")),
            "bid_size": _finite_float(item.get("bidSize")),
            "ask_size": _finite_float(item.get("askSize")),
            "source": "tiingo_iex",
            "quote_semantics": (
                "iex_top_of_book"
                if bid is not None and ask is not None
                else "tiingo_derived_reference"
            ),
        }

    async def get_consolidated_equity_snapshot(self, ticker: str) -> dict[str, Any]:
        """Return Tiingo consolidated reference and liquidity quote metrics.

        Tiingo documents lqBidPrice/lqAskPrice as liquidity-risk components, not
        as an executable SIP NBBO. Callers must preserve that distinction.
        """
        symbol = ticker.strip().upper()
        item = await self._request_item(f"{self._equity_base_url}/{symbol}", symbol)
        bid = _finite_float(item.get("lqBidPrice"))
        ask = _finite_float(item.get("lqAskPrice"))
        reference_price = _finite_float(item.get("lqRefPrice"))
        reference_price_field = "lqRefPrice"
        if reference_price is None:
            reference_price = _finite_float(item.get("tngoLast"))
            reference_price_field = "tngoLast"
        if reference_price is None and bid is not None and ask is not None:
            reference_price = (bid + ask) / 2
            reference_price_field = "liquidity_bid_ask_mid"
        if reference_price is None:
            raise TiingoAPIError(
                f"Tiingo consolidated endpoint returned no usable price for {symbol}"
            )
        return {
            "ticker": str(item.get("ticker", symbol)).upper(),
            "bid": bid,
            "ask": ask,
            "mid": (bid + ask) / 2 if bid is not None and ask is not None else None,
            "reference_price": reference_price,
            "reference_price_field": reference_price_field,
            "timestamp": _iso_timestamp(item.get("timestamp")),
            "tngo_last": _finite_float(item.get("tngoLast")),
            "volume": _finite_float(item.get("volume")),
            "bid_size": _finite_float(item.get("lqBidSize")),
            "ask_size": _finite_float(item.get("lqAskSize")),
            "liquidity_spread": _finite_float(item.get("lqSpread")),
            "source": "tiingo_consolidated",
            "quote_semantics": "consolidated_liquidity_reference_not_verified_nbbo",
        }

    async def _request_item(self, url: str, symbol: str) -> dict[str, Any]:
        if not self._api_key:
            raise TiingoAPIError(
                "TIINGO_API_KEY is not configured; add it to .env before running the comparison"
            )
        client = await self._get_client()
        response = await client.get(
            url,
            headers={"Authorization": f"Token {self._api_key}"},
        )
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise TiingoAPIError(
                f"Tiingo returned HTTP {response.status_code} for {symbol}"
            ) from exc
        payload = response.json()
        rows = payload if isinstance(payload, list) else [payload]
        item = next(
            (
                row
                for row in rows
                if isinstance(row, dict) and str(row.get("ticker", "")).upper() == symbol
            ),
            rows[0] if rows and isinstance(rows[0], dict) else None,
        )
        if not item or item.get("detail"):
            raise TiingoAPIError(f"Tiingo returned no equity snapshot for {symbol}")
        return item


def _finite_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _iso_timestamp(value: Any) -> str | None:
    if value in {None, ""}:
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        raw = float(value)
        if raw > 100_000_000_000_000:
            raw /= 1_000_000
        elif raw > 100_000_000_000:
            raw /= 1_000
        parsed = datetime.fromtimestamp(raw, tz=UTC)
    else:
        try:
            parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()
