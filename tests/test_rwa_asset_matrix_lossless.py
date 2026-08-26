"""Acceptance checks for the lossless RWA asset-matrix grain."""

from __future__ import annotations

from collections import Counter

from src.rwa_coverage import _coverage_rows, build_rwa_asset_matrix
from src.rwa_non_crypto_feeds import build_non_crypto_feed_catalog
from src.rwa_symbol_registry import (
    build_rwa_venue_registry,
    resolve_rwa_symbol,
)


def _nested_counts(matrix: dict) -> Counter[str]:
    return Counter(
        venue_id
        for asset in matrix["assets"]
        for venue_id, venue_group in asset["venues"].items()
        for _instrument in venue_group["instruments"]
    )


def test_asset_matrix_retains_every_coverage_row_at_instrument_grain() -> None:
    rows = _coverage_rows()
    matrix = build_rwa_asset_matrix()
    nested_counts = _nested_counts(matrix)
    source_counts = Counter(str(row["venue"]) for row in rows)

    assert matrix["matrix_schema"]["version"] == 2
    assert matrix["summary"]["coverage_row_count"] == len(rows) == 5_161
    assert matrix["summary"]["nested_instrument_count"] == len(rows)
    assert sum(nested_counts.values()) == len(rows)
    assert nested_counts == source_counts
    assert nested_counts["aevo"] == 1_915
    assert nested_counts["ostium"] == 66


def test_collision_groups_are_deterministic_and_flat_fields_are_compatible() -> None:
    first = build_rwa_asset_matrix()
    second = build_rwa_asset_matrix()
    assert first["assets"] == second["assets"]

    instrument_ids: list[str] = []
    for asset in first["assets"]:
        assert asset["instrument_count"] == sum(
            int(group["instrument_count"])
            for group in asset["venues"].values()
        )
        for group in asset["venues"].values():
            assert group["instrument_count"] == len(group["instruments"])
            assert group["symbol"] == group["instruments"][0]["symbol"]
            assert group["instrument_id"] == group["instruments"][0]["instrument_id"]
            assert group["compatibility_projection"]["authoritative_field"] == (
                "instruments"
            )
            instrument_ids.extend(
                str(instrument["instrument_id"])
                for instrument in group["instruments"]
            )
    assert len(instrument_ids) == len(set(instrument_ids))

    usd = next(asset for asset in first["assets"] if asset["asset_id"] == "USD")
    ostium = usd["venues"]["ostium"]
    assert ostium["instrument_count"] == 5
    assert {row["symbol"] for row in ostium["instruments"]} == {
        "USD/CAD",
        "USD/CHF",
        "USD/JPY",
        "USD/KRW",
        "USD/MXN",
    }


def test_ostium_fx_builds_two_unique_feeds_for_each_pair() -> None:
    catalog = build_non_crypto_feed_catalog(asset_class="fx", venue="ostium")
    feeds = [*catalog["vwap_feeds"], *catalog["bidask_feeds"]]

    assert catalog["summary"]["feed_count"] == 18
    assert catalog["summary"]["vwap_feed_count"] == 9
    assert catalog["summary"]["bidask_feed_count"] == 9
    assert len({feed["feed_id"] for feed in feeds}) == 18
    assert Counter(feed["symbol"] for feed in feeds) == Counter(
        {
            "AUD/USD": 2,
            "EUR/USD": 2,
            "GBP/USD": 2,
            "NZD/USD": 2,
            "USD/CAD": 2,
            "USD/CHF": 2,
            "USD/JPY": 2,
            "USD/KRW": 2,
            "USD/MXN": 2,
        }
    )
    assert Counter(
        feed["blocksize_benchmark"]["status"] for feed in feeds
    ) == Counter(
        {
            "ready_for_blocksize_benchmark": 16,
            "requires_blocksize_instrument_check": 2,
        }
    )
    assert {
        feed["kind"]
        for feed in feeds
        if feed["symbol"] == "USD/KRW"
        and feed["blocksize_benchmark"]["status"]
        == "requires_blocksize_instrument_check"
    } == {"vwap", "bidask"}


def test_venue_registry_consumes_all_nested_instruments() -> None:
    registry = build_rwa_venue_registry()
    venues = {row["venue_id"]: row for row in registry["venues"]}
    assert len(venues["aevo"]["assets"]) == 1_915
    assert len(venues["ostium"]["assets"]) == 66


def test_exact_pair_resolution_does_not_return_sibling_fx_instruments() -> None:
    resolved = resolve_rwa_symbol("USD/CAD", venue="ostium")
    assert resolved["match_count"] == 1
    match = resolved["matches"][0]
    venue_group = match["venues"]["ostium"]
    assert venue_group["instrument_count"] == 1
    assert venue_group["instruments"][0]["symbol"] == "USD/CAD"
    assert match["canonical_symbols"] == ["USD/CAD"]


def test_source_snapshot_manifest_reconciles_rows_without_current_time_claim() -> None:
    matrix = build_rwa_asset_matrix(limit=10)
    manifest = matrix["source_snapshot_manifest"]
    components = {
        component["component_id"]: component
        for component in manifest["components"]
    }

    assert manifest["included_coverage_row_count"] == 5_161
    assert sum(
        int(component["included_coverage_row_count"])
        for component in components.values()
    ) == 5_161
    assert "does not mean every component" in manifest["assembled_at_semantics"]
    assert components["static_coverage_catalog"]["snapshot_at"] is None
    assert components["static_coverage_catalog"]["freshness_status"] == (
        "not_time_series_static_catalog"
    )
    for component_id in (
        "hyperliquid_tradeable_discovery",
        "derivative_venue_discovery",
        "rwa_xyz_new_asset_monitor",
    ):
        assert components[component_id]["snapshot_at"]
        assert components[component_id]["freshness_status"] in {
            "current_within_catalog_cadence",
            "stale",
            "future_dated",
        }
