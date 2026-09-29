"""Signup alerts, welcome emails, and the daily signup digest.

Clerk delivers ``user.created`` webhooks through Svix. The receiver verifies
the Svix signature with ``CLERK_WEBHOOK_SECRET``, then:

1. records the signup in the encrypted signup store (``SIGNUP_DB_SECRET``),
2. emails the operator (``SIGNUP_ALERT_TO``) through the Resend API,
3. sends the new user a welcome email when ``USER_EMAIL_FROM`` names a sender
   on a domain verified in Resend (otherwise the welcome is skipped).

``build_digest`` and ``send_digest`` produce the daily summary of signups,
activations, free-tier thresholds and upgrade clicks for the operator.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import html
import json
import logging
import os
import time
from collections import Counter
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from src import signup_store
from src.free_tier import grant_key_for_email
from src.observability import fingerprint, get_global_store, record_usage_event

logger = logging.getLogger(__name__)

SIGNATURE_TOLERANCE_SECONDS = 300
MAX_BODY_BYTES = 64 * 1024
RESEND_ENDPOINT = "https://api.resend.com/emails"
DEFAULT_FROM = "Blocksize signups <onboarding@resend.dev>"
PUBLIC_BASE = "https://mcp.blocksize.info"
TERMS_URL = "https://blocksize.info/terms-conditions-data/"
_SEEN_MESSAGE_IDS: dict[str, float] = {}


class SignupAlertError(Exception):
    def __init__(self, error: str, status: int = 400):
        super().__init__(error)
        self.error = error
        self.status = status


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def configured() -> bool:
    return bool(os.environ.get("CLERK_WEBHOOK_SECRET", "").strip()) and email_configured()


def email_configured() -> bool:
    return bool(os.environ.get("RESEND_API_KEY", "").strip())


def alert_recipient() -> str:
    return os.environ.get("SIGNUP_ALERT_TO", "jf@blocksize-capital.com").strip()


def user_email_sender() -> str:
    """Sender for emails to users; empty means user emails are off."""
    return os.environ.get("USER_EMAIL_FROM", "").strip()


def digest_hour_utc() -> int:
    try:
        return max(0, min(23, int(os.environ.get("SIGNUP_DIGEST_HOUR_UTC", "7"))))
    except ValueError:
        return 7


# ---------------------------------------------------------------------------
# Svix verification
# ---------------------------------------------------------------------------


def _secret_bytes() -> bytes:
    raw = os.environ.get("CLERK_WEBHOOK_SECRET", "").strip()
    if raw.startswith("whsec_"):
        raw = raw[len("whsec_") :]
    try:
        return base64.b64decode(raw + "=" * (-len(raw) % 4))
    except (ValueError, TypeError) as exc:
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


# ---------------------------------------------------------------------------
# Clerk user payload
# ---------------------------------------------------------------------------


def summarize_user(data: dict[str, Any]) -> dict[str, str]:
    """Pick the few fields worth keeping from a Clerk user object."""
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


def grant_hash_for(email: str) -> str | None:
    """The same salted value free-tier usage events carry as ``grant_hash``."""
    key = grant_key_for_email(email)
    return fingerprint(key) if key else None


# ---------------------------------------------------------------------------
# Emails
# ---------------------------------------------------------------------------


def _table(rows: list[tuple[str, str]]) -> str:
    cells = "".join(
        f"<tr><td style='padding:4px 12px 4px 0;color:#555'>{html.escape(k)}</td>"
        f"<td style='padding:4px 0'>{html.escape(v)}</td></tr>"
        for k, v in rows
    )
    return f"<table style='font-family:system-ui,sans-serif;font-size:14px'>{cells}</table>"


def build_email(user: dict[str, str]) -> dict[str, str]:
    """Operator alert for one new signup."""
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
    body_html = (
        "<p>A new user signed up through the Blocksize MCP connectors (Clerk).</p>"
        + _table([
            ("Email", user["email"]),
            ("Name", user["name"]),
            ("Email status", user["email_verified"]),
            ("Signed up", user["created_at"]),
            ("Clerk user", user["user_id"]),
        ])
        + "<p>They get 15,000 free live-data credits this month.</p>"
    )
    return {"subject": subject, "text": text, "html": body_html}


def build_welcome_email(user: dict[str, str]) -> dict[str, str]:
    """Welcome email to the new user; plain, short, with one next step."""
    first = user["name"].split(" ")[0] if user["name"] != "not given" else ""
    greeting = f"Hi {first}," if first else "Hi,"
    trial = f"{PUBLIC_BASE}/go/free-trial?utm_source=email&utm_medium=email&utm_campaign=free-tier-welcome"
    pricing = f"{PUBLIC_BASE}/go/pricing?utm_source=email&utm_medium=email&utm_campaign=free-tier-welcome"
    quickstart = f"{PUBLIC_BASE}/quickstart/first-price"
    text = (
        f"{greeting}\n\n"
        "Thanks for connecting Blocksize live market data. Your account includes "
        "15,000 free live-data credits every calendar month: one credit per price, "
        "two for FX and metals.\n\n"
        "Try it now in your assistant:\n"
        '  "What is the current multi-venue VWAP for BTC/USD?"\n'
        '  "Compare the bid/ask spread for ETH/USD and SOL/USD."\n'
        '  "How many Blocksize credits do I have left this month?"\n\n'
        f"Quickstart and examples: {quickstart}\n\n"
        "The free tier is an evaluation licence with \"Data by Blocksize\" attribution. "
        "When you need production use, more feeds or history, plans start at EUR 49 "
        f"per month with a free trial: {trial}\n"
        f"Compare plans: {pricing}\n\n"
        "Reply to this email if anything is unclear. We read every message.\n\n"
        "Blocksize Capital GmbH\n"
        f"Data terms: {TERMS_URL}\n"
        "You receive this because you created a Blocksize connector account.\n"
    )
    body_html = (
        f"<p>{html.escape(greeting)}</p>"
        "<p>Thanks for connecting Blocksize live market data. Your account includes "
        "<b>15,000 free live-data credits every calendar month</b>: one credit per price, "
        "two for FX and metals.</p>"
        "<p>Try it now in your assistant:</p><ul>"
        "<li>\"What is the current multi-venue VWAP for BTC/USD?\"</li>"
        "<li>\"Compare the bid/ask spread for ETH/USD and SOL/USD.\"</li>"
        "<li>\"How many Blocksize credits do I have left this month?\"</li></ul>"
        f"<p>Quickstart and examples: <a href='{quickstart}'>{quickstart}</a></p>"
        "<p>The free tier is an evaluation licence with \"Data by Blocksize\" attribution. "
        "When you need production use, more feeds or history, plans start at EUR 49 per month "
        f"with a <a href='{trial}'>free trial</a>. <a href='{pricing}'>Compare plans</a>.</p>"
        "<p>Reply to this email if anything is unclear. We read every message.</p>"
        f"<p style='color:#777;font-size:12px'>Blocksize Capital GmbH · <a href='{TERMS_URL}'>Data terms</a> · "
        "You receive this because you created a Blocksize connector account.</p>"
    )
    return {"subject": "Your Blocksize live market data access", "text": text, "html": body_html}


async def send_email(
    message: dict[str, str],
    *,
    to: str | None = None,
    sender: str | None = None,
    reply_to: str | None = None,
    post=None,
) -> None:
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    payload: dict[str, Any] = {
        "from": sender or os.environ.get("SIGNUP_ALERT_FROM", DEFAULT_FROM).strip() or DEFAULT_FROM,
        "to": [to or alert_recipient()],
        "subject": message["subject"],
        "text": message["text"],
        "html": message["html"],
    }
    if reply_to:
        payload["reply_to"] = reply_to
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        if post is not None:
            response = await post(RESEND_ENDPOINT, json=payload, headers=headers, timeout=10.0)
        else:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.post(RESEND_ENDPOINT, json=payload, headers=headers)
    except Exception as exc:  # noqa: BLE001 - report the kind, never the payload
        logger.warning("email send failed: %s", type(exc).__name__)
        raise SignupAlertError("email_send_failed", 502) from exc
    status = getattr(response, "status_code", 0)
    if status >= 300:
        logger.warning("email rejected by provider: http_%s", status)
        raise SignupAlertError("email_send_failed", 502)


# ---------------------------------------------------------------------------
# Webhook handling
# ---------------------------------------------------------------------------


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
    grant_hash = grant_hash_for(user["email"])
    stored = signup_store.record(grant_hash, user)
    await send_email(build_email(user), post=post)
    welcome = "skipped"
    sender = user_email_sender()
    if sender and user["email"] != "unknown" and "@" in user["email"]:
        try:
            await send_email(
                build_welcome_email(user), to=user["email"], sender=sender,
                reply_to=alert_recipient(), post=post,
            )
            welcome = "sent"
        except SignupAlertError:
            welcome = "failed"
    record_usage_event(
        "clerk_user_created",
        surface="clerk",
        identity_hash=fingerprint(f"email:{user['email'].casefold()}"),
        metadata={"email_verified": user["email_verified"], "grant_hash": grant_hash,
                  "welcome_email": welcome, "stored": stored},
    )
    logger.info("signup alert sent for new Clerk user (verified=%s, welcome=%s, stored=%s)",
                user["email_verified"], welcome, stored)
    return {"status": "sent", "welcome_email": welcome, "stored": stored}


# ---------------------------------------------------------------------------
# Daily digest
# ---------------------------------------------------------------------------


def build_digest(
    events: list[dict[str, Any]],
    signups: list[dict[str, Any]],
    *,
    since: datetime,
    until: datetime,
    known: dict[str, dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Summarize one window of usage events and signups for the operator.

    ``events`` are usage-event dicts (with ``metadata``) from the window,
    ``signups`` the stored signups from the window, and ``known`` maps grant
    hashes seen in events to stored signups, so activity can be named.
    """
    known = known or {}
    grants: set[str] = set()
    exhausted: set[str] = set()
    thresholds: dict[str, set[str]] = {}
    denials: Counter[str] = Counter()
    cta_impressions = 0
    go_clicks: Counter[str] = Counter()
    agent_steps: Counter[str] = Counter()
    clerk_signups = 0
    first_live: set[str] = set()
    for event in events:
        name = str(event.get("event") or "")
        meta = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        grant_hash = str(meta.get("grant_hash") or "")
        if name == "free_tier_grant_created" and grant_hash:
            grants.add(grant_hash)
        elif name == "free_tier_threshold_crossed" and grant_hash:
            thresholds.setdefault(f"{meta.get('threshold_pct', '?')}%", set()).add(grant_hash)
        elif name == "free_tier_exhausted" and grant_hash:
            exhausted.add(grant_hash)
        elif name == "free_tier_rate_limited":
            denials[str(event.get("reason") or "rate_limited")] += 1
        elif name == "mcp_credit_drawdown_failed":
            reason = str(event.get("reason") or "")
            if reason.startswith(("free_", "ineligible_")) or reason == "suspended":
                denials[reason] += 1
        elif name == "upgrade_cta_shown":
            cta_impressions += 1
        elif name == "outbound_conversion_click":
            go_clicks[str(meta.get("destination") or event.get("subject") or "unknown")] += 1
        elif name.startswith("agent_auth_"):
            agent_steps[name.removeprefix("agent_auth_")] += 1
        elif name == "clerk_user_created":
            clerk_signups += 1
        elif name == "first_live_price_delivered":
            first_live.add(str(meta.get("identity_hash") or ""))

    def label(grant_hash: str) -> str:
        info = known.get(grant_hash)
        return str(info.get("email")) if info else f"grant {grant_hash[:10]}"

    activated = [s for s in signups if s.get("grant_hash") in grants]
    not_activated = [s for s in signups if s.get("grant_hash") not in grants]
    return {
        "window": {"since": since.isoformat(timespec="minutes"), "until": until.isoformat(timespec="minutes")},
        "signups": signups,
        "signups_count": len(signups) if signups else clerk_signups,
        "activated_signups": activated,
        "not_activated_signups": not_activated,
        "free_tier_grants": sorted(label(g) for g in grants),
        "thresholds": {level: sorted(label(g) for g in hashes) for level, hashes in sorted(thresholds.items())},
        "exhausted": sorted(label(g) for g in exhausted),
        "denials": dict(denials.most_common()),
        "cta_impressions": cta_impressions,
        "go_clicks": dict(go_clicks.most_common()),
        "agent_auth": dict(agent_steps.most_common()),
        "first_live_price_identities": len({h for h in first_live if h}),
    }


def render_digest(digest: dict[str, Any]) -> dict[str, str]:
    w = digest["window"]
    day = w["since"][:10]
    lines: list[str] = [f"Blocksize connector digest for {day} (UTC {w['since'][11:16]} to {w['until'][11:16]})", ""]
    lines.append(f"New signups: {digest['signups_count']}")
    for s in digest["signups"]:
        mark = "activated" if s in digest["activated_signups"] else "no live call yet"
        lines.append(f"  - {s.get('email')}  ({s.get('name')}, {s.get('email_verified')})  {mark}")
    lines.append("")
    lines.append(f"Free-tier grants (first live call): {len(digest['free_tier_grants'])}")
    for g in digest["free_tier_grants"]:
        lines.append(f"  - {g}")
    if digest["thresholds"]:
        lines.append("")
        lines.append("Pool thresholds crossed:")
        for level, who in digest["thresholds"].items():
            lines.append(f"  {level}: " + ", ".join(who))
    if digest["exhausted"]:
        lines.append("")
        lines.append("Pool exhausted: " + ", ".join(digest["exhausted"]))
    lines.append("")
    lines.append(f"Upgrade prompts shown: {digest['cta_impressions']}; link clicks: "
                 + (", ".join(f"{k} {v}" for k, v in digest["go_clicks"].items()) or "none"))
    if digest["denials"]:
        lines.append("Free-tier denials: " + ", ".join(f"{k} {v}" for k, v in digest["denials"].items()))
    if digest["agent_auth"]:
        lines.append("Agent registrations: " + ", ".join(f"{k} {v}" for k, v in digest["agent_auth"].items()))
    lines.append(f"Identities with a first live price (all rails): {digest['first_live_price_identities']}")
    if digest["not_activated_signups"]:
        lines.append("")
        lines.append("Worth a personal note (signed up, no live call yet):")
        for s in digest["not_activated_signups"]:
            lines.append(f"  - {s.get('email')}")
    text = "\n".join(lines) + "\n"
    body_html = "<pre style='font-family:ui-monospace,Menlo,monospace;font-size:13px'>" + html.escape(text) + "</pre>"
    subject = f"Blocksize digest {day}: {digest['signups_count']} signups, {len(digest['free_tier_grants'])} activations"
    return {"subject": subject, "text": text, "html": body_html}


def digest_window(now: datetime | None = None, *, hours: int = 24) -> tuple[datetime, datetime]:
    until = now or datetime.now(UTC)
    return until - timedelta(hours=hours), until


async def send_digest(store, *, hours: int = 24, now: datetime | None = None, post=None) -> dict[str, Any]:
    """Compose and send the digest for the trailing window; returns the digest."""
    since, until = digest_window(now, hours=hours)
    events = store.events_since(since.isoformat()) if store is not None else []
    signups = signup_store.signups_since(since.isoformat())
    hashes = {str((e.get("metadata") or {}).get("grant_hash") or "") for e in events}
    known = signup_store.lookup({h for h in hashes if h})
    digest = build_digest(events, signups, since=since, until=until, known=known)
    await send_email(render_digest(digest), post=post)
    logger.info("signup digest sent: %d signups, %d grants", digest["signups_count"], len(digest["free_tier_grants"]))
    return digest


# ---------------------------------------------------------------------------
# Lifecycle emails to users: pool thresholds and the no-activity nudge
# ---------------------------------------------------------------------------

NUDGE_AFTER_HOURS = 48
NUDGE_WINDOW_HOURS = 24
LIFECYCLE_THRESHOLDS = (80, 100)


def _campaign_links(campaign: str) -> tuple[str, str]:
    tags = f"utm_source=email&utm_medium=email&utm_campaign={campaign}"
    return f"{PUBLIC_BASE}/go/free-trial?{tags}", f"{PUBLIC_BASE}/go/pricing?{tags}"


def _footer_text() -> str:
    return ("\nReply to this email if anything is unclear.\n\nBlocksize Capital GmbH\n"
            f"Data terms: {TERMS_URL}\n"
            "You receive this because you created a Blocksize connector account.\n")


def _footer_html(trial: str, pricing: str) -> str:
    return (
        f"<p>Start a <a href='{trial}'>free trial</a> or <a href='{pricing}'>compare plans</a>. "
        "Reply to this email if anything is unclear.</p>"
        f"<p style='color:#777;font-size:12px'>Blocksize Capital GmbH · <a href='{TERMS_URL}'>Data terms</a> · "
        "You receive this because you created a Blocksize connector account.</p>"
    )


def build_threshold_email(user: dict[str, Any], pct: int, snapshot: dict[str, Any]) -> dict[str, str]:
    first = str(user.get("name", "")).split(" ")[0] if user.get("name") not in (None, "not given") else ""
    greeting = f"Hi {first}," if first else "Hi,"
    limit = int(snapshot.get("monthly_limit") or 15000)
    remaining = int(snapshot.get("credits_remaining") or 0)
    resets = str(snapshot.get("resets_at") or "the first of next month")
    trial, pricing = _campaign_links(f"free-tier-{pct}")
    if pct >= 100:
        subject = "Your free Blocksize credits are used up for this month"
        lead = (f"You have used all {limit:,} free live-data credits for this month. "
                f"They reset on {resets}.")
    else:
        subject = f"You have used {pct}% of your free Blocksize credits"
        lead = (f"You have used {pct}% of your {limit:,} free live-data credits this month; "
                f"{remaining:,} remain until they reset on {resets}.")
    text = (
        f"{greeting}\n\n{lead}\n\n"
        "If Blocksize data is becoming part of a workflow, a subscription removes the "
        "monthly limit and adds more feeds and history. Plans start at EUR 49 per month "
        f"with a free trial: {trial}\nCompare plans: {pricing}\n"
        + _footer_text()
    )
    body_html = (
        f"<p>{html.escape(greeting)}</p><p>{html.escape(lead)}</p>"
        "<p>If Blocksize data is becoming part of a workflow, a subscription removes the "
        "monthly limit and adds more feeds and history. Plans start at EUR 49 per month.</p>"
        + _footer_html(trial, pricing)
    )
    return {"subject": subject, "text": text, "html": body_html}


def build_nudge_email(user: dict[str, Any]) -> dict[str, str]:
    first = str(user.get("name", "")).split(" ")[0] if user.get("name") not in (None, "not given") else ""
    greeting = f"Hi {first}," if first else "Hi,"
    trial, pricing = _campaign_links("free-tier-nudge")
    quickstart = f"{PUBLIC_BASE}/quickstart/first-price"
    text = (
        f"{greeting}\n\n"
        "You connected Blocksize live market data two days ago but have not fetched a price yet. "
        "Your 15,000 free credits for this month are waiting.\n\n"
        "Try one of these in your assistant:\n"
        '  "What is the current multi-venue VWAP for BTC/USD?"\n'
        '  "Compare the bid/ask spread for ETH/USD and SOL/USD."\n\n'
        f"Quickstart: {quickstart}\n\n"
        "If something got in the way, reply and tell us. We read every message.\n"
        f"When you need production use, plans start at EUR 49 per month with a free trial: {trial}\n"
        f"Compare plans: {pricing}\n"
        + _footer_text()
    )
    body_html = (
        f"<p>{html.escape(greeting)}</p>"
        "<p>You connected Blocksize live market data two days ago but have not fetched a price yet. "
        "Your 15,000 free credits for this month are waiting.</p>"
        "<p>Try one of these in your assistant:</p><ul>"
        "<li>\"What is the current multi-venue VWAP for BTC/USD?\"</li>"
        "<li>\"Compare the bid/ask spread for ETH/USD and SOL/USD.\"</li></ul>"
        f"<p>Quickstart: <a href='{quickstart}'>{quickstart}</a></p>"
        "<p>If something got in the way, reply and tell us. We read every message.</p>"
        + _footer_html(trial, pricing)
    )
    return {"subject": "Your Blocksize credits are waiting", "text": text, "html": body_html}


def user_emails_enabled() -> bool:
    return bool(user_email_sender()) and email_configured() and signup_store.enabled()


async def notify_threshold(store, grant_hash: str, pct: int, snapshot: dict[str, Any], *, post=None) -> str:
    """Email the user once per threshold per month; returns what happened."""
    if pct not in LIFECYCLE_THRESHOLDS or not user_emails_enabled() or store is None:
        return "disabled"
    user = signup_store.lookup({grant_hash}).get(grant_hash)
    if not user or "@" not in str(user.get("email", "")):
        return "unknown_user"
    period = str(snapshot.get("period") or datetime.now(UTC).strftime("%Y-%m"))
    if not store.claim_milestone(f"user_email_threshold_{pct}_{period}", grant_hash):
        return "already_sent"
    await send_email(build_threshold_email(user, pct, snapshot), to=str(user["email"]),
                     sender=user_email_sender(), reply_to=alert_recipient(), post=post)
    record_usage_event("user_email_sent", surface="email", reason=f"threshold_{pct}",
                       metadata={"grant_hash": grant_hash, "campaign": f"free-tier-{pct}"})
    return "sent"


def notify_threshold_background(grant_hash: str | None, pct: int, snapshot: dict[str, Any]) -> None:
    """Fire-and-forget from the connector tool path; never raises."""
    if not grant_hash or pct not in LIFECYCLE_THRESHOLDS or not user_emails_enabled():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def run() -> None:
        try:
            await notify_threshold(get_global_store(), grant_hash, pct, dict(snapshot))
        except Exception as exc:  # noqa: BLE001 - lifecycle email must never affect data delivery
            logger.warning("threshold email failed: %s", type(exc).__name__)

    loop.create_task(run())


async def send_nudges(store, *, now: datetime | None = None, post=None) -> int:
    """Email signups that are two days old and have never made a live call."""
    if not user_emails_enabled() or store is None:
        return 0
    current = now or datetime.now(UTC)
    newest = current - timedelta(hours=NUDGE_AFTER_HOURS)
    oldest = newest - timedelta(hours=NUDGE_WINDOW_HOURS)
    sent = 0
    for signup in signup_store.signups_since(oldest.isoformat()):
        created = str(signup.get("created_at") or "")
        if created > newest.isoformat() or "@" not in str(signup.get("email", "")):
            continue
        grant_hash = str(signup.get("grant_hash") or "")
        activity = store.events_since(created)
        if any(str(e.get("event")) == "free_tier_grant_created"
               and str((e.get("metadata") or {}).get("grant_hash") or "") == grant_hash for e in activity):
            continue
        if not store.claim_milestone("user_email_nudge", grant_hash):
            continue
        await send_email(build_nudge_email(signup), to=str(signup["email"]),
                         sender=user_email_sender(), reply_to=alert_recipient(), post=post)
        record_usage_event("user_email_sent", surface="email", reason="nudge",
                           metadata={"grant_hash": grant_hash, "campaign": "free-tier-nudge"})
        sent += 1
    return sent
