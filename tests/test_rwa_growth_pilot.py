from datetime import UTC, datetime, timedelta

from scripts.run_rwa_growth_pilot import PILOT_FEEDS, evaluate_history, persist_capture
from src.rwa_store import RWAObservationStore


def test_growth_pilot_never_auto_promotes_after_source_monitoring_passes():
    started_at = datetime.now(UTC) - timedelta(days=15)
    rows = []
    for feed in PILOT_FEEDS:
        for index in range(672):
            timestamp = started_at + timedelta(minutes=32 * index)
            rows.append(
                {
                    **feed,
                    "checked_at": timestamp.isoformat(),
                    "status": "ok",
                    "checks": {"freshness_pass": True, "bidask_sanity_pass": True},
                }
            )

    latest_timestamp = max(datetime.fromisoformat(row["checked_at"]) for row in rows)
    report = evaluate_history(rows, now=latest_timestamp)

    assert report["source_monitoring_ready"] is True
    assert report["promotion_ready"] is False
    assert report["production_promoted_feed_count"] == 0
    assert report["policy"]["automatic_promotion"] is False
    assert report["policy"]["tiingo_runtime_dependency"] is False


def test_growth_pilot_requires_complete_success_and_freshness_history():
    checked_at = datetime.now(UTC)
    report = evaluate_history(
        [
            {
                **PILOT_FEEDS[0],
                "checked_at": checked_at.isoformat(),
                "status": "error",
                "checks": {"freshness_pass": False, "bidask_sanity_pass": False},
            }
        ]
    )

    assert report["source_monitoring_ready"] is False
    first = report["feeds"][0]
    assert first["sample_count"] == 1
    assert first["success_rate"] == 0.0
    assert first["source_monitoring_ready"] is False


def test_growth_pilot_persists_successful_captures_to_observation_ledger(tmp_path):
    checked_at = datetime.now(UTC).isoformat()
    store = RWAObservationStore(str(tmp_path / "rwa.db"))
    captures = [
        {
            **PILOT_FEEDS[0],
            "checked_at": checked_at,
            "status": "ok",
            "checks": {"freshness_pass": True, "bidask_sanity_pass": True},
            "raw_observation": {
                "symbol": "AAPL/USDC",
                "venue": "hyperliquid_rwa_spot",
                "asset_class": "equity",
                "source_type": "native_l2",
                "bid": 201.0,
                "ask": 201.1,
                "timestamp": checked_at,
                "metadata": {"raw_payload": {"levels": []}},
            },
        },
        {
            **PILOT_FEEDS[1],
            "checked_at": checked_at,
            "status": "error",
            "error_type": "TimeoutError",
            "message": "Upstream adapter timed out.",
            "checks": {"freshness_pass": False, "bidask_sanity_pass": False},
        },
    ]

    report = persist_capture(
        store,
        captures,
        status_output=tmp_path / "status.json",
        observation_store=store,
        alignment_report={
            "generated_at": checked_at,
            "status": "point_in_time_evidence",
            "summary": {
                "feeds_attempted": 1,
                "timestamp_aligned_comparisons": 1,
            },
            "gate_assessment": {
                "independent_benchmark_alignment_complete": False,
            },
            "rows": [
                {
                    "pilot_id": PILOT_FEEDS[0]["pilot_id"],
                    "status": "ok",
                    "benchmark_service": "bidask",
                    "benchmark_symbol": "AAPL",
                    "comparison": {"decision": "pass", "basis_bps": 2.0},
                    "timestamp_alignment": {"pass": True, "gap_seconds": 1.0},
                    "evidence_decision": "pass",
                }
            ],
        },
        depth_report={
            "generated_at": checked_at,
            "status": "point_in_time_evidence_candidate_only",
            "summary": {"feeds_attempted": 1, "executable_depth_observed_feed_count": 1},
            "gate_assessment": {"manipulation_and_depth_review_complete": False},
            "rows": [
                {
                    "pilot_id": PILOT_FEEDS[0]["pilot_id"],
                    "evidence_class": "native_l2_point_in_time",
                    "point_in_time_depth_observed": True,
                    "manipulation_review_complete": False,
                }
            ],
        },
    )

    assert report["current_capture"]["ledger_persisted"] == 1
    assert len(report["current_capture"]["ledger_observation_ids"]) == 1
    assert report["observation_ledger"]["total_observations"] == 1
    assert report["production_promoted_feed_count"] == 0
    rows = store.list_observations()
    assert rows[0]["symbol"] == "AAPL/USDC"
    assert rows[0]["promotion"]["production_promoted"] is False
    assert rows[0]["blocksize_benchmark"]["evidence_decision"] == "pass"
    assert rows[0]["realtime_quality"]["liquidity_depth_evidence"][
        "point_in_time_depth_observed"
    ] is True
    assert report["benchmark_alignment_latest"]["gate_assessment"] == {
        "independent_benchmark_alignment_complete": False,
    }
    assert report["depth_and_manipulation_latest"]["gate_assessment"] == {
        "manipulation_and_depth_review_complete": False,
    }


def test_growth_pilot_bounds_replay_payload_in_observation_ledger(tmp_path):
    checked_at = datetime.now(UTC).isoformat()
    store = RWAObservationStore(str(tmp_path / "rwa.db"))
    capture = {
        **PILOT_FEEDS[1],
        "checked_at": checked_at,
        "status": "ok",
        "checks": {"freshness_pass": True, "bidask_sanity_pass": True},
        "raw_observation": {
            "symbol": "PAXG/USDC",
            "venue": "uniswap_v3_v4",
            "asset_class": "commodity",
            "source_type": "ethereum_rpc_pool_state",
            "bid": 3_500.0,
            "ask": 3_500.5,
            "timestamp": checked_at,
        },
    }
    swap_logs = [{"blockNumber": hex(index)} for index in range(250)]

    report = persist_capture(
        store,
        [capture],
        observation_store=store,
        depth_report={
            "rows": [
                {
                    "pilot_id": PILOT_FEEDS[1]["pilot_id"],
                    "replay_evidence": {
                        "raw_pool_state_payload_hash": "sha256:pool",
                        "raw_pool_state_payload": {"state": "raw"},
                        "tick_and_swap_payload_hash": "sha256:replay",
                        "tick_and_swap_payload": {
                            "bitmap_words": [{"word": 1}],
                            "initialized_ticks": [{"tick": 2}],
                            "swap_logs": swap_logs,
                        },
                    },
                }
            ]
        },
    )

    assert report["current_capture"]["ledger_persisted"] == 1
    evidence = store.list_observations()[0]["realtime_quality"][
        "liquidity_depth_evidence"
    ]["replay_evidence"]
    assert evidence["payload_storage"] == "rwa_depth_report"
    assert evidence["payloads_omitted_from_observation_ledger"] is True
    assert evidence["raw_pool_state_payload_present"] is True
    assert evidence["tick_and_swap_payload_summary"] == {
        "bitmap_word_count": 1,
        "initialized_tick_count": 1,
        "swap_log_count": 250,
    }
    assert "raw_pool_state_payload" not in evidence
    assert "tick_and_swap_payload" not in evidence


def test_growth_pilot_rejects_burst_backfill_without_slot_coverage():
    now = datetime.now(UTC)
    rows = []
    for feed in PILOT_FEEDS:
        rows.append(
            {
                **feed,
                "checked_at": (now - timedelta(days=14)).isoformat(),
                "status": "ok",
                "checks": {"freshness_pass": True, "bidask_sanity_pass": True},
            }
        )
        for index in range(671):
            rows.append(
                {
                    **feed,
                    "checked_at": (now - timedelta(seconds=671 - index)).isoformat(),
                    "status": "ok",
                    "checks": {"freshness_pass": True, "bidask_sanity_pass": True},
                }
            )

    report = evaluate_history(rows, now=now)

    assert report["source_monitoring_ready"] is False
    assert all(
        row["source_monitoring_gates"]["temporal_slot_coverage_pass"] is False
        for row in report["feeds"]
    )


def test_growth_pilot_wrong_feed_identity_never_satisfies_gates():
    now = datetime.now(UTC)
    rows = []
    for feed in PILOT_FEEDS:
        for index in range(672):
            rows.append(
                {
                    **feed,
                    "symbol": "BTC/USD",
                    "venue": "wrong_venue",
                    "source_lane": "wrong_lane",
                    "checked_at": (now - timedelta(minutes=30 * (671 - index))).isoformat(),
                    "status": "ok",
                    "checks": {"freshness_pass": True, "bidask_sanity_pass": True},
                }
            )

    report = evaluate_history(rows, now=now)

    assert report["source_monitoring_ready"] is False
    assert all(row["sample_count"] == 0 for row in report["feeds"])
    assert all(row["identity_mismatch_count"] == 672 for row in report["feeds"])
