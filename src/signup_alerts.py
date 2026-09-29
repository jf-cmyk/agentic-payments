"""Email the operator when a new Clerk user signs up.

Clerk delivers ``user.created`` webhooks through Svix. The receiver verifies
the Svix signature with ``CLERK_WEBHOOK_SECRET`` (the ``whsec_...`` value shown
in the Clerk dashboard), then sends one email per new user through the Resend
HTTP API (``RESEND_API_KEY``) to ``SIGNUP_ALERT_TO``. Nothing is stored beyond a
privacy-safe usage event, and the route is absent until both secrets are set.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import time
from typing import Any

import httpx

from src.observability import fingerprint, record_usage_event

logger = logging.getLogger(__name__)

SIGNATURE_TOLERANCE_SECONDS = 300
MAX_BODY_BYTES = 64 * 1024
RESEND_ENDPOINT = "https://api.resend.com/emails"
DEFAULT_FROM = "Blocksize signups <onboarding@resend.dev>"
_SEEN_MESSAGE_IDS: dict[str, float] = {}


class SignupAlertError(Exception):
    def __init__(self, error: str, status: int = 400):
        super().__init__(error)
        self.error = error
        self.status = status


def configured() -> bool:
    return bool(os.environ.get("CLERK_WEBHOOK_SECRET", "").strip()) and bool(
        os.environ.get("RESEND_API_KEY", "").strip()
    )


def alert_recipient() -> str:
    return os.environ.get("SIGNUP_ALERT_TO", "jf@blocksize-capital.com").strip()


def _secret_bytes() -> bytes:
    raw = os.environ.get("CLERK_WEBHOOK_SECRET", "").strip()
    if raw.startswith("whsec_"):
        raw = raw[len("whsec_") :]
    try:
        return base64.b64decode(raw + "=" * (-len(raw) % 4))
    except (ValueError, TypeError) as exc:  # noqa: PERF203 - one-off parse
        raise SignupAlertError("webhook_secret_invalid", 500) from exc


def verify_svix(headers: dict[str, str], body: bytes, *, now: float | None = None) -> str:
    """Return the Svix message id after verifying the signature; raise otherwise."""
    lower = {k.lower(): v for k, v in headers.items()}
    msg_id = lower.get("svix-id", "").strip()
    timestamp = lower.get("svix-timestamp", "").strip()
    signatures = lower.get("svix-signature", "").strip()
    if not msg_id or not timestamp or not signatures:
        raise SignupAlertError("missing_signature_headers", 401)
    try:
        sent_at = int(timestamp)
    except ValueError as exc:
        raise SignupAlertError("invalid_timestamp", 401) from exc
    current = now if now is not None else time.time()
    if abs(current - sent_at) > SIGNATURE_TOLERANCE_SECONDS:
        raise SignupAlertError("stale_timestamp", 401)
    signed = f"{msg_id}.{timestamp}.".encode() + body
    expected = base64.b64encode(hmac.new(_secret_bytes(), signed, hashlib.sha256).digest()).decode()
    for candidate in signatures.split():
        version, _, value = candidate.partition(",")
        if version == "v1" and hmac.compare_digest(value, expected):
            return msg_id
    raise SignupAlertError("invalid_signature", 401)


def remember_message(msg_id: str, *, now: float | None = None) -> bool:
    """Return False when this Svix message id was already processed recently."""
    current = now if now is not None else time.time()
    for key in [k for k, exp in _SEEN_MESSAGE_IDS.items() if exp <= current]:
        _SEEN_MESSAGE_IDS.pop(key, None)
    if msg_id in _SEEN_MESSAGE_IDS:
        return False
    if len(_SEEN_MESSAGE_IDS) >= 5000:
        _SEEN_MESSAGE_IDS.clear()
    _SEEN_MESSAGE_IDS[msg_id] = current + 24 * 3600
    return True


def summarize_user(data: dict[str, Any]) -> dict[str, str]:
    """Pick the few fields worth emailing from a Clerk user object."""
    primary_id = data.get("primary_email_address_id")
    addresses = data.get("email_addresses") or []
    primary = next((a for a in addresses if a.get("id") == primary_id), addresses[0] if addresses else {})
    email = str(primary.get("email_address") or "unknown")
    verification = (primary.get("verification") or {}).get("status") or "unknown"
    name = " ".join(part for part in (data.get("first_name"), data.get("last_name")) if part) or "not given"
    created = data.get("created_at")
    created_text = (
        time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(int(created) / 1000))
        if isinstance(created, (int, float)) else "unknown"
    )
    return {
        "email": email,
        "name": name,
        "email_verified": verification,
        "created_at": created_text,
        "user_id": str(data.get("id") or "unknown"),
    }


def build_email(user: dict[str, str]) -> dict[str, str]:
    subject = f"New Blocksize connector signup: {user['email']}"
    text = (
        "A new user signed up through the Blocksize MCP connectors (Clerk).\n\n"
        f"Email:        {user['email']}\n"
        f"Name:         {user['name']}\n"
        f"Email status: {user['email_verified']}\n"
        f"Signed up:    {user['created_at']}\n"
        f"Clerk user:   {user['user_id']}\n\n"
        "They get 15,000 free live-data credits this month. Reply to this email to "
        "keep a note, or write to them directly.\n"
    )
    rows = "".join(
        f"<tr><td style='padding:4px 12px 4px 0;color:#555'>{html.escape(k)}</td>"
        f"<td style='padding:4px 0'>{html.escape(v)}</td></tr>"
        for k, v in (
            ("Email", user["email"]),
            ("Name", user["name"]),
            ("Email status", user["email_verified"]),
            ("Signed up", user["created_at"]),
            ("Clerk user", user["user_id"]),
        )
    )
    body_html = (
        "<p>A new user signed up through the Blocksize MCP connectors (Clerk).</p>"
        f"<table style='font-family:system-ui,sans-serif;font-size:14px'>{rows}</table>"
        "<p>They get 15,000 free live-data credits this month.</p>"
    )
    return {"subject": subject, "text": text, "html": body_html}


async def send_email(message: dict[str, str], *, post=None) -> None:
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    payload = {
        "from": os.environ.get("SIGNUP_ALERT_FROM", DEFAULT_FROM).strip() or DEFAULT_FROM,
        "to": [alert_recipient()],
        "subject": message["subject"],
        "text": message["text"],
        "html": message["html"],
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        if post is not None:
            response = await post(RESEND_ENDPOINT, json=payload, headers=headers, timeout=10.0)
        else:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(RESEND_ENDPOINT, json=payload, headers=headers)
    except Exception as exc:  # noqa: BLE001 - report the kind, never the payload
        logger.warning("signup alert email failed: %s", type(exc).__name__)
        raise SignupAlertError("email_send_failed", 502) from exc
    status = getattr(response, "status_code", 0)
    if status >= 300:
        logger.warning("signup alert email rejected: http_%s", status)
        raise SignupAlertError("email_send_failed", 502)


async def handle_webhook(headers: dict[str, str], body: bytes, *, post=None) -> dict[str, Any]:
    """Verify, deduplicate, and act on one Clerk webhook delivery."""
    if not configured():
        raise SignupAlertError("not_found", 404)
    if len(body) > MAX_BODY_BYTES:
        raise SignupAlertError("payload_too_large", 413)
    msg_id = verify_svix(headers, body)
    try:
        event = json.loads(body)
    except ValueError as exc:
        raise SignupAlertError("invalid_json", 400) from exc
    if not isinstance(event, dict) or event.get("type") != "user.created":
        return {"status": "ignored", "type": str(event.get("type")) if isinstance(event, dict) else None}
    if not remember_message(msg_id):
        return {"status": "duplicate"}
    user = summarize_user(event.get("data") or {})
    await send_email(build_email(user), post=post)
    record_usage_event(
        "clerk_user_created",
        surface="clerk",
        identity_hash=fingerprint(f"email:{user['email'].casefold()}"),
        metadata={"email_verified": user["email_verified"]},
    )
    logger.info("signup alert sent for new Clerk user (verified=%s)", user["email_verified"])
    return {"status": "sent"}
