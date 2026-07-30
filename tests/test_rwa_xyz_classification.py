import pytest

from src.rwa_xyz_monitor import (
    _write_csv,
    build_rwa_xyz_monitor_report,
    load_rwa_xyz_monitor_report,
    normalize_rwa_xyz_asset_class,
    normalize_rwa_xyz_asset_row,
    reclassify_rwa_xyz_monitor_report,
)


RWA_XYZ_CLASSIFICATION_FIXTURES = (
    pytest.param(
        {
            "id": "commodity-oil",
            "asset_class_name": "Commodities",
            "name": "United States Oil Fund",
            "ticker": "USO",
            "tokens": [],
        },
        "commodity",
        id="commodity",
    ),
    pytest.param(
        {
            "id": "metal-platinum",
            "asset_class_name": "Commodities",
            "name": "Auxite Platinum Gram",
            "ticker": "AUXPT",
            "tokens": [],
        },
        "metal",
        id="metal",
    ),
    pytest.param(
        {
            "id": "us-treasury",
            "asset_class_name": "US Treasury Debt",
            "name": "Ondo Short-Term U.S. Government Bond Fund",
            "ticker": "OUSG",
            "tokens": [],
        },
        "treasury_fund",
        id="us-treasury",
    ),
    pytest.param(
        {
            "id": "non-us-government-debt",
            "asset_class_name": "non-US Government Debt",
            "name": "Non-US Government Bond Fund",
            "ticker": "EUBOND",
            "tokens": [],
        },
        "sovereign_debt",
        id="non-us-government-debt",
    ),
)


@pytest.mark.parametrize(("asset", "expected"), RWA_XYZ_CLASSIFICATION_FIXTURES)
def test_rwa_xyz_source_classes_normalize_without_substring_collisions(
    asset: dict,
    expected: str,
):
    assert normalize_rwa_xyz_asset_class(asset) == expected


def test_rwa_xyz_report_preserves_all_explicit_classifications():
    assets = [parameter.values[0] for parameter in RWA_XYZ_CLASSIFICATION_FIXTURES]

    report = build_rwa_xyz_monitor_report(assets)

    assert report["summary"]["by_asset_class"] == {
        "commodity": 1,
        "metal": 1,
        "treasury_fund": 1,
        "sovereign_debt": 1,
    }
    assert {row["asset_class"] for row in report["coverage_rows"]} == {
        "commodity",
        "metal",
        "treasury_fund",
        "sovereign_debt",
    }


def test_captured_report_rows_can_be_reclassified_without_refetching():
    report = {
        "summary": {"by_asset_class": {"tokenized_fund": 2}},
        "asset_rows": [
            {
                "asset_class": "tokenized_fund",
                "rwa_xyz_asset_class": "Commodities",
                "name": "Auxite Gold",
                "rwa_xyz_ticker": "AUXG",
            },
            {
                "asset_class": "treasury_fund",
                "rwa_xyz_asset_class": "non-US Government Debt",
                "name": "Mexican Government Bond",
                "rwa_xyz_ticker": "MXBOND",
            },
        ],
        "token_rows": [],
        "coverage_rows": [
            {
                "asset_class": "tokenized_fund",
                "symbol": "AUXG/USD",
                "metadata": {
                    "asset_name": "Auxite Gold",
                    "rwa_xyz_asset_class": "Commodities",
                    "rwa_xyz_ticker": "AUXG",
                },
            }
        ],
    }

    updated = reclassify_rwa_xyz_monitor_report(report)

    assert updated["summary"]["by_asset_class"] == {
        "metal": 1,
        "sovereign_debt": 1,
    }
    assert updated["coverage_rows"][0]["asset_class"] == "metal"
    assert report["coverage_rows"][0]["asset_class"] == "tokenized_fund"


@pytest.mark.parametrize("ticker", ("SPYx", "QQQx", "IWMx", "IEMGx"))
def test_known_etf_wrappers_are_classified_as_etfs(ticker: str):
    asset = {
        "id": f"source-{ticker}",
        "asset_class_name": "Stocks",
        "name": f"Tokenized {ticker}",
        "ticker": ticker,
        "tokens": [],
    }

    row = normalize_rwa_xyz_asset_row(asset)

    assert row["asset_class"] == "etf"
    assert row["asset_id"] == ticker.removesuffix("x")
    assert row["identity_mapping_status"] == "high"
    assert row["identity_verification_status"] == "verified"
    assert row["canonical_underlying_candidate"] == ticker.removesuffix("x")


def test_unverified_bare_tickers_are_source_scoped_and_not_canonical():
    first = normalize_rwa_xyz_asset_row(
        {
            "id": "stock-dash",
            "asset_class_name": "Stocks",
            "name": "DoorDash",
            "ticker": "DASH",
            "tokens": [],
        }
    )
    second = normalize_rwa_xyz_asset_row(
        {
            "id": "fund-dash",
            "asset_class_name": "Active Strategies",
            "name": "Unrelated DASH Fund",
            "ticker": "DASH",
            "tokens": [],
        }
    )

    assert first["asset_id"] == "RWA_XYZ_stockdash"
    assert second["asset_id"] == "RWA_XYZ_funddash"
    assert first["asset_id"] != second["asset_id"]
    assert first["identity_mapping_status"] == "issuer_symbol_only"
    assert first["identity_verification_status"] == "unverified"
    assert first["canonical_underlying_candidate"] is None
    assert second["canonical_underlying_candidate"] is None


def test_unverified_cross_class_ticker_is_not_a_mixed_identity():
    report = build_rwa_xyz_monitor_report(
        [
            {
                "id": "equity-cper",
                "asset_class_name": "Stocks",
                "name": "Unrelated CPER Equity",
                "ticker": "CPER",
                "tokens": [],
            },
            {
                "id": "metal-cper",
                "asset_class_name": "Commodities",
                "name": "United States Copper Index Fund",
                "ticker": "CPER",
                "tokens": [],
            },
        ]
    )

    quality = report["summary"]["identity_quality"]
    assert quality["denominator_asset_count"] == 2
    assert quality["verified_asset_count"] == 0
    assert quality["unverified_asset_count"] == 2
    assert quality["mixed_class_asset_id_count"] == 0
    assert quality["decision_grade_mixed_class_asset_id_count"] == 0


def test_yield_metrics_have_numeric_values_units_bases_and_raw_trends():
    ytm = {"val": 0.041, "val_30d": 0.039}
    apy = {"val": 4.3, "val_30d": 4.1}

    row = normalize_rwa_xyz_asset_row(
        {
            "id": "yield-asset",
            "asset_class_name": "Active Strategies",
            "name": "Yield Asset",
            "ticker": "YIELD",
            "yield_to_maturity_percent": ytm,
            "apy_30_day": apy,
            "tokens": [],
        }
    )

    assert row["yield_to_maturity_value"] == pytest.approx(0.041)
    assert row["yield_to_maturity_unit"] == "decimal_fraction"
    assert row["yield_to_maturity_basis"] == "annualized_yield_to_maturity"
    assert row["yield_to_maturity_raw_trend"] == ytm
    assert row["apy_30_day_value"] == pytest.approx(4.3)
    assert row["apy_30_day_unit"] == "percentage_points"
    assert row["apy_30_day_basis"] == "annual_percentage_yield_trailing_30_day"
    assert row["apy_30_day_raw_trend"] == apy
    assert row["yield_to_maturity_percent"] == ytm
    assert row["apy_30_day"] == apy


@pytest.mark.parametrize("invalid", ("not-a-number", float("nan"), float("inf")))
def test_non_finite_or_invalid_yields_are_not_promoted_to_numeric_values(invalid):
    row = normalize_rwa_xyz_asset_row(
        {
            "id": "invalid-yield-asset",
            "asset_class_name": "Active Strategies",
            "name": "Invalid Yield Asset",
            "ticker": "BADYIELD",
            "yield_to_maturity_percent": {"val": invalid},
            "apy_30_day": {"val": invalid},
            "tokens": [],
        }
    )

    assert row["yield_to_maturity_value"] is None
    assert row["apy_30_day_value"] is None


def test_captured_report_normalization_is_idempotent_and_preserves_snapshot_time():
    captured = {
        "generated_at": "2026-07-30T15:12:10.896537+00:00",
        "source": {"fetched_at": "2026-07-30T15:12:10.892971+00:00"},
        "summary": {},
        "source_assessment": {},
        "asset_rows": [
            {
                "rwa_xyz_asset_id": "source-1",
                "rwa_xyz_asset_class": "Stocks",
                "rwa_xyz_ticker": "SPYx",
                "name": "SPDR S&P 500 ETF",
                "yield_to_maturity_percent": None,
                "apy_30_day": {"val": 4.2},
            }
        ],
        "token_rows": [],
        "coverage_rows": [],
    }

    once = reclassify_rwa_xyz_monitor_report(captured)
    twice = reclassify_rwa_xyz_monitor_report(once)

    assert twice == once
    assert once["generated_at"] == captured["generated_at"]
    assert once["source"]["fetched_at"] == captured["source"]["fetched_at"]


def test_checked_in_snapshot_has_identity_and_unit_acceptance_evidence():
    report = load_rwa_xyz_monitor_report()
    unverified = [
        row
        for row in report["asset_rows"]
        if row["identity_verification_status"] == "unverified"
    ]

    assert report["generated_at"] == "2026-07-30T15:12:10.896537+00:00"
    assert report["source"]["fetched_at"] == "2026-07-30T15:12:10.892971+00:00"
    assert report["summary"]["asset_count"] == 1169
    quality = report["summary"]["identity_quality"]
    assert quality["verified_asset_count"] == 93
    assert quality["unverified_asset_count"] == 1076
    assert quality["denominator_asset_count"] == 1169
    assert quality["mixed_class_asset_id_count"] == 0
    assert quality["decision_grade_mixed_class_asset_id_count"] == 0
    assert len({row["asset_id"] for row in unverified}) == 1076
    assert all(row["asset_id"].startswith("RWA_XYZ_") for row in unverified)
    assert all(row["canonical_underlying_candidate"] is None for row in unverified)
    assert report["summary"]["yield_metric_quality"]["yield_to_maturity"][
        "unit"
    ] == "decimal_fraction"
    assert report["summary"]["yield_metric_quality"]["yield_to_maturity"][
        "populated_asset_count"
    ] == 24
    assert report["summary"]["yield_metric_quality"]["apy_30_day"][
        "populated_asset_count"
    ] == 175


def test_csv_report_serialization_is_key_order_independent(tmp_path):
    first_path = tmp_path / "first.csv"
    second_path = tmp_path / "second.csv"
    rows = [{"b": 2, "a": 1}, {"a": 3, "b": 4}]
    reordered_rows = [dict(reversed(list(row.items()))) for row in rows]

    _write_csv(first_path, rows)
    _write_csv(second_path, reordered_rows)

    assert first_path.read_bytes() == second_path.read_bytes()
    assert first_path.read_text(encoding="utf-8").splitlines()[0] == "a,b"
