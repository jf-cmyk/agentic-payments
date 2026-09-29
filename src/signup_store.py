"""Encrypted record of connector signups, so the operator can contact them.

Rows are keyed by the free-tier grant hash (the same salted value the usage
events carry as ``grant_hash``), which lets the daily digest join a signup to
its later activity without storing anything else in the telemetry store.
Bodies are Fernet-encrypted with a key derived from ``SIGNUP_DB_SECRET``; when
that secret is unset nothing is stored and callers get empty results.
"""

from __future__ import annotations

import base64
import hmac
import json
import logging
import os
import sqlite3
import time
from pathlib import Path
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

logger = logging.getLogger(__name__)


def _secret() -> str:
    return os.environ.get("SIGNUP_DB_SECRET", "").strip()


def enabled() -> bool:
    return len(_secret()) >= 32


def db_path() -> str:
    return os.environ.get("SIGNUP_DB_PATH", "signups.sqlite3").strip() or "signups.sqlite3"


def _box() -> Fernet:
    key = hmac.digest(_secret().encode(), b"blocksize-signup-storage-v1", "sha256")
    return Fernet(base64.urlsafe_b64encode(key))


def _connect() -> sqlite3.Connection:
    path = Path(db_path())
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=5)
    conn.execute(
        "CREATE TABLE IF NOT EXISTS signups (grant_hash TEXT PRIMARY KEY, "
        "created_at TEXT NOT NULL, value BLOB NOT NULL)"
    )
    conn.execute("CREATE INDEX IF NOT EXISTS signups_created ON signups(created_at)")
    try:
        os.chmod(str(path), 0o600)
    except OSError:
        pass
    return conn


def record(grant_hash: str | None, user: dict[str, Any], *, created_at: str | None = None) -> bool:
    """Store one signup; returns False when storage is disabled or the key is missing."""
    if not enabled() or not grant_hash:
        return False
    stamp = created_at or time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime())
    body = _box().encrypt(json.dumps(user, sort_keys=True).encode())
    try:
        with _connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO signups (grant_hash, created_at, value) VALUES (?, ?, ?)",
                (grant_hash, stamp, body),
            )
        return True
    except sqlite3.Error as exc:
        logger.warning("signup store write failed: %s", type(exc).__name__)
        return False


def signups_since(since_iso: str) -> list[dict[str, Any]]:
    """Return signups recorded at or after ``since_iso`` (UTC ISO text), newest last."""
    if not enabled():
        return []
    box = _box()
    out: list[dict[str, Any]] = []
    try:
        with _connect() as conn:
            rows = conn.execute(
                "SELECT grant_hash, created_at, value FROM signups WHERE created_at >= ? "
                "ORDER BY created_at ASC",
                (since_iso,),
            ).fetchall()
    except sqlite3.Error as exc:
        logger.warning("signup store read failed: %s", type(exc).__name__)
        return []
    for grant_hash, created_at, value in rows:
        try:
            user = json.loads(box.decrypt(value))
        except (InvalidToken, ValueError):
            continue
        out.append({"grant_hash": grant_hash, "created_at": created_at, **user})
    return out


def lookup(grant_hashes: set[str]) -> dict[str, dict[str, Any]]:
    """Return signup details for the given grant hashes (those the store knows)."""
    if not enabled() or not grant_hashes:
        return {}
    box = _box()
    found: dict[str, dict[str, Any]] = {}
    try:
        with _connect() as conn:
            marks = ",".join("?" for _ in grant_hashes)
            rows = conn.execute(
                f"SELECT grant_hash, created_at, value FROM signups WHERE grant_hash IN ({marks})",
                tuple(grant_hashes),
            ).fetchall()
    except sqlite3.Error as exc:
        logger.warning("signup store read failed: %s", type(exc).__name__)
        return {}
    for grant_hash, created_at, value in rows:
        try:
            found[grant_hash] = {"created_at": created_at, **json.loads(box.decrypt(value))}
        except (InvalidToken, ValueError):
            continue
    return found
