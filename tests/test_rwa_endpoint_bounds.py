"""Acceptance checks for bounded, lossless RWA collection endpoints."""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from src import (
    rwa_consensus,
    rwa_equity_universes,
    rwa_market_expansion,
    rwa_non_crypto_feeds,
    rwa_sourcing,
)
from src.resource_server import app
from src.rwa_api_collections import paginate_rows


@pytest.fixture
def client() -> TestClient:
    with TestClient(app) as test_client:
        yield test_client


@pytest.mark.parametrize(
    ("path", "row_key", "pagination_key", "total_summary_key"),
    [
        (
            "/v1/rwa/sourcing/jobs",
            "jobs",
            None,
            "matching_job_count",
        ),
        (
            "/v1/rwa/discovery",
            "feeds",
            None,
            "feed_count",
        ),
        (
            "/v1/rwa/derivative-venues",
            "coverage_rows",
            "coverage_rows",
            "matching_coverage_row_count",
        ),
        (
            "/v1/rwa/identity-audit",
            "rows",
            None,
            "matching_asset_count",
        ),
    ],
)
def test_large_rwa_endpoint_defaults_are_bounded_and_reconcilable(
    client: TestClient,
    path: str,
    row_key: str,
    pagination_key: str | None,
    total_summary_key: str,
) -> None:
    first = client.get(path)
    repeated = client.get(path)

    assert first.status_code == 200
    assert len(first.content) < 1_000_000
    first_body = first.json()
    repeated_body = repeated.json()
    pagination = first_body["pagination"]
    repeated_pagination = repeated_body["pagination"]
    if pagination_key:
        pagination = pagination[pagination_key]
        repeated_pagination = repeated_pagination[pagination_key]

    assert pagination["limit"] == 50
    assert pagination["offset"] == 0
    assert pagination["returned"] == len(first_body[row_key])
    assert pagination["total"] == first_body["summary"][total_summary_key]
    assert pagination == repeated_pagination
    assert first_body[row_key] == repeated_body[row_key]

    next_offset = pagination["next_offset"]
    assert next_offset == pagination["returned"]
    second = client.get(f"{path}?offset={next_offset}")
    assert second.status_code == 200
    second_body = second.json()
    second_pagination = second_body["pagination"]
    if pagination_key:
        second_pagination = second_pagination[pagination_key]
    assert second_pagination["offset"] == next_offset
    assert second_pagination["total"] == pagination["total"]

    first_rows = {
        json.dumps(row, sort_keys=True)
        for row in first_body[row_key]
    }
    second_rows = {
        json.dumps(row, sort_keys=True)
        for row in second_body[row_key]
    }
    assert first_rows.isdisjoint(second_rows)

    tail_offset = max(0, pagination["total"] - 3)
    tail = client.get(f"{path}?limit=3&offset={tail_offset}")
    assert tail.status_code == 200
    tail_body = tail.json()
    tail_pagination = tail_body["pagination"]
    if pagination_key:
        tail_pagination = tail_pagination[pagination_key]
    assert tail_pagination == {
        "limit": 3,
        "offset": tail_offset,
        "returned": min(3, pagination["total"]),
        "total": pagination["total"],
        "has_more": False,
        "next_offset": None,
    }


def test_pagination_helper_reconciles_every_row_without_loss() -> None:
    rows = [{"id": index} for index in range(237)]
    collected: list[dict[str, int]] = []
    offset = 0

    while True:
        page, pagination = paginate_rows(rows, limit=37, offset=offset)
        collected.extend(page)
        if not pagination["has_more"]:
            break
        offset = pagination["next_offset"]

    assert collected == rows
    assert pagination["returned"] == 15
    assert pagination["total"] == 237
    assert pagination["next_offset"] is None


def test_non_crypto_feed_default_collections_are_bounded_and_reconcilable(
    client: TestClient,
) -> None:
    response = client.get("/v1/rwa/non-crypto-feeds")

    assert response.status_code == 200
    assert len(response.content) < 1_000_000
    body = response.json()
    collection_counts = {
        "vwap_feeds": "vwap_feed_count",
        "bidask_feeds": "bidask_feed_count",
        "excluded_rows": "excluded_tokenized_stock_rows",
    }
    for collection, total_key in collection_counts.items():
        pagination = body["pagination"][collection]
        assert pagination["returned"] == len(body[collection])
        assert pagination["total"] == body["summary"][total_key]
        assert pagination["limit"] == 50
        assert pagination["offset"] == 0

        tail_offset = max(0, pagination["total"] - 2)
        tail = client.get(
            f"/v1/rwa/non-crypto-feeds?limit=2&offset={tail_offset}"
        )
        assert tail.status_code == 200
        tail_pagination = tail.json()["pagination"][collection]
        assert tail_pagination["total"] == pagination["total"]
        assert tail_pagination["has_more"] is False
        assert tail_pagination["next_offset"] is None


def test_identity_audit_supports_bounded_exact_lookup(client: TestClient) -> None:
    response = client.get(
        "/v1/rwa/identity-audit?asset_id=BUIDL,USCC,SGOV"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["asset_count"] > 3
    assert body["summary"]["matching_asset_count"] == 3
    assert body["pagination"]["total"] == 3
    assert {row["asset_id"] for row in body["rows"]} == {
        "BUIDL",
        "SGOV",
        "USCC",
    }


def test_sourcing_job_filters_keep_global_and_matching_counts(
    client: TestClient,
) -> None:
    response = client.get(
        "/v1/rwa/sourcing/jobs?venue=hyperliquid_rwa_spot&limit=100"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["summary"]["job_count"] >= body["summary"]["matching_job_count"]
    assert body["pagination"]["total"] == body["summary"]["matching_job_count"]
    assert body["pagination"]["has_more"] is False
    assert all(
        row["venue"] == "hyperliquid_rwa_spot"
        for row in body["jobs"]
    )


def test_equity_universe_builds_registry_once(monkeypatch: pytest.MonkeyPatch) -> None:
    original = rwa_equity_universes.build_rwa_symbol_registry
    calls = 0

    def counted_registry(
        *,
        matrix: dict[str, object] | None = None,
    ) -> dict[str, object]:
        nonlocal calls
        calls += 1
        return original(matrix=matrix)

    monkeypatch.setattr(
        rwa_equity_universes,
        "build_rwa_symbol_registry",
        counted_registry,
    )

    result = rwa_equity_universes.build_equity_universe_sourcing_plan()

    assert result["summary"]["universe_count"] >= 12
    assert calls == 1


def test_market_expansion_builds_asset_matrix_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = rwa_market_expansion.build_rwa_asset_matrix
    calls = 0

    def counted_matrix() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return original()

    monkeypatch.setattr(
        rwa_market_expansion,
        "build_rwa_asset_matrix",
        counted_matrix,
    )

    result = rwa_market_expansion.build_market_expansion_plan()

    assert result["summary"]["expanded_venue_count"] >= 15
    assert calls == 1


def test_consensus_shares_one_asset_matrix_across_builders(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = rwa_consensus.build_rwa_asset_matrix
    calls = 0

    def counted_matrix() -> dict[str, object]:
        nonlocal calls
        calls += 1
        return original()

    def unexpected_matrix_build(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("nested builder rebuilt the shared asset matrix")

    monkeypatch.setattr(rwa_consensus, "build_rwa_asset_matrix", counted_matrix)
    monkeypatch.setattr(
        rwa_non_crypto_feeds,
        "build_rwa_asset_matrix",
        unexpected_matrix_build,
    )
    monkeypatch.setattr(
        rwa_market_expansion,
        "build_rwa_asset_matrix",
        unexpected_matrix_build,
    )
    monkeypatch.setattr(
        rwa_sourcing,
        "build_rwa_asset_matrix",
        unexpected_matrix_build,
    )

    result = rwa_consensus.build_consensus_source_plan()

    assert result["summary"]["asset_count"] > 0
    assert calls == 1
