"""Build non-crypto RWA VWAP and bid/ask feed definitions."""

from __future__ import annotations

from collections import Counter
from typing import Any

from src.rwa_blocksize_benchmark import resolve_blocksize_benchmark
from src.rwa_coverage import (
    build_rwa_asset_matrix,
    iter_asset_venue_instruments,
)


NON_CRYPTO_ASSET_CLASSES = {
    "equity",
    "etf",
    "index",
    "fx",
    "commodity",
    "metal",
    "treasury",
    "treasury_fund",
    "tokenized_fund",
}
TOKENIZED_STOCK_VENUES = {
    "kraken_xstocks",
    "jupiter_xstocks",
    "bybit_xstocks",
    "ondo_stocks",
    "hyperliquid_spot",
    "hyperliquid_rwa_spot",
}
TOKENIZED_STOCK_SOURCE_TYPES = {"quote_sweep", "onchain_clmm_pool"}


def _base_symbol(symbol: str) -> str:
    return symbol.replace("-", "/").split("/", 1)[0].upper()


def _is_tokenized_stock_row(asset_class: str, symbol: str, venue: str, source_type: str) -> bool:
    if asset_class not in {"equity", "etf"}:
        return False
    raw_base = symbol.replace("-", "/").split("/", 1)[0]
    if raw_base.endswith("x") and len(raw_base) > 1:
        return True
    if venue in TOKENIZED_STOCK_VENUES:
        return True
    return source_type in TOKENIZED_STOCK_SOURCE_TYPES and asset_class in {"equity", "etf"}


def _supports_vwap(venue_data: dict[str, Any]) -> bool:
    value = str(venue_data.get("vwap_support") or "").lower()
    status = str(venue_data.get("coverage_status") or "").lower()
    return bool(
        value
        and "not_" not in value
        and value != "unsupported"
        and not value.startswith("requires_")
        and "rejected" not in status
    )


def _supports_bidask(venue_data: dict[str, Any]) -> bool:
    value = str(venue_data.get("bidask_support") or "").lower()
    status = str(venue_data.get("coverage_status") or "").lower()
    return bool(
        value
        and "not_" not in value
        and value != "unsupported"
        and not value.startswith("requires_")
        and "rejected" not in status
    )


def _benchmark_status(benchmark: dict[str, str]) -> str:
    service = benchmark.get("service")
    if service in {"bidask", "fx", "metal"}:
        return "ready_for_blocksize_benchmark"
    if service == "state":
        return "requires_blocksize_state_instrument_check"
    return "requires_blocksize_instrument_check"


def _feed_record(
    *,
    kind: str,
    asset: dict[str, Any],
    venue_id: str,
    venue_data: dict[str, Any],
    venue_instrument_count: int,
) -> dict[str, Any]:
    symbol = str(venue_data["symbol"])
    source_type = str(venue_data["source_type"])
    asset_class = str(asset["asset_classes"][0]) if asset.get("asset_classes") else "unknown"
    benchmark = resolve_blocksize_benchmark(
        {
            "symbol": symbol,
            "asset_class": asset_class,
            "venue": venue_id,
            "source_type": source_type,
        }
    )
    legacy_feed_id = f"rwa_{kind}:{venue_id}:{asset['asset_id']}:{source_type}"
    instrument_id = str(venue_data.get("instrument_id") or "")
    feed_id = (
        f"{legacy_feed_id}:{instrument_id.rsplit(':', 1)[-1]}"
        if venue_instrument_count > 1 and instrument_id
        else legacy_feed_id
    )
    record = {
        "feed_id": feed_id,
        "kind": kind,
        "asset_id": asset["asset_id"],
        "asset_classes": asset["asset_classes"],
        "asset_class": asset_class,
        "instrument_id": instrument_id or None,
        "symbol": symbol,
        "venue": venue_id,
        "source_type": source_type,
        "coverage_status": venue_data["coverage_status"],
        "support": venue_data[f"{kind}_support"],
        "block_sizes_usd": asset["block_sizes_usd"],
        "endpoint_template": (
            f"/v1/rwa/{kind}/{{symbol}}?venue={venue_id}"
            if kind == "bidask"
            else f"/v1/rwa/{kind}/{{symbol}}?venue={venue_id}&block_size_usd={{block_size_usd}}"
        ),
        "blocksize_benchmark": {
            **benchmark,
            "status": _benchmark_status(benchmark),
            "comparison_endpoint": "/v1/rwa/benchmark/blocksize",
        },
        "comparison_rule": (
            "Fetch venue observation, normalize it, then compare through /v1/rwa/benchmark/blocksize before consolidation."
        ),
    }
    if venue_data.get("metadata"):
        record["metadata"] = venue_data["metadata"]
    return record


def build_non_crypto_feed_catalog(
    *,
    exclude_tokenized_stocks: bool = True,
    asset_class: str | None = None,
    venue: str | None = None,
    asset_matrix: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return all sourceable non-crypto VWAP and bid/ask feed definitions."""
    if asset_matrix is not None and (asset_class is not None or venue is not None):
        raise ValueError(
            "asset_matrix can only be supplied for an unfiltered feed catalog"
        )
    matrix = asset_matrix or build_rwa_asset_matrix(
        asset_class=asset_class,
        venue=venue,
    )
    vwap_feeds: list[dict[str, Any]] = []
    bidask_feeds: list[dict[str, Any]] = []
    excluded_rows: list[dict[str, Any]] = []

    for asset in matrix["assets"]:
        for venue_id, venue_data in iter_asset_venue_instruments(asset):
            row_asset_class = str(
                venue_data.get("asset_class")
                or (
                    asset["asset_classes"][0]
                    if asset.get("asset_classes")
                    else "unknown"
                )
            )
            if row_asset_class not in NON_CRYPTO_ASSET_CLASSES:
                continue
            source_type = str(venue_data["source_type"])
            venue_instrument_count = int(
                (asset["venues"].get(venue_id) or {}).get("instrument_count")
                or 1
            )
            tokenized_stock = _is_tokenized_stock_row(
                row_asset_class,
                str(venue_data["symbol"]),
                str(venue_id),
                source_type,
            )
            if exclude_tokenized_stocks and tokenized_stock:
                excluded_rows.append(
                    {
                        "asset_id": asset["asset_id"],
                        "symbol": venue_data["symbol"],
                        "venue": venue_id,
                        "asset_classes": asset["asset_classes"],
                        "reason": "tokenized_stock_excluded",
                    }
                )
                continue
            if _supports_vwap(venue_data):
                vwap_feeds.append(
                    _feed_record(
                        kind="vwap",
                        asset=asset,
                        venue_id=venue_id,
                        venue_data=venue_data,
                        venue_instrument_count=venue_instrument_count,
                    )
                )
            if _supports_bidask(venue_data):
                bidask_feeds.append(
                    _feed_record(
                        kind="bidask",
                        asset=asset,
                        venue_id=venue_id,
                        venue_data=venue_data,
                        venue_instrument_count=venue_instrument_count,
                    )
                )

    all_feeds = [*vwap_feeds, *bidask_feeds]
    by_asset_class = Counter(
        asset_class
        for feed in all_feeds
        for asset_class in feed["asset_classes"]
    )
    by_venue = Counter(feed["venue"] for feed in all_feeds)
    by_kind = Counter(feed["kind"] for feed in all_feeds)
    by_benchmark = Counter(feed["blocksize_benchmark"]["status"] for feed in all_feeds)
    return {
        "source_snapshot_manifest": matrix["source_snapshot_manifest"],
        "summary": {
            "feed_count": len(all_feeds),
            "vwap_feed_count": len(vwap_feeds),
            "bidask_feed_count": len(bidask_feeds),
            "asset_count": len({feed["asset_id"] for feed in all_feeds}),
            "excluded_tokenized_stock_rows": len(excluded_rows),
            "by_asset_class": dict(sorted(by_asset_class.items())),
            "by_venue": dict(sorted(by_venue.items())),
            "by_kind": dict(sorted(by_kind.items())),
            "by_blocksize_benchmark_status": dict(sorted(by_benchmark.items())),
        },
        "filters": {
            "exclude_tokenized_stocks": exclude_tokenized_stocks,
            "asset_class": asset_class or "all",
            "venue": venue or "all",
        },
        "vwap_feeds": sorted(vwap_feeds, key=lambda item: str(item["feed_id"])),
        "bidask_feeds": sorted(bidask_feeds, key=lambda item: str(item["feed_id"])),
        "excluded_rows": sorted(
            excluded_rows,
            key=lambda item: (str(item["venue"]), str(item["asset_id"]), str(item["symbol"])),
        ),
        "comparison_workflow": [
            "Resolve feed symbol and venue through /v1/rwa/resolve.",
            "Fetch or receive the venue observation through the adapter.",
            "Normalize as bid/ask, VWAP, quote-sweep, pool-state, mark, or NAV according to source_type.",
            "Run /v1/rwa/realtime/quality.",
            "Run /v1/rwa/benchmark/blocksize when blocksize_benchmark.status is ready_for_blocksize_benchmark.",
            "Persist the raw and normalized observation through /v1/rwa/observations/store.",
        ],
    }
