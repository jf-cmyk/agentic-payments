"""Release packaging, readiness, and provenance safeguards."""

from __future__ import annotations

import json
import hashlib
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import threading
import tomllib
import zipfile
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from packaging.requirements import Requirement
from packaging.version import Version

from src import public_metadata, resource_server, runtime_data
from src.config import settings
from src.security_config import PRIVACY_SALT_SETTINGS
from scripts import (
    check_secret_hygiene,
    verify_installed_release,
    verify_release_artifact,
)


ROOT = Path(__file__).resolve().parents[1]


def _canonical_json(value: object) -> str:
    if isinstance(value, dict):
        return "{" + ",".join(
            f"{json.dumps(key, separators=(',', ':'))}:{_canonical_json(value[key])}"
            for key in sorted(value)
        ) + "}"
    if isinstance(value, list):
        return "[" + ",".join(_canonical_json(item) for item in value) + "]"
    return json.dumps(value, separators=(",", ":"))


def _legacy_bridge_fixture(now_epoch_ms: int) -> tuple[dict[str, object], dict[str, object]]:
    from datetime import UTC, datetime, timedelta

    now = datetime.fromtimestamp(now_epoch_ms / 1000, tz=UTC)

    def stamp(delta: timedelta) -> str:
        return (now + delta).isoformat().replace("+00:00", "Z")
    payment_counts = {
        "total": 0,
        "pending": 0,
        "settled": 0,
        "settlement_unknown": 0,
        "released": 0,
        "finalized": 0,
        "unknown": 0,
        "finalized_cached_responses": 0,
        "recent_finalized_cached_responses": 0,
    }
    usage = {
        connector: {"row_count": 0, "credits_spent_total": 0}
        for connector in ("anthropic", "cursor", "openai")
    }
    fingerprints = {
        "creditDb": "1" * 64,
        "connectors": {
            "anthropic": "2" * 64,
            "cursor": "3" * 64,
            "openai": "4" * 64,
        },
    }
    def sample(delta: timedelta) -> dict[str, object]:
        return {
            "sampledAt": stamp(delta),
            "databaseFingerprints": fingerprints,
            "connector_daily_usage": usage,
            "payment_proofs": payment_counts,
        }
    prior_id = "33333333-3333-4333-8333-333333333333"
    attestation = {
        "schemaVersion": 1,
        "kind": "blocksize_legacy_transaction_drain_v1",
        "attestedBy": "release-operator@example.com",
        "candidateCommit": "a" * 40,
        "target": {
            "project": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "environment": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
            "service": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
            "domains": {"custom": [], "service": ["production.example"]},
        },
        "prior": {
            "deploymentId": prior_id,
            "version": "0.6.2",
            "imageDigest": "sha256:prior",
            "snapshotId": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            "compatibilityFixtureCommit": "1791c5c9c46163cdcc1c9b69613f2855bee4d7a1",
            "liveSchemaBehaviorAuditSha256": "5" * 64,
        },
        "freeze": {
            "ingressFrozen": True,
            "economicWritesFrozen": True,
            "startedAt": stamp(timedelta(minutes=-3)),
            "drainWaitCompletedAt": stamp(timedelta(minutes=-2)),
            "minimumDrainSeconds": 60,
            "expiresAt": stamp(timedelta(hours=2)),
            "enforcement": {
                "mechanism": "all_domain_ingress_block",
                "changeReference": "edge-change-123",
                "zeroInFlightObservedAt": stamp(timedelta(seconds=-110)),
            },
            "stableLedgerSamples": [
                sample(timedelta(seconds=-100)),
                sample(timedelta(seconds=-90)),
            ],
        },
        "directCounts": {
            "connector_pending_charges": 0,
            "connector_pending_charges_by_connector": {
                "anthropic": 0,
                "cursor": 0,
                "openai": 0,
            },
            "payment_proofs": payment_counts,
        },
    }
    source = (_canonical_json(attestation) + "\n").encode()
    bridge = {
        "required": True,
        "phase": "legacy_lock",
        "sourceSha256": hashlib.sha256(source).hexdigest(),
        "attestation": attestation,
    }
    prior = {
        "id": prior_id,
        "imageDigest": "sha256:prior",
        "snapshotId": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
        "health": {"version": "0.6.2"},
        "readiness": {"status": 404},
    }
    return bridge, prior

SECURITY_VERSION_FLOORS = {
    "cryptography": Version("50.0.0"),
    "idna": Version("3.15"),
    "joserfc": Version("1.6.8"),
    "mcp": Version("1.28.1"),
    "pydantic-settings": Version("2.14.2"),
    "pyjwt": Version("2.13.0"),
    "python-multipart": Version("0.0.31"),
    "starlette": Version("1.3.1"),
    "urllib3": Version("2.7.0"),
}


def test_runtime_metadata_and_lock_enforce_dependency_security_floors() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    requirements = {
        requirement.name: requirement
        for raw_requirement in project["project"]["dependencies"]
        if (requirement := Requirement(raw_requirement))
    }
    locked = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked_versions = {
        package["name"]: Version(package["version"])
        for package in locked["package"]
        if "version" in package
    }

    for package_name, safe_floor in SECURITY_VERSION_FLOORS.items():
        requirement = requirements[package_name]
        lower_bounds = [
            Version(specifier.version)
            for specifier in requirement.specifier
            if specifier.operator in {">=", ">", "==", "==="}
        ]
        assert lower_bounds and max(lower_bounds) >= safe_floor
        assert locked_versions[package_name] >= safe_floor


def test_secret_hygiene_guard_detects_keys_without_returning_secret_values() -> None:
    secret_value = "sk-proj-" + ("A" * 32)

    matches = check_secret_hygiene.find_secret_patterns(f"OPENAI_API_KEY={secret_value}")

    assert matches == ["openai_api_key"]
    assert secret_value not in repr(matches)


def test_tracked_candidate_files_pass_secret_hygiene_gate() -> None:
    assert check_secret_hygiene.scan_tracked_files(ROOT) == []


def test_release_version_and_registry_description_are_coherent() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    tracked_server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    smithery = json.loads((ROOT / "docs" / "smithery_manifest.json").read_text(encoding="utf-8"))

    assert project["project"]["version"] == public_metadata.APP_VERSION
    assert tracked_server == public_metadata.build_server_json()
    assert tracked_server["version"] == public_metadata.APP_VERSION
    assert smithery["version"] == public_metadata.APP_VERSION
    assert 1 <= len(tracked_server["description"]) <= 100


def test_resource_server_import_is_independent_of_working_directory(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment.pop("RWA_REPORTS_DIR", None)
    environment["PYTHONPATH"] = str(ROOT)
    environment["OBSERVABILITY_ENABLED"] = "false"
    environment["BLOCKSIZE_API_KEY"] = "release-import-test"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from src.resource_server import DOCS_DIR, RWA_REPORTS_DIR; "
                "from src.runtime_data import REQUIRED_RWA_REPORT_FILENAMES; "
                "assert (DOCS_DIR / 'developer_portal.html').is_file(); "
                "assert all((RWA_REPORTS_DIR / name).is_file() "
                "for name in REQUIRED_RWA_REPORT_FILENAMES); "
                "print(DOCS_DIR, RWA_REPORTS_DIR)"
            ),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert str(ROOT / "docs") in result.stdout
    assert str(ROOT / "reports") in result.stdout


def test_rwa_reports_override_is_authoritative_without_source_fallback(
    tmp_path: Path,
) -> None:
    override = tmp_path / "missing-reports"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT)
    environment["RWA_REPORTS_DIR"] = str(override)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from src.runtime_data import RWA_REPORTS_DIR, "
                "resolve_rwa_report_path; "
                f"assert str(RWA_REPORTS_DIR) == {str(override)!r}; "
                "assert resolve_rwa_report_path('rwa_xyz_new_asset_monitor.json') "
                "== RWA_REPORTS_DIR / 'rwa_xyz_new_asset_monitor.json'; "
                "assert not RWA_REPORTS_DIR.exists()"
            ),
        ],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_readiness_passes_with_required_runtime_and_static_files() -> None:
    with TestClient(resource_server.app) as client:
        response = client.get("/readyz")
        health = client.get("/health")

    assert response.status_code == 200
    payload = response.json()
    assert payload["status"] == "ready"
    assert payload["ready"] is True
    assert all(check["ready"] for check in payload["checks"].values())
    assert payload["checks"]["privacy_security"]["ready"] is True
    assert health.json()["version"] == public_metadata.APP_VERSION


def _contains_exact_key(value: object, keys: set[str]) -> bool:
    if isinstance(value, dict):
        return any(
            key in keys or _contains_exact_key(item, keys)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_exact_key(item, keys) for item in value)
    return False


def test_public_readiness_redacts_filesystem_paths_without_mutating_report() -> None:
    canary = "/private/runtime/secret-state.db"
    report = {
        "ready": False,
        "checks": {
            "credit_ledger": {"path": canary, "ready": False},
            "rwa_operator_store": {"database_path": canary, "ready": False},
            "connectors": {
                "providers": {
                    "openai": {
                        "oauth_storage": {"path": canary, "ready": False},
                        "public_url": {
                            "url": "https://staging.example/openai/mcp",
                            "expected_origin": "https://staging.example",
                            "expected_path": "/openai/mcp",
                        },
                    }
                }
            },
            "static_product": {"missing": ["assets/favicon.ico"]},
        },
    }

    public = resource_server._public_readiness_report(report)

    assert report["checks"]["credit_ledger"]["path"] == canary
    assert report["checks"]["rwa_operator_store"]["database_path"] == canary
    assert not _contains_exact_key(public, {"path", "database_path"})
    assert canary not in json.dumps(public)
    assert public["checks"]["connectors"]["providers"]["openai"]["public_url"] == {
        "url": "https://staging.example/openai/mcp",
        "expected_origin": "https://staging.example",
        "expected_path": "/openai/mcp",
    }
    assert public["checks"]["static_product"]["missing"] == ["assets/favicon.ico"]


@pytest.mark.asyncio
@pytest.mark.parametrize(("ready", "expected_status"), [(True, 200), (False, 503)])
async def test_readiness_route_uses_internal_status_while_redacting_paths(
    monkeypatch: pytest.MonkeyPatch,
    ready: bool,
    expected_status: int,
) -> None:
    monkeypatch.setattr(
        resource_server,
        "_readiness_report",
        lambda: {
            "status": "ready" if ready else "not_ready",
            "ready": ready,
            "checks": {
                "credit_ledger": {
                    "ready": ready,
                    "path": "/data/private-credit-ledger.db",
                }
            },
        },
    )

    response = await resource_server.readiness_check()
    payload = json.loads(response.body)

    assert response.status_code == expected_status
    assert payload["ready"] is ready
    assert payload["checks"]["credit_ledger"] == {"ready": ready}


def test_repeated_readiness_requests_only_use_cached_store_probes(monkeypatch) -> None:
    def forbidden_probe(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("readiness request performed a database probe")

    with TestClient(resource_server.app) as client:
        # Startup is the controlled point at which the bounded probes run.
        monkeypatch.setattr(
            resource_server,
            "_sqlite_database_readiness",
            forbidden_probe,
        )
        monkeypatch.setattr(
            resource_server.RWAObservationStore,
            "schema_status",
            forbidden_probe,
        )

        first = client.get("/readyz")
        second = client.get("/readyz")

    assert first.status_code == 200
    assert second.status_code == 200


def test_repeated_readiness_reuses_unchanged_rwa_integrity_results(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime_data._cached_rwa_report_integrity.cache_clear()
    original_validator = runtime_data.validate_rwa_report_payload
    validations: list[str] = []

    def tracking_validator(filename: str, payload: object) -> tuple[str, ...]:
        validations.append(filename)
        return original_validator(filename, payload)

    monkeypatch.setattr(
        runtime_data,
        "validate_rwa_report_payload",
        tracking_validator,
    )

    first = resource_server._rwa_runtime_reports_readiness()
    second = resource_server._rwa_runtime_reports_readiness()

    assert first["ready"] is True
    assert second == first
    assert validations == list(runtime_data.REQUIRED_RWA_REPORT_FILENAMES)


def test_readiness_fails_closed_when_store_probe_is_stale() -> None:
    with TestClient(resource_server.app) as client:
        snapshot = resource_server.app.state.store_readiness_snapshots["credit_ledger"]
        snapshot["checked_at"] -= resource_server._store_readiness_max_age_seconds() + 1

        response = client.get("/readyz")

    assert response.status_code == 503
    credit = response.json()["checks"]["credit_ledger"]
    assert credit["ready"] is False
    assert credit["reason"] == "probe_stale"
    assert "readiness_probe_stale" in credit["blockers"]


def test_readiness_fails_closed_when_store_configuration_changes(monkeypatch) -> None:
    with TestClient(resource_server.app) as client:
        manager = resource_server.app.state.credits
        monkeypatch.setattr(manager, "db_path", f"{manager.db_path}.changed")

        response = client.get("/readyz")

    assert response.status_code == 503
    credit = response.json()["checks"]["credit_ledger"]
    assert credit["ready"] is False
    assert credit["reason"] == "configuration_changed_since_probe"
    assert "configuration_changed_since_probe" in credit["blockers"]


def test_readiness_rejects_incomplete_production_privacy_configuration(
    monkeypatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("OBSERVABILITY_DASHBOARD_TOKEN", raising=False)
    weak_dashboard_value = "weak-dashboard-value"
    monkeypatch.setattr(
        settings.server,
        "observability_dashboard_token",
        weak_dashboard_value,
    )
    for environment_name, setting_name in PRIVACY_SALT_SETTINGS.items():
        monkeypatch.delenv(environment_name, raising=False)
        monkeypatch.setattr(settings.server, setting_name, "")

    with TestClient(resource_server.app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    security = response.json()["checks"]["privacy_security"]
    assert security["ready"] is False
    assert security["production"] is True
    assert security["production_requirements_met"] is False
    assert weak_dashboard_value not in response.text


def test_readiness_fails_closed_when_static_product_is_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(resource_server, "DOCS_DIR", tmp_path)
    with TestClient(resource_server.app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    payload = response.json()
    assert payload["status"] == "not_ready"
    assert payload["checks"]["static_product"]["ready"] is False
    assert "developer_portal.html" in payload["checks"]["static_product"]["missing"]


def test_readiness_fails_closed_when_packaged_rwa_reports_are_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(resource_server, "RWA_REPORTS_DIR", tmp_path)

    with TestClient(resource_server.app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    check = response.json()["checks"]["rwa_runtime_reports"]
    assert check == {
        "ready": False,
        "required_report_count": len(runtime_data.REQUIRED_RWA_REPORT_FILENAMES),
        "checked_report_count": len(runtime_data.REQUIRED_RWA_REPORT_FILENAMES),
        "failures": {
            filename: ["missing"] for filename in runtime_data.REQUIRED_RWA_REPORT_FILENAMES
        },
    }


def test_readiness_fails_closed_for_missing_file_specific_rwa_override(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    missing_override = tmp_path / "operator-private-token" / "missing.json"
    monkeypatch.setenv("RWA_JUPITER_ROUTE_ALLOWLIST_PATH", str(missing_override))

    with TestClient(resource_server.app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    check = response.json()["checks"]["rwa_runtime_reports"]
    assert check["failures"] == {"rwa_jupiter_route_allowlist.json": ["missing"]}
    assert str(missing_override) not in response.text


def test_readiness_fails_closed_for_invalid_rwa_report_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invalid_report = tmp_path / "invalid.json"
    invalid_report.write_text("{not-json\n", encoding="utf-8")
    effective_paths = runtime_data.effective_rwa_report_paths()
    effective_paths["rwa_evm_pool_allowlist.json"] = invalid_report
    monkeypatch.setattr(
        resource_server,
        "effective_rwa_report_paths",
        lambda **_kwargs: effective_paths,
    )

    with TestClient(resource_server.app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["checks"]["rwa_runtime_reports"]["failures"] == {
        "rwa_evm_pool_allowlist.json": ["invalid_json"]
    }


def test_readiness_fails_closed_for_structurally_empty_rwa_report(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    collapsed_report = tmp_path / "collapsed.json"
    collapsed_report.write_text(
        json.dumps(
            {
                "product": "rwa_evm_pool_allowlist",
                "summary": {"pool_count": 0},
                "pools": [],
            }
        ),
        encoding="utf-8",
    )
    effective_paths = runtime_data.effective_rwa_report_paths()
    effective_paths["rwa_evm_pool_allowlist.json"] = collapsed_report
    monkeypatch.setattr(
        resource_server,
        "effective_rwa_report_paths",
        lambda **_kwargs: effective_paths,
    )

    with TestClient(resource_server.app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["checks"]["rwa_runtime_reports"]["failures"] == {
        "rwa_evm_pool_allowlist.json": ["structurally_empty"]
    }


def test_readiness_fails_closed_for_wrong_rwa_report_root_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    wrong_root = tmp_path / "wrong-root.json"
    wrong_root.write_text("[]\n", encoding="utf-8")
    effective_paths = runtime_data.effective_rwa_report_paths()
    effective_paths["rwa_solana_pool_allowlist.json"] = wrong_root
    monkeypatch.setattr(
        resource_server,
        "effective_rwa_report_paths",
        lambda **_kwargs: effective_paths,
    )

    with TestClient(resource_server.app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["checks"]["rwa_runtime_reports"]["failures"] == {
        "rwa_solana_pool_allowlist.json": ["root_not_object"]
    }


@pytest.mark.parametrize(
    ("filename", "rows_path"),
    [
        ("hyperliquid_tradeable_feeds.json", "coverage_rows"),
        ("rwa_blocksize_state_discovery.json", "symbols"),
        ("rwa_daily_feed_agent.json", "new_assets"),
        ("rwa_derivative_venue_discovery.json", "market_rows"),
        ("rwa_evm_pool_allowlist.json", "pools"),
        ("rwa_hyperliquid_paxg_probe.json", "result.results"),
        ("rwa_jupiter_route_allowlist.json", "routes"),
        ("rwa_rights_clearance.json", "scope.allowed_uses"),
        ("rwa_solana_pool_allowlist.json", "pools"),
        ("rwa_solana_token_mints.json", "tokens"),
        ("rwa_xyz_new_asset_monitor.json", "asset_rows"),
    ],
)
def test_required_rwa_report_contract_rejects_unusable_rows(
    filename: str,
    rows_path: str,
) -> None:
    payload = json.loads((ROOT / "reports" / filename).read_text(encoding="utf-8"))
    rows: object = payload
    for part in rows_path.split("."):
        assert isinstance(rows, dict)
        rows = rows[part]
    assert isinstance(rows, list)

    original_rows = list(rows)
    rows[:] = [None]
    assert "row_invalid" in runtime_data.validate_rwa_report_payload(
        filename,
        payload,
    )

    rows[:] = [{}]
    assert "row_invalid" in runtime_data.validate_rwa_report_payload(
        filename,
        payload,
    )
    rows[:] = original_rows


@pytest.mark.parametrize(
    ("filename", "rows_path", "field_path", "invalid_values"),
    [
        (
            "rwa_evm_pool_allowlist.json",
            "pools",
            "pool_id",
            (0, False, "", "   ", [], {}),
        ),
        (
            "rwa_solana_token_mints.json",
            "tokens",
            "mint",
            (0, False, "", "   ", [], {}),
        ),
        (
            "hyperliquid_tradeable_feeds.json",
            "coverage_rows",
            "asset_id",
            (0, False, "", "   ", [], {}),
        ),
        (
            "rwa_hyperliquid_paxg_probe.json",
            "result.results",
            "job.job_id",
            (0, False, "", "   ", [], {}),
        ),
        (
            "rwa_solana_token_mints.json",
            "tokens",
            "decimals",
            (False, -1, 1.5, "8", float("nan"), float("inf"), 256),
        ),
        (
            "rwa_evm_pool_allowlist.json",
            "pools",
            "block_number",
            (0, False, -1, 1.5, "1", float("nan"), float("inf")),
        ),
        (
            "rwa_solana_pool_allowlist.json",
            "pools",
            "slot",
            (0, False, -1, 1.5, "1", float("nan"), float("inf")),
        ),
        (
            "rwa_hyperliquid_paxg_probe.json",
            "result.results",
            "block_vwap.block_size_usd",
            (0, False, -1, "10000", float("nan"), float("inf")),
        ),
        (
            "rwa_hyperliquid_paxg_probe.json",
            "result.quality.observations",
            "age_ms",
            (False, -1, "0", float("nan"), float("inf")),
        ),
        (
            "rwa_hyperliquid_paxg_probe.json",
            "result.quality.observations",
            "usable_for_realtime",
            (0, 1, "false", None, [], {}),
        ),
    ],
)
def test_required_rwa_typed_row_contracts_reject_invalid_field_values(
    filename: str,
    rows_path: str,
    field_path: str,
    invalid_values: tuple[object, ...],
) -> None:
    payload = json.loads((ROOT / "reports" / filename).read_text(encoding="utf-8"))
    rows: object = payload
    for part in rows_path.split("."):
        assert isinstance(rows, dict)
        rows = rows[part]
    assert isinstance(rows, list) and isinstance(rows[0], dict)
    row = rows[0]
    target = row
    field_parts = field_path.split(".")
    for part in field_parts[:-1]:
        assert isinstance(target.get(part), dict)
        target = target[part]
    field_name = field_parts[-1]
    original_value = target[field_name]

    for invalid_value in invalid_values:
        target[field_name] = invalid_value
        assert "row_invalid" in runtime_data.validate_rwa_report_payload(
            filename,
            payload,
        )
    target[field_name] = original_value


@pytest.mark.parametrize(
    ("filename", "rows_path", "field_path"),
    [
        ("rwa_solana_token_mints.json", "tokens", "decimals"),
        (
            "rwa_hyperliquid_paxg_probe.json",
            "result.quality.observations",
            "age_ms",
        ),
    ],
)
def test_required_rwa_numeric_row_contracts_allow_legitimate_zero(
    filename: str,
    rows_path: str,
    field_path: str,
) -> None:
    payload = json.loads((ROOT / "reports" / filename).read_text(encoding="utf-8"))
    rows: object = payload
    for part in rows_path.split("."):
        assert isinstance(rows, dict)
        rows = rows[part]
    assert isinstance(rows, list) and isinstance(rows[0], dict)
    row = rows[0]
    target = row
    field_parts = field_path.split(".")
    for part in field_parts[:-1]:
        assert isinstance(target.get(part), dict)
        target = target[part]
    target[field_parts[-1]] = 0

    assert "row_invalid" not in runtime_data.validate_rwa_report_payload(
        filename,
        payload,
    )


def test_required_rwa_boolean_row_contract_allows_false() -> None:
    filename = "rwa_hyperliquid_paxg_probe.json"
    payload = json.loads((ROOT / "reports" / filename).read_text(encoding="utf-8"))
    payload["result"]["quality"]["observations"][0]["usable_for_realtime"] = False

    assert "row_invalid" not in runtime_data.validate_rwa_report_payload(
        filename,
        payload,
    )


def _synchronize_solana_token_status_summary(payload: dict[str, object]) -> None:
    tokens = payload["tokens"]
    assert isinstance(tokens, list)
    statuses = Counter(
        str(row["status"]).strip().lower() for row in tokens if isinstance(row, dict)
    )
    summary = payload["summary"]
    assert isinstance(summary, dict)
    summary["by_status"] = dict(sorted(statuses.items()))
    summary["resolved"] = statuses.get("resolved", 0)


def test_solana_token_contract_rejects_unsupported_nonempty_status() -> None:
    filename = "rwa_solana_token_mints.json"
    payload = json.loads((ROOT / "reports" / filename).read_text(encoding="utf-8"))
    payload["tokens"][0]["status"] = "unresolved"
    _synchronize_solana_token_status_summary(payload)

    errors = runtime_data.validate_rwa_report_payload(filename, payload)

    assert "row_invalid" in errors
    assert "count_mismatch" not in errors


@pytest.mark.parametrize(
    "status",
    ["resolved", "verified", "configured", " VERIFIED "],
)
def test_solana_token_contract_allows_every_loader_status(status: str) -> None:
    filename = "rwa_solana_token_mints.json"
    payload = json.loads((ROOT / "reports" / filename).read_text(encoding="utf-8"))
    payload["tokens"][0]["status"] = status
    _synchronize_solana_token_status_summary(payload)

    assert runtime_data.validate_rwa_report_payload(filename, payload) == ()


def test_solana_token_resolved_summary_matches_exact_status_count() -> None:
    filename = "rwa_solana_token_mints.json"
    payload = json.loads((ROOT / "reports" / filename).read_text(encoding="utf-8"))
    payload["summary"]["resolved"] -= 1

    assert runtime_data.validate_rwa_report_payload(filename, payload) == ("count_mismatch",)


def test_solana_token_by_status_summary_matches_exact_histogram() -> None:
    filename = "rwa_solana_token_mints.json"
    payload = json.loads((ROOT / "reports" / filename).read_text(encoding="utf-8"))
    payload["summary"]["by_status"] = {"resolved": 14, "verified": 1}

    assert runtime_data.validate_rwa_report_payload(filename, payload) == ("count_mismatch",)


def test_readiness_reconciles_daily_report_with_effective_xyz_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    daily_path = tmp_path / "rwa_daily_feed_agent.json"
    xyz_path = tmp_path / "rwa_xyz_new_asset_monitor.json"
    daily_path.write_bytes((ROOT / "reports" / daily_path.name).read_bytes())
    xyz_payload = json.loads((ROOT / "reports" / xyz_path.name).read_text(encoding="utf-8"))
    xyz_payload["source"]["next_build_id"] = "individually-valid-different-snapshot"
    xyz_path.write_text(json.dumps(xyz_payload), encoding="utf-8")

    assert (
        runtime_data.inspect_required_rwa_report(
            "rwa_daily_feed_agent.json",
            daily_path,
        )
        == ()
    )
    assert (
        runtime_data.inspect_required_rwa_report(
            "rwa_xyz_new_asset_monitor.json",
            xyz_path,
        )
        == ()
    )

    effective_paths = runtime_data.effective_rwa_report_paths()
    effective_paths["rwa_daily_feed_agent.json"] = daily_path
    effective_paths["rwa_xyz_new_asset_monitor.json"] = xyz_path
    monkeypatch.setattr(resource_server, "RWA_REPORTS_DIR", tmp_path)
    monkeypatch.setattr(
        resource_server,
        "effective_rwa_report_paths",
        lambda **_kwargs: effective_paths,
    )

    with TestClient(resource_server.app) as client:
        response = client.get("/readyz")

    assert response.status_code == 503
    assert response.json()["checks"]["rwa_runtime_reports"]["failures"] == {
        "rwa_daily_feed_agent.json": ["daily_snapshot_mismatch"]
    }


def test_daily_report_must_target_the_effective_xyz_report(
    tmp_path: Path,
) -> None:
    daily_path = tmp_path / "rwa_daily_feed_agent.json"
    xyz_path = tmp_path / "rwa_xyz_new_asset_monitor.json"
    daily_payload = json.loads((ROOT / "reports" / daily_path.name).read_text(encoding="utf-8"))
    daily_payload["source"]["current_report"] = "individually-valid-other.json"
    daily_path.write_text(json.dumps(daily_payload), encoding="utf-8")
    xyz_path.write_bytes((ROOT / "reports" / xyz_path.name).read_bytes())

    assert (
        runtime_data.inspect_required_rwa_report(
            "rwa_daily_feed_agent.json",
            daily_path,
        )
        == ()
    )
    assert runtime_data.inspect_daily_xyz_reconciliation(
        daily_path,
        xyz_path,
        reports_dir=tmp_path,
    ) == ("daily_source_target_mismatch",)


def test_missing_source_manifest_cannot_fall_back_to_unrelated_data(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        resource_server,
        "SERVER_JSON_PATHS",
        (tmp_path / "missing-server.json",),
    )

    status = resource_server._release_manifest_check()

    assert status == {"ready": False, "reason": "server.json is not packaged"}


def test_credit_database_readiness_checks_integrity_schema_and_exact_path(
    tmp_path: Path,
) -> None:
    valid_path = tmp_path / "credits.db"
    resource_server.CreditManager(str(valid_path))

    valid = resource_server._sqlite_database_readiness(
        valid_path,
        expected_schema=resource_server._CREDIT_DB_SCHEMA,
        hosted=False,
        configured_path=valid_path,
    )
    mismatched = resource_server._sqlite_database_readiness(
        valid_path,
        expected_schema=resource_server._CREDIT_DB_SCHEMA,
        hosted=False,
        configured_path=tmp_path / "other.db",
    )
    empty_path = tmp_path / "empty.db"
    empty_path.touch()
    empty = resource_server._sqlite_database_readiness(
        empty_path,
        expected_schema=resource_server._CREDIT_DB_SCHEMA,
        hosted=False,
        configured_path=empty_path,
    )

    assert valid["ready"] is True
    assert valid["integrity"] == "ok"
    assert valid["integrity_scope"] == "sqlite_schema"
    assert mismatched["ready"] is False
    assert "runtime_path_mismatch" in mismatched["blockers"]
    assert empty["ready"] is False
    assert "schema_tables_missing" in empty["blockers"]


def test_cached_upstream_probe_fails_closed_after_configuration_change(
    monkeypatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    client = resource_server.BlocksizeClient(
        api_key="first-key",
        base_url="https://data.example.test/marketdata/v1",
    )
    resource_server.app.state.blocksize_dependency = {
        "checked": True,
        "available": True,
        "checked_at": resource_server.time.time(),
        "reason": None,
        "configuration_fingerprint": (resource_server._blocksize_configuration_fingerprint(client)),
    }
    client._api_key = "changed-key"

    status = resource_server._blocksize_dependency_readiness(client)

    assert status["ready"] is False
    assert status["reason"] == "configuration_changed_since_probe"


def test_facilitator_capability_snapshot_fails_closed_when_stale_or_reconfigured(
    monkeypatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    snapshot = {
        "checked": True,
        "available": True,
        "required": True,
        "reason": None,
        "kinds": [
            {
                "x402Version": 2,
                "scheme": "exact",
                "network": settings.x402.base_network,
                "extra": {},
            }
        ],
        "checked_at": resource_server.time.time(),
        "configuration_fingerprint": (resource_server._facilitator_configuration_fingerprint()),
    }

    current = resource_server._facilitator_support_readiness(snapshot)
    stale_snapshot = {
        **snapshot,
        "checked_at": (
            resource_server.time.time() - resource_server._facilitator_probe_max_age_seconds() - 1
        ),
    }
    stale = resource_server._facilitator_support_readiness(stale_snapshot)
    stale_requirements = resource_server._facilitator_supported_requirements(
        settings.payment_requirements(resource_server.Decimal("0.002")),
        stale_snapshot,
    )

    monkeypatch.setattr(settings.x402, "base_network", "eip155:84532")
    changed = resource_server._facilitator_support_readiness(snapshot)

    assert current["ready"] is True
    assert stale["ready"] is False
    assert stale["reason"] == "probe_stale"
    assert stale_requirements == []
    assert changed["ready"] is False
    assert changed["reason"] == "configuration_changed_since_probe"


def test_production_oauth_storage_requires_supported_encrypted_durable_state(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("ANTHROPIC_AUTH_PROVIDER", "clerk")
    monkeypatch.delenv("ANTHROPIC_OAUTH_JWT_SIGNING_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_OAUTH_STORAGE_ENCRYPTION_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_OAUTH_STORAGE_DIR", raising=False)
    missing = resource_server._oauth_storage_readiness(
        "ANTHROPIC",
        hosted=False,
        production=True,
    )

    oauth_dir = tmp_path / "oauth"
    oauth_dir.mkdir()
    monkeypatch.setenv(
        "ANTHROPIC_OAUTH_JWT_SIGNING_KEY",
        "jwt-key-0123456789abcdefghijklmnopqrstuvwxyz-ABCDEF",
    )
    monkeypatch.setenv(
        "ANTHROPIC_OAUTH_STORAGE_ENCRYPTION_KEY",
        "storage-key-9876543210ABCDEFGHIJKLMNOPQRSTUVWXYZ-abcdef",
    )
    monkeypatch.setenv("ANTHROPIC_OAUTH_STORAGE_DIR", str(oauth_dir))
    configured = resource_server._oauth_storage_readiness(
        "ANTHROPIC",
        hosted=False,
        production=True,
    )

    monkeypatch.setenv("ANTHROPIC_AUTH_PROVIDER", "supabase")
    unsupported = resource_server._oauth_storage_readiness(
        "ANTHROPIC",
        hosted=False,
        production=True,
    )

    assert missing["ready"] is False
    assert {
        "jwt_signing_key_missing_or_weak",
        "storage_encryption_key_missing_or_weak",
        "storage_directory_missing",
    }.issubset(missing["blockers"])
    assert configured["ready"] is True
    assert configured["backend"] == "encrypted_filetree"
    assert configured["absolute"] is True
    assert unsupported["ready"] is False
    assert "provider_missing_local_oauth_routes" in unsupported["blockers"]


def test_hosted_connector_public_url_must_match_candidate_origin(
    monkeypatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setattr(
        resource_server,
        "PUBLIC_BASE_URL",
        "https://staging-mcp.example.test",
    )
    connector = type("Connector", (), {"auth": object()})()
    entitlement = {"ready": True}

    monkeypatch.setenv(
        "ANTHROPIC_MCP_PUBLIC_URL",
        "https://mcp.blocksize.info/anthropic/mcp",
    )
    mismatched = resource_server._connector_readiness(
        "ANTHROPIC",
        connector,
        entitlement,
    )
    monkeypatch.setenv(
        "ANTHROPIC_MCP_PUBLIC_URL",
        "https://staging-mcp.example.test/anthropic/mcp",
    )
    matched = resource_server._connector_readiness(
        "ANTHROPIC",
        connector,
        entitlement,
    )

    assert mismatched["public_url"]["ready"] is False
    assert mismatched["public_url"]["reason"] == ("connector_public_url_must_match_PUBLIC_BASE_URL")
    assert matched["public_url"]["ready"] is True


def test_state_store_paths_must_be_pairwise_unique(tmp_path: Path) -> None:
    shared = tmp_path / "shared.db"

    status = resource_server._state_store_isolation(
        {
            "credit_ledger": shared,
            "observability_store": shared,
            "rwa_store": tmp_path / "rwa.db",
        }
    )

    assert status["ready"] is False
    assert ["credit_ledger", "observability_store"] in status["collisions"]


def test_state_store_isolation_rejects_ancestor_and_descendant_paths(
    tmp_path: Path,
) -> None:
    oauth_root = tmp_path / "oauth"

    status = resource_server._state_store_isolation(
        {
            "anthropic_oauth_storage": oauth_root,
            "credit_ledger": oauth_root / "credits.db",
            "cursor_oauth_storage": oauth_root / "cursor",
            "rwa_store": tmp_path / "rwa.db",
        }
    )

    assert status["ready"] is False
    assert ["anthropic_oauth_storage", "credit_ledger"] in status["collisions"]
    assert ["anthropic_oauth_storage", "cursor_oauth_storage"] in status["collisions"]


def test_full_hosted_surface_requires_every_configured_stream_ticker_fresh(
    monkeypatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("ANTHROPIC_ONLY_MODE", "false")
    cache = resource_server.BlocksizeStreamCache(
        api_key="stream-test",
        enabled=True,
        fixed_vwap_tickers=["BTCUSD", "ETHUSD"],
    )
    monkeypatch.setattr(
        cache,
        "status",
        lambda: {
            "enabled": True,
            "ready": True,
            "fixed_vwap_tickers": 2,
            "cached_24h_vwap": 2,
            "fresh_configured_24h_vwap": 1,
        },
    )

    stale = resource_server._stream_cache_readiness(cache)

    monkeypatch.setattr(
        cache,
        "status",
        lambda: {
            "enabled": True,
            "ready": True,
            "fixed_vwap_tickers": 2,
            "cached_24h_vwap": 2,
            "fresh_configured_24h_vwap": 2,
        },
    )
    fresh = resource_server._stream_cache_readiness(cache)

    assert stale["ready"] is False
    assert "configured_fixed_vwap_cache_not_fully_seeded" in stale["blockers"]
    assert fresh["ready"] is True


def test_configured_clerk_connectors_advertise_only_mounted_oauth_routes(
    tmp_path: Path,
) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    environment = os.environ.copy()
    for name in list(environment):
        if name.startswith(("ANTHROPIC_", "CURSOR_", "OPENAI_", "RAILWAY_")):
            environment.pop(name, None)
    environment.update(
        {
            "PYTHONPATH": str(ROOT),
            "BLOCKSIZE_API_KEY": "oauth-route-test",
            "BLOCKSIZE_STREAM_CACHE_ENABLED": "false",
            "OBSERVABILITY_ENABLED": "false",
            "CREDIT_DB_PATH": str(state_dir / "credits.db"),
            "RWA_OBSERVATION_DB_PATH": str(state_dir / "rwa.db"),
            "CLERK_DOMAIN": "https://clerk.example.test",
            "CLERK_CLIENT_ID": "oauth-route-client",
            "CLERK_CLIENT_SECRET": "s" * 64,
        }
    )
    for connector in ("ANTHROPIC", "CURSOR", "OPENAI"):
        slug = connector.lower()
        environment.update(
            {
                f"{connector}_AUTH_PROVIDER": "clerk",
                f"{connector}_MCP_PUBLIC_URL": (f"https://mcp.blocksize.info/{slug}/mcp"),
                f"{connector}_OAUTH_JWT_SIGNING_KEY": (
                    "jwt-key-0123456789abcdefghijklmnopqrstuvwxyz-ABCDEF"
                ),
                f"{connector}_OAUTH_STORAGE_ENCRYPTION_KEY": (
                    "storage-key-9876543210ABCDEFGHIJKLMNOPQRSTUVWXYZ-abcdef"
                ),
                f"{connector}_OAUTH_STORAGE_DIR": str(state_dir / f"{slug}-oauth"),
                f"{connector}_ENTITLEMENT_DB_PATH": str(state_dir / f"{slug}-entitlements.db"),
            }
        )
    probe = textwrap.dedent(
        """
        from urllib.parse import urlsplit
        from fastapi.testclient import TestClient
        from src.resource_server import app

        def path_for(url):
            parsed = urlsplit(url)
            return parsed.path + (("?" + parsed.query) if parsed.query else "")

        with TestClient(app, base_url="https://mcp.blocksize.info") as client:
            health = client.get("/health").json()
            for connector in ("anthropic", "cursor", "openai"):
                protected = client.get(
                    f"/.well-known/oauth-protected-resource/{connector}/mcp/"
                ).json()
                authorization = client.get(
                    f"/.well-known/oauth-authorization-server/{connector}/mcp"
                ).json()
                assert protected["oauth_available"] is True
                assert authorization["oauth_available"] is True
                checks = (
                    (authorization["authorization_endpoint"], "GET", None),
                    (authorization["token_endpoint"], "POST", {"grant_type": "authorization_code"}),
                    (authorization["registration_endpoint"], "POST", {}),
                    (health["links"][f"{connector}_oauth_callback"], "GET", None),
                )
                for url, method, body in checks:
                    if method == "GET":
                        response = client.get(path_for(url), follow_redirects=False)
                    else:
                        response = client.post(path_for(url), json=body, follow_redirects=False)
                    assert 300 <= response.status_code < 500, (connector, url, response.status_code)
                    assert response.status_code not in {404, 405}
        """
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=tmp_path,
        env=environment,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr


def test_railway_promotes_only_dependency_ready_releases() -> None:
    railway = (ROOT / ".railway" / "railway.ts").read_text(encoding="utf-8")
    requirements = (ROOT / "requirements.txt").read_text(encoding="utf-8")

    assert 'builder: "RAILPACK"' in railway
    assert 'railpackVersion: "0.38.0"' in railway
    assert "--no-emit-project" in requirements
    assert "-e ." not in requirements
    assert "--editable ." not in requirements
    assert 'healthcheck: "/readyz"' in railway
    assert "healthcheckTimeout: 180" in railway
    assert 'restartPolicyType: "ON_FAILURE"' in railway
    assert 'branch: "main"' in railway
    assert "commitSha:" not in railway


def test_railpack_build_log_audit_requires_the_exact_pinned_version() -> None:
    script = ROOT / "scripts" / "audit_railpack_build_log.mjs"
    accepted_log = "\n".join(
        [
            "using build driver railpack-v0.38.0",
            "[railway] prepare railpack-v0.38.0",
            "\x1b[95m│ Railpack 0.38.0 │\x1b[0m",
            "resolve image config for docker-image://ghcr.io/railwayapp/railpack-frontend:v0.38.0",
        ]
    )
    accepted = subprocess.run(
        ["node", str(script)],
        cwd=ROOT,
        input=accepted_log,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    drifted = subprocess.run(
        ["node", str(script)],
        cwd=ROOT,
        input=accepted_log.replace("0.38.0", "0.38.1"),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert accepted.returncode == 0, accepted.stdout + accepted.stderr
    assert "pinned Railpack 0.38.0" in accepted.stdout
    assert drifted.returncode != 0
    assert "marker observed 0.38.1" in drifted.stderr


def test_legacy_bridge_validator_binds_digest_domains_samples_and_fresh_readiness(
    tmp_path: Path,
) -> None:
    now_epoch_ms = int(__import__("time").time() * 1000)
    bridge, prior = _legacy_bridge_fixture(now_epoch_ms)
    target = bridge["attestation"]["target"]  # type: ignore[index]
    readiness_counts = {
        **bridge["attestation"]["directCounts"],  # type: ignore[index]
        "connector_daily_usage": bridge["attestation"]["freeze"][  # type: ignore[index]
            "stableLedgerSamples"
        ][1]["connector_daily_usage"],
        "database_fingerprints": bridge["attestation"]["freeze"][  # type: ignore[index]
            "stableLedgerSamples"
        ][1]["databaseFingerprints"],
    }
    readiness = {
        "status": 200,
        "ready": True,
        "version": "0.6.5",
        "commitSha": "a" * 40,
        "legacyTransactionBridge": {
            "ready": True,
            "checked": True,
            "configuration_valid": True,
            "economic_writes_locked": True,
            "mode": "locked",
            "reason": None,
            "blockers": [],
            "direct_counts": readiness_counts,
        },
    }
    payload_path = tmp_path / "payload.json"
    payload_path.write_text(
        json.dumps(
            {
                "bridge": bridge,
                "prior": prior,
                "target": {
                    "project": target["project"],  # type: ignore[index]
                    "environment": target["environment"],  # type: ignore[index]
                    "service": target["service"],  # type: ignore[index]
                },
                "domains": target["domains"],  # type: ignore[index]
                "readiness": readiness,
                "now": now_epoch_ms,
            }
        ),
        encoding="utf-8",
    )
    probe = textwrap.dedent(
        f"""
        import {{ readFile }} from "node:fs/promises";
        import {{
          expectedLegacyBridgePhase,
          validateLegacyBridgeReadiness,
          validateLegacyBridgeState,
        }} from {json.dumps((ROOT / "scripts/railway_release_control.mjs").as_uri())};
        const value = JSON.parse(await readFile({json.dumps(str(payload_path))}, "utf8"));
        const context = {{
          target: value.target,
          targetDomains: value.domains,
          prior: value.prior,
          expectedCommit: "{'a' * 40}",
          expectedDigest: value.bridge.sourceSha256,
          now: value.now,
          requireUnexpired: true,
        }};
        validateLegacyBridgeState(value.bridge, context);
        validateLegacyBridgeReadiness(value.readiness, value.bridge);
        const rejects = async (call) => {{
          try {{ await call(); return false; }} catch {{ return true; }}
        }};
        const tampered = structuredClone(value.bridge);
        tampered.attestation.directCounts.payment_proofs.released = 1;
        const wrongDomains = structuredClone(value.domains);
        wrongDomains.service.push("other.example");
        const stale = structuredClone(value.readiness);
        stale.legacyTransactionBridge.reason = "probe_stale";
        stale.legacyTransactionBridge.blockers = ["readiness_probe_stale"];
        const wrongCommitPrior = structuredClone(value.prior);
        wrongCommitPrior.health = {{ version: "0.6.5", commitSha: "{'b' * 40}" }};
        wrongCommitPrior.readiness = {{
          ...value.readiness,
          legacyTransactionBridge: value.readiness.legacyTransactionBridge,
        }};
        const unlock = structuredClone(value.bridge);
        unlock.phase = "bridge_unlock";
        console.log(JSON.stringify({{
          phase: expectedLegacyBridgePhase(value.prior),
          unlockedBypass: expectedLegacyBridgePhase({{
            health: {{ version: "0.6.5" }},
            readiness: {{ legacyTransactionBridge: {{
              configuration_valid: true,
              economic_writes_locked: false,
            }} }},
          }}),
          tamperedRejected: await rejects(() => validateLegacyBridgeState(tampered, context)),
          domainsRejected: await rejects(() => validateLegacyBridgeState(value.bridge, {{
            ...context, targetDomains: wrongDomains,
          }})),
          staleRejected: await rejects(() => validateLegacyBridgeReadiness(stale, value.bridge)),
          wrongUnlockRejected: await rejects(() => validateLegacyBridgeState(unlock, {{
            ...context, prior: wrongCommitPrior,
          }})),
        }}));
        """
    )
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", probe],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    outcome = json.loads(result.stdout)
    assert outcome == {
        "phase": "legacy_lock",
        "unlockedBypass": None,
        "tamperedRejected": True,
        "domainsRejected": True,
        "staleRejected": True,
        "wrongUnlockRejected": True,
    }


def test_first_legacy_cutover_is_hard_blocked_before_deploy_mutation() -> None:
    probe = textwrap.dedent(
        f"""
        import {{
          requireExecutableLegacyBridgePhase,
        }} from {json.dumps((ROOT / "scripts/railway_release_control.mjs").as_uri())};
        const rejects = (phase) => {{
          try {{
            requireExecutableLegacyBridgePhase(phase);
            return null;
          }} catch (error) {{
            return String(error.message || error);
          }}
        }};
        console.log(JSON.stringify({{
          legacy: rejects("legacy_lock"),
          unlock: rejects("bridge_unlock"),
          bypass: requireExecutableLegacyBridgePhase(null),
          malformed: rejects("unexpected"),
        }}));
        """
    )
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", probe],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    outcome = json.loads(result.stdout)
    assert "operationally blocked before mutation" in outcome["legacy"]
    assert "operationally blocked before mutation" in outcome["unlock"]
    assert outcome["bypass"] is None
    assert "unsupported legacy transaction bridge phase" in outcome["malformed"]

    deploy_source = (ROOT / "scripts" / "deploy_railway_exact.mjs").read_text(
        encoding="utf-8"
    )
    gate_offset = deploy_source.index("requireExecutableLegacyBridgePhase(phase)")
    mutation_offset = deploy_source.index("state.bridgeVariable.changeArmed = true")
    upload_offset = deploy_source.index("const upload = await runRailway(")
    assert gate_offset < mutation_offset < upload_offset


def test_railway_bridge_variable_mutation_uses_stdin_skip_deploys_and_readback(
    tmp_path: Path,
) -> None:
    fake_railway = tmp_path / "railway"
    fake_railway.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json
            import os
            from pathlib import Path
            import sys

            args = sys.argv[1:]
            root = Path(os.environ["FAKE_RAILWAY_STATE"])
            root.mkdir(exist_ok=True)
            if args[:2] == ["variable", "set"]:
                value = sys.stdin.read()
                (root / "value").write_text(value.strip(), encoding="utf-8")
                (root / "set-call.json").write_text(
                    json.dumps({{"args": args, "stdin": value}}), encoding="utf-8"
                )
                raise SystemExit(0)
            if args[:2] == ["variable", "list"]:
                value = (root / "value").read_text(encoding="utf-8")
                print(json.dumps({{"LEGACY_TRANSACTION_BRIDGE_LOCK": value}}))
                raise SystemExit(0)
            raise SystemExit("unexpected Railway command")
            """
        ),
        encoding="utf-8",
    )
    fake_railway.chmod(0o755)
    state_dir = tmp_path / "state"
    probe = textwrap.dedent(
        f"""
        import {{ setExactServiceVariable }} from {
            json.dumps((ROOT / "scripts/railway_release_control.mjs").as_uri())
        };
        await setExactServiceVariable({{
          project: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
          environment: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
          service: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        }}, "LEGACY_TRANSACTION_BRIDGE_LOCK", "true");
        """
    )
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", probe],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_RAILWAY_STATE": str(state_dir),
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    call = json.loads((state_dir / "set-call.json").read_text(encoding="utf-8"))
    assert call["stdin"] == "true\n"
    assert "true" not in call["args"]
    assert call["args"][:3] == [
        "variable",
        "set",
        "LEGACY_TRANSACTION_BRIDGE_LOCK",
    ]
    assert "--stdin" in call["args"]
    assert "--skip-deploys" in call["args"]


def test_transaction_bridge_projection_is_migration_invariant_and_fail_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite3

    from src.credit_manager import CreditManager
    from src.entitlement_manager import EntitlementManager
    from src.transaction_bridge import transaction_bridge_readiness

    credit_db = tmp_path / "credits.db"
    connector_paths = {
        connector: tmp_path / f"{connector}.db"
        for connector in ("anthropic", "cursor", "openai")
    }
    with sqlite3.connect(credit_db) as conn:
        conn.executescript(
            """
            CREATE TABLE wallets (address TEXT PRIMARY KEY, balance_credits REAL, last_updated TIMESTAMP);
            CREATE TABLE credit_purchases (tx_hash TEXT PRIMARY KEY, address TEXT, amount_usdc REAL, credits_added REAL, timestamp TIMESTAMP);
            CREATE TABLE trial_history (ip_hash TEXT PRIMARY KEY, address TEXT, funding_address TEXT, timestamp TIMESTAMP, subject_hash TEXT, subject_type TEXT DEFAULT 'wallet', device_hash TEXT, session_hash TEXT, user_agent_hash TEXT);
            CREATE TABLE payment_proofs (tx_hash TEXT PRIMARY KEY, network TEXT NOT NULL, amount_atomic INTEGER DEFAULT 0, recipient TEXT DEFAULT '', purpose TEXT DEFAULT '', timestamp TIMESTAMP);
            CREATE TABLE price_receipts (receipt_id TEXT PRIMARY KEY, product TEXT NOT NULL, subject TEXT DEFAULT '', payload_json TEXT NOT NULL, created_at TIMESTAMP);
            """
        )
    legacy_fixture = ROOT / "tests" / "fixtures" / "entitlement_manager_v062.py"
    import importlib.util

    spec = importlib.util.spec_from_file_location("_release_bridge_v062", legacy_fixture)
    assert spec and spec.loader
    legacy_module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = legacy_module
    spec.loader.exec_module(legacy_module)
    for connector, path in connector_paths.items():
        legacy = legacy_module.EntitlementManager(path, default_daily_credits=5)
        legacy.status(f"{connector}-user", usage_date="2026-08-13")

    # Capture the reviewed v0.6.2 business projections, then run real candidate
    # constructors that add lifecycle/identity columns and tables.
    from src.transaction_bridge import (
        _CONNECTOR_LEGACY_PROJECTIONS,
        _CREDIT_LEGACY_PROJECTIONS,
        _legacy_business_fingerprint,
        _read_only_connection,
    )

    with _read_only_connection(credit_db) as conn:
        legacy_credit_fingerprint = _legacy_business_fingerprint(
            conn, _CREDIT_LEGACY_PROJECTIONS
        )
    legacy_connector_fingerprints = {}
    for connector, path in connector_paths.items():
        with _read_only_connection(path) as conn:
            legacy_connector_fingerprints[connector] = _legacy_business_fingerprint(
                conn, _CONNECTOR_LEGACY_PROJECTIONS
            )
    CreditManager(str(credit_db))
    for path in connector_paths.values():
        EntitlementManager(path, default_daily_credits=5)

    monkeypatch.setenv("LEGACY_TRANSACTION_BRIDGE_LOCK", "true")
    before = transaction_bridge_readiness(credit_db, connector_paths)
    assert before["ready"] is True
    assert before["direct_counts"]["connector_pending_charges"] == 0
    assert before["direct_counts"]["payment_proofs"]["pending"] == 0
    assert before["direct_counts"]["database_fingerprints"] == {
        "creditDb": legacy_credit_fingerprint,
        "connectors": legacy_connector_fingerprints,
    }

    with sqlite3.connect(credit_db) as conn:
        conn.execute("ALTER TABLE wallets ADD COLUMN additive_candidate_column TEXT")
    with sqlite3.connect(connector_paths["anthropic"]) as conn:
        conn.execute("ALTER TABLE users ADD COLUMN additive_candidate_column TEXT")
    after_additive_migration = transaction_bridge_readiness(credit_db, connector_paths)
    assert (
        after_additive_migration["direct_counts"]["database_fingerprints"]
        == before["direct_counts"]["database_fingerprints"]
    )

    with sqlite3.connect(connector_paths["anthropic"]) as conn:
        conn.execute(
            "UPDATE daily_usage SET credits_spent = credits_spent + 1 "
            "WHERE user_id = ?",
            ("anthropic-user",),
        )
    changed = transaction_bridge_readiness(credit_db, connector_paths)
    assert (
        changed["direct_counts"]["database_fingerprints"]["connectors"]["anthropic"]
        != before["direct_counts"]["database_fingerprints"]["connectors"]["anthropic"]
    )

    with sqlite3.connect(credit_db) as conn:
        conn.execute(
            """
            INSERT INTO payment_proofs (
              tx_hash, network, state, request_binding, reservation_id, attempt_id
            ) VALUES ('pending-proof', 'eip155:8453', 'pending', 'binding', 'r', 'a')
            """
        )
    blocked = transaction_bridge_readiness(credit_db, connector_paths)
    assert blocked["ready"] is False
    assert "payment_proof_transient_states_not_drained" in blocked["blockers"]

    monkeypatch.setenv("LEGACY_TRANSACTION_BRIDGE_LOCK", "malformed")
    invalid = transaction_bridge_readiness(credit_db, connector_paths)
    assert invalid["economic_writes_locked"] is True
    assert invalid["configuration_valid"] is False
    assert invalid["ready"] is False


def test_locked_http_bridge_preserves_discovery_and_blocks_new_economic_writes(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("LEGACY_TRANSACTION_BRIDGE_LOCK", "true")
    monkeypatch.setenv("CREDIT_DB_PATH", str(tmp_path / "bridge-http.db"))
    monkeypatch.setattr(resource_server, "_facilitator_support_required", lambda: False)
    monkeypatch.setattr(settings.server, "unverified_http_credits_enabled", True)
    monkeypatch.setattr(settings.server, "x402_allow_mock_payments", True)
    upstream = AsyncMock()
    upstream.get_vwap_latest = AsyncMock()
    with TestClient(resource_server.app, base_url="https://testserver") as client:
        runtime_upstream = resource_server.app.state.blocksize
        runtime_facilitator_support = resource_server.app.state.facilitator_support
        resource_server.app.state.blocksize = upstream
        resource_server.app.state.facilitator_support = {
            "checked": True,
            "available": True,
            "required": False,
            "kinds": [
                {
                    "x402Version": 2,
                    "scheme": "exact",
                    "network": requirement["network"],
                    "extra": {},
                }
                for requirement in settings.payment_requirements(
                    settings.pricing.core_crypto
                )
            ],
        }
        manager = resource_server.app.state.credits
        try:
            discovery = client.get("/v1/vwap/btc-usd")
            signed = client.get(
                "/v1/vwap/btc-usd",
                headers={
                    "PAYMENT-SIGNATURE": __import__("base64").b64encode(
                        json.dumps(
                            {"proof": "new-bridge-proof", "network": settings.x402.solana_network}
                        ).encode()
                    ).decode()
                },
            )
            claim = client.post(
                "/v1/credits/claim",
                json={"proof": "proof", "tier": "starter", "wallet": "w" * 32},
            )
        finally:
            resource_server.app.state.blocksize = runtime_upstream
            resource_server.app.state.facilitator_support = runtime_facilitator_support

    # Some test security profiles have no operational payment rail; unsigned
    # discovery must remain non-economic and never reach the upstream either way.
    assert discovery.status_code in {402, 404, 503}
    if discovery.status_code == 402:
        assert signed.status_code == 503
        assert signed.json()["error_code"] == "ECONOMIC_WRITES_LOCKED"
        assert signed.headers["cache-control"] == "no-store"
    else:
        # Catalog/readiness preflight happens before proof handling. An
        # unsupported or unconfirmable instrument remains a free 404/503
        # even when a caller attaches a proof-shaped header.
        assert signed.status_code == discovery.status_code
    assert claim.status_code == 503
    assert claim.headers["cache-control"] == "no-store"
    assert manager.payment_proof_state("new-bridge-proof") is None
    upstream.get_vwap_latest.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("module_name", "tool_prefix"),
    [
        ("src.anthropic_mcp_server", "anthropic"),
        ("src.cursor_mcp_server", "cursor"),
        ("src.openai_mcp_server", "openai"),
    ],
)
async def test_locked_connector_tools_return_before_any_ledger_or_upstream_access(
    monkeypatch: pytest.MonkeyPatch,
    module_name: str,
    tool_prefix: str,
) -> None:
    import importlib

    module = importlib.import_module(module_name)

    class ForbiddenAccess:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"bridge lock touched forbidden dependency attribute {name}")

    monkeypatch.setenv("LEGACY_TRANSACTION_BRIDGE_LOCK", "true")
    monkeypatch.setattr(module, "_client", ForbiddenAccess())
    monkeypatch.setattr(module, "_entitlements", ForbiddenAccess())

    paid = await getattr(module, f"{tool_prefix}_get_vwap")("BTCUSD")
    balance = await getattr(module, f"{tool_prefix}_get_credit_balance")()

    assert json.loads(paid)["error_code"] == "ECONOMIC_WRITES_LOCKED"
    assert json.loads(balance)["error_code"] == "ECONOMIC_WRITES_LOCKED"


def test_transaction_bridge_direct_inspection_has_a_hard_progress_deadline(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import sqlite3
    import time

    from src.credit_manager import CreditManager
    from src.entitlement_manager import EntitlementManager
    from src import transaction_bridge

    credit_db = tmp_path / "deadline-credits.db"
    connector_paths = {
        connector: tmp_path / f"deadline-{connector}.db"
        for connector in ("anthropic", "cursor", "openai")
    }
    CreditManager(str(credit_db))
    for connector, path in connector_paths.items():
        EntitlementManager(path, default_daily_credits=5)
        with sqlite3.connect(path) as conn:
            conn.executemany(
                "INSERT INTO users VALUES (?, NULL, 5, 'active', 'now', 'now')",
                [(f"{connector}-{index}",) for index in range(2_000)],
            )
            conn.executemany(
                "INSERT INTO daily_usage VALUES (?, '2026-08-13', 0, 'now')",
                [(f"{connector}-{index}",) for index in range(2_000)],
            )
    monkeypatch.setenv("LEGACY_TRANSACTION_BRIDGE_LOCK", "true")
    monkeypatch.setattr(transaction_bridge, "_INSPECTION_TIMEOUT_SECONDS", 0.000001)

    started = time.monotonic()
    result = transaction_bridge.transaction_bridge_readiness(
        credit_db,
        connector_paths,
    )

    assert time.monotonic() - started < 1
    assert result["ready"] is False
    assert result["direct_counts"] is None
    assert result["blockers"] == ["economic_ledger_direct_inspection_failed"]


def _run_exact_railway_helper(
    tmp_path: Path,
    mode: str,
    *,
    forbidden_environment: str = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
) -> tuple[subprocess.CompletedProcess[str], list[list[str]]]:
    fake_railway = tmp_path / "railway"
    fake_railway.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import datetime
            import json
            import os
            from pathlib import Path
            import sys

            args = sys.argv[1:]
            state_dir = Path(os.environ["FAKE_RAILWAY_STATE"])
            state_dir.mkdir(parents=True, exist_ok=True)
            with (state_dir / "calls.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(args) + "\\n")
            deployment_id = "11111111-1111-4111-8111-111111111111"
            unrelated_id = "22222222-2222-4222-8222-222222222222"
            project_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            environment_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
            service_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
            mode = os.environ["FAKE_RAILWAY_MODE"]
            if args == ["--version"]:
                print("railway 5.30.1")
                raise SystemExit(0)
            if args[:2] == ["status", "--json"]:
                requested_environment = args[args.index("--environment") + 1]
                requested_project = args[args.index("--project") + 1]
                is_production = requested_environment == "production-environment"
                resolved_environment_id = (
                    "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
                    if is_production
                    else environment_id
                )
                resolved_service_id = (
                    "ffffffff-ffff-4fff-8fff-ffffffffffff"
                    if is_production
                    else service_id
                )
                print(json.dumps({{
                    "id": project_id if requested_project == "project-id" else requested_project,
                    "environments": {{"edges": [{{"node": {{
                        "id": resolved_environment_id,
                        "name": requested_environment,
                        "serviceInstances": {{"edges": [{{"node": {{
                            "serviceId": resolved_service_id,
                            "serviceName": "production-service" if is_production else "service-id",
                            "environmentId": resolved_environment_id,
                        }}}}]}},
                    }}}}]}},
                }}))
                raise SystemExit(0)
            if args[0] == "up":
                message = args[args.index("--message") + 1]
                (state_dir / "message").write_text(message, encoding="utf-8")
                if mode == "fallback":
                    print("simulated lost upload response", file=sys.stderr)
                    raise SystemExit(1)
                print(json.dumps({{"deploymentId": deployment_id, "logsUrl": "https://example.invalid"}}))
                raise SystemExit(0)
            if args[:2] == ["deployment", "list"]:
                now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
                message_file = state_dir / "message"
                if not message_file.exists():
                    print(json.dumps([{{
                        "id": unrelated_id,
                        "status": "SUCCESS",
                        "createdAt": now,
                        "meta": {{"cliMessage": "old-deployment"}},
                    }}]))
                    raise SystemExit(0)
                message = message_file.read_text(encoding="utf-8")
                count_file = state_dir / "count"
                count = int(count_file.read_text() if count_file.exists() else "0")
                count_file.write_text(str(count + 1), encoding="utf-8")
                if (state_dir / "stopped").exists():
                    status = "REMOVED"
                elif mode == "success":
                    status = ["INITIALIZING", "BUILDING", "DEPLOYING", "SUCCESS"][min(count, 3)]
                elif mode == "fallback":
                    status = "SUCCESS"
                elif mode == "mismatch":
                    status = "BUILDING"
                    message = "different-ci-message"
                else:
                    status = "UNKNOWN_NEW_STATE"
                rows = [
                    {{
                        "id": unrelated_id,
                        "status": "SUCCESS",
                        "createdAt": now,
                        "meta": {{"cliMessage": "unrelated-newer-deployment"}},
                    }},
                    {{
                        "id": deployment_id,
                        "status": status,
                        "createdAt": now,
                        "meta": {{"cliMessage": message}},
                    }},
                ]
                print(json.dumps(rows))
                raise SystemExit(0)
            if args[0] == "api":
                query = args[1]
                if "ReleaseMutationContract" in query:
                    fields = [
                        {{
                            "name": name,
                            "args": [] if mode == "contract_drift" else [{{
                                "name": "id",
                                "type": {{
                                    "kind": "NON_NULL",
                                    "name": None,
                                    "ofType": {{"kind": "SCALAR", "name": "String"}},
                                }},
                            }}],
                            "type": {{
                                "kind": "NON_NULL",
                                "name": None,
                                "ofType": {{"kind": "SCALAR", "name": "Boolean"}},
                            }},
                        }}
                        for name in ("deploymentCancel", "deploymentStop", "deploymentRollback")
                    ]
                    print(json.dumps({{"data": {{"__type": {{"fields": fields}}}}}}))
                    raise SystemExit(0)
                if "ReleaseDeployAuthority" in query:
                    print(json.dumps({{"data": {{"service": {{
                        "id": service_id,
                        "projectId": project_id,
                        "repoTriggers": {{
                            "edges": [],
                            "pageInfo": {{"hasNextPage": False}},
                        }},
                    }}}}}}))
                    raise SystemExit(0)
                if "ActiveDeployments" in query:
                    print(json.dumps({{"data": {{"serviceInstance": {{"activeDeployments": []}}}}}}))
                    raise SystemExit(0)
                if "TargetDomains" in query:
                    print(json.dumps({{"data": {{"serviceInstance": {{
                        "environmentId": environment_id,
                        "serviceId": service_id,
                        "domains": {{
                            "customDomains": [],
                            "serviceDomains": [{{
                                "domain": "staging.example",
                                "environmentId": environment_id,
                                "serviceId": service_id,
                                "syncStatus": "ACTIVE",
                            }}],
                        }},
                        "tcpProxies": [],
                    }}}}}}))
                    raise SystemExit(0)
                if "ExactDeployment" in query:
                    if f"id={{deployment_id}}" not in args:
                        raise SystemExit("exact query did not target the candidate")
                    stopped = (state_dir / "stopped").exists()
                    message = (state_dir / "message").read_text(encoding="utf-8")
                    count_file = state_dir / "count"
                    count = int(count_file.read_text() if count_file.exists() else "1") - 1
                    if stopped:
                        status = "REMOVED"
                    elif mode == "success":
                        status = ["INITIALIZING", "BUILDING", "DEPLOYING", "SUCCESS"][min(count, 3)]
                    elif mode == "fallback":
                        status = "SUCCESS"
                    elif mode == "mismatch":
                        status = "BUILDING"
                        message = "different-ci-message"
                    else:
                        status = "UNKNOWN_NEW_STATE"
                    print(json.dumps({{"data": {{"deployment": {{
                        "id": deployment_id,
                        "projectId": project_id,
                        "environmentId": environment_id,
                        "serviceId": service_id,
                        "snapshotId": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                        "status": status,
                        "deploymentStopped": stopped,
                        "canRollback": True,
                        "createdAt": datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z"),
                        "meta": {{
                            "cliMessage": message,
                            "imageDigest": "sha256:candidate",
                            "fileServiceManifest": {{"deploy": {{
                                "healthcheckPath": "/readyz",
                                "healthcheckTimeout": 180,
                                "restartPolicyType": "ON_FAILURE",
                                "restartPolicyMaxRetries": 3,
                            }}}},
                            "volumeMounts": ["/data"],
                        }},
                        "instances": [] if stopped or status != "SUCCESS" else [{{"id": "instance", "status": "RUNNING"}}],
                    }}}}}}))
                    raise SystemExit(0)
                raw_id = args[args.index("--raw-var") + 1] if "--raw-var" in args else ""
                if raw_id != f"id={{deployment_id}}":
                    raise SystemExit("cleanup did not target the exact deployment id")
                if "deploymentStop" in query and mode in {{"mismatch", "unknown"}}:
                    print(json.dumps({{"data": {{"deploymentStop": False}}}}))
                    raise SystemExit(0)
                (state_dir / "stopped").write_text("true", encoding="utf-8")
                mutation = "deploymentCancel" if "deploymentCancel" in query else "deploymentStop"
                print(json.dumps({{"data": {{mutation: True}}}}))
                raise SystemExit(0)
            raise SystemExit("unexpected fake Railway command")
            """
        ),
        encoding="utf-8",
    )
    fake_railway.chmod(0o755)
    state_dir = tmp_path / "state"
    environment = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}",
        "FAKE_RAILWAY_MODE": mode,
        "FAKE_RAILWAY_STATE": str(state_dir),
    }
    result = subprocess.run(
        [
            "node",
            str(ROOT / "scripts" / "deploy_railway_exact.mjs"),
            "--project",
            "project-id",
            "--environment",
            "environment-id",
            "--service",
            "service-id",
            "--message",
            "bsmcp:staging:123:456:" + ("a" * 40),
            "--mode",
            "staging",
            "--base-url",
            "https://staging.example",
            "--forbidden-project",
            "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
            "--forbidden-environment",
            forbidden_environment,
            "--forbidden-service",
            "ffffffff-ffff-4fff-8fff-ffffffffffff",
            "--forbidden-base-url",
            "https://production.example",
            "--state-file",
            str(tmp_path / "release-state.json"),
            "--timeout-seconds",
            "10",
            "--poll-ms",
            "1",
            "--discovery-grace-seconds",
            "2",
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    calls = [
        json.loads(line)
        for line in (state_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    return result, calls


@pytest.mark.parametrize("mode", ["success", "fallback"])
def test_exact_railway_helper_selects_only_its_own_successful_deployment(
    tmp_path: Path,
    mode: str,
) -> None:
    result, calls = _run_exact_railway_helper(tmp_path, mode)

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "11111111-1111-4111-8111-111111111111"
    assert any(call[:3] == ["up", "--detach", "--json"] for call in calls)
    assert all(call.count("--detach") == 1 for call in calls if call[0] == "up")
    assert all("--latest" not in call for call in calls)
    assert not any(
        call[1].startswith("mutation")
        for call in calls
        if call[0] == "api"
    )


@pytest.mark.parametrize("mode", ["mismatch"])
def test_exact_railway_helper_does_not_mutate_a_message_mismatch(
    tmp_path: Path,
    mode: str,
) -> None:
    result, calls = _run_exact_railway_helper(tmp_path, mode)

    assert result.returncode != 0
    assert result.stdout == ""
    assert any(call[0] == "api" for call in calls)
    assert not any(
        call[0] == "api" and call[1].startswith("mutation")
        for call in calls
    )
    assert "different Railway CLI message" in result.stderr


def test_exact_railway_helper_fails_closed_without_mutating_unknown_status(
    tmp_path: Path,
) -> None:
    result, calls = _run_exact_railway_helper(tmp_path, "unknown")

    assert result.returncode != 0
    assert "unknown status" in result.stderr
    assert not any(
        call[0] == "api" and call[1].startswith("mutation")
        for call in calls
    )


def test_exact_railway_helper_rejects_a_canonical_staging_environment_alias(
    tmp_path: Path,
) -> None:
    result, calls = _run_exact_railway_helper(
        tmp_path,
        "success",
        forbidden_environment="bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
    )

    assert result.returncode != 0
    assert "distinct environment" in result.stderr
    assert not any(call[0] == "up" for call in calls)
    assert not any(
        call[0] == "api" and call[1].startswith("mutation")
        for call in calls
    )


def test_exact_railway_helper_rejects_mutation_contract_drift_before_upload(
    tmp_path: Path,
) -> None:
    result, calls = _run_exact_railway_helper(tmp_path, "contract_drift")

    assert result.returncode != 0
    assert "mutation contract drifted" in result.stderr
    assert not any(call[0] == "up" for call in calls)
    assert not any(
        call[0] == "api" and call[1].startswith("mutation")
        for call in calls
    )


@pytest.mark.parametrize(
    ("scenario", "accepted", "expected_error"),
    [
        ("empty", True, None),
        ("trigger", False, "repository auto-deploy trigger"),
        ("paginated", False, "repository-trigger query was truncated"),
        ("malformed", False, "repository-trigger query was not bound"),
    ],
)
def test_repository_deploy_authority_preflight_fails_closed(
    tmp_path: Path,
    scenario: str,
    accepted: bool,
    expected_error: str | None,
) -> None:
    fake_railway = tmp_path / "railway"
    fake_railway.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json
            import os
            import sys

            args = sys.argv[1:]
            scenario = os.environ["FAKE_TRIGGER_SCENARIO"]
            project_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            environment_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
            service_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
            if args[0] != "api" or "ReleaseDeployAuthority" not in args[1]:
                raise SystemExit("unexpected Railway query")
            if f"serviceId={{service_id}}" not in args:
                raise SystemExit("query did not bind the exact service id")
            trigger = {{
                "id": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                "projectId": project_id,
                "environmentId": environment_id,
                "serviceId": service_id,
                "branch": "main",
                "repository": "owner/repository",
                "provider": "github",
            }}
            service = {{
                "id": service_id,
                "projectId": project_id,
                "repoTriggers": {{
                    "edges": [{{"node": trigger}}] if scenario == "trigger" else [],
                    "pageInfo": {{"hasNextPage": scenario == "paginated"}},
                }},
            }}
            if scenario == "malformed":
                service["projectId"] = "eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee"
            print(json.dumps({{"data": {{"service": service}}}}))
            """
        ),
        encoding="utf-8",
    )
    fake_railway.chmod(0o755)
    probe = textwrap.dedent(
        f"""
        import {{ verifyNoRepositoryDeployTriggers }} from "{(ROOT / 'scripts' / 'railway_release_control.mjs').as_uri()}";
        await verifyNoRepositoryDeployTriggers({{
          project: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
          environment: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
          service: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        }});
        console.log("accepted");
        """
    )
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", probe],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_TRIGGER_SCENARIO": scenario,
        },
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    if accepted:
        assert result.returncode == 0, result.stdout + result.stderr
        assert result.stdout.strip() == "accepted"
    else:
        assert result.returncode != 0
        assert expected_error in result.stderr


def test_release_state_temp_files_are_excluded_from_git_and_railway_uploads() -> None:
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")
    railwayignore = (ROOT / ".railwayignore").read_text(encoding="utf-8")

    assert ".railway-release-*.json*" in gitignore
    assert ".railway-release-*.json*" in railwayignore


def test_release_state_writes_are_private_and_running_is_never_stopped(
    tmp_path: Path,
) -> None:
    state_file = tmp_path / "release.json"
    sentinel_file = tmp_path / "sentinel.txt"
    sentinel_file.write_text("do-not-overwrite", encoding="utf-8")
    probe = textwrap.dedent(
        f"""
        import {{ readFile, stat, symlink }} from "node:fs/promises";
        import {{
          atomicWriteJson,
          deploymentIsStopped,
          deploymentOccupiesActiveSet,
        }} from "{(ROOT / 'scripts' / 'railway_release_control.mjs').as_uri()}";
        const path = {json.dumps(str(state_file))};
        const sentinel = {json.dumps(str(sentinel_file))};
        await symlink(sentinel, `${{path}}.${{process.pid}}.tmp`);
        await atomicWriteJson(path, {{ safe: true }});
        const mode = (await stat(path)).mode & 0o777;
        const sentinelBody = await readFile(sentinel, "utf8");
        const contradictory = deploymentIsStopped({{
          status: "FAILED",
          deploymentStopped: true,
          instances: [{{ status: "RUNNING" }}],
        }});
        const terminal = deploymentIsStopped({{
          status: "FAILED",
          deploymentStopped: true,
          instances: [{{ status: "EXITED" }}],
        }});
        const transitioningOccupies = deploymentOccupiesActiveSet({{
          status: "DEPLOYING",
          deploymentStopped: false,
          instances: [],
        }});
        console.log(JSON.stringify({{
          mode,
          sentinelBody,
          contradictory,
          terminal,
          transitioningOccupies,
        }}));
        """
    )

    result = subprocess.run(
        ["node", "--input-type=module", "--eval", probe],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload == {
        "mode": 0o600,
        "sentinelBody": "do-not-overwrite",
        "contradictory": False,
        "terminal": True,
        "transitioningOccupies": True,
    }


def test_restored_health_requires_consecutive_readiness_matches() -> None:
    probe = textwrap.dedent(
        f"""
        import {{ verifyHealthRestored }} from "{(ROOT / 'scripts' / 'railway_release_control.mjs').as_uri()}";
        let readinessCalls = 0;
        globalThis.fetch = async (url) => {{
          const isReadiness = String(url).endsWith("/readyz");
          if (isReadiness) readinessCalls += 1;
          const readinessReady = readinessCalls !== 2;
          return {{
            status: isReadiness && !readinessReady ? 503 : 200,
            async json() {{
              if (isReadiness) {{
                return {{
                  status: readinessReady ? "ready" : "not_ready",
                  ready: readinessReady,
                  version: "0.6.5",
                  commit_sha: "{'a' * 40}",
                }};
              }}
              return {{
                status: "healthy",
                service: "Blocksize Real-Time Market Data MCP",
                version: "0.6.5",
                commit_sha: "{'a' * 40}",
              }};
            }},
          }};
        }};
        const state = {{
          baseUrl: "https://production.example",
          prior: {{
            health: {{
              status: 200,
              applicationStatus: "healthy",
              service: "Blocksize Real-Time Market Data MCP",
              version: "0.6.5",
              commitSha: "{'a' * 40}",
            }},
            readiness: {{
              status: 200,
              ready: true,
              version: "0.6.5",
              commitSha: "{'a' * 40}",
            }},
          }},
        }};
        await verifyHealthRestored(state, 1_000, 1);
        console.log(readinessCalls);
        """
    )

    result = subprocess.run(
        ["node", "--input-type=module", "--eval", probe],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.stdout.strip() == "5"


def test_release_history_cap_and_daily_backup_policy_are_enforced() -> None:
    probe = textwrap.dedent(
        f"""
        import {{
          parseDeploymentList,
          requireReleaseHistoryHeadroom,
          validateProductionBackupEvidence,
        }} from "{(ROOT / 'scripts' / 'railway_release_control.mjs').as_uri()}";
        const target = {{
          project: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
          environment: "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
          service: "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
        }};
        const rows = Array.from({{ length: 1000 }}, (_, index) => ({{
          id: `00000000-0000-4000-8000-${{String(index).padStart(12, "0")}}`,
          status: "SUCCESS",
          createdAt: "2026-08-13T12:00:00Z",
        }}));
        let capRejected = false;
        try {{ parseDeploymentList(JSON.stringify(rows)); }} catch {{ capRejected = true; }}
        const headroomRows = rows.slice(0, 998);
        let headroomRejected = false;
        try {{ requireReleaseHistoryHeadroom(headroomRows); }} catch {{ headroomRejected = true; }}
        const headroomAccepted = requireReleaseHistoryHeadroom(rows.slice(0, 997)).length;
        const evidence = {{
          volumeInstance: {{
            id: "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            environmentId: target.environment,
            serviceId: target.service,
            mountPath: "/data",
            state: "READY",
            isPendingDeletion: false,
          }},
          volumeInstanceBackupList: [{{
            id: "backup-id",
            createdAt: "2026-08-13T11:00:00Z",
            expiresAt: null,
          }}],
          volumeInstanceBackupScheduleList: [{{ kind: "WEEKLY", cron: "0 0 * * 0" }}],
        }};
        let weeklyRejected = false;
        try {{
          validateProductionBackupEvidence(
            evidence,
            evidence.volumeInstance.id,
            target,
            Date.parse("2026-08-13T12:00:00Z"),
          );
        }} catch {{ weeklyRejected = true; }}
        evidence.volumeInstanceBackupScheduleList.push({{ kind: "DAILY", cron: "0 0 * * *" }});
        const accepted = validateProductionBackupEvidence(
          evidence,
          evidence.volumeInstance.id,
          target,
          Date.parse("2026-08-13T12:00:00Z"),
        );
        console.log(JSON.stringify({{
          capRejected,
          headroomRejected,
          headroomAccepted,
          weeklyRejected,
          accepted,
        }}));
        """
    )

    result = subprocess.run(
        ["node", "--input-type=module", "--eval", probe],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["capRejected"] is True
    assert payload["headroomRejected"] is True
    assert payload["headroomAccepted"] == 997
    assert payload["weeklyRejected"] is True
    assert payload["accepted"]["backupId"] == "backup-id"
    assert payload["accepted"]["scheduleKinds"] == ["DAILY", "WEEKLY"]


@pytest.mark.parametrize("candidate_status", ["SUCCESS", "DEPLOYING", "SLEEPING"])
def test_production_recovery_binds_one_new_rollback_and_is_idempotent(
    tmp_path: Path,
    candidate_status: str,
) -> None:
    fake_railway = tmp_path / "railway"
    fake_railway.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import datetime
            import json
            import os
            from pathlib import Path
            import sys

            args = sys.argv[1:]
            state_dir = Path(os.environ["FAKE_RAILWAY_STATE"])
            state_dir.mkdir(parents=True, exist_ok=True)
            with (state_dir / "calls.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(args) + "\\n")
            project_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            environment_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
            service_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
            prior_id = "33333333-3333-4333-8333-333333333333"
            candidate_id = "11111111-1111-4111-8111-111111111111"
            rollback_id = "44444444-4444-4444-8444-444444444444"
            message = "bsmcp:production:123:456:" + ("a" * 40) + ":prev:" + prior_id
            now = datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")
            rolled_back = (state_dir / "rolled-back").exists()
            candidate_status = os.environ["FAKE_CANDIDATE_STATUS"]

            def deployment(
                deployment_id,
                *,
                status="SUCCESS",
                digest,
                running=True,
                cli_message=None,
                snapshot_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            ):
                return {{
                    "id": deployment_id,
                    "projectId": project_id,
                    "environmentId": environment_id,
                    "serviceId": service_id,
                    "snapshotId": snapshot_id,
                    "status": status,
                    "deploymentStopped": False,
                    "canRollback": True,
                    "createdAt": now,
                    "meta": {{"imageDigest": digest, "cliMessage": cli_message}},
                    "instances": [{{"id": "instance", "status": "RUNNING"}}] if running else [],
                }}

            candidate = deployment(
                candidate_id,
                status=candidate_status,
                digest="sha256:candidate",
                running=candidate_status == "SUCCESS",
                cli_message=message,
            )
            if rolled_back:
                candidate["status"] = "REMOVED"
                candidate["deploymentStopped"] = True
                candidate["instances"] = []
            prior = deployment(
                prior_id,
                digest="sha256:prior",
                running=candidate_status != "SUCCESS",
                cli_message="prior",
            )
            rollback = deployment(
                rollback_id,
                digest="sha256:prior",
                cli_message="rollback",
                snapshot_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            )

            if args[:2] == ["deployment", "list"]:
                rows = [
                    {{"id": candidate_id, "status": "REMOVED" if rolled_back else candidate_status, "createdAt": now, "meta": {{"cliMessage": message, "imageDigest": "sha256:candidate"}}}},
                    {{"id": prior_id, "status": "REMOVED", "createdAt": now, "meta": {{"cliMessage": "prior", "imageDigest": "sha256:prior"}}}},
                ]
                if rolled_back:
                    rows.insert(0, {{"id": rollback_id, "status": "SUCCESS", "createdAt": now, "meta": {{"cliMessage": "rollback", "imageDigest": "sha256:prior"}}}})
                print(json.dumps(rows))
                raise SystemExit(0)
            if args[0] != "api":
                raise SystemExit("unexpected fake Railway command")
            query = args[1]
            if "ActiveDeployments" in query:
                active = [rollback] if rolled_back else ([candidate] if candidate_status == "SUCCESS" else [prior])
                print(json.dumps({{"data": {{"serviceInstance": {{"activeDeployments": active}}}}}}))
                raise SystemExit(0)
            if "TargetDomains" in query:
                print(json.dumps({{"data": {{"serviceInstance": {{
                    "environmentId": environment_id,
                    "serviceId": service_id,
                    "domains": {{
                        "customDomains": [],
                        "serviceDomains": [{{
                            "domain": "production.example",
                            "environmentId": environment_id,
                            "serviceId": service_id,
                            "syncStatus": "ACTIVE",
                        }}],
                    }},
                    "tcpProxies": [],
                }}}}}}))
                raise SystemExit(0)
            if "ExactDeployment" in query:
                raw_id = next(value for index, value in enumerate(args) if args[index - 1] == "--raw-var")
                deployment_id = raw_id.split("=", 1)[1]
                if deployment_id == candidate_id:
                    selected = candidate
                elif deployment_id == prior_id:
                    selected = prior
                else:
                    selected = rollback
                print(json.dumps({{"data": {{"deployment": selected}}}}))
                raise SystemExit(0)
            if "deploymentRollback" in query:
                raw_id = next(value for index, value in enumerate(args) if args[index - 1] == "--raw-var")
                if raw_id != f"id={{prior_id}}":
                    raise SystemExit("rollback did not target the recorded prior deployment")
                (state_dir / "rolled-back").write_text("true", encoding="utf-8")
                print(json.dumps({{"data": {{"deploymentRollback": True}}}}))
                raise SystemExit(0)
            raise SystemExit("unexpected fake Railway API query")
            """
        ),
        encoding="utf-8",
    )
    fake_railway.chmod(0o755)

    fetch_stub = tmp_path / "fetch-stub.mjs"
    fetch_stub.write_text(
        "globalThis.fetch = async () => ({ status: 200, async json() { "
        "return { status: 'healthy', version: '0.6.5', "
        "commit_sha: null, ready: true }; } });\n",
        encoding="utf-8",
    )
    state_dir = tmp_path / "railway-state"
    state_file = tmp_path / "release-state.json"
    prior_id = "33333333-3333-4333-8333-333333333333"
    candidate_id = "11111111-1111-4111-8111-111111111111"
    message = f"bsmcp:production:123:456:{'a' * 40}:prev:{prior_id}"
    state_file.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "phase": "candidate_success" if candidate_status == "SUCCESS" else "candidate_bound",
                "mode": "production",
                "baseUrl": "https://production.example",
                "message": message,
                "startedAt": "2026-08-13T00:00:00Z",
                "startedAtEpochMs": int(__import__("time").time() * 1000),
                "target": {
                    "project": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "environment": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                    "service": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                },
                "beforeDeploymentIds": [prior_id],
                "prior": {
                    "id": prior_id,
                    "projectId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "environmentId": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                    "serviceId": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                    "canRollback": True,
                    "imageDigest": "sha256:prior",
                    "snapshotId": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                    "health": {"status": 200, "version": "0.6.5", "commitSha": None},
                    "readiness": {
                        "status": 200,
                        "ready": True,
                        "legacyTransactionBridge": {
                            "configuration_valid": True,
                            "economic_writes_locked": False,
                        },
                    },
                },
                "backup": {"backupId": "backup-id"},
                "candidate": {
                    "id": candidate_id,
                    "status": candidate_status,
                    "imageDigest": "sha256:candidate",
                },
                "accepted": False,
                "recovery": None,
            }
        ),
        encoding="utf-8",
    )
    environment = {
        **os.environ,
        "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}",
        "FAKE_RAILWAY_STATE": str(state_dir),
        "FAKE_CANDIDATE_STATUS": candidate_status,
        "NODE_OPTIONS": f"--import={fetch_stub}",
    }
    command = [
        "node",
        str(ROOT / "scripts" / "recover_railway_release.mjs"),
        "--state-file",
        str(state_file),
    ]
    first = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    calls_after_first = (state_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    second = subprocess.run(
        command,
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    calls_after_second = (state_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()

    assert first.returncode == 0, first.stdout + first.stderr
    assert second.returncode == 0, second.stdout + second.stderr
    assert len(calls_after_second) == len(calls_after_first)
    calls = [json.loads(call) for call in calls_after_first]
    rollback_calls = [call for call in calls if call[0] == "api" and "deploymentRollback" in call[1]]
    assert len(rollback_calls) == 1
    assert not any(
        call[0] == "api"
        and call[1].startswith("mutation")
        and ("deploymentStop" in call[1] or "deploymentCancel" in call[1])
        for call in calls
    )
    recovered = json.loads(state_file.read_text(encoding="utf-8"))
    assert recovered["phase"] == "recovered"
    assert recovered["recovery"]["action"] == "rolled_back"
    assert recovered["recovery"]["deploymentId"] == "44444444-4444-4444-8444-444444444444"


def _run_recovery_adversary(
    tmp_path: Path,
    scenario: str,
    *,
    initial_recovery: str | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[list[str]], dict[str, object]]:
    fake_railway = tmp_path / "railway"
    fake_railway.write_text(
        textwrap.dedent(
            """\
            #!__PYTHON__
            import datetime
            import json
            import os
            from pathlib import Path
            import sys
            import time

            args = sys.argv[1:]
            state_dir = Path(os.environ["FAKE_RAILWAY_STATE"])
            state_dir.mkdir(parents=True, exist_ok=True)
            with (state_dir / "calls.jsonl").open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(args) + "\\n")

            scenario = os.environ["FAKE_RECOVERY_SCENARIO"]
            project_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
            environment_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
            service_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
            prior_id = "33333333-3333-4333-8333-333333333333"
            candidate_id = "11111111-1111-4111-8111-111111111111"
            rollback_id = "44444444-4444-4444-8444-444444444444"
            unrelated_id = "55555555-5555-4555-8555-555555555555"
            lagged_id = "66666666-6666-4666-8666-666666666666"
            message = "bsmcp:production:123:456:" + ("a" * 40) + ":prev:" + prior_id
            now = "2026-08-13T00:00:02Z"
            if scenario == "rollback_transient_reads":
                created_at_file = state_dir / "created-at"
                if created_at_file.exists():
                    now = created_at_file.read_text(encoding="utf-8")
                else:
                    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
                    created_at_file.write_text(now, encoding="utf-8")
            rollback_requested = (state_dir / "rollback-requested").exists()

            def fail_once(site):
                marker = state_dir / f"failed-{site}"
                if marker.exists():
                    return
                marker.write_text("true", encoding="utf-8")
                print(f"transient Railway failure at {site}", file=sys.stderr)
                raise SystemExit(1)

            def hang_once(site):
                marker = state_dir / f"hung-{site}"
                if marker.exists():
                    return
                marker.write_text("true", encoding="utf-8")
                time.sleep(2)

            def numbered_call(site):
                counter = state_dir / f"{site}-calls"
                count = int(counter.read_text() if counter.exists() else "0") + 1
                counter.write_text(str(count), encoding="utf-8")
                return count

            def deployment(
                deployment_id,
                *,
                status,
                digest,
                running,
                cli_message,
                stopped=False,
                snapshot_id="dddddddd-dddd-4ddd-8ddd-dddddddddddd",
            ):
                return {
                    "id": deployment_id,
                    "projectId": project_id,
                    "environmentId": environment_id,
                    "serviceId": service_id,
                    "snapshotId": snapshot_id,
                    "status": status,
                    "deploymentStopped": stopped,
                    "canRollback": True,
                    "createdAt": now,
                    "meta": {"imageDigest": digest, "cliMessage": cli_message},
                    "instances": [{"id": "instance", "status": "RUNNING"}] if running else [],
                }

            def candidate():
                if scenario.startswith("rollback_"):
                    if scenario in {"rollback_arm_before_mutation", "rollback_transient_reads"}:
                        stopped = rollback_requested
                    elif scenario == "rollback_prearm_lagged":
                        stopped = False
                    else:
                        stopped = True
                    return deployment(
                        candidate_id,
                        status="REMOVED" if stopped else "DEPLOYING",
                        digest="sha256:candidate",
                        running=False,
                        cli_message=message,
                        stopped=stopped,
                    )
                if not (state_dir / "cancel-requested").exists():
                    status = "BUILDING"
                elif scenario == "cancel_false_terminal":
                    status = "REMOVED"
                else:
                    reads_file = state_dir / "post-cancel-reads"
                    reads = int(reads_file.read_text() if reads_file.exists() else "0")
                    reads_file.write_text(str(reads + 1), encoding="utf-8")
                    status = "REMOVING" if reads == 0 else "REMOVED"
                return deployment(
                    candidate_id,
                    status=status,
                    digest="sha256:candidate",
                    running=False,
                    cli_message=message,
                    stopped=status == "REMOVED",
                )

            prior = deployment(
                prior_id,
                status="SUCCESS",
                digest="sha256:prior",
                running=True,
                cli_message="prior",
            )
            rollback_digest = (
                "sha256:unrelated" if scenario == "rollback_wrong_digest" else "sha256:prior"
            )
            rollback = deployment(
                rollback_id,
                status="SUCCESS",
                digest=rollback_digest,
                running=True,
                cli_message="rollback",
                snapshot_id="eeeeeeee-eeee-4eee-8eee-eeeeeeeeeeee",
            )
            unrelated = deployment(
                unrelated_id,
                status="BUILDING",
                digest="sha256:unrelated",
                running=False,
                cli_message="external-deploy",
            )
            lagged = deployment(
                lagged_id,
                status="REMOVED",
                digest="sha256:prior",
                running=False,
                cli_message="lagged-same-digest",
                stopped=True,
            )

            if args[:2] == ["deployment", "list"]:
                rows = [candidate(), prior]
                rollback_visible = scenario in {
                    "rollback_resume",
                    "rollback_multiple_delta",
                    "rollback_wrong_digest",
                } or (
                    scenario in {"rollback_arm_before_mutation", "rollback_transient_reads"}
                    and rollback_requested
                )
                if rollback_visible:
                    rows.insert(0, rollback)
                if scenario == "rollback_multiple_delta":
                    rows.insert(0, unrelated)
                if scenario == "rollback_prearm_lagged":
                    rows.insert(0, lagged)
                print(json.dumps(rows))
                raise SystemExit(0)
            if args[0] != "api":
                raise SystemExit("unexpected fake Railway command")

            query = args[1]
            if "TargetDomains" in query:
                if scenario == "cancel_timeout_read":
                    hang_once("domain")
                if scenario in {"cancel_transient_reads", "rollback_transient_reads"}:
                    fail_once("domain")
                print(json.dumps({"data": {"serviceInstance": {
                    "environmentId": environment_id,
                    "serviceId": service_id,
                    "domains": {
                        "customDomains": [],
                        "serviceDomains": [{
                            "domain": "production.example",
                            "environmentId": environment_id,
                            "serviceId": service_id,
                            "syncStatus": "ACTIVE",
                        }],
                    },
                    "tcpProxies": [],
                }}}))
                raise SystemExit(0)
            if "ActiveDeployments" in query:
                active_call = numbered_call("active")
                if scenario == "rollback_transient_reads" and active_call == 1:
                    fail_once("initial-active")
                if scenario == "cancel_transient_reads" and active_call == 2:
                    fail_once("pre-cancel-active")
                rollback_active = scenario in {
                    "rollback_resume",
                    "rollback_multiple_delta",
                    "rollback_wrong_digest",
                } or (
                    scenario in {"rollback_arm_before_mutation", "rollback_transient_reads"}
                    and rollback_requested
                )
                active = [rollback] if rollback_active else [prior]
                print(json.dumps({"data": {"serviceInstance": {"activeDeployments": active}}}))
                raise SystemExit(0)
            if "ExactDeployment" in query:
                raw_vars = [args[index + 1] for index, item in enumerate(args) if item == "--raw-var"]
                deployment_id = next(value.split("=", 1)[1] for value in raw_vars if value.startswith("id="))
                if deployment_id == candidate_id:
                    candidate_call = numbered_call("candidate")
                    if scenario == "rollback_transient_reads" and candidate_call == 1:
                        fail_once("initial-candidate")
                    if scenario == "cancel_transient_reads" and candidate_call == 2:
                        fail_once("pre-cancel-candidate")
                if deployment_id == prior_id and scenario == "rollback_transient_reads":
                    fail_once("prior")
                selected = (
                    candidate()
                    if deployment_id == candidate_id
                    else prior
                    if deployment_id == prior_id
                    else rollback
                )
                print(json.dumps({"data": {"deployment": selected}}))
                raise SystemExit(0)
            if "deploymentCancel" in query:
                (state_dir / "cancel-requested").write_text("true", encoding="utf-8")
                if scenario == "cancel_transient_reads":
                    fail_once("cancel-after-apply")
                acknowledged = scenario == "cancel_ack_removing"
                print(json.dumps({"data": {"deploymentCancel": acknowledged}}))
                raise SystemExit(0)
            if "deploymentRollback" in query:
                if (
                    scenario not in {"rollback_arm_before_mutation", "rollback_transient_reads"}
                    or rollback_requested
                ):
                    raise SystemExit("unsafe duplicate rollback mutation")
                (state_dir / "rollback-requested").write_text("true", encoding="utf-8")
                print(json.dumps({"data": {"deploymentRollback": True}}))
                raise SystemExit(0)
            if "deploymentStop" in query:
                raise SystemExit("unsafe stop mutation")
            raise SystemExit("unexpected fake Railway API query")
            """
        ).replace("__PYTHON__", sys.executable),
        encoding="utf-8",
    )
    fake_railway.chmod(0o755)

    fetch_stub = tmp_path / "fetch-stub.mjs"
    fetch_stub.write_text(
        textwrap.dedent(
            """
            const nativeSetTimeout = globalThis.setTimeout;
            globalThis.setTimeout = (callback, delay = 0, ...args) => {
              const numericDelay = Number(delay);
              const preserveReadTimeout =
                process.env.FAKE_RECOVERY_SCENARIO === "cancel_timeout_read"
                && numericDelay === 500;
              return nativeSetTimeout(
                callback,
                !preserveReadTimeout && numericDelay <= 5000 ? 0 : delay,
                ...args,
              );
            };
            globalThis.fetch = async () => ({
              status: 200,
              async json() { return { status: "healthy", version: "0.6.5", ready: true }; },
            });
            """
        ),
        encoding="utf-8",
    )

    state_dir = tmp_path / "railway-state"
    state_file = tmp_path / "release-state.json"
    prior_id = "33333333-3333-4333-8333-333333333333"
    candidate_id = "11111111-1111-4111-8111-111111111111"
    started_at_ms = int(
        __import__("datetime")
        .datetime.fromisoformat("2026-08-13T00:00:00+00:00")
        .timestamp()
        * 1000
    )
    rollback_armed = scenario in {
        "rollback_resume",
        "rollback_multiple_delta",
        "rollback_wrong_digest",
        "rollback_arm_before_mutation",
    }
    recovery_action = initial_recovery or ("rollback_armed" if rollback_armed else None)
    state_file.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "phase": "candidate_bound",
                "mode": "production",
                "baseUrl": "https://production.example",
                "message": f"bsmcp:production:123:456:{'a' * 40}:prev:{prior_id}",
                "startedAt": "2026-08-13T00:00:00Z",
                "startedAtEpochMs": started_at_ms,
                "target": {
                    "project": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "environment": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                    "service": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                },
                "beforeDeploymentIds": [prior_id],
                "prior": {
                    "id": prior_id,
                    "projectId": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
                    "environmentId": "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb",
                    "serviceId": "cccccccc-cccc-4ccc-8ccc-cccccccccccc",
                    "canRollback": True,
                    "imageDigest": "sha256:prior",
                    "snapshotId": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                    "health": {"status": 200, "version": "0.6.5", "commitSha": None},
                    "readiness": {
                        "status": 200,
                        "ready": True,
                        "legacyTransactionBridge": {
                            "configuration_valid": True,
                            "economic_writes_locked": False,
                        },
                    },
                },
                "backup": {"backupId": "backup-id"},
                "candidate": {
                    "id": candidate_id,
                    "status": "REMOVED" if rollback_armed else "BUILDING",
                    "imageDigest": "sha256:candidate",
                },
                "accepted": False,
                "recovery": (
                    {
                        "action": "rollback_armed",
                        "at": "2026-08-13T00:00:01Z",
                        "armedAtEpochMs": started_at_ms + 3_000,
                        "candidateCreatedAtEpochMs": started_at_ms + 2_000,
                        "priorDeploymentId": prior_id,
                        "candidateDeploymentId": candidate_id,
                        "beforeDeploymentIds": [prior_id, candidate_id],
                        "mutationAttempted": scenario != "rollback_arm_before_mutation",
                        "mutationAcknowledged": scenario != "rollback_arm_before_mutation",
                        "rollbackDeploymentId": None,
                    }
                    if recovery_action == "rollback_armed"
                    else {
                        "action": "candidate_cancel_armed",
                        "at": "2026-08-13T00:00:01Z",
                        "candidateDeploymentId": candidate_id,
                        "mutationAttempted": False,
                        "mutationAcknowledged": False,
                    }
                    if recovery_action == "candidate_cancel_armed"
                    else None
                ),
            }
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "node",
            str(ROOT / "scripts" / "recover_railway_release.mjs"),
            "--state-file",
            str(state_file),
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}",
            "FAKE_RAILWAY_STATE": str(state_dir),
            "FAKE_RECOVERY_SCENARIO": scenario,
            "NODE_OPTIONS": f"--import={fetch_stub}",
            "RELEASE_RECOVERY_TIMEOUT_MS": "60000",
            "RELEASE_RECOVERY_READ_ATTEMPT_TIMEOUT_MS": (
                "500" if scenario == "cancel_timeout_read" else "10000"
            ),
        },
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    calls = [
        json.loads(line)
        for line in (state_dir / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    ]
    final_state = json.loads(state_file.read_text(encoding="utf-8"))
    return result, calls, final_state


@pytest.mark.parametrize("scenario", ["cancel_false_terminal", "cancel_ack_removing"])
def test_production_cancel_terminal_transitions_never_stop_or_rollback(
    tmp_path: Path,
    scenario: str,
) -> None:
    result, calls, state = _run_recovery_adversary(tmp_path, scenario)

    assert result.returncode == 0, result.stdout + result.stderr
    cancel_calls = [
        call for call in calls if call[0] == "api" and "deploymentCancel" in call[1]
    ]
    assert len(cancel_calls) == 1
    assert not any(
        call[0] == "api"
        and call[1].startswith("mutation")
        and ("deploymentRollback" in call[1] or "deploymentStop" in call[1])
        for call in calls
    )
    assert state["phase"] == "recovered"
    assert state["recovery"]["action"] == "candidate_ended"
    assert state["recovery"]["deploymentId"] == "11111111-1111-4111-8111-111111111111"


def test_armed_rollback_with_exact_created_row_resumes_without_second_mutation(
    tmp_path: Path,
) -> None:
    result, calls, state = _run_recovery_adversary(tmp_path, "rollback_resume")

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Resuming an already armed Railway rollback" in result.stderr
    assert not any(
        call[0] == "api" and call[1].startswith("mutation")
        for call in calls
    )
    assert state["phase"] == "recovered"
    assert state["recovery"]["action"] == "rolled_back"
    assert state["recovery"]["deploymentId"] == "44444444-4444-4444-8444-444444444444"


@pytest.mark.parametrize(
    ("scenario", "expected_error"),
    [
        ("rollback_multiple_delta", "multiple deployments appeared after rollback was armed"),
        ("rollback_wrong_digest", "unexpected image digest"),
    ],
)
def test_armed_rollback_rejects_unrelated_post_arm_delta_without_mutation(
    tmp_path: Path,
    scenario: str,
    expected_error: str,
) -> None:
    result, calls, state = _run_recovery_adversary(tmp_path, scenario)

    assert result.returncode != 0
    assert expected_error in result.stderr
    assert not any(
        call[0] == "api" and call[1].startswith("mutation")
        for call in calls
    )
    assert state["recovery"]["action"] == "rollback_armed"


def test_rollback_armed_before_mutation_refences_and_issues_exactly_once(
    tmp_path: Path,
) -> None:
    result, calls, state = _run_recovery_adversary(tmp_path, "rollback_arm_before_mutation")

    assert result.returncode == 0, result.stdout + result.stderr
    rollback_calls = [
        call for call in calls if call[0] == "api" and "deploymentRollback" in call[1]
    ]
    assert len(rollback_calls) == 1
    first_mutation_index = next(
        index for index, call in enumerate(calls) if call in rollback_calls
    )
    assert any(call[:2] == ["deployment", "list"] for call in calls[:first_mutation_index])
    assert state["phase"] == "recovered"
    assert state["recovery"]["action"] == "rolled_back"


def test_candidate_cancel_armed_before_mutation_refences_and_issues_exactly_once(
    tmp_path: Path,
) -> None:
    result, calls, state = _run_recovery_adversary(
        tmp_path,
        "cancel_ack_removing",
        initial_recovery="candidate_cancel_armed",
    )

    assert result.returncode == 0, result.stdout + result.stderr
    cancel_calls = [
        call for call in calls if call[0] == "api" and "deploymentCancel" in call[1]
    ]
    assert len(cancel_calls) == 1
    first_mutation_index = next(index for index, call in enumerate(calls) if call in cancel_calls)
    assert any(call[:2] == ["deployment", "list"] for call in calls[:first_mutation_index])
    assert not any(
        call[0] == "api"
        and call[1].startswith("mutation")
        and ("deploymentRollback" in call[1] or "deploymentStop" in call[1])
        for call in calls
    )
    assert state["phase"] == "recovered"
    assert state["recovery"]["action"] == "candidate_ended"


def test_lagged_prearm_same_digest_row_is_rejected_before_rollback_mutation(
    tmp_path: Path,
) -> None:
    result, calls, state = _run_recovery_adversary(tmp_path, "rollback_prearm_lagged")

    assert result.returncode != 0
    assert "unrelated deployment appeared before the rollback was armed" in result.stderr
    assert not any(
        call[0] == "api" and call[1].startswith("mutation")
        for call in calls
    )
    assert state["recovery"] is None


def test_transient_rollback_reads_retry_before_exactly_one_mutation(
    tmp_path: Path,
) -> None:
    result, calls, state = _run_recovery_adversary(tmp_path, "rollback_transient_reads")

    assert result.returncode == 0, result.stdout + result.stderr
    state_dir = tmp_path / "railway-state"
    for site in ("domain", "initial-candidate", "initial-active", "prior"):
        assert (state_dir / f"failed-{site}").is_file()
    rollback_calls = [
        call for call in calls if call[0] == "api" and "deploymentRollback" in call[1]
    ]
    assert len(rollback_calls) == 1
    assert not any(
        call[0] == "api"
        and call[1].startswith("mutation")
        and ("deploymentCancel" in call[1] or "deploymentStop" in call[1])
        for call in calls
    )
    assert state["phase"] == "recovered"
    assert state["recovery"]["action"] == "rolled_back"


def test_transient_precancel_reads_and_ambiguous_cancel_complete_without_reissue(
    tmp_path: Path,
) -> None:
    result, calls, state = _run_recovery_adversary(tmp_path, "cancel_transient_reads")

    assert result.returncode == 0, result.stdout + result.stderr
    state_dir = tmp_path / "railway-state"
    for site in (
        "domain",
        "pre-cancel-candidate",
        "pre-cancel-active",
        "cancel-after-apply",
    ):
        assert (state_dir / f"failed-{site}").is_file()
    cancel_calls = [
        call for call in calls if call[0] == "api" and "deploymentCancel" in call[1]
    ]
    assert len(cancel_calls) == 1
    assert not any(
        call[0] == "api"
        and call[1].startswith("mutation")
        and ("deploymentRollback" in call[1] or "deploymentStop" in call[1])
        for call in calls
    )
    assert state["phase"] == "recovered"
    assert state["recovery"]["action"] == "candidate_ended"


def test_timed_out_recovery_read_is_killed_and_retried_before_cancellation(
    tmp_path: Path,
) -> None:
    result, calls, state = _run_recovery_adversary(tmp_path, "cancel_timeout_read")

    assert result.returncode == 0, result.stdout + result.stderr
    assert (tmp_path / "railway-state" / "hung-domain").is_file()
    domain_calls = [
        call for call in calls if call[0] == "api" and "TargetDomains" in call[1]
    ]
    cancel_calls = [
        call for call in calls if call[0] == "api" and "deploymentCancel" in call[1]
    ]
    first_cancel_index = next(index for index, call in enumerate(calls) if call in cancel_calls)
    domain_calls_before_cancel = [
        call
        for call in calls[:first_cancel_index]
        if call[0] == "api" and "TargetDomains" in call[1]
    ]
    assert len(domain_calls_before_cancel) == 2
    assert len(domain_calls) >= 2
    assert len(cancel_calls) == 1
    assert state["phase"] == "recovered"
    assert state["recovery"]["action"] == "candidate_ended"


def test_recovery_rejects_corrupt_state_without_calling_railway(tmp_path: Path) -> None:
    state_file = tmp_path / "release-state.json"
    state_file.write_text('{"schemaVersion":1,"mode":"production"}', encoding="utf-8")

    result = subprocess.run(
        [
            "node",
            str(ROOT / "scripts" / "recover_railway_release.mjs"),
            "--state-file",
            str(state_file),
        ],
        cwd=ROOT,
        env=os.environ.copy(),
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert result.returncode != 0
    assert "release state is invalid" in result.stderr


@pytest.mark.parametrize(
    ("served_commit_matches", "interference"),
    [
        (True, "none"),
        (False, "none"),
        (True, "history"),
        (True, "active"),
    ],
)
def test_release_acceptance_requires_the_exact_active_ready_commit(
    tmp_path: Path,
    served_commit_matches: bool,
    interference: str,
) -> None:
    expected_commit = "a" * 40
    served_commit = expected_commit if served_commit_matches else "b" * 40
    deployment_id = "11111111-1111-4111-8111-111111111111"
    project_id = "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa"
    environment_id = "bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb"
    service_id = "cccccccc-cccc-4ccc-8ccc-cccccccccccc"
    prior_id = "22222222-2222-4222-8222-222222222222"
    interfering_id = "44444444-4444-4444-8444-444444444444"
    message = f"bsmcp:staging:123:456:{expected_commit}"
    fake_railway = tmp_path / "railway"
    fake_railway.write_text(
        textwrap.dedent(
            f"""\
            #!{sys.executable}
            import json
            import sys
            args = sys.argv[1:]
            if args[:2] == ["deployment", "list"]:
                rows = [
                    {{"id": "{deployment_id}", "status": "SUCCESS", "createdAt": "2026-08-13T00:01:00Z", "meta": {{"cliMessage": "{message}"}}}},
                    {{"id": "{prior_id}", "status": "REMOVED", "createdAt": "2026-08-13T00:00:00Z", "meta": {{"cliMessage": "prior"}}}},
                ]
                if {interference == "history"!r}:
                    rows.insert(0, {{"id": "{interfering_id}", "status": "BUILDING", "createdAt": "2026-08-13T00:02:00Z", "meta": {{"cliMessage": "external"}}}})
                print(json.dumps(rows))
                raise SystemExit(0)
            if args[0] != "api":
                raise SystemExit("unexpected command")
            deployment = {{
                "id": "{deployment_id}",
                "projectId": "{project_id}",
                "environmentId": "{environment_id}",
                "serviceId": "{service_id}",
                "snapshotId": "dddddddd-dddd-4ddd-8ddd-dddddddddddd",
                "status": "SUCCESS",
                "deploymentStopped": False,
                "canRollback": True,
                "createdAt": "2026-08-13T00:00:00Z",
                "meta": {{"imageDigest": "sha256:candidate", "cliMessage": "{message}"}},
                "instances": [{{"id": "instance", "status": "RUNNING"}}],
            }}
            if "ExactDeployment" in args[1]:
                print(json.dumps({{"data": {{"deployment": deployment}}}}))
            elif "ActiveDeployments" in args[1]:
                active = [deployment]
                if {interference == "active"!r}:
                    active.append({{
                        **deployment,
                        "id": "{interfering_id}",
                        "status": "DEPLOYING",
                        "meta": {{"imageDigest": "sha256:external", "cliMessage": "external"}},
                        "instances": [],
                    }})
                print(json.dumps({{"data": {{"serviceInstance": {{"activeDeployments": active}}}}}}))
            elif "TargetDomains" in args[1]:
                print(json.dumps({{"data": {{"serviceInstance": {{
                    "environmentId": "{environment_id}",
                    "serviceId": "{service_id}",
                    "domains": {{"customDomains": [], "serviceDomains": [{{
                        "domain": "staging.example",
                        "environmentId": "{environment_id}",
                        "serviceId": "{service_id}",
                        "syncStatus": "ACTIVE",
                    }}]}},
                    "tcpProxies": [],
                }}}}}}))
            else:
                raise SystemExit("unexpected API query")
            """
        ),
        encoding="utf-8",
    )
    fake_railway.chmod(0o755)
    fetch_stub = tmp_path / "fetch-stub.mjs"
    fetch_stub.write_text(
        "globalThis.fetch = async () => ({ status: 200, async json() { "
        f"return {{ status: 'ready', ready: true, version: '0.6.5', commit_sha: '{served_commit}' }}; "
        "} });\n",
        encoding="utf-8",
    )
    state_file = tmp_path / "release-state.json"
    state_file.write_text(
        json.dumps(
            {
                "schemaVersion": 1,
                "phase": "candidate_success",
                "mode": "staging",
                "baseUrl": "https://staging.example",
                "message": message,
                "target": {
                    "project": project_id,
                    "environment": environment_id,
                    "service": service_id,
                },
                "beforeDeploymentIds": [prior_id],
                "candidate": {
                    "id": deployment_id,
                    "status": "SUCCESS",
                    "imageDigest": "sha256:candidate",
                },
                "accepted": False,
            }
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        [
            "node",
            str(ROOT / "scripts" / "accept_railway_release.mjs"),
            "--state-file",
            str(state_file),
            "--deployment-id",
            deployment_id,
            "--expected-commit",
            expected_commit,
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "PATH": f"{tmp_path}{os.pathsep}{os.environ.get('PATH', '')}",
            "NODE_OPTIONS": f"--import={fetch_stub}",
        },
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    accepted_state = json.loads(state_file.read_text(encoding="utf-8"))
    if served_commit_matches and interference == "none":
        assert result.returncode == 0, result.stdout + result.stderr
        assert accepted_state["accepted"] is True
        assert accepted_state["phase"] == "accepted"
        assert accepted_state["acceptedCommit"] == expected_commit
    else:
        assert result.returncode != 0
        if not served_commit_matches:
            assert "final hosted readiness" in result.stderr
        elif interference == "history":
            assert "deployment history" in result.stderr
        else:
            assert "sole active" in result.stderr
        assert accepted_state["accepted"] is False


def test_gitlab_is_deployment_inert_after_github_cutover() -> None:
    pipeline = (ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8")

    assert "validate_gitlab_deploy_authority_disabled:" in pipeline
    assert "GitLab CI is intentionally deployment-inert" in pipeline
    assert "deploy_staging:" not in pipeline
    assert "deploy_production:" not in pipeline
    assert "railway up" not in pipeline
    assert "railway deploy" not in pipeline
    assert "RAILWAY_TOKEN" not in pipeline
    assert "PRODUCTION_RAILWAY_TOKEN" not in pipeline
    assert "STAGING_RAILWAY_TOKEN" not in pipeline


def test_hosted_audit_executes_every_oauth_route_with_the_expected_method() -> None:
    expected_manifest = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    expected_tools = [
        "fetch",
        "get_market_data_endpoint",
        "get_pricing_info",
        "get_product_catalog",
        "get_workflow_endpoint",
        "list_instruments",
        "search",
        "search_pairs",
    ]
    expected_post_bodies = {
        "/v1/briefs/market": {"symbols": ["BTCUSD"]},
        "/v1/checks/pre-trade": {
            "symbol": "BTCUSD",
            "side": "buy",
            "notional_usd": 1,
        },
        "/v1/receipts/price": {"symbol": "BTCUSD"},
        "/v1/snapshots/macro": {"universe": ["BTCUSD"]},
        "/v1/indicators/token-quality": {"symbol": "BTCUSD"},
        "/v1/indicators/state-divergence": {"symbol": "BTCUSD"},
        "/v1/signals/solana-token-brief": {"symbols": ["SOLUSD"]},
        "/v1/signals/trader-alpha-pack": {"symbols": ["BTCUSD"]},
    }

    class CandidateHandler(BaseHTTPRequestHandler):
        calls: list[tuple[str, str]] = []
        request_bodies: dict[str, object] = {}
        user_agents: list[str] = []
        include_unknown_x402_resource = False
        base_url = ""

        def log_message(self, _format: str, *_args: object) -> None:
            return

        def _send(
            self,
            status: int,
            body: bytes = b"",
            *,
            content_type: str = "text/plain",
            headers: dict[str, str] | None = None,
        ) -> None:
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            if body:
                self.wfile.write(body)

        def _json(
            self,
            status: int,
            payload: object,
            *,
            headers: dict[str, str] | None = None,
        ) -> None:
            self._send(
                status,
                json.dumps(payload).encode("utf-8"),
                content_type="application/json",
                headers=headers,
            )

        def do_GET(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            self.calls.append(("GET", path))
            self.user_agents.append(self.headers.get("User-Agent", ""))
            if path == "/readyz":
                self._json(
                    200,
                    {
                        "status": "ready",
                        "ready": True,
                        "version": expected_manifest["version"],
                        "commit_sha": None,
                        "checks": {"controlled_candidate": {"ready": True}},
                    },
                )
                return
            if path == "/health":
                links = {
                    "remote_mcp": f"{self.base_url}/mcp/server/",
                    "anthropic_mcp": f"{self.base_url}/anthropic/mcp/",
                    "cursor_mcp": f"{self.base_url}/cursor/mcp/",
                    "openai_mcp": f"{self.base_url}/openai/mcp/",
                    "manifest": f"{self.base_url}/mcp/manifest.json",
                }
                for connector in ("anthropic", "cursor", "openai"):
                    links[f"{connector}_oauth_callback"] = (
                        f"{self.base_url}/{connector}/mcp/auth/callback"
                    )
                self._json(
                    200,
                    {
                        "status": "healthy",
                        "version": expected_manifest["version"],
                        "commit_sha": None,
                        "links": links,
                    },
                )
                return
            if path == "/server.json":
                candidate_manifest = {
                    **expected_manifest,
                    "homepage": f"{self.base_url}/",
                    "websiteUrl": f"{self.base_url}/",
                    "remotes": [
                        {
                            **remote,
                            "url": f"{self.base_url}/mcp/server/",
                        }
                        for remote in expected_manifest["remotes"]
                    ],
                }
                self._json(200, candidate_manifest)
                return
            if path == "/mcp/manifest.json":
                self._json(
                    200,
                    {
                        "links": {
                            "homepage": self.base_url,
                            "support": f"{self.base_url}/support",
                        }
                    },
                )
                return
            if path == "/sitemap.xml":
                body = (
                    f"<urlset><url><loc>{self.base_url}/</loc></url>"
                    f"<url><loc>{self.base_url}/support</loc></url></urlset>"
                ).encode("utf-8")
                self._send(200, body, content_type="application/xml")
                return
            if path.startswith("/.well-known/oauth-protected-resource/"):
                connector = path.split("/")[3]
                self._json(
                    200,
                    {
                        "oauth_available": True,
                        "authorization_servers": [f"{self.base_url}/{connector}/mcp"],
                    },
                )
                return
            if path.startswith("/.well-known/oauth-authorization-server/"):
                connector = path.split("/")[3]
                prefix = f"{self.base_url}/{connector}/mcp"
                self._json(
                    200,
                    {
                        "oauth_available": True,
                        "authorization_endpoint": f"{prefix}/authorize",
                        "token_endpoint": f"{prefix}/token",
                        "registration_endpoint": f"{prefix}/register",
                    },
                )
                return
            if path == "/.well-known/x402":
                resources = [
                    f"{self.base_url}/v1/vwap/BTC-USD",
                    *(
                        f"{self.base_url}{post_path}"
                        for post_path in expected_post_bodies
                    ),
                ]
                if self.include_unknown_x402_resource:
                    resources.append(f"{self.base_url}/v1/unknown-side-effect")
                self._json(
                    200,
                    {
                        "version": 1,
                        "resources": resources,
                    },
                )
                return
            if path in {"/anthropic/mcp/", "/cursor/mcp/", "/openai/mcp/"}:
                self._json(
                    401,
                    {"error": "authentication required"},
                    headers={
                        "WWW-Authenticate": (
                            'Bearer resource_metadata="/.well-known/oauth-protected-resource"'
                        )
                    },
                )
                return
            if path.endswith(("/authorize", "/auth/callback")):
                self._json(400, {"error": "controlled_oauth_request"})
                return
            if path == "/v1/vwap/BTC-USD":
                self._json(
                    402,
                    {"x402Version": 2},
                    headers={"PAYMENT-REQUIRED": "test", "Cache-Control": "no-store"},
                )
                return
            if path in {"/.env", "/.git/config"}:
                self._send(404)
                return
            self._send(200, b"controlled candidate")

        def do_POST(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            self.calls.append(("POST", path))
            self.user_agents.append(self.headers.get("User-Agent", ""))
            length = int(self.headers.get("Content-Length", "0"))
            raw_body = self.rfile.read(length) if length else b""
            if path == "/mcp/server/":
                request = json.loads(raw_body or b"{}")
                method = request.get("method")
                if method == "notifications/initialized":
                    self._send(202)
                    return
                if method == "initialize":
                    result = {
                        "protocolVersion": "2025-03-26",
                        "serverInfo": {
                            "name": "controlled-candidate",
                            "version": expected_manifest["version"],
                        },
                    }
                elif method == "tools/list":
                    result = {
                        "tools": [
                            {
                                "name": name,
                                "annotations": {"readOnlyHint": True},
                            }
                            for name in expected_tools
                        ]
                    }
                elif method == "tools/call":
                    result = {"content": [], "isError": False}
                else:
                    self._json(400, {"error": "unexpected MCP method"})
                    return
                payload = {
                    "jsonrpc": "2.0",
                    "id": request.get("id"),
                    "result": result,
                }
                body = f"data: {json.dumps(payload)}\n\n".encode("utf-8")
                self._send(
                    200,
                    body,
                    content_type="text/event-stream",
                    headers={"Mcp-Session-Id": "controlled-session"},
                )
                return
            if path.endswith(("/token", "/register")):
                self._json(400, {"error": "controlled_oauth_request"})
                return
            if path in expected_post_bodies:
                self.request_bodies[path] = json.loads(raw_body or b"{}")
                self._json(
                    402,
                    {"x402Version": 2},
                    headers={"PAYMENT-REQUIRED": "test", "Cache-Control": "no-store"},
                )
                return
            self._send(404)

        def do_DELETE(self) -> None:  # noqa: N802
            path = self.path.split("?", 1)[0]
            self.calls.append(("DELETE", path))
            self.user_agents.append(self.headers.get("User-Agent", ""))
            self._send(200 if path == "/mcp/server/" else 404)

    try:
        server = ThreadingHTTPServer(("127.0.0.1", 0), CandidateHandler)
    except PermissionError:
        result = subprocess.run(
            ["node", str(ROOT / "scripts" / "test_hosted_release_audit.mjs")],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        return
    CandidateHandler.base_url = f"http://127.0.0.1:{server.server_port}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        result = subprocess.run(
            ["node", str(ROOT / "scripts" / "audit_hosted_release.mjs"), CandidateHandler.base_url],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        CandidateHandler.include_unknown_x402_resource = True
        CandidateHandler.calls.clear()
        unknown_result = subprocess.run(
            ["node", str(ROOT / "scripts" / "audit_hosted_release.mjs"), CandidateHandler.base_url],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    assert result.returncode == 0, result.stdout + result.stderr
    for connector in ("anthropic", "cursor", "openai"):
        assert ("GET", f"/{connector}/mcp/authorize") in CandidateHandler.calls
        assert ("POST", f"/{connector}/mcp/token") in CandidateHandler.calls
        assert ("POST", f"/{connector}/mcp/register") in CandidateHandler.calls
        assert ("GET", f"/{connector}/mcp/auth/callback") in CandidateHandler.calls
    assert ("GET", "/v1/vwap/BTC-USD") in CandidateHandler.calls
    for path, body in expected_post_bodies.items():
        assert ("POST", path) in CandidateHandler.calls
        assert CandidateHandler.request_bodies[path] == body
    assert CandidateHandler.user_agents
    assert set(CandidateHandler.user_agents) == {"blocksize-hosted-smoke/1.0"}
    assert unknown_result.returncode != 0
    assert "refusing to request unknown x402 discovery resource" in unknown_result.stderr
    assert ("GET", "/v1/unknown-side-effect") not in CandidateHandler.calls
    assert ("POST", "/v1/unknown-side-effect") not in CandidateHandler.calls


def test_release_ci_builds_twice_and_smokes_the_installed_wheel() -> None:
    github = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    gitlab = (ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8")

    assert "dist-repro" in github
    assert "--reproducible-with" in github
    assert "verify_installed_release.py" in github
    assert "--requirements requirements.txt" in github
    assert "scripts/check_secret_hygiene.py" in github
    assert "pip-audit==2.10.1" in github

    assert (ROOT / ".python-version").read_text(encoding="utf-8").strip() == "3.12"
    assert 'python-version: "3.12"' in github
    assert "actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4" in github
    assert "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5" in github
    assert "cancel-in-progress: true" in github
    assert "timeout-minutes: 20" in github
    assert "railway up --" not in gitlab.lower()
    assert "npm install -g @railway/cli" not in gitlab.lower()


def test_gitlab_validation_job_cannot_checkout_or_deploy() -> None:
    pipeline = (ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8")

    assert "GIT_STRATEGY: none" in pipeline
    assert "image: alpine:3.20" in pipeline
    assert "checkout" not in pipeline.lower()
    assert "curl" not in pipeline.lower()
    assert "npm" not in pipeline.lower()
    assert "uv " not in pipeline.lower()


def test_installed_release_uses_only_exact_frozen_runtime_dependencies(
    tmp_path: Path,
) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        "# generated\nfastapi==1.2.3\ncolorama==0.4.6 ; sys_platform == 'win32'\n",
        encoding="utf-8",
    )

    frozen = verify_installed_release._frozen_runtime_requirements(requirements)

    assert "-e ." not in frozen
    assert "fastapi==1.2.3" in frozen
    assert "colorama==0.4.6 ; sys_platform == 'win32'" in frozen


def test_installed_release_rejects_unfrozen_or_editable_dependencies(
    tmp_path: Path,
) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("fastapi>=1.2.3\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="exact version pins"):
        verify_installed_release._frozen_runtime_requirements(requirements)

    requirements.write_text(
        "-e .\nfastapi==1.2.3\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="contain an editable"):
        verify_installed_release._frozen_runtime_requirements(requirements)


def test_installed_release_probe_environment_drops_host_python_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PYTHONPATH", "/borrowed/site-packages")
    monkeypatch.setenv("PYTHONHOME", "/borrowed/python")
    monkeypatch.setenv("VIRTUAL_ENV", "/borrowed/venv")

    environment = verify_installed_release._isolated_environment()

    assert "PYTHONPATH" not in environment
    assert "PYTHONHOME" not in environment
    assert "VIRTUAL_ENV" not in environment
    assert environment["PYTHONNOUSERSITE"] == "1"
    assert environment["PYTHONSAFEPATH"] == "1"


def test_release_artifact_rejects_any_unlisted_public_data_file(
    tmp_path: Path,
) -> None:
    wheel = tmp_path / "synthetic-release.whl"
    members = set(verify_release_artifact.REQUIRED_MEMBERS) | set(
        verify_release_artifact.ALLOWED_PUBLIC_DOC_FILES
    )
    with zipfile.ZipFile(wheel, "w") as archive:
        for member in sorted(members):
            if member == "share/blocksize-mcp/server.json":
                payload = json.dumps(
                    {"version": public_metadata.APP_VERSION, "description": "test"}
                )
            elif member == "share/blocksize-mcp/docs/developer_portal.html":
                payload = "<html><body>test</body></html>"
            else:
                payload = "test"
            archive.writestr(member, payload)
        archive.writestr(
            "synthetic.dist-info/METADATA",
            (f"Metadata-Version: 2.1\nName: synthetic\nVersion: {public_metadata.APP_VERSION}\n"),
        )
        archive.writestr(
            "share/blocksize-mcp/docs/evidence/internal-secrets.txt",
            "must never ship",
        )
        archive.writestr(
            "share/blocksize-mcp/internal-secrets.txt",
            "must never ship",
        )

    result = verify_release_artifact.verify(wheel)

    assert result["passed"] is False
    assert result["checks"]["only_public_docs_packaged"] is False
    assert result["unexpected_docs"] == [
        "share/blocksize-mcp/docs/evidence/internal-secrets.txt",
        "share/blocksize-mcp/internal-secrets.txt",
    ]


def test_rwa_runtime_data_manifest_and_release_guards_share_one_exact_allowlist() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    shared_data = project["tool"]["hatch"]["build"]["targets"]["wheel"]["shared-data"]
    expected_source_files = {
        f"reports/{filename}" for filename in runtime_data.REQUIRED_RWA_REPORT_FILENAMES
    }
    expected_installed_files = {
        f"share/blocksize-mcp/reports/{filename}"
        for filename in runtime_data.REQUIRED_RWA_REPORT_FILENAMES
    }

    assert {
        source for source in shared_data if source.startswith("reports/")
    } == expected_source_files
    assert {shared_data[source] for source in expected_source_files} == (expected_installed_files)
    assert verify_release_artifact.RWA_RUNTIME_DATA_FILES == expected_installed_files
    assert set(resource_server.READINESS_REQUIRED_RWA_REPORT_PATHS) == set(
        runtime_data.REQUIRED_RWA_REPORT_FILENAMES
    )
    assert tuple(runtime_data.RWA_REPORT_OVERRIDE_ENVS) == (
        runtime_data.REQUIRED_RWA_REPORT_FILENAMES
    )
    assert {
        filename: environment_name
        for filename, environment_name in runtime_data.RWA_REPORT_OVERRIDE_ENVS.items()
        if environment_name
    } == {
        "rwa_evm_pool_allowlist.json": "RWA_EVM_POOL_ALLOWLIST_PATH",
        "rwa_jupiter_route_allowlist.json": "RWA_JUPITER_ROUTE_ALLOWLIST_PATH",
        "rwa_rights_clearance.json": "RWA_RIGHTS_CLEARANCE_PATH",
        "rwa_solana_pool_allowlist.json": "RWA_SOLANA_POOL_ALLOWLIST_PATH",
        "rwa_solana_token_mints.json": "RWA_SOLANA_TOKEN_MINTS_PATH",
    }


def test_hosted_example_uses_production_policy_and_durable_distinct_state() -> None:
    example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "APP_ENV=production" in example
    assert "PUBLIC_BASE_URL=https://mcp.blocksize.info" in example
    assert "RELEASE_COMMIT_SHA=" in example
    assert "PORT=8080" in example
    assert "BLOCKSIZE_STREAM_CACHE_ENABLED=true" in example
    assert "OBSERVABILITY_DB_PATH=/data/usage_events.db" in example
    assert "ANTHROPIC_OAUTH_STORAGE_DIR=/data/anthropic_oauth" in example
    assert "CURSOR_OAUTH_STORAGE_DIR=/data/cursor_oauth" in example
    assert "OPENAI_OAUTH_STORAGE_DIR=/data/openai_oauth" in example


def test_railway_cutover_requires_staging_and_disables_auto_deploy_first() -> None:
    runbook = (ROOT / "docs" / "gtm" / "gitlab_railway_deploy.md").read_text(encoding="utf-8")

    prerequisite = runbook.index("## Hard pre-deploy prerequisites")
    disable_auto_deploy = runbook.index("Disable or pause Railway GitHub/repository auto-deploy")
    push_pipeline = runbook.index("Push the pipeline to GitLab")
    assert prerequisite < disable_auto_deploy < push_pipeline
    assert "GitLab must be the sole deploy" in runbook
    assert "distinct Railway staging environment and service" in runbook
    assert "including `anthropic-mcp-beta`" in runbook
    assert "healthcheckPath=/readyz" in runbook
    assert "only to the GitLab `staging` environment" in runbook
    assert "only to `production`" in runbook
    assert "all-environments (`*`) scope" in runbook
    assert "blocking manual `deploy_production` job" in runbook
    assert "Allowed to deploy" in runbook
    assert "currently active, successful production deployment id" in runbook
    assert "## Rollback procedure" in runbook
    assert "does **not** roll back volume contents" in runbook


def test_portal_does_not_request_missing_font_assets() -> None:
    portal = (ROOT / "docs" / "developer_portal.html").read_text(encoding="utf-8")

    assert "/fonts/" not in portal
    assert "@font-face" not in portal
