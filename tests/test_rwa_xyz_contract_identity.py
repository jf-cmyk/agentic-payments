from copy import deepcopy

from src.rwa_xyz_monitor import (
    build_rwa_xyz_monitor_report,
    load_rwa_xyz_monitor_report,
    reclassify_rwa_xyz_monitor_report,
    rwa_xyz_contract_identity_quality_summary,
)


def _asset(source_id: str, token_id: str, address: str, *, network: str = "Ethereum"):
    return {
        "id": source_id,
        "asset_class_name": "Stocks",
        "name": f"Asset {source_id}",
        "ticker": f"T{source_id}",
        "tokens": [
            {
                "id": token_id,
                "address": address,
                "network": {"name": network, "slug": network.lower().replace(" ", "-")},
                "platform": {"name": "Test Platform", "slug": "test-platform"},
            }
        ],
    }


def test_same_source_duplicate_contract_listings_are_preserved_and_non_blocking():
    asset = _asset("asset-1", "token-1", "0xAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA")
    asset["tokens"].append(
        deepcopy(
            {
                **asset["tokens"][0],
                "id": "token-2",
                "address": "0xaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            }
        )
    )

    report = build_rwa_xyz_monitor_report([asset])
    quality = report["summary"]["contract_identity_quality"]

    assert report["summary"]["token_count"] == 2
    assert len(report["token_rows"]) == 2
    assert {row["rwa_xyz_token_id"] for row in report["token_rows"]} == {
        "token-1",
        "token-2",
    }
    assert quality["input_token_row_count"] == 2
    assert quality["preserved_token_row_count"] == 2
    assert quality["unique_contract_identity_count"] == 1
    assert quality["duplicate_contract_identity_group_count"] == 1
    assert quality["duplicate_contract_identity_row_count"] == 2
    assert quality["duplicate_contract_identity_excess_row_count"] == 1
    assert quality["benign_duplicate_source_listing_group_count"] == 1
    assert quality["cross_asset_contract_collision_group_count"] == 0
    assert quality["decision_grade_acceptance"]["status"] == "pass"
    assert quality["decision_grade_acceptance"]["accepted"] is True


def test_cross_asset_contract_collision_is_a_decision_grade_blocker():
    address = "0xBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
    report = build_rwa_xyz_monitor_report(
        [
            _asset("asset-a", "token-a", address),
            _asset("asset-b", "token-b", address.casefold()),
        ]
    )
    quality = report["summary"]["contract_identity_quality"]

    assert len(report["token_rows"]) == 2
    assert quality["unique_contract_identity_count"] == 1
    assert quality["cross_asset_contract_collision_group_count"] == 1
    assert quality["cross_asset_contract_collision_row_count"] == 2
    assert quality["benign_duplicate_source_listing_group_count"] == 0
    assert quality["decision_grade_acceptance"]["status"] == "blocked"
    assert quality["decision_grade_acceptance"]["accepted"] is False
    blocker = quality["decision_grade_acceptance"]["blockers"][0]
    assert blocker["blocker_type"] == "cross_asset_contract_collision"
    assert blocker["rwa_xyz_asset_ids"] == ["asset-a", "asset-b"]
    assert blocker["token_row_ids"] == [
        "rwa_xyz:asset-a:token-a",
        "rwa_xyz:asset-b:token-b",
    ]


def test_contract_identity_is_network_scoped_and_non_hex_address_case_is_preserved():
    token_rows = [
        {
            "token_row_id": "ethereum",
            "rwa_xyz_asset_id": "a",
            "network_slug": "ethereum",
            "address": "0xCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCCC",
        },
        {
            "token_row_id": "base",
            "rwa_xyz_asset_id": "b",
            "network_slug": "base",
            "address": "0xcccccccccccccccccccccccccccccccccccccccc",
        },
        {
            "token_row_id": "sol-upper",
            "rwa_xyz_asset_id": "c",
            "network_slug": "solana",
            "address": "AbCd",
        },
        {
            "token_row_id": "sol-lower",
            "rwa_xyz_asset_id": "d",
            "network_slug": "solana",
            "address": "abcd",
        },
    ]

    quality = rwa_xyz_contract_identity_quality_summary(token_rows)

    assert quality["unique_contract_identity_count"] == 4
    assert quality["duplicate_contract_identity_group_count"] == 0
    assert quality["decision_grade_acceptance"]["status"] == "pass"


def test_incomplete_contract_identity_fails_closed():
    token_rows = [
        {
            "token_row_id": "missing-address",
            "rwa_xyz_asset_id": "asset-a",
            "network_slug": "ethereum",
            "address": "",
        }
    ]

    quality = rwa_xyz_contract_identity_quality_summary(token_rows)

    assert quality["missing_address_row_count"] == 1
    assert quality["decision_grade_acceptance"]["status"] == "blocked"
    assert quality["decision_grade_acceptance"]["blockers"] == [
        {
            "blocker_type": "incomplete_contract_identity",
            "invalid_token_row_count": 0,
            "incomplete_token_row_count": 1,
            "token_row_ids": ["missing-address"],
        }
    ]


def test_invalid_token_row_fails_closed_and_is_counted_without_being_dropped():
    quality = rwa_xyz_contract_identity_quality_summary(["invalid-source-row"])

    assert quality["input_token_row_count"] == 1
    assert quality["preserved_token_row_count"] == 1
    assert quality["invalid_token_row_count"] == 1
    assert quality["decision_grade_acceptance"]["status"] == "blocked"
    assert quality["decision_grade_acceptance"]["blockers"] == [
        {
            "blocker_type": "incomplete_contract_identity",
            "invalid_token_row_count": 1,
            "incomplete_token_row_count": 0,
            "token_row_ids": [],
        }
    ]


def test_renormalization_recomputes_contract_quality_without_changing_snapshot_time():
    captured = {
        "generated_at": "2026-07-30T15:12:10.896537+00:00",
        "source": {"fetched_at": "2026-07-30T15:12:10.892971+00:00"},
        "summary": {},
        "source_assessment": {},
        "asset_rows": [],
        "token_rows": [
            {
                "token_row_id": "rwa_xyz:asset-1:token-1",
                "rwa_xyz_asset_id": "asset-1",
                "rwa_xyz_token_id": "token-1",
                "rwa_xyz_asset_class": "Stocks",
                "rwa_xyz_ticker": "TEST",
                "network": "Ethereum",
                "network_slug": "ethereum",
                "address": "0xdddddddddddddddddddddddddddddddddddddddd",
            }
        ],
        "coverage_rows": [],
    }

    updated = reclassify_rwa_xyz_monitor_report(captured)

    assert len(updated["token_rows"]) == 1
    assert updated["summary"]["contract_identity_quality"][
        "preserved_token_row_count"
    ] == 1
    assert updated["summary"]["contract_identity_quality"][
        "decision_grade_acceptance"
    ]["status"] == "pass"
    assert updated["generated_at"] == captured["generated_at"]
    assert updated["source"]["fetched_at"] == captured["source"]["fetched_at"]


def test_checked_in_snapshot_contract_identity_acceptance_is_exact_and_lossless():
    report = load_rwa_xyz_monitor_report()
    quality = report["summary"]["contract_identity_quality"]

    assert report["generated_at"] == "2026-07-30T15:12:10.896537+00:00"
    assert report["source"]["fetched_at"] == "2026-07-30T15:12:10.892971+00:00"
    assert report["summary"]["token_count"] == 3438
    assert len(report["token_rows"]) == 3438
    assert quality["input_token_row_count"] == 3438
    assert quality["preserved_token_row_count"] == 3438
    assert quality["normalized_contract_identity_row_count"] == 3438
    assert quality["unique_contract_identity_count"] == 3435
    assert quality["duplicate_contract_identity_group_count"] == 3
    assert quality["duplicate_contract_identity_row_count"] == 6
    assert quality["duplicate_contract_identity_excess_row_count"] == 3
    assert quality["benign_duplicate_source_listing_group_count"] == 3
    assert quality["cross_asset_contract_collision_group_count"] == 0
    assert quality["decision_grade_acceptance"]["status"] == "pass"
    assert [
        group["rwa_xyz_asset_ids"]
        for group in quality["benign_duplicate_source_listing_groups"]
    ] == [["5172"], ["35233"], ["27"]]
