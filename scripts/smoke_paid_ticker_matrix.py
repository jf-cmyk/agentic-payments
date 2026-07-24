from __future__ import annotations

import json
import random
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from src.credit_manager import CreditManager
from src.models import (
    BidAskData,
    FXData,
    MetalData,
    StatePriceData,
    VWAP24HrData,
    VWAP30MinData,
    VWAPData,
)
from src.observability import UsageEventStore, configure_global_store
from src.resource_server import app


SEED = 402
SAMPLE_SIZE = 10
NOW = datetime(2026, 6, 25, 12, 0, tzinfo=UTC)

CRYPTO_CANDIDATES = [
    "BTCUSD",
    "ETHUSD",
    "SOLUSD",
    "LINKUSD",
    "AAVEUSD",
    "JUPUSD",
    "PYTHUSD",
    "AVAXUSD",
    "UNIUSD",
    "DOGEUSD",
    "BNBUSD",
    "XRPUSD",
    "ADAUSD",
    "LTCUSD",
    "ATOMUSD",
    "NEARUSD",
]
STATE_CANDIDATES = [
    "MSOLUSD",
    "JUPSOLUSD",
    "WSTETHETH",
    "WSTETHUSD",
    "JITOSOLUSD",
    "BSOLUSD",
    "INFUSD",
    "JLPUSD",
    "RETHETH",
    "CBETHETH",
    "EZETHETH",
    "WBTCBTC",
]
EQUITY_CANDIDATES = [
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "GOOGL",
    "META",
    "TSLA",
    "AMD",
    "NFLX",
    "COIN",
    "PLTR",
    "AVGO",
]
FX_CANDIDATES = [
    "EURUSD",
    "GBPUSD",
    "USDJPY",
    "USDCHF",
    "AUDUSD",
    "USDCAD",
    "NZDUSD",
    "EURGBP",
    "EURJPY",
    "GBPJPY",
    "USDMXN",
    "USDZAR",
]
SUPPORTED_NON_GOLD_METALS = ["XAGUSD", "XPTUSD", "XPDUSD", "COPPERUSD"]


def _sample(candidates: list[str], *, size: int = SAMPLE_SIZE) -> list[str]:
    rng = random.Random(SEED + len(candidates))
    if len(candidates) <= size:
        return list(candidates)
    return rng.sample(candidates, size)


def _base_quote(symbol: str) -> tuple[str, str]:
    for quote in ("USDT", "USDC", "USD", "EUR", "GBP", "JPY", "BTC", "ETH"):
        if symbol.endswith(quote) and len(symbol) > len(quote):
            return symbol[: -len(quote)], quote
    return symbol[:3], symbol[3:] or "USD"


def _mock_blocksize_client() -> AsyncMock:
    mock = AsyncMock()

    async def vwap(pair: str) -> VWAPData:
        return VWAPData(pair=pair, vwap=100.0, volume=12345.0, timestamp=NOW, currency="USD")

    async def bidask(pair: str) -> BidAskData:
        return BidAskData(
            pair=pair,
            bid=99.5,
            ask=100.5,
            spread=1.0,
            spread_pct=1.0,
            timestamp=NOW,
        )

    async def state(pair: str) -> StatePriceData:
        return StatePriceData(pair=pair, price=100.0, timestamp=NOW)

    async def vwap30m(pair: str) -> VWAP30MinData:
        base, quote = _base_quote(pair)
        return VWAP30MinData(ticker=base, vwap=100.0, quote_currency=quote, timestamp=NOW)

    async def vwap24h(pair: str) -> VWAP24HrData:
        return VWAP24HrData(pair=pair, vwap=100.0, volume=12345.0, timestamp=NOW)

    async def fx(pair: str) -> FXData:
        base, quote = _base_quote(pair)
        return FXData(
            pair=pair,
            base_currency=base,
            quote_currency=quote,
            bid=1.1,
            ask=1.2,
            mid=1.15,
            timestamp=NOW,
        )

    async def metal(ticker: str) -> MetalData:
        names = {
            "XAGUSD": "Silver",
            "XPTUSD": "Platinum",
            "XPDUSD": "Palladium",
            "COPPERUSD": "Copper",
        }
        return MetalData(ticker=ticker, name=names.get(ticker, ticker), price=100.0, timestamp=NOW)

    mock.get_vwap_latest = AsyncMock(side_effect=vwap)
    mock.get_bidask_snapshot = AsyncMock(side_effect=bidask)
    mock.get_state_price = AsyncMock(side_effect=state)
    mock.get_vwap_30min = AsyncMock(side_effect=vwap30m)
    mock.get_vwap_30min_trades = AsyncMock(return_value=[])
    mock.get_vwap_24hr = AsyncMock(side_effect=vwap24h)
    mock.get_fx_rate = AsyncMock(side_effect=fx)
    mock.get_metal_price = AsyncMock(side_effect=metal)
    return mock


def _paths() -> dict[str, list[str]]:
    crypto = _sample(CRYPTO_CANDIDATES)
    return {
        "priority_bidask_btc_usd": ["/v1/bidask/BTC-USD"],
        "vwap": [f"/v1/vwap/{symbol}" for symbol in crypto],
        "bidask": [f"/v1/bidask/{symbol}" for symbol in _sample(EQUITY_CANDIDATES)],
        "state": [f"/v1/state/{symbol}" for symbol in _sample(STATE_CANDIDATES)],
        "vwap30m": [f"/v1/vwap30m/{symbol}" for symbol in crypto],
        "vwap24h": [f"/v1/vwap24h/{symbol}" for symbol in crypto],
        "fx": [f"/v1/fx/{symbol}" for symbol in _sample(FX_CANDIDATES)],
        "metal": [f"/v1/metal/{symbol}" for symbol in _sample(SUPPORTED_NON_GOLD_METALS)],
    }


def main() -> int:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = UsageEventStore(Path(tmpdir) / "usage_events.db")
        configure_global_store(store)

        with patch("src.resource_server.OBSERVABILITY", store), patch(
            "src.resource_server._verify_payment",
            new_callable=AsyncMock,
            return_value={"valid": True, "network": "solana", "mock": True},
        ), patch(
            "src.resource_server._settle_payment",
            new_callable=AsyncMock,
            return_value={"success": True},
        ), TestClient(app) as client:
            app.state.blocksize = _mock_blocksize_client()
            app.state.credits = CreditManager(str(Path(tmpdir) / "credits.db"))
            if hasattr(app.state, "stream_cache"):
                app.state.stream_cache.enabled = False

            results: dict[str, list[dict[str, object]]] = {}
            for category, paths in _paths().items():
                category_results = []
                for path in paths:
                    response = client.get(path, headers={"X-PAYMENT": "mock-paid-proof"})
                    payload = response.json()
                    category_results.append(
                        {
                            "path": path,
                            "status_code": response.status_code,
                            "delivered": response.status_code == 200
                            and payload.get("status") == "ok",
                        }
                    )
                results[category] = category_results

            stats = store.summarize(days=1)

        configure_global_store(None)

    failures = [
        row
        for rows in results.values()
        for row in rows
        if not row["delivered"]
    ]
    output = {
        "seed": SEED,
        "sample_size_target": SAMPLE_SIZE,
        "metal_note": (
            "Gold/XAUUSD intentionally excluded; only four supported non-gold metal "
            "tickers exist in the local catalog."
        ),
        "categories": {
            category: {
                "checked": len(rows),
                "delivered": sum(1 for row in rows if row["delivered"]),
                "paths": rows,
            }
            for category, rows in results.items()
        },
        "observability": {
            "data_delivered": stats["event_counts"].get("data_delivered", 0),
            "charged_delivery_failed": stats["event_counts"].get("charged_delivery_failed", 0),
            "paid_calls": stats["overview"]["paid_calls"],
        },
        "status": "pass" if not failures else "fail",
    }
    print(json.dumps(output, indent=2))
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
