import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from src import free_tier, signup_alerts, signup_store
from src.observability import UsageEventStore
from src.resource_server import app
from tests.test_signup_alerts import SECRET, signed_headers, user_created


@pytest.fixture
def store_env(monkeypatch, tmp_path):
    monkeypatch.setenv("SIGNUP_DB_SECRET", "s" * 40)
    monkeypatch.setenv("SIGNUP_DB_PATH", str(tmp_path / "signups.sqlite3"))


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


def test_signup_store_round_trip_and_disabled_without_secret(store_env, monkeypatch):
    user = {"email": "a@example.com", "name": "A", "email_verified": "verified", "user_id": "u1"}
    assert signup_store.record("hash_a", user, created_at="2026-09-29T01:00:00+00:00")
    assert signup_store.lookup({"hash_a"})["hash_a"]["email"] == "a@example.com"
    rows = signup_store.signups_since("2026-09-29T00:00:00+00:00")
    assert rows and rows[0]["grant_hash"] == "hash_a"
    assert signup_store.signups_since("2026-09-30T00:00:00+00:00") == []
    # Encrypted at rest: the email does not appear in the file.
    assert b"a@example.com" not in open(signup_store.db_path(), "rb").read()
    monkeypatch.delenv("SIGNUP_DB_SECRET")
    assert signup_store.record("hash_b", user) is False
    assert signup_store.lookup({"hash_a"}) == {}


def test_webhook_stores_signup_and_sends_welcome_when_sender_configured(store_env, mail, monkeypatch):
    monkeypatch.setenv("CLERK_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("SIGNUP_ALERT_TO", "owner@example.com")
    monkeypatch.setenv("USER_EMAIL_FROM", "Blocksize <hello@blocksize.info>")
    signup_alerts._SEEN_MESSAGE_IDS.clear()
    body = user_created(email="new.user@example.com")
    with TestClient(app) as client:
        response = client.post("/internal/clerk/webhook", content=body, headers=signed_headers(body, msg_id="msg_w1"))
    assert response.json() == {"status": "sent", "welcome_email": "sent", "stored": True}
    assert [m["to"] for m in mail] == [["owner@example.com"], ["new.user@example.com"]]
    welcome = mail[1]
    assert welcome["from"] == "Blocksize <hello@blocksize.info>"
    assert welcome["reply_to"] == "owner@example.com"
    assert f"{free_tier.allowance_label()} free live-data credits" in welcome["text"]
    assert "Run a pre-trade check" in welcome["text"]
    assert "utm_campaign=free-tier-welcome" in welcome["text"]
    assert "Data terms" in welcome["html"]
    stored = signup_store.signups_since("2000-01-01")
    assert stored and stored[0]["email"] == "new.user@example.com"
    assert stored[0]["grant_hash"] == signup_alerts.grant_hash_for("new.user@example.com")


def test_webhook_skips_welcome_without_user_sender(store_env, mail, monkeypatch):
    monkeypatch.setenv("CLERK_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.delenv("USER_EMAIL_FROM", raising=False)
    signup_alerts._SEEN_MESSAGE_IDS.clear()
    body = user_created(email="quiet@example.com")
    with TestClient(app) as client:
        response = client.post("/internal/clerk/webhook", content=body, headers=signed_headers(body, msg_id="msg_w2"))
    assert response.json()["welcome_email"] == "skipped"
    assert len(mail) == 1


def test_digest_names_signups_activations_and_thresholds(store_env, mail, monkeypatch, tmp_path):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    monkeypatch.setenv("SIGNUP_ALERT_TO", "owner@example.com")
    now = datetime(2026, 9, 29, 7, 0, tzinfo=UTC)
    since = now - timedelta(hours=24)
    active_hash = signup_alerts.grant_hash_for("active@example.com")
    idle_hash = signup_alerts.grant_hash_for("idle@example.com")
    old_hash = signup_alerts.grant_hash_for("old@example.com")
    signup_store.record(active_hash, {"email": "active@example.com", "name": "Ann", "email_verified": "verified", "user_id": "u_a"},
                        created_at=(since + timedelta(hours=1)).isoformat())
    signup_store.record(idle_hash, {"email": "idle@example.com", "name": "Ivo", "email_verified": "verified", "user_id": "u_i"},
                        created_at=(since + timedelta(hours=2)).isoformat())
    signup_store.record(old_hash, {"email": "old@example.com", "name": "Old", "email_verified": "verified", "user_id": "u_o"},
                        created_at=(since - timedelta(days=3)).isoformat())
    store = UsageEventStore(tmp_path / "usage.db")
    stamp = (since + timedelta(hours=3)).isoformat()
    with store._connect() as conn:
        for name, meta in (
            ("free_tier_grant_created", {"grant_hash": active_hash}),
            ("free_tier_grant_created", {"grant_hash": old_hash}),
            ("free_tier_threshold_crossed", {"grant_hash": old_hash, "threshold_pct": 80}),
            ("upgrade_cta_shown", {"trigger": "threshold"}),
            ("outbound_conversion_click", {"destination": "free-trial"}),
            ("agent_auth_approved", {"registration_hash": "r1"}),
        ):
            conn.execute("INSERT INTO usage_events (timestamp, event, surface, metadata_json) VALUES (?, ?, ?, ?)",
                         (stamp, name, "anthropic_mcp", json.dumps(meta)))
        conn.execute("INSERT INTO usage_events (timestamp, event, metadata_json) VALUES (?, ?, ?)",
                     ((since - timedelta(days=1)).isoformat(), "free_tier_grant_created", json.dumps({"grant_hash": idle_hash})))
    digest = asyncio.run(signup_alerts.send_digest(store, now=now))
    assert digest["signups_count"] == 2
    assert [s["email"] for s in digest["activated_signups"]] == ["active@example.com"]
    assert [s["email"] for s in digest["not_activated_signups"]] == ["idle@example.com"]
    assert digest["free_tier_grants"] == sorted(["active@example.com", "old@example.com"])
    assert digest["thresholds"] == {"80%": ["old@example.com"]}
    assert digest["go_clicks"] == {"free-trial": 1}
    assert digest["agent_auth"] == {"approved": 1}
    sent = mail[-1]
    assert sent["to"] == ["owner@example.com"]
    assert sent["subject"] == "Blocksize digest 2026-09-28: 2 signups, 2 activations"
    assert "Worth a personal note" in sent["text"]
    assert "idle@example.com" in sent["text"]
    assert "old@example.com" in sent["text"]


def test_digest_route_refuses_without_operator_token(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    with TestClient(app) as client:
        anonymous = client.post("/internal/observability/signup-digest")
        wrong = client.post("/internal/observability/signup-digest",
                            headers={"Authorization": "Bearer definitely-not-the-token"})
    # 503 is the existing answer when no dashboard token is configured at all.
    assert anonymous.status_code in {401, 403, 503}
    assert wrong.status_code in {401, 403, 503}
    assert anonymous.json().get("status") != "sent"
