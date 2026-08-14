from __future__ import annotations

import json
import tomllib
from collections import Counter
from pathlib import Path

from src.observability import UsageEventStore
from src.public_metadata import APP_VERSION, build_server_json
from src.rwa_adapters import build_default_registry
from src.rwa_coverage import build_rwa_coverage_overview
from src.rwa_symbol_registry import build_rwa_venue_registry


ROOT = Path(__file__).resolve().parents[1]


def test_public_versions_and_tracked_registry_metadata_are_synchronized() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    tracked_server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))

    assert project["project"]["version"] == APP_VERSION
    assert tracked_server == build_server_json()


def test_legacy_mcp_config_targets_the_canonical_remote_server_directly() -> None:
    config = json.loads((ROOT / "mcp_config.json").read_text(encoding="utf-8"))

    assert config == {
        "mcpServers": {
            "blocksize-market-data-public": {
                "type": "http",
                "url": "https://mcp.blocksize.info/mcp/server/",
            }
        }
    }


def test_every_adapter_has_a_canonical_venue_registry_entry() -> None:
    adapter_ids = {
        row["venue_id"] for row in build_default_registry().list_metadata()
    }
    venue_ids = {
        row["venue_id"] for row in build_rwa_venue_registry()["venues"]
    }

    assert adapter_ids == venue_ids


def test_coverage_instrument_keys_are_unique_at_the_declared_grain() -> None:
    rows = build_rwa_coverage_overview()["symbols"]

    def instrument_key(row: dict[str, object]) -> tuple[str, str, str]:
        metadata = row.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        identity = (
            metadata.get("venue_market_id")
            or metadata.get("market_id")
            or metadata.get("pool_address")
            or metadata.get("address")
            or row["symbol"]
        )
        instrument_type = (
            metadata.get("market_type")
            or metadata.get("instrument_type")
            or row["asset_class"]
        )
        return str(row["venue"]), str(instrument_type), str(identity)

    counts = Counter(instrument_key(row) for row in rows)
    assert [key for key, count in counts.items() if count > 1] == []


def test_observability_can_exclude_synthetic_events(tmp_path: Path) -> None:
    store = UsageEventStore(tmp_path / "usage.db")
    store.record(
        "http_request",
        endpoint="/synthetic",
        status_code=200,
        user_agent="testclient",
    )
    store.record(
        "http_request",
        endpoint="/smoke",
        status_code=200,
        user_agent="blocksize-hosted-smoke/1.0",
    )
    store.record(
        "http_request",
        endpoint="/production",
        status_code=200,
        ip_hash="private-client-fingerprint",
        user_agent="curl/8",
    )
    store.record(
        "http_request",
        endpoint="/outside-window",
        status_code=200,
        user_agent="curl/8",
    )
    with store._connect() as conn:
        conn.execute(
            "UPDATE usage_events SET timestamp = ? WHERE endpoint = ?",
            ("2000-01-01T00:00:00+00:00", "/outside-window"),
        )

    all_events = store.summarize(days=1)
    production = store.summarize(days=1, include_synthetic=False)

    assert all_events["overview"]["total_events"] == 3
    assert production["overview"]["total_events"] == 1
    assert production["telemetry_scope"] == {
        "include_synthetic": False,
        "matching_events": 3,
        "included_events": 1,
        "excluded_synthetic_events": 2,
        "detected_synthetic_events": 2,
    }
    assert [event["endpoint"] for event in production["recent_events"]] == [
        "/production"
    ]
    assert "ip_hash" not in production["recent_events"][0]


def test_observability_recent_events_are_filtered_bounded_and_newest_first(
    tmp_path: Path,
) -> None:
    store = UsageEventStore(tmp_path / "usage.db")
    for index in range(55):
        store.record(
            "http_request",
            endpoint=f"/production/{index:02d}",
            status_code=200,
            ip_hash=f"private-{index}",
            user_agent="curl/8",
        )
        store.record(
            "http_request",
            endpoint=f"/synthetic/{index:02d}",
            status_code=200,
            user_agent="blocksize-hosted-smoke/1.0",
        )

    production = store.summarize(days=1, include_synthetic=False)
    recent = production["recent_events"]

    assert len(recent) == 50
    assert [event["endpoint"] for event in recent[:3]] == [
        "/production/54",
        "/production/53",
        "/production/52",
    ]
    assert recent[-1]["endpoint"] == "/production/05"
    assert all(not event["endpoint"].startswith("/synthetic/") for event in recent)
    assert all("ip_hash" not in event for event in recent)
