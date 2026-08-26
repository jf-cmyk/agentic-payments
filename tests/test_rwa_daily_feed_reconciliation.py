import json
from copy import deepcopy

import pytest

import src.rwa_daily_feed_agent as daily_agent
from src.rwa_daily_feed_agent import (
    build_daily_feed_agent_report,
    load_daily_feed_agent_report,
    rwa_xyz_monitor_snapshot_identity,
    validate_daily_feed_agent_report,
    write_daily_feed_agent_baseline,
    write_daily_feed_agent_report,
)


def _asset(asset_id: str) -> dict:
    return {
        "rwa_xyz_asset_id": asset_id,
        "asset_id": f"RWA_XYZ_{asset_id}",
        "symbol": f"ASSET{asset_id}/USD",
        "asset_class": "tokenized_fund",
        "created_at": "2026-07-30T10:00:00+00:00",
    }


def _token(asset_id: str, address: str) -> dict:
    return {
        "rwa_xyz_asset_id": asset_id,
        "rwa_xyz_token_id": f"token-{asset_id}",
        "token_row_id": f"ethereum:{address}",
        "asset_id": f"RWA_XYZ_{asset_id}",
        "symbol": f"ASSET{asset_id}/USD",
        "asset_class": "tokenized_fund",
        "network": "Ethereum",
        "network_slug": "ethereum",
        "platform": "Example",
        "address": address,
        "standards": ["ERC-20"],
    }


def _monitor(
    *,
    generated_at: str,
    assets: list[dict],
    tokens: list[dict],
    build_id: str,
) -> dict:
    coverage = [
        {
            "venue": "rwa_xyz_new_asset_monitor",
            "asset_id": row["asset_id"],
            "symbol": row["symbol"],
        }
        for row in assets
    ]
    return {
        "generated_at": generated_at,
        "source": {
            "platform": "RWA.xyz",
            "fetched_at": generated_at,
            "next_build_id": build_id,
        },
        "summary": {
            "asset_count": len(assets),
            "token_count": len(tokens),
            "coverage_row_count": len(coverage),
        },
        "asset_rows": assets,
        "token_rows": tokens,
        "coverage_rows": coverage,
    }


def _write_json(path, payload: dict) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def test_report_reconciles_exact_current_snapshot_and_is_deterministic():
    previous = _monitor(
        generated_at="2026-07-29T12:00:00+00:00",
        assets=[_asset("1")],
        tokens=[_token("1", "0x01")],
        build_id="previous",
    )
    current = _monitor(
        generated_at="2026-07-30T12:00:00+00:00",
        assets=[_asset("1"), _asset("2")],
        tokens=[_token("1", "0x01"), _token("2", "0x02")],
        build_id="current",
    )

    first = build_daily_feed_agent_report(
        previous_report=previous,
        current_report=current,
        generated_at="2026-07-30T12:01:00+00:00",
    )
    second = build_daily_feed_agent_report(
        previous_report=deepcopy(previous),
        current_report=deepcopy(current),
        generated_at="2026-07-30T12:01:00+00:00",
    )
    default_first = build_daily_feed_agent_report(
        previous_report=previous,
        current_report=current,
    )
    default_second = build_daily_feed_agent_report(
        previous_report=deepcopy(previous),
        current_report=deepcopy(current),
    )

    assert first == second
    assert default_first == default_second
    assert default_first["generated_at"] == current["generated_at"]
    assert first["source_snapshot"] == rwa_xyz_monitor_snapshot_identity(current)
    assert first["summary"]["current_asset_count"] == current["summary"]["asset_count"]
    assert first["summary"]["current_token_count"] == current["summary"]["token_count"]
    assert (
        first["summary"]["current_coverage_row_count"]
        == current["summary"]["coverage_row_count"]
    )
    assert first["summary"]["alert_level"] == "new_p0_tokens"
    assert first["comparison"]["state"] == "verified_distinct_snapshots"
    assert validate_daily_feed_agent_report(first, current_report=current) == []


def test_no_new_feeds_requires_verified_distinct_snapshots():
    rows = [_asset("1")]
    tokens = [_token("1", "0x01")]
    previous = _monitor(
        generated_at="2026-07-29T12:00:00+00:00",
        assets=deepcopy(rows),
        tokens=deepcopy(tokens),
        build_id="previous",
    )
    current = _monitor(
        generated_at="2026-07-30T12:00:00+00:00",
        assets=deepcopy(rows),
        tokens=deepcopy(tokens),
        build_id="current",
    )

    report = build_daily_feed_agent_report(
        previous_report=previous,
        current_report=current,
        generated_at="2026-07-30T12:01:00+00:00",
    )
    unchanged = build_daily_feed_agent_report(
        previous_report=current,
        current_report=current,
        generated_at="2026-07-30T12:01:00+00:00",
    )

    assert report["summary"]["alert_level"] == "no_new_feeds"
    assert report["comparison"]["state"] == "verified_distinct_snapshots"
    assert unchanged["summary"]["alert_level"] == "snapshot_unchanged"
    assert unchanged["comparison"]["state"] == "snapshot_unchanged"


def test_mismatched_or_legacy_daily_evidence_fails_closed(tmp_path):
    original = _monitor(
        generated_at="2026-07-30T12:00:00+00:00",
        assets=[_asset("1")],
        tokens=[_token("1", "0x01")],
        build_id="original",
    )
    changed = _monitor(
        generated_at="2026-07-30T13:00:00+00:00",
        assets=[_asset("1"), _asset("2")],
        tokens=[_token("1", "0x01"), _token("2", "0x02")],
        build_id="changed",
    )
    canonical_path = tmp_path / "monitor.json"
    daily_path = tmp_path / "daily.json"
    report = build_daily_feed_agent_report(
        previous_report={},
        current_report=original,
        generated_at="2026-07-30T12:01:00+00:00",
        current_report_path=canonical_path,
    )
    _write_json(daily_path, report)
    _write_json(canonical_path, changed)

    rejected = load_daily_feed_agent_report(
        daily_path,
        current_report_path=canonical_path,
    )
    assert rejected["status"]["acceptance"] == "failed_closed"
    assert rejected["status"]["decision_usable"] is False
    assert rejected["summary"]["alert_level"] == "source_snapshot_rejected"
    assert rejected["new_tokens"] == []

    legacy = deepcopy(report)
    legacy.pop("source_snapshot")
    legacy["summary"]["alert_level"] = "no_new_feeds"
    _write_json(daily_path, legacy)
    legacy_rejected = load_daily_feed_agent_report(
        daily_path,
        current_report_path=canonical_path,
    )
    assert legacy_rejected["summary"]["alert_level"] == "source_snapshot_rejected"
    assert legacy_rejected["summary"]["reported_alert_level"] == "no_new_feeds"


@pytest.mark.parametrize(
    "unsafe_reference",
    ["../outside-secret.json", "/tmp/private-secret.json"],
)
def test_persisted_daily_source_reference_cannot_escape_reports_directory(
    tmp_path,
    unsafe_reference,
):
    current = _monitor(
        generated_at="2026-07-30T12:00:00+00:00",
        assets=[_asset("1")],
        tokens=[_token("1", "0x01")],
        build_id="current",
    )
    canonical_path = tmp_path / "monitor.json"
    daily_path = tmp_path / "daily.json"
    _write_json(canonical_path, current)
    report = build_daily_feed_agent_report(
        previous_report={},
        current_report=current,
        generated_at="2026-07-30T12:01:00+00:00",
        current_report_path=canonical_path,
    )
    report["source"]["current_report"] = unsafe_reference
    _write_json(daily_path, report)

    rejected = load_daily_feed_agent_report(daily_path)

    assert rejected["status"]["acceptance"] == "failed_closed"
    assert rejected["status"]["decision_usable"] is False
    assert rejected["status"]["errors"] == [
        "daily persisted source reference is unsafe"
    ]
    assert rejected["source"]["current_report"] is None
    assert unsafe_reference not in json.dumps(rejected)

    trusted = load_daily_feed_agent_report(
        daily_path,
        current_report_path=canonical_path,
    )
    assert trusted["status"]["acceptance"] == "passed"


def test_invalid_counts_and_stale_generation_are_rejected():
    current = _monitor(
        generated_at="2026-07-30T12:00:00+00:00",
        assets=[_asset("1")],
        tokens=[_token("1", "0x01")],
        build_id="current",
    )
    invalid = deepcopy(current)
    invalid["summary"]["token_count"] = 99
    with pytest.raises(ValueError, match="token_count"):
        rwa_xyz_monitor_snapshot_identity(invalid)

    with pytest.raises(ValueError, match="predates"):
        build_daily_feed_agent_report(
            previous_report={},
            current_report=current,
            generated_at="2026-07-30T11:59:59+00:00",
        )


def test_snapshot_identity_preserves_case_for_non_hex_contract_addresses():
    current = _monitor(
        generated_at="2026-07-30T12:00:00+00:00",
        assets=[_asset("1"), _asset("2")],
        tokens=[
            {
                **_token("1", "AbCd"),
                "network": "Solana",
                "network_slug": "solana",
            },
            {
                **_token("2", "abcd"),
                "network": "Solana",
                "network_slug": "solana",
            },
        ],
        build_id="case-sensitive",
    )

    identity = rwa_xyz_monitor_snapshot_identity(current)

    assert identity["token_count"] == 2
    assert identity["unique_token_contract_count"] == 2


def test_canonical_reconciliation_publishes_atomic_verified_baseline(tmp_path):
    current = _monitor(
        generated_at="2026-07-30T12:00:00+00:00",
        assets=[_asset("1"), _asset("2")],
        tokens=[_token("1", "0x01"), _token("2", "0x02")],
        build_id="current",
    )
    canonical_path = tmp_path / "monitor.json"
    daily_path = tmp_path / "daily.json"
    csv_path = tmp_path / "new.csv"
    history_dir = tmp_path / "history"
    _write_json(canonical_path, current)

    report = write_daily_feed_agent_baseline(
        json_path=daily_path,
        csv_path=csv_path,
        history_dir=history_dir,
        current_report_path=canonical_path,
        generated_at="2026-07-30T12:01:00+00:00",
    )

    assert report["summary"]["alert_level"] == "baseline_created"
    assert report["source_snapshot"] == rwa_xyz_monitor_snapshot_identity(current)
    assert load_daily_feed_agent_report(
        daily_path,
        current_report_path=canonical_path,
    )["status"]["acceptance"] == "passed"
    assert csv_path.read_text(encoding="utf-8").startswith("priority,lane,")
    assert (history_dir / "2026-07-30.json").read_bytes() == daily_path.read_bytes()
    assert not list(tmp_path.rglob("*.tmp"))


def test_invalid_canonical_does_not_overwrite_existing_daily_artifact(tmp_path):
    canonical_path = tmp_path / "monitor.json"
    daily_path = tmp_path / "daily.json"
    invalid = _monitor(
        generated_at="2026-07-30T12:00:00+00:00",
        assets=[_asset("1")],
        tokens=[_token("1", "0x01")],
        build_id="invalid",
    )
    invalid["summary"]["asset_count"] = 7
    _write_json(canonical_path, invalid)
    daily_path.write_text("sentinel\n", encoding="utf-8")

    with pytest.raises(ValueError, match="asset_count"):
        write_daily_feed_agent_baseline(
            json_path=daily_path,
            csv_path=tmp_path / "new.csv",
            history_dir=tmp_path / "history",
            current_report_path=canonical_path,
            generated_at="2026-07-30T12:01:00+00:00",
        )
    assert daily_path.read_text(encoding="utf-8") == "sentinel\n"


def test_refresh_stages_and_promotes_a_reconciled_monitor_before_daily_publish(
    tmp_path,
    monkeypatch,
):
    previous = _monitor(
        generated_at="2026-07-29T12:00:00+00:00",
        assets=[_asset("1")],
        tokens=[_token("1", "0x01")],
        build_id="previous",
    )
    current = _monitor(
        generated_at="2026-07-30T12:00:00+00:00",
        assets=[_asset("1"), _asset("2")],
        tokens=[_token("1", "0x01"), _token("2", "0x02")],
        build_id="current",
    )
    canonical_path = tmp_path / "monitor.json"
    assets_csv_path = tmp_path / "assets.csv"
    tokens_csv_path = tmp_path / "tokens.csv"
    daily_path = tmp_path / "daily.json"
    _write_json(canonical_path, previous)

    monkeypatch.setattr(
        daily_agent,
        "fetch_rwa_xyz_monitor_payload",
        lambda **_kwargs: ({"ignored": True}, {"source": "test"}),
    )

    def fake_monitor_writer(*, json_path, asset_csv_path, token_csv_path, **_kwargs):
        _write_json(json_path, current)
        asset_csv_path.write_text("asset_id\n", encoding="utf-8")
        token_csv_path.write_text("token_id\n", encoding="utf-8")
        return deepcopy(current)

    monkeypatch.setattr(daily_agent, "write_rwa_xyz_monitor_reports", fake_monitor_writer)

    report = write_daily_feed_agent_report(
        json_path=daily_path,
        csv_path=tmp_path / "new.csv",
        history_dir=tmp_path / "history",
        refresh_json_path=canonical_path,
        refresh_asset_csv_path=assets_csv_path,
        refresh_token_csv_path=tokens_csv_path,
    )

    assert json.loads(canonical_path.read_text(encoding="utf-8")) == current
    assert assets_csv_path.read_text(encoding="utf-8") == "asset_id\n"
    assert tokens_csv_path.read_text(encoding="utf-8") == "token_id\n"
    assert report["summary"]["alert_level"] == "new_p0_tokens"
    assert report["source_snapshot"] == rwa_xyz_monitor_snapshot_identity(current)
    assert load_daily_feed_agent_report(
        daily_path,
        current_report_path=canonical_path,
    )["status"]["acceptance"] == "passed"
