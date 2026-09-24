"""Global test isolation for telemetry side effects."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# A clean checkout intentionally has no developer `.env`. Keep the production
# setting required while giving tests a non-secret, process-local credential.
os.environ.setdefault("BLOCKSIZE_API_KEY", "test-only-blocksize-api-key")

from src.config import settings  # noqa: E402
from src import free_tier_ledger  # noqa: E402
from src.observability import UsageEventStore, configure_global_store  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_free_tier_ledger(tmp_path: Path, monkeypatch):
    """Keep the shared free-tier grant ledger out of the workspace database."""
    monkeypatch.setattr(
        settings.free_tier,
        "ledger_db_path",
        str(tmp_path / "isolated_free_tier_ledger.db"),
    )
    free_tier_ledger.reset_free_tier_ledger()
    yield
    free_tier_ledger.reset_free_tier_ledger()


@pytest.fixture(autouse=True)
def isolate_usage_event_store(tmp_path: Path):
    """Prevent direct MCP tool tests from writing into the workspace database."""
    store = UsageEventStore(tmp_path / "isolated_usage_events.db")
    configure_global_store(store)
    yield store
    configure_global_store(None)
