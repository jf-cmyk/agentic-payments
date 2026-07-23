from datetime import UTC, datetime, timedelta

from scripts.run_rwa_growth_pilot import PILOT_FEEDS, evaluate_history


def test_growth_pilot_never_auto_promotes_after_source_monitoring_passes():
    started_at = datetime.now(UTC) - timedelta(days=15)
    rows = []
    for feed in PILOT_FEEDS:
        for index in range(672):
            timestamp = started_at + timedelta(minutes=32 * index)
            rows.append(
                {
                    "pilot_id": feed["pilot_id"],
                    "checked_at": timestamp.isoformat(),
                    "status": "ok",
                    "checks": {"freshness_pass": True, "bidask_sanity_pass": True},
                }
            )

    report = evaluate_history(rows)

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
                "pilot_id": PILOT_FEEDS[0]["pilot_id"],
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
