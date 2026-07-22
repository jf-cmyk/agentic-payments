from __future__ import annotations

import pytest

from src.rwa_aggregator import build_blocksize_oracle_snapshot
from src.rwa_pricing import calculate_executable_composite, calculate_reference_composite


NOW = "2026-07-16T12:00:00+00:00"
IDENTITY = {
    "canonical_asset_id": "US0378331005",
    "quote_currency": "USD",
}
RIGHTS = ["approved_internal"]


def _book(
    venue: str,
    instrument_id: str,
    side: str,
    levels: list[dict[str, float]],
    **overrides: object,
) -> dict[str, object]:
    return {
        **IDENTITY,
        "venue": venue,
        "instrument_id": instrument_id,
        "source_kind": "executable_l2",
        "side": side,
        "event_time": "2026-07-16T11:59:59+00:00",
        "sequence": 7,
        "rights_status": "approved_internal",
        "reliability": 1.0,
        "taker_fee_bps": 0,
        "levels": levels,
        **overrides,
    }


def _route_payload(**overrides: object) -> dict[str, object]:
    return {
        **IDENTITY,
        "side": "buy",
        "request_kind": "base_quantity",
        "requested_amount": 120,
        "now": NOW,
        "max_age_ms": 5_000,
        "min_reliability": 0.99,
        "max_venue_share": 0.60,
        "min_venues": 2,
        "allowed_rights_statuses": RIGHTS,
        **overrides,
    }


def test_executable_router_walks_fee_adjusted_levels_and_applies_concentration_cap() -> None:
    result = calculate_executable_composite(
        _route_payload(
            books=[
                _book(
                    "venue_a",
                    "AAPL-USD-A",
                    "buy",
                    [{"price": 100, "size": 50}, {"price": 101, "size": 100}],
                    taker_fee_bps=10,
                ),
                _book("venue_b", "AAPL-USD-B", "buy", [{"price": 100.05, "size": 100}]),
                _book(
                    "stale_cheap",
                    "AAPL-USD-STALE",
                    "buy",
                    [{"price": 1, "size": 1_000}],
                    event_time="2026-07-16T11:00:00+00:00",
                ),
                _book(
                    "unapproved_cheap",
                    "AAPL-USD-UNAPPROVED",
                    "buy",
                    [{"price": 1, "size": 1_000}],
                    rights_status="unknown",
                ),
            ]
        )
    )

    assert result["status"] == "full_fill"
    assert result["price_type"] == "executable_block_vwap"
    assert result["filled_base"] == pytest.approx(120)
    assert result["unfilled_request_amount"] == pytest.approx(0)
    assert result["venue_request_fills"] == {"venue_a": 48.0, "venue_b": 72.0}
    assert [row["venue"] for row in result["route"]] == ["venue_b", "venue_a"]
    assert result["gross_quote"] == pytest.approx(12_003.6)
    assert result["fees_quote"] == pytest.approx(4.8)
    assert result["effective_vwap"] == pytest.approx(100.07)
    assert {row["reason"] for row in result["excluded_books"]} == {"stale", "rights_not_allowed"}


def test_quote_notional_router_reports_partial_fill_without_extrapolation() -> None:
    result = calculate_executable_composite(
        _route_payload(
            request_kind="quote_notional",
            requested_amount=10_000,
            max_venue_share=1,
            min_venues=1,
            books=[
                _book(
                    "venue_a",
                    "AAPL-USD-A",
                    "sell",
                    [{"price": 100, "size": 25}, {"price": 99, "size": 25}],
                    taker_fee_bps=20,
                    venue_cap_quote=4_000,
                )
            ],
            side="sell",
        )
    )

    assert result["status"] == "partial_fill"
    assert result["filled_request_amount"] == pytest.approx(4_000)
    assert result["unfilled_request_amount"] == pytest.approx(6_000)
    assert result["gross_quote"] == pytest.approx(4_000)
    assert result["fees_quote"] == pytest.approx(8)
    assert result["net_quote"] == pytest.approx(3_992)
    assert result["quality_flags"] == ["partial_fill"]


def test_concentration_cap_is_aggregate_across_multiple_instruments_at_one_venue() -> None:
    result = calculate_executable_composite(
        _route_payload(
            requested_amount=100,
            max_venue_share=0.5,
            books=[
                _book("venue_a", "AAPL-USD-A1", "buy", [{"price": 98, "size": 40}]),
                _book("venue_a", "AAPL-USD-A2", "buy", [{"price": 99, "size": 40}]),
                _book("venue_b", "AAPL-USD-B", "buy", [{"price": 100, "size": 50}]),
            ],
        )
    )

    assert result["status"] == "full_fill"
    assert result["venue_request_fills"] == {"venue_a": 50.0, "venue_b": 50.0}


def test_router_rejects_symbol_only_or_mismatched_identity() -> None:
    symbol_only = _book("venue_a", "AAPL-USD-A", "buy", [{"price": 100, "size": 1}])
    symbol_only.pop("canonical_asset_id")
    symbol_only["symbol"] = "AAPL"
    with pytest.raises(ValueError, match="symbol-only joins are not allowed"):
        calculate_executable_composite(_route_payload(requested_amount=1, books=[symbol_only]))

    mismatch = _book("venue_a", "AAPL-USD-A", "buy", [{"price": 100, "size": 1}])
    mismatch["canonical_asset_id"] = "US5949181045"
    with pytest.raises(ValueError, match="identity mismatch"):
        calculate_executable_composite(_route_payload(requested_amount=1, books=[mismatch]))


def _reference(
    source_id: str,
    lineage_group: str,
    value: float,
    **overrides: object,
) -> dict[str, object]:
    return {
        **IDENTITY,
        "instrument_id": f"REF-{source_id}",
        "source_id": source_id,
        "lineage_group": lineage_group,
        "source_kind": "oracle_reference",
        "value": value,
        "quality_weight": 1,
        "event_time": "2026-07-16T11:59:58+00:00",
        "rights_status": "approved_internal",
        **overrides,
    }


def test_reference_composite_deduplicates_lineage_and_rejects_outlier_and_cycle() -> None:
    result = calculate_reference_composite(
        {
            **IDENTITY,
            "composite_id": "blocksize:aapl-usd:reference",
            "now": NOW,
            "max_age_ms": 5_000,
            "min_independent_sources": 3,
            "max_source_weight": 0.4,
            "allowed_rights_statuses": RIGHTS,
            "observations": [
                _reference("publisher_a", "shared_exchange_x", 100.0),
                _reference("publisher_b", "shared_exchange_x", 100.2),
                _reference("publisher_c", "independent_y", 99.9),
                _reference("publisher_d", "independent_z", 100.1),
                _reference("bad", "independent_outlier", 150),
                _reference(
                    "cycle",
                    "circular",
                    100,
                    upstream_composite_ids=["blocksize:aapl-usd:reference"],
                ),
            ],
        }
    )

    assert result["price_type"] == "robust_reference"
    assert result["status"] == "valid_reference"
    assert result["independent_source_count"] == 3
    assert result["reference_price"] == pytest.approx(100.1)
    shared = next(row for row in result["lineage_observations"] if row["lineage_group"] == "shared_exchange_x")
    assert shared["source_ids"] == ["publisher_a", "publisher_b"]
    assert shared["value"] == pytest.approx(100.1)
    included_weights = [
        row["normalized_weight"] for row in result["lineage_observations"] if row["included"]
    ]
    assert sum(included_weights) == pytest.approx(1)
    assert max(included_weights) <= 0.4
    assert {row["reason"] for row in result["excluded_observations"]} == {
        "circular_dependency",
        "mad_outlier",
    }


def test_two_sided_snapshot_keeps_reference_separate_and_halts_crossed_market() -> None:
    result = build_blocksize_oracle_snapshot(
        {
            **IDENTITY,
            "composite_id": "blocksize:aapl-usd",
            "request_kind": "base_quantity",
            "requested_amount": 10,
            "now": NOW,
            "max_age_ms": 5_000,
            "allowed_rights_statuses": RIGHTS,
            "buy_books": [_book("venue_a", "AAPL-USD-A", "buy", [{"price": 99, "size": 10}])],
            "sell_books": [_book("venue_b", "AAPL-USD-B", "sell", [{"price": 101, "size": 10}])],
            "reference_observations": [
                _reference("publisher_a", "lineage_a", 100),
                _reference("publisher_b", "lineage_b", 100.1),
            ],
        }
    )

    assert result["executable"]["status"] == "halt_crossed_composite"
    assert "crossed_composite" in result["executable"]["quality_flags"]
    assert result["reference"]["status"] == "valid_reference"
    assert result["reference"]["price_type"] == "robust_reference"
    assert result["separation_policy"] == "reference observations never fill executable routes"
