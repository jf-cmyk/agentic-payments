"""Canonical RWA asset and venue registry with alias resolution."""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from copy import deepcopy
from typing import Any

from src.rwa_coverage import (
    RWA_ASSET_MATRIX_DEFAULT_LIMIT,
    RWA_COLLECTION_DEFAULT_LIMIT,
    RWA_COLLECTION_MAX_LIMIT,
    VENUES,
    build_rwa_asset_matrix,
    iter_asset_venue_instruments,
)


_NON_ALNUM = re.compile(r"[^A-Z0-9]+")
RWA_REGISTRY_ASSET_DEFAULT_LIMIT = RWA_ASSET_MATRIX_DEFAULT_LIMIT
RWA_REGISTRY_VENUE_INSTRUMENT_DEFAULT_LIMIT = RWA_COLLECTION_DEFAULT_LIMIT


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


def build_rwa_symbol_registry(
    *,
    matrix: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build canonical assets plus alias indexes from current coverage."""
    matrix = matrix or build_rwa_asset_matrix()
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
                "canonical_underlying_asset_class": asset[
                    "canonical_underlying_asset_class"
                ],
                "raw_source_asset_ids": asset["raw_source_asset_ids"],
                "raw_source_asset_classes": asset[
                    "raw_source_asset_classes"
                ],
                "identity_status": asset["identity_status"],
                "identity_statuses": asset["identity_statuses"],
                "decision_grade": asset["decision_grade"],
                "manual_verification_required": asset[
                    "manual_verification_required"
                ],
                "canonical_symbols": asset["symbols"],
                "aliases": sorted(aliases),
                "venue_count": asset["venue_count"],
                "instrument_count": asset["instrument_count"],
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
            "canonical_asset_count": len(assets),
            "asset_count": len(assets),
            "alias_count": len(alias_index),
            "coverage_row_count": matrix["summary"]["coverage_row_count"],
            "coverage_rows": matrix["summary"]["coverage_rows"],
            "nested_instrument_count": matrix["summary"][
                "nested_instrument_count"
            ],
            "nested_instruments": matrix["summary"]["nested_instrument_count"],
            "registry_venue_count": matrix["summary"]["registry_venue_count"],
            "identity_quality": deepcopy(
                matrix["summary"]["identity_quality"]
            ),
            "decision_grade_canonical_asset_count": matrix["summary"][
                "decision_grade_canonical_asset_count"
            ],
            "manual_verification_asset_count": matrix["summary"][
                "manual_verification_asset_count"
            ],
            "ambiguous_source_scoped_asset_count": matrix["summary"][
                "ambiguous_source_scoped_asset_count"
            ],
            "decision_grade_mixed_class_asset_id_count": matrix["summary"][
                "decision_grade_mixed_class_asset_id_count"
            ],
            "metric_grains": {
                "canonical_asset_count": "canonical_asset",
                "asset_count": "canonical_asset_compatibility_alias",
                "alias_count": "normalized_symbol_alias",
                "coverage_row_count": "venue_instrument",
                "coverage_rows": "venue_instrument_compatibility_alias",
                "nested_instrument_count": "nested_venue_instrument",
                "nested_instruments": (
                    "nested_venue_instrument_compatibility_alias"
                ),
                "registry_venue_count": "registry_venue",
                "decision_grade_canonical_asset_count": (
                    "decision_grade_canonical_asset"
                ),
                "manual_verification_asset_count": (
                    "canonical_asset_requiring_manual_identity_verification"
                ),
                "ambiguous_source_scoped_asset_count": (
                    "source_scoped_ambiguous_asset"
                ),
                "decision_grade_mixed_class_asset_id_count": (
                    "decision_grade_canonical_asset_identity_violation"
                ),
            },
        },
        "source_snapshot_manifest": matrix["source_snapshot_manifest"],
        "assets": assets,
        "alias_index": {key: sorted(value) for key, value in sorted(alias_index.items())},
    }


def build_rwa_venue_registry(
    *,
    matrix: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build venue capabilities plus alias indexes and covered assets."""
    matrix = matrix or build_rwa_asset_matrix()
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
        for venue_id, venue_data in iter_asset_venue_instruments(asset):
            if venue_id not in venue_rows:
                continue
            row = venue_rows[venue_id]
            row["asset_classes"].update(asset["asset_classes"])
            row["source_types"].add(venue_data["source_type"])
            row["symbols"].append(venue_data["symbol"])
            venue_asset = {
                "asset_id": asset["asset_id"],
                "instrument_id": venue_data.get("instrument_id"),
                "symbol": venue_data["symbol"],
                "asset_class": venue_data.get("asset_class"),
                "asset_classes": asset["asset_classes"],
                "raw_source_asset_id": venue_data.get(
                    "raw_source_asset_id"
                ),
                "raw_source_asset_class": venue_data.get(
                    "raw_source_asset_class"
                ),
                "canonical_underlying_id": venue_data.get(
                    "canonical_underlying_id"
                ),
                "underlying_asset_class": venue_data.get(
                    "underlying_asset_class"
                ),
                "contract_type": venue_data.get("contract_type"),
                "identity_status": venue_data.get("identity_status"),
                "decision_grade": venue_data.get("decision_grade"),
                "manual_verification_required": venue_data.get(
                    "manual_verification_required"
                ),
                "identity_evidence": deepcopy(
                    venue_data.get("identity_evidence")
                ),
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
        row["instrument_count"] = len(row["assets"])
        row["symbol_count"] = len(row["symbols"])
        row["assets"].sort(
            key=lambda item: (
                str(item["asset_id"]),
                str(item["symbol"]),
                str(item.get("instrument_id") or ""),
            )
        )
        for alias in row["aliases"]:
            venue_alias_index[alias] = row["venue_id"]
    return {
        "summary": {
            "venue_count": len(venue_rows),
            "venue_alias_count": len(venue_alias_index),
            "venues_with_assets": len([row for row in venue_rows.values() if row["asset_count"] > 0]),
            "venue_instrument_count": sum(
                int(row["instrument_count"]) for row in venue_rows.values()
            ),
            "identity_quality": deepcopy(
                matrix["summary"]["identity_quality"]
            ),
            "decision_grade_canonical_asset_count": matrix["summary"][
                "decision_grade_canonical_asset_count"
            ],
            "manual_verification_asset_count": matrix["summary"][
                "manual_verification_asset_count"
            ],
            "ambiguous_source_scoped_asset_count": matrix["summary"][
                "ambiguous_source_scoped_asset_count"
            ],
            "decision_grade_mixed_class_asset_id_count": matrix["summary"][
                "decision_grade_mixed_class_asset_id_count"
            ],
            "metric_grains": {
                "venue_count": "registry_venue",
                "venue_alias_count": "normalized_venue_alias",
                "venues_with_assets": "registry_venue",
                "venue_instrument_count": "venue_instrument",
                "venues.asset_count": "canonical_asset_within_venue",
                "venues.instrument_count": "venue_instrument_within_venue",
                "venues.symbol_count": "distinct_symbol_within_venue",
                "decision_grade_canonical_asset_count": (
                    "decision_grade_canonical_asset"
                ),
                "manual_verification_asset_count": (
                    "canonical_asset_requiring_manual_identity_verification"
                ),
                "ambiguous_source_scoped_asset_count": (
                    "source_scoped_ambiguous_asset"
                ),
                "decision_grade_mixed_class_asset_id_count": (
                    "decision_grade_canonical_asset_identity_violation"
                ),
            },
        },
        "source_snapshot_manifest": matrix["source_snapshot_manifest"],
        "venues": sorted(venue_rows.values(), key=lambda item: str(item["venue_id"])),
        "venue_alias_index": dict(sorted(venue_alias_index.items())),
    }


def _paginate_registry_collection(
    items: list[dict[str, Any]],
    *,
    limit: int | None,
    offset: int,
    collection: str,
    grain: str,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None]:
    """Return a stable registry page with a discoverable continuation query."""
    if offset < 0:
        raise ValueError("offset must be greater than or equal to 0")
    if limit is not None and not 1 <= limit <= RWA_COLLECTION_MAX_LIMIT:
        raise ValueError(
            f"limit must be between 1 and {RWA_COLLECTION_MAX_LIMIT}"
        )
    if limit is None:
        if offset == 0:
            return items, None
        page = items[offset:]
    else:
        page = items[offset : offset + limit]
    total = len(items)
    candidate_next_offset = offset + len(page)
    has_more = candidate_next_offset < total
    next_offset = candidate_next_offset if has_more else None
    return page, {
        "collection": collection,
        "grain": grain,
        "limit": limit,
        "offset": offset,
        "returned": len(page),
        "total": total,
        "has_more": has_more,
        "next_offset": next_offset,
        "next": (
            {"limit": limit, "offset": next_offset}
            if next_offset is not None
            else None
        ),
    }


def _public_venue_summary(
    venue_row: dict[str, Any],
    *,
    include_aliases: bool,
) -> dict[str, Any]:
    """Project a full venue row without its unbounded instrument collections."""
    summary = {
        key: deepcopy(value)
        for key, value in venue_row.items()
        if key not in {"assets", "symbols"}
    }
    if not include_aliases:
        summary.pop("aliases", None)
    summary["instrument_collection"] = {
        "endpoint": "/v1/rwa/registry/venues",
        "venue": venue_row["venue_id"],
        "grain": "venue_instrument",
        "total": venue_row["instrument_count"],
        "paginated": True,
    }
    return summary


def _public_asset_projection(
    asset: dict[str, Any],
    *,
    include_aliases: bool,
    include_exact_instruments: bool,
) -> dict[str, Any]:
    """Project asset venues onto bounded summaries or exact-match instruments."""
    projected = deepcopy(asset)
    if not include_aliases:
        projected.pop("aliases", None)
    projected_venues: dict[str, dict[str, Any]] = {}
    for venue_id, venue_group in projected["venues"].items():
        instruments = venue_group.get("instruments")
        instrument_count = int(
            venue_group.get("instrument_count")
            or (len(instruments) if isinstance(instruments, list) else 1)
        )
        if include_exact_instruments:
            projected_group = venue_group
            projected_group["instruments_included"] = True
        else:
            projected_group = {
                key: value
                for key, value in venue_group.items()
                if key != "instruments"
            }
            projected_group["instruments_included"] = False
            projected_group["instrument_collection"] = {
                "endpoint": "/v1/rwa/registry/venues",
                "venue": venue_id,
                "asset_id": asset["asset_id"],
                "grain": "venue_instrument",
                "total": instrument_count,
                "paginated": True,
            }
        projected_venues[str(venue_id)] = projected_group
    projected["venues"] = projected_venues
    return projected


def _build_rwa_venue_registry_page(
    registry: dict[str, Any],
    *,
    venue: str | None,
    include_aliases: bool,
    limit: int,
    offset: int,
    asset_id: str | None = None,
) -> dict[str, Any]:
    """Page a prebuilt venue registry at its authoritative instrument grain."""
    selected_venues = registry["venues"]
    resolved_venue = None
    if venue:
        resolved_venue = registry["venue_alias_index"].get(
            normalize_venue_alias(venue)
        )
        if resolved_venue is None:
            raise ValueError(f"Unsupported venue: {venue}")
        selected_venues = [
            row
            for row in selected_venues
            if row["venue_id"] == resolved_venue
        ]

    instrument_rows = [
        {"venue_id": row["venue_id"], "instrument": instrument}
        for row in selected_venues
        for instrument in row["assets"]
        if asset_id is None
        or str(instrument["asset_id"]).casefold()
        == asset_id.strip().casefold()
    ]
    page, pagination = _paginate_registry_collection(
        instrument_rows,
        limit=limit,
        offset=offset,
        collection="venue_instruments",
        grain="venue_instrument",
    )
    page_by_venue: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in page:
        page_by_venue[str(item["venue_id"])].append(item["instrument"])
    matching_count_by_venue = Counter(
        str(item["venue_id"]) for item in instrument_rows
    )

    projected_venues: list[dict[str, Any]] = []
    for row in selected_venues:
        venue_id = str(row["venue_id"])
        returned_instruments = deepcopy(page_by_venue.get(venue_id, []))
        projected = _public_venue_summary(
            row,
            include_aliases=include_aliases,
        )
        projected["assets"] = returned_instruments
        projected["matching_instrument_count"] = matching_count_by_venue[
            venue_id
        ]
        projected["returned_instrument_count"] = len(returned_instruments)
        projected["symbols"] = sorted(
            {
                str(instrument["symbol"])
                for instrument in returned_instruments
            }
        )
        projected["symbols_scope"] = "returned_venue_instrument_page"
        projected_venues.append(projected)

    assert pagination is not None
    return {
        "filters": {
            "venue": venue,
            "resolved_venue": resolved_venue,
            "asset_id": asset_id,
            "include_aliases": include_aliases,
            "limit": limit,
            "offset": offset,
        },
        "summary": {
            **registry["summary"],
            "matching_venues": len(selected_venues),
            "matching_venue_instruments": len(instrument_rows),
            "returned_venues": len(projected_venues),
            "venues_with_returned_instruments": len(page_by_venue),
            "returned_venue_instruments": len(page),
        },
        "source_snapshot_manifest": registry["source_snapshot_manifest"],
        "pagination": pagination,
        "venues": projected_venues,
    }


def build_rwa_venue_registry_page(
    *,
    venue: str | None = None,
    asset_id: str | None = None,
    include_aliases: bool = False,
    limit: int = RWA_REGISTRY_VENUE_INSTRUMENT_DEFAULT_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """Build a bounded public venue registry page."""
    return _build_rwa_venue_registry_page(
        build_rwa_venue_registry(),
        venue=venue,
        asset_id=asset_id,
        include_aliases=include_aliases,
        limit=limit,
        offset=offset,
    )


def _filter_venue_group_to_symbol(
    venue_group: dict[str, Any],
    symbol_key: str,
    *,
    require_exact: bool,
) -> dict[str, Any] | None:
    """Narrow a lossless venue group only when the query names an exact pair."""
    instruments = venue_group.get("instruments")
    if not isinstance(instruments, list):
        return venue_group
    matches = [
        instrument
        for instrument in instruments
        if isinstance(instrument, dict)
        and normalize_symbol_alias(str(instrument.get("symbol") or ""))
        == symbol_key
    ]
    if not matches:
        return None if require_exact else venue_group
    if len(matches) == len(instruments):
        return venue_group
    representative = matches[0]
    return {
        **representative,
        "instrument_count": len(matches),
        "instruments": matches,
        "compatibility_projection": {
            "mode": "first_in_stable_instrument_order",
            "representative_instrument_id": representative.get(
                "instrument_id"
            ),
            "authoritative_field": "instruments",
            "query_filtered": True,
        },
    }


def _project_asset_to_venues(
    asset: dict[str, Any],
    venues: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Recompute compatibility fields after venue-instrument filtering."""
    projected = dict(asset)
    projected["venues"] = venues
    projected["venue_count"] = len(venues)
    projected["instrument_count"] = sum(
        int(group.get("instrument_count") or 1)
        for group in venues.values()
    )
    projected["source_types"] = sorted(
        {
            str(instrument["source_type"])
            for _venue_id, instrument in iter_asset_venue_instruments(
                projected
            )
        }
    )
    selected_symbols = sorted(
        {
            str(instrument["symbol"])
            for _venue_id, instrument in iter_asset_venue_instruments(
                projected
            )
        }
    )
    if selected_symbols:
        projected["canonical_symbols"] = selected_symbols
    executable_venues = set(asset.get("executable_venues") or [])
    reference_venues = set(asset.get("reference_venues") or [])
    projected["executable_venues"] = sorted(
        executable_venues.intersection(venues)
    )
    projected["reference_venues"] = sorted(
        reference_venues.intersection(venues)
    )
    return projected


def _resolve_rwa_symbol_from_registries(
    symbol: str,
    *,
    venue: str | None,
    asset_registry: dict[str, Any],
    venue_registry: dict[str, Any],
) -> dict[str, Any]:
    """Resolve against prebuilt registries without repeating catalog assembly."""
    symbol_key = normalize_symbol_alias(symbol)
    query_has_quote = _split_symbol(symbol)[1] is not None
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
            venue_data = _filter_venue_group_to_symbol(
                asset["venues"][venue_id],
                symbol_key,
                require_exact=query_has_quote,
            )
            if venue_data is None:
                continue
            asset = _project_asset_to_venues(
                asset,
                {venue_id: venue_data},
            )
        elif query_has_quote:
            matching_venues: dict[str, dict[str, Any]] = {}
            for candidate_venue_id, venue_group in asset["venues"].items():
                venue_data = _filter_venue_group_to_symbol(
                    venue_group,
                    symbol_key,
                    require_exact=True,
                )
                if venue_data is not None:
                    matching_venues[str(candidate_venue_id)] = venue_data
            if not matching_venues:
                continue
            asset = _project_asset_to_venues(asset, matching_venues)
        matches.append(asset)
    return {
        "query": {"symbol": symbol, "normalized_symbol_alias": symbol_key, "venue": venue, "resolved_venue": venue_id},
        "match_count": len(matches),
        "ambiguous": len(matches) > 1,
        "matches": matches,
        "known_venue": venue_registry["venue_alias_index"].get(normalize_venue_alias(venue or "")) if venue else None,
    }


def resolve_rwa_symbol(symbol: str, *, venue: str | None = None) -> dict[str, Any]:
    """Resolve a caller symbol and optional venue into canonical coverage rows."""
    matrix = build_rwa_asset_matrix()
    return _resolve_rwa_symbol_from_registries(
        symbol,
        venue=venue,
        asset_registry=build_rwa_symbol_registry(matrix=matrix),
        venue_registry=build_rwa_venue_registry(matrix=matrix),
    )


def build_rwa_registry_overview(
    *,
    symbol: str | None = None,
    venue: str | None = None,
    include_aliases: bool = False,
    limit: int | None = None,
    offset: int = 0,
    include_venue_instruments: bool = True,
) -> dict[str, Any]:
    """Return canonical asset and venue coverage in one service response."""
    matrix = build_rwa_asset_matrix()
    asset_registry = build_rwa_symbol_registry(matrix=matrix)
    venue_registry = build_rwa_venue_registry(matrix=matrix)
    assets = asset_registry["assets"]
    venues = venue_registry["venues"]
    resolution = None
    if symbol:
        resolution = _resolve_rwa_symbol_from_registries(
            symbol,
            venue=venue,
            asset_registry=asset_registry,
            venue_registry=venue_registry,
        )
        assets = resolution["matches"]
        if resolution["query"]["resolved_venue"]:
            venues = [
                row
                for row in venues
                if row["venue_id"] == resolution["query"]["resolved_venue"]
            ]
    elif venue:
        venue_key = normalize_venue_alias(venue)
        venue_id = venue_registry["venue_alias_index"].get(venue_key)
        if venue_id is None:
            raise ValueError(f"Unsupported venue: {venue}")
        assets = [asset for asset in assets if venue_id in asset["venues"]]
        venues = [row for row in venues if row["venue_id"] == venue_id]
    matching_asset_count = len(assets)
    assets, pagination = _paginate_registry_collection(
        assets,
        limit=limit,
        offset=offset,
        collection="assets",
        grain="canonical_asset",
    )
    if include_venue_instruments:
        if not include_aliases:
            for asset in assets:
                asset.pop("aliases", None)
            for venue_row in venues:
                venue_row.pop("aliases", None)
    else:
        assets = [
            _public_asset_projection(
                asset,
                include_aliases=include_aliases,
                include_exact_instruments=bool(
                    symbol and _split_symbol(symbol)[1] is not None
                ),
            )
            for asset in assets
        ]
        venues = [
            _public_venue_summary(
                venue_row,
                include_aliases=include_aliases,
            )
            for venue_row in venues
        ]
        if resolution is not None:
            resolution = {
                key: value
                for key, value in resolution.items()
                if key != "matches"
            }
            resolution["matches_collection"] = "assets"
    response = {
        "collection_contract": {
            "assets": {
                "grain": "canonical_asset",
                "paginated": pagination is not None,
            },
            "venues": {
                "grain": "registry_venue_summary",
                "instrument_collection_endpoint": (
                    "/v1/rwa/registry/venues"
                ),
                "instrument_grain": "venue_instrument",
                "instrument_collection_included": include_venue_instruments,
            },
        },
        "source_snapshot_manifest": asset_registry[
            "source_snapshot_manifest"
        ],
        "summary": {
            **asset_registry["summary"],
            "matching_assets": matching_asset_count,
            "returned_assets": len(assets),
            "returned_venues": len(venues),
        },
        "resolution": resolution,
        "assets": assets,
        "venues": venues,
    }
    if pagination is not None:
        response["pagination"] = pagination
    return response
