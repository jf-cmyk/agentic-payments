"""Public metadata must import in release tooling that has no server credentials."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_public_metadata_imports_without_blocksize_api_key(tmp_path):
    env = {
        key: value
        for key, value in os.environ.items()
        if key not in {"BLOCKSIZE_API_KEY", "FREE_TIER_MONTHLY_CREDITS"}
    }
    env["PYTHONPATH"] = str(ROOT)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from src import public_metadata as pm; "
            "print(pm.FREE_TIER_OFFER_LINE); print(pm.build_server_json()['description'])",
        ],
        cwd=tmp_path,  # no .env in reach, exactly like CI release steps
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    offer_line, description = result.stdout.strip().splitlines()[-2:]
    assert offer_line.startswith("15,000 free live-data credits every month")
    assert len(description) <= 100


def test_release_contract_check_runs_without_blocksize_api_key(tmp_path):
    env = {key: value for key, value in os.environ.items() if key != "BLOCKSIZE_API_KEY"}
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts/check_release_contracts.py"), "--expected-version", "0.6.22"],
        cwd=ROOT,
        env={**env, "PYTHONPATH": str(ROOT)},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-2000:]
