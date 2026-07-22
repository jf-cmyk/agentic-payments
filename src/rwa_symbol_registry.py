"""Canonical RWA asset and venue registry with alias resolution."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any

from src.rwa_coverage import VENUES, build_rwa_asset_matrix


_NON_ALNUM = re.compile(r"[^A-Z0-9]+")


def _compact(value: str) -> str:
    return _NON_ALNUM.sub("", value.upper())


def _split_symbol(symbol: str) -> tuple[str, str | None]:
    raw = symbol.strip().upper().replace("-", "/").replace(" ", "")
    if "/" in raw:
        base, quote = raw.split("/", 1)
        return base, quote or None
    for quote in ("USDC", "USDT", "USD", "EUR", "GBP", "JPY", "CAD", "CHF", "CNH", "MXN", "KRW"):
        if raw.endswith(quote) and len(raw) > len(quote):
            return raw[: -len(quote)], quote
    return raw, None


def normalize_symbol_alias(symbol: str) -> str:
    """Normalize caller symbol text into a venue-independent alias key."""
    base, quote = _split_symbol(symbol)
    base = base.replace("_1", "")
    if base.endswith("X") and len(base) > 1:
        base = base[:-1]
    return f"{_compact(base)}{_compact(quote or '')}"


def normalize_venue_alias(venue: str) -> str:
    """Normalize caller venue text into a comparable alias key."""
    return _compact(venue.replace("_", " "))


def _symbol_aliases(symbol: str, asset_id: str) -> set[str]:
    aliases = {normalize_symbol_alias(symbol), normalize_symbol_alias(asset_id)}
    base, quote = _split_symbol(symbol)
    base = base.replace("_1", "")
    stripped_base = base[:-1] if base.endswith("X") and len(base) > 1 else base
    for candidate_base in {base, stripped_base, asset_id, f"{asset_id}x"}:
        for candidate_quote in {quote, "USD", "USDC", None}:
            if candidate_quote:
                aliases.add(normalize_symbol_alias(f"{candidate_base}/{candidate_quote}"))
                aliases.add(normalize_symbol_alias(f"{candidate_base}{candidate_quote}"))
            aliases.add(normalize_symbol_alias(candidate_base))
    return {alias for alias in aliases if alias}


def _venue_aliases(venue: dict[str, Any]) -> set[str]:
    venue_id = str(venue["id"])
    name = str(venue.get("name") or venue_id)
    aliases = {
        normalize_venue_alias(venue_id),
        normalize_venue_alias(name),
        normalize_venue_alias(venue_id.replace("_", " ")),
    }
    first_word = name.split()[0] if name.split() else ""
    if first_word:
        aliases.add(normalize_venue_alias(first_word))
    if venue_id.endswith("_pools"):
        aliases.add(normalize_venue_alias(venue_id.removesuffix("_pools")))
    if venue_id.endswith("_clmm"):
        aliases.add(normalize_venue_alias(venue_id.removesuffix("_clmm")))
    if venue_id.endswith("_router"):
        aliases.add(normalize_venue_alias(venue_id.removesuffix("_router")))
    if venue_id == "uniswap_v3_v4":
        aliases.update({normalize_venue_alias("uniswap"), normalize_venue_alias("uniswap v3"), normalize_venue_alias("uniswap v4")})
    if venue_id == "curve_stableswap":
        aliases.add(normalize_venue_alias("curve"))
    if venue_id == "aerodrome_slipstream":
        aliases.add(normalize_venue_alias("aerodrome"))
    return {alias for alias in aliases if alias}


def build_rwa_symbol_registry() -> dict[str, Any]:
    """Build canonical assets plus alias indexes from current coverage."""
    matrix = build_rwa_asset_matrix()
    assets: list[dict[str, Any]] = []
    alias_index: dict[str, set[str]] = defaultdict(set)
    for asset in matrix["assets"]:
        asset_id = str(asset["asset_id"])
        aliases: set[str] = set()
        for symbol in asset["symbols"]:
            aliases.update(_symbol_aliases(str(symbol), asset_id))
        for alias in aliases:
            alias_index[alias].add(asset_id)
        assets.append(
            {
                "asset_id": asset_id,
                "asset_classes": asset["asset_classes"],
                "canonical_symbols": asset["symbols"],
                "aliases": sorted(aliases),
                "venue_count": asset["venue_count"],
                "venues": asset["venues"],
                "source_types": asset["source_types"],
                "executable_venues": asset["executable_venues"],
                "reference_venues": asset["reference_venues"],
                "sourcing_status": asset["sourcing_status"],
                "block_sizes_usd": asset["block_sizes_usd"],
            }
        )
    return {
        "summary": {
            "asset_count": len(assets),
            "alias_count": len(alias_index),
            "coverage_rows": matrix["summary"]["coverage_rows"],
            "registry_venue_count": matrix["summary"]["registry_venue_count"],
        },
        "assets": assets,
        "alias_index": {key: sorted(value) for key, value in sorted(alias_index.items())},
    }


def build_rwa_venue_registry() -> dict[str, Any]:
    """Build venue capabilities plus alias indexes and covered assets."""
    matrix = build_rwa_asset_matrix()
    venue_rows: dict[str, dict[str, Any]] = {}
    for venue in VENUES:
        venue_id = str(venue["id"])
        venue_rows[venue_id] = {
            "venue_id": venue_id,
            "name": venue["name"],
            "aliases": sorted(_venue_aliases(venue)),
            "status": venue["status"],
            "instrument_type": venue["instrument_type"],
            "source_tier": venue["source_tier"],
            "coverage_mode": venue["coverage_mode"],
            "data": venue["data"],
            "vwap_method": venue["vwap_method"],
            "bidask_method": venue["bidask_method"],
            "legal_note": venue["legal_note"],
            "asset_count": 0,
            "asset_classes": set(),
            "source_types": set(),
            "symbols": [],
            "assets": [],
        }
    for asset in matrix["assets"]:
        for venue_id, venue_data in asset["venues"].items():
            if venue_id not in venue_rows:
                continue
            row = venue_rows[venue_id]
            row["asset_classes"].update(asset["asset_classes"])
            row["source_types"].add(venue_data["source_type"])
            row["symbols"].append(venue_data["symbol"])
            venue_asset = {
                "asset_id": asset["asset_id"],
                "symbol": venue_data["symbol"],
                "asset_classes": asset["asset_classes"],
                "source_type": venue_data["source_type"],
                "coverage_status": venue_data["coverage_status"],
                "vwap_support": venue_data["vwap_support"],
                "bidask_support": venue_data["bidask_support"],
            }
            if venue_data.get("metadata"):
                venue_asset["metadata"] = venue_data["metadata"]
            row["assets"].append(venue_asset)
    venue_alias_index: dict[str, str] = {}
    for row in venue_rows.values():
        row["asset_count"] = len({asset["asset_id"] for asset in row["assets"]})
        row["asset_classes"] = sorted(row["asset_classes"])
        row["source_types"] = sorted(row["source_types"])
        row["symbols"] = sorted(set(row["symbols"]))
        row["assets"].sort(key=lambda item: (str(item["asset_id"]), str(item["symbol"])))
        for alias in row["aliases"]:
            venue_alias_index[alias] = row["venue_id"]
    return {
        "summary": {
            "venue_count": len(venue_rows),
            "venue_alias_count": len(venue_alias_index),
            "venues_with_assets": len([row for row in venue_rows.values() if row["asset_count"] > 0]),
        },
        "venues": sorted(venue_rows.values(), key=lambda item: str(item["venue_id"])),
        "venue_alias_index": dict(sorted(venue_alias_index.items())),
    }


def resolve_rwa_symbol(symbol: str, *, venue: str | None = None) -> dict[str, Any]:
    """Resolve a caller symbol and optional venue into canonical coverage rows."""
    symbol_key = normalize_symbol_alias(symbol)
    asset_registry = build_rwa_symbol_registry()
    venue_registry = build_rwa_venue_registry()
    candidate_ids = asset_registry["alias_index"].get(symbol_key, [])
    assets_by_id = {asset["asset_id"]: asset for asset in asset_registry["assets"]}
    venue_id = None
    if venue:
        venue_key = normalize_venue_alias(venue)
        venue_id = venue_registry["venue_alias_index"].get(venue_key)
        if venue_id is None:
            raise ValueError(f"Unsupported venue: {venue}")
    matches = []
    for asset_id in candidate_ids:
        asset = assets_by_id[asset_id]
        if venue_id and venue_id not in asset["venues"]:
            continue
        if venue_id:
            asset = dict(asset)
            venue_data = asset["venues"][venue_id]
            asset["venues"] = {venue_id: venue_data}
            asset["venue_count"] = 1
            asset["source_types"] = [venue_data["source_type"]]
            executable = venue_id in set(asset.get("executable_venues") or [])
            reference = venue_id in set(asset.get("reference_venues") or [])
            asset["executable_venues"] = [venue_id] if executable else []
            asset["reference_venues"] = [venue_id] if reference else []
        matches.append(asset)
    return {
        "query": {"symbol": symbol, "normalized_symbol_alias": symbol_key, "venue": venue, "resolved_venue": venue_id},
        "match_count": len(matches),
        "ambiguous": len(matches) > 1,
        "matches": matches,
        "known_venue": venue_registry["venue_alias_index"].get(normalize_venue_alias(venue or "")) if venue else None,
    }


def build_rwa_registry_overview(
    *,
    symbol: str | None = None,
    venue: str | None = None,
    include_aliases: bool = False,
) -> dict[str, Any]:
    """Return canonical asset and venue coverage in one service response."""
    asset_registry = build_rwa_symbol_registry()
    venue_registry = build_rwa_venue_registry()
    assets = asset_registry["assets"]
    venues = venue_registry["venues"]
    resolution = None
    if symbol:
        resolution = resolve_rwa_symbol(symbol, venue=venue)
        assets = resolution["matches"]
    elif venue:
        venue_key = normalize_venue_alias(venue)
        venue_id = venue_registry["venue_alias_index"].get(venue_key)
        if venue_id is None:
            raise ValueError(f"Unsupported venue: {venue}")
        assets = [asset for asset in assets if venue_id in asset["venues"]]
        venues = [row for row in venues if row["venue_id"] == venue_id]
    if not include_aliases:
        for asset in assets:
            asset.pop("aliases", None)
        for venue_row in venues:
            venue_row.pop("aliases", None)
    return {
        "summary": {
            **asset_registry["summary"],
            "returned_assets": len(assets),
            "returned_venues": len(venues),
        },
        "resolution": resolution,
        "assets": assets,
        "venues": venues,
    }
