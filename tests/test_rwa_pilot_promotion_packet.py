import json

from scripts.build_rwa_pilot_promotion_packet import (
    build_promotion_packet,
    persist_promotion_packet,
)
from scripts.run_rwa_growth_pilot import PILOT_FEEDS


def test_promotion_packet_records_evidence_but_never_auto_promotes(tmp_path):
    monitoring = {
        "feeds": [
            {**feed, "source_monitoring_ready": True}
            for feed in PILOT_FEEDS
        ]
    }
    alignment = {
        "rows": [
            {**feed, "timestamp_alignment": {"pass": True}}
            for feed in PILOT_FEEDS
        ]
    }
    depth = {
        "rows": [
            {
                **feed,
                "point_in_time_volume_window_observed": True,
                "point_in_time_tick_replay_observed": (
                    feed["source_lane"] != "venue_api_order_book"
                ),
                "point_in_time_quality_pass": True,
            }
            for feed in PILOT_FEEDS
        ]
    }

    packet = build_promotion_packet(monitoring, alignment, depth)

    assert packet["production_promoted_feed_count"] == 0
    assert packet["summary"]["promotion_ready_count"] == 0
    assert packet["policy"]["automatic_promotion"] is False
    for row in packet["feeds"]:
        assert row["decision"] == "hold_candidate"
        assert row["production_promoted"] is False
        assert row["gates"]["human_promotion_approval"] is False
        assert "human_promotion_approval" in row["blocking_gates"]
        assert row["gates"]["initialized_tick_replay_snapshot"] is True

    paths = persist_promotion_packet(
        packet,
        history_path=tmp_path / "history.jsonl",
        latest_path=tmp_path / "latest.json",
    )
    assert paths["latest_path"].endswith("latest.json")
    assert json.loads((tmp_path / "latest.json").read_text())["summary"] == packet["summary"]
