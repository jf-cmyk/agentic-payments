"""Global test isolation for telemetry side effects."""

from __future__ import annotations

from pathlib import Path

import pytest

from src.observability import UsageEventStore, configure_global_store


@pytest.fixture(autouse=True)
def isolate_usage_event_store(tmp_path: Path):
    """Prevent direct MCP tool tests from writing into the workspace database."""
    store = UsageEventStore(tmp_path / "isolated_usage_events.db")
    configure_global_store(store)
    yield store
    configure_global_store(None)
