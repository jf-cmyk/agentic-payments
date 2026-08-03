#!/usr/bin/env python3
"""Build reproducible, allowlisted Blocksize agent-skill plugin archives."""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path, PurePosixPath
import re
import tempfile
import zipfile


ROOT = Path(__file__).resolve().parents[1]
FIXED_ZIP_TIME = (1980, 1, 1, 0, 0, 0)
MAX_MEMBER_BYTES = 2_000_000
MAX_ARCHIVE_INPUT_BYTES = 10_000_000
TEXT_SUFFIXES = {".json", ".md", ".yaml", ".yml", ".txt"}
SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\bsk_(?:live|test)_[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bsk-proj-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bBearer\s+eyJ[A-Za-z0-9._-]{20,}\b", re.IGNORECASE),
)

SHARED_SKILL_MEMBERS = (
    "skills/use-blocksize-market-data/SKILL.md",
    "skills/use-blocksize-market-data/references/response-contract.md",
    "skills/use-blocksize-market-data/references/tool-surfaces.md",
)


@dataclass(frozen=True)
class PackageSpec:
    label: str
    source_root: Path
    archive_root: str
    filename: str
    members: tuple[str, ...]


def _manifest_version(path: Path) -> str:
    value = json.loads(path.read_text(encoding="utf-8")).get("version")
    if not isinstance(value, str) or not re.fullmatch(r"\d+\.\d+\.\d+", value):
        raise ValueError(f"invalid semantic version in {path}: {value!r}")
    return value


def package_specs() -> tuple[PackageSpec, ...]:
    openai_root = ROOT / "openai-plugin/blocksize-market-data"
    claude_root = ROOT / "claude-plugin/blocksize-market-data"
    cursor_root = ROOT / "blocksize-cursor-plugin/plugins/blocksize-market-data"
    skill_root = openai_root / "skills/use-blocksize-market-data"
    openai_version = _manifest_version(openai_root / ".codex-plugin/plugin.json")
    claude_version = _manifest_version(claude_root / ".claude-plugin/plugin.json")
    cursor_version = _manifest_version(cursor_root / ".cursor-plugin/plugin.json")

    return (
        PackageSpec(
            label="openai",
            source_root=openai_root,
            archive_root="blocksize-market-data",
            filename=f"blocksize-market-data-openai-plugin-{openai_version}.zip",
            members=(
                ".codex-plugin/plugin.json",
                ".mcp.json",
                "LICENSE",
                "README.md",
                *SHARED_SKILL_MEMBERS,
                "skills/use-blocksize-market-data/agents/openai.yaml",
            ),
        ),
        PackageSpec(
            label="claude",
            source_root=claude_root,
            archive_root="blocksize-market-data",
            filename=f"blocksize-market-data-claude-plugin-{claude_version}.zip",
            members=(
                ".claude-plugin/plugin.json",
                ".mcp.json",
                "LICENSE",
                "README.md",
                "SETUP.md",
                *SHARED_SKILL_MEMBERS,
            ),
        ),
        PackageSpec(
            label="cursor",
            source_root=cursor_root,
            archive_root="blocksize-market-data",
            filename=f"blocksize-market-data-cursor-plugin-{cursor_version}.zip",
            members=(
                ".cursor-plugin/plugin.json",
                "CHANGELOG.md",
                "LICENSE",
                "README.md",
                "assets/logo.png",
                "assets/logo.svg",
                "mcp.json",
                *SHARED_SKILL_MEMBERS,
            ),
        ),
        PackageSpec(
            label="universal_skill",
            source_root=skill_root,
            archive_root="use-blocksize-market-data",
            filename=(f"use-blocksize-market-data-universal-skill-{openai_version}.zip"),
            members=(
                "SKILL.md",
                "agents/openai.yaml",
                "references/response-contract.md",
                "references/tool-surfaces.md",
            ),
        ),
    )


def _validate_member(spec: PackageSpec, relative_name: str) -> tuple[Path, bytes]:
    relative = PurePosixPath(relative_name)
    if relative.is_absolute() or ".." in relative.parts or relative_name.startswith("/"):
        raise ValueError(f"unsafe package member: {relative_name}")
    if any(part in {".git", "__pycache__", ".DS_Store"} for part in relative.parts):
        raise ValueError(f"forbidden package member: {relative_name}")

    path = spec.source_root.joinpath(*relative.parts)
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"member must be a regular non-symlink file: {path}")
    payload = path.read_bytes()
    if len(payload) > MAX_MEMBER_BYTES:
        raise ValueError(f"package member exceeds size limit: {path}")

    if path.suffix.lower() in TEXT_SUFFIXES:
        text = payload.decode("utf-8")
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                raise ValueError(f"possible credential in {path}: {pattern.pattern}")
    if path.suffix.lower() == ".json":
        json.loads(payload)
    return path, payload


def _source_tree_digest(members: list[tuple[str, bytes]]) -> str:
    digest = hashlib.sha256()
    for name, payload in members:
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(payload)
        digest.update(b"\0")
    return digest.hexdigest()


def build_package(spec: PackageSpec, output_dir: Path) -> dict[str, object]:
    if len(set(spec.members)) != len(spec.members):
        raise ValueError(f"duplicate allowlisted members for {spec.label}")

    validated: list[tuple[str, bytes]] = []
    total_bytes = 0
    for relative_name in sorted(spec.members):
        _, payload = _validate_member(spec, relative_name)
        total_bytes += len(payload)
        validated.append((relative_name, payload))
    if total_bytes > MAX_ARCHIVE_INPUT_BYTES:
        raise ValueError(f"package input exceeds size limit: {spec.label}")

    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / spec.filename
    temporary_path = output_path.with_suffix(".zip.tmp")
    with zipfile.ZipFile(
        temporary_path,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for relative_name, payload in validated:
            archive_name = str(PurePosixPath(spec.archive_root) / relative_name)
            info = zipfile.ZipInfo(archive_name, date_time=FIXED_ZIP_TIME)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            info.flag_bits |= 0x800
            archive.writestr(info, payload, compress_type=zipfile.ZIP_DEFLATED, compresslevel=9)
    temporary_path.replace(output_path)

    archive_bytes = output_path.read_bytes()
    return {
        "label": spec.label,
        "filename": spec.filename,
        "sha256": hashlib.sha256(archive_bytes).hexdigest(),
        "size_bytes": len(archive_bytes),
        "source_tree_sha256": _source_tree_digest(validated),
        "members": len(validated),
    }


def build_all(output_dir: Path) -> dict[str, object]:
    packages = [build_package(spec, output_dir) for spec in package_specs()]
    release_version = _manifest_version(
        ROOT / "openai-plugin/blocksize-market-data/.codex-plugin/plugin.json"
    )
    manifest = {
        "schema_version": 1,
        "release": f"agent-skill-{release_version}",
        "signature": None,
        "signature_status": "unsigned-local-build",
        "packages": packages,
    }
    manifest_path = output_dir / f"agent-skill-release-{release_version}.json"
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return {"manifest": str(manifest_path), **manifest}


def verify_reproducible() -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="blocksize-agent-skill-a-") as first_dir:
        with tempfile.TemporaryDirectory(prefix="blocksize-agent-skill-b-") as second_dir:
            first = Path(first_dir)
            second = Path(second_dir)
            first_result = build_all(first)
            build_all(second)
            mismatches = [
                spec.filename
                for spec in package_specs()
                if first.joinpath(spec.filename).read_bytes()
                != second.joinpath(spec.filename).read_bytes()
            ]
    return {"passed": not mismatches, "mismatches": mismatches, **first_result}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, default=ROOT / "deliverables")
    parser.add_argument("--verify-reproducible", action="store_true")
    args = parser.parse_args()
    result = (
        verify_reproducible() if args.verify_reproducible else build_all(args.output_dir.resolve())
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    if args.verify_reproducible and not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
