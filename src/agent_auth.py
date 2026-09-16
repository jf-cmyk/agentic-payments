"""Opt-in Auth.md service_auth delegations for the existing Clerk connector.

No anonymous credentials, identity-by-header, new ledger identities, or refresh
credentials. Every delegated request revalidates the original Clerk-backed token.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import hmac
import html
import json
import os
from pathlib import Path
import secrets
import sqlite3
import time
from urllib.parse import parse_qs, urlencode, urlsplit

from cryptography.fernet import Fernet
import jwt
from mcp.server.auth.provider import AuthorizationParams
from mcp.shared.auth import OAuthClientInformationFull
from pydantic import AnyUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

from src.connector_auth import identity_from_access_token

CLAIM_GRANT = "urn:workos:agent-auth:grant-type:claim"
ASSERTION_GRANT = "urn:ietf:params:oauth:grant-type:jwt-bearer"
COOKIE = "__Host-blocksize-agent"
TTL = 600
HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache",
           "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff",
           "Content-Security-Policy": "default-src 'none'; form-action 'self'; frame-ancestors 'none'"}


def enabled() -> bool:
    return os.environ.get("AGENT_AUTH_ENABLED", "").lower() == "true"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def random_token() -> str:
    return secrets.token_urlsafe(32)


def challenge(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")


class AuthError(Exception):
    def __init__(self, error: str, status: int = 400):
        self.error, self.status = error, status


class Store:
    """Encrypted, bounded-lived records with transactional compare/update semantics."""
    def __init__(self, path: str, secret: str):
        if len(secret) < 43:
            raise ValueError("AGENT_AUTH_SECRET must contain at least 32 random bytes (base64url)")
        self.path = path
        parent = Path(path).parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.box = Fernet(base64.urlsafe_b64encode(
            hmac.digest(secret.encode(), b"blocksize-agent-storage-v1", "sha256")))
        self.signing_key = hmac.digest(secret.encode(), b"blocksize-agent-assertion-v1", "sha256")
        with self.transaction() as db:
            db.execute("CREATE TABLE IF NOT EXISTS records (key TEXT PRIMARY KEY, "
                       "kind TEXT NOT NULL, expiry INTEGER NOT NULL, value BLOB NOT NULL)")
            db.execute("CREATE INDEX IF NOT EXISTS records_expiry ON records(expiry)")
        os.chmod(path, 0o600)

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.path, timeout=5, isolation_level=None)
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.execute("COMMIT")
        except BaseException:
            db.execute("ROLLBACK")
            raise
        finally:
            db.close()

    def get(self, db, kind, key):
        row = db.execute("SELECT value FROM records WHERE key=? AND kind=? AND expiry>?",
                         (digest(kind + key), kind, int(time.time()))).fetchone()
        return json.loads(self.box.decrypt(row[0])) if row else None

    def put(self, db, kind, key, value, expiry):
        db.execute("INSERT OR REPLACE INTO records VALUES (?,?,?,?)",
                   (digest(kind + key), kind, expiry,
                    self.box.encrypt(json.dumps(value).encode())))

    def delete(self, db, kind, key):
        db.execute("DELETE FROM records WHERE key=?", (digest(kind + key),))

    def audit(self, db, event, registration):
        now = int(time.time())
        self.put(db, "audit", random_token(), {"event": event, "registration": registration,
                 "at": now}, now + 30 * 86400)

    def rate(self, db, key, limit, seconds=3600):
        now = int(time.time())
        bucket = self.get(db, "rate", key) or {"count": 0, "exp": now + seconds}
        if bucket["count"] >= limit:
            raise AuthError("slow_down", 429)
        bucket["count"] += 1
        self.put(db, "rate", key, bucket, bucket["exp"])


class AgentAuth:
    def __init__(self, provider, *, issuer, secret, path, scopes):
        self.provider = provider
        self.issuer = issuer.rstrip("/")
        parts = urlsplit(self.issuer)
        if parts.scheme != "https" or parts.query or parts.fragment:
            raise ValueError("Agent auth requires a canonical HTTPS connector issuer")
        self.origin = f"{parts.scheme}://{parts.netloc}"
        self.base = self.issuer + "/agent"
        self.callback_uri = self.base + "/callback"
        if not {"email", "profile"}.issubset(scopes) or not set(scopes) <= {"openid", "email", "profile"}:
            raise ValueError("Agent auth requires only identity scopes: email, profile, optional openid")
        self.scopes = scopes
        self.store = Store(path, secret)
        self.client = OAuthClientInformationFull(
            client_id="blocksize-agent-consent-v1", client_name="Blocksize agent consent",
            redirect_uris=[AnyUrl(self.callback_uri)],
            token_endpoint_auth_method="none", grant_types=["authorization_code"],
            response_types=["code"], scope=" ".join(scopes))

    def metadata(self):
        return {"revocation_endpoint": self.base + "/revoke",
                "agent_auth": {"skill": self.origin + "/auth.md",
                               "identity_endpoint": self.base + "/identity",
                               "identity_types_supported": ["service_auth"]}}

    async def body(self, request, *, form=False):
        raw = b""
        async for part in request.stream():
            raw += part
            if len(raw) > 8192:
                raise AuthError("invalid_request", 413)
        try:
            if form:
                values = parse_qs(raw.decode(), keep_blank_values=True, max_num_fields=20)
                if any(len(v) != 1 for v in values.values()):
                    raise ValueError()
                return {k: v[0] for k, v in values.items()}
            value = json.loads(raw)
            if not isinstance(value, dict):
                raise ValueError()
            return value
        except (ValueError, UnicodeDecodeError):
            raise AuthError("invalid_request") from None

    def json(self, data, status=200):
        return JSONResponse(data, status_code=status, headers=HEADERS)

    def page(self, content):
        return HTMLResponse("<!doctype html><html lang=en><meta charset=utf-8>"
                            "<meta name=viewport content='width=device-width'>"
                            "<title>Blocksize agent access</title><main>"
                            "<h1>Blocksize agent access</h1>" + content + "</main></html>",
                            headers=HEADERS)

    async def guarded(self, request):
        try:
            handler = {"identity": self.register, "claim": self.claim,
                       "callback": self.callback, "consent": self.consent,
                       "agents": self.agents, "revoke": self.revoke}[request.url.path.rsplit("/", 1)[-1]]
            return await handler(request)
        except AuthError as exc:
            return self.json({"error": exc.error}, exc.status)

    async def register(self, request):
        body = await self.body(request)
        email = body.get("login_hint")
        name = body.get("agent_name", "Agent")
        if body.get("type") != "service_auth":
            raise AuthError("unsupported_identity_type")
        if (not isinstance(email, str) or len(email) > 254 or email.count("@") != 1
                or any(c.isspace() for c in email) or not all(email.split("@"))
                or not isinstance(name, str) or not 1 <= len(name) <= 80):
            raise AuthError("invalid_request")
        now = int(time.time())
        reg, claim, attempt = "reg_" + random_token(), random_token(), random_token()
        code = f"{secrets.randbelow(1000000):06d}"
        row = {"id": reg, "email": email.casefold(), "name": name, "code": digest(code),
               "attempts": 0, "exp": now + TTL, "status": "pending"}
        with self.store.transaction() as db:
            db.execute("DELETE FROM records WHERE expiry<=?", (now,))
            self.store.rate(db, "registration-global", 2000)
            self.store.rate(db, "registration-email:" + email.casefold(), 10)
            self.store.put(db, "registration", reg, row, row["exp"])
            self.store.put(db, "claim", claim, {"id": reg}, row["exp"])
            self.store.put(db, "attempt", attempt, {"id": reg}, row["exp"])
            self.store.audit(db, "registered", reg)
        url = self.base + "/claim?" + urlencode({"claim_attempt_token": attempt})
        return self.json({"registration_id": reg, "registration_type": "service_auth",
                          "claim_token": claim, "claim_token_expires": datetime.fromtimestamp(now + TTL, timezone.utc).isoformat(),
                          "claim_url": url, "post_claim_scopes": self.scopes,
                          "claim": {"user_code": code, "expires_in": TTL,
                                    "verification_uri": url, "interval": 5}})

    async def login(self, registration=None):
        sid, state, verifier = random_token(), random_token(), random_token()
        now = int(time.time())
        with self.store.transaction() as db:
            self.store.rate(db, "login-global", 2000)
            self.store.put(db, "session", sid, {"state": digest(state), "verifier": verifier,
                           "registration": registration, "exp": now + TTL}, now + TTL)
        await self.provider.register_client(self.client)
        url = await self.provider.authorize(self.client, AuthorizationParams(
            state=state, scopes=self.scopes, code_challenge=challenge(verifier),
            redirect_uri=AnyUrl(self.callback_uri), redirect_uri_provided_explicitly=True,
            resource=self.issuer + "/"))
        response = RedirectResponse(url, status_code=303, headers=HEADERS)
        response.set_cookie(COOKIE, sid, secure=True, httponly=True, samesite="lax",
                            max_age=TTL, path="/")
        return response

    async def claim(self, request):
        attempt = request.query_params.get("claim_attempt_token", "")
        with self.store.transaction() as db:
            ref = self.store.get(db, "attempt", attempt)
            row = self.store.get(db, "registration", ref["id"]) if ref else None
        if not row or row["status"] != "pending":
            raise AuthError("expired_token")
        return await self.login(row["id"])

    async def callback(self, request):
        sid = request.cookies.get(COOKIE, "")
        with self.store.transaction() as db:
            session = self.store.get(db, "session", sid)
            if (not session or "state" not in session or not hmac.compare_digest(
                    session["state"], digest(request.query_params.get("state", "")))):
                raise AuthError("invalid_state")
            # Consume state BEFORE awaiting any network or exchanging a one-use code.
            self.store.delete(db, "session", sid)
        code = await self.provider.load_authorization_code(
            self.client, request.query_params.get("code", ""))
        if (not code or code.expires_at <= time.time()
                or str(code.redirect_uri) != self.callback_uri
                or not hmac.compare_digest(code.code_challenge, challenge(session["verifier"]))):
            raise AuthError("invalid_grant")
        issued = await self.provider.exchange_authorization_code(self.client, code)
        token = await self.provider.load_access_token(issued.access_token)
        claims = token.claims or {} if token else {}
        upstream = claims.get("upstream_claims")
        if isinstance(upstream, dict):
            claims = {**claims, **upstream}
        identity = identity_from_access_token(token, namespace="ANTHROPIC") if token else None
        if (not identity or not identity.email or claims.get("email_verified") is not True
                or not token.expires_at or not set(self.scopes).issubset(token.scopes)):
            raise AuthError("verified_account_required", 403)
        expiry = min(int(token.expires_at), int(time.time()) + 3600,
                     int(time.time()) + int(issued.expires_in or 0))
        if expiry <= time.time():
            raise AuthError("expired_token")
        new_sid = random_token()
        approved = {"registration": session["registration"], "owner": identity.ledger_subject,
                    "email": identity.email.casefold(), "upstream": issued.access_token,
                    "exp": expiry, "csrf": random_token()}
        with self.store.transaction() as db:
            self.store.put(db, "session", new_sid, approved, expiry)
        response = RedirectResponse(self.base + "/agents" if not session["registration"]
                                    else self.base + "/consent", status_code=303, headers=HEADERS)
        response.set_cookie(COOKIE, new_sid, secure=True, httponly=True, samesite="lax",
                            max_age=expiry-int(time.time()), path="/")
        return response

    async def session(self, request):
        with self.store.transaction() as db:
            session = self.store.get(db, "session", request.cookies.get(COOKIE, ""))
        if not session or "owner" not in session:
            raise AuthError("login_required", 401)
        token = await self.provider.load_access_token(session["upstream"])
        identity = identity_from_access_token(token, namespace="ANTHROPIC") if token else None
        if not identity or identity.ledger_subject != session["owner"]:
            raise AuthError("login_required", 401)
        return session

    def csrf(self, request, session, body):
        if (request.headers.get("origin") != self.origin or not hmac.compare_digest(
                str(body.get("csrf", "")), session["csrf"])):
            raise AuthError("invalid_csrf", 403)

    async def consent(self, request):
        session = await self.session(request)
        with self.store.transaction() as db:
            row = self.store.get(db, "registration", session["registration"] or "")
        if not row or row["status"] != "pending":
            raise AuthError("expired_token")
        if not hmac.compare_digest(row["email"].encode(), session["email"].encode()):
            raise AuthError("account_mismatch", 403)
        if request.method == "GET":
            return self.page(f"<p>Signed in as {html.escape(session['email'])}.</p>"
                f"<h2>Authorize this agent?</h2><p>Unverified agent label: {html.escape(row['name'])}</p>"
                "<p>This agent can read paid market data and spend your existing Blocksize "
                "connector credits under your account entitlement. It cannot add funds, "
                "send payments, or execute trades. Access lasts up to one hour.</p>"
                "<p>Only continue if you started this request. Enter the six-digit code "
                "shown by your agent. Never enter a code sent by someone else.</p>"
                f"<form method=post action='{self.base}/consent'>"
                f"<input type=hidden name=csrf value='{session['csrf']}'>"
                "<label>Agent code <input name=user_code pattern='[0-9]{6}' "
                "inputmode=numeric maxlength=6 required autocomplete=off></label>"
                "<button name=decision value=approve>Allow agent to use my credits</button>"
                "<button name=decision value=deny formnovalidate>Deny access</button></form>")
        body = await self.body(request, form=True)
        self.csrf(request, session, body)
        error = None
        with self.store.transaction() as db:
            row = self.store.get(db, "registration", session["registration"])
            if not row or row["status"] != "pending":
                raise AuthError("expired_token")
            if body.get("decision") == "deny":
                row["status"] = "denied"
            elif body.get("decision") != "approve":
                raise AuthError("invalid_request")
            elif not hmac.compare_digest(row["code"], digest(str(body.get("user_code", "")))):
                row["attempts"] += 1
                if row["attempts"] >= 5:
                    row["status"] = "denied"
                error = "invalid_user_code"
            else:
                row.update(status="approved", owner=session["owner"], upstream=session["upstream"],
                           exp=session["exp"], access_token="bsa_" + random_token())
                self.store.put(db, "access", row["access_token"], {"id": row["id"]}, row["exp"])
            self.store.put(db, "registration", row["id"], row, row["exp"])
            self.store.audit(db, "code_rejected" if error else row["status"], row["id"])
        if error:
            raise AuthError(error)
        return self.page("<p>Access " + ("approved" if row["status"] == "approved" else "denied")
                         + f".</p><p><a href='{self.base}/agents'>Manage agent access</a></p>")

    async def agents(self, request):
        try:
            session = await self.session(request)
        except AuthError:
            if request.method == "GET":
                return await self.login()
            raise
        if request.method == "POST":
            body = await self.body(request, form=True)
            self.csrf(request, session, body)
            with self.store.transaction() as db:
                row = self.store.get(db, "registration", str(body.get("registration_id", "")))
                if not row or row.get("owner") != session["owner"]:
                    raise AuthError("not_found", 404)
                row["status"] = "revoked"
                self.store.put(db, "registration", row["id"], row, row["exp"])
                self.store.audit(db, "delegation_revoked", row["id"])
        with self.store.transaction() as db:
            records = db.execute("SELECT value FROM records WHERE kind='registration' AND expiry>?",
                                 (int(time.time()),)).fetchall()
        content = (f"<p>Signed in as {html.escape(session['email'])}.</p>"
                   "<p>Revocation stops new requests immediately. Requests already in progress may finish.</p>")
        for record in records:
            row = json.loads(self.store.box.decrypt(record[0]))
            if row.get("owner") == session["owner"]:
                content += (f"<p>{html.escape(row['name'])}: {html.escape(row['status'])}</p>"
                            f"<form method=post action='{self.base}/agents'>"
                            f"<input type=hidden name=csrf value='{session['csrf']}'>"
                            f"<input type=hidden name=registration_id value='{row['id']}'>"
                            "<button>Revoke access</button></form>")
        return self.page(content)

    def active(self, db, registration):
        row = self.store.get(db, "registration", registration)
        if not row or row["status"] != "approved":
            raise AuthError("invalid_grant")
        return row

    async def delegated_token(self, value):
        with self.store.transaction() as db:
            ref = self.store.get(db, "access", value)
            try:
                row = self.active(db, ref["id"]) if ref else None
            except AuthError:
                return None
        if not row:
            return None
        token = await self.provider.load_original_token(row["upstream"])
        identity = identity_from_access_token(token, namespace="ANTHROPIC") if token else None
        if not identity or identity.ledger_subject != row["owner"]:
            return None
        # Re-check after upstream validation so a revoke during that await wins.
        with self.store.transaction() as db:
            try:
                self.active(db, row["id"])
                if not self.store.get(db, "access", value):
                    return None
            except AuthError:
                return None
        return token.model_copy(update={"token": value, "expires_at": row["exp"]})

    async def token(self, request, body):
        if body.get("resource", self.issuer + "/") != self.issuer + "/":
            raise AuthError("invalid_target")
        if body.get("scope") and set(body["scope"].split()) != set(self.scopes):
            raise AuthError("invalid_scope")
        with self.store.transaction() as db:
            if body["grant_type"] == CLAIM_GRANT:
                ref = self.store.get(db, "claim", body.get("claim_token", ""))
                row = self.store.get(db, "registration", ref["id"]) if ref else None
                if not row:
                    raise AuthError("expired_token")
                self.store.rate(db, "poll:" + row["id"], 1, 5)
                if row["status"] == "pending":
                    # Commit rate record even for pending responses.
                    return self.json({"error": "authorization_pending"}, 400)
                if row["status"] != "approved":
                    return self.json({"error": "access_denied"}, 400)
            else:
                try:
                    assertion = body.get("assertion", "")
                    if jwt.get_unverified_header(assertion).get("typ") != "oauth-id-jag+jwt":
                        raise ValueError()
                    claims = jwt.decode(assertion, self.store.signing_key, algorithms=["HS256"],
                                        issuer=self.issuer, audience=self.issuer,
                                        options={"require": ["exp", "iat", "sub", "jti"]})
                    row = self.active(db, claims["sub"])
                except (jwt.PyJWTError, ValueError, KeyError):
                    raise AuthError("invalid_grant") from None
            if not self.store.get(db, "access", row["access_token"]):
                row["access_token"] = "bsa_" + random_token()
                self.store.put(db, "access", row["access_token"], {"id": row["id"]}, row["exp"])
                self.store.put(db, "registration", row["id"], row, row["exp"])
            self.store.rate(db, "exchange:" + row["id"], 60, 60)
            self.store.audit(db, "token_exchanged", row["id"])
        if not await self.delegated_token(row["access_token"]):
            raise AuthError("invalid_grant")
        assertion = jwt.encode({"iss": self.issuer, "aud": self.issuer, "sub": row["id"],
                                "iat": int(time.time()), "exp": row["exp"], "jti": row["id"]},
                               self.store.signing_key, algorithm="HS256",
                               headers={"typ": "oauth-id-jag+jwt"})
        return self.json({"access_token": row["access_token"], "token_type": "Bearer",
                          "expires_in": max(0, row["exp"]-int(time.time())),
                          "scope": " ".join(self.scopes), "identity_assertion": assertion})

    async def revoke(self, request):
        body = await self.body(request, form=True)
        if not body.get("token"):
            raise AuthError("invalid_request")
        with self.store.transaction() as db:
            ref = self.store.get(db, "access", body.get("token", ""))
            row = self.store.get(db, "registration", ref["id"]) if ref else None
            if row:
                self.store.delete(db, "access", body.get("token", ""))
                self.store.audit(db, "token_revoked", row["id"])
        return Response(status_code=200, headers=HEADERS)

    def routes(self):
        return [Route("/agent/" + name, self.guarded, methods=methods)
                for name, methods in [("identity", ["POST"]), ("claim", ["GET"]),
                                      ("callback", ["GET"]), ("consent", ["GET", "POST"]),
                                      ("agents", ["GET", "POST"]), ("revoke", ["POST"])]]


class TokenDispatch:
    """Extend /token without modifying ordinary OAuth grants or their middleware."""
    def __init__(self, original, service):
        self.original, self.service = original, service

    async def __call__(self, scope, receive, send):
        if scope["method"] != "POST":
            return await self.original(scope, receive, send)
        request = Request(scope, receive)
        try:
            body = await self.service.body(request, form=True)
            if body.get("grant_type") in {CLAIM_GRANT, ASSERTION_GRANT}:
                response = await self.service.token(request, body)
                return await response(scope, receive, send)
        except AuthError as exc:
            return await self.service.json({"error": exc.error}, exc.status)(scope, receive, send)
        # Replay parsed bounded form for standard grants; all framework checks remain.
        encoded = urlencode(body).encode()
        replayed = False

        async def replay():
            nonlocal replayed
            if not replayed:
                replayed = True
                return {"type": "http.request", "body": encoded, "more_body": False}
            return await receive()

        await self.original(scope, replay, send)


class MetadataDispatch:
    """Keep mounted OAuth metadata consistent with the root discovery aliases."""
    def __init__(self, original, service):
        self.original, self.service = original, service

    async def __call__(self, scope, receive, send):
        if scope["method"] not in {"GET", "HEAD"}:
            return await self.original(scope, receive, send)
        messages = []

        async def capture(message):
            messages.append(message)

        await self.original(scope, receive, capture)
        start = next(message for message in messages if message["type"] == "http.response.start")
        if start["status"] != 200:
            for message in messages:
                await send(message)
            return
        body = b"".join(m.get("body", b"") for m in messages if m["type"] == "http.response.body")
        metadata = json.loads(body)
        metadata.update(self.service.metadata())
        metadata["grant_types_supported"] = list(dict.fromkeys([
            *metadata.get("grant_types_supported", []), CLAIM_GRANT, ASSERTION_GRANT]))
        metadata["token_endpoint_auth_methods_supported"] = list(dict.fromkeys([
            *metadata.get("token_endpoint_auth_methods_supported", []), "none"]))
        headers = {k.decode(): v.decode() for k, v in start["headers"]
                   if k.lower() not in {b"content-length", b"content-type"}}
        await JSONResponse(metadata, headers=headers)(scope, receive, send)
