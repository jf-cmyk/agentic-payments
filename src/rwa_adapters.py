"""Adapter contracts and venue registry for RWA market-data feeds."""

from __future__ import annotations

import os
import asyncio
import base64
import json
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from src.cex_stream_cache import CEXBookCache, CEXBookUnavailable

from src.rwa_coverage import VENUES
from src.rwa_hyperliquid import (
    HYPERLIQUID_RWA_SPOT_SYMBOLS,
    HYPERLIQUID_RWA_SPOT_VENUE_ID,
    hyperliquid_is_unverified,
    hyperliquid_normalized_asset_class,
    hyperliquid_symbol_by_alias,
)
from src.rwa_hyperliquid_discovery import (
    HYPERLIQUID_PERPS_VENUE_ID,
    HYPERLIQUID_SPOT_VENUE_ID,
    load_hyperliquid_tradeable_coverage_rows,
)
from src.rwa_clmm_replay import (
    decode_signed_word,
    encode_signed_argument,
    simulate_exact_input,
    summarize_swap_logs,
)
from src.runtime_data import RWA_REPORTS_DIR, resolve_required_rwa_report_path


JUPITER_DEFAULT_TOKEN_MINTS: dict[str, dict[str, Any]] = {
    "USDC": {
        "symbol": "USDC",
        "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "decimals": 6,
        "source": "jupiter_docs_example",
        "status": "verified_quote_token",
    },
    "USD": {
        "symbol": "USDC",
        "mint": "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
        "decimals": 6,
        "source": "usd_proxy_to_usdc",
        "status": "quote_proxy",
    },
}

JUPITER_ASSET_CLASS_BY_BASE: dict[str, str] = {
    "AAPLX": "equity",
    "AMZNX": "equity",
    "MSFTX": "equity",
    "NVDAX": "equity",
    "TSLAX": "equity",
    "METAX": "equity",
    "SPYX": "etf",
    "QQQX": "etf",
    "VOOX": "etf",
    "SGOVX": "etf",
    "TBLLX": "etf",
    "EURC": "fx",
    "USDY": "treasury_fund",
    "OUSG": "treasury_fund",
}

DEFAULT_REPORTS_DIR = RWA_REPORTS_DIR


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _iso_now() -> str:
    return datetime.now(UTC).isoformat()


def _iso_from_epoch(value: Any) -> str | None:
    if value in {None, ""}:
        return None
    try:
        raw = float(value)
    except (TypeError, ValueError):
        return None
    if raw > 10_000_000_000_000_000:
        raw /= 1_000_000_000
    elif raw > 10_000_000_000_000:
        raw /= 1_000
    elif raw > 10_000_000_000:
        raw /= 1_000
    return datetime.fromtimestamp(raw, tz=UTC).isoformat()


def _float_value(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _clean_symbol_key(value: str) -> str:
    return value.strip().upper().replace("-", "/").replace(" ", "")


def _base_symbol(symbol: str) -> str:
    return _clean_symbol_key(symbol).split("/", 1)[0]


def _compact_market(symbol: str) -> str:
    return _clean_symbol_key(symbol).replace("/", "")


def _levels_from_lists(rows: Any) -> list[dict[str, float]]:
    levels: list[dict[str, float]] = []
    if not isinstance(rows, list):
        return levels
    for row in rows:
        if isinstance(row, dict):
            price = _float_value(row.get("price") or row.get("p"))
            size = _float_value(
                row.get("size")
                or row.get("sz")
                or row.get("remaining_base_amount")
                or row.get("quantity")
                or row.get("q")
            )
        elif isinstance(row, (list, tuple)) and len(row) >= 2:
            price = _float_value(row[0])
            size = _float_value(row[1])
        else:
            continue
        if price is not None and size is not None and price > 0 and size > 0:
            levels.append({"price": price, "size": size})
    return levels


def _iter_dicts(value: Any) -> Any:
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _iter_dicts(child)
    elif isinstance(value, list):
        for child in value:
            yield from _iter_dicts(child)


def _load_derivative_rows(venue_id: str) -> list[dict[str, Any]]:
    payload = _read_json_file(
        resolve_required_rwa_report_path("rwa_derivative_venue_discovery.json")
    )
    rows = payload.get("coverage_rows")
    if not isinstance(rows, list):
        return []
    return [
        row for row in rows
        if isinstance(row, dict) and str(row.get("venue") or "") == venue_id
    ]


def _derivative_aliases(venue_id: str) -> dict[str, dict[str, Any]]:
    aliases: dict[str, dict[str, Any]] = {}
    for row in _load_derivative_rows(venue_id):
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        for value in (
            row.get("symbol"),
            row.get("asset_id"),
            metadata.get("venue_symbol"),
            metadata.get("venue_market_id"),
            metadata.get("cross_symbol_name"),
        ):
            if value not in {None, ""}:
                aliases[_clean_symbol_key(str(value))] = row
                aliases[_compact_market(str(value))] = row
        symbol = str(row.get("symbol") or "")
        if symbol:
            aliases[_base_symbol(symbol)] = row
            aliases[_compact_market(symbol)] = row
    return aliases


def _load_jupiter_token_mints() -> dict[str, dict[str, Any]]:
    token_mints: dict[str, dict[str, Any]] = {}
    payload = _read_json_file(
        resolve_required_rwa_report_path("rwa_solana_token_mints.json")
    )
    rows = payload.get("tokens") if isinstance(payload.get("tokens"), list) else []
    for row in rows:
        if not isinstance(row, dict) or row.get("mint") in {None, ""} or row.get("decimals") is None:
            continue
        # Only reviewed/resolved registry rows may enter quote routing.  Keeping a
        # mint in the discovery catalog is not enough to make it probeable: stale,
        # ambiguous and explicitly rejected rows must remain visible as blocked
        # inventory instead of being sent repeatedly to Jupiter.
        discovery_status = str(row.get("status") or "").strip().lower()
        if discovery_status not in {"resolved", "verified", "configured"}:
            continue
        token = {
            "symbol": str(row.get("symbol") or row.get("query_symbol") or row.get("token_key") or "").upper(),
            "mint": str(row["mint"]),
            "decimals": int(row["decimals"]),
            "source": row.get("source", "rwa_solana_token_mint_registry"),
            "status": row.get("review_status") or row.get("status") or "configured",
            "review_status": row.get("review_status"),
            "liquidity": row.get("liquidity"),
            "organic_score": row.get("organic_score"),
        }
        roles = {str(role).lower() for role in row.get("roles") or []}
        keys = {
            str(row.get("token_key") or ""),
            str(row.get("symbol") or ""),
            str(row.get("query_symbol") or ""),
        }
        if "base" in roles:
            for source_symbol in row.get("source_symbols") or []:
                keys.add(_base_symbol(str(source_symbol)))
        for key in keys:
            clean = JupiterRouterAdapter._token_key(key) if key else ""
            if clean:
                token_mints[clean] = token
    return token_mints


def _load_jupiter_blocked_tokens() -> dict[str, str]:
    """Load known non-tradable Jupiter symbols from the route allowlist evidence."""
    payload = _read_json_file(
        resolve_required_rwa_report_path("rwa_jupiter_route_allowlist.json")
    )
    routes = payload.get("routes") if isinstance(payload.get("routes"), list) else []
    blocked: dict[str, str] = {}
    for row in routes:
        if not isinstance(row, dict):
            continue
        error = str(row.get("error") or "")
        if "TOKEN_NOT_TRADABLE" not in error:
            continue
        keys = {
            _base_symbol(str(row.get("symbol") or "")),
            str(row.get("asset_id") or ""),
        }
        allowlist_id = str(row.get("allowlist_id") or "")
        parts = allowlist_id.split(":")
        if len(parts) >= 3:
            keys.add(parts[2])
        for key in keys:
            clean = JupiterRouterAdapter._token_key(key) if key else ""
            if clean:
                blocked[clean] = error
    return blocked


class RWAFeedAdapter(Protocol):
    """Protocol every RWA venue adapter must satisfy."""

    venue_id: str

    def metadata(self) -> dict[str, Any]:
        """Return adapter capabilities and operational state."""

    async def fetch_bidask(self, symbol: str) -> dict[str, Any]:
        """Fetch and normalize a bid/ask observation."""

    async def fetch_order_book(
        self,
        symbol: str,
        *,
        side: str = "buy",
        depth: int = 100,
    ) -> dict[str, Any]:
        """Fetch and normalize one side of an order book for block VWAP."""


@dataclass(frozen=True)
class AdapterCapability:
    venue_id: str
    adapter_type: str
    status: str
    source_type: str
    supports_bidask: bool
    supports_l2_vwap: bool
    supports_trade_vwap: bool
    requires_auth: bool
    implementation: str
    notes: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "venue_id": self.venue_id,
            "adapter_type": self.adapter_type,
            "status": self.status,
            "source_type": self.source_type,
            "supports_bidask": self.supports_bidask,
            "supports_l2_vwap": self.supports_l2_vwap,
            "supports_trade_vwap": self.supports_trade_vwap,
            "requires_auth": self.requires_auth,
            "implementation": self.implementation,
            "notes": self.notes,
        }


class StaticCapabilityAdapter:
    """Registry-only adapter for venues whose live fetchers are not wired yet."""

    def __init__(self, capability: AdapterCapability) -> None:
        self.venue_id = capability.venue_id
        self._capability = capability

    def metadata(self) -> dict[str, Any]:
        return self._capability.as_dict()

    async def fetch_bidask(self, symbol: str) -> dict[str, Any]:
        raise NotImplementedError(f"{self.venue_id} bid/ask adapter is not live-wired yet")

    async def fetch_order_book(
        self,
        symbol: str,
        *,
        side: str = "buy",
        depth: int = 100,
    ) -> dict[str, Any]:
        raise NotImplementedError(f"{self.venue_id} order-book adapter is not live-wired yet")


class RWAAdapterBlockedError(ValueError):
    """Adapter-level blocker that should be surfaced as a data-quality reason."""

    def __init__(self, blocker_category: str, message: str) -> None:
        super().__init__(message)
        self.blocker_category = blocker_category


P0_BLOCKED_ADAPTER_SPECS: dict[str, dict[str, Any]] = {
    "drift": {
        "status": "implemented_blocked_on_solana_rpc_and_dlob",
        "notes": [
            "P0 spec: wire Drift SDK/DLOB or market-account replay through low-latency Solana RPC/WebSocket.",
            "Required evidence before live use: market account, oracle dependencies, slot lag, replayable raw account payloads, and liquidity/manipulation checks.",
        ],
    },
    "ostium": {
        "status": "implemented_blocked_on_ostium_api_or_contract_replay",
        "notes": [
            "P0 spec: source Ostium public/builder API bid/mid/ask, candles, fills, and simulated depth if permitted.",
            "Fallback spec: decode contracts/oracles/position state through EVM RPC/indexer and label any depth as synthetic.",
        ],
    },
    "gains": {
        "status": "implemented_blocked_on_gains_api_or_rpc_replay",
        "notes": [
            "P0 spec: source Gains market price stream, pair parameters, trade history, and price-impact parameters from official API/subgraph/RPC.",
            "Do not expose L2 VWAP unless replayable protocol depth or price-impact parameters are available.",
        ],
    },
    "uniswap_v3_v4": {
        "status": "implemented_blocked_on_evm_rpc_and_pool_state",
        "notes": [
            "P0 spec: implement direct pool-state/tick replay for Uniswap v3/v4 pools with block freshness and raw RPC payload capture.",
            "Jupiter-style router quotes are not acceptable as direct pool-state evidence for production.",
        ],
    },
    "balancer_pools": {
        "status": "implemented_blocked_on_evm_rpc_and_pool_state",
        "notes": [
            "P1 spec: implement Balancer pool balance/weight replay through EVM RPC or approved indexer.",
            "Required evidence before live use: verified pool IDs, balances, block number, pool type, and imbalance/manipulation checks.",
        ],
    },
    "curve_stableswap": {
        "status": "implemented_blocked_on_evm_rpc_and_pool_state",
        "notes": [
            "P1 spec: implement Curve registry/pool balance/virtual-price replay through EVM RPC or approved indexer.",
            "Required evidence before live use: verified pool address, balances, virtual price where applicable, block freshness, and depeg/imbalance checks.",
        ],
    },
    "aerodrome_slipstream": {
        "status": "implemented_blocked_on_evm_rpc_and_pool_state",
        "notes": [
            "P1 spec: implement Aerodrome Slipstream pool/tick replay on Base with block freshness and raw RPC payload capture.",
            "Required evidence before live use: verified pool address, tick/liquidity state, block number, route concentration, and manipulation checks.",
        ],
    },
    "blocksize_state": {
        "status": "implemented_blocked_on_state_instrument_confirmation",
        "notes": [
            "P1 spec: Blocksize state rows are candidate/reference coverage only until state instrument mapping, freshness, and replay evidence are confirmed.",
            "Required evidence before live use: state source entitlement, instrument identity, slot/block state, replay payloads, and benchmark alignment.",
        ],
    },
    "treasury_nav": {
        "status": "implemented_blocked_on_issuer_nav_access",
        "notes": [
            "P1 spec: source issuer NAV/reserve/redemption data with explicit redistribution rights.",
            "NAV rows are benchmarks/reference inputs, not executable L2 liquidity.",
        ],
    },
}


class KrakenXStocksAdapter:
    """Kraken xStocks adapter preferring public WebSocket state with REST fallback."""

    venue_id = "kraken_xstocks"

    def __init__(
        self,
        *,
        base_url: str = "https://api.kraken.com",
        client: httpx.AsyncClient | None = None,
        stream_cache: CEXBookCache | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._stream_cache = stream_cache

    def metadata(self) -> dict[str, Any]:
        return AdapterCapability(
            venue_id=self.venue_id,
            adapter_type="websocket_public_with_rest_fallback",
            status="implemented_unprobed",
            source_type="native_l2",
            supports_bidask=True,
            supports_l2_vwap=True,
            supports_trade_vwap=True,
            requires_auth=False,
            implementation="src.rwa_adapters.KrakenXStocksAdapter",
            notes=[
                "Prefers a fresh Kraken WebSocket v2 L2 book and falls back to public Ticker and Depth REST endpoints.",
                "Non-crypto tokenized-asset symbol discovery should be validated dynamically before production rollout.",
            ],
        ).as_dict()

    @staticmethod
    def normalize_symbol(symbol: str) -> str:
        raw = symbol.strip()
        if "/" in raw:
            base, quote = raw.split("/", 1)
        else:
            compact = raw.upper()
            if compact.endswith("USD"):
                base, quote = raw[:-3], "USD"
            else:
                base, quote = raw, "USD"
        if not base.lower().endswith("x"):
            base = f"{base}x"
        return f"{base}/{quote.upper()}"

    @staticmethod
    def _compact_pair(pair: str) -> str:
        return pair.replace("/", "").replace("-", "").upper()

    async def resolve_pair(self, symbol: str) -> tuple[str, str]:
        """Resolve a caller symbol to a Kraken public AssetPairs key."""
        display_pair = self.normalize_symbol(symbol)
        compact_pair = self._compact_pair(display_pair)
        payload = await self._get("/0/public/AssetPairs", {"pair": compact_pair})
        errors = payload.get("error") or []
        result = payload.get("result")
        if errors or not isinstance(result, dict) or not result:
            reason = ", ".join(str(item) for item in errors) if errors else "empty AssetPairs result"
            raise ValueError(
                f"Kraken xStocks pair is not listed in public AssetPairs: "
                f"{display_pair} ({compact_pair}); reason={reason}"
            )
        api_pair, metadata = next(iter(result.items()))
        if not isinstance(metadata, dict):
            raise ValueError(f"Kraken AssetPairs metadata was not an object for {display_pair}")
        return str(api_pair), str(metadata.get("wsname") or display_pair)

    async def _get(self, path: str, params: dict[str, Any]) -> dict[str, Any]:
        if self._client is not None:
            response = await self._client.get(f"{self.base_url}{path}", params=params)
            response.raise_for_status()
            return response.json()
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.get(f"{self.base_url}{path}", params=params)
            response.raise_for_status()
            return response.json()

    @staticmethod
    def _first_result(payload: dict[str, Any]) -> dict[str, Any]:
        errors = payload.get("error") or []
        if errors:
            raise ValueError(f"Kraken API error: {', '.join(str(item) for item in errors)}")
        result = payload.get("result")
        if not isinstance(result, dict) or not result:
            raise ValueError("Kraken response did not include a result")
        first = next(iter(result.values()))
        if not isinstance(first, dict):
            raise ValueError("Kraken result was not an object")
        return first

    async def fetch_bidask(self, symbol: str) -> dict[str, Any]:
        api_pair, display_pair = await self.resolve_pair(symbol)
        streamed = self._streamed_book(display_pair)
        if streamed is not None and streamed["bids"] and streamed["asks"]:
            return {
                "symbol": display_pair,
                "venue": self.venue_id,
                "asset_class": "equity",
                "source_type": "native_l1",
                "bid": streamed["bids"][0]["price"],
                "ask": streamed["asks"][0]["price"],
                "timestamp": streamed["exchange_timestamp"],
                "metadata": {"transport": "websocket", "sequence": streamed["sequence"], "age_ms": streamed["age_ms"]},
            }
        payload = await self._get("/0/public/Ticker", {"pair": api_pair})
        result = self._first_result(payload)
        ask = float(result["a"][0])
        bid = float(result["b"][0])
        return {
            "symbol": display_pair,
            "venue": self.venue_id,
            "asset_class": "equity",
            "source_type": "native_l1",
            "bid": bid,
            "ask": ask,
        }

    async def fetch_order_book(
        self,
        symbol: str,
        *,
        side: str = "buy",
        depth: int = 100,
    ) -> dict[str, Any]:
        api_pair, display_pair = await self.resolve_pair(symbol)
        clean_side = side.strip().lower()
        if clean_side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        streamed = self._streamed_book(display_pair)
        if streamed is not None:
            levels = (streamed["asks"] if clean_side == "buy" else streamed["bids"])[:depth]
            metadata = {"transport": "websocket", "sequence": streamed["sequence"], "age_ms": streamed["age_ms"]}
        else:
            payload = await self._get("/0/public/Depth", {"pair": api_pair, "count": depth})
            result = self._first_result(payload)
            source_rows = result["asks"] if clean_side == "buy" else result["bids"]
            levels = [{"price": float(row[0]), "size": float(row[1])} for row in source_rows]
            metadata = {"transport": "rest"}
        return {
            "symbol": display_pair,
            "venue": self.venue_id,
            "asset_class": "equity",
            "source_type": "native_l2",
            "side": clean_side,
            "levels": levels,
            "metadata": metadata,
        }

    def _streamed_book(self, symbol: str) -> dict[str, Any] | None:
        if self._stream_cache is None:
            return None
        try:
            return self._stream_cache.get(self.venue_id, symbol)
        except CEXBookUnavailable:
            return None

    def fetch_trade_vwap(self, symbol: str, *, max_age_seconds: float = 60.0) -> dict[str, Any]:
        if self._stream_cache is None:
            raise RWAAdapterBlockedError("stream_not_configured", f"{self.venue_id} trade stream is not configured")
        display_pair = self.normalize_symbol(symbol)
        return self._stream_cache.trade_vwap(self.venue_id, display_pair, max_age_seconds=max_age_seconds)


class KrakenSpotAdapter(KrakenXStocksAdapter):
    """Kraken public spot adapter for dynamically listed crypto and FX pairs."""

    venue_id = "kraken_spot"

    @staticmethod
    def normalize_symbol(symbol: str) -> str:
        clean = _clean_symbol_key(symbol)
        if "/" not in clean:
            for quote in ("USDT", "USDC", "USD", "EUR", "XBT"):
                if clean.endswith(quote) and len(clean) > len(quote):
                    return f"{clean[:-len(quote)]}/{quote}"
            return f"{clean}/USD"
        return clean

    def metadata(self) -> dict[str, Any]:
        metadata = super().metadata()
        metadata.update({
            "venue_id": self.venue_id,
            "implementation": "src.rwa_adapters.KrakenSpotAdapter",
            "status": "implemented_unprobed",
        })
        metadata["notes"] = [
            "Uses dynamically listed Kraken spot pairs; it does not infer xStocks availability.",
            *metadata["notes"],
        ]
        return metadata


class RevolutXAdapter:
    """Read-only Revolut X native order-book adapter using signed REST requests."""

    venue_id = "revolut_x"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        private_key_pem: str | None = None,
        base_url: str = "https://revx.revolut.com/api",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("REVOLUT_X_API_KEY")
        self.private_key_pem = private_key_pem or os.getenv("REVOLUT_X_PRIVATE_KEY_PEM")
        self.base_url = base_url.rstrip("/")
        self._client = client

    def metadata(self) -> dict[str, Any]:
        configured = bool(self.api_key and self.private_key_pem)
        return AdapterCapability(
            venue_id=self.venue_id,
            adapter_type="signed_rest_read_only",
            status="implemented_unprobed" if configured else "implemented_blocked_on_credentials",
            source_type="native_l2",
            supports_bidask=True,
            supports_l2_vwap=True,
            supports_trade_vwap=True,
            requires_auth=True,
            implementation="src.rwa_adapters.RevolutXAdapter",
            notes=[
                "Uses the official signed Revolut X order-book endpoint; no trading methods are implemented.",
                "REST snapshots remain supplemental until freshness, liquidity, replay, rights, and benchmark gates pass.",
                "A Cursor MCP proxy must not be treated as native exchange depth without equivalent provenance.",
            ],
        ).as_dict()

    @staticmethod
    def normalize_symbol(symbol: str) -> str:
        return _clean_symbol_key(symbol).replace("/", "-")

    def _headers(self, method: str, path: str, query: str = "", body: str = "") -> dict[str, str]:
        if not self.api_key or not self.private_key_pem:
            raise RWAAdapterBlockedError(
                "credentials",
                "Revolut X requires REVOLUT_X_API_KEY and REVOLUT_X_PRIVATE_KEY_PEM",
            )
        timestamp = str(int(datetime.now(UTC).timestamp() * 1000))
        message = f"{timestamp}{method.upper()}{path}{query}{body}".encode()
        try:
            from cryptography.hazmat.primitives import serialization
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("cryptography is required for Revolut X Ed25519 signing") from exc
        private_key = serialization.load_pem_private_key(self.private_key_pem.encode(), password=None)
        signature = base64.b64encode(private_key.sign(message)).decode()
        return {
            "X-Revx-API-Key": self.api_key,
            "X-Revx-Timestamp": timestamp,
            "X-Revx-Signature": signature,
        }

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        params = params or {}
        query = str(httpx.QueryParams(params))
        headers = self._headers("GET", f"/api{path}", query)

        async def request(client: httpx.AsyncClient) -> dict[str, Any]:
            response = await client.get(f"{self.base_url}{path}", params=params, headers=headers)
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Revolut X response was not an object")
            return payload

        if self._client is not None:
            return await request(self._client)
        async with httpx.AsyncClient(timeout=15) as client:
            return await request(client)

    async def _public_get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.api_key:
            raise RWAAdapterBlockedError("credentials", "Revolut X public-data endpoints require REVOLUT_X_API_KEY")
        async def request(client: httpx.AsyncClient) -> dict[str, Any]:
            response = await client.get(
                f"{self.base_url}{path}",
                params=params or {},
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Revolut X public response was not an object")
            return payload
        if self._client is not None:
            return await request(self._client)
        async with httpx.AsyncClient(timeout=15) as client:
            return await request(client)

    async def discover_pairs(self, *, region: str = "EEA") -> dict[str, Any]:
        return await self._public_get_json("/1.0/public/configuration/pairs", {"region": region})

    async def fetch_public_trades(
        self,
        symbols: list[str],
        *,
        limit: int = 1000,
    ) -> dict[str, Any]:
        return await self._public_get_json(
            "/1.0/public/trades/all",
            {"symbols": ",".join(self.normalize_symbol(symbol) for symbol in symbols), "limit": limit},
        )

    async def _book(self, symbol: str, depth: int) -> tuple[str, dict[str, Any]]:
        market = self.normalize_symbol(symbol)
        payload = await self._get_json(f"/1.0/order-book/{market}", {"limit": depth})
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ValueError("Revolut X order-book response did not contain data")
        return market.replace("-", "/"), payload

    @staticmethod
    def _revx_levels(rows: Any) -> list[dict[str, float]]:
        if not isinstance(rows, list):
            return []
        return [
            {"price": float(row["p"]), "size": float(row["q"])}
            for row in rows
            if isinstance(row, dict) and row.get("p") is not None and row.get("q") is not None
        ]

    async def fetch_bidask(self, symbol: str) -> dict[str, Any]:
        display, payload = await self._book(symbol, 1)
        data = payload["data"]
        bids, asks = self._revx_levels(data.get("bids")), self._revx_levels(data.get("asks"))
        if not bids or not asks:
            raise ValueError(f"Revolut X returned an incomplete order book for {display}")
        return {
            "symbol": display,
            "venue": self.venue_id,
            "asset_class": "crypto",
            "source_type": "native_l1",
            "bid": max(row["price"] for row in bids),
            "ask": min(row["price"] for row in asks),
            "timestamp": (payload.get("metadata") or {}).get("timestamp"),
            "metadata": {"transport": "signed_rest", "raw_payload": payload},
        }

    async def fetch_order_book(self, symbol: str, *, side: str = "buy", depth: int = 100) -> dict[str, Any]:
        clean_side = side.strip().lower()
        if clean_side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        display, payload = await self._book(symbol, depth)
        source = payload["data"].get("asks" if clean_side == "buy" else "bids")
        return {
            "symbol": display,
            "venue": self.venue_id,
            "asset_class": "crypto",
            "source_type": "native_l2",
            "side": clean_side,
            "levels": self._revx_levels(source),
            "timestamp": (payload.get("metadata") or {}).get("timestamp"),
            "metadata": {"transport": "signed_rest", "raw_payload": payload},
        }


class XStocksPublicPriceAdapter:
    """xStocks public issuer/reference price and token metadata adapter."""

    venue_id = "xstocks_public"

    def __init__(
        self,
        *,
        base_url: str = "https://api.backed.fi/api/v2",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._asset_cache: dict[str, dict[str, Any]] = {}

    def metadata(self) -> dict[str, Any]:
        return AdapterCapability(
            venue_id=self.venue_id,
            adapter_type="rest_public",
            status="implemented_unprobed",
            source_type="issuer_reference_price",
            supports_bidask=True,
            supports_l2_vwap=False,
            supports_trade_vwap=False,
            requires_auth=False,
            implementation="src.rwa_adapters.XStocksPublicPriceAdapter",
            notes=[
                "Uses the public xStocks asset metadata and price-data endpoints without authentication.",
                "The quote is an issuer/reference price, not native bid/ask or exchange depth.",
                "The source does not currently provide a source timestamp in the quote payload; ingestion time is retained separately.",
                "Production use still requires redistribution clearance, continuous quality windows, benchmark alignment, and independent consensus.",
            ],
        ).as_dict()

    @staticmethod
    def _source_symbol(symbol: str) -> str:
        base = _base_symbol(symbol)
        if base.lower().endswith("x"):
            return f"{base[:-1]}x"
        return f"{base}x"

    async def _get_json(self, path: str) -> tuple[dict[str, Any], dict[str, str]]:
        async def _request(client: httpx.AsyncClient) -> tuple[dict[str, Any], dict[str, str]]:
            response = await client.get(f"{self.base_url}{path}")
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise ValueError(
                    f"xStocks {path} returned HTTP {response.status_code}: {response.text[:500]}"
                ) from exc
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError(f"xStocks {path} response was not an object")
            return payload, dict(response.headers)

        if self._client is not None:
            return await _request(self._client)
        async with httpx.AsyncClient(timeout=15) as client:
            return await _request(client)

    async def _asset(self, source_symbol: str) -> dict[str, Any]:
        cache_key = source_symbol.upper()
        if cache_key not in self._asset_cache:
            payload, _ = await self._get_json(f"/public/assets/{source_symbol}")
            self._asset_cache[cache_key] = payload
        return self._asset_cache[cache_key]

    async def fetch_bidask(self, symbol: str) -> dict[str, Any]:
        source_symbol = self._source_symbol(symbol)
        asset = await self._asset(source_symbol)
        payload, headers = await self._get_json(f"/public/assets/{source_symbol}/price-data")
        price = _float_value(payload.get("quote"))
        if price is None or price <= 0:
            raise ValueError(f"xStocks returned no positive public quote for {source_symbol}")
        observed_at = _iso_now()
        return {
            "symbol": f"{asset.get('underlyingSymbol') or source_symbol[:-1]}/USD",
            "venue": self.venue_id,
            "asset_class": "etf" if " ETF " in f" {asset.get('name', '')} ".upper() else "equity",
            "source_type": "issuer_reference_price",
            "mid": price,
            "price": price,
            "timestamp": None,
            "metadata": {
                "endpoint": "xStocks /api/v2/public/assets/{symbol}/price-data",
                "source_symbol": asset.get("symbol") or source_symbol,
                "underlying_symbol": asset.get("underlyingSymbol"),
                "isin": asset.get("isin"),
                "underlying_isin": asset.get("underlyingIsin"),
                "is_trading_halted": asset.get("isTradingHalted"),
                "deployments": asset.get("deployments") or [],
                "http_date": headers.get("date"),
                "ingested_at": observed_at,
                "source_timestamp_unavailable": True,
                "reference_only_exception": "public issuer quote has no source timestamp or executable book",
                "raw_payload": {"asset": asset, "price_data": payload},
            },
        }

    async def fetch_order_book(
        self,
        symbol: str,
        *,
        side: str = "buy",
        depth: int = 100,
    ) -> dict[str, Any]:
        raise RWAAdapterBlockedError(
            "reference_only_not_l2_liquidity",
            "xStocks public price-data is a point reference quote, not an order book; use a venue L2 or verified pool-state adapter for VWAP.",
        )


class HyperliquidPAXGAdapter:
    """Hyperliquid public REST adapter for the PAXG perpetual order book."""

    venue_id = "hyperliquid_paxg"
    supported_coin = "PAXG"

    def __init__(
        self,
        *,
        base_url: str = "https://api.hyperliquid.xyz",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client

    def metadata(self) -> dict[str, Any]:
        return AdapterCapability(
            venue_id=self.venue_id,
            adapter_type="rest_public",
            status="implemented_unprobed",
            source_type="native_l2",
            supports_bidask=True,
            supports_l2_vwap=True,
            supports_trade_vwap=True,
            requires_auth=False,
            implementation="src.rwa_adapters.HyperliquidPAXGAdapter",
            notes=[
                "Uses Hyperliquid public info endpoint with l2Book for PAXG.",
                "Hyperliquid PAXG is a gold-token/perp overlap source, not a broad metals venue.",
                "The public l2Book endpoint returns at most 20 levels per side.",
            ],
        ).as_dict()

    @classmethod
    def normalize_coin(cls, symbol: str) -> str:
        raw = symbol.strip().upper().replace("-", "/")
        base = raw.split("/", 1)[0]
        if base in {cls.supported_coin, "PAXGUSD", "PAXGUSDC"}:
            return cls.supported_coin
        raise ValueError("Hyperliquid RWA adapter currently supports only PAXG")

    async def _post_info(self, body: dict[str, Any]) -> dict[str, Any]:
        if self._client is not None:
            response = await self._client.post(f"{self.base_url}/info", json=body)
            response.raise_for_status()
            return response.json()
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(f"{self.base_url}/info", json=body)
            response.raise_for_status()
            return response.json()

    @staticmethod
    def _timestamp_from_payload(payload: dict[str, Any]) -> str | None:
        raw = payload.get("time")
        if raw is None:
            return None
        value = float(raw)
        if value > 10_000_000_000:
            value /= 1000
        from datetime import UTC, datetime

        return datetime.fromtimestamp(value, tz=UTC).isoformat()

    @staticmethod
    def _levels_for_side(payload: dict[str, Any], side: str, depth: int) -> list[dict[str, float]]:
        levels = payload.get("levels")
        if not isinstance(levels, list) or len(levels) < 2:
            raise ValueError("Hyperliquid l2Book response did not include bid and ask levels")
        source_rows = levels[1] if side == "buy" else levels[0]
        if not isinstance(source_rows, list) or not source_rows:
            raise ValueError("Hyperliquid l2Book response had no fillable levels")
        parsed = []
        for row in source_rows[: max(1, min(depth, 20))]:
            if not isinstance(row, dict):
                continue
            parsed.append(
                {
                    "price": float(row["px"]),
                    "size": float(row["sz"]),
                }
            )
        if not parsed:
            raise ValueError("Hyperliquid l2Book levels were not parseable")
        return parsed

    async def fetch_bidask(self, symbol: str) -> dict[str, Any]:
        coin = self.normalize_coin(symbol)
        payload = await self._post_info({"type": "l2Book", "coin": coin})
        bids = self._levels_for_side(payload, "sell", 1)
        asks = self._levels_for_side(payload, "buy", 1)
        return {
            "symbol": f"{coin}/USD",
            "venue": self.venue_id,
            "asset_class": "metal",
            "source_type": "native_l1",
            "bid": bids[0]["price"],
            "ask": asks[0]["price"],
            "timestamp": self._timestamp_from_payload(payload),
            "metadata": {
                "coin": coin,
                "endpoint": "Hyperliquid /info l2Book",
                "book_time_ms": payload.get("time"),
            },
        }

    async def fetch_order_book(
        self,
        symbol: str,
        *,
        side: str = "buy",
        depth: int = 100,
    ) -> dict[str, Any]:
        coin = self.normalize_coin(symbol)
        clean_side = side.strip().lower()
        if clean_side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        payload = await self._post_info({"type": "l2Book", "coin": coin})
        return {
            "symbol": f"{coin}/USD",
            "venue": self.venue_id,
            "asset_class": "metal",
            "source_type": "native_l2",
            "side": clean_side,
            "levels": self._levels_for_side(payload, clean_side, depth),
            "timestamp": self._timestamp_from_payload(payload),
            "metadata": {
                "coin": coin,
                "endpoint": "Hyperliquid /info l2Book",
                "book_time_ms": payload.get("time"),
                "max_levels_per_side": 20,
            },
        }


class HyperliquidSpotRWAAdapter:
    """Hyperliquid public REST adapter for RWA/traditional spot candidates."""

    venue_id = HYPERLIQUID_RWA_SPOT_VENUE_ID

    def __init__(
        self,
        *,
        base_url: str = "https://api.hyperliquid.xyz",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._aliases = hyperliquid_symbol_by_alias()

    def metadata(self) -> dict[str, Any]:
        unverified_count = sum(1 for row in HYPERLIQUID_RWA_SPOT_SYMBOLS if hyperliquid_is_unverified(row))
        return AdapterCapability(
            venue_id=self.venue_id,
            adapter_type="rest_public",
            status="implemented_unprobed",
            source_type="native_l2",
            supports_bidask=True,
            supports_l2_vwap=True,
            supports_trade_vwap=True,
            requires_auth=False,
            implementation="src.rwa_adapters.HyperliquidSpotRWAAdapter",
            notes=[
                "Uses Hyperliquid public info endpoint with spot l2Book @index symbols.",
                "Uses spotMetaAndAssetCtxs for native 24-hour base/notional volume.",
                f"Seeded with {len(HYPERLIQUID_RWA_SPOT_SYMBOLS)} RWA/traditional spot candidates; {unverified_count} require manual identity review.",
                "The public l2Book endpoint returns at most 20 levels per side.",
                "Rows are supplemental until identity, issuer, liquidity, and benchmark checks pass.",
            ],
        ).as_dict()

    def resolve_symbol(self, symbol: str) -> dict[str, Any]:
        clean = symbol.strip().upper().replace("-", "/").replace(" ", "")
        row = self._aliases.get(clean)
        if row is not None:
            return row
        raise ValueError(f"Hyperliquid RWA spot symbol is not in the sourced candidate set: {symbol}")

    async def _post_info(self, body: dict[str, Any]) -> dict[str, Any]:
        if self._client is not None:
            response = await self._client.post(f"{self.base_url}/info", json=body)
            response.raise_for_status()
            return response.json()
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(f"{self.base_url}/info", json=body)
            response.raise_for_status()
            return response.json()

    @staticmethod
    def _timestamp_from_payload(payload: dict[str, Any]) -> str | None:
        return HyperliquidPAXGAdapter._timestamp_from_payload(payload)

    @staticmethod
    def _levels_for_side(payload: dict[str, Any], side: str, depth: int) -> list[dict[str, float]]:
        return HyperliquidPAXGAdapter._levels_for_side(payload, side, depth)

    @staticmethod
    def _observation_metadata(row: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "hyperliquid_coin": row["hyperliquid_coin"],
            "pair_index": row["pair_index"],
            "endpoint": "Hyperliquid /info l2Book",
            "book_time_ms": payload.get("time"),
            "hyperliquid_asset_class": row["asset_class"],
            "identity_note": row["identity_note"],
            "token_id": row["token_id"],
            "evm_contract": row["evm_contract"],
            "use_case": row["use_case"],
            "promotion_gate": (
                "manual_identity_review_required"
                if hyperliquid_is_unverified(row)
                else "issuer_identity_liquidity_and_benchmark_validation_required"
            ),
        }

    async def fetch_bidask(self, symbol: str) -> dict[str, Any]:
        row = self.resolve_symbol(symbol)
        coin = str(row["hyperliquid_coin"])
        payload = await self._post_info({"type": "l2Book", "coin": coin})
        bids = self._levels_for_side(payload, "sell", 1)
        asks = self._levels_for_side(payload, "buy", 1)
        return {
            "symbol": row["display_pair"],
            "venue": self.venue_id,
            "asset_class": hyperliquid_normalized_asset_class(row),
            "source_type": "native_l1",
            "bid": bids[0]["price"],
            "ask": asks[0]["price"],
            "timestamp": self._timestamp_from_payload(payload),
            "metadata": self._observation_metadata(row, payload),
        }

    async def fetch_order_book(
        self,
        symbol: str,
        *,
        side: str = "buy",
        depth: int = 100,
    ) -> dict[str, Any]:
        row = self.resolve_symbol(symbol)
        clean_side = side.strip().lower()
        if clean_side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        payload = await self._post_info({"type": "l2Book", "coin": row["hyperliquid_coin"]})
        metadata = self._observation_metadata(row, payload)
        metadata["max_levels_per_side"] = 20
        return {
            "symbol": row["display_pair"],
            "venue": self.venue_id,
            "asset_class": hyperliquid_normalized_asset_class(row),
            "source_type": "native_l2",
            "side": clean_side,
            "levels": self._levels_for_side(payload, clean_side, depth),
            "timestamp": self._timestamp_from_payload(payload),
            "metadata": metadata,
        }

    async def fetch_market_activity(self, symbol: str) -> dict[str, Any]:
        """Return Hyperliquid-native rolling 24-hour spot activity."""
        row = self.resolve_symbol(symbol)
        coin = str(row["hyperliquid_coin"])
        payload = await self._post_info({"type": "spotMetaAndAssetCtxs"})
        if not isinstance(payload, list) or len(payload) < 2:
            raise ValueError("Hyperliquid spot contexts response was not a two-item list")
        meta = payload[0] if isinstance(payload[0], dict) else {}
        contexts = payload[1] if isinstance(payload[1], list) else []
        context = next(
            (
                item
                for item in contexts
                if isinstance(item, dict) and str(item.get("coin") or "") == coin
            ),
            None,
        )
        if context is None:
            universe = meta.get("universe") if isinstance(meta.get("universe"), list) else []
            market = next(
                (
                    item
                    for item in universe
                    if isinstance(item, dict) and str(item.get("name") or "") == coin
                ),
                None,
            )
            market_index = market.get("index") if isinstance(market, dict) else None
            if isinstance(market_index, int) and 0 <= market_index < len(contexts):
                candidate = contexts[market_index]
                context = candidate if isinstance(candidate, dict) else None
        if context is None:
            raise ValueError(f"Hyperliquid spot context was unavailable for {coin}")
        return {
            "symbol": row["display_pair"],
            "venue": self.venue_id,
            "source_type": "native_venue_rolling_stats",
            "window_seconds": 86_400,
            "captured_at": _iso_now(),
            "base_volume": _float_value(context.get("dayBaseVlm")),
            "notional_volume_usd": _float_value(context.get("dayNtlVlm")),
            "mark_price": _float_value(context.get("markPx")),
            "mid_price": _float_value(context.get("midPx")),
            "previous_day_price": _float_value(context.get("prevDayPx")),
            "trade_count": None,
            "metadata": {
                "hyperliquid_coin": coin,
                "endpoint": "Hyperliquid /info spotMetaAndAssetCtxs",
                "volume_semantics": "venue_native_rolling_24h_notional_and_base_volume",
                "raw_context": context,
            },
        }


class HyperliquidTradeableAdapter:
    """Generic Hyperliquid public l2Book adapter for discovered perp or spot rows."""

    def __init__(
        self,
        *,
        venue_id: str,
        market_type: str,
        base_url: str = "https://api.hyperliquid.xyz",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.venue_id = venue_id
        self.market_type = market_type
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._aliases = self._build_aliases()

    def _build_aliases(self) -> dict[str, dict[str, Any]]:
        aliases: dict[str, dict[str, Any]] = {}
        preferred_base_rows: dict[str, dict[str, Any]] = {}
        for row in load_hyperliquid_tradeable_coverage_rows():
            if row.get("venue") != self.venue_id:
                continue
            symbol = str(row.get("symbol") or "").upper()
            asset_id = str(row.get("asset_id") or "").upper()
            metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            coin = str(metadata.get("hyperliquid_coin") or "").upper()
            for alias in {symbol, symbol.replace("/", ""), asset_id, coin}:
                if alias:
                    aliases[alias] = row
            preferred = preferred_base_rows.get(asset_id)
            if asset_id and (preferred is None or symbol.endswith("/USDC")):
                preferred_base_rows[asset_id] = row
        for asset_id, row in preferred_base_rows.items():
            aliases[asset_id] = row
            aliases[f"{asset_id}/USD"] = row
            aliases[f"{asset_id}USD"] = row
        return aliases

    def metadata(self) -> dict[str, Any]:
        row_count = len({id(row) for row in self._aliases.values()})
        return AdapterCapability(
            venue_id=self.venue_id,
            adapter_type="rest_public",
            status="implemented_unprobed" if row_count else "implemented_blocked_on_discovery_report",
            source_type="native_l2",
            supports_bidask=True,
            supports_l2_vwap=True,
            supports_trade_vwap=True,
            requires_auth=False,
            implementation="src.rwa_adapters.HyperliquidTradeableAdapter",
            notes=[
                f"Uses Hyperliquid public info endpoint with l2Book for discovered {self.market_type} rows.",
                f"Loaded {row_count} discovered coverage rows from reports/hyperliquid_tradeable_feeds.json.",
                "Rows remain candidates until freshness, depth, manipulation, and benchmark windows pass.",
            ],
        ).as_dict()

    def resolve_symbol(self, symbol: str) -> dict[str, Any]:
        clean = symbol.strip().upper().replace("-", "/").replace(" ", "")
        row = self._aliases.get(clean) or self._aliases.get(clean.replace("/", ""))
        if row is not None:
            return row
        raise ValueError(f"Hyperliquid {self.market_type} symbol is not in the discovered coverage set: {symbol}")

    async def _post_info(self, body: dict[str, Any]) -> dict[str, Any]:
        if self._client is not None:
            response = await self._client.post(f"{self.base_url}/info", json=body)
            response.raise_for_status()
            return response.json()
        async with httpx.AsyncClient(timeout=10) as client:
            response = await client.post(f"{self.base_url}/info", json=body)
            response.raise_for_status()
            return response.json()

    @staticmethod
    def _timestamp_from_payload(payload: dict[str, Any]) -> str | None:
        return HyperliquidPAXGAdapter._timestamp_from_payload(payload)

    @staticmethod
    def _levels_for_side(payload: dict[str, Any], side: str, depth: int) -> list[dict[str, float]]:
        return HyperliquidPAXGAdapter._levels_for_side(payload, side, depth)

    @staticmethod
    def _metadata(row: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        row_metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        return {
            **row_metadata,
            "endpoint": "Hyperliquid /info l2Book",
            "book_time_ms": payload.get("time"),
            "coverage_status": row.get("coverage_status"),
            "coverage_delta": row.get("coverage_delta"),
            "max_levels_per_side": 20,
        }

    async def fetch_bidask(self, symbol: str) -> dict[str, Any]:
        row = self.resolve_symbol(symbol)
        row_metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        coin = str(row_metadata.get("hyperliquid_coin") or row["asset_id"])
        payload = await self._post_info({"type": "l2Book", "coin": coin})
        bids = self._levels_for_side(payload, "sell", 1)
        asks = self._levels_for_side(payload, "buy", 1)
        return {
            "symbol": row["symbol"],
            "venue": self.venue_id,
            "asset_class": row["asset_class"],
            "source_type": "native_l1",
            "bid": bids[0]["price"],
            "ask": asks[0]["price"],
            "timestamp": self._timestamp_from_payload(payload),
            "metadata": self._metadata(row, payload),
        }

    async def fetch_order_book(
        self,
        symbol: str,
        *,
        side: str = "buy",
        depth: int = 100,
    ) -> dict[str, Any]:
        row = self.resolve_symbol(symbol)
        clean_side = side.strip().lower()
        if clean_side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        row_metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        coin = str(row_metadata.get("hyperliquid_coin") or row["asset_id"])
        payload = await self._post_info({"type": "l2Book", "coin": coin})
        return {
            "symbol": row["symbol"],
            "venue": self.venue_id,
            "asset_class": row["asset_class"],
            "source_type": "native_l2",
            "side": clean_side,
            "levels": self._levels_for_side(payload, clean_side, depth),
            "timestamp": self._timestamp_from_payload(payload),
            "metadata": self._metadata(row, payload),
        }


class JupiterRouterAdapter:
    """Jupiter quote-sweep adapter for Solana RWA route candidates."""

    venue_id = "jupiter_router"
    default_sweep_usd = (1_000, 5_000, 10_000, 25_000)
    _request_semaphore = asyncio.Semaphore(int(os.getenv("JUPITER_MAX_CONCURRENT_REQUESTS", "1")))
    _rate_limit_backoff_seconds = tuple(
        float(item)
        for item in os.getenv("JUPITER_RATE_LIMIT_BACKOFF_SECONDS", "0.5,1.0,2.0").split(",")
        if item.strip()
    ) or (0.5, 1.0, 2.0)

    def __init__(
        self,
        *,
        base_url: str = "https://api.jup.ag",
        api_key: str | None = None,
        token_mints: dict[str, dict[str, Any]] | None = None,
        blocked_tokens: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
        slippage_bps: int = 50,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = (api_key if api_key is not None else os.getenv("JUPITER_API_KEY", "")).strip()
        self._client = client
        self.slippage_bps = int(slippage_bps)
        self._token_mints = {
            **JUPITER_DEFAULT_TOKEN_MINTS,
            **{self._token_key(key): value for key, value in (token_mints or {}).items()},
        }
        self._blocked_tokens = {
            self._token_key(key): value for key, value in (blocked_tokens or {}).items()
        }
        self._runtime_blocked_tokens: dict[str, str] = {}

    def metadata(self) -> dict[str, Any]:
        configured_non_quote_tokens = [
            key for key in self._token_mints if key not in {"USD", "USDC"}
        ]
        is_configured = bool(self.api_key or configured_non_quote_tokens)
        return AdapterCapability(
            venue_id=self.venue_id,
            adapter_type="rest_api_keyed",
            status="implemented_unprobed" if is_configured else "implemented_blocked_on_token_catalog_or_api_key",
            source_type="quote_sweep",
            supports_bidask=True,
            supports_l2_vwap=True,
            supports_trade_vwap=False,
            requires_auth=not is_configured,
            implementation="src.rwa_adapters.JupiterRouterAdapter",
            notes=[
                "Uses Jupiter /swap/v1/quote to sweep route quotes by notional size.",
                "Requires configured token mints or JUPITER_API_KEY-backed token search for non-quote tokens.",
                "Prunes unresolved, rejected, blocked, bad-mint and not-tradable registry rows before routing.",
                "Returns quote_sweep observations with routePlan, contextSlot, priceImpactPct and timeTaken metadata.",
                "Quote sweeps are executable route snapshots, not native exchange order books.",
            ],
        ).as_dict()

    @staticmethod
    def _token_key(value: str) -> str:
        return value.strip().upper().replace("-", "").replace("/", "")

    @classmethod
    def normalize_symbol(cls, symbol: str) -> tuple[str, str, str]:
        raw = symbol.strip().upper().replace("-", "/").replace(" ", "")
        if "/" in raw:
            base, quote = raw.split("/", 1)
        else:
            quote = "USDC" if raw.endswith("USDC") else "USD"
            base = raw[: -len(quote)] if raw.endswith(quote) else raw
        if quote == "USD":
            quote = "USDC"
        display = f"{base}/{quote}"
        return base, quote, display

    def _asset_class(self, base: str) -> str:
        return JUPITER_ASSET_CLASS_BY_BASE.get(self._token_key(base), "tokenized_asset")

    def _headers(self) -> dict[str, str]:
        return {"x-api-key": self.api_key} if self.api_key else {}

    @staticmethod
    def _is_bad_or_unapproved_token(token: dict[str, Any]) -> str | None:
        status = str(token.get("status") or "").lower()
        review_status = str(token.get("review_status") or "").lower()
        combined = f"{status} {review_status}"
        if any(term in combined for term in ("bad_mint", "not_tradable", "rejected", "blocked")):
            return f"token registry status is {status or review_status}"
        allow_manual_review = os.getenv("JUPITER_ALLOW_MANUAL_REVIEW_TOKENS", "").lower() in {"1", "true", "yes"}
        if "needs_manual_review" in combined and not allow_manual_review:
            return "token registry review_status is needs_manual_review"
        return None

    def _remember_token_blocker(self, token: dict[str, Any], reason: str) -> None:
        mint = str(token.get("mint") or "")
        symbol = self._token_key(str(token.get("symbol") or ""))
        if mint:
            self._runtime_blocked_tokens[mint] = reason
        if symbol:
            self._runtime_blocked_tokens[symbol] = reason

    def _guard_token_is_routeable(self, token: dict[str, Any]) -> None:
        symbol = str(token.get("symbol") or "")
        token_key = self._token_key(symbol)
        mint = str(token.get("mint") or "")
        reason = (
            self._blocked_tokens.get(token_key)
            or self._blocked_tokens.get(mint)
            or self._runtime_blocked_tokens.get(token_key)
            or self._runtime_blocked_tokens.get(mint)
            or self._is_bad_or_unapproved_token(token)
        )
        if reason:
            raise RWAAdapterBlockedError(
                "token_not_tradable_or_bad_mint",
                f"Jupiter token mint rejected by route allowlist: {symbol} mint={mint}; reason={reason}",
            )

    @staticmethod
    def _retry_after_seconds(response: httpx.Response) -> float | None:
        value = response.headers.get("retry-after")
        if value in {None, ""}:
            return None
        try:
            parsed = float(value)
        except (TypeError, ValueError):
            return None
        return max(0.0, min(parsed, 60.0))

    async def _get(self, path: str, params: dict[str, Any]) -> Any:
        async def _request(client: httpx.AsyncClient) -> Any:
            attempts = len(self._rate_limit_backoff_seconds) + 1
            for attempt in range(attempts):
                async with self._request_semaphore:
                    response = await client.get(f"{self.base_url}{path}", params=params, headers=self._headers())
                if response.status_code == 429 and attempt < attempts - 1:
                    cap = self._rate_limit_backoff_seconds[attempt]
                    retry_after = self._retry_after_seconds(response)
                    await asyncio.sleep(min(retry_after, cap) if retry_after is not None else cap)
                    continue
                try:
                    response.raise_for_status()
                except httpx.HTTPStatusError as exc:
                    if response.status_code == 429:
                        raise RWAAdapterBlockedError(
                            "quota_rate_limit",
                            f"Jupiter quota/rate limit exhausted for {path} after {attempt + 1} attempts; "
                            "use production Jupiter quota or lower probe concurrency/backoff. "
                            f"response={response.text[:500]}"
                        ) from exc
                    raise ValueError(
                        f"Jupiter {path} returned HTTP {response.status_code}: {response.text[:500]}"
                    ) from exc
                return response.json()
            raise ValueError(f"Jupiter {path} retry loop ended without a response")

        if self._client is not None:
            return await _request(self._client)
        async with httpx.AsyncClient(timeout=10) as client:
            return await _request(client)

    async def _resolve_token(self, symbol: str) -> dict[str, Any]:
        key = self._token_key(symbol)
        if key in self._blocked_tokens:
            raise RWAAdapterBlockedError(
                "token_not_tradable_or_bad_mint",
                f"Jupiter token mint is marked non-tradable in route allowlist for {symbol}; "
                f"prune or replace this mint before production probing. evidence={self._blocked_tokens[key][:300]}",
            )
        token = self._token_mints.get(key)
        if token is not None:
            resolved = {
                "symbol": str(token.get("symbol") or symbol).upper(),
                "mint": str(token["mint"]),
                "decimals": int(token["decimals"]),
                "source": token.get("source", "configured"),
                "status": token.get("status", "configured"),
                "review_status": token.get("review_status"),
                "liquidity": token.get("liquidity"),
                "organic_score": token.get("organic_score"),
            }
            self._guard_token_is_routeable(resolved)
            return resolved
        if not self.api_key:
            raise ValueError(
                f"Jupiter token mint is not configured for {symbol}; "
                "set JUPITER_API_KEY or provide token_mints before probing this route"
            )
        payload = await self._get("/tokens/v2/search", {"query": symbol})
        if not isinstance(payload, list) or not payload:
            raise ValueError(f"Jupiter token search returned no mint for {symbol}")
        exact_symbol = self._token_key(symbol)
        candidates = [
            item for item in payload
            if isinstance(item, dict)
            and self._token_key(str(item.get("symbol") or "")) == exact_symbol
        ]
        if not candidates:
            candidates = [item for item in payload if isinstance(item, dict)]
        candidates.sort(
            key=lambda item: (
                not bool(item.get("isVerified")),
                -float(item.get("liquidity") or 0),
                str(item.get("id") or ""),
            )
        )
        selected = candidates[0]
        if selected.get("id") is None or selected.get("decimals") is None:
            raise ValueError(f"Jupiter token search returned incomplete mint metadata for {symbol}")
        token = {
            "symbol": str(selected.get("symbol") or symbol).upper(),
            "mint": str(selected["id"]),
            "decimals": int(selected["decimals"]),
            "source": "jupiter_tokens_v2_search",
            "status": "verified" if selected.get("isVerified") else "unverified_search_result",
            "review_status": "verified" if selected.get("isVerified") else "unverified_search_result",
            "liquidity": selected.get("liquidity"),
            "organic_score": selected.get("organicScore"),
        }
        self._token_mints[key] = token
        self._guard_token_is_routeable(token)
        return token

    @staticmethod
    def _atomic_amount(amount: float, decimals: int) -> int:
        if amount <= 0:
            raise ValueError("Jupiter quote amount must be greater than zero")
        return max(1, int(round(amount * (10 ** decimals))))

    @staticmethod
    def _amount_from_atomic(raw: Any, decimals: int) -> float:
        return float(raw) / (10 ** decimals)

    async def _quote_exact_in(
        self,
        *,
        input_token: dict[str, Any],
        output_token: dict[str, Any],
        input_amount: float,
    ) -> dict[str, Any]:
        params = {
            "inputMint": input_token["mint"],
            "outputMint": output_token["mint"],
            "amount": str(self._atomic_amount(input_amount, int(input_token["decimals"]))),
            "slippageBps": str(self.slippage_bps),
            "restrictIntermediateTokens": "true",
            "instructionVersion": "V2",
        }
        try:
            payload = await self._get("/swap/v1/quote", params)
        except ValueError as exc:
            message = str(exc)
            if "TOKEN_NOT_TRADABLE" in message or "not tradable" in message.lower():
                self._remember_token_blocker(input_token, "TOKEN_NOT_TRADABLE")
                self._remember_token_blocker(output_token, "TOKEN_NOT_TRADABLE")
                raise RWAAdapterBlockedError(
                    "token_not_tradable_or_bad_mint",
                    "Jupiter route rejected at least one token as not tradable: "
                    f"input={input_token.get('symbol')}:{input_token.get('mint')} "
                    f"output={output_token.get('symbol')}:{output_token.get('mint')}",
                ) from exc
            raise
        if not isinstance(payload, dict):
            raise ValueError("Jupiter quote response was not an object")
        if payload.get("outAmount") is None or payload.get("inAmount") is None:
            raise ValueError("Jupiter quote response did not include inAmount/outAmount")
        return payload

    def _quote_metadata(
        self,
        *,
        quote: dict[str, Any],
        input_token: dict[str, Any],
        output_token: dict[str, Any],
    ) -> dict[str, Any]:
        return {
            "endpoint": "Jupiter /swap/v1/quote",
            "input_mint": input_token["mint"],
            "output_mint": output_token["mint"],
            "input_symbol": input_token["symbol"],
            "output_symbol": output_token["symbol"],
            "context_slot": quote.get("contextSlot"),
            "time_taken": quote.get("timeTaken"),
            "price_impact_pct": quote.get("priceImpactPct"),
            "route_plan": quote.get("routePlan") or [],
            "swap_mode": quote.get("swapMode"),
            "slippage_bps": quote.get("slippageBps"),
            "other_amount_threshold": quote.get("otherAmountThreshold"),
        }

    async def _buy_quote(self, base_token: dict[str, Any], quote_token: dict[str, Any], quote_notional: float) -> dict[str, Any]:
        quote = await self._quote_exact_in(
            input_token=quote_token,
            output_token=base_token,
            input_amount=quote_notional,
        )
        input_amount = self._amount_from_atomic(quote["inAmount"], int(quote_token["decimals"]))
        output_amount = self._amount_from_atomic(quote["outAmount"], int(base_token["decimals"]))
        if output_amount <= 0:
            raise ValueError("Jupiter buy quote returned zero output amount")
        return {
            "quote": quote,
            "input_amount": input_amount,
            "output_amount": output_amount,
            "price": input_amount / output_amount,
            "metadata": self._quote_metadata(quote=quote, input_token=quote_token, output_token=base_token),
        }

    async def _sell_quote(self, base_token: dict[str, Any], quote_token: dict[str, Any], base_amount: float) -> dict[str, Any]:
        quote = await self._quote_exact_in(
            input_token=base_token,
            output_token=quote_token,
            input_amount=base_amount,
        )
        input_amount = self._amount_from_atomic(quote["inAmount"], int(base_token["decimals"]))
        output_amount = self._amount_from_atomic(quote["outAmount"], int(quote_token["decimals"]))
        if input_amount <= 0:
            raise ValueError("Jupiter sell quote returned zero input amount")
        return {
            "quote": quote,
            "input_amount": input_amount,
            "output_amount": output_amount,
            "price": output_amount / input_amount,
            "metadata": self._quote_metadata(quote=quote, input_token=base_token, output_token=quote_token),
        }

    @staticmethod
    def _utc_now_iso() -> str:
        return datetime.now(UTC).isoformat()

    async def fetch_bidask(self, symbol: str) -> dict[str, Any]:
        base, quote_symbol, display_pair = self.normalize_symbol(symbol)
        base_token = await self._resolve_token(base)
        quote_token = await self._resolve_token(quote_symbol)
        buy = await self._buy_quote(base_token, quote_token, 100.0)
        sell = await self._sell_quote(base_token, quote_token, buy["output_amount"])
        return {
            "symbol": display_pair,
            "venue": self.venue_id,
            "asset_class": self._asset_class(base),
            "source_type": "quote_sweep",
            "bid": sell["price"],
            "ask": buy["price"],
            "timestamp": self._utc_now_iso(),
            "metadata": {
                "quote_notional": 100.0,
                "bid_quote": sell["metadata"],
                "ask_quote": buy["metadata"],
                "base_token": base_token,
                "quote_token": quote_token,
            },
        }

    async def fetch_order_book(
        self,
        symbol: str,
        *,
        side: str = "buy",
        depth: int = 100,
    ) -> dict[str, Any]:
        clean_side = side.strip().lower()
        if clean_side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        base, quote_symbol, display_pair = self.normalize_symbol(symbol)
        base_token = await self._resolve_token(base)
        quote_token = await self._resolve_token(quote_symbol)
        sweep_sizes = list(self.default_sweep_usd[: max(1, min(depth, len(self.default_sweep_usd)))])

        levels: list[dict[str, float]] = []
        sweep_quotes: list[dict[str, Any]] = []
        previous_input = 0.0
        previous_output = 0.0
        reference_price: float | None = None
        if clean_side == "sell":
            reference_price = (await self._buy_quote(base_token, quote_token, 100.0))["price"]

        for notional in sweep_sizes:
            if clean_side == "buy":
                parsed = await self._buy_quote(base_token, quote_token, float(notional))
                cumulative_input = parsed["input_amount"]
                cumulative_output = parsed["output_amount"]
                marginal_input = cumulative_input - previous_input
                marginal_output = cumulative_output - previous_output
                previous_input = cumulative_input
                previous_output = cumulative_output
                if marginal_output <= 0:
                    continue
                levels.append({"price": marginal_input / marginal_output, "size": marginal_output})
            else:
                assert reference_price is not None
                base_amount = float(notional) / reference_price
                parsed = await self._sell_quote(base_token, quote_token, base_amount)
                cumulative_input = parsed["input_amount"]
                cumulative_output = parsed["output_amount"]
                marginal_input = cumulative_input - previous_input
                marginal_output = cumulative_output - previous_output
                previous_input = cumulative_input
                previous_output = cumulative_output
                if marginal_input <= 0:
                    continue
                levels.append({"price": marginal_output / marginal_input, "size": marginal_input})
            sweep_quotes.append(
                {
                    "notional_usd": float(notional),
                    "cumulative_price": parsed["price"],
                    "context_slot": parsed["metadata"].get("context_slot"),
                    "price_impact_pct": parsed["metadata"].get("price_impact_pct"),
                    "route_plan": parsed["metadata"].get("route_plan"),
                    "time_taken": parsed["metadata"].get("time_taken"),
                }
            )

        if not levels:
            raise ValueError("Jupiter quote sweep produced no fillable marginal levels")
        return {
            "symbol": display_pair,
            "venue": self.venue_id,
            "asset_class": self._asset_class(base),
            "source_type": "quote_sweep",
            "side": clean_side,
            "levels": levels,
            "timestamp": self._utc_now_iso(),
            "metadata": {
                "endpoint": "Jupiter /swap/v1/quote",
                "base_token": base_token,
                "quote_token": quote_token,
                "sweep_quotes": sweep_quotes,
                "sweep_sizes_usd": sweep_sizes,
                "level_semantics": "marginalized_from_cumulative_exact_in_route_quotes",
            },
        }


class DiscoveryBackedOrderBookAdapter:
    """Public REST order-book adapter backed by the derivative discovery report."""

    venue_id = ""
    source_type = "native_l2"
    source_note = "Uses a public venue order-book endpoint discovered from venue metadata."

    def __init__(
        self,
        *,
        base_url: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._aliases = _derivative_aliases(self.venue_id)

    def metadata(self) -> dict[str, Any]:
        row_count = len({id(row) for row in self._aliases.values()})
        return AdapterCapability(
            venue_id=self.venue_id,
            adapter_type="rest_public",
            status="implemented_unprobed" if row_count else "implemented_blocked_on_discovery_report",
            source_type=self.source_type,
            supports_bidask=True,
            supports_l2_vwap=True,
            supports_trade_vwap=False,
            requires_auth=False,
            implementation=f"src.rwa_adapters.{self.__class__.__name__}",
            notes=[
                self.source_note,
                f"Loaded {row_count} aliases from reports/rwa_derivative_venue_discovery.json.",
                "Derivative RWA books are supplemental until basis, funding, liquidity, and benchmark gates pass.",
            ],
        ).as_dict()

    def resolve_symbol(self, symbol: str) -> dict[str, Any]:
        clean = _clean_symbol_key(symbol)
        raw_lower = str(symbol or "").strip().lower()
        row = (
            self._aliases.get(raw_lower)
            or self._aliases.get(clean)
            or self._aliases.get(clean.replace("/", ""))
            or self._aliases.get(_base_symbol(symbol))
        )
        if row is None:
            raise ValueError(f"{self.venue_id} symbol is not in the derivative discovery set: {symbol}")
        return row

    async def _get_json(self, path: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = f"{self.base_url}{path}"

        async def _request(client: httpx.AsyncClient) -> dict[str, Any]:
            response = await client.get(url, params=params or {})
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise ValueError(
                    f"{self.venue_id} {path} returned HTTP {response.status_code}: {response.text[:500]}"
                ) from exc
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError(f"{self.venue_id} {path} response was not an object")
            return payload

        if self._client is not None:
            return await _request(self._client)
        async with httpx.AsyncClient(timeout=10) as client:
            return await _request(client)

    def _row_metadata(self, row: dict[str, Any]) -> dict[str, Any]:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        return {
            **metadata,
            "coverage_status": row.get("coverage_status"),
            "pricing_methodology": metadata.get("pricing_methodology"),
            "fair_value_policy": metadata.get("fair_value_policy"),
        }

    def _display_symbol(self, row: dict[str, Any]) -> str:
        return str(row.get("symbol") or row.get("asset_id") or "")

    def _asset_class(self, row: dict[str, Any]) -> str:
        return str(row.get("asset_class") or "unknown")

    def _market_id(self, row: dict[str, Any], *keys: str) -> str:
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        for key in keys:
            value = metadata.get(key)
            if value not in {None, ""}:
                return str(value)
        return _compact_market(self._display_symbol(row))

    @staticmethod
    def _require_book(book: dict[str, Any]) -> None:
        if not book.get("bids") or not book.get("asks"):
            raise ValueError("venue order-book response did not include both bid and ask levels")

    async def _fetch_book(self, row: dict[str, Any], *, depth: int) -> dict[str, Any]:
        raise NotImplementedError

    async def fetch_bidask(self, symbol: str) -> dict[str, Any]:
        row = self.resolve_symbol(symbol)
        book = await self._fetch_book(row, depth=1)
        self._require_book(book)
        return {
            "symbol": self._display_symbol(row),
            "venue": self.venue_id,
            "asset_class": self._asset_class(row),
            "source_type": "native_l1",
            "bid": book["bids"][0]["price"],
            "ask": book["asks"][0]["price"],
            "timestamp": book.get("timestamp"),
            "metadata": {
                **self._row_metadata(row),
                **(book.get("metadata") if isinstance(book.get("metadata"), dict) else {}),
            },
        }

    async def fetch_order_book(
        self,
        symbol: str,
        *,
        side: str = "buy",
        depth: int = 100,
    ) -> dict[str, Any]:
        row = self.resolve_symbol(symbol)
        clean_side = side.strip().lower()
        if clean_side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        book = await self._fetch_book(row, depth=depth)
        self._require_book(book)
        return {
            "symbol": self._display_symbol(row),
            "venue": self.venue_id,
            "asset_class": self._asset_class(row),
            "source_type": self.source_type,
            "side": clean_side,
            "levels": book["asks"] if clean_side == "buy" else book["bids"],
            "timestamp": book.get("timestamp"),
            "metadata": {
                **self._row_metadata(row),
                **(book.get("metadata") if isinstance(book.get("metadata"), dict) else {}),
            },
        }


class AsterOrderBookAdapter(DiscoveryBackedOrderBookAdapter):
    venue_id = "aster"
    source_note = "Uses Aster public futures depth endpoint."

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(base_url="https://fapi.asterdex.com", client=client)

    async def _fetch_book(self, row: dict[str, Any], *, depth: int) -> dict[str, Any]:
        market = self._market_id(row, "venue_symbol", "venue_market_id")
        payload = await self._get_json(
            "/fapi/v1/depth",
            {"symbol": market, "limit": max(5, min(int(depth), 1000))},
        )
        return {
            "bids": _levels_from_lists(payload.get("bids")),
            "asks": _levels_from_lists(payload.get("asks")),
            "timestamp": _iso_from_epoch(payload.get("T") or payload.get("E")),
            "metadata": {
                "endpoint": "Aster /fapi/v1/depth",
                "venue_symbol": market,
                "last_update_id": payload.get("lastUpdateId"),
                "event_time_ms": payload.get("E"),
                "transaction_time_ms": payload.get("T"),
            },
        }


class AevoOrderBookAdapter(DiscoveryBackedOrderBookAdapter):
    venue_id = "aevo"
    source_note = "Uses Aevo public orderbook endpoint by instrument_name."

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(base_url="https://api.aevo.xyz", client=client)

    async def _fetch_book(self, row: dict[str, Any], *, depth: int) -> dict[str, Any]:
        market = self._market_id(row, "venue_symbol")
        payload = await self._get_json("/orderbook", {"instrument_name": market})
        return {
            "bids": _levels_from_lists(payload.get("bids"))[:depth],
            "asks": _levels_from_lists(payload.get("asks"))[:depth],
            "timestamp": _iso_from_epoch(payload.get("last_updated")),
            "metadata": {
                "endpoint": "Aevo /orderbook",
                "instrument_name": market,
                "instrument_id": payload.get("instrument_id"),
                "last_updated": payload.get("last_updated"),
            },
        }


class ApeXOmniOrderBookAdapter(DiscoveryBackedOrderBookAdapter):
    venue_id = "apex_omni"
    source_note = "Uses ApeX Omni public v3 depth endpoint."

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(base_url="https://omni.apex.exchange", client=client)

    async def _fetch_book(self, row: dict[str, Any], *, depth: int) -> dict[str, Any]:
        market = self._market_id(row, "cross_symbol_name", "venue_symbol", "venue_market_id").replace("-", "")
        payload = await self._get_json("/api/v3/depth", {"symbol": market, "limit": max(5, min(int(depth), 200))})
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        return {
            "bids": _levels_from_lists(data.get("b"))[:depth],
            "asks": _levels_from_lists(data.get("a"))[:depth],
            "timestamp": None,
            "metadata": {
                "endpoint": "ApeX Omni /api/v3/depth",
                "venue_symbol": market,
                "update_id": data.get("u"),
                "ingested_at": _iso_now(),
                "source_timestamp_unavailable": True,
            },
        }


class LighterOrderBookAdapter(DiscoveryBackedOrderBookAdapter):
    venue_id = "lighter"
    source_note = "Uses Lighter public orderBookOrders endpoint by market_id."

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(base_url="https://mainnet.zklighter.elliot.ai", client=client)

    async def _fetch_book(self, row: dict[str, Any], *, depth: int) -> dict[str, Any]:
        market_id = self._market_id(row, "venue_market_id")
        payload = await self._get_json(
            "/api/v1/orderBookOrders",
            {"market_id": market_id, "limit": max(1, min(int(depth), 100))},
        )
        if payload.get("code") not in {200, "200"}:
            raise ValueError(f"Lighter orderBookOrders returned code={payload.get('code')}")
        return {
            "bids": _levels_from_lists(payload.get("bids"))[:depth],
            "asks": _levels_from_lists(payload.get("asks"))[:depth],
            "timestamp": None,
            "metadata": {
                "endpoint": "Lighter /api/v1/orderBookOrders",
                "market_id": market_id,
                "total_bids": payload.get("total_bids"),
                "total_asks": payload.get("total_asks"),
                "ingested_at": _iso_now(),
                "source_timestamp_unavailable": True,
            },
        }


class DydxOrderBookAdapter(DiscoveryBackedOrderBookAdapter):
    venue_id = "dydx"
    source_note = "Uses dYdX indexer public orderbooks endpoint."

    def __init__(self, *, client: httpx.AsyncClient | None = None) -> None:
        super().__init__(base_url="https://indexer.dydx.trade", client=client)

    async def _fetch_book(self, row: dict[str, Any], *, depth: int) -> dict[str, Any]:
        market = self._market_id(row, "venue_symbol")
        payload = await self._get_json(f"/v4/orderbooks/perpetualMarket/{market}", {})
        return {
            "bids": _levels_from_lists(payload.get("bids"))[:depth],
            "asks": _levels_from_lists(payload.get("asks"))[:depth],
            "timestamp": None,
            "metadata": {
                "endpoint": "dYdX /v4/orderbooks/perpetualMarket/{market}",
                "venue_symbol": market,
                "ingested_at": _iso_now(),
                "source_timestamp_unavailable": True,
            },
        }


class OrderlyReferencePriceAdapter:
    """Orderly public futures reference adapter for mark/index prices."""

    venue_id = "orderly"

    def __init__(
        self,
        *,
        base_url: str = "https://api.orderly.org",
        client: httpx.AsyncClient | None = None,
        orderbook_path_template: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._aliases = _derivative_aliases(self.venue_id)
        self.orderbook_path_template = (
            orderbook_path_template
            if orderbook_path_template is not None
            else os.getenv("ORDERLY_ORDERBOOK_PATH_TEMPLATE", "")
        ).strip()

    def metadata(self) -> dict[str, Any]:
        row_count = len({id(row) for row in self._aliases.values()})
        return AdapterCapability(
            venue_id=self.venue_id,
            adapter_type="rest_public",
            status="implemented_unprobed" if row_count else "implemented_blocked_on_discovery_report",
            source_type="native_mark_reference",
            supports_bidask=True,
            supports_l2_vwap=bool(self.orderbook_path_template),
            supports_trade_vwap=False,
            requires_auth=False,
            implementation="src.rwa_adapters.OrderlyReferencePriceAdapter",
            notes=[
                "Uses Orderly public futures endpoint for mark_price/index_price.",
                "VWAP probes only attempt the public orderbook path when ORDERLY_ORDERBOOK_PATH_TEMPLATE is configured.",
                "The guessed /v1/public/orderbook/{symbol} path returned path-not-found in live validation and is not used by default.",
                "If Orderly L2 requires account headers or a different entitlement, VWAP rows remain blocked instead of using synthetic depth.",
                "Reference and L2 prices remain supplemental until freshness, depth, replay, benchmark, manipulation, and rights gates pass.",
            ],
        ).as_dict()

    def resolve_symbol(self, symbol: str) -> dict[str, Any]:
        clean = _clean_symbol_key(symbol)
        row = (
            self._aliases.get(clean)
            or self._aliases.get(clean.replace("/", ""))
            or self._aliases.get(_base_symbol(symbol))
        )
        if row is None:
            raise ValueError(f"Orderly symbol is not in the derivative discovery set: {symbol}")
        return row

    async def _get_json(self, path: str) -> dict[str, Any]:
        async def _request(client: httpx.AsyncClient) -> dict[str, Any]:
            headers: dict[str, str] = {}
            api_key = os.getenv("ORDERLY_API_KEY", "").strip()
            if api_key:
                headers["x-api-key"] = api_key
            response = await client.get(f"{self.base_url}{path}", headers=headers)
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise ValueError(
                    f"Orderly {path} returned HTTP {response.status_code}: {response.text[:500]}"
                ) from exc
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError(f"Orderly {path} response was not an object")
            return payload

        if self._client is not None:
            return await _request(self._client)
        async with httpx.AsyncClient(timeout=10) as client:
            return await _request(client)

    @staticmethod
    def _book_data(payload: dict[str, Any]) -> dict[str, Any]:
        data = payload.get("data")
        if isinstance(data, dict):
            return data
        return payload

    @staticmethod
    def _book_timestamp(payload: dict[str, Any], data: dict[str, Any]) -> str | None:
        for value in (
            data.get("timestamp"),
            data.get("ts"),
            data.get("time"),
            payload.get("timestamp"),
            payload.get("ts"),
            payload.get("time"),
        ):
            parsed = _iso_from_epoch(value)
            if parsed is not None:
                return parsed
        return None

    def _orderbook_path(self, market: str) -> str:
        if not self.orderbook_path_template:
            raise RWAAdapterBlockedError(
                "orderly_l2_depth_blocked",
                "Orderly L2 depth is not configured; set ORDERLY_ORDERBOOK_PATH_TEMPLATE after confirming "
                "a documented public endpoint or account/API entitlement. Mark/index reference remains separate.",
            )
        return self.orderbook_path_template.format(symbol=market, market=market, depth=100)

    async def fetch_bidask(self, symbol: str) -> dict[str, Any]:
        row = self.resolve_symbol(symbol)
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        market = str(metadata.get("venue_symbol") or metadata.get("venue_market_id") or _compact_market(symbol))
        payload = await self._get_json(f"/v1/public/futures/{market}")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        price = _float_value(data.get("mark_price")) or _float_value(data.get("index_price"))
        if price is None or price <= 0:
            raise ValueError(f"Orderly public futures response had no positive mark/index price for {market}")
        return {
            "symbol": str(row.get("symbol") or symbol),
            "venue": self.venue_id,
            "asset_class": str(row.get("asset_class") or "unknown"),
            "source_type": "native_mark_reference",
            "mid": price,
            "price": price,
            "timestamp": _iso_from_epoch(payload.get("timestamp")),
            "metadata": {
                **metadata,
                "endpoint": "Orderly /v1/public/futures/{symbol}",
                "venue_symbol": market,
                "mark_price": data.get("mark_price"),
                "index_price": data.get("index_price"),
                "open_interest": data.get("open_interest"),
                "volume_24h": data.get("24h_volume"),
            },
        }

    async def fetch_order_book(
        self,
        symbol: str,
        *,
        side: str = "buy",
        depth: int = 100,
    ) -> dict[str, Any]:
        row = self.resolve_symbol(symbol)
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        market = str(metadata.get("venue_symbol") or metadata.get("venue_market_id") or _compact_market(symbol))
        clean_side = side.strip().lower()
        if clean_side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        path = self._orderbook_path(market)
        payload = await self._get_json(path)
        data = self._book_data(payload)
        bids = _levels_from_lists(data.get("bids") or data.get("b"))
        asks = _levels_from_lists(data.get("asks") or data.get("a"))
        if not bids or not asks:
            raise ValueError(f"Orderly order-book response for {market} did not include both bid and ask levels")
        return {
            "symbol": str(row.get("symbol") or symbol),
            "venue": self.venue_id,
            "asset_class": str(row.get("asset_class") or "unknown"),
            "source_type": "native_l2",
            "side": clean_side,
            "levels": (asks if clean_side == "buy" else bids)[:depth],
            "timestamp": self._book_timestamp(payload, data),
            "metadata": {
                **metadata,
                "endpoint": f"Orderly {path}",
                "venue_symbol": market,
                "orderly_payload_status": payload.get("success"),
                "last_update_id": data.get("lastUpdateId") or data.get("last_update_id"),
            },
        }


class DeriveReferencePriceAdapter:
    """Derive public currency adapter for spot reference prices."""

    venue_id = "derive"

    def __init__(
        self,
        *,
        base_url: str = "https://api.lyra.finance",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._aliases = _derivative_aliases(self.venue_id)

    def metadata(self) -> dict[str, Any]:
        row_count = len({id(row) for row in self._aliases.values()})
        return AdapterCapability(
            venue_id=self.venue_id,
            adapter_type="rest_public",
            status="implemented_unprobed" if row_count else "implemented_blocked_on_discovery_report",
            source_type="price_stream_no_book",
            supports_bidask=True,
            supports_l2_vwap=False,
            supports_trade_vwap=False,
            requires_auth=False,
            implementation="src.rwa_adapters.DeriveReferencePriceAdapter",
            notes=[
                "Uses Derive/Lyra public get_all_currencies endpoint for spot_price.",
                "The endpoint does not expose source timestamps or depth, so freshness and VWAP remain blocked.",
            ],
        ).as_dict()

    def resolve_symbol(self, symbol: str) -> dict[str, Any]:
        clean = _clean_symbol_key(symbol)
        row = (
            self._aliases.get(clean)
            or self._aliases.get(clean.replace("/", ""))
            or self._aliases.get(_base_symbol(symbol))
        )
        if row is None:
            raise ValueError(f"Derive symbol is not in the derivative discovery set: {symbol}")
        return row

    async def _get_json(self, path: str) -> dict[str, Any]:
        async def _request(client: httpx.AsyncClient) -> dict[str, Any]:
            response = await client.get(f"{self.base_url}{path}")
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise ValueError(
                    f"Derive {path} returned HTTP {response.status_code}: {response.text[:500]}"
                ) from exc
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError(f"Derive {path} response was not an object")
            return payload

        if self._client is not None:
            return await _request(self._client)
        async with httpx.AsyncClient(timeout=10) as client:
            return await _request(client)

    async def fetch_bidask(self, symbol: str) -> dict[str, Any]:
        row = self.resolve_symbol(symbol)
        metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
        currency = str(metadata.get("venue_symbol") or metadata.get("venue_market_id") or _base_symbol(symbol)).upper()
        payload = await self._get_json("/public/get_all_currencies")
        matches = [
            item for item in payload.get("result") or []
            if isinstance(item, dict) and _clean_symbol_key(str(item.get("currency") or "")) == currency
        ]
        if not matches:
            raise ValueError(f"Derive get_all_currencies returned no currency row for {currency}")
        selected = matches[0]
        price = _float_value(selected.get("spot_price"))
        if price is None or price <= 0:
            raise ValueError(f"Derive currency row had no positive spot_price for {currency}")
        return {
            "symbol": str(row.get("symbol") or symbol),
            "venue": self.venue_id,
            "asset_class": str(row.get("asset_class") or "unknown"),
            "source_type": "price_stream_no_book",
            "mid": price,
            "price": price,
            "timestamp": None,
            "metadata": {
                **metadata,
                "endpoint": "Derive /public/get_all_currencies",
                "currency": currency,
                "spot_price": selected.get("spot_price"),
                "spot_price_24h": selected.get("spot_price_24h"),
                "instrument_types": selected.get("instrument_types"),
                "ingested_at": _iso_now(),
                "source_timestamp_unavailable": True,
            },
        }

    async def fetch_order_book(
        self,
        symbol: str,
        *,
        side: str = "buy",
        depth: int = 100,
    ) -> dict[str, Any]:
        raise ValueError("Derive get_all_currencies is a reference price stream and does not expose L2 depth")


GAINS_PAIR_INDEX_BY_SYMBOL: dict[str, int] = {
    "AAPL/USD": 58,
    "MSFT/USD": 62,
    "SNAP/USD": 64,
    "NVDA/USD": 65,
    "PYPL/USD": 74,
    "MCD/USD": 80,
    "META/USD": 81,
    "GOOGL_1/USD": 82,
    "GME_1/USD": 83,
    "AMZN_1/USD": 84,
    "TSLA_1/USD": 85,
    "SPY/USD": 86,
    "QQQ/USD": 87,
    "IWM/USD": 88,
    "DIA/USD": 89,
    "XAU/USD": 90,
    "XAG/USD": 91,
    "USD/SGD": 93,
    "EUR/AUD": 110,
    "GBP/CAD": 115,
    "GBP/JPY": 117,
    "WTI/USD": 187,
    "XPT/USD": 188,
    "HG/USD": 190,
    "COIN/USD": 376,
    "HOOD/USD": 377,
    "MSTR/USD": 378,
    "CRCL/USD": 386,
    "PLTR/USD": 394,
    "LMT/USD": 397,
    "RIOT/USD": 398,
    "MARA/USD": 399,
    "NFLX_1/USD": 439,
    "GDX/USD": 446,
    "URA/USD": 447,
    "WPM/USD": 448,
    "URNM/USD": 451,
    "SPCX/USD": 454,
    "AVGO/USD": 458,
    "SNDK/USD": 459,
    "MU/USD": 460,
    "MRVL/USD": 461,
    "SAMSUNG/USD": 464,
    "SKHYNIX/USD": 465,
    "BOT/USD": 468,
    "BB/USD": 469,
    "LPTH/USD": 470,
    "ABCL/USD": 471,
    "IOVA/USD": 472,
    "BRUN/USD": 475,
    "WYFI/USD": 476,
    "SHAZ/USD": 477,
    "BE/USD": 478,
    "NBIS/USD": 479,
    "CRWV/USD": 480,
    "IREN/USD": 481,
}


class GainsPriceStreamAdapter:
    """Gains public pricing adapter for mark/index reference prices."""

    venue_id = "gains"
    source_type = "price_stream_no_book"

    def __init__(
        self,
        *,
        base_url: str = "https://backend-pricing.eu.gains.trade",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._cache_payload: dict[str, Any] | None = None
        self._cache_loaded_at = 0.0
        self._cache_lock = asyncio.Lock()

    def metadata(self) -> dict[str, Any]:
        return AdapterCapability(
            venue_id=self.venue_id,
            adapter_type="rest_public_reference_price",
            status="implemented_unprobed",
            source_type=self.source_type,
            supports_bidask=True,
            supports_l2_vwap=False,
            supports_trade_vwap=False,
            requires_auth=False,
            implementation="src.rwa_adapters.GainsPriceStreamAdapter",
            notes=[
                "Uses Gains public /charts snapshot endpoint plus documented pairIndex mapping.",
                "This is mark/index price evidence only; no native L2 book is exposed by this adapter.",
                "Production VWAP requires the Gains virtual-order-book or price-impact path and recent trade windows.",
            ],
        ).as_dict()

    @staticmethod
    def _symbol_key(symbol: str) -> str:
        return _clean_symbol_key(symbol).replace("-", "_")

    @classmethod
    def _resolve_pair_index(cls, symbol: str) -> int:
        clean = cls._symbol_key(symbol)
        aliases = {
            clean,
            clean.replace("_1/", "/"),
            clean.replace("/", ""),
        }
        for pair_symbol, index in GAINS_PAIR_INDEX_BY_SYMBOL.items():
            pair_key = cls._symbol_key(pair_symbol)
            if pair_key in aliases or pair_key.replace("_1/", "/") in aliases:
                return index
        raise RWAAdapterBlockedError(
            "gains_pair_index_not_mapped",
            f"Gains pairIndex is not mapped for {symbol}; update GAINS_PAIR_INDEX_BY_SYMBOL from the official pair list.",
        )

    async def _get_charts(self) -> dict[str, Any]:
        now = datetime.now(UTC).timestamp()
        if self._cache_payload is not None and now - self._cache_loaded_at < 1.0:
            return self._cache_payload
        async with self._cache_lock:
            now = datetime.now(UTC).timestamp()
            if self._cache_payload is not None and now - self._cache_loaded_at < 1.0:
                return self._cache_payload

            async def _request(client: httpx.AsyncClient) -> dict[str, Any]:
                response = await client.get(f"{self.base_url}/charts", headers={"accept": "application/json"})
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("Gains /charts response was not an object")
                return payload

            if self._client is not None:
                payload = await _request(self._client)
            else:
                async with httpx.AsyncClient(timeout=12) as client:
                    payload = await _request(client)
            self._cache_payload = payload
            self._cache_loaded_at = now
            return payload

    @staticmethod
    def _array_value(payload: dict[str, Any], key: str, index: int) -> float | None:
        values = payload.get(key)
        if not isinstance(values, list) or index >= len(values):
            return None
        return _float_value(values[index])

    @staticmethod
    def _asset_class(symbol: str) -> str:
        base = _base_symbol(symbol)
        if base in {"XAU", "XAG", "XPT", "HG", "WTI"}:
            return "commodity"
        if "/" in symbol and base in {"EUR", "GBP", "USD"}:
            return "fx"
        if base in {"SPY", "QQQ", "IWM", "DIA", "GDX", "URA", "URNM"}:
            return "index"
        return "equity"

    def _metadata(self, symbol: str, pair_index: int, payload: dict[str, Any]) -> dict[str, Any]:
        value_keys = ("opens", "highs", "lows", "closes", "indexPrices")
        raw_row = {key: self._array_value(payload, key, pair_index) for key in value_keys}
        return {
            "endpoint": f"{self.base_url}/charts",
            "pair_index": pair_index,
            "source_doc": "https://docs.gains.trade/developer/integrators/price-feed",
            "pair_list_doc": "https://docs.gains.trade/gtrade-leveraged-trading/pair-list",
            "raw_payload": {
                "time": payload.get("time"),
                "symbol": symbol,
                "pair_index": pair_index,
                "row": raw_row,
            },
            "raw_payload_ref": "gains_charts_snapshot",
            "depth_semantics": "reference_price_only_not_l2_depth",
            "production_gate": "candidate_only_pending_virtual_order_book_price_impact_recent_trade_windows_benchmark_rights",
        }

    async def fetch_bidask(self, symbol: str) -> dict[str, Any]:
        pair_index = self._resolve_pair_index(symbol)
        payload = await self._get_charts()
        price = self._array_value(payload, "indexPrices", pair_index) or self._array_value(payload, "closes", pair_index)
        if price is None or price <= 0:
            raise RWAAdapterBlockedError(
                "gains_price_missing_or_disabled",
                f"Gains /charts returned no positive price for {symbol} pairIndex={pair_index}",
            )
        spread_bps = 10.0
        return {
            "symbol": symbol,
            "venue": self.venue_id,
            "asset_class": self._asset_class(symbol),
            "source_type": self.source_type,
            "bid": price * (1 - spread_bps / 20_000),
            "ask": price * (1 + spread_bps / 20_000),
            "mid": price,
            "timestamp": _iso_from_epoch(payload.get("time")) or _iso_now(),
            "metadata": self._metadata(symbol, pair_index, payload),
        }

    async def fetch_order_book(
        self,
        symbol: str,
        *,
        side: str = "buy",
        depth: int = 100,
    ) -> dict[str, Any]:
        observation = await self.fetch_bidask(symbol)
        clean_side = side.strip().lower()
        if clean_side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        price = float(observation["ask"] if clean_side == "buy" else observation["bid"])
        return {
            **observation,
            "side": clean_side,
            "levels": [{"price": price, "notional_usd": 10_000.0}],
            "metadata": {
                **observation["metadata"],
                "depth_semantics": "single_level_reference_price_for_probe_only_not_vwap_depth",
            },
        }


class OstiumBuilderPriceAdapter:
    """Ostium Builder API adapter for live bid/mid/ask snapshots."""

    venue_id = "ostium"
    source_type = "synthetic_depth"

    def __init__(
        self,
        *,
        base_url: str = "https://builder.ostium.io",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = client
        self._cache_payload: dict[str, Any] | None = None
        self._cache_loaded_at = 0.0
        self._cache_lock = asyncio.Lock()

    def metadata(self) -> dict[str, Any]:
        return AdapterCapability(
            venue_id=self.venue_id,
            adapter_type="rest_public_builder_price",
            status="implemented_unprobed",
            source_type=self.source_type,
            supports_bidask=True,
            supports_l2_vwap=True,
            supports_trade_vwap=False,
            requires_auth=False,
            implementation="src.rwa_adapters.OstiumBuilderPriceAdapter",
            notes=[
                "Uses Ostium Builder API GET /v1/prices for bid/mid/ask and source timestamps.",
                "Order-book probes use conservative synthetic levels from bid/ask only.",
                "For production block VWAP, install/use the Builder SDK getSimOrderbook or equivalent permitted API and retain replayable responses.",
            ],
        ).as_dict()

    @staticmethod
    def _pair_key(symbol: str) -> str:
        return _clean_symbol_key(symbol).replace("/", "-").replace("_", "-")

    async def _get_prices(self) -> dict[str, Any]:
        now = datetime.now(UTC).timestamp()
        if self._cache_payload is not None and now - self._cache_loaded_at < 1.0:
            return self._cache_payload
        async with self._cache_lock:
            now = datetime.now(UTC).timestamp()
            if self._cache_payload is not None and now - self._cache_loaded_at < 1.0:
                return self._cache_payload

            async def _request(client: httpx.AsyncClient) -> dict[str, Any]:
                response = await client.get(f"{self.base_url}/v1/prices", headers={"accept": "application/json"})
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("Ostium /v1/prices response was not an object")
                return payload

            if self._client is not None:
                payload = await _request(self._client)
            else:
                async with httpx.AsyncClient(timeout=12) as client:
                    payload = await _request(client)
            self._cache_payload = payload
            self._cache_loaded_at = now
            return payload

    def _resolve_row(self, symbol: str, payload: dict[str, Any]) -> dict[str, Any]:
        prices = payload.get("prices") if isinstance(payload.get("prices"), list) else []
        target = self._pair_key(symbol)
        aliases = {target, target.replace("-1-", "-")}
        for row in prices:
            if not isinstance(row, dict):
                continue
            pair = self._pair_key(str(row.get("pair") or ""))
            pair_from = str(row.get("from") or "").upper()
            pair_to = str(row.get("to") or "").upper()
            if pair in aliases or self._pair_key(f"{pair_from}/{pair_to}") in aliases:
                return row
        raise RWAAdapterBlockedError(
            "ostium_pair_not_listed",
            f"Ostium /v1/prices returned no pair for {symbol}",
        )

    @staticmethod
    def _asset_class(row: dict[str, Any]) -> str:
        pair = str(row.get("pair") or "")
        base = str(row.get("from") or pair.split("-", 1)[0]).upper()
        quote = str(row.get("to") or "").upper()
        if base in {"XAU", "XAG", "XCU", "XPT", "XPD", "CL", "WTI", "BRENT"}:
            return "commodity"
        if quote in {"EUR", "GBP", "JPY", "HKD"} or base in {"AUD", "EUR", "GBP", "NZD", "USD"}:
            return "fx" if "/" in pair or "-" in pair and len(base) == 3 else "index"
        if base in {"US500", "US100", "US30", "GER40", "UK100", "JP225", "HK50"}:
            return "index"
        return "equity"

    def _metadata(self, row: dict[str, Any], payload: dict[str, Any]) -> dict[str, Any]:
        return {
            "endpoint": f"{self.base_url}/v1/prices",
            "source_doc": "https://docs.ostium.com/developer/builder-api/get-prices",
            "feed_id": row.get("feed_id"),
            "generated_at": payload.get("generatedAt"),
            "stale": payload.get("stale"),
            "is_market_open": row.get("isMarketOpen"),
            "is_day_trading_closed": row.get("isDayTradingClosed"),
            "schedule": row.get("schedule"),
            "raw_payload": {
                "row": row,
                "generatedAt": payload.get("generatedAt"),
                "stale": payload.get("stale"),
            },
            "raw_payload_ref": "ostium_builder_prices_snapshot",
            "depth_semantics": "synthetic_from_builder_bid_ask_not_native_l2",
            "production_gate": "candidate_only_pending_builder_sdk_sim_orderbook_depth_windows_benchmark_rights",
        }

    async def fetch_bidask(self, symbol: str) -> dict[str, Any]:
        payload = await self._get_prices()
        row = self._resolve_row(symbol, payload)
        bid = _float_value(row.get("bid"))
        ask = _float_value(row.get("ask"))
        mid = _float_value(row.get("mid"))
        if bid is None or ask is None or bid <= 0 or ask <= 0 or bid > ask:
            raise ValueError(f"Ostium returned invalid bid/ask for {symbol}")
        return {
            "symbol": str(row.get("pair") or symbol).replace("-", "/"),
            "venue": self.venue_id,
            "asset_class": self._asset_class(row),
            "source_type": self.source_type,
            "bid": bid,
            "ask": ask,
            "mid": mid or (bid + ask) / 2,
            "timestamp": _iso_from_epoch(row.get("timestampSeconds")) or _iso_from_epoch(payload.get("generatedAt")) or _iso_now(),
            "metadata": self._metadata(row, payload),
        }

    async def fetch_order_book(
        self,
        symbol: str,
        *,
        side: str = "buy",
        depth: int = 100,
    ) -> dict[str, Any]:
        observation = await self.fetch_bidask(symbol)
        clean_side = side.strip().lower()
        if clean_side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        bid = float(observation["bid"])
        ask = float(observation["ask"])
        spread = max(ask - bid, max((bid + ask) / 2, 1.0) * 0.0001)
        level_count = max(1, min(depth, 8))
        levels: list[dict[str, float]] = []
        for index in range(level_count):
            price = ask + spread * index if clean_side == "buy" else bid - spread * index
            levels.append({"price": price, "notional_usd": 10_000.0 / level_count})
        return {
            **observation,
            "side": clean_side,
            "levels": levels,
            "metadata": {
                **observation["metadata"],
                "depth_semantics": "synthetic_ladder_from_builder_bid_ask_for_probe_only",
            },
        }


class EVMPoolStateAdapter:
    """Candidate EVM CLMM state, swap-volume, and bounded tick-replay adapter."""

    source_type = "onchain_clmm_pool"
    _DECIMALS_SELECTOR = "0x313ce567"
    _TICK_BITMAP_SELECTOR = "0x5339c296"
    _TICKS_SELECTOR = "0xf30dba93"
    _SWAP_EVENT_TOPIC = "0xc42079f94a6350d7e6235f29174924f928cc2ac818eb64fed8004e115fbcca67"
    _AVERAGE_BLOCK_SECONDS = {"ethereum": 12.0, "base": 2.0}
    _POOL_SELECTORS = {
        "token0": "0x0dfe1681",
        "token1": "0xd21220a7",
        "fee": "0xddca3f43",
        "tick_spacing": "0xd0c93a7c",
        "liquidity": "0x1a686502",
        "slot0": "0x3850c7bd",
    }
    _RPC_ENV_BY_CHAIN = {
        "ethereum": "EVM_RPC_ETHEREUM_URL",
        "base": "EVM_RPC_BASE_URL",
        "arbitrum": "EVM_RPC_ARBITRUM_URL",
        "polygon": "EVM_RPC_POLYGON_URL",
        "optimism": "EVM_RPC_OPTIMISM_URL",
        "bsc": "EVM_RPC_BSC_URL",
        "bnb-chain": "EVM_RPC_BSC_URL",
        "avalanche": "EVM_RPC_AVALANCHE_URL",
        "avalanche-c-chain": "EVM_RPC_AVALANCHE_URL",
        "mantle": "EVM_RPC_MANTLE_URL",
        "gnosischain": "EVM_RPC_GNOSIS_URL",
        "gnosis": "EVM_RPC_GNOSIS_URL",
        "celo": "EVM_RPC_CELO_URL",
        "hyperevm": "EVM_RPC_HYPEREVM_URL",
        "ink": "EVM_RPC_INK_URL",
        "plume": "EVM_RPC_PLUME_URL",
        "plasma": "EVM_RPC_PLASMA_URL",
        "monad": "EVM_RPC_MONAD_URL",
        "sei": "EVM_RPC_SEI_URL",
        "xdc": "EVM_RPC_XDC_URL",
        "pharos": "EVM_RPC_PHAROS_URL",
        "zksync-era": "EVM_RPC_ZKSYNC_URL",
    }
    _RPC_URLS_ENV_BY_CHAIN = {
        chain: f"{env_name.removesuffix('_URL')}_URLS"
        for chain, env_name in _RPC_ENV_BY_CHAIN.items()
    }
    _PUBLIC_RPC_FALLBACKS = {
        "ethereum": (
            "https://ethereum-rpc.publicnode.com",
            "https://eth.llamarpc.com",
        ),
        "base": ("https://mainnet.base.org", "https://base-rpc.publicnode.com"),
    }
    _KNOWN_DECIMALS = {
        # Mainnet
        "0xa0b86991c6218b36c1d19d4a2e9eb0ce3606eb48": 6,  # USDC
        "0x45804880de22913dafe09f4980848ece6ecbaf78": 18,  # PAXG
        # Base
        "0x833589fcd6edb6e08f4c7c32d4f71b54bda02913": 6,  # USDC
        "0x60a3e35cc302bfa44cb288bc5a4f316fdb1adb42": 6,  # EURC
    }

    def __init__(
        self,
        *,
        venue_id: str,
        pool_path: str | Path | None = None,
        swap_cache_dir: str | Path | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.venue_id = venue_id
        self.pool_path = (
            Path(pool_path).expanduser()
            if pool_path
            else resolve_required_rwa_report_path("rwa_evm_pool_allowlist.json")
        )
        self._client = client
        self.swap_cache_dir = Path(
            swap_cache_dir
            or os.getenv("RWA_EVM_SWAP_CACHE_DIR", "")
            or "/data/rwa_evm_swap_cache"
        )
        self._aliases = self._load_aliases()
        self._decimals_cache: dict[tuple[str, str], int] = {}
        self._rpc_rate_lock = asyncio.Lock()
        self._rpc_last_call = 0.0

    def metadata(self) -> dict[str, Any]:
        row_count = len({id(row) for row in self._aliases.values()})
        return AdapterCapability(
            venue_id=self.venue_id,
            adapter_type="evm_rpc_pool_state",
            status="implemented_unprobed" if row_count else "implemented_blocked_on_discovery_report",
            source_type=self.source_type,
            supports_bidask=True,
            supports_l2_vwap=True,
            supports_trade_vwap=False,
            requires_auth=not bool(row_count),
            implementation="src.rwa_adapters.EVMPoolStateAdapter",
            notes=[
                f"Loaded {row_count} aliases from {self.pool_path}.",
                "Reads EVM pool slot0/liquidity/token metadata through configured RPC.",
                "Captures Swap logs plus a bounded initialized-tick range for exact-input replay.",
                "Synthetic levels remain excluded; bounded replay remains candidate evidence until sustained manipulation/depth, benchmark, rights, and human gates pass.",
            ],
        ).as_dict()

    @staticmethod
    def _json_dict(value: Any) -> dict[str, Any]:
        return value if isinstance(value, dict) else {}

    @staticmethod
    def _hex_to_int(value: Any) -> int | None:
        if not isinstance(value, str) or not value.startswith("0x"):
            return None
        try:
            return int(value, 16)
        except ValueError:
            return None

    @staticmethod
    def _hex_to_address(value: Any) -> str | None:
        if not isinstance(value, str) or not value.startswith("0x"):
            return None
        encoded = value[2:]
        if len(encoded) < 40:
            return None
        address = encoded[-40:]
        if set(address) == {"0"}:
            return None
        return f"0x{address.lower()}"

    @staticmethod
    def _pool_contract(row: dict[str, Any]) -> str:
        pool_address = str(row.get("pool_address") or "").strip()
        if pool_address.startswith("0x") and len(pool_address) == 42:
            return pool_address
        pool_id = str(row.get("pool_id") or "").strip()
        if pool_id.startswith("0x") and len(pool_id) == 42:
            return pool_id
        if pool_id.startswith("0x") and len(pool_id) == 66:
            raise RWAAdapterBlockedError(
                "uniswap_v4_pool_manager_decoder_missing",
                f"Uniswap v4 pool id for {row.get('symbol')} is not a pool contract address; "
                "decode PoolManager state by pool id before probing.",
            )
        raise ValueError(f"EVM pool row has no contract address: {row.get('symbol')}")

    @staticmethod
    def _chain(row: dict[str, Any]) -> str:
        return str(row.get("chain") or row.get("chain_id") or "").lower().strip()

    @staticmethod
    def _token_key(value: Any) -> str:
        return str(value or "").lower().strip()

    @staticmethod
    def _alias_keys(row: dict[str, Any]) -> set[str]:
        symbol = str(row.get("symbol") or "")
        asset_id = str(row.get("asset_id") or row.get("base_symbol") or "")
        base = str(row.get("base_symbol") or asset_id)
        quote = str(row.get("quote_symbol") or "USDC")
        base_token = str(row.get("base_token") or "").strip()
        keys = {
            _clean_symbol_key(symbol),
            _compact_market(symbol),
            _base_symbol(symbol) if symbol else "",
            _clean_symbol_key(f"{base}/{quote}"),
            _compact_market(f"{base}/{quote}"),
            _clean_symbol_key(asset_id),
            _clean_symbol_key(f"{asset_id}/USD") if asset_id else "",
            _compact_market(f"{asset_id}/USD") if asset_id else "",
            base_token.lower(),
            base_token.upper(),
            _clean_symbol_key(base_token) if base_token else "",
        }
        return {key for key in keys if key}

    def _load_aliases(self) -> dict[str, dict[str, Any]]:
        payload = _read_json_file(self.pool_path)
        pools = payload.get("pools") if isinstance(payload.get("pools"), list) else []
        aliases: dict[str, dict[str, Any]] = {}
        rows = [
            row for row in pools
            if isinstance(row, dict) and str(row.get("venue") or "") == self.venue_id
        ]
        rows.sort(
            key=lambda row: (
                str(row.get("review_status") or "") != "pool_block_state_ready_pending_live_quality",
                -float(row.get("liquidity_usd") or 0),
                str(row.get("symbol") or ""),
            )
        )
        for row in rows:
            for key in self._alias_keys(row):
                aliases.setdefault(key, row)
        return aliases

    def resolve_symbol(self, symbol: str) -> dict[str, Any]:
        clean = _clean_symbol_key(symbol)
        raw_lower = str(symbol or "").strip().lower()
        row = (
            self._aliases.get(raw_lower)
            or self._aliases.get(clean)
            or self._aliases.get(clean.replace("/", ""))
            or self._aliases.get(_base_symbol(symbol))
        )
        if row is None:
            raise RWAAdapterBlockedError(
                "evm_pool_not_allowlisted",
                f"{self.venue_id} has no reviewed EVM pool allowlist row for {symbol}; run EVM pool discovery or add a verified pool.",
            )
        return row

    def _rpc_candidates(self, chain: str) -> list[tuple[str, str]]:
        candidates: list[tuple[str, str]] = []
        env_name = self._RPC_ENV_BY_CHAIN.get(chain)
        if env_name and os.getenv(env_name):
            candidates.append((f"env:{env_name}", str(os.getenv(env_name))))
        urls_env_name = self._RPC_URLS_ENV_BY_CHAIN.get(chain)
        if urls_env_name:
            configured_urls = [
                url.strip()
                for url in str(os.getenv(urls_env_name) or "").replace("\n", ",").split(",")
                if url.strip()
            ]
            candidates.extend(
                (f"env:{urls_env_name}[{index}]", url)
                for index, url in enumerate(configured_urls, start=1)
            )
        if os.getenv("RWA_EVM_DISABLE_PUBLIC_RPC_FALLBACKS", "").strip().lower() not in {"1", "true", "yes"}:
            candidates.extend(
                (f"public_fallback:{url}", url) for url in self._PUBLIC_RPC_FALLBACKS.get(chain, ())
            )
        unique: list[tuple[str, str]] = []
        seen: set[str] = set()
        for source, url in candidates:
            if url in seen:
                continue
            seen.add(url)
            unique.append((source, url))
        return unique

    async def _json_rpc(self, rpc_url: str, method: str, params: list[Any]) -> Any:
        async def _request(client: httpx.AsyncClient) -> Any:
            response = await client.post(
                rpc_url,
                json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
                headers={"accept": "application/json", "content-type": "application/json"},
            )
            try:
                response.raise_for_status()
            except httpx.HTTPStatusError as exc:
                raise ValueError(f"EVM RPC {method} returned HTTP {response.status_code}: {response.text[:500]}") from exc
            payload = response.json()
            if isinstance(payload, dict) and payload.get("error"):
                raise ValueError(f"EVM RPC {method} returned error: {json.dumps(payload['error'], sort_keys=True)[:500]}")
            return payload.get("result") if isinstance(payload, dict) else None

        if self._client is not None:
            return await _request(self._client)
        async with httpx.AsyncClient(timeout=12) as client:
            return await _request(client)

    async def _call_first_rpc(self, chain: str, method: str, params: list[Any]) -> tuple[Any, str]:
        errors: list[str] = []
        for source, url in self._rpc_candidates(chain):
            retry_attempts = max(1, int(os.getenv("RWA_EVM_RPC_RETRY_ATTEMPTS", "4")))
            for attempt in range(retry_attempts):
                minimum_interval = max(
                    0.0,
                    float(os.getenv("RWA_EVM_RPC_MIN_INTERVAL_SECONDS", "0.09")),
                )
                async with self._rpc_rate_lock:
                    loop = asyncio.get_running_loop()
                    wait_seconds = self._rpc_last_call + minimum_interval - loop.time()
                    if wait_seconds > 0:
                        await asyncio.sleep(wait_seconds)
                    self._rpc_last_call = loop.time()
                    try:
                        return await self._json_rpc(url, method, params), source
                    except ValueError as exc:
                        message = str(exc)
                transient = any(
                    marker in message.lower()
                    for marker in ("429", "rate limit", "too many requests", "timeout")
                )
                errors.append(f"{source}: {message}")
                if not transient or attempt + 1 >= retry_attempts:
                    break
                await asyncio.sleep(min(4.0, 0.5 * 2**attempt))
        raise RWAAdapterBlockedError(
            "evm_rpc_and_pool_state",
            f"No working EVM RPC for chain={chain}; errors={errors[:3]}",
        )

    async def _eth_call(self, chain: str, contract: str, data: str, block_tag: str) -> tuple[Any, str]:
        return await self._call_first_rpc(chain, "eth_call", [{"to": contract, "data": data}, block_tag])

    async def _block_timestamp(self, chain: str, block_number: int) -> tuple[int, str]:
        payload, source = await self._call_first_rpc(
            chain,
            "eth_getBlockByNumber",
            [hex(block_number), False],
        )
        timestamp = self._hex_to_int(payload.get("timestamp")) if isinstance(payload, dict) else None
        if timestamp is None:
            raise ValueError(f"Could not decode timestamp for {chain} block {block_number}")
        return timestamp, source

    async def _start_block_for_window(
        self,
        chain: str,
        end_block: int,
        lookback_seconds: int,
    ) -> tuple[int, int, int, str]:
        end_timestamp, source = await self._block_timestamp(chain, end_block)
        target_timestamp = max(0, end_timestamp - lookback_seconds)
        average_seconds = self._AVERAGE_BLOCK_SECONDS.get(chain, 3.0)
        estimated_span = max(1, int(lookback_seconds / average_seconds * 1.5))
        low = max(0, end_block - estimated_span)
        low_timestamp, source = await self._block_timestamp(chain, low)
        while low > 0 and low_timestamp > target_timestamp:
            estimated_span *= 2
            low = max(0, end_block - estimated_span)
            low_timestamp, source = await self._block_timestamp(chain, low)
        high = end_block
        while low < high:
            mid = (low + high) // 2
            timestamp, source = await self._block_timestamp(chain, mid)
            if timestamp < target_timestamp:
                low = mid + 1
            else:
                high = mid
        start_timestamp, source = await self._block_timestamp(chain, low)
        return low, start_timestamp, end_timestamp, source

    async def _swap_logs(
        self,
        *,
        chain: str,
        contract: str,
        start_block: int,
        end_block: int,
        chunk_size: int | None = None,
    ) -> tuple[list[dict[str, Any]], list[str]]:
        effective_chunk_size = (
            max(1, int(chunk_size))
            if chunk_size is not None
            else max(100, int(os.getenv("RWA_EVM_LOG_BLOCK_CHUNK_SIZE", "5000")))
        )
        logs: list[dict[str, Any]] = []
        sources: list[str] = []
        for chunk_start in range(start_block, end_block + 1, effective_chunk_size):
            chunk_end = min(end_block, chunk_start + effective_chunk_size - 1)
            payload, source = await self._call_first_rpc(
                chain,
                "eth_getLogs",
                [
                    {
                        "address": contract,
                        "fromBlock": hex(chunk_start),
                        "toBlock": hex(chunk_end),
                        "topics": [self._SWAP_EVENT_TOPIC],
                    }
                ],
            )
            if isinstance(payload, list):
                logs.extend(row for row in payload if isinstance(row, dict))
            sources.append(source)
        return logs, sorted(set(sources))

    def _swap_cache_path(self, chain: str, contract: str) -> Path:
        safe_contract = contract.lower().replace("0x", "", 1)
        return self.swap_cache_dir / f"{chain}-{safe_contract}.json"

    def _load_swap_cache(self, chain: str, contract: str) -> dict[str, Any]:
        payload = _read_json_file(self._swap_cache_path(chain, contract))
        if (
            payload.get("chain") != chain
            or str(payload.get("pool_contract") or "").lower() != contract.lower()
            or not isinstance(payload.get("logs"), list)
        ):
            return {}
        return payload

    def _persist_swap_cache(
        self,
        chain: str,
        contract: str,
        payload: dict[str, Any],
    ) -> bool:
        path = self._swap_cache_path(chain, contract)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".tmp")
            temporary.write_text(
                json.dumps(payload, sort_keys=True, default=str) + "\n",
                encoding="utf-8",
            )
            temporary.replace(path)
        except OSError:
            return False
        return True

    @staticmethod
    def _provider_log_range_limit(exc: Exception) -> int | None:
        match = re.search(r"limited to an?\s+(\d+)\s+range", str(exc), re.IGNORECASE)
        return max(1, int(match.group(1))) if match else None

    @staticmethod
    def _deduplicate_logs(logs: list[dict[str, Any]]) -> list[dict[str, Any]]:
        unique: dict[tuple[str, str], dict[str, Any]] = {}
        for log in logs:
            key = (
                str(log.get("transactionHash") or ""),
                str(log.get("logIndex") or ""),
            )
            unique[key] = log
        return sorted(
            unique.values(),
            key=lambda log: (
                int(str(log.get("blockNumber") or "0x0"), 16),
                int(str(log.get("logIndex") or "0x0"), 16),
            ),
        )

    async def _collect_swap_log_window(
        self,
        *,
        chain: str,
        contract: str,
        end_block: int,
        lookback_seconds: int,
    ) -> tuple[list[dict[str, Any]], dict[str, Any], list[str]]:
        """Collect a full window or extend a persistent provider-limited cache."""
        target_start, _, end_timestamp, block_source = await self._start_block_for_window(
            chain,
            end_block,
            lookback_seconds,
        )
        cache = self._load_swap_cache(chain, contract)
        cached_start = int(cache.get("start_block") or 0) if cache else 0
        cached_end = int(cache.get("end_block") or -1) if cache else -1
        cached_logs = [row for row in (cache.get("logs") or []) if isinstance(row, dict)]
        provider_chunk_size = int(cache.get("provider_chunk_size") or 0) if cache else 0
        sources = [str(value) for value in (cache.get("rpc_sources") or []) if value]
        collection_error: str | None = None

        if cache and cached_start <= cached_end <= end_block:
            if cached_end < end_block:
                try:
                    new_logs, new_sources = await self._swap_logs(
                        chain=chain,
                        contract=contract,
                        start_block=cached_end + 1,
                        end_block=end_block,
                        chunk_size=provider_chunk_size or None,
                    )
                    cached_logs.extend(new_logs)
                    sources.extend(new_sources)
                    cached_end = end_block
                except Exception as exc:
                    collection_error = f"{type(exc).__name__}: {str(exc)[:1000]}"
        else:
            cache = {}
            cached_logs = []
            try:
                cached_logs, new_sources = await self._swap_logs(
                    chain=chain,
                    contract=contract,
                    start_block=target_start,
                    end_block=end_block,
                )
                sources.extend(new_sources)
                cached_start = target_start
                cached_end = end_block
            except Exception as exc:
                provider_chunk_size = self._provider_log_range_limit(exc) or 0
                if not provider_chunk_size:
                    raise
                bootstrap_seconds = max(
                    300,
                    min(
                        lookback_seconds,
                        int(os.getenv("RWA_EVM_SWAP_CACHE_BOOTSTRAP_SECONDS", "1800")),
                    ),
                )
                average_seconds = self._AVERAGE_BLOCK_SECONDS.get(chain, 3.0)
                bootstrap_blocks = max(1, int(bootstrap_seconds / average_seconds))
                cached_start = max(target_start, end_block - bootstrap_blocks)
                cached_logs, new_sources = await self._swap_logs(
                    chain=chain,
                    contract=contract,
                    start_block=cached_start,
                    end_block=end_block,
                    chunk_size=provider_chunk_size,
                )
                sources.extend(new_sources)
                cached_end = end_block

        retained_start = max(target_start, cached_start)
        retained_logs = self._deduplicate_logs(
            [
                log
                for log in cached_logs
                if int(str(log.get("blockNumber") or "0x0"), 16) >= retained_start
            ]
        )
        coverage_end = min(cached_end, end_block)
        start_timestamp, start_source = await self._block_timestamp(chain, retained_start)
        if coverage_end == end_block:
            coverage_end_timestamp = end_timestamp
        else:
            coverage_end_timestamp, _ = await self._block_timestamp(chain, coverage_end)
        sources.extend([block_source, start_source])
        complete = retained_start <= target_start and coverage_end >= end_block
        cache_payload = {
            "schema_version": 1,
            "chain": chain,
            "pool_contract": contract.lower(),
            "start_block": retained_start,
            "end_block": coverage_end,
            "provider_chunk_size": provider_chunk_size or None,
            "rpc_sources": sorted(set(sources)),
            "logs": retained_logs,
            "updated_at": _iso_now(),
        }
        cache_persisted = self._persist_swap_cache(chain, contract, cache_payload)
        window = {
            "status": "ok" if complete and collection_error is None else "collecting",
            "lookback_seconds": lookback_seconds,
            "start_block": retained_start,
            "end_block": coverage_end,
            "target_start_block": target_start,
            "target_end_block": end_block,
            "start_timestamp": datetime.fromtimestamp(start_timestamp, tz=UTC).isoformat(),
            "end_timestamp": datetime.fromtimestamp(
                coverage_end_timestamp,
                tz=UTC,
            ).isoformat(),
            "window_coverage_seconds": max(0, coverage_end_timestamp - start_timestamp),
            "provider_chunk_size": provider_chunk_size or None,
            "cache_persisted": cache_persisted,
            "collection_error": collection_error,
        }
        return retained_logs, window, sorted(set(sources))

    async def _token_decimals(self, chain: str, token: str, block_tag: str) -> int:
        token_key = self._token_key(token)
        if token_key in self._KNOWN_DECIMALS:
            return self._KNOWN_DECIMALS[token_key]
        cache_key = (chain, token_key)
        if cache_key in self._decimals_cache:
            return self._decimals_cache[cache_key]
        result, _ = await self._eth_call(chain, token, self._DECIMALS_SELECTOR, block_tag)
        parsed = self._hex_to_int(result)
        if parsed is None or parsed < 0 or parsed > 36:
            raise ValueError(f"Could not decode ERC20 decimals for {token} on {chain}")
        self._decimals_cache[cache_key] = parsed
        return parsed

    @staticmethod
    def _decode_slot0(slot0_raw: str) -> dict[str, int]:
        if not isinstance(slot0_raw, str) or not slot0_raw.startswith("0x") or len(slot0_raw) < 66:
            raise ValueError("slot0 response was missing or too short")
        body = slot0_raw[2:]
        words = [body[index:index + 64] for index in range(0, len(body), 64)]
        sqrt_price_x96 = int(words[0], 16)
        tick_raw = int(words[1], 16) if len(words) > 1 and words[1] else 0
        if tick_raw >= 2 ** 255:
            tick_raw -= 2 ** 256
        return {"sqrt_price_x96": sqrt_price_x96, "tick": tick_raw}

    @staticmethod
    def _price_from_slot0(
        *,
        sqrt_price_x96: int,
        token0: str,
        token1: str,
        decimals0: int,
        decimals1: int,
        base_token: str,
        quote_token: str,
    ) -> float:
        ratio_token1_per_token0 = (sqrt_price_x96 * sqrt_price_x96 / 2 ** 192) * (10 ** (decimals0 - decimals1))
        token0_key = EVMPoolStateAdapter._token_key(token0)
        token1_key = EVMPoolStateAdapter._token_key(token1)
        base_key = EVMPoolStateAdapter._token_key(base_token)
        quote_key = EVMPoolStateAdapter._token_key(quote_token)
        if base_key == token0_key and quote_key == token1_key:
            return float(ratio_token1_per_token0)
        if base_key == token1_key and quote_key == token0_key:
            if ratio_token1_per_token0 <= 0:
                raise ValueError("slot0 ratio was non-positive")
            return float(1 / ratio_token1_per_token0)
        raise ValueError(
            f"Pool token order does not match expected pair: token0={token0} token1={token1} base={base_token} quote={quote_token}"
        )

    async def _fetch_state(self, row: dict[str, Any]) -> dict[str, Any]:
        chain = self._chain(row)
        contract = self._pool_contract(row)
        block_hex, block_source = await self._call_first_rpc(chain, "eth_blockNumber", [])
        block_number = self._hex_to_int(block_hex)
        block_tag = hex(block_number) if block_number is not None else "latest"
        raw_calls: dict[str, Any] = {}
        call_sources: dict[str, str] = {"block_number": block_source}
        for name, selector in self._POOL_SELECTORS.items():
            result, source = await self._eth_call(chain, contract, selector, block_tag)
            raw_calls[name] = result
            call_sources[name] = source
        token0 = self._hex_to_address(raw_calls.get("token0")) or str(row.get("token0") or "")
        token1 = self._hex_to_address(raw_calls.get("token1")) or str(row.get("token1") or "")
        if not token0 or not token1:
            raise ValueError("EVM pool token0/token1 calls did not return addresses")
        decimals0 = await self._token_decimals(chain, token0, block_tag)
        decimals1 = await self._token_decimals(chain, token1, block_tag)
        slot0 = self._decode_slot0(str(raw_calls.get("slot0") or ""))
        price = self._price_from_slot0(
            sqrt_price_x96=slot0["sqrt_price_x96"],
            token0=token0,
            token1=token1,
            decimals0=decimals0,
            decimals1=decimals1,
            base_token=str(row.get("base_token") or ""),
            quote_token=str(row.get("quote_token") or ""),
        )
        if price <= 0:
            raise ValueError("EVM pool-state price was non-positive")
        return {
            "chain": chain,
            "pool_contract": contract,
            "block_number": block_number,
            "block_tag": block_tag,
            "raw_calls": raw_calls,
            "call_sources": call_sources,
            "token0": token0,
            "token1": token1,
            "decimals0": decimals0,
            "decimals1": decimals1,
            "fee_tier": self._hex_to_int(raw_calls.get("fee")),
            "tick_spacing": self._hex_to_int(raw_calls.get("tick_spacing")),
            "liquidity": self._hex_to_int(raw_calls.get("liquidity")),
            "sqrt_price_x96": slot0["sqrt_price_x96"],
            "tick": slot0["tick"],
            "price": price,
        }

    @staticmethod
    def _asset_class(row: dict[str, Any]) -> str:
        asset = str(row.get("asset_id") or row.get("base_symbol") or "").upper()
        if asset in {"PAXG", "XAUT"}:
            return "metal"
        if asset in {"EURC"}:
            return "fx"
        if asset in {"BUIDL", "OUSG", "TBILL", "USCC", "USDY", "USTB"}:
            return "treasury_fund"
        return "tokenized_asset"

    @staticmethod
    def _display_symbol(row: dict[str, Any]) -> str:
        return str(row.get("symbol") or f"{row.get('base_symbol')}/{row.get('quote_symbol')}")

    @staticmethod
    def _spread_bps(state: dict[str, Any]) -> float:
        fee = state.get("fee_tier")
        if isinstance(fee, int) and fee > 0:
            # Uniswap v3 fee tiers are in hundredths of a basis point.
            return max(2.0, min(100.0, fee / 100))
        return 10.0

    @staticmethod
    def _synthetic_levels(price: float, liquidity_usd: float, spread_bps: float, side: str, depth: int) -> list[dict[str, float]]:
        usable_liquidity = max(1_000.0, min(max(liquidity_usd * 0.02, 1_000.0), 250_000.0))
        level_count = max(1, min(depth, 8))
        per_level = usable_liquidity / level_count
        levels: list[dict[str, float]] = []
        for index in range(level_count):
            impact_bps = spread_bps / 2 + index * max(spread_bps, 5.0)
            level_price = price * (1 + impact_bps / 10_000) if side == "buy" else price * (1 - impact_bps / 10_000)
            levels.append({"price": level_price, "notional_usd": per_level})
        return levels

    def _metadata(self, row: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
        return {
            "endpoint": "EVM JSON-RPC eth_call",
            "pool_allowlist_path": str(self.pool_path),
            "pool_contract": state["pool_contract"],
            "chain": state["chain"],
            "block_number": state["block_number"],
            "block_tag": state["block_tag"],
            "rpc_sources": state["call_sources"],
            "token0": state["token0"],
            "token1": state["token1"],
            "base_token": str(row.get("base_token") or ""),
            "quote_token": str(row.get("quote_token") or ""),
            "decimals0": state["decimals0"],
            "decimals1": state["decimals1"],
            "fee_tier": state.get("fee_tier"),
            "tick_spacing": state.get("tick_spacing"),
            "liquidity": state.get("liquidity"),
            "sqrt_price_x96": str(state["sqrt_price_x96"]),
            "tick": state["tick"],
            "raw_payload": {
                "method": "eth_call",
                "pool_contract": state["pool_contract"],
                "block_tag": state["block_tag"],
                "raw_calls": state["raw_calls"],
            },
            "raw_payload_ref": str(self.pool_path),
            "depth_semantics": "synthetic_from_current_pool_mid_and_liquidity_not_full_tick_replay",
            "production_gate": "candidate_only_pending_tick_bitmap_liquidity_replay_depth_manipulation_benchmark_rights",
            "discovery_liquidity_usd": row.get("liquidity_usd"),
            "discovery_url": row.get("url"),
        }

    async def fetch_pool_replay_evidence(
        self,
        symbol: str,
        observation: dict[str, Any],
        *,
        lookback_seconds: int = 86_400,
        word_radius: int = 2,
        max_initialized_ticks: int = 128,
        target_notionals_usd: tuple[float, ...] = (1_000.0, 10_000.0, 50_000.0),
    ) -> dict[str, Any]:
        """Capture swap volume and bounded initialized-tick replay at one block."""
        row = self.resolve_symbol(symbol)
        metadata = observation.get("metadata") if isinstance(observation.get("metadata"), dict) else {}
        chain = str(metadata.get("chain") or self._chain(row))
        contract = str(metadata.get("pool_contract") or self._pool_contract(row))
        block_number = int(metadata.get("block_number"))
        block_tag = str(metadata.get("block_tag") or hex(block_number))
        tick = int(metadata.get("tick"))
        tick_spacing = int(metadata.get("tick_spacing"))
        if tick_spacing <= 0:
            raise ValueError("tick spacing must be positive")
        compressed = tick // tick_spacing
        current_word = compressed >> 8
        radius = max(1, min(int(word_radius), 8))
        word_positions = list(range(current_word - radius, current_word + radius + 1))

        async def fetch_bitmap(word_position: int) -> tuple[int, int, str]:
            data = f"{self._TICK_BITMAP_SELECTOR}{encode_signed_argument(word_position, 16)}"
            result, source = await self._eth_call(chain, contract, data, block_tag)
            bitmap = self._hex_to_int(result)
            if bitmap is None:
                raise ValueError(f"Could not decode tick bitmap word {word_position}")
            return word_position, bitmap, source

        bitmap_rows = await asyncio.gather(*(fetch_bitmap(word) for word in word_positions))
        initialized_tick_numbers = sorted(
            (word << 8 | bit) * tick_spacing
            for word, bitmap, _ in bitmap_rows
            for bit in range(256)
            if bitmap & (1 << bit)
        )
        ticks_truncated = len(initialized_tick_numbers) > max_initialized_ticks
        if ticks_truncated:
            initialized_tick_numbers = sorted(
                sorted(initialized_tick_numbers, key=lambda value: abs(value - tick))[
                    :max_initialized_ticks
                ]
            )

        async def fetch_tick(tick_number: int) -> dict[str, Any]:
            data = f"{self._TICKS_SELECTOR}{encode_signed_argument(tick_number, 24)}"
            result, source = await self._eth_call(chain, contract, data, block_tag)
            raw = str(result or "")
            if not raw.startswith("0x") or len(raw) < 2 + 64 * 2:
                raise ValueError(f"Could not decode initialized tick {tick_number}")
            words = [raw[2 + index * 64 : 2 + (index + 1) * 64] for index in range(2)]
            return {
                "tick": tick_number,
                "liquidity_gross": int(words[0], 16),
                "liquidity_net": decode_signed_word(words[1]),
                "initialized": int(words[0], 16) > 0,
                "rpc_source": source,
            }

        initialized_ticks: list[dict[str, Any]] = []
        for offset in range(0, len(initialized_tick_numbers), 16):
            initialized_ticks.extend(
                await asyncio.gather(
                    *(fetch_tick(value) for value in initialized_tick_numbers[offset : offset + 16])
                )
            )

        raw_logs: list[dict[str, Any]] = []
        log_sources: list[str] = []
        volume_window: dict[str, Any]
        try:
            raw_logs, collection_window, log_sources = await self._collect_swap_log_window(
                chain=chain,
                contract=contract,
                end_block=block_number,
                lookback_seconds=lookback_seconds,
            )
            volume = summarize_swap_logs(
                raw_logs,
                token0=str(metadata.get("token0")),
                token1=str(metadata.get("token1")),
                decimals0=int(metadata.get("decimals0")),
                decimals1=int(metadata.get("decimals1")),
                base_token=str(metadata.get("base_token") or row.get("base_token")),
                quote_token=str(metadata.get("quote_token") or row.get("quote_token")),
            )
            volume_window = {
                **{key: value for key, value in volume.items() if key != "decoded_swaps"},
                **collection_window,
            }
        except Exception as exc:
            volume_window = {
                "status": "error",
                "lookback_seconds": lookback_seconds,
                "end_block": block_number,
                "quote_volume_usd": None,
                "window_coverage_seconds": 0,
                "error": f"{type(exc).__name__}: {str(exc)[:1000]}",
            }
        replay_state = {
            "token0": str(metadata.get("token0")),
            "token1": str(metadata.get("token1")),
            "base_token": str(metadata.get("base_token") or row.get("base_token")),
            "quote_token": str(metadata.get("quote_token") or row.get("quote_token")),
            "decimals0": int(metadata.get("decimals0")),
            "decimals1": int(metadata.get("decimals1")),
            "fee_tier": int(metadata.get("fee_tier") or 0),
            "sqrt_price_x96": int(metadata.get("sqrt_price_x96")),
            "tick": tick,
            "tick_spacing": tick_spacing,
            "liquidity": int(metadata.get("liquidity")),
            "price": (float(observation["bid"]) + float(observation["ask"])) / 2,
            "initialized_ticks": initialized_ticks,
            "max_ticks_crossed": max_initialized_ticks,
        }
        target_fills = {
            side: [
                simulate_exact_input(
                    replay_state,
                    side=side,
                    target_notional_usd=target,
                )
                for target in target_notionals_usd
            ]
            for side in ("buy", "sell")
        }
        return {
            "symbol": self._display_symbol(row),
            "venue": self.venue_id,
            "source_type": "block_pinned_clmm_tick_and_swap_replay",
            "captured_at": _iso_now(),
            "chain": chain,
            "pool_contract": contract,
            "block_number": block_number,
            "block_tag": block_tag,
            "tick_word_range": [word_positions[0], word_positions[-1]],
            "tick_word_count": len(word_positions),
            "initialized_tick_count": len(initialized_ticks),
            "initialized_ticks_truncated": ticks_truncated,
            "initialized_ticks": initialized_ticks,
            "target_fills": target_fills,
            "volume_window": volume_window,
            "replay_payload": {
                "bitmap_words": [
                    {"word_position": word, "bitmap": hex(bitmap)}
                    for word, bitmap, _ in bitmap_rows
                ],
                "initialized_ticks": initialized_ticks,
                "swap_logs": raw_logs,
            },
            "rpc_sources": sorted(
                {
                    *log_sources,
                    *(source for _, _, source in bitmap_rows),
                    *(str(item["rpc_source"]) for item in initialized_ticks),
                }
            ),
            "semantics": {
                "depth": "bounded_exact_input_replay_from_block_pinned_tick_bitmap_and_liquidity_net",
                "volume": "decoded_pool_Swap_events_over_timestamp_bounded_window",
                "limitations": (
                    "Replay is conservative outside the captured bitmap range; partial fills remain partial. "
                    "Sender/recipient counts are routing proxies, not unique beneficial traders."
                ),
            },
        }

    async def fetch_bidask(self, symbol: str) -> dict[str, Any]:
        row = self.resolve_symbol(symbol)
        state = await self._fetch_state(row)
        spread_bps = self._spread_bps(state)
        mid = state["price"]
        return {
            "symbol": self._display_symbol(row),
            "venue": self.venue_id,
            "asset_class": self._asset_class(row),
            "source_type": self.source_type,
            "bid": mid * (1 - spread_bps / 20_000),
            "ask": mid * (1 + spread_bps / 20_000),
            "timestamp": _iso_now(),
            "metadata": self._metadata(row, state),
        }

    async def fetch_order_book(
        self,
        symbol: str,
        *,
        side: str = "buy",
        depth: int = 100,
    ) -> dict[str, Any]:
        row = self.resolve_symbol(symbol)
        clean_side = side.strip().lower()
        if clean_side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        state = await self._fetch_state(row)
        price = state["price"]
        liquidity_usd = _float_value(row.get("liquidity_usd")) or 0.0
        return {
            "symbol": self._display_symbol(row),
            "venue": self.venue_id,
            "asset_class": self._asset_class(row),
            "source_type": self.source_type,
            "side": clean_side,
            "levels": self._synthetic_levels(price, liquidity_usd, self._spread_bps(state), clean_side, depth),
            "timestamp": _iso_now(),
            "metadata": self._metadata(row, state),
        }


class EVMPairMetadataAdapter:
    """Live candidate adapter for EVM pool rows backed by pair metadata.

    This is used for pool families whose exact invariant math has not been
    implemented yet. It refreshes public pair metadata and attaches RPC block
    evidence, but keeps depth synthetic and promotion blocked.
    """

    _RPC_ENV_BY_CHAIN = EVMPoolStateAdapter._RPC_ENV_BY_CHAIN
    _PUBLIC_RPC_FALLBACKS = EVMPoolStateAdapter._PUBLIC_RPC_FALLBACKS

    def __init__(
        self,
        *,
        venue_id: str,
        source_type: str = "onchain_stableswap_pool",
        pool_path: str | Path | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.venue_id = venue_id
        self.source_type = source_type
        self.pool_path = (
            Path(pool_path).expanduser()
            if pool_path
            else resolve_required_rwa_report_path("rwa_evm_pool_allowlist.json")
        )
        self._client = client
        self._aliases = self._load_aliases()

    def metadata(self) -> dict[str, Any]:
        row_count = len({id(row) for row in self._aliases.values()})
        return AdapterCapability(
            venue_id=self.venue_id,
            adapter_type="evm_pair_metadata_candidate",
            status="implemented_unprobed" if row_count else "implemented_blocked_on_discovery_report",
            source_type=self.source_type,
            supports_bidask=True,
            supports_l2_vwap=True,
            supports_trade_vwap=False,
            requires_auth=not bool(row_count),
            implementation="src.rwa_adapters.EVMPairMetadataAdapter",
            notes=[
                f"Loaded {row_count} aliases from {self.pool_path}.",
                "Refreshes public pair metadata and attaches current RPC block evidence.",
                "Depth is synthetic from reported liquidity; this is not invariant-level pool replay.",
                "Balancer/Curve production promotion requires protocol-specific balance/weight/amplification math.",
            ],
        ).as_dict()

    @staticmethod
    def _alias_keys(row: dict[str, Any]) -> set[str]:
        return EVMPoolStateAdapter._alias_keys(row)

    def _load_aliases(self) -> dict[str, dict[str, Any]]:
        payload = _read_json_file(self.pool_path)
        pools = payload.get("pools") if isinstance(payload.get("pools"), list) else []
        aliases: dict[str, dict[str, Any]] = {}
        rows = [
            row for row in pools
            if isinstance(row, dict) and str(row.get("venue") or "") == self.venue_id
        ]
        rows.sort(
            key=lambda row: (
                -float(row.get("liquidity_usd") or 0),
                str(row.get("symbol") or ""),
            )
        )
        for row in rows:
            for key in self._alias_keys(row):
                aliases.setdefault(key, row)
        return aliases

    def resolve_symbol(self, symbol: str) -> dict[str, Any]:
        clean = _clean_symbol_key(symbol)
        raw_lower = str(symbol or "").strip().lower()
        row = (
            self._aliases.get(raw_lower)
            or self._aliases.get(clean)
            or self._aliases.get(clean.replace("/", ""))
            or self._aliases.get(_base_symbol(symbol))
        )
        if row is None:
            raise RWAAdapterBlockedError(
                "evm_pool_not_allowlisted",
                f"{self.venue_id} has no reviewed EVM pool allowlist row for {symbol}; run EVM pool discovery or add a verified pool.",
            )
        return row

    def _rpc_candidates(self, chain: str) -> list[tuple[str, str]]:
        candidates: list[tuple[str, str]] = []
        env_name = self._RPC_ENV_BY_CHAIN.get(chain)
        if env_name and os.getenv(env_name):
            candidates.append((f"env:{env_name}", str(os.getenv(env_name))))
        if os.getenv("RWA_EVM_DISABLE_PUBLIC_RPC_FALLBACKS", "").strip().lower() not in {"1", "true", "yes"}:
            candidates.extend(
                (f"public_fallback:{url}", url) for url in self._PUBLIC_RPC_FALLBACKS.get(chain, ())
            )
        return candidates

    async def _block_number(self, chain: str) -> tuple[int | None, str | None]:
        candidates = self._rpc_candidates(chain)
        errors: list[str] = []
        async def _request(rpc_url: str) -> Any:
            if self._client is not None:
                response = await self._client.post(
                    rpc_url,
                    json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
                    headers={"accept": "application/json", "content-type": "application/json"},
                )
                response.raise_for_status()
                payload = response.json()
                return payload.get("result") if isinstance(payload, dict) else None
            async with httpx.AsyncClient(timeout=10) as client:
                response = await client.post(
                    rpc_url,
                    json={"jsonrpc": "2.0", "id": 1, "method": "eth_blockNumber", "params": []},
                    headers={"accept": "application/json", "content-type": "application/json"},
                )
                response.raise_for_status()
                payload = response.json()
                return payload.get("result") if isinstance(payload, dict) else None

        for source, url in candidates:
            try:
                parsed = EVMPoolStateAdapter._hex_to_int(await _request(url))
                if parsed is not None:
                    return parsed, source
            except (httpx.HTTPError, ValueError) as exc:
                errors.append(f"{source}: {exc}")
        if errors:
            return None, "; ".join(errors[:3])
        return None, None

    @staticmethod
    def _base_quote(symbol: str) -> tuple[str, str]:
        if "/" in symbol:
            base, quote = symbol.split("/", 1)
            return base.upper(), quote.upper()
        compact = symbol.upper()
        return compact.removesuffix("USD").removesuffix("USDC"), "USDC"

    @staticmethod
    def _pair_matches(row: dict[str, Any], pair: dict[str, Any]) -> bool:
        chain = str(row.get("chain") or "").lower()
        if str(pair.get("chainId") or "").lower() != chain:
            return False
        expected_dex = str(row.get("dex_id") or "").lower()
        if expected_dex and str(pair.get("dexId") or "").lower() != expected_dex:
            return False
        base, quote = EVMPairMetadataAdapter._base_quote(str(row.get("symbol") or ""))
        base_token = pair.get("baseToken") if isinstance(pair.get("baseToken"), dict) else {}
        quote_token = pair.get("quoteToken") if isinstance(pair.get("quoteToken"), dict) else {}
        pair_base = str(base_token.get("symbol") or "").upper().replace("-", "").replace("_", "")
        pair_quote = str(quote_token.get("symbol") or "").upper().replace("-", "").replace("_", "")
        return pair_base == base.replace("-", "").replace("_", "") and pair_quote in {
            quote.replace("-", "").replace("_", ""),
            "USDC",
            "USDBC",
        }

    async def _fetch_pair(self, row: dict[str, Any]) -> dict[str, Any]:
        base, quote = self._base_quote(str(row.get("symbol") or ""))
        url = "https://api.dexscreener.com/latest/dex/search/"
        params = {"q": f"{base} {quote}"}

        async def _request(client: httpx.AsyncClient) -> dict[str, Any]:
            response = await client.get(url, params=params, headers={"accept": "application/json"})
            response.raise_for_status()
            payload = response.json()
            if not isinstance(payload, dict):
                raise ValueError("Dexscreener search response was not an object")
            return payload

        if self._client is not None:
            payload = await _request(self._client)
        else:
            async with httpx.AsyncClient(timeout=12) as client:
                payload = await _request(client)
        pairs = payload.get("pairs") if isinstance(payload.get("pairs"), list) else []
        matches = [pair for pair in pairs if isinstance(pair, dict) and self._pair_matches(row, pair)]
        if not matches:
            raise ValueError(f"Dexscreener returned no current pair metadata for {self.venue_id} {row.get('symbol')}")
        matches.sort(
            key=lambda pair: _float_value((pair.get("liquidity") or {}).get("usd") if isinstance(pair.get("liquidity"), dict) else None)
            or 0,
            reverse=True,
        )
        return matches[0]

    @staticmethod
    def _price(pair: dict[str, Any]) -> float:
        price = _float_value(pair.get("priceUsd"))
        if price is None or price <= 0:
            raise ValueError("pair metadata did not include positive priceUsd")
        return price

    @staticmethod
    def _liquidity_usd(pair: dict[str, Any], row: dict[str, Any]) -> float:
        liquidity = pair.get("liquidity") if isinstance(pair.get("liquidity"), dict) else {}
        return _float_value(liquidity.get("usd")) or _float_value(row.get("liquidity_usd")) or 1_000.0

    @staticmethod
    def _synthetic_levels(price: float, liquidity_usd: float, side: str, depth: int) -> list[dict[str, float]]:
        usable = max(500.0, min(liquidity_usd * 0.01, 100_000.0))
        count = max(1, min(depth, 6))
        per_level = usable / count
        levels: list[dict[str, float]] = []
        for index in range(count):
            impact_bps = 5 + index * 10
            level_price = price * (1 + impact_bps / 10_000) if side == "buy" else price * (1 - impact_bps / 10_000)
            levels.append({"price": level_price, "notional_usd": per_level})
        return levels

    def _metadata(
        self,
        *,
        row: dict[str, Any],
        pair: dict[str, Any],
        block_number: int | None,
        block_source: str | None,
    ) -> dict[str, Any]:
        return {
            "endpoint": "Dexscreener /latest/dex/search plus EVM eth_blockNumber",
            "pool_allowlist_path": str(self.pool_path),
            "pool_id": row.get("pool_id"),
            "pool_address": row.get("pool_address"),
            "chain": row.get("chain"),
            "dex_id": pair.get("dexId"),
            "pair_address": pair.get("pairAddress"),
            "block_number": block_number,
            "rpc_source": block_source,
            "liquidity_usd": self._liquidity_usd(pair, row),
            "volume_h24": (pair.get("volume") or {}).get("h24") if isinstance(pair.get("volume"), dict) else None,
            "raw_payload": {
                "pair": pair,
                "block_number": block_number,
                "rpc_source": block_source,
            },
            "raw_payload_ref": str(self.pool_path),
            "depth_semantics": "synthetic_from_pair_metadata_liquidity_not_protocol_invariant_replay",
            "production_gate": "candidate_only_pending_protocol_pool_math_depth_manipulation_benchmark_rights",
        }

    async def fetch_bidask(self, symbol: str) -> dict[str, Any]:
        row = self.resolve_symbol(symbol)
        pair = await self._fetch_pair(row)
        block_number, block_source = await self._block_number(str(row.get("chain") or "").lower())
        price = self._price(pair)
        spread_bps = 10.0
        return {
            "symbol": str(row.get("symbol") or symbol),
            "venue": self.venue_id,
            "asset_class": EVMPoolStateAdapter._asset_class(row),
            "source_type": self.source_type,
            "bid": price * (1 - spread_bps / 20_000),
            "ask": price * (1 + spread_bps / 20_000),
            "timestamp": _iso_now(),
            "metadata": self._metadata(row=row, pair=pair, block_number=block_number, block_source=block_source),
        }

    async def fetch_order_book(
        self,
        symbol: str,
        *,
        side: str = "buy",
        depth: int = 100,
    ) -> dict[str, Any]:
        row = self.resolve_symbol(symbol)
        clean_side = side.strip().lower()
        if clean_side not in {"buy", "sell"}:
            raise ValueError("side must be buy or sell")
        pair = await self._fetch_pair(row)
        block_number, block_source = await self._block_number(str(row.get("chain") or "").lower())
        price = self._price(pair)
        liquidity = self._liquidity_usd(pair, row)
        return {
            "symbol": str(row.get("symbol") or symbol),
            "venue": self.venue_id,
            "asset_class": EVMPoolStateAdapter._asset_class(row),
            "source_type": self.source_type,
            "side": clean_side,
            "levels": self._synthetic_levels(price, liquidity, clean_side, depth),
            "timestamp": _iso_now(),
            "metadata": self._metadata(row=row, pair=pair, block_number=block_number, block_source=block_source),
        }


class JupiterRouteLabelAdapter(JupiterRouterAdapter):
    """Jupiter quote adapter that requires route plans to include venue labels."""

    def __init__(
        self,
        *,
        venue_id: str,
        route_labels: tuple[str, ...],
        base_url: str = "https://api.jup.ag",
        api_key: str | None = None,
        token_mints: dict[str, dict[str, Any]] | None = None,
        blocked_tokens: dict[str, str] | None = None,
        client: httpx.AsyncClient | None = None,
        slippage_bps: int = 50,
    ) -> None:
        self.venue_id = venue_id
        self.route_labels = tuple(label.lower() for label in route_labels)
        super().__init__(
            base_url=base_url,
            api_key=api_key,
            token_mints=token_mints,
            blocked_tokens=blocked_tokens,
            client=client,
            slippage_bps=slippage_bps,
        )

    def metadata(self) -> dict[str, Any]:
        metadata = super().metadata()
        metadata.update(
            {
                "venue_id": self.venue_id,
                "source_type": "jupiter_route_filtered_pool_quote",
                "implementation": "src.rwa_adapters.JupiterRouteLabelAdapter",
                "notes": [
                    "Uses Jupiter executable quote snapshots and requires the routePlan to include the configured venue label.",
                    "This is a route-filtered executable quote, not direct pool-state replay.",
                    f"Required route labels: {', '.join(self.route_labels)}.",
                ],
            }
        )
        return metadata

    @staticmethod
    def _labels_in_plan(route_plan: Any) -> list[str]:
        labels: list[str] = []
        for item in _iter_dicts(route_plan):
            for key in ("label", "ammName", "name"):
                value = item.get(key)
                if value not in {None, ""}:
                    labels.append(str(value))
        return labels

    def _plan_matches(self, route_plan: Any) -> bool:
        labels = self._labels_in_plan(route_plan)
        lowered = [label.lower() for label in labels]
        return any(required in label for required in self.route_labels for label in lowered)

    @staticmethod
    def _route_plans(metadata: dict[str, Any]) -> list[Any]:
        plans: list[Any] = []
        if isinstance(metadata.get("route_plan"), list):
            plans.append(metadata["route_plan"])
        for key in ("bid_quote", "ask_quote"):
            quote = metadata.get(key)
            if isinstance(quote, dict) and isinstance(quote.get("route_plan"), list):
                plans.append(quote["route_plan"])
        for sweep_quote in metadata.get("sweep_quotes") or []:
            if isinstance(sweep_quote, dict) and isinstance(sweep_quote.get("route_plan"), list):
                plans.append(sweep_quote["route_plan"])
        return plans

    def _assert_route_filter(self, observation: dict[str, Any]) -> None:
        metadata = observation.get("metadata") if isinstance(observation.get("metadata"), dict) else {}
        plans = self._route_plans(metadata)
        if not plans:
            raise ValueError(f"Jupiter routePlan missing; cannot verify {self.venue_id} route source")
        missing = [self._labels_in_plan(plan) for plan in plans if not self._plan_matches(plan)]
        if missing:
            raise RWAAdapterBlockedError(
                "route_label_mismatch",
                f"Jupiter quote did not route through {self.venue_id}; "
                f"required_labels={self.route_labels}; observed_labels={missing[:3]}"
            )

    def _tag_observation(self, observation: dict[str, Any]) -> dict[str, Any]:
        self._assert_route_filter(observation)
        metadata = observation.get("metadata") if isinstance(observation.get("metadata"), dict) else {}
        return {
            **observation,
            "venue": self.venue_id,
            "source_type": "jupiter_route_filtered_pool_quote",
            "metadata": {
                **metadata,
                "route_label_filter": list(self.route_labels),
                "route_filter_semantics": "all quoted route plans must include at least one required label",
            },
        }

    async def fetch_bidask(self, symbol: str) -> dict[str, Any]:
        return self._tag_observation(await super().fetch_bidask(symbol))

    async def fetch_order_book(
        self,
        symbol: str,
        *,
        side: str = "buy",
        depth: int = 100,
    ) -> dict[str, Any]:
        return self._tag_observation(await super().fetch_order_book(symbol, side=side, depth=depth))


class RWAAdapterRegistry:
    """Holds live and planned venue adapters behind one query surface."""

    def __init__(self) -> None:
        self._adapters: dict[str, RWAFeedAdapter] = {}

    def register(self, adapter: RWAFeedAdapter) -> None:
        self._adapters[adapter.venue_id] = adapter

    def get(self, venue_id: str) -> RWAFeedAdapter:
        clean = venue_id.strip().lower()
        if clean not in self._adapters:
            raise KeyError(f"Unknown RWA venue: {venue_id}")
        return self._adapters[clean]

    def list_metadata(self) -> list[dict[str, Any]]:
        return sorted(
            (adapter.metadata() for adapter in self._adapters.values()),
            key=lambda item: str(item["venue_id"]),
        )


def build_default_registry() -> RWAAdapterRegistry:
    """Build the default registry with implemented and planned adapters."""
    registry = RWAAdapterRegistry()
    jupiter_token_mints = _load_jupiter_token_mints()
    jupiter_blocked_tokens = _load_jupiter_blocked_tokens()
    registry.register(KrakenXStocksAdapter())
    registry.register(KrakenSpotAdapter())
    registry.register(RevolutXAdapter())
    registry.register(XStocksPublicPriceAdapter())
    registry.register(HyperliquidPAXGAdapter())
    registry.register(HyperliquidSpotRWAAdapter())
    registry.register(HyperliquidTradeableAdapter(venue_id=HYPERLIQUID_PERPS_VENUE_ID, market_type="perp"))
    registry.register(HyperliquidTradeableAdapter(venue_id=HYPERLIQUID_SPOT_VENUE_ID, market_type="spot"))
    registry.register(AsterOrderBookAdapter())
    registry.register(AevoOrderBookAdapter())
    registry.register(ApeXOmniOrderBookAdapter())
    registry.register(LighterOrderBookAdapter())
    registry.register(DydxOrderBookAdapter())
    registry.register(OrderlyReferencePriceAdapter())
    registry.register(DeriveReferencePriceAdapter())
    registry.register(GainsPriceStreamAdapter())
    registry.register(OstiumBuilderPriceAdapter())
    registry.register(EVMPoolStateAdapter(venue_id="uniswap_v3_v4"))
    registry.register(EVMPoolStateAdapter(venue_id="aerodrome_slipstream"))
    registry.register(EVMPairMetadataAdapter(venue_id="balancer_pools"))
    registry.register(EVMPairMetadataAdapter(venue_id="curve_stableswap"))
    registry.register(JupiterRouterAdapter(token_mints=jupiter_token_mints, blocked_tokens=jupiter_blocked_tokens))
    registry.register(
        JupiterRouteLabelAdapter(
            venue_id="raydium_clmm",
            route_labels=("Raydium",),
            token_mints=jupiter_token_mints,
            blocked_tokens=jupiter_blocked_tokens,
        )
    )
    registry.register(
        JupiterRouteLabelAdapter(
            venue_id="orca_whirlpool",
            route_labels=("Orca", "Whirlpool"),
            token_mints=jupiter_token_mints,
            blocked_tokens=jupiter_blocked_tokens,
        )
    )
    registry.register(
        JupiterRouteLabelAdapter(
            venue_id="meteora_dlmm",
            route_labels=("Meteora",),
            token_mints=jupiter_token_mints,
            blocked_tokens=jupiter_blocked_tokens,
        )
    )
    for venue in VENUES:
        venue_id = str(venue["id"])
        if venue_id in {
            KrakenXStocksAdapter.venue_id,
            KrakenSpotAdapter.venue_id,
            RevolutXAdapter.venue_id,
            XStocksPublicPriceAdapter.venue_id,
            HyperliquidPAXGAdapter.venue_id,
            HyperliquidSpotRWAAdapter.venue_id,
            HYPERLIQUID_PERPS_VENUE_ID,
            HYPERLIQUID_SPOT_VENUE_ID,
            AsterOrderBookAdapter.venue_id,
            AevoOrderBookAdapter.venue_id,
            ApeXOmniOrderBookAdapter.venue_id,
            LighterOrderBookAdapter.venue_id,
            DydxOrderBookAdapter.venue_id,
            OrderlyReferencePriceAdapter.venue_id,
            DeriveReferencePriceAdapter.venue_id,
            GainsPriceStreamAdapter.venue_id,
            OstiumBuilderPriceAdapter.venue_id,
            "uniswap_v3_v4",
            "aerodrome_slipstream",
            "balancer_pools",
            "curve_stableswap",
            JupiterRouterAdapter.venue_id,
            "raydium_clmm",
            "orca_whirlpool",
            "meteora_dlmm",
        }:
            continue
        source_tier = str(venue["source_tier"])
        data = set(venue.get("data") or [])
        blocked_spec = P0_BLOCKED_ADAPTER_SPECS.get(venue_id, {})
        registry.register(
            StaticCapabilityAdapter(
                AdapterCapability(
                    venue_id=venue_id,
                    adapter_type="planned",
                    status=str(blocked_spec.get("status") or "planned"),
                    source_type=source_tier,
                    supports_bidask=bool(
                        {
                            "bid",
                            "mid",
                            "ask",
                            "l1_bid_ask",
                            "quote",
                            "nbbo",
                            "real_time_quotes",
                            "market_depth",
                        }
                        & data
                    ),
                    supports_l2_vwap=source_tier in {
                        "native_l2",
                        "synthetic_depth",
                        "quote_sweep",
                        "quote_stream",
                        "onchain_clmm_pool",
                        "onchain_stableswap_pool",
                        "licensed_consolidated_tape",
                        "licensed_exchange_feed",
                    },
                    supports_trade_vwap=bool(
                        {"trades", "tick_trades", "recent_trades", "fills", "real_time_trades"} & data
                    ),
                    requires_auth=bool(venue.get("requires_auth")) or venue_id in {
                        "jupiter_xstocks",
                        "jupiter_router",
                        "treasury_nav",
                        "ondo_stocks",
                        "uniswap_v3_v4",
                        "balancer_pools",
                        "polygon_tradfi_reference",
                        "us_equity_consolidated_tape",
                        "hkex_licensed_equities",
                        "china_a_share_licensed_equities",
                        "krx_licensed_equities",
                        "jpx_licensed_equities",
                        "twse_licensed_equities",
                        "india_nse_bse_licensed_equities",
                        "lse_lseg_licensed_equities",
                        "euronext_licensed_equities",
                        "deutsche_boerse_xetra_licensed_equities",
                        "tsx_licensed_equities",
                        "asx_licensed_equities",
                        "sgx_licensed_equities",
                        "pyth_oracle_reference",
                        "chainlink_oracle_reference",
                    },
                    implementation="planned_adapter",
                    notes=[
                        *(str(note) for note in blocked_spec.get("notes", [])),
                        str(venue.get("vwap_method") or ""),
                        str(venue.get("bidask_method") or ""),
                        str(venue.get("legal_note") or ""),
                    ],
                )
            )
        )
    return registry


RWA_ADAPTER_REGISTRY = build_default_registry()
