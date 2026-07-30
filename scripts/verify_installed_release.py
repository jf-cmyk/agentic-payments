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


PROBE = r'''
from __future__ import annotations

import json
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
assert SERVER_JSON_PATHS == (expected_data_root / "server.json",)
assert (DOCS_DIR / "developer_portal.html").is_file()
assert SERVER_JSON_PATHS[0].is_file()

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
    "manifest": str(SERVER_JSON_PATHS[0]),
    "sitemap_urls": len(sitemap_urls),
    "internal_assets": len(internal_assets),
}, sort_keys=True))
'''


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
                    "runtime requirements must contain only exact version pins; "
                    f"found {stripped!r}"
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
            raise RuntimeError(
                dependencies_installed.stderr or dependencies_installed.stdout
            )
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
                "ANTHROPIC_ENTITLEMENT_DB_PATH": str(
                    target_state_dir / "anthropic.db"
                ),
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
