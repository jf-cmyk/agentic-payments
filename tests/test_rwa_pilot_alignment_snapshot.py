from __future__ import annotations

import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from scripts.run_rwa_pilot_alignment_snapshot import (
    capture_blocksize_benchmarks,
    evaluate_alignment,
    persist_alignment_report,
)


def _capture(pilot_id: str, symbol: str, venue: str, value: float) -> dict:
    return {
        "pilot_id": pilot_id,
        "status": "ok",
        "checked_at": "2026-07-24T05:00:00+00:00",
        "checks": {"freshness_pass": True, "bidask_sanity_pass": True},
        "raw_observation": {
            "symbol": symbol,
            "venue": venue,
            "source_type": "test",
            "bid": value - 0.01,
            "ask": value + 0.01,
            "timestamp": "2026-07-24T05:00:00+00:00",
        },
    }


def _benchmark(service: str, symbol: str, value: float) -> dict:
    return {
        "status": "ok",
        "service": service,
        "symbol": symbol,
        "endpoint": "bidask_getSnapshot",
        "timestamp": "2026-07-24T05:00:00+00:00",
        "value": value,
        "data": {"mid": value},
    }


def test_alignment_snapshot_compares_all_feeds_without_opening_promotion_gate() -> None:
    captures = [
        _capture("aapl_hyperliquid_spot", "AAPL/USDC", "hyperliquid_rwa_spot", 200.0),
        _capture("paxg_uniswap_ethereum", "PAXG/USDC", "uniswap_v3_v4", 3400.0),
        _capture("eurc_aerodrome_base", "EURC/USDC", "aerodrome_slipstream", 1.15),
    ]
    benchmarks = {
        "aapl_hyperliquid_spot": _benchmark("bidask", "AAPL", 200.0),
        "paxg_uniswap_ethereum": _benchmark("metal", "XAUUSD", 3400.0),
        "eurc_aerodrome_base": _benchmark("fx", "EURUSD", 1.15),
    }

    report = evaluate_alignment(captures, benchmarks)

    assert report["summary"] == {
        "feeds_attempted": 3,
        "comparisons_succeeded": 3,
        "comparisons_failed": 0,
        "timestamp_aligned_comparisons": 3,
        "timestamp_misaligned_comparisons": 0,
        "evidence_decisions": {"pass": 3},
    }
    assert report["gate_assessment"]["point_in_time_alignment_observed"] is True
    assert report["gate_assessment"]["independent_benchmark_alignment_complete"] is False
    assert report["gate_assessment"]["production_promotion_allowed"] is False
    assert report["policy"]["production_promoted_feed_count"] == 0
    assert report["policy"]["pilot_runtime_tiingo_dependency"] is False


def test_alignment_snapshot_preserves_failed_benchmark_as_evidence_gap() -> None:
    captures = [
        _capture("aapl_hyperliquid_spot", "AAPL/USDC", "hyperliquid_rwa_spot", 200.0),
    ]
    benchmarks = {
        "aapl_hyperliquid_spot": {
            "status": "error",
            "service": "bidask",
            "symbol": "AAPL",
        }
    }

    report = evaluate_alignment(captures, benchmarks)

    assert report["summary"]["comparisons_succeeded"] == 0
    assert report["summary"]["comparisons_failed"] == 3
    assert report["gate_assessment"]["point_in_time_alignment_observed"] is False
    assert all(row["production_promoted"] is False for row in report["rows"])


def test_alignment_snapshot_rejects_stale_benchmark_as_timestamp_misaligned() -> None:
    capture = _capture(
        "aapl_hyperliquid_spot",
        "AAPL/USDC",
        "hyperliquid_rwa_spot",
        200.0,
    )
    benchmark = _benchmark("bidask", "AAPL", 200.0)
    benchmark["timestamp"] = "2026-07-24T04:00:00+00:00"

    report = evaluate_alignment(
        [capture],
        {"aapl_hyperliquid_spot": benchmark},
    )

    row = report["rows"][0]
    assert row["comparison"]["decision"] == "pass"
    assert row["evidence_decision"] == "not_timestamp_aligned"
    assert row["timestamp_alignment"]["pass"] is False
    assert report["gate_assessment"]["point_in_time_alignment_observed"] is False


@pytest.mark.asyncio
async def test_bidask_benchmark_computes_midpoint_from_model_fields() -> None:
    now = datetime.now(UTC)

    class FakeClient:
        async def get_bidask_snapshot(self, symbol):
            return SimpleNamespace(bid=199.0, ask=201.0, timestamp=now)

        async def get_metal_price(self, symbol):
            return SimpleNamespace(price=3400.0, currency="USD", timestamp=now)

        async def get_fx_rate(self, symbol):
            return SimpleNamespace(bid=1.14, ask=1.16, mid=1.15, timestamp=now)

    rows = await capture_blocksize_benchmarks(FakeClient())

    assert rows["aapl_hyperliquid_spot"]["value"] == 200.0
    assert rows["aapl_hyperliquid_spot"]["data"]["mid"] == 200.0


def test_alignment_report_persists_replay_history_and_latest_status(tmp_path) -> None:
    report = {"generated_at": "2026-07-24T05:00:00+00:00", "summary": {"pass": 2}}
    history = tmp_path / "alignment.jsonl"
    latest = tmp_path / "alignment-latest.json"

    paths = persist_alignment_report(report, history_path=history, latest_path=latest)

    assert json.loads(history.read_text().strip()) == report
    assert json.loads(latest.read_text()) == report
    assert paths == {"history_path": str(history), "latest_path": str(latest)}
