"""Delete data received through the Claude connector within 30 days.

The privacy policy and the Claude plugin directory listing commit to keeping
Claude connector data for under 30 days. This job enforces that with a 29-day
cutoff, run hourly, so nothing outlives the commitment by more than an hour:

- request records (tool calls, instruments, search text, hashed IPs, user
  agents, charges) are deleted 29 days after they are written;
- account records (account ID, email, monthly counters, once-per-account
  markers) are deleted once the account has been idle for 29 days;
- OAuth token files (upstream Clerk tokens, refresh tokens, JTI maps, pending
  transactions and codes) are deleted 29 days after they were last written.
  Client registrations are kept: they describe the Claude app, not a user.

Two things deliberately outlive the cutoff: a suspended free-tier grant keeps
its salted-hash row (and this month's credit total) so the block holds, and an
allowance-override account (subscriber or beta grant) keeps its row with the
email cleared.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import os
from pathlib import Path
from typing import Any

from src.observability import fingerprint

RETENTION_DAYS = 29
# Observability surfaces written by the Claude connector: tool events use the
# client label ("claude_mcp"), the HTTP middleware uses the mount path
# ("anthropic_mcp"). Agent registration and account emails reach the same users.
CLAUDE_SURFACES = ("claude_mcp", "anthropic_mcp", "agent_auth", "email")
# Once-per-account markers kept while the account is active.
ACCOUNT_MILESTONES = ("first_live_price_delivered",)
# fastmcp OAuthProxy collections that hold per-user tokens. The
# "mcp-oauth-proxy-clients" collection (the Claude client registration) is kept.
OAUTH_TOKEN_COLLECTIONS = (
    "mcp-upstream-tokens",
    "mcp-refresh-tokens",
    "mcp-jti-mappings",
    "mcp-oauth-transactions",
    "mcp-authorization-codes",
)


def retention_cutoff(now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)) - timedelta(days=RETENTION_DAYS)


def oauth_storage_dir() -> Path | None:
    raw = os.environ.get("ANTHROPIC_OAUTH_STORAGE_DIR", "").strip()
    return Path(raw).expanduser() if raw else None


def purge_oauth_token_files(storage_dir: Path | None, cutoff: datetime) -> int:
    """Delete token entries not rewritten since ``cutoff``; returns files removed."""
    if storage_dir is None or not storage_dir.is_dir():
        return 0
    # The encrypted FileTreeStore renames collection directories (for example
    # "S_mcp_upstream_tokens-064b3cac"); use its own strategy to find them.
    from key_value.aio.stores.filetree import FileTreeV1CollectionSanitizationStrategy

    sanitize = FileTreeV1CollectionSanitizationStrategy(storage_dir).sanitize
    cutoff_ts = cutoff.timestamp()
    removed = 0
    for collection in OAUTH_TOKEN_COLLECTIONS:
        directory = storage_dir / sanitize(collection)
        if not directory.is_dir():
            continue
        for path in directory.glob("*.json"):
            if path.stem == "info" or not path.is_file() or path.is_symlink():
                continue
            if path.stat().st_mtime < cutoff_ts:
                path.unlink(missing_ok=True)
                removed += 1
    return removed


def purge_claude_data(
    *,
    entitlements: Any,
    ledger: Any,
    observability: Any | None,
    agent_auth_store: Any | None,
    oauth_dir: Path | None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Run one retention pass over every store that holds Claude connector data."""
    cutoff = retention_cutoff(now)
    usage_date = (now or datetime.now(UTC)).astimezone(UTC).date().isoformat()
    report: dict[str, object] = {"cutoff": cutoff.isoformat()}

    entitlement_report = dict(entitlements.purge_expired_data(cutoff, usage_date=usage_date))
    deleted_subjects = entitlement_report.pop("deleted_subjects", [])
    report["entitlements"] = entitlement_report
    report["free_tier"] = ledger.purge_expired_data(cutoff, usage_date=usage_date)
    if observability is not None:
        identity_hashes = tuple(
            value
            for subject in deleted_subjects
            if (value := fingerprint(f"user:{subject}")) is not None
        )
        report["observability"] = observability.purge_expired(
            cutoff,
            surfaces=CLAUDE_SURFACES,
            keep_milestone_events=ACCOUNT_MILESTONES,
            milestone_identity_hashes=identity_hashes,
        )
    if agent_auth_store is not None:
        report["agent_auth_records"] = agent_auth_store.purge_expired()
    report["oauth_token_files"] = purge_oauth_token_files(oauth_dir, cutoff)
    return report
