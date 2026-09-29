import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from src import signup_alerts, signup_store
from src.observability import UsageEventStore


@pytest.fixture
def lifecycle_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SIGNUP_DB_SECRET", "s" * 40)
    monkeypatch.setenv("SIGNUP_DB_PATH", str(tmp_path / "signups.sqlite3"))
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("SIGNUP_ALERT_TO", "owner@example.com")
    monkeypatch.setenv("USER_EMAIL_FROM", "Blocksize <hello@blocksize.info>")
    return UsageEventStore(tmp_path / "usage.db")


@pytest.fixture
def mail(monkeypatch):
    sent = []

    async def fake_post(url, *, json, headers, timeout):
        sent.append(json)

        class Response:
            status_code = 200

        return Response()

    real = signup_alerts.send_email

    async def send_with_fake(message, **kwargs):
        kwargs["post"] = fake_post
        return await real(message, **kwargs)

    monkeypatch.setattr(signup_alerts, "send_email", send_with_fake)
    return sent


def _signup(email, created_at, name="Sam"):
    h = signup_alerts.grant_hash_for(email)
    signup_store.record(h, {"email": email, "name": name, "email_verified": "verified", "user_id": "u"},
                        created_at=created_at)
    return h


def test_threshold_email_once_per_threshold_per_month(lifecycle_env, mail):
    store = lifecycle_env
    h = _signup("heavy@example.com", "2026-09-20T10:00:00+00:00")
    snap = {"period": "2026-09", "monthly_limit": 15000, "credits_remaining": 3000, "resets_at": "2026-10-01"}
    assert asyncio.run(signup_alerts.notify_threshold(store, h, 80, snap)) == "sent"
    assert asyncio.run(signup_alerts.notify_threshold(store, h, 80, snap)) == "already_sent"
    assert asyncio.run(signup_alerts.notify_threshold(store, h, 50, snap)) == "disabled"
    assert asyncio.run(signup_alerts.notify_threshold(store, h, 100, {**snap, "credits_remaining": 0})) == "sent"
    assert asyncio.run(signup_alerts.notify_threshold(store, "ft_unknown", 80, snap)) == "unknown_user"
    assert [m["to"] for m in mail] == [["heavy@example.com"]] * 2
    assert "80%" in mail[0]["subject"] and "3,000 remain" in mail[0]["text"]
    assert "used up" in mail[1]["subject"] and "reset on 2026-10-01" in mail[1]["text"]
    assert "utm_campaign=free-tier-100" in mail[1]["text"]
    assert all(m["from"] == "Blocksize <hello@blocksize.info>" and m["reply_to"] == "owner@example.com" for m in mail)
    # New month, same threshold: allowed again.
    assert asyncio.run(signup_alerts.notify_threshold(store, h, 80, {**snap, "period": "2026-10"})) == "sent"


def test_threshold_email_disabled_without_user_sender(lifecycle_env, mail, monkeypatch):
    monkeypatch.delenv("USER_EMAIL_FROM")
    h = _signup("quiet@example.com", "2026-09-20T10:00:00+00:00")
    assert asyncio.run(signup_alerts.notify_threshold(lifecycle_env, h, 80, {"period": "2026-09"})) == "disabled"
    assert mail == []


def test_nudge_goes_only_to_two_day_old_signups_without_a_live_call(lifecycle_env, mail):
    store = lifecycle_env
    now = datetime(2026, 9, 29, 7, 0, tzinfo=UTC)
    idle = _signup("idle@example.com", (now - timedelta(hours=60)).isoformat())
    active = _signup("active@example.com", (now - timedelta(hours=60)).isoformat())
    _signup("fresh@example.com", (now - timedelta(hours=10)).isoformat())
    _signup("stale@example.com", (now - timedelta(hours=100)).isoformat())
    with store._connect() as conn:
        conn.execute("INSERT INTO usage_events (timestamp, event, metadata_json) VALUES (?, ?, ?)",
                     ((now - timedelta(hours=30)).isoformat(), "free_tier_grant_created",
                      json.dumps({"grant_hash": active})))
    assert asyncio.run(signup_alerts.send_nudges(store, now=now)) == 1
    assert [m["to"] for m in mail] == [["idle@example.com"]]
    assert mail[0]["subject"] == "Your Blocksize credits are waiting"
    assert "utm_campaign=free-tier-nudge" in mail[0]["text"]
    # Running again the same day sends nothing more.
    assert asyncio.run(signup_alerts.send_nudges(store, now=now)) == 0
    assert idle != active


def test_background_threshold_hook_never_raises_without_a_loop(lifecycle_env):
    signup_alerts.notify_threshold_background("ft_x", 80, {"period": "2026-09"})
    signup_alerts.notify_threshold_background(None, 80, {})
    signup_alerts.notify_threshold_background("ft_x", 95, {})
