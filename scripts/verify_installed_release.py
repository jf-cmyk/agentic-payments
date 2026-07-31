#!/usr/bin/env python3
"""Install a release wheel and smoke it without any source-tree fallback."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_RUNTIME_REQUIREMENTS = ROOT / "requirements.txt"


PROBE = r"""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlparse

from fastapi.testclient import TestClient
import fastapi
import fastmcp
import httpx
import pydantic
import starlette
import src
from src import public_metadata
from src.runtime_data import (
    REQUIRED_RWA_REPORT_FILENAMES,
    RWA_REPORTS_DIR,
    effective_rwa_report_paths,
    inspect_daily_xyz_reconciliation,
    inspect_required_rwa_report,
)
from src.resource_server import (
    DOCS_DIR,
    INSTALLED_DISTRIBUTION,
    PACKAGED_DATA_ROOT,
    SERVER_JSON_PATHS,
    SOURCE_CHECKOUT,
    app,
)

install_root = Path(os.environ["EXPECTED_INSTALL_ROOT"]).resolve()
module_root = Path(os.environ["EXPECTED_MODULE_ROOT"]).resolve()
dependency_root = Path(os.environ["EXPECTED_DEPENDENCY_ROOT"]).resolve()
module_path = Path(src.__file__).resolve()
expected_data_root = (install_root / "share" / "blocksize-mcp").resolve()
assert module_path.is_relative_to(module_root), (module_path, module_root)
for dependency in (fastapi, fastmcp, httpx, pydantic, starlette):
    dependency_path = Path(dependency.__file__).resolve()
    assert dependency_path.is_relative_to(dependency_root), (
        dependency.__name__,
        dependency_path,
        dependency_root,
    )
expected_pythonpath = os.environ.get("EXPECTED_ALLOWED_PYTHONPATH", "")
assert os.environ.get("PYTHONPATH", "") == expected_pythonpath
assert INSTALLED_DISTRIBUTION is True
assert SOURCE_CHECKOUT is False
assert PACKAGED_DATA_ROOT.resolve() == expected_data_root
assert DOCS_DIR.resolve() == expected_data_root / "docs"
assert RWA_REPORTS_DIR.resolve() == expected_data_root / "reports"
assert SERVER_JSON_PATHS == (expected_data_root / "server.json",)
assert (DOCS_DIR / "developer_portal.html").is_file()
assert SERVER_JSON_PATHS[0].is_file()
assert {
    path.name for path in RWA_REPORTS_DIR.iterdir() if path.is_file()
} == set(REQUIRED_RWA_REPORT_FILENAMES)
effective_report_paths = effective_rwa_report_paths()
assert effective_report_paths == {
    filename: RWA_REPORTS_DIR / filename
    for filename in REQUIRED_RWA_REPORT_FILENAMES
}
reports = {
    filename: json.loads(path.read_text(encoding="utf-8"))
    for filename, path in effective_report_paths.items()
}
for filename, path in effective_report_paths.items():
    assert inspect_required_rwa_report(filename, path) == (), filename
assert inspect_daily_xyz_reconciliation(
    effective_report_paths["rwa_daily_feed_agent.json"],
    effective_report_paths["rwa_xyz_new_asset_monitor.json"],
    reports_dir=RWA_REPORTS_DIR,
) == ()

def nested_value(payload: dict, dotted_path: str):
    value = payload
    for part in dotted_path.split("."):
        assert isinstance(value, dict) and part in value
        value = value[part]
    return value

def assert_typed_rows(
    filename: str,
    rows_field: str,
    text_fields: tuple[str, ...],
    *,
    integer_fields: tuple[tuple[str, int | None, int | None], ...] = (),
    number_fields: tuple[tuple[str, float, bool], ...] = (),
    boolean_fields: tuple[str, ...] = (),
    allowed_text_values: tuple[tuple[str, tuple[str, ...]], ...] = (),
) -> None:
    rows = nested_value(reports[filename], rows_field)
    assert isinstance(rows, list)
    for row in rows:
        assert isinstance(row, dict)
        for text_field in text_fields:
            value = nested_value(row, text_field)
            assert isinstance(value, str) and value.strip()
        for integer_field, minimum, maximum in integer_fields:
            value = nested_value(row, integer_field)
            assert isinstance(value, int) and not isinstance(value, bool)
            assert minimum is None or value >= minimum
            assert maximum is None or value <= maximum
        for number_field, minimum, minimum_exclusive in number_fields:
            value = nested_value(row, number_field)
            assert isinstance(value, (int, float)) and not isinstance(value, bool)
            assert not isinstance(value, float) or math.isfinite(value)
            assert value > minimum if minimum_exclusive else value >= minimum
        for boolean_field in boolean_fields:
            assert isinstance(nested_value(row, boolean_field), bool)
        for text_field, allowed_values in allowed_text_values:
            value = nested_value(row, text_field)
            assert isinstance(value, str)
            assert value.strip().lower() in allowed_values

def assert_rows(filename: str, rows_field: str, count_field: str) -> None:
    payload = reports[filename]
    rows = payload[rows_field]
    count = payload["summary"][count_field]
    assert isinstance(rows, list) and rows
    assert count == len(rows) and count > 0

assert_rows(
    "hyperliquid_tradeable_feeds.json", "perp_markets", "perp_market_count"
)
assert_rows("hyperliquid_tradeable_feeds.json", "spot_pairs", "spot_pair_count")
assert_rows(
    "hyperliquid_tradeable_feeds.json", "coverage_rows", "coverage_row_count"
)
for rows_field in ("perp_markets", "spot_pairs", "coverage_rows"):
    assert_typed_rows(
        "hyperliquid_tradeable_feeds.json",
        rows_field,
        ("asset_id", "symbol", "venue", "source_type"),
    )
assert_rows("rwa_blocksize_state_discovery.json", "symbols", "target_count")
assert_typed_rows(
    "rwa_blocksize_state_discovery.json",
    "symbols",
    ("asset_id", "symbol", "state_symbol", "status"),
)
assert_rows("rwa_derivative_venue_discovery.json", "venues", "venue_count")
assert_rows(
    "rwa_derivative_venue_discovery.json", "market_rows", "market_row_count"
)
assert_rows(
    "rwa_derivative_venue_discovery.json", "coverage_rows", "coverage_row_count"
)
assert_rows("rwa_evm_pool_allowlist.json", "pools", "pool_count")
assert_typed_rows(
    "rwa_evm_pool_allowlist.json",
    "pools",
    ("allowlist_id", "symbol", "venue", "chain", "pool_id"),
    integer_fields=(("block_number", 1, None),),
)
assert_rows("rwa_jupiter_route_allowlist.json", "routes", "route_count")
assert_typed_rows(
    "rwa_jupiter_route_allowlist.json",
    "routes",
    ("allowlist_id", "symbol", "venue", "status"),
)
assert_rows("rwa_solana_pool_allowlist.json", "pools", "pool_count")
assert_typed_rows(
    "rwa_solana_pool_allowlist.json",
    "pools",
    ("allowlist_id", "symbol", "venue", "chain", "pool_id"),
    integer_fields=(("slot", 1, None),),
)
assert_rows("rwa_solana_token_mints.json", "tokens", "token_count")
assert_typed_rows(
    "rwa_solana_token_mints.json",
    "tokens",
    ("token_key", "mint", "status"),
    integer_fields=(("decimals", 0, 255),),
    allowed_text_values=(("status", ("resolved", "verified", "configured")),),
)
solana_token_report = reports["rwa_solana_token_mints.json"]
solana_token_rows = solana_token_report["tokens"]
solana_loader_statuses = {"resolved", "verified", "configured"}
solana_status_counts: dict[str, int] = {}
solana_loader_accepted_rows = 0
for row in solana_token_rows:
    status = row["status"].strip().lower()
    solana_status_counts[status] = solana_status_counts.get(status, 0) + 1
    if (
        isinstance(row["mint"], str)
        and row["mint"].strip()
        and isinstance(row["decimals"], int)
        and not isinstance(row["decimals"], bool)
        and 0 <= row["decimals"] <= 255
        and status in solana_loader_statuses
    ):
        solana_loader_accepted_rows += 1
assert solana_loader_accepted_rows == len(solana_token_rows)
assert solana_token_report["summary"]["by_status"] == solana_status_counts
solana_resolved_count = solana_token_report["summary"]["resolved"]
assert isinstance(solana_resolved_count, int) and not isinstance(
    solana_resolved_count, bool
)
assert solana_resolved_count == solana_status_counts.get("resolved", 0)
assert_rows("rwa_xyz_new_asset_monitor.json", "asset_rows", "asset_count")
assert_rows("rwa_xyz_new_asset_monitor.json", "token_rows", "token_count")
assert_rows(
    "rwa_xyz_new_asset_monitor.json", "coverage_rows", "coverage_row_count"
)
assert_typed_rows(
    "rwa_xyz_new_asset_monitor.json",
    "asset_rows",
    ("rwa_xyz_asset_id", "asset_id", "symbol", "asset_class"),
)
assert_typed_rows(
    "rwa_xyz_new_asset_monitor.json",
    "token_rows",
    ("token_row_id", "asset_id", "network", "address"),
)
assert_typed_rows(
    "rwa_xyz_new_asset_monitor.json",
    "coverage_rows",
    ("asset_id", "symbol", "venue", "source_type"),
)

for rows_field, required_fields in (
    ("venues", ("venue_id", "name", "discovery_status")),
    ("market_rows", ("asset_id", "symbol", "venue", "source_type")),
    ("coverage_rows", ("asset_id", "symbol", "venue", "source_type")),
):
    assert_typed_rows(
        "rwa_derivative_venue_discovery.json",
        rows_field,
        required_fields,
    )

daily_report = reports["rwa_daily_feed_agent.json"]
assert daily_report["status"] == {
    "acceptance": "passed",
    "decision_usable": True,
    "snapshot_reconciled": True,
}
for summary_field, snapshot_field in (
    ("current_asset_count", "asset_count"),
    ("current_token_count", "token_count"),
    ("current_coverage_row_count", "coverage_row_count"),
):
    assert daily_report["summary"][summary_field] > 0
    assert (
        daily_report["summary"][summary_field]
        == daily_report["source_snapshot"][snapshot_field]
    )
for rows_field, required_fields in (
    ("new_assets", ("asset_id", "symbol")),
    ("new_tokens", ("token_row_id", "asset_id", "network", "address")),
    ("removed_assets", ("asset_id", "symbol")),
    ("removed_tokens", ("token_row_id", "asset_id", "network", "address")),
    ("sourcing_actions", ("asset_id", "lane", "priority")),
):
    assert_typed_rows("rwa_daily_feed_agent.json", rows_field, required_fields)

paxg_result = reports["rwa_hyperliquid_paxg_probe.json"]["result"]
assert paxg_result["results"]
assert paxg_result["quality"]["observations"]
assert paxg_result["summary"]["jobs_selected"] == len(paxg_result["results"])
assert paxg_result["summary"]["jobs_succeeded"] > 0
assert paxg_result["summary"]["observations"] == len(
    paxg_result["quality"]["observations"]
)
assert_typed_rows(
    "rwa_hyperliquid_paxg_probe.json",
    "result.results",
    (
        "status",
        "job.job_id",
        "job.symbol",
        "job.venue",
        "bidask.symbol",
        "bidask.venue",
        "block_vwap.status",
    ),
    number_fields=(("block_vwap.block_size_usd", 0, True),),
)
assert_typed_rows(
    "rwa_hyperliquid_paxg_probe.json",
    "result.quality.observations",
    ("symbol", "venue", "status", "source_type"),
    number_fields=(("age_ms", 0, False),),
    boolean_fields=("usable_for_realtime",),
)

rights = reports["rwa_rights_clearance.json"]
assert rights["rights_cleared"] is True
assert rights["policy_acknowledgements"]
assert rights["scope"]["allowed_uses"]
assert rights["scope"]["registered_source_scope"]
for rows_field in (
    "scope.allowed_uses",
    "scope.registered_source_scope",
    "still_requires_technical_gates",
):
    rows = nested_value(rights, rows_field)
    assert all(isinstance(row, str) and row.strip() for row in rows)

def path_for(url: str) -> str:
    parsed = urlparse(url)
    assert parsed.netloc == "mcp.blocksize.info", url
    return parsed.path + (("?" + parsed.query) if parsed.query else "")

with TestClient(app, base_url="https://mcp.blocksize.info") as client:
    health_response = client.get("/health")
    assert health_response.status_code == 200
    health = health_response.json()
    assert health["version"] == public_metadata.APP_VERSION

    readiness = client.get("/readyz")
    assert readiness.status_code == 200, readiness.text
    assert readiness.json()["ready"] is True

    expected_snapshot = public_metadata.RWA_DISCOVERY_SNAPSHOT
    assets = client.get("/v1/rwa/assets?limit=1")
    assert assets.status_code == 200
    asset_summary = assets.json()["summary"]
    assert asset_summary["canonical_asset_count"] == expected_snapshot[
        "canonical_asset_rows"
    ]
    assert asset_summary["coverage_row_count"] == expected_snapshot[
        "venue_instrument_rows"
    ]
    assert asset_summary["decision_grade_canonical_asset_count"] == expected_snapshot[
        "decision_grade_canonical_asset_rows"
    ]

    registry = client.get("/v1/rwa/registry?limit=1")
    assert registry.status_code == 200
    registry_summary = registry.json()["summary"]
    assert registry_summary["canonical_asset_count"] == asset_summary[
        "canonical_asset_count"
    ]
    assert registry_summary["coverage_row_count"] == asset_summary[
        "coverage_row_count"
    ]

    venues = client.get("/v1/rwa/registry/venues?limit=1")
    assert venues.status_code == 200
    assert venues.json()["summary"]["venue_instrument_count"] == asset_summary[
        "coverage_row_count"
    ]

    xyz_monitor = client.get("/v1/rwa/rwa-xyz-monitor")
    assert xyz_monitor.status_code == 200
    xyz_summary = xyz_monitor.json()["summary"]
    assert xyz_summary["asset_count"] == expected_snapshot["rwa_xyz_source_asset_rows"]
    assert xyz_summary["token_count"] == expected_snapshot["rwa_xyz_token_listing_rows"]

    daily = client.get("/v1/rwa/daily-feed-agent")
    assert daily.status_code == 200
    daily_payload = daily.json()
    assert daily_payload["status"]["acceptance"] == "passed"
    assert daily_payload["status"]["decision_usable"] is True
    assert daily_payload["summary"]["current_asset_count"] == xyz_summary[
        "asset_count"
    ]
    assert daily_payload["summary"]["current_token_count"] == xyz_summary[
        "token_count"
    ]

    derivative_source = json.loads(
        (RWA_REPORTS_DIR / "rwa_derivative_venue_discovery.json").read_text(
            encoding="utf-8"
        )
    )
    derivative = client.get("/v1/rwa/derivative-venues?limit=1")
    assert derivative.status_code == 200
    derivative_summary = derivative.json()["summary"]
    assert derivative_summary["coverage_row_count"] > 0
    assert derivative_summary["coverage_row_count"] == derivative_source["summary"][
        "coverage_row_count"
    ]

    replay = client.get("/v1/rwa/replay-inventory")
    assert replay.status_code == 200
    replay_summary = replay.json()["summary"]
    assert {
        key: replay_summary[key]
        for key in (
            "candidate_count",
            "replay_ready",
            "raw_payload_available",
            "route_plan_available",
            "pool_state_available",
        )
    } == {
        "candidate_count": 60,
        "replay_ready": 24,
        "raw_payload_available": 25,
        "route_plan_available": 10,
        "pool_state_available": 14,
    }

    sitemap = client.get("/sitemap.xml")
    assert sitemap.status_code == 200
    sitemap_urls = re.findall(r"<loc>([^<]+)</loc>", sitemap.text)
    assert sitemap_urls
    for url in sitemap_urls:
        response = client.get(path_for(url), follow_redirects=False)
        assert response.status_code == 200, (url, response.status_code)

    protocol_links = {
        "remote_mcp",
        "anthropic_mcp",
        "cursor_mcp",
        "openai_mcp",
    }
    for name, url in health["links"].items():
        if name in protocol_links:
            continue
        response = client.get(path_for(url), follow_redirects=False)
        assert response.status_code == 200, (name, url, response.status_code)

    for connector in ("anthropic", "cursor", "openai"):
        response = client.get(
            f"/{connector}/mcp/",
            headers={"Accept": "application/json, text/event-stream"},
            follow_redirects=False,
        )
        assert 400 <= response.status_code < 500 and response.status_code != 404, (
            connector,
            response.status_code,
        )

        for kind in ("oauth-protected-resource", "oauth-authorization-server"):
            metadata = client.get(f"/.well-known/{kind}/{connector}/mcp")
            assert metadata.status_code == 200
            payload = metadata.json()
            assert payload["oauth_available"] is False
            assert payload.get("authorization_servers", []) == []
            for endpoint in (
                "authorization_endpoint",
                "token_endpoint",
                "registration_endpoint",
            ):
                assert endpoint not in payload

    initialize = client.post(
        "/mcp/server/",
        headers={
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
        },
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "installed-wheel-gate", "version": "1.0"},
            },
        },
    )
    assert initialize.status_code == 200, initialize.text
    data_line = next(
        line for line in initialize.text.splitlines() if line.startswith("data:")
    )
    initialize_payload = json.loads(data_line.split(":", 1)[1].strip())
    assert initialize_payload["result"]["serverInfo"]["version"] == public_metadata.APP_VERSION

    portal = client.get("/")
    internal_assets = {
        match
        for match in re.findall(r'(?:href|src)=["\']([^"\']+)', portal.text)
        if match.startswith(("/assets/", "/pdf/", "/evidence/"))
    }
    assert internal_assets
    for asset in sorted(internal_assets):
        response = client.get(asset, follow_redirects=False)
        assert response.status_code == 200, (asset, response.status_code)

print(json.dumps({
    "passed": True,
    "module": str(module_path),
    "dependency_root": str(dependency_root),
    "pythonpath": os.environ.get("PYTHONPATH") or None,
    "docs": str(DOCS_DIR),
    "rwa_reports": str(RWA_REPORTS_DIR),
    "rwa_report_count": len(REQUIRED_RWA_REPORT_FILENAMES),
    "rwa_assets": asset_summary["canonical_asset_count"],
    "rwa_venue_instruments": asset_summary["coverage_row_count"],
    "rwa_replay_ready": replay_summary["replay_ready"],
    "manifest": str(SERVER_JSON_PATHS[0]),
    "sitemap_urls": len(sitemap_urls),
    "internal_assets": len(internal_assets),
}, sort_keys=True))
"""


def _run(command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        **kwargs,
    )


def _frozen_runtime_requirements(source: Path) -> str:
    """Return an exact runtime lock export without its editable local project."""
    source = source.resolve(strict=True)
    output: list[str] = []
    runtime_requirements = 0
    removed_local_project = False
    for raw_line in source.read_text(encoding="utf-8").splitlines():
        stripped = raw_line.strip()
        if stripped in {"-e .", "--editable ."}:
            removed_local_project = True
            continue
        if stripped.startswith(("-e ", "--editable ")):
            raise RuntimeError(
                f"frozen runtime requirements contain an unexpected editable: {stripped}"
            )
        if stripped and not stripped.startswith("#"):
            requirement = stripped.split(";", 1)[0].strip()
            if not re.fullmatch(r"[A-Za-z0-9_.-]+(?:\[[^]]+\])?==[^\s]+", requirement):
                raise RuntimeError(
                    f"runtime requirements must contain only exact version pins; found {stripped!r}"
                )
            runtime_requirements += 1
        output.append(raw_line)

    if not removed_local_project:
        raise RuntimeError("frozen runtime requirements must contain the local '-e .' project")
    if not runtime_requirements:
        raise RuntimeError("frozen runtime requirements contain no pinned dependencies")
    return "\n".join(output) + "\n"


def _isolated_environment() -> dict[str, str]:
    environment = os.environ.copy()
    for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "UV_PROJECT_ENVIRONMENT"):
        environment.pop(name, None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONSAFEPATH"] = "1"
    return environment


def verify(
    wheel: Path,
    requirements: Path = DEFAULT_RUNTIME_REQUIREMENTS,
) -> dict[str, object]:
    wheel = wheel.resolve(strict=True)
    requirements = requirements.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="blocksize-installed-release-") as temp_raw:
        temp = Path(temp_raw)
        environment_dir = temp / "venv"
        work_dir = temp / "cwd"
        state_dir = temp / "state"
        target_dir = temp / "target"
        target_work_dir = temp / "target-cwd"
        target_state_dir = temp / "target-state"
        frozen_requirements = temp / "frozen-runtime-requirements.txt"
        work_dir.mkdir()
        state_dir.mkdir()
        target_dir.mkdir()
        target_work_dir.mkdir()
        target_state_dir.mkdir()
        for decoy_root in (work_dir, target_work_dir):
            decoy_reports = decoy_root / "reports"
            decoy_reports.mkdir()
            for filename in (
                "rwa_xyz_new_asset_monitor.json",
                "rwa_derivative_venue_discovery.json",
                "rwa_daily_feed_agent.json",
            ):
                (decoy_reports / filename).write_text("{}\n", encoding="utf-8")
        frozen_requirements.write_text(
            _frozen_runtime_requirements(requirements),
            encoding="utf-8",
        )

        isolated_environment = _isolated_environment()
        created = _run(
            [sys.executable, "-m", "venv", str(environment_dir)],
            env=isolated_environment,
            timeout=60,
        )
        if created.returncode:
            raise RuntimeError(created.stderr or created.stdout)
        python = environment_dir / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        pip = environment_dir / ("Scripts/pip.exe" if os.name == "nt" else "bin/pip")
        dependencies_installed = _run(
            [
                str(pip),
                "install",
                "--no-deps",
                "--requirement",
                str(frozen_requirements),
            ],
            env=isolated_environment,
            timeout=300,
        )
        if dependencies_installed.returncode:
            raise RuntimeError(dependencies_installed.stderr or dependencies_installed.stdout)
        installed = _run(
            [str(pip), "install", "--no-deps", str(wheel)],
            env=isolated_environment,
            timeout=60,
        )
        if installed.returncode:
            raise RuntimeError(installed.stderr or installed.stdout)
        dependency_check = _run(
            [str(pip), "check"],
            env=isolated_environment,
            timeout=60,
        )
        if dependency_check.returncode:
            raise RuntimeError(dependency_check.stderr or dependency_check.stdout)

        purelib_result = _run(
            [str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"],
            env=isolated_environment,
            timeout=30,
        )
        if purelib_result.returncode:
            raise RuntimeError(purelib_result.stderr or purelib_result.stdout)
        purelib = Path(purelib_result.stdout.strip())

        # These decoys reproduced the old fallback bug. A wheel must ignore
        # adjacent, unrelated site-packages data and use its installed share.
        (purelib / "docs").mkdir()
        (purelib / "server.json").write_text('{"version":"decoy"}', encoding="utf-8")
        (purelib / "pyproject.toml").write_text(
            '[project]\nname="decoy"\nversion="0"\n',
            encoding="utf-8",
        )

        environment = isolated_environment.copy()
        for name in list(environment):
            if name.startswith(
                (
                    "ANTHROPIC_",
                    "BLOCKSIZE_",
                    "CDP_",
                    "CURSOR_",
                    "OPENAI_",
                    "RAILWAY_",
                    "RWA_",
                    "X402_",
                )
            ) or name in {"APP_ENV", "BLOCKSIZE_DOCS_DIR", "RELEASE_COMMIT_SHA"}:
                environment.pop(name, None)
        environment.update(
            {
                "EXPECTED_INSTALL_ROOT": str(environment_dir),
                "EXPECTED_MODULE_ROOT": str(environment_dir),
                "EXPECTED_DEPENDENCY_ROOT": str(purelib),
                "EXPECTED_ALLOWED_PYTHONPATH": "",
                "BLOCKSIZE_API_KEY": "installed-release-probe",
                "OBSERVABILITY_ENABLED": "false",
                "DISCOVERY_RATE_LIMIT_ENABLED": "true",
                "CREDIT_DB_PATH": str(state_dir / "credits.db"),
                "RWA_OBSERVATION_DB_PATH": str(state_dir / "rwa.db"),
                "ANTHROPIC_ENTITLEMENT_DB_PATH": str(state_dir / "anthropic.db"),
                "CURSOR_ENTITLEMENT_DB_PATH": str(state_dir / "cursor.db"),
                "OPENAI_ENTITLEMENT_DB_PATH": str(state_dir / "openai.db"),
            }
        )
        probed = _run(
            [str(python), "-P", "-c", PROBE],
            cwd=work_dir,
            env=environment,
            timeout=120,
        )
        if probed.returncode:
            raise RuntimeError(
                "installed release probe failed\n"
                f"stdout:\n{probed.stdout}\n"
                f"stderr:\n{probed.stderr}"
            )
        prefix_result = json.loads(probed.stdout.strip().splitlines()[-1])
        prefix_result["installation_scheme"] = "prefix"

        target_installed = _run(
            [
                str(pip),
                "install",
                "--no-deps",
                "--target",
                str(target_dir),
                str(wheel),
            ],
            env=isolated_environment,
            timeout=60,
        )
        if target_installed.returncode:
            raise RuntimeError(target_installed.stderr or target_installed.stdout)

        # A target/user-style installation has its package and shared data under
        # a non-sysconfig root. Adjacent source-shaped decoys must not affect it.
        (target_dir / "docs").mkdir()
        (target_dir / "server.json").write_text(
            '{"version":"decoy"}',
            encoding="utf-8",
        )
        (target_dir / "pyproject.toml").write_text(
            '[project]\nname="decoy"\nversion="0"\n',
            encoding="utf-8",
        )
        target_environment = environment.copy()
        target_environment.update(
            {
                "PYTHONPATH": str(target_dir),
                "EXPECTED_INSTALL_ROOT": str(target_dir),
                "EXPECTED_MODULE_ROOT": str(target_dir),
                "EXPECTED_DEPENDENCY_ROOT": str(purelib),
                "EXPECTED_ALLOWED_PYTHONPATH": str(target_dir),
                "CREDIT_DB_PATH": str(target_state_dir / "credits.db"),
                "RWA_OBSERVATION_DB_PATH": str(target_state_dir / "rwa.db"),
                "ANTHROPIC_ENTITLEMENT_DB_PATH": str(target_state_dir / "anthropic.db"),
                "CURSOR_ENTITLEMENT_DB_PATH": str(target_state_dir / "cursor.db"),
                "OPENAI_ENTITLEMENT_DB_PATH": str(target_state_dir / "openai.db"),
            }
        )
        target_probed = _run(
            [str(python), "-P", "-c", PROBE],
            cwd=target_work_dir,
            env=target_environment,
            timeout=120,
        )
        if target_probed.returncode:
            raise RuntimeError(
                "target-installed release probe failed\n"
                f"stdout:\n{target_probed.stdout}\n"
                f"stderr:\n{target_probed.stderr}"
            )
        target_result = json.loads(target_probed.stdout.strip().splitlines()[-1])
        target_result["installation_scheme"] = "target"
        return {
            "passed": True,
            "wheel": str(wheel),
            "requirements": str(requirements),
            "dependency_check": "passed",
            "prefix_install": prefix_result,
            "target_install": target_result,
        }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    parser.add_argument(
        "--requirements",
        type=Path,
        default=DEFAULT_RUNTIME_REQUIREMENTS,
        help="Frozen uv runtime export; its editable local project entry is replaced by the wheel.",
    )
    args = parser.parse_args()
    result = verify(args.wheel, args.requirements)
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
