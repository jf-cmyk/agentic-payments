"""
Blocksize Capital API client.

The deployed Agentic Payments gateway currently uses JSON-RPC 2.0 over HTTP.
It does not maintain websocket subscriptions itself.

Verified upstream HTTP methods for the deployed key:
  - vwap_latest
  - vwap_instruments
  - bidask_getSnapshot
  - bidask_instruments
  - state_instruments
  - state_pool
  - closingprice_list
  - closingprice_trades

The public docs expose 24-hour fixed VWAP and aggregate state subscriptions
over websocket. Those are handled by src.blocksize_stream_cache.
"""

from __future__ import annotations

import logging
import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from src.config import TOP_250_CRYPTO, settings
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
from src.instrument_discovery import commercialize_pair, rank_pair_candidates

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
PAIR_QUOTE_SUFFIXES = ("USDT", "USDC", "USD", "EUR", "GBP", "JPY", "BTC", "ETH")


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
        clean = _normalize_ticker(ticker)
        quote = _extract_quote(clean)
        base = clean[: -len(quote)] if quote and len(clean) > len(quote) else clean
        if not quote:
            quote = "USD"
        result = await self._rpc_call(
            "closingprice_list",
            {"ts": _latest_completed_30m_ms(), "quote": quote},
        )

        if isinstance(result, dict) and isinstance(result.get("prices"), list):
            match = next(
                (
                    item
                    for item in result["prices"]
                    if isinstance(item, dict)
                    and str(item.get("base", "")).upper() == base
                    and str(item.get("quote", "")).upper() == quote
                ),
                None,
            )
            if match:
                return VWAP30MinData(
                    ticker=base,
                    vwap=float(match.get("price", 0)),
                    quote_currency=quote,
                    timestamp=_parse_timestamp(match.get("timestamp", match.get("ts"))),
                    source="blocksize",
                )
            raise BlocksizeAPIError(-32000, f"closingprice_list did not include {base}{quote}")

        raise BlocksizeAPIError(-1, f"Unexpected response for closingprice_list: {result}")

    async def get_vwap_30min_trades(self, ticker: str, *, limit: int = 25) -> list[dict[str, Any]]:
        """Return trade evidence for the latest completed 30-minute close."""
        clean = _normalize_ticker(ticker)
        quote = _extract_quote(clean)
        base = clean[: -len(quote)] if quote and len(clean) > len(quote) else clean
        if not quote:
            quote = "USD"
        result = await self._rpc_call(
            "closingprice_trades",
            {"base": base, "quote": quote, "ts": _latest_completed_30m_ms()},
        )
        if isinstance(result, dict) and isinstance(result.get("prices"), list):
            return [item for item in result["prices"][:limit] if isinstance(item, dict)]
        raise BlocksizeAPIError(-1, f"Unexpected response for closingprice_trades: {result}")

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
        try:
            result = await self._rpc_call("vwap_24h_latest", {"ticker": pair})
        except BlocksizeAPIError as exc:
            if exc.code == -32601:
                raise BlocksizeAPIError(
                    -32004,
                    "24-hour fixed VWAP is websocket-only in the public docs; enable "
                    "the Blocksize stream cache for HTTP reads.",
                ) from exc
            raise

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

    async def get_state_pool(
        self,
        *,
        network: str,
        pool: str,
        symbol: str | None = None,
    ) -> dict[str, Any]:
        """Return the documented HTTP AMM state snapshot for a pool."""
        params: dict[str, Any] = {"network": network, "pool": pool}
        if symbol:
            params["symbol"] = symbol
        result = await self._rpc_call("state_pool", params)
        if isinstance(result, dict) and isinstance(result.get("state"), dict):
            state = result["state"]
            state.setdefault("network", network)
            state.setdefault("pool", pool)
            if symbol:
                state.setdefault("symbol", symbol)
            return state
        if isinstance(result, dict):
            return result
        raise BlocksizeAPIError(-1, f"Unexpected response for state_pool: {result}")

    async def get_state_price(self, pair: str) -> StatePriceData:
        """
        Get the state/reference price for a crypto pair.

        The documented HTTP state path is pool-level (`state_pool`), not a
        ticker-level `state_price_latest` method. We resolve the requested
        symbol through `state_instruments`, call available pools, and derive a
        weighted state price where possible.

        Args:
            pair: Trading pair identifier

        Returns:
            StatePriceData with the reference/settlement price.
        """
        pool_error: BlocksizeAPIError | None = None
        try:
            return await self._get_state_price_from_pools(pair)
        except BlocksizeAPIError as exc:
            pool_error = exc
            if exc.code not in {-32000, -32601, -32602, -32603, -1}:
                raise

        method_candidates = [
            os.getenv("BLOCKSIZE_STATE_PRICE_METHOD", "").strip(),
            "state_price_latest",
            "state_latest",
            "state_getLatest",
            "state_get_latest",
            "state_price",
            "state_getPrice",
            "state_get_price",
            "reference_price_latest",
            "reference_latest",
            "reference_price",
        ]
        last_method_not_found: BlocksizeAPIError | None = None
        result: Any = None
        for method in [item for item in method_candidates if item]:
            try:
                result = await self._rpc_call(method, {"ticker": pair})
                break
            except BlocksizeAPIError as exc:
                if exc.code == -32601:
                    last_method_not_found = exc
                    continue
                raise
        else:
            if last_method_not_found:
                raise pool_error or last_method_not_found
            raise BlocksizeAPIError(-32601, "No state/reference price RPC method configured")

        if isinstance(result, dict):
            state_payload = result.get("state") or result.get("price") or result.get("state_price") or result
            if isinstance(state_payload, dict):
                result = state_payload
            price = _first_float(result, ("price", "state_price", "reference_price", "value", "mid"))
            if price is None:
                raise BlocksizeAPIError(-1, f"State price response did not include a price field: {result}")
            return StatePriceData(
                pair=result.get("ticker", pair),
                price=price,
                timestamp=_parse_timestamp(result.get("timestamp", result.get("ts"))),
                source="blocksize",
            )

        raise BlocksizeAPIError(-1, f"Unexpected response for state_price: {result}")

    async def _get_state_price_from_pools(self, pair: str) -> StatePriceData:
        clean = _normalize_ticker(pair)
        instruments = await self.list_state_instruments()
        matches = _matching_state_instruments(clean, instruments)
        if not matches:
            raise BlocksizeAPIError(
                -32000,
                f"No state_instruments pool coverage for {clean}",
            )

        pool_states: list[dict[str, Any]] = []
        errors: list[str] = []
        for instrument in matches:
            symbol = str(instrument.get("symbol") or clean).upper()
            pools = instrument.get("pools") if isinstance(instrument.get("pools"), list) else []
            for pool in pools:
                if not isinstance(pool, dict) or not pool.get("network") or not pool.get("address"):
                    continue
                try:
                    state = await self.get_state_pool(
                        network=str(pool["network"]),
                        pool=str(pool["address"]),
                        symbol=symbol,
                    )
                except BlocksizeAPIError as exc:
                    errors.append(f"{symbol}:{pool.get('network')}:{exc.code}:{exc.message}")
                    continue
                if _first_float(state, ("state_price_usd", "price", "state_price", "state")) is not None:
                    pool_states.append(state)

        if not pool_states:
            detail = "; ".join(errors[:3]) if errors else "no pools returned a usable price"
            raise BlocksizeAPIError(
                -32000,
                f"No usable state_pool price for {clean}: {detail}",
            )

        weighted_sum = 0.0
        weight_sum = 0.0
        unweighted: list[float] = []
        latest_ts: Any = None
        for state in pool_states:
            price = _first_float(state, ("state_price_usd", "price", "state_price", "state"))
            if price is None:
                continue
            unweighted.append(price)
            weight = _first_float(state, ("weight",))
            if weight is not None and weight > 0:
                weighted_sum += price * weight
                weight_sum += weight
            latest_ts = state.get("block_time") or state.get("timestamp") or state.get("ts") or latest_ts

        if not unweighted:
            raise BlocksizeAPIError(-1, f"state_pool responses did not include a price field for {clean}")

        price = weighted_sum / weight_sum if weight_sum else sum(unweighted) / len(unweighted)
        return StatePriceData(
            pair=clean,
            price=price,
            timestamp=_parse_timestamp(latest_ts),
            source="blocksize",
        )

    async def list_state_instruments(self) -> list[dict[str, Any]]:
        """List available state-data instruments and their pool/network metadata."""
        result = await self._rpc_call("state_instruments")
        if isinstance(result, dict) and isinstance(result.get("instruments"), list):
            return result["instruments"]
        if isinstance(result, list):
            return result
        return []

    # -----------------------------------------------------------------------
    # Bid/Ask (Shared Multi-Asset Namespace)
    # -----------------------------------------------------------------------

    async def get_bidask_snapshot(self, pair: str) -> BidAskData:
        """
        Get the current best bid/ask snapshot for a shared bid/ask symbol.

        Args:
            pair: Trading pair or ticker identifier (e.g., 'btc-usd', 'AAPL')

        Returns:
            BidAskData with bid, ask, spread, and metadata.
        """
        result = await self._rpc_call("bidask_getSnapshot", {"ticker": pair})

        if isinstance(result, dict):
            item = result
            if "snapshot" in result:
                items = result["snapshot"]
                item = _find_snapshot_item(items, pair)
            if not item:
                raise BlocksizeAPIError(404, f"Bid/ask ticker {pair} not found in master stream")
            
            bid = _first_float(item, ("agg_bid_price", "bid", "bidPrice")) or 0.0
            ask = _first_float(item, ("agg_ask_price", "ask", "askPrice")) or 0.0
            mid = _first_float(item, ("agg_mid_price", "mid", "last", "price"))
            if mid is None:
                mid = (bid + ask) / 2 if ask > 0 else 0.0
            
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
        """List all instruments available in the shared bid/ask namespace."""
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
                item = _find_snapshot_item(items, ticker)
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
        matches = await self._search_pair_candidates(query, asset_class, strict=False)
        return matches[:50]

    async def search_pairs_page(
        self,
        query: str,
        asset_class: str = "all",
        *,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[PairInfo], int]:
        """Return one search page and the honest total across searched catalogs."""
        if limit < 1:
            raise ValueError("limit must be at least 1")
        if offset < 0:
            raise ValueError("offset must be non-negative")
        matches = await self._search_pair_candidates(query, asset_class, strict=True)
        return matches[offset : offset + limit], len(matches)

    async def _search_pair_candidates(
        self,
        query: str,
        asset_class: str = "all",
        *,
        strict: bool,
    ) -> list[PairInfo]:
        """Collect every matching instrument before response pagination."""
        asset_filter = asset_class.lower().strip()
        if asset_filter == "equities":
            asset_filter = "equity"

        candidates: list[PairInfo] = []
        bidask_entries: list[dict[str, str]] | None = None

        async def _bidask_entries() -> list[dict[str, str]]:
            nonlocal bidask_entries
            if bidask_entries is None:
                bidask_entries = await self._list_bidask_entries()
            return bidask_entries

        # Search crypto instruments
        if asset_filter in ("all", "crypto"):
            try:
                vwap_instruments = set(await self.list_vwap_instruments())
                bidask_instruments = {
                    entry["ticker"]
                    for entry in await _bidask_entries()
                    if not self._is_fx_entry(entry)
                    and not self._is_metal_entry(entry)
                    and not self._is_equity_like_entry(entry)
                }
                all_crypto = vwap_instruments | bidask_instruments

                for instrument in sorted(all_crypto):
                    base, quote = _split_pair(instrument)
                    services = []
                    if instrument in vwap_instruments:
                        services.append("vwap")
                    if instrument in bidask_instruments:
                        services.append("bidask")

                    tier = "core" if base in TOP_250_CRYPTO else "extended"
                    candidates.append(PairInfo(
                        pair=instrument,
                        base_currency=base,
                        quote_currency=quote,
                        asset_class="crypto",
                        services=services,
                        capability_check_services=(
                            ["vwap30m", "vwap24h"] if "vwap" in services else []
                        ),
                        tier=tier,
                    ))
            except BlocksizeAPIError:
                if strict:
                    raise
                logger.warning("Could not search crypto instruments")

        # Search equities in the shared bid/ask namespace.
        if asset_filter in ("all", "equity"):
            try:
                equity_entries = [
                    entry
                    for entry in await _bidask_entries()
                    if self._is_equity_like_entry(entry)
                ]
                for entry in sorted(equity_entries, key=lambda item: item["ticker"]):
                    ticker = entry["ticker"]
                    base = entry["base_currency"]
                    candidates.append(PairInfo(
                        pair=ticker,
                        base_currency=base.removesuffix("X"),
                        quote_currency=entry["quote_currency"],
                        asset_class="equity",
                        services=["bidask"],
                        tier="equities",
                    ))
            except BlocksizeAPIError:
                if strict:
                    raise
                logger.warning("Could not search equity instruments")

        # Search FX
        if asset_filter in ("all", "fx"):
            try:
                fx_instruments = sorted(
                    entry["ticker"]
                    for entry in await _bidask_entries()
                    if self._is_fx_entry(entry)
                )
                for inst in fx_instruments:
                    base, quote = _split_pair(inst)
                    candidates.append(PairInfo(
                        pair=inst,
                        base_currency=base,
                        quote_currency=quote,
                        asset_class="fx",
                        services=["fx"],
                        tier="tradfi",
                    ))
            except BlocksizeAPIError:
                if strict:
                    raise
                logger.warning("Could not search FX instruments")

        # Search metals
        if asset_filter in ("all", "metal"):
            for inst in await self.list_metal_instruments():
                base, quote = _split_pair(inst)
                candidates.append(PairInfo(
                    pair=inst,
                    base_currency=base,
                    quote_currency=quote,
                    asset_class="metal",
                    services=["metal"],
                    tier="tradfi",
                ))

        ranked = rank_pair_candidates(
            query,
            candidates,
            diversify_asset_classes=asset_filter == "all",
        )
        return [commercialize_pair(pair, settings.pricing) for pair in ranked]

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
                    "asset_class": "",
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
            asset_class = str(
                item.get("asset_class")
                or item.get("assetClass")
                or item.get("class")
                or item.get("type")
                or item.get("category")
                or ""
            ).lower()
            entries.append({
                "ticker": ticker,
                "base_currency": base,
                "quote_currency": quote,
                "asset_class": asset_class,
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
        asset_class = entry.get("asset_class", "").lower()
        if asset_class in {"equity", "equities", "stock", "stocks"}:
            return True

        ticker = entry["ticker"]
        base = entry["base_currency"]
        quote = entry["quote_currency"]
        return (
            base.endswith("X")
            and quote in {"USD", "USDT", "USDC"}
        ) or (
            ticker.isalpha()
            and 1 <= len(ticker) <= 5
            and ticker not in TOP_250_CRYPTO
            and quote not in FIAT_CURRENCIES
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_snapshot_item(items: Any, ticker: str) -> dict[str, Any]:
    """Resolve bare equity tickers against upstream USD-suffixed snapshot symbols."""
    if not isinstance(items, list):
        return {}
    clean = _normalize_ticker(ticker)
    candidates = {clean}
    if clean.isalpha() and 1 <= len(clean) <= 5 and not clean.endswith("USD"):
        candidates.add(f"{clean}USD")
    return next(
        (
            item
            for item in items
            if isinstance(item, dict)
            and _normalize_ticker(str(item.get("ticker", ""))) in candidates
        ),
        {},
    )


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
    clean = pair.upper()
    for quote in PAIR_QUOTE_SUFFIXES:
        if clean.endswith(quote) and len(clean) > len(quote):
            return quote
    return ""


def _split_pair(pair: str) -> tuple[str, str]:
    """Split a pair only when separators or an authoritative quote suffix exist."""
    for sep in ["-", "/", "_"]:
        if sep in pair:
            parts = pair.split(sep, 1)
            return parts[0].upper(), parts[1].upper()
    clean = pair.upper()
    quote = _extract_quote(clean)
    if quote:
        return clean[: -len(quote)], quote
    return clean, ""


def _normalize_ticker(ticker: str) -> str:
    """Normalize ticker strings to Blocksize's compact uppercase form."""
    return (
        ticker.replace("-", "")
        .replace("/", "")
        .replace("_", "")
        .replace(" ", "")
        .upper()
    )


def _latest_completed_30m_ms() -> int:
    """Return the latest completed UTC 30-minute boundary in milliseconds."""
    now = datetime.now(timezone.utc)
    minute = 30 if now.minute >= 30 else 0
    boundary = now.replace(minute=minute, second=0, microsecond=0)
    if boundary >= now:
        boundary = boundary - timedelta(minutes=30)
    return int(boundary.timestamp() * 1000)


def _matching_state_instruments(symbol: str, instruments: list[Any]) -> list[dict[str, Any]]:
    """Find state instruments that are exact or common stable-quote variants."""
    clean = _normalize_ticker(symbol)
    base, _quote = _split_pair(clean)
    target_symbols = {clean, f"{base}USD", f"{base}USDC", f"{base}USDT"}
    matches: list[dict[str, Any]] = []
    for item in instruments:
        if not isinstance(item, dict):
            continue
        item_symbol = str(item.get("symbol") or "").upper()
        if item_symbol in target_symbols:
            matches.append(item)
    return matches


def _safe_float(val: Any) -> float | None:
    """Safely convert a value to float, returning None on failure."""
    if val is None:
        return None
    try:
        return float(val)
    except (ValueError, TypeError):
        return None


def _first_float(item: dict[str, Any], keys: tuple[str, ...]) -> float | None:
    """Return the first parseable numeric value from a response payload."""
    for key in keys:
        value = _safe_float(item.get(key))
        if value is not None:
            return value
    return None
