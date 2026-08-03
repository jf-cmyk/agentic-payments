"""Acceptance coverage for underlying identity versus contract semantics."""

from __future__ import annotations

from collections import Counter

from src.rwa_asset_identity import build_rwa_ticker_identity_audit
from src.rwa_coverage import (
    _identity_quality_summary,
    build_rwa_asset_matrix,
    build_rwa_coverage_overview,
)
from src.rwa_derivative_venues import (
    _derivative_identity_quality,
    load_derivative_venue_discovery_report,
    reclassify_derivative_venue_discovery_report,
)
from src.rwa_symbol_registry import (
    build_rwa_symbol_registry,
    build_rwa_venue_registry,
    resolve_rwa_symbol,
)


def _asset(matrix: dict, asset_id: str) -> dict:
    return next(
        row for row in matrix["assets"] if row["asset_id"] == asset_id
    )


def _instruments(asset: dict) -> list[dict]:
    return [
        instrument
        for venue in asset["venues"].values()
        for instrument in venue["instruments"]
    ]


def test_full_snapshot_is_lossless_and_cross_class_acceptance_passes() -> None:
    matrix = build_rwa_asset_matrix()
    quality = matrix["summary"]["identity_quality"]

    assert matrix["summary"]["coverage_row_count"] == 5_161
    assert matrix["summary"]["nested_instrument_count"] == 5_161
    assert matrix["summary"]["canonical_asset_count"] == 2_139
    assert quality["raw_mixed_class_asset_id_count"] == 55
    assert quality["canonical_mixed_class_asset_id_count"] == 0
    assert quality["decision_grade_mixed_class_asset_id_count"] == 0
    assert quality["decision_grade_canonical_asset_count"] == 104
    assert quality["manual_verification_asset_count"] == 2_035
    assert quality["ambiguous_source_scoped_asset_count"] == 2
    assert quality["acceptance"]["status"] == "pass"


def test_known_security_and_commodity_underlyings_reconcile_raw_classes() -> None:
    matrix = build_rwa_asset_matrix()
    expected = {
        "SNDK": ("equity", {"crypto", "equity"}),
        "BZ": ("commodity", {"commodity", "crypto", "equity"}),
        "XAG": ("metal", {"commodity", "equity", "metal"}),
        "XAU": ("metal", {"commodity", "equity", "metal"}),
        "QQQ": ("etf", {"etf", "index"}),
        "SPY": ("etf", {"etf", "index"}),
        "IWM": ("etf", {"etf", "index"}),
    }

    for asset_id, (underlying_class, raw_classes) in expected.items():
        asset = _asset(matrix, asset_id)
        assert asset["canonical_underlying_asset_class"] == underlying_class
        assert asset["asset_classes"] == [underlying_class]
        assert set(asset["raw_source_asset_classes"]) == raw_classes
        assert asset["decision_grade"] is True
        assert all(
            row["underlying_asset_class"] == underlying_class
            for row in _instruments(asset)
        )


def test_option_is_contract_type_not_btc_underlying_asset_class() -> None:
    matrix = build_rwa_asset_matrix()
    btc = _asset(matrix, "BTC")
    instruments = _instruments(btc)

    assert btc["asset_classes"] == ["crypto"]
    assert btc["canonical_underlying_asset_class"] == "crypto"
    assert btc["instrument_count"] == 843
    assert Counter(row["contract_type"] for row in instruments) == Counter(
        {"option": 833, "perpetual": 10}
    )
    option = next(row for row in instruments if row["contract_type"] == "option")
    assert option["asset_class"] == "crypto"
    assert option["underlying_asset_class"] == "crypto"
    assert option["metadata"]["raw_instrument_type"].lower() in {
        "option",
        "perp,option,erc20",
    }


def test_tokenized_gold_option_contracts_retain_metal_underlying() -> None:
    matrix = build_rwa_asset_matrix()

    xaut = _asset(matrix, "XAUT")
    xaut0 = _asset(matrix, "XAUT0")
    assert xaut["canonical_underlying_asset_class"] == "metal"
    assert xaut["raw_source_asset_classes"] == ["option"]
    assert {row["contract_type"] for row in _instruments(xaut)} == {"option"}
    assert xaut0["canonical_underlying_asset_class"] == "metal"
    assert all(
        row["underlying_asset_class"] == "metal"
        for row in _instruments(xaut0)
    )

    overview = build_rwa_coverage_overview(include_symbols=True)
    assert not any(row["asset_class"] == "option" for row in overview["symbols"])
    assert sum(
        row["contract_type"] == "option" for row in overview["symbols"]
    ) == 1_855


def test_legacy_option_filter_selects_contracts_without_relabeling_underlying() -> None:
    response = build_rwa_coverage_overview(
        asset_class="option",
        limit=10,
    )

    assert response["coverage_summary"]["coverage_row_count"] == 1_855
    assert len(response["symbols"]) == 10
    assert all(row["contract_type"] == "option" for row in response["symbols"])
    assert all(row["asset_class"] == "crypto" for row in response["symbols"])


def test_ambiguous_cat_bare_ticker_is_source_scoped_and_fail_closed() -> None:
    matrix = build_rwa_asset_matrix()
    equity = _asset(matrix, "CAT")
    token = _asset(matrix, "HYPERLIQUID_SPOT_CAT_126")

    assert equity["canonical_underlying_asset_class"] == "equity"
    assert equity["decision_grade"] is True
    assert token["raw_source_asset_ids"] == ["CAT"]
    assert token["canonical_underlying_asset_class"] == "unknown"
    assert token["identity_status"] == "source_scoped_ambiguous"
    assert token["decision_grade"] is False
    assert token["manual_verification_required"] is True

    unresolved = resolve_rwa_symbol("CAT")
    assert unresolved["match_count"] == 2
    assert {row["asset_id"] for row in unresolved["matches"]} == {
        "CAT",
        "HYPERLIQUID_SPOT_CAT_126",
    }
    ostium = resolve_rwa_symbol("CAT/USD", venue="ostium")
    assert [row["asset_id"] for row in ostium["matches"]] == ["CAT"]


def test_unverified_spcx_token_does_not_merge_with_documented_equity() -> None:
    matrix = build_rwa_asset_matrix()
    equity = _asset(matrix, "SPCX")
    token = _asset(matrix, "HYPERLIQUID_SPOT_SPCX_590")

    assert equity["canonical_underlying_asset_class"] == "equity"
    assert equity["instrument_count"] == 6
    assert token["canonical_underlying_asset_class"] == "unknown"
    assert token["instrument_count"] == 2
    assert token["decision_grade"] is False


def test_full_market_suffix_ids_are_not_collapsed_to_shared_source_bases() -> None:
    """Guard the 29-to-14 collapse that caused the transient net -15 drift."""
    matrix = build_rwa_asset_matrix()
    asset_ids = {row["asset_id"] for row in matrix["assets"]}
    full_market_ids = {
        "BREAKPOINT-IGGYERIC",
        "DEMOCRATS-WIN-MICHIGAN",
        "FED-CUT-50-SEPT-2024",
        "JITOSOL-2",
        "JITOSOL-3",
        "JLP-1",
        "JTO-2",
        "JTO-3",
        "KAMALA-POPULAR-VOTE-2024",
        "LANDO-F1-SGP-WIN",
        "LNDO-WIN-F1-24-US-GP",
        "METAMASK_FDV_ABOVEBDAY_AFTER_LAUNCH",
        "NBAFINALS25-BOS",
        "NBAFINALS25-OKC",
        "PT-DSOL-30JUN25-3",
        "PT-FRAGSOL-10JUL25-3",
        "PT-FRAGSOL-31OCT25-3",
        "PT-KYSOL-15JUN25-3",
        "REPUBLICAN-POPULAR-AND-WIN",
        "SOL-2",
        "SUPERBOWL-LIX-CHIEFS",
        "SUPERBOWL-LIX-LIONS",
        "TRUMP-WIN-2024",
        "USDC-1",
        "USDC-4",
        "USDT-4",
        "VRSTPN-WIN-F1-24-DRVRS-CHMP",
        "WARWICK-FIGHT-WIN",
        "WLF-5B-1W",
    }

    assert len(full_market_ids) == 29
    assert len({asset_id.casefold() for asset_id in full_market_ids}) == 29
    assert full_market_ids <= asset_ids
    assert _asset(matrix, "NBAFINALS25-BOS")["raw_source_asset_ids"] == [
        "NBAFINALS25"
    ]
    assert _asset(matrix, "NBAFINALS25-OKC")["raw_source_asset_ids"] == [
        "NBAFINALS25"
    ]
    assert _asset(matrix, "JITOSOL-2")["raw_source_asset_ids"] == [
        "JITOSOL"
    ]
    assert _asset(matrix, "JITOSOL-3")["raw_source_asset_ids"] == [
        "JITOSOL"
    ]


def test_captured_derivative_artifact_is_offline_idempotent_and_lossless() -> None:
    report = load_derivative_venue_discovery_report()
    quality = report["summary"]["identity_quality"]

    assert report["generated_at"] == "2026-07-13T17:28:17.216493+00:00"
    assert report["summary"]["coverage_row_count"] == 3_264
    assert report["summary"]["market_row_count"] == 3_714
    assert report["summary"]["by_contract_type"]["option"] == 1_855
    assert quality["raw_mixed_class_asset_id_count"] == 28
    assert quality["canonical_mixed_class_asset_id_count"] == 0
    assert quality["decision_grade_mixed_class_asset_id_count"] == 0
    assert quality["acceptance"]["status"] == "pass"
    assert reclassify_derivative_venue_discovery_report(report) == report


def test_synthetic_decision_grade_class_collision_blocks_acceptance() -> None:
    rows = [
        {
            "raw_source_asset_id": "COLLIDE",
            "raw_source_asset_class": "equity",
            "asset_id": "COLLIDE",
            "asset_class": "equity",
            "underlying_asset_class": "equity",
            "decision_grade": True,
            "identity_status": "verified_curated_underlying",
        },
        {
            "raw_source_asset_id": "COLLIDE",
            "raw_source_asset_class": "crypto",
            "asset_id": "COLLIDE",
            "asset_class": "crypto",
            "underlying_asset_class": "crypto",
            "decision_grade": True,
            "identity_status": "verified_curated_underlying",
        },
    ]

    for quality in (
        _identity_quality_summary(rows),
        _derivative_identity_quality(rows),
    ):
        assert quality["canonical_mixed_class_asset_id_count"] == 1
        assert quality["decision_grade_mixed_class_asset_id_count"] == 1
        assert quality["decision_grade_mixed_class_asset_ids"] == ["COLLIDE"]
        assert quality["acceptance"]["status"] == "blocked"


def test_identity_and_registry_consumers_expose_decision_grade_semantics() -> None:
    audit = build_rwa_ticker_identity_audit()
    registry = build_rwa_symbol_registry()
    venues = build_rwa_venue_registry()

    assert audit["summary"]["decision_grade_canonical_asset_count"] == 104
    assert audit["summary"]["manual_verification_asset_count"] == 2_035
    assert audit["summary"]["decision_grade_mixed_class_asset_id_count"] == 0
    assert registry["summary"]["decision_grade_canonical_asset_count"] == 104
    assert registry["summary"]["manual_verification_asset_count"] == 2_035
    assert registry["summary"]["decision_grade_mixed_class_asset_id_count"] == 0
    assert venues["summary"]["decision_grade_mixed_class_asset_id_count"] == 0

    sndk = next(row for row in registry["assets"] if row["asset_id"] == "SNDK")
    assert sndk["canonical_underlying_asset_class"] == "equity"
    assert sndk["raw_source_asset_classes"] == ["crypto", "equity"]
    aevo = next(row for row in venues["venues"] if row["venue_id"] == "aevo")
    btc_option = next(
        row
        for row in aevo["assets"]
        if row["asset_id"] == "BTC" and row["contract_type"] == "option"
    )
    assert btc_option["underlying_asset_class"] == "crypto"
    assert btc_option["raw_source_asset_class"] == "option"
