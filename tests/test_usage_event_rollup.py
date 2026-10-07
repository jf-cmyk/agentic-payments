"""Raw usage events past the retention window fold into daily rows and are deleted."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import sqlite3

from src import usage_event_rollup as rollup
from src.observability import UsageEventStore

NOW = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)


def _seed(store: UsageEventStore) -> None:
    old = (NOW - timedelta(days=200)).isoformat()
    older = (NOW - timedelta(days=201)).isoformat()
    recent = (NOW - timedelta(days=10)).isoformat()
    with sqlite3.connect(store.db_path) as conn:
        rows = [
            (old, "http_request", "http_api", 402, None),
            (old, "http_request", "http_api", 402, None),
            (old, "http_request", "http_api", 200, None),
            (old, "payment_settled", "http_api", None, 0.002),
            (older, "http_request", "public_mcp", 200, None),
            (recent, "http_request", "http_api", 402, None),
            (recent, "payment_settled", "http_api", None, 0.005),
        ]
        conn.executemany(
            "INSERT INTO usage_events (timestamp, event, surface, status_code, price_usdc) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )


def _rows(db_path: str, sql: str) -> list[tuple]:
    with sqlite3.connect(db_path) as conn:
        return conn.execute(sql).fetchall()


def test_old_rows_fold_into_daily_rollup_and_recent_rows_stay(tmp_path) -> None:
    store = UsageEventStore(tmp_path / "usage.db")
    _seed(store)

    report = rollup.rollup_and_prune(store, now=NOW)

    assert report["rows_deleted"] == 5
    assert report["remaining_rows_past_cutoff"] == 0
    assert report["retention_days"] == rollup.DEFAULT_RETENTION_DAYS
    assert _rows(store.db_path, "SELECT COUNT(*) FROM usage_events") == [(2,)]
    day = (NOW - timedelta(days=200)).date().isoformat()
    assert _rows(
        store.db_path,
        "SELECT day, event, surface, status_code, count, price_usdc_sum "
        "FROM usage_daily_rollup ORDER BY day, event, surface, status_code",
    ) == [
        ((NOW - timedelta(days=201)).date().isoformat(), "http_request", "public_mcp", 200, 1, 0.0),
        (day, "http_request", "http_api", 200, 1, 0.0),
        (day, "http_request", "http_api", 402, 2, 0.0),
        (day, "payment_settled", "http_api", 0, 1, 0.002),
    ]


def test_second_pass_is_a_no_op(tmp_path) -> None:
    store = UsageEventStore(tmp_path / "usage.db")
    _seed(store)
    rollup.rollup_and_prune(store, now=NOW)

    report = rollup.rollup_and_prune(store, now=NOW)

    assert report["rows_deleted"] == 0
    assert _rows(store.db_path, "SELECT SUM(count) FROM usage_daily_rollup") == [(5,)]


def test_batches_cover_all_old_rows_without_double_counting(tmp_path) -> None:
    store = UsageEventStore(tmp_path / "usage.db")
    old = (NOW - timedelta(days=300)).isoformat()
    with sqlite3.connect(store.db_path) as conn:
        conn.executemany(
            "INSERT INTO usage_events (timestamp, event, surface, status_code) VALUES (?, ?, ?, ?)",
            [(old, "http_request", "http_api", 200)] * 7,
        )

    report = rollup.rollup_and_prune(store, now=NOW, batch_rows=3)

    assert report["batches"] == 3
    assert report["rows_deleted"] == 7
    assert _rows(store.db_path, "SELECT count FROM usage_daily_rollup") == [(7,)]


def test_retention_never_drops_below_two_dashboard_windows(monkeypatch) -> None:
    monkeypatch.setenv(rollup.RETENTION_DAYS_ENV, "30")
    assert rollup.retention_days() == rollup.MIN_RETENTION_DAYS
    monkeypatch.setenv(rollup.RETENTION_DAYS_ENV, "400")
    assert rollup.retention_days() == 400
    monkeypatch.setenv(rollup.RETENTION_DAYS_ENV, "not-a-number")
    assert rollup.retention_days() == rollup.DEFAULT_RETENTION_DAYS


def test_storage_status_reports_the_volume(tmp_path) -> None:
    store = UsageEventStore(tmp_path / "usage.db")
    status = rollup.storage_status(store.db_path, volume_path=str(tmp_path))
    assert status["volume_available"] is True
    assert status["usage_events_db_bytes"] > 0
    assert 0 <= status["volume_used_percent"] <= 100
    assert status["volume_pressure"] in (True, False)
