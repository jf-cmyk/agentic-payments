"""Release packaging, readiness, and provenance safeguards."""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import threading
import tomllib
import zipfile

import pytest
from fastapi.testclient import TestClient
from packaging.requirements import Requirement
from packaging.version import Version

from src import public_metadata, resource_server
from src.config import settings
from src.security_config import PRIVACY_SALT_SETTINGS
from scripts import (
    check_secret_hygiene,
    verify_installed_release,
    verify_release_artifact,
)


ROOT = Path(__file__).resolve().parents[1]

SECURITY_VERSION_FLOORS = {
    "cryptography": Version("48.0.1"),
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

    matches = check_secret_hygiene.find_secret_patterns(
        f"OPENAI_API_KEY={secret_value}"
    )

    assert matches == ["openai_api_key"]
    assert secret_value not in repr(matches)


def test_tracked_candidate_files_pass_secret_hygiene_gate() -> None:
    assert check_secret_hygiene.scan_tracked_files(ROOT) == []


def test_release_version_and_registry_description_are_coherent() -> None:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    tracked_server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    smithery = json.loads(
        (ROOT / "docs" / "smithery_manifest.json").read_text(encoding="utf-8")
    )

    assert project["project"]["version"] == public_metadata.APP_VERSION
    assert tracked_server == public_metadata.build_server_json()
    assert tracked_server["version"] == public_metadata.APP_VERSION
    assert smithery["version"] == public_metadata.APP_VERSION
    assert 1 <= len(tracked_server["description"]) <= 100


def test_resource_server_import_is_independent_of_working_directory(tmp_path: Path) -> None:
    environment = os.environ.copy()
    environment["PYTHONPATH"] = str(ROOT)
    environment["OBSERVABILITY_ENABLED"] = "false"
    environment["BLOCKSIZE_API_KEY"] = "release-import-test"
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from src.resource_server import DOCS_DIR; "
                "assert (DOCS_DIR / 'developer_portal.html').is_file(); "
                "print(DOCS_DIR)"
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


def test_readiness_fails_closed_when_store_probe_is_stale() -> None:
    with TestClient(resource_server.app) as client:
        snapshot = resource_server.app.state.store_readiness_snapshots[
            "credit_ledger"
        ]
        snapshot["checked_at"] -= (
            resource_server._store_readiness_max_age_seconds() + 1
        )

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
        "configuration_fingerprint": (
            resource_server._blocksize_configuration_fingerprint(client)
        ),
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
        "configuration_fingerprint": (
            resource_server._facilitator_configuration_fingerprint()
        ),
    }

    current = resource_server._facilitator_support_readiness(snapshot)
    stale_snapshot = {
        **snapshot,
        "checked_at": (
            resource_server.time.time()
            - resource_server._facilitator_probe_max_age_seconds()
            - 1
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
    assert mismatched["public_url"]["reason"] == (
        "connector_public_url_must_match_PUBLIC_BASE_URL"
    )
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
                f"{connector}_MCP_PUBLIC_URL": (
                    f"https://mcp.blocksize.info/{slug}/mcp"
                ),
                f"{connector}_OAUTH_JWT_SIGNING_KEY": (
                    "jwt-key-0123456789abcdefghijklmnopqrstuvwxyz-ABCDEF"
                ),
                f"{connector}_OAUTH_STORAGE_ENCRYPTION_KEY": (
                    "storage-key-9876543210ABCDEFGHIJKLMNOPQRSTUVWXYZ-abcdef"
                ),
                f"{connector}_OAUTH_STORAGE_DIR": str(state_dir / f"{slug}-oauth"),
                f"{connector}_ENTITLEMENT_DB_PATH": str(
                    state_dir / f"{slug}-entitlements.db"
                ),
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
    railway = tomllib.loads((ROOT / "railway.toml").read_text(encoding="utf-8"))

    assert railway["build"] == {
        "builder": "RAILPACK",
        "railpackVersion": "0.35.0",
    }
    assert railway["deploy"]["healthcheckPath"] == "/readyz"
    assert railway["deploy"]["healthcheckTimeout"] >= 60
    assert railway["deploy"]["restartPolicyType"] == "ON_FAILURE"


def test_gitlab_requires_distinct_staging_and_exact_commit_smoke_promotion() -> None:
    pipeline = (ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8")
    staging_job = pipeline.split("deploy_staging:", 1)[1].split(
        "deploy_production:", 1
    )[0]
    production_job = pipeline.split("deploy_production:", 1)[1]

    assert "deploy_staging:" in pipeline
    assert "needs:\n    - deploy_staging" in pipeline
    assert 'test "$STAGING_BASE_URL" != "$PUBLIC_BASE_URL"' in pipeline
    assert 'test "$STAGING_RAILWAY_ENVIRONMENT" != "$RAILWAY_ENVIRONMENT"' in pipeline
    assert 'test "$STAGING_RAILWAY_SERVICE_NAME" != "$RAILWAY_SERVICE_NAME"' in pipeline
    assert "STAGING_RAILWAY_PROJECT_ID:$STAGING_RAILWAY_ENVIRONMENT:$STAGING_RAILWAY_SERVICE_NAME" in pipeline
    assert 'audit_hosted_release.mjs "$STAGING_BASE_URL" "$CI_COMMIT_SHA"' in pipeline
    assert 'audit_hosted_release.mjs "$PUBLIC_BASE_URL" "$CI_COMMIT_SHA"' in pipeline
    assert 'audit_hosted_release.mjs "${PUBLIC_BASE_URL:-' not in pipeline
    assert 'RAILWAY_TOKEN: "$STAGING_RAILWAY_TOKEN"' in pipeline
    assert 'RAILWAY_TOKEN: "$PRODUCTION_RAILWAY_TOKEN"' in pipeline
    assert "environment:\n    name: staging" in staging_job
    assert "environment:\n    name: production" in production_job
    assert "PRODUCTION_RAILWAY_TOKEN" not in staging_job
    assert "STAGING_RAILWAY_TOKEN" not in production_job
    assert pipeline.count("npm install -g @railway/cli@5.30.1") == 2
    assert "npm install -g @railway/cli\n" not in pipeline
    assert "when: manual" in production_job
    assert "allow_failure: false" in production_job
    assert "execute the recorded Railway rollback procedure" in production_job


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

    class CandidateHandler(BaseHTTPRequestHandler):
        calls: list[tuple[str, str]] = []
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
                        "authorization_servers": [
                            f"{self.base_url}/{connector}/mcp"
                        ],
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
                self._json(
                    200,
                    {
                        "version": 1,
                        "resources": [
                            f"{self.base_url}/v1/vwap/BTC-USD",
                            f"{self.base_url}/v1/briefs/market",
                        ],
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
            if path == "/v1/briefs/market":
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
    assert ("POST", "/v1/briefs/market") in CandidateHandler.calls


def test_release_ci_builds_twice_and_smokes_the_installed_wheel() -> None:
    github = (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    gitlab = (ROOT / ".gitlab-ci.yml").read_text(encoding="utf-8")

    for pipeline in (github, gitlab):
        assert "dist-repro" in pipeline
        assert "--reproducible-with" in pipeline
        assert "verify_installed_release.py" in pipeline
        assert "--requirements requirements.txt" in pipeline
        assert "scripts/check_secret_hygiene.py" in pipeline
        assert "pip-audit==2.10.1" in pipeline

    assert (ROOT / ".python-version").read_text(encoding="utf-8").strip() == "3.12"
    assert 'python-version: "3.12"' in github
    assert "image: python:3.12" in gitlab
    assert "actions/checkout@11d5960a326750d5838078e36cf38b85af677262 # v4" in github
    assert "actions/setup-python@a26af69be951a213d495a4c3e4e4022e16d87065 # v5" in github
    assert "cancel-in-progress: true" in github
    assert "timeout-minutes: 20" in github
    assert gitlab.count("timeout: 20m") == 3


def test_installed_release_uses_only_exact_frozen_runtime_dependencies(
    tmp_path: Path,
) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text(
        "# generated\n-e .\nfastapi==1.2.3\n"
        "colorama==0.4.6 ; sys_platform == 'win32'\n",
        encoding="utf-8",
    )

    frozen = verify_installed_release._frozen_runtime_requirements(requirements)

    assert "-e ." not in frozen
    assert "fastapi==1.2.3" in frozen
    assert "colorama==0.4.6 ; sys_platform == 'win32'" in frozen


def test_installed_release_rejects_unfrozen_or_foreign_editable_dependencies(
    tmp_path: Path,
) -> None:
    requirements = tmp_path / "requirements.txt"
    requirements.write_text("-e .\nfastapi>=1.2.3\n", encoding="utf-8")
    with pytest.raises(RuntimeError, match="exact version pins"):
        verify_installed_release._frozen_runtime_requirements(requirements)

    requirements.write_text(
        "-e .\n-e ../another-project\nfastapi==1.2.3\n",
        encoding="utf-8",
    )
    with pytest.raises(RuntimeError, match="unexpected editable"):
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
            (
                "Metadata-Version: 2.1\n"
                "Name: synthetic\n"
                f"Version: {public_metadata.APP_VERSION}\n"
            ),
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


def test_hosted_example_uses_production_policy_and_durable_distinct_state() -> None:
    example = (ROOT / ".env.example").read_text(encoding="utf-8")

    assert "APP_ENV=production" in example
    assert "BLOCKSIZE_STREAM_CACHE_ENABLED=true" in example
    assert "OBSERVABILITY_DB_PATH=/data/usage_events.db" in example
    assert "ANTHROPIC_OAUTH_STORAGE_DIR=/data/anthropic_oauth" in example
    assert "CURSOR_OAUTH_STORAGE_DIR=/data/cursor_oauth" in example
    assert "OPENAI_OAUTH_STORAGE_DIR=/data/openai_oauth" in example


def test_railway_cutover_requires_staging_and_disables_auto_deploy_first() -> None:
    runbook = (ROOT / "docs" / "gtm" / "gitlab_railway_deploy.md").read_text(
        encoding="utf-8"
    )

    prerequisite = runbook.index("## Hard pre-deploy prerequisites")
    disable_auto_deploy = runbook.index(
        "Disable or pause Railway GitHub/repository auto-deploy"
    )
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
