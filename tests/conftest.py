"""Global test isolation for telemetry side effects."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# A clean checkout intentionally has no developer `.env`. Keep the production
# setting required while giving tests a non-secret, process-local credential.
os.environ.setdefault("BLOCKSIZE_API_KEY", "test-only-blocksize-api-key")

from src.observability import UsageEventStore, configure_global_store  # noqa: E402


@pytest.fixture(autouse=True)
def isolate_usage_event_store(tmp_path: Path):
    """Prevent direct MCP tool tests from writing into the workspace database."""
    store = UsageEventStore(tmp_path / "isolated_usage_events.db")
    configure_global_store(store)
    yield store
    configure_global_store(None)
