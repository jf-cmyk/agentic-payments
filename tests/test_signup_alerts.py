import base64
import hashlib
import hmac
import json
import time

import pytest
from fastapi.testclient import TestClient

from src import signup_alerts
from src.resource_server import app

RAW_SECRET = base64.b64encode(b"0123456789abcdef0123456789abcdef").decode()
SECRET = "whsec_" + RAW_SECRET


def signed_headers(body: bytes, *, msg_id="msg_1", ts=None, secret=RAW_SECRET):
    ts = str(int(ts if ts is not None else time.time()))
    digest = hmac.new(base64.b64decode(secret), f"{msg_id}.{ts}.".encode() + body, hashlib.sha256).digest()
    return {
        "svix-id": msg_id,
        "svix-timestamp": ts,
        "svix-signature": "v1," + base64.b64encode(digest).decode(),
        "content-type": "application/json",
    }


def user_created(email="new.user@example.com", verified="verified"):
    return json.dumps({
        "type": "user.created",
        "data": {
            "id": "user_abc",
            "first_name": "New",
            "last_name": "User",
            "created_at": 1790000000000,
            "primary_email_address_id": "idn_1",
            "email_addresses": [
                {"id": "idn_1", "email_address": email, "verification": {"status": verified}},
            ],
        },
    }).encode()


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("CLERK_WEBHOOK_SECRET", SECRET)
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("SIGNUP_ALERT_TO", "owner@example.com")
    signup_alerts._SEEN_MESSAGE_IDS.clear()


@pytest.fixture
def sent(monkeypatch):
    calls = []

    async def fake_post(url, *, json, headers, timeout):
        calls.append({"url": url, "json": json, "headers": headers})

        class Response:
            status_code = 200

        return Response()

    real = signup_alerts.send_email

    async def send_with_fake(message, *, post=None):
        return await real(message, post=fake_post)

    monkeypatch.setattr(signup_alerts, "send_email", send_with_fake)
    return calls


def test_verify_svix_accepts_valid_and_rejects_bad_stale_or_missing(configured):
    body = user_created()
    assert signup_alerts.verify_svix(signed_headers(body), body) == "msg_1"
    with pytest.raises(signup_alerts.SignupAlertError) as bad:
        signup_alerts.verify_svix(signed_headers(body, secret=base64.b64encode(b"x" * 32).decode()), body)
    assert bad.value.error == "invalid_signature"
    with pytest.raises(signup_alerts.SignupAlertError) as stale:
        signup_alerts.verify_svix(signed_headers(body, ts=time.time() - 3600), body)
    assert stale.value.error == "stale_timestamp"
    with pytest.raises(signup_alerts.SignupAlertError) as missing:
        signup_alerts.verify_svix({"content-type": "application/json"}, body)
    assert missing.value.status == 401


def test_route_is_absent_until_configured(monkeypatch):
    monkeypatch.delenv("CLERK_WEBHOOK_SECRET", raising=False)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    body = user_created()
    with TestClient(app) as client:
        response = client.post("/internal/clerk/webhook", content=body, headers=signed_headers(body))
    assert response.status_code == 404


def test_new_user_triggers_one_email_and_replays_are_ignored(configured, sent):
    body = user_created()
    with TestClient(app) as client:
        first = client.post("/internal/clerk/webhook", content=body, headers=signed_headers(body))
        replay = client.post("/internal/clerk/webhook", content=body, headers=signed_headers(body))
        other = json.dumps({"type": "session.created", "data": {}}).encode()
        ignored = client.post("/internal/clerk/webhook", content=other,
                              headers=signed_headers(other, msg_id="msg_2"))
        forged = client.post("/internal/clerk/webhook", content=body,
                             headers={**signed_headers(body, msg_id="msg_3"), "svix-signature": "v1,AAAA"})
    assert first.status_code == 200 and first.json()["status"] == "sent"
    assert replay.json() == {"status": "duplicate"}
    assert ignored.json() == {"status": "ignored", "type": "session.created"}
    assert forged.status_code == 401
    assert len(sent) == 1
    mail = sent[0]["json"]
    assert mail["to"] == ["owner@example.com"]
    assert mail["subject"] == "New Blocksize connector signup: new.user@example.com"
    assert "Email status: verified" in mail["text"]
    assert "2026-" in mail["text"]
    assert sent[0]["headers"]["Authorization"] == "Bearer re_test_key"


def test_email_failure_returns_502_so_svix_retries(configured, monkeypatch):
    async def failing_post(url, **_kwargs):
        raise ConnectionError("boom")

    real = signup_alerts.send_email

    async def send_failing(message, *, post=None):
        return await real(message, post=failing_post)

    monkeypatch.setattr(signup_alerts, "send_email", send_failing)
    signup_alerts._SEEN_MESSAGE_IDS.clear()
    body = user_created()
    with TestClient(app) as client:
        response = client.post("/internal/clerk/webhook", content=body, headers=signed_headers(body, msg_id="msg_9"))
    assert response.status_code == 502
    assert response.json() == {"error": "email_send_failed"}


def test_summarize_user_handles_missing_fields():
    summary = signup_alerts.summarize_user({"id": "user_x", "email_addresses": []})
    assert summary["email"] == "unknown"
    assert summary["name"] == "not given"
    assert summary["created_at"] == "unknown"
