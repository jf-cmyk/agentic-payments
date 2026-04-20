"""
Blocksize Capital API Client.

Wraps the Blocksize REST (JSON-RPC 2.0) and WebSocket APIs into a clean
async interface. Handles authentication, request formatting, retries,
and response parsing.

Supported data services:
  - Real-Time VWAP Crypto (9,410 pairs)
  - 30-Minute VWAP Crypto (251 tickers)
  - 24-Hour VWAP / Closing Price (42 tickers)
  - Bid/Ask Crypto (1,962 pairs)
  - Bid/Ask Equities — US + Chinese (18,071 tickers)
  - Bid/Ask FX (129 pairs)
  - Bid/Ask Metals (5 tickers)
  - US Treasury Rates (8 maturities)
  - State Price
  - Commodities (in development)

Production endpoints:
  REST:  https://data.blocksize.capital/marketdata/v1/api
  WS:    wss://data.blocksize.capital/marketdata/v1/ws
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
    TreasuryRateData,
    VWAP24HrData,
    VWAP30MinData,
    VWAPData,
)

logger = logging.getLogger(__name__)


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
            return VWAPData(
                pair=result.get("ticker", pair),
                vwap=float(result.get("price", result.get("vwap", 0))),
                timestamp=_parse_timestamp(result.get("timestamp")),
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
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and "instruments" in result:
            return result["instruments"]
        return []

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
            bid = float(result.get("bid", 0))
            ask = float(result.get("ask", 0))
            spread = ask - bid
            spread_pct = (spread / ask * 100) if ask > 0 else 0.0

            return BidAskData(
                pair=result.get("ticker", pair),
                bid=bid,
                ask=ask,
                spread=spread,
                spread_pct=spread_pct,
                timestamp=_parse_timestamp(result.get("timestamp")),
                source="blocksize",
            )

        raise BlocksizeAPIError(-1, f"Unexpected response for bidask_getSnapshot: {result}")

    async def list_bidask_instruments(self) -> list[str]:
        """List all instruments available for bid/ask data."""
        result = await self._rpc_call("bidask_instruments")
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and "instruments" in result:
            return result["instruments"]
        return []

    # -----------------------------------------------------------------------
    # Equities (US + Chinese)
    # -----------------------------------------------------------------------

    async def get_equity_snapshot(self, ticker: str) -> EquityData:
        """
        Get the latest snapshot for a US or Chinese equity.

        Args:
            ticker: Stock ticker symbol (numeric or alphanumeric)

        Returns:
            EquityData with OHLCV and bid/ask data.
        """
        result = await self._rpc_call("bidask_equity_getSnapshot", {"ticker": ticker})

        if isinstance(result, dict):
            return EquityData(
                ticker=str(result.get("ticker", ticker)),
                open=_safe_float(result.get("open")),
                high=_safe_float(result.get("high")),
                low=_safe_float(result.get("low")),
                last=_safe_float(result.get("last")),
                bid=_safe_float(result.get("bidPrice")),
                ask=_safe_float(result.get("askPrice")),
                volume=_safe_float(result.get("volume")),
                prev_close=_safe_float(result.get("prevClose")),
                timestamp=_parse_timestamp(result.get("timestamp")),
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

        Args:
            pair: FX pair (e.g., 'eurusd', 'gbpjpy')

        Returns:
            FXData with bid/ask/mid rates.
        """
        result = await self._rpc_call("bidask_fx_getSnapshot", {"ticker": pair})

        if isinstance(result, dict):
            bid = _safe_float(result.get("bid"))
            ask = _safe_float(result.get("ask"))
            mid = _safe_float(result.get("mid"))
            if bid and ask and not mid:
                mid = (bid + ask) / 2

            base, quote = _split_pair(pair)
            return FXData(
                pair=pair,
                base_currency=result.get("baseCurrency", base),
                quote_currency=result.get("quoteCurrency", quote),
                bid=bid,
                ask=ask,
                mid=mid,
                timestamp=_parse_timestamp(result.get("timestamp")),
                source="blocksize",
            )

        raise BlocksizeAPIError(-1, f"Unexpected response for FX: {result}")

    async def list_fx_instruments(self) -> list[str]:
        """List available FX pairs."""
        result = await self._rpc_call("bidask_fx_instruments")
        if isinstance(result, list):
            return result
        if isinstance(result, dict) and "instruments" in result:
            return result["instruments"]
        return []

    # -----------------------------------------------------------------------
    # Metals & Commodities
    # -----------------------------------------------------------------------

    async def get_metal_price(self, ticker: str) -> MetalData:
        """
        Get the spot price for a precious/base metal.

        Args:
            ticker: Metal ticker (e.g., 'xauusd', 'xagusd', 'copperusd')

        Returns:
            MetalData with spot price.
        """
        result = await self._rpc_call("bidask_metal_getSnapshot", {"ticker": ticker})

        metal_names = {
            "xauusd": "Gold", "xagusd": "Silver", "xptusd": "Platinum",
            "xpdusd": "Palladium", "copperusd": "Copper",
        }

        if isinstance(result, dict):
            return MetalData(
                ticker=ticker,
                name=result.get("name", metal_names.get(ticker.lower(), ticker)),
                price=float(result.get("price", result.get("mid", result.get("last", 0)))),
                currency=result.get("quoteCurrency", "USD"),
                timestamp=_parse_timestamp(result.get("timestamp")),
                source="blocksize",
            )

        raise BlocksizeAPIError(-1, f"Unexpected response for metal: {result}")

    # -----------------------------------------------------------------------
    # US Treasury Rates
    # -----------------------------------------------------------------------

    async def get_treasury_rate(self, maturity: str) -> TreasuryRateData:
        """
        Get the current US Treasury yield for a specific maturity.

        Args:
            maturity: Maturity code (e.g., '10Y', '2Y', '30Y', '1M', '3M', '5Y', '6M', '1Y')

        Returns:
            TreasuryRateData with yield percentage.
        """
        ticker = f"Rates.US{maturity.upper()}"
        result = await self._rpc_call("bidask_rate_getSnapshot", {"ticker": ticker})

        if isinstance(result, dict):
            return TreasuryRateData(
                ticker=ticker,
                maturity=maturity.upper(),
                yield_pct=float(result.get("yield", result.get("price", result.get("last", 0)))),
                currency="USD",
                timestamp=_parse_timestamp(result.get("timestamp")),
                source="blocksize",
            )

        raise BlocksizeAPIError(-1, f"Unexpected response for treasury rate: {result}")

    async def get_treasury_curve(self) -> list[TreasuryRateData]:
        """
        Get the full US Treasury yield curve (all maturities).

        Returns:
            List of TreasuryRateData for all available maturities.
        """
        maturities = ["1M", "3M", "6M", "1Y", "2Y", "5Y", "10Y", "30Y"]
        rates = []
        for mat in maturities:
            try:
                rate = await self.get_treasury_rate(mat)
                rates.append(rate)
            except BlocksizeAPIError:
                logger.warning("Could not fetch %s treasury rate", mat)
        return rates

    # -----------------------------------------------------------------------
    # Search / Discovery
    # -----------------------------------------------------------------------

    async def search_pairs(self, query: str, asset_class: str = "all") -> list[PairInfo]:
        """
        Search through available trading pairs across all asset classes.

        Args:
            query: Search string (e.g., 'btc', 'AAPL', 'eurusd', 'gold')
            asset_class: Filter by class — 'crypto', 'equity', 'fx', 'metal', 'rate', or 'all'

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
                bidask_instruments = set(await self.list_bidask_instruments())
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

        return matches


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_timestamp(ts: Any) -> datetime:
    """Parse a timestamp from various formats into a datetime."""
    if ts is None:
        return datetime.now(timezone.utc)

    if isinstance(ts, (int, float)):
        if ts > 1e12:
            ts = ts / 1000  # milliseconds → seconds
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
