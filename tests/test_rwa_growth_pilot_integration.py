from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from scripts.run_rwa_growth_pilot import PILOT_FEEDS
from src import resource_server
from src.rwa_store import RWAObservationStore


def _capture(feed: dict[str, object], *, status: str = "ok") -> dict[str, object]:
    checked_at = datetime.now(UTC).isoformat()
    row: dict[str, object] = {
        **feed,
        "started_at": checked_at,
        "checked_at": checked_at,
        "status": status,
        "checks": {
            "freshness_pass": status == "ok",
            "bidask_sanity_pass": status == "ok",
        },
        "production_promoted": False,
    }
    if status == "ok":
        row["raw_observation"] = {
            "symbol": feed["symbol"],
            "venue": feed["venue"],
            "timestamp": checked_at,
            "bid": 99.0,
            "ask": 101.0,
        }
    else:
        row.update({"error_type": "TimeoutError", "message": "safe timeout"})
    return row


def test_dashboard_status_is_derived_from_sqlite_ledger(tmp_path, monkeypatch):
    store = RWAObservationStore(str(tmp_path / "rwa.db"))
    store.store_pilot_outcomes([_capture(feed) for feed in PILOT_FEEDS])
    app = SimpleNamespace(state=SimpleNamespace(rwa_store=store))
    monkeypatch.setenv("RWA_GROWTH_PILOT_ENABLED", "true")

    report = resource_server._rwa_growth_pilot_dashboard_status(app)

    assert report["enabled"] is True
    assert report["ledger"]["authoritative"] is True
    assert report["ledger"]["source_of_truth"] == "rwa_observation_store"
    assert report["freshness"]["status"] == "healthy"
    assert report["current_capture"] == {
        "attempted": len(PILOT_FEEDS),
        "succeeded": len(PILOT_FEEDS),
        "failed": 0,
        "derived_from": "latest_ledger_outcome_per_feed",
    }
    assert report["production_promoted_feed_count"] == 0
    assert report["policy"]["automatic_promotion"] is False


def test_dashboard_latest_outcomes_include_ledger_failures(tmp_path, monkeypatch):
    store = RWAObservationStore(str(tmp_path / "rwa.db"))
    captures = [_capture(feed) for feed in PILOT_FEEDS]
    captures[-1] = _capture(PILOT_FEEDS[-1], status="error")
    store.store_pilot_outcomes(captures)
    app = SimpleNamespace(state=SimpleNamespace(rwa_store=store))
    monkeypatch.setenv("RWA_GROWTH_PILOT_ENABLED", "true")

    report = resource_server._rwa_growth_pilot_dashboard_status(app)

    assert report["current_capture"] == {
        "attempted": len(PILOT_FEEDS),
        "succeeded": len(PILOT_FEEDS) - 1,
        "failed": 1,
        "derived_from": "latest_ledger_outcome_per_feed",
    }
    assert report["freshness"]["status"] == "degraded"


@pytest.mark.asyncio
async def test_scheduler_persists_to_ledger_without_file_sidecar(
    tmp_path,
    monkeypatch,
):
    store = RWAObservationStore(str(tmp_path / "rwa.db"))
    captures = [_capture(feed) for feed in PILOT_FEEDS]
    target_app = SimpleNamespace(
        state=SimpleNamespace(
            rwa_store=store,
            rwa_adapter_registry=object(),
        )
    )
    sleep_calls = 0

    async def fake_capture(_registry, *, timeout_seconds):
        assert timeout_seconds >= 1
        return captures

    async def fake_sleep(_seconds):
        nonlocal sleep_calls
        sleep_calls += 1
        if sleep_calls > 1:
            raise asyncio.CancelledError

    monkeypatch.setattr(resource_server, "capture_pilot", fake_capture)
    monkeypatch.setattr(resource_server.asyncio, "sleep", fake_sleep)

    with pytest.raises(asyncio.CancelledError):
        await resource_server._run_rwa_growth_pilot_loop(target_app)

    rows = store.list_pilot_outcomes(
        pilot_ids=[feed["pilot_id"] for feed in PILOT_FEEDS]
    )
    assert len(rows) == len(PILOT_FEEDS)
    assert all(row["ingestion_source"] == "growth_pilot" for row in rows)
    assert not list(tmp_path.glob("*.json*"))
