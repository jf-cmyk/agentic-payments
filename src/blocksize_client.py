"""
Blocksize Capital API client.

The deployed Agentic Payments gateway currently uses JSON-RPC 2.0 over HTTP.
It does not maintain websocket subscriptions itself.

Verified upstream methods for the deployed key:
  - vwap_latest
  - vwap_instruments
  - bidask_getSnapshot
  - bidask_instruments

The wider Blocksize platform documentation discusses websocket transport and
subscription-style notifications in general, but this repo's integration path
is request/response over HTTP POST.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from src.config import settings
from src.models import (
    BidAskData,
    EquityData,
    FXData,
    MetalData,
    PairInfo,
    StatePriceData,
    VWAP24HrData,
    VWAP30MinData,
    VWAPData,
)

logger = logging.getLogger(__name__)

FIAT_CURRENCIES = {
    "USD", "EUR", "GBP", "JPY", "CHF", "CAD", "AUD", "NZD", "SEK", "NOK",
    "DKK", "CNH", "CNY", "HKD", "SGD", "MXN", "BRL", "ZAR", "TRY", "PLN",
    "CZK", "HUF", "RON", "ILS", "INR",
}
METAL_TICKERS = {
    "XAUUSD": "Gold",
    "XAGUSD": "Silver",
    "XPTUSD": "Platinum",
    "XPDUSD": "Palladium",
    "COPPERUSD": "Copper",
}


class BlocksizeAPIError(Exception):
    """Raised when the Blocksize API returns an error."""

    def __init__(self, code: int, message: str, data: Any = None) -> None:
        self.code = code
        self.message = message
        self.data = data
        super().__init__(f"Blocksize API Error {code}: {message}")


class BlocksizeClient:
    """
    Async client for the Blocksize Capital Market Data API.

    Uses JSON-RPC 2.0 over HTTP for REST calls.
    All requests are authenticated via the x-api-key header.
    """

    def __init__(
        self,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._api_key = api_key or settings.blocksize.api_key
        self._rest_url = (base_url or settings.blocksize.rest_url)
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def _get_client(self) -> httpx.AsyncClient:
        """Lazy-initialize the async HTTP client with connection pooling."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                timeout=httpx.Timeout(self._timeout),
                headers={
                    "x-api-key": self._api_key,
                    "Content-Type": "application/json",
                },
                limits=httpx.Limits(
                    max_connections=20,
                    max_keepalive_connections=10,
                ),
            )
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()

    # -----------------------------------------------------------------------
    # JSON-RPC 2.0 Core
    # -----------------------------------------------------------------------

    def _build_rpc_request(
        self, method: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """Build a JSON-RPC 2.0 request payload."""
        return {
            "jsonrpc": "2.0",
            "id": str(uuid.uuid4()),
            "method": method,
            "params": params or {},
        }

    async def _rpc_call(
        self, method: str, params: dict[str, Any] | None = None
    ) -> Any:
        """
        Execute a JSON-RPC 2.0 call against the Blocksize REST API.

        Returns the 'result' field of the response, or raises
        BlocksizeAPIError if the response contains an error.
        """
        client = await self._get_client()
        payload = self._build_rpc_request(method, params)

        logger.debug("RPC call: %s params=%s", method, params)

        response = await client.post(self._rest_url, json=payload)
        response.raise_for_status()

        data = response.json()

        if "error" in data:
            err = data["error"]
            raise BlocksizeAPIError(
                code=err.get("code", -1),
                message=err.get("message", "Unknown error"),
                data=err.get("data"),
            )

        return data.get("result")

    # -----------------------------------------------------------------------
    # Real-Time VWAP (Crypto)
    # -----------------------------------------------------------------------

    async def get_vwap_latest(self, pair: str) -> VWAPData:
        """
        Get the latest real-time VWAP for a crypto trading pair.

        Args:
            pair: Trading pair identifier (e.g., 'btc-usd', 'eth-eur')

        Returns:
            VWAPData with the current VWAP price and metadata.
        """
        result = await self._rpc_call("vwap_latest", {"ticker": pair})

        if isinstance(result, dict):
            if "vwap" in result and isinstance(result["vwap"], dict):
                result = result["vwap"]
            return VWAPData(
                pair=result.get("ticker", pair),
                vwap=float(result.get("price", result.get("vwap", 0))),
                volume=_safe_float(result.get("volume")),
                market_cap=_safe_float(result.get("market_cap")),
                timestamp=_parse_timestamp(result.get("timestamp", result.get("ts"))),
                currency=result.get("currency", _extract_quote(pair)),
                source="blocksize",
            )

        if isinstance(result, list) and len(result) > 0:
            item = result[0]
            return VWAPData(
                pair=item.get("ticker", pair),
                vwap=float(item.get("price", item.get("vwap", 0))),
                timestamp=_parse_timestamp(item.get("timestamp")),
                currency=item.get("currency", _extract_quote(pair)),
                source="blocksize",
            )

        raise BlocksizeAPIError(-1, f"Unexpected response format for vwap_latest: {result}")

    async def list_vwap_instruments(self) -> list[str]:
        """List all instruments available for real-time VWAP data."""
        result = await self._rpc_call("vwap_instruments")
        return self._extract_instrument_tickers(result)

    # -----------------------------------------------------------------------
    # 30-Minute VWAP (Crypto)
    # -----------------------------------------------------------------------

    async def get_vwap_30min(self, ticker: str) -> VWAP30MinData:
        """
        Get the latest 30-minute VWAP for a crypto ticker.

        Args:
            ticker: Base currency ticker (e.g., 'BTC', 'ETH')

        Returns:
            VWAP30MinData with 30-minute aggregated VWAP.
        """
        result = await self._rpc_call("vwap_30min_latest", {"ticker": ticker})

        if isinstance(result, dict):
            return VWAP30MinData(
                ticker=result.get("ticker", ticker),
                vwap=float(result.get("price", result.get("vwap", 0))),
                quote_currency=result.get("quote", result.get("quote_currency", "EUR")),
                timestamp=_parse_timestamp(result.get("timestamp", result.get("ts"))),
                source="blocksize",
            )

        raise BlocksizeAPIError(-1, f"Unexpected response for vwap_30min: {result}")

    # -----------------------------------------------------------------------
    # 24-Hour VWAP / Closing Price (Crypto)
    # -----------------------------------------------------------------------

    async def get_vwap_24hr(self, pair: str) -> VWAP24HrData:
        """
        Get the 24-hour VWAP (closing price) for a crypto pair.

        Args:
            pair: Trading pair identifier (e.g., 'BTCUSD')

        Returns:
            VWAP24HrData with 24-hour closing VWAP and volume.
        """
        result = await self._rpc_call("vwap_24h_latest", {"ticker": pair})

        if isinstance(result, dict):
            return VWAP24HrData(
                pair=result.get("ticker", pair),
                vwap=float(result.get("price", result.get("vwap", 0))),
                volume=float(result.get("volume", 0)),
                timestamp=_parse_timestamp(result.get("timestamp", result.get("ts"))),
                source="blocksize",
            )

        raise BlocksizeAPIError(-1, f"Unexpected response for vwap_24h: {result}")

    # -----------------------------------------------------------------------
    # State Price (Crypto)
    # -----------------------------------------------------------------------

    async def get_state_price(self, pair: str) -> StatePriceData:
        """
        Get the state/reference price for a crypto pair.

        Args:
            pair: Trading pair identifier

        Returns:
            StatePriceData with the reference/settlement price.
        """
        result = await self._rpc_call("state_price_latest", {"ticker": pair})

        if isinstance(result, dict):
            return StatePriceData(
                pair=result.get("ticker", pair),
                price=float(result.get("price", 0)),
                timestamp=_parse_timestamp(result.get("timestamp")),
                source="blocksize",
            )

        raise BlocksizeAPIError(-1, f"Unexpected response for state_price: {result}")

    # -----------------------------------------------------------------------
    # Bid/Ask (Crypto)
    # -----------------------------------------------------------------------

    async def get_bidask_snapshot(self, pair: str) -> BidAskData:
        """
        Get the current best bid/ask snapshot for a crypto pair.

        Args:
            pair: Trading pair identifier (e.g., 'btc-usd')

        Returns:
            BidAskData with bid, ask, spread, and metadata.
        """
        result = await self._rpc_call("bidask_getSnapshot", {"ticker": pair})

        if isinstance(result, dict):
            item = result
            if "snapshot" in result:
                items = result["snapshot"]
                # Find the specific pair in the massive returned list, or default to empty dict
                item = next((x for x in items if x.get("ticker", "").upper() == pair.upper()), {})
            
            bid = float(item.get("agg_bid_price", item.get("bid", 0)))
            ask = float(item.get("agg_ask_price", item.get("ask", 0)))
            mid = float(item.get("agg_mid_price", item.get("mid", (bid + ask) / 2 if ask > 0 else 0)))
            
            spread = ask - bid
            spread_pct = (spread / ask * 100) if ask > 0 else 0.0

            return BidAskData(
                pair=item.get("ticker", pair),
                bid=bid,
                ask=ask,
                mid=mid,
                spread=spread,
                spread_pct=spread_pct,
                timestamp=_parse_timestamp(item.get("ts", item.get("timestamp"))),
                source="blocksize",
            )

        raise BlocksizeAPIError(-1, f"Unexpected response for bidask_getSnapshot: {result}")

    async def list_bidask_instruments(self) -> list[str]:
        """List all instruments available for bid/ask data."""
        result = await self._rpc_call("bidask_instruments")
        return self._extract_instrument_tickers(result)

    # -----------------------------------------------------------------------
    # Equities (US + Chinese)
    # -----------------------------------------------------------------------

    async def get_equity_snapshot(self, ticker: str) -> EquityData:
        """
        Get the latest snapshot for a US or Chinese equity.
        """
        # Blocksize deprecated specific equity endpoints; all data now routes via main snapshot array
        result = await self._rpc_call("bidask_getSnapshot", {"ticker": ticker})

        if isinstance(result, dict):
            item = result
            if "snapshot" in result:
                items = result["snapshot"]
                item = next((x for x in items if x.get("ticker", "").upper() == ticker.upper()), {})
            if not item:
                raise BlocksizeAPIError(404, f"Equity ticker {ticker} not found in master stream")
                
            return EquityData(
                ticker=str(item.get("ticker", ticker)),
                open=_safe_float(item.get("open")),
                high=_safe_float(item.get("high")),
                low=_safe_float(item.get("low")),
                last=_safe_float(item.get("last", item.get("agg_mid_price"))),  # Fallback to mid price
                bid=_safe_float(item.get("bidPrice", item.get("agg_bid_price"))),
                ask=_safe_float(item.get("askPrice", item.get("agg_ask_price"))),
                volume=_safe_float(item.get("volume", item.get("agg_bid_size"))),
                prev_close=_safe_float(item.get("prevClose")),
                timestamp=_parse_timestamp(item.get("ts", item.get("timestamp"))),
                source="blocksize",
            )

        raise BlocksizeAPIError(-1, f"Unexpected response for equity: {result}")

    async def list_equity_instruments(self) -> list[str]:
        """List available equity ticker symbols."""
        result = await self._rpc_call("bidask_equity_instruments")
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and "instruments" in result:
            return result["instruments"]
        return []

    # -----------------------------------------------------------------------
    # FX (Foreign Exchange)
    # -----------------------------------------------------------------------

    async def get_fx_rate(self, pair: str) -> FXData:
        """
        Get the current FX rate for a currency pair.
        """
        result = await self._rpc_call("bidask_getSnapshot", {"ticker": pair})

        if isinstance(result, dict):
            item = result
            if "snapshot" in result:
                items = result["snapshot"]
                item = next((x for x in items if x.get("ticker", "").upper() == pair.upper()), {})
            if not item:
                raise BlocksizeAPIError(404, f"FX pair {pair} not found in master stream")
                
            bid = _safe_float(item.get("agg_bid_price", item.get("bid")))
            ask = _safe_float(item.get("agg_ask_price", item.get("ask")))
            mid = _safe_float(item.get("agg_mid_price", item.get("mid")))
            if bid and ask and not mid:
                mid = (bid + ask) / 2

            base, quote = _split_pair(pair)
            return FXData(
                pair=pair,
                base_currency=item.get("baseCurrency", base),
                quote_currency=item.get("quoteCurrency", quote),
                bid=bid,
                ask=ask,
                mid=mid,
                timestamp=_parse_timestamp(item.get("ts", item.get("timestamp"))),
                source="blocksize",
            )

        raise BlocksizeAPIError(-1, f"Unexpected response for FX: {result}")

    async def list_fx_instruments(self) -> list[str]:
        """List available FX pairs derived from the shared bid/ask catalog."""
        entries = await self._list_bidask_entries()
        return sorted(
            entry["ticker"]
            for entry in entries
            if self._is_fx_entry(entry)
        )

    async def list_metal_instruments(self) -> list[str]:
        """Return the metal tickers supported by the HTTP gateway."""
        return sorted(METAL_TICKERS)

    # -----------------------------------------------------------------------
    # Metals & Commodities
    # -----------------------------------------------------------------------

    async def get_metal_price(self, ticker: str) -> MetalData:
        """
        Get the spot price for a precious/base metal.
        """
        result = await self._rpc_call("bidask_getSnapshot", {"ticker": ticker})

        metal_names = {
            "xauusd": "Gold", "xagusd": "Silver", "xptusd": "Platinum",
            "xpdusd": "Palladium", "copperusd": "Copper",
        }

        if isinstance(result, dict):
            item = result
            if "snapshot" in result:
                items = result["snapshot"]
                item = next((x for x in items if x.get("ticker", "").upper() == ticker.upper()), {})
            if not item:
                raise BlocksizeAPIError(404, f"Metal ticker {ticker} not found in master stream")

            return MetalData(
                ticker=ticker,
                name=item.get("name", metal_names.get(ticker.lower(), ticker)),
                price=float(item.get("agg_mid_price", item.get("price", 0))),
                currency=item.get("quoteCurrency", "USD"),
                timestamp=_parse_timestamp(item.get("ts", item.get("timestamp"))),
                source="blocksize",
            )

        raise BlocksizeAPIError(-1, f"Unexpected response for metal: {result}")


    # -----------------------------------------------------------------------
    # Search / Discovery
    # -----------------------------------------------------------------------

    async def search_pairs(self, query: str, asset_class: str = "all") -> list[PairInfo]:
        """
        Search through available trading pairs across all asset classes.

        Args:
            query: Search string (e.g., 'btc', 'AAPL', 'eurusd', 'gold')
            asset_class: Filter by class — 'crypto', 'equity', 'fx', 'metal', or 'all'

        Returns:
            List of matching PairInfo objects (max 50).
        """
        from src.config import TOP_250_CRYPTO

        query_lower = query.lower()
        matches: list[PairInfo] = []

        # Search crypto instruments
        if asset_class in ("all", "crypto"):
            try:
                vwap_instruments = set(await self.list_vwap_instruments())
                bidask_instruments = {
                    entry["ticker"]
                    for entry in await self._list_bidask_entries()
                    if not self._is_fx_entry(entry)
                    and not self._is_metal_entry(entry)
                    and not self._is_equity_like_entry(entry)
                }
                all_crypto = vwap_instruments | bidask_instruments

                for instrument in sorted(all_crypto):
                    if query_lower in instrument.lower() and len(matches) < 50:
                        services = []
                        if instrument in vwap_instruments:
                            services.append("vwap")
                        if instrument in bidask_instruments:
                            services.append("bidask")

                        base, quote = _split_pair(instrument)
                        tier = "core" if base in TOP_250_CRYPTO else "extended"
                        matches.append(PairInfo(
                            pair=instrument,
                            base_currency=base,
                            quote_currency=quote,
                            asset_class="crypto",
                            services=services,
                            tier=tier,
                        ))
            except BlocksizeAPIError:
                logger.warning("Could not search crypto instruments")

        # Search FX
        if asset_class in ("all", "fx") and len(matches) < 50:
            try:
                fx_instruments = await self.list_fx_instruments()
                for inst in fx_instruments:
                    if query_lower in inst.lower() and len(matches) < 50:
                        base, quote = _split_pair(inst)
                        matches.append(PairInfo(
                            pair=inst,
                            base_currency=base,
                            quote_currency=quote,
                            asset_class="fx",
                            services=["bidask"],
                            tier="tradfi",
                        ))
            except BlocksizeAPIError:
                logger.warning("Could not search FX instruments")

        # Search metals
        if asset_class in ("all", "metal") and len(matches) < 50:
            for inst in await self.list_metal_instruments():
                if query_lower in inst.lower() and len(matches) < 50:
                    base, quote = _split_pair(inst)
                    matches.append(PairInfo(
                        pair=inst,
                        base_currency=base,
                        quote_currency=quote,
                        asset_class="metal",
                        services=["metal"],
                        tier="tradfi",
                    ))

        return matches

    async def _list_bidask_entries(self) -> list[dict[str, str]]:
        """Return normalized bid/ask instrument entries from the shared catalog."""
        result = await self._rpc_call("bidask_instruments")
        return self._extract_instrument_entries(result)

    @staticmethod
    def _extract_instrument_entries(result: Any) -> list[dict[str, str]]:
        """Normalize instrument payloads into ticker/base/quote records."""
        if isinstance(result, dict) and "instruments" in result:
            raw_items = result["instruments"]
        elif isinstance(result, list):
            raw_items = result
        else:
            raw_items = []

        entries: list[dict[str, str]] = []
        for item in raw_items:
            if isinstance(item, str):
                ticker = item.upper()
                base, quote = _split_pair(ticker)
                entries.append({
                    "ticker": ticker,
                    "base_currency": base,
                    "quote_currency": quote,
                })
                continue

            if not isinstance(item, dict):
                continue

            ticker = str(item.get("ticker", "")).upper()
            if not ticker:
                continue

            base = str(
                item.get("base_currency")
                or item.get("baseCurrency")
                or _split_pair(ticker)[0]
            ).upper()
            quote = str(
                item.get("quote_currency")
                or item.get("quoteCurrency")
                or _split_pair(ticker)[1]
            ).upper()
            entries.append({
                "ticker": ticker,
                "base_currency": base,
                "quote_currency": quote,
            })

        return entries

    @classmethod
    def _extract_instrument_tickers(cls, result: Any) -> list[str]:
        """Extract just the instrument ticker strings from an RPC result."""
        return [entry["ticker"] for entry in cls._extract_instrument_entries(result)]

    @staticmethod
    def _is_fx_entry(entry: dict[str, str]) -> bool:
        """Return True when an instrument looks like an FX pair."""
        return (
            entry["base_currency"] in FIAT_CURRENCIES
            and entry["quote_currency"] in FIAT_CURRENCIES
        )

    @staticmethod
    def _is_metal_entry(entry: dict[str, str]) -> bool:
        """Return True when an instrument is one of the supported metals."""
        return entry["ticker"] in METAL_TICKERS

    @staticmethod
    def _is_equity_like_entry(entry: dict[str, str]) -> bool:
        """Identify exchange-listed equity-like symbols in the shared bid/ask feed."""
        return (
            entry["base_currency"].endswith("X")
            and entry["quote_currency"] in {"USD", "USDT", "USDC"}
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_timestamp(ts: Any) -> datetime:
    """Parse a timestamp from various formats into a datetime."""
    if ts is None:
        return datetime.now(timezone.utc)

    if isinstance(ts, (int, float)):
        # Handle microseconds (> 1e14)
        if ts > 1e14:
            ts = ts / 1e6
        # Handle milliseconds (> 1e11)
        elif ts > 1e11:
            ts = ts / 1000
        return datetime.fromtimestamp(ts, tz=timezone.utc)

    if isinstance(ts, str):
        try:
            return datetime.fromisoformat(ts.replace("Z", "+00:00"))
        except ValueError:
            pass

    return datetime.now(timezone.utc)


def _extract_quote(pair: str) -> str:
    """Extract the quote currency from a pair string."""
    for sep in ["-", "/", "_"]:
        if sep in pair:
            return pair.split(sep)[-1].upper()
    return pair[-3:].upper() if len(pair) >= 6 else ""


def _split_pair(pair: str) -> tuple[str, str]:
    """Split a pair into base and quote currencies."""
    for sep in ["-", "/", "_"]:
        if sep in pair:
            parts = pair.split(sep, 1)
            return parts[0].upper(), parts[1].upper()
    mid = len(pair) // 2
    return pair[:mid].upper(), pair[mid:].upper()


def _safe_float(val: Any) -> float | None:
    """Safely convert a value to float, returning None on failure."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None
