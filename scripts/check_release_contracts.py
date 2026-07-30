#!/usr/bin/env python3
"""Check release identity and deployment contracts before building or deploying."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess
import tomllib

from src.public_metadata import APP_VERSION, build_server_json


ROOT = Path(__file__).resolve().parents[1]


def _git_is_clean() -> bool:
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0 and not result.stdout.strip()


def check(expected_version: str | None, require_clean: bool) -> dict[str, object]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    server = json.loads((ROOT / "server.json").read_text(encoding="utf-8"))
    smithery = json.loads(
        (ROOT / "docs" / "smithery_manifest.json").read_text(encoding="utf-8")
    )
    railway = tomllib.loads((ROOT / "railway.toml").read_text(encoding="utf-8"))
    versions = {
        "application": APP_VERSION,
        "project": project["project"]["version"],
        "server": server["version"],
        "smithery": smithery["version"],
    }
    clean = _git_is_clean()
    checks = {
        "versions_match": len(set(versions.values())) == 1,
        "expected_version_matches": expected_version is None or APP_VERSION == expected_version,
        "server_generated_from_canonical_metadata": server == build_server_json(),
        "registry_description_valid": 1 <= len(str(server.get("description", ""))) <= 100,
        "canonical_repository": server.get("repository")
        == {
            "url": "https://github.com/jf-cmyk/agentic-payments",
            "source": "github",
        },
        "railway_uses_readiness": railway.get("deploy", {}).get("healthcheckPath")
        == "/readyz",
        "clean_worktree_when_required": clean or not require_clean,
    }
    return {
        "version": APP_VERSION,
        "versions": versions,
        "git_clean": clean,
        "require_clean": require_clean,
        "checks": checks,
        "passed": all(checks.values()),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-version")
    parser.add_argument("--require-clean", action="store_true")
    args = parser.parse_args()
    result = check(args.expected_version, args.require_clean)
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
