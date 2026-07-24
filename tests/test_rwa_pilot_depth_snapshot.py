import json
from datetime import UTC, datetime

from scripts.run_rwa_growth_pilot import PILOT_FEEDS
from scripts.run_rwa_pilot_depth_snapshot import (
    evaluate_depth_evidence,
    persist_depth_report,
)


def _capture(feed, observation):
    return {
        **feed,
        "status": "ok",
        "checked_at": observation["timestamp"],
        "checks": {"freshness_pass": True, "bidask_sanity_pass": True},
        "raw_observation": observation,
    }


def _pool_observation(feed, *, block_number, liquidity):
    now = datetime.now(UTC).isoformat()
    return _capture(
        feed,
        {
            "symbol": feed["symbol"],
            "venue": feed["venue"],
            "source_type": "onchain_clmm_pool",
            "bid": 100.0,
            "ask": 100.1,
            "timestamp": now,
            "metadata": {
                "chain": "ethereum",
                "pool_contract": "0x" + "1" * 40,
                "block_number": block_number,
                "fee_tier": 500,
                "tick_spacing": 10,
                "liquidity": liquidity,
                "sqrt_price_x96": "123456",
                "tick": 100,
                "token0": "0x" + "2" * 40,
                "token1": "0x" + "3" * 40,
                "depth_semantics": "synthetic_from_current_pool_mid_and_liquidity_not_full_tick_replay",
            },
        },
    )


def _evidence_inputs():
    now = datetime.now(UTC).isoformat()
    equity = _capture(
        PILOT_FEEDS[0],
        {
            "symbol": "AAPL/USDC",
            "venue": "hyperliquid_rwa_spot",
            "source_type": "native_l1",
            "bid": 200.0,
            "ask": 200.2,
            "timestamp": now,
        },
    )
    captures = [
        equity,
        _pool_observation(PILOT_FEEDS[1], block_number=22_000_000, liquidity=1_000_000),
        _pool_observation(PILOT_FEEDS[2], block_number=33_000_000, liquidity=2_000_000),
    ]
    books = {
        PILOT_FEEDS[0]["pilot_id"]: {
            "buy": {
                "timestamp": now,
                "levels": [
                    {"price": 200.2, "size": 10.0},
                    {"price": 200.4, "size": 50.0},
                ],
            },
            "sell": {
                "timestamp": now,
                "levels": [
                    {"price": 200.0, "size": 12.0},
                    {"price": 199.8, "size": 60.0},
                ],
            },
            "activity": {
                "captured_at": now,
                "window_seconds": 86_400,
                "base_volume": 1_250.0,
                "notional_volume_usd": 250_000.0,
                "source_type": "native_venue_rolling_stats",
            },
        }
    }
    return captures, books


def test_depth_evidence_separates_native_l2_from_pool_state():
    captures, books = _evidence_inputs()
    report = evaluate_depth_evidence(captures, books)

    assert report["gate_assessment"]["point_in_time_depth_or_state_evidence_collected"] is True
    assert report["gate_assessment"]["manipulation_and_depth_review_complete"] is False
    assert report["gate_assessment"]["production_promotion_allowed"] is False
    assert report["summary"]["executable_depth_observed_feed_count"] == 1
    assert report["summary"]["pool_state_observed"] == 2

    equity = report["rows"][0]
    assert equity["evidence_class"] == "native_l2_point_in_time"
    assert equity["point_in_time_depth_observed"] is True
    assert equity["point_in_time_quality_pass"] is True
    assert equity["quality_decision"] == "candidate_snapshot_pass"
    assert equity["visible_depth"]["buy"]["level_count"] == 2
    assert equity["visible_depth"]["buy"]["target_fills"][0]["fill_ratio"] == 1.0

    for pool in report["rows"][1:]:
        assert pool["evidence_class"] == "block_pinned_pool_state_not_executable_depth"
        assert pool["point_in_time_pool_state_observed"] is True
        assert pool["point_in_time_depth_observed"] is False
        assert pool["point_in_time_quality_pass"] is False
        assert "synthetic_depth_excluded" in pool["risk_flags"]


def test_depth_evidence_flags_incomplete_pool_state():
    captures, books = _evidence_inputs()
    captures[1]["raw_observation"]["metadata"]["liquidity"] = 0

    report = evaluate_depth_evidence(captures, books)

    pool = report["rows"][1]
    assert pool["status"] == "error"
    assert pool["point_in_time_pool_state_observed"] is False
    assert report["gate_assessment"]["point_in_time_depth_or_state_evidence_collected"] is False


def test_native_l2_is_indicative_when_required_block_cannot_fill():
    captures, books = _evidence_inputs()
    books[PILOT_FEEDS[0]["pilot_id"]]["buy"]["levels"] = [{"price": 200.2, "size": 1.0}]

    report = evaluate_depth_evidence(captures, books)

    equity = report["rows"][0]
    assert equity["point_in_time_depth_observed"] is True
    assert equity["point_in_time_quality_pass"] is False
    assert equity["quality_decision"] == "indicative_only_exclude"
    assert "required_block_partial_fill" in equity["risk_flags"]
    assert equity["replay_evidence"]["raw_depth_payload_hash"].startswith("sha256:")


def test_native_l2_is_indicative_when_organic_volume_is_below_threshold():
    captures, books = _evidence_inputs()
    books[PILOT_FEEDS[0]["pilot_id"]]["activity"]["notional_volume_usd"] = 0.0

    report = evaluate_depth_evidence(captures, books)

    equity = report["rows"][0]
    assert equity["point_in_time_depth_observed"] is True
    assert equity["point_in_time_quality_pass"] is False
    assert equity["quality_decision"] == "indicative_only_exclude"
    assert "organic_volume_below_threshold" in equity["risk_flags"]


def test_pool_tick_and_swap_replay_is_measured_without_opening_promotion_gate():
    captures, books = _evidence_inputs()
    pool_id = PILOT_FEEDS[1]["pilot_id"]
    full_fill = {
        "target_notional_usd": 10_000.0,
        "filled_notional_usd": 10_000.0,
        "fill_ratio": 1.0,
        "slippage_bps": 10.0,
        "captured_range_sufficient": True,
    }
    books[pool_id] = {
        "pool_replay": {
            "block_number": 22_000_000,
            "tick_word_range": [-2, 2],
            "tick_word_count": 5,
            "initialized_tick_count": 4,
            "initialized_ticks_truncated": False,
            "target_fills": {
                "buy": [full_fill],
                "sell": [full_fill],
            },
            "volume_window": {
                "quote_volume_usd": 250_000.0,
                "window_coverage_seconds": 86_400,
            },
            "replay_payload": {"bitmap_words": [], "initialized_ticks": [], "swap_logs": []},
            "semantics": {"depth": "bounded_exact_input_replay"},
        }
    }

    report = evaluate_depth_evidence(captures, books)
    pool = next(row for row in report["rows"] if row["pilot_id"] == pool_id)

    assert pool["evidence_class"] == "block_pinned_clmm_tick_and_swap_replay"
    assert pool["point_in_time_tick_replay_observed"] is True
    assert pool["point_in_time_volume_window_observed"] is True
    assert pool["point_in_time_quality_pass"] is True
    assert report["gate_assessment"]["tick_liquidity_replay_complete"] is False
    assert report["gate_assessment"]["production_promotion_allowed"] is False


def test_depth_report_persists_history_and_latest(tmp_path):
    captures, books = _evidence_inputs()
    report = evaluate_depth_evidence(captures, books)
    history = tmp_path / "history.jsonl"
    latest = tmp_path / "latest.json"

    paths = persist_depth_report(report, history_path=history, latest_path=latest)

    assert paths == {"history_path": str(history), "latest_path": str(latest)}
    assert json.loads(history.read_text().splitlines()[0])["product"] == report["product"]
    assert json.loads(latest.read_text())["production_promoted_feed_count"] == 0


def test_depth_history_uses_robust_spread_outlier_rule():
    captures, books = _evidence_inputs()
    history = []
    for spread_bps in (4.9, 5.0, 5.0, 5.1):
        historical = evaluate_depth_evidence(captures, books)
        historical["rows"][0]["spread_bps"] = spread_bps
        history.append(historical)
    captures[0]["raw_observation"]["ask"] = 201.0

    report = evaluate_depth_evidence(captures, books, history=history)

    stats = report["history_statistics"][0]
    assert stats["snapshot_count"] == 5
    assert stats["current_spread_robust_outlier"] is True
