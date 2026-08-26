"""Acceptance checks for bounded, lossless public RWA registries."""

from __future__ import annotations

from collections import Counter

import pytest
from fastapi.testclient import TestClient

from src.config import settings
from src.resource_server import app
from src.rwa_symbol_registry import (
    _build_rwa_venue_registry_page,
    _paginate_registry_collection,
    build_rwa_symbol_registry,
    build_rwa_venue_registry,
    resolve_rwa_symbol,
)


@pytest.fixture
def test_client(monkeypatch, tmp_path):
    """Create an isolated client for public registry contract checks."""
    monkeypatch.setenv("RWA_MUTATIONS_ENABLED", "true")
    monkeypatch.setenv(
        "RWA_OPERATOR_TOKEN",
        "rwa-registry-test-operator-token-0123456789abcdef",
    )
    monkeypatch.setenv(
        "RWA_OBSERVATION_DB_PATH",
        str(tmp_path / "rwa_observations.db"),
    )
    monkeypatch.setenv("CREDIT_DB_PATH", str(tmp_path / "credits.db"))
    monkeypatch.setattr(
        settings.server,
        "unverified_http_credits_enabled",
        True,
    )
    monkeypatch.setattr(
        settings.x402,
        "solana_wallet_address",
        "11111111111111111111111111111111",
    )
    monkeypatch.setattr(
        settings.x402,
        "solana_fee_payer",
        "SysvarRent111111111111111111111111111111111",
    )
    monkeypatch.setattr(
        settings.x402,
        "evm_wallet_address",
        "0x1111111111111111111111111111111111111111",
    )
    with TestClient(app, base_url="https://testserver") as client:
        yield client


def test_public_registry_default_is_bounded_at_canonical_asset_grain(
    test_client,
) -> None:
    response = test_client.get("/v1/rwa/registry")

    assert response.status_code == 200
    assert len(response.content) < 1_000_000
    payload = response.json()
    assert payload["pagination"] == {
        "collection": "assets",
        "grain": "canonical_asset",
        "limit": 10,
        "offset": 0,
        "returned": 10,
        "total": payload["summary"]["canonical_asset_count"],
        "has_more": True,
        "next_offset": 10,
        "next": {"limit": 10, "offset": 10},
    }
    assert payload["summary"]["returned_assets"] == len(payload["assets"])
    assert payload["summary"][
        "decision_grade_mixed_class_asset_id_count"
    ] == 0
    assert payload["summary"]["identity_quality"]["acceptance"][
        "status"
    ] == "pass"
    assert all(
        {
            "canonical_underlying_asset_class",
            "raw_source_asset_ids",
            "raw_source_asset_classes",
            "identity_status",
            "decision_grade",
            "manual_verification_required",
        }.issubset(asset)
        for asset in payload["assets"]
    )
    assert all(
        group["instruments_included"] is False
        and "instruments" not in group
        and group["instrument_collection"]["grain"] == "venue_instrument"
        for asset in payload["assets"]
        for group in asset["venues"].values()
    )
    assert all("assets" not in venue for venue in payload["venues"])
    assert all("symbols" not in venue for venue in payload["venues"])
    assert {
        venue["instrument_collection"]["grain"]
        for venue in payload["venues"]
    } == {"venue_instrument"}


def test_public_venue_registry_default_is_bounded_at_instrument_grain(
    test_client,
) -> None:
    response = test_client.get("/v1/rwa/registry/venues")

    assert response.status_code == 200
    assert len(response.content) < 1_000_000
    payload = response.json()
    pagination = payload["pagination"]
    assert pagination["collection"] == "venue_instruments"
    assert pagination["grain"] == "venue_instrument"
    assert pagination["limit"] == 50
    assert pagination["returned"] == 50
    assert pagination["total"] == 5_161
    assert pagination["next_offset"] == 50
    assert pagination["next"] == {"limit": 50, "offset": 50}
    assert sum(
        len(venue["assets"]) for venue in payload["venues"]
    ) == pagination["returned"]
    assert payload["summary"]["venue_instrument_count"] == 5_161
    assert payload["summary"]["returned_venue_instruments"] == 50
    assert payload["summary"][
        "decision_grade_mixed_class_asset_id_count"
    ] == 0
    assert all(
        {
            "raw_source_asset_id",
            "raw_source_asset_class",
            "canonical_underlying_id",
            "underlying_asset_class",
            "contract_type",
            "identity_status",
            "decision_grade",
            "manual_verification_required",
            "identity_evidence",
        }.issubset(instrument)
        for venue in payload["venues"]
        for instrument in venue["assets"]
    )


def test_asset_pages_are_complete_stable_and_non_overlapping() -> None:
    registry = build_rwa_symbol_registry()
    expected_ids = [asset["asset_id"] for asset in registry["assets"]]
    returned_ids: list[str] = []
    offset = 0

    while True:
        page, pagination = _paginate_registry_collection(
            registry["assets"],
            limit=100,
            offset=offset,
            collection="assets",
            grain="canonical_asset",
        )
        assert pagination is not None
        returned_ids.extend(str(asset["asset_id"]) for asset in page)
        if not pagination["has_more"]:
            assert pagination["next_offset"] is None
            assert pagination["next"] is None
            break
        assert pagination["next"] == {
            "limit": 100,
            "offset": pagination["next_offset"],
        }
        offset = int(pagination["next_offset"])

    assert returned_ids == expected_ids
    assert len(returned_ids) == len(set(returned_ids))
    assert len(returned_ids) == registry["summary"]["canonical_asset_count"]


def test_venue_instrument_pages_reconcile_losslessly_to_full_registry() -> None:
    registry = build_rwa_venue_registry()
    expected_counts = {
        str(venue["venue_id"]): int(venue["instrument_count"])
        for venue in registry["venues"]
    }
    returned_keys: list[tuple[str, str]] = []
    returned_counts: Counter[str] = Counter()
    offset = 0

    while True:
        payload = _build_rwa_venue_registry_page(
            registry,
            venue=None,
            include_aliases=False,
            limit=100,
            offset=offset,
        )
        for venue in payload["venues"]:
            venue_id = str(venue["venue_id"])
            for instrument in venue["assets"]:
                instrument_id = str(instrument["instrument_id"])
                returned_keys.append((venue_id, instrument_id))
                returned_counts[venue_id] += 1
        pagination = payload["pagination"]
        if not pagination["has_more"]:
            assert pagination["next_offset"] is None
            break
        offset = int(pagination["next_offset"])

    assert len(returned_keys) == 5_161
    assert len(returned_keys) == len(set(returned_keys))
    assert dict(returned_counts) == {
        venue_id: count
        for venue_id, count in expected_counts.items()
        if count
    }
    assert sum(expected_counts.values()) == 5_161


def test_collision_heavy_venue_counts_and_tail_pages_are_exact() -> None:
    registry = build_rwa_venue_registry()

    aevo = _build_rwa_venue_registry_page(
        registry,
        venue="aevo",
        include_aliases=False,
        limit=100,
        offset=1_900,
    )
    assert aevo["summary"]["matching_venue_instruments"] == 1_915
    assert aevo["pagination"]["returned"] == 15
    assert aevo["pagination"]["has_more"] is False
    assert len(aevo["venues"][0]["assets"]) == 15

    ostium = _build_rwa_venue_registry_page(
        registry,
        venue="ostium",
        include_aliases=False,
        limit=50,
        offset=50,
    )
    assert ostium["summary"]["matching_venue_instruments"] == 66
    assert ostium["pagination"]["returned"] == 16
    assert ostium["pagination"]["has_more"] is False
    assert len(ostium["venues"][0]["assets"]) == 16


def test_collision_heavy_asset_response_links_to_bounded_instrument_pages(
    test_client,
) -> None:
    asset_response = test_client.get(
        "/v1/rwa/registry",
        params={"symbol": "BTC", "venue": "aevo"},
    )
    assert asset_response.status_code == 200
    assert len(asset_response.content) < 1_000_000
    asset = asset_response.json()["assets"][0]
    venue_group = asset["venues"]["aevo"]
    expected_count = int(venue_group["instrument_count"])
    assert expected_count > 800
    assert venue_group["instruments_included"] is False
    assert "instruments" not in venue_group
    assert venue_group["instrument_collection"] == {
        "endpoint": "/v1/rwa/registry/venues",
        "venue": "aevo",
        "asset_id": "BTC",
        "grain": "venue_instrument",
        "total": expected_count,
        "paginated": True,
    }

    first = test_client.get(
        "/v1/rwa/registry/venues",
        params={
            "venue": "aevo",
            "asset_id": "BTC",
            "limit": 100,
        },
    ).json()
    tail = test_client.get(
        "/v1/rwa/registry/venues",
        params={
            "venue": "aevo",
            "asset_id": "BTC",
            "limit": 100,
            "offset": 800,
        },
    ).json()
    assert first["pagination"]["total"] == expected_count
    assert first["pagination"]["returned"] == 100
    assert first["pagination"]["next_offset"] == 100
    assert first["venues"][0]["matching_instrument_count"] == expected_count
    assert {
        instrument["asset_id"]
        for instrument in first["venues"][0]["assets"]
    } == {"BTC"}
    assert tail["pagination"]["returned"] == expected_count - 800
    assert tail["pagination"]["has_more"] is False
    assert tail["pagination"]["next_offset"] is None


def test_exact_pair_resolution_returns_only_matching_venue_instrument(
    test_client,
) -> None:
    resolved = resolve_rwa_symbol("USD/CAD", venue="ostium")
    assert resolved["match_count"] == 1
    match = resolved["matches"][0]
    assert match["instrument_count"] == 1
    assert match["canonical_symbols"] == ["USD/CAD"]
    assert [
        instrument["symbol"]
        for instrument in match["venues"]["ostium"]["instruments"]
    ] == ["USD/CAD"]

    response = test_client.get(
        "/v1/rwa/registry",
        params={"symbol": "USD/CAD", "venue": "ostium"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["resolution"]["matches_collection"] == "assets"
    assert "matches" not in payload["resolution"]
    assert payload["pagination"]["total"] == 1
    assert payload["assets"][0]["instrument_count"] == 1
    assert [
        instrument["symbol"]
        for instrument in payload["assets"][0]["venues"]["ostium"][
            "instruments"
        ]
    ] == ["USD/CAD"]

    cross_venue = resolve_rwa_symbol("USD/CAD")
    assert cross_venue["match_count"] == 1
    assert {
        instrument["symbol"]
        for group in cross_venue["matches"][0]["venues"].values()
        for instrument in group["instruments"]
    } == {"USD/CAD"}

    absent_pair = resolve_rwa_symbol("BTC/USD", venue="aevo")
    assert absent_pair["match_count"] == 0
    assert absent_pair["matches"] == []


def test_registry_query_limits_are_rejected_above_public_maximum(
    test_client,
) -> None:
    assert test_client.get(
        "/v1/rwa/registry",
        params={"limit": 101},
    ).status_code == 422
    assert test_client.get(
        "/v1/rwa/registry/venues",
        params={"limit": 101},
    ).status_code == 422
