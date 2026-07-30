#!/usr/bin/env python3
"""Fail when high-confidence credential material is present in tracked files."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess


ROOT = Path(__file__).resolve().parents[1]
MAX_TEXT_FILE_BYTES = 5_000_000
BINARY_SUFFIXES = {
    ".avif",
    ".gif",
    ".ico",
    ".jpeg",
    ".jpg",
    ".pdf",
    ".png",
    ".pyc",
    ".svgz",
    ".webp",
    ".zip",
}
SECRET_PATTERNS = {
    "aws_access_key_id": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "github_token": re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9_]{30,}|github_pat_[A-Za-z0-9_]{30,})\b"
    ),
    "openai_api_key": re.compile(r"\bsk-proj-[A-Za-z0-9_-]{20,}\b"),
    "anthropic_api_key": re.compile(r"\bsk-ant-[A-Za-z0-9_-]{20,}\b"),
    "stripe_live_secret": re.compile(r"\bsk_live_[A-Za-z0-9]{16,}\b"),
    "google_api_key": re.compile(r"\bAIza[0-9A-Za-z_-]{30,}\b"),
    "slack_token": re.compile(r"\bxox[baprs]-[0-9A-Za-z-]{20,}\b"),
    "private_key_pem": re.compile(
        r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
    ),
}
_APPROVED_FIXTURE_PATH = Path("tests/test_rwa_safeguards.py")
_APPROVED_FIXTURE_MARKERS = (
    "must-not-be-stored",
    "sk-" "proj-abcdefghijklmnopqrstuvwx",
)


def find_secret_patterns(text: str) -> list[str]:
    """Return pattern names only; never echo possible secret values."""
    return [name for name, pattern in SECRET_PATTERNS.items() if pattern.search(text)]


def _tracked_paths(root: Path) -> list[Path]:
    result = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=root,
        capture_output=True,
        check=True,
    )
    return [root / item.decode("utf-8") for item in result.stdout.split(b"\0") if item]


def scan_tracked_files(root: Path = ROOT) -> list[dict[str, object]]:
    findings: list[dict[str, object]] = []
    for path in _tracked_paths(root):
        relative_path = path.relative_to(root)
        if path.suffix.lower() in BINARY_SUFFIXES or not path.is_file():
            continue
        if path.stat().st_size > MAX_TEXT_FILE_BYTES:
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except UnicodeDecodeError:
            continue
        for line_number, line in enumerate(lines, start=1):
            if relative_path == _APPROVED_FIXTURE_PATH and any(
                marker in line for marker in _APPROVED_FIXTURE_MARKERS
            ):
                continue
            for pattern_name in find_secret_patterns(line):
                findings.append(
                    {
                        "path": relative_path.as_posix(),
                        "line": line_number,
                        "pattern": pattern_name,
                    }
                )
    return findings


def main() -> None:
    findings = scan_tracked_files()
    if findings:
        for finding in findings:
            print(
                f"{finding['path']}:{finding['line']} "
                f"potential {finding['pattern']}"
            )
        raise SystemExit(1)
    print("Tracked-file secret hygiene check passed")


if __name__ == "__main__":
    main()
