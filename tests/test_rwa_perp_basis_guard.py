from src.rwa_consensus import calculate_consensus_metric
from src.rwa_pricing import calculate_perp_basis_guard


NOW = "2026-07-16T18:28:00+00:00"
FRESH = "2026-07-16T18:27:59+00:00"


def test_raw_perp_premium_is_excluded_from_spot_vwap() -> None:
    result = calculate_perp_basis_guard(
        {
            "symbol": "AAPL",
            "asset_class": "equity",
            "venue": "aster",
            "perp_price": 101,
            "spot_anchor_price": 100,
        }
    )

    assert result["basis_bps"] == 100
    assert result["basis_direction"] == "premium"
    assert result["raw_perp_allowed_in_spot_vwap"] is False
    assert result["include_in_spot_vwap"] is False
    assert result["status"] == "exclude_derivative_premium"


def test_consensus_excludes_unadjusted_perp_before_weighting() -> None:
    result = calculate_consensus_metric(
        {
            "symbol": "AAPL",
            "asset_class": "equity",
            "now": NOW,
            "benchmark_price": 100,
            "observations": [
                {"venue": "spot_a", "source_type": "native_l2", "value": 100, "timestamp": FRESH},
                {
                    "venue": "aster",
                    "source_type": "native_l2",
                    "market_type": "perp",
                    "value": 101,
                    "timestamp": FRESH,
                },
            ],
        }
    )

    perp_row = result["observations"][1]
    assert perp_row["include_in_consensus"] is False
    assert perp_row["base_weight"] == 0
    assert perp_row["perp_basis_status"] == "exclude_derivative_premium"
    assert "raw_perp_not_spot" in perp_row["flags"]


def test_basis_adjusted_perp_is_capped_when_residual_basis_passes() -> None:
    result = calculate_consensus_metric(
        {
            "symbol": "AAPL",
            "asset_class": "equity",
            "now": NOW,
            "benchmark_price": 100,
            "observations": [
                {"venue": "spot_a", "source_type": "native_l2", "value": 100, "timestamp": FRESH},
                {
                    "venue": "aster",
                    "source_type": "native_l2",
                    "market_type": "perp",
                    "value": 100.1,
                    "basis_adjusted": True,
                    "timestamp": FRESH,
                },
            ],
        }
    )

    perp_row = result["observations"][1]
    assert perp_row["include_in_consensus"] is True
    assert perp_row["raw_perp_allowed_in_spot_vwap"] is False
    assert perp_row["perp_basis_status"] == "basis_adjusted_pass_cap_weight"
    assert perp_row["base_weight"] == 0.15
