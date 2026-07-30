#!/usr/bin/env python3
"""Verify that a built wheel carries the public product and coherent release identity."""

from __future__ import annotations

import argparse
from email.parser import BytesParser
import json
from pathlib import Path
import re
import zipfile


REQUIRED_MEMBERS = (
    "src/resource_server.py",
    "scripts/__init__.py",
    "scripts/run_rwa_growth_pilot.py",
    "share/blocksize-mcp/server.json",
    "share/blocksize-mcp/docs/developer_portal.html",
    "share/blocksize-mcp/docs/remote_mcp_quickstart.html",
    "share/blocksize-mcp/docs/first_price_quickstart.html",
    "share/blocksize-mcp/docs/prompt_examples.html",
    "share/blocksize-mcp/docs/privacy_policy.html",
    "share/blocksize-mcp/docs/support.html",
    "share/blocksize-mcp/docs/claude_connector.html",
    "share/blocksize-mcp/docs/assets/favicon.ico",
    "share/blocksize-mcp/docs/assets/favicon.png",
    "share/blocksize-mcp/docs/assets/architecture_diagram.png",
    "share/blocksize-mcp/docs/assets/logo-square.svg",
    "share/blocksize-mcp/docs/assets/swimlane_diagram.jpg",
    "share/blocksize-mcp/docs/pdf/Blocksize_Agent_Manual.pdf",
    "share/blocksize-mcp/docs/pdf/Blocksize_Data_Catalog.pdf",
    "share/blocksize-mcp/docs/pdf/Blocksize_Oracle_Lineage_Index.pdf",
    "share/blocksize-mcp/docs/pdf/Blocksize_Pricing_Guide.pdf",
    "share/blocksize-mcp/docs/pdf/Blocksize_RWA_Coverage_Index.pdf",
    "share/blocksize-mcp/docs/pdf/Blocksize_User_Flow.pdf",
    "share/blocksize-mcp/docs/evidence/rwa-coverage-index.html",
    "share/blocksize-mcp/docs/evidence/oracle-lineage-index.html",
)
PUBLIC_HTML_FILES = {
    "share/blocksize-mcp/docs/developer_portal.html",
    "share/blocksize-mcp/docs/remote_mcp_quickstart.html",
    "share/blocksize-mcp/docs/first_price_quickstart.html",
    "share/blocksize-mcp/docs/prompt_examples.html",
    "share/blocksize-mcp/docs/privacy_policy.html",
    "share/blocksize-mcp/docs/support.html",
    "share/blocksize-mcp/docs/claude_connector.html",
}
ALLOWED_PUBLIC_DOC_FILES = PUBLIC_HTML_FILES | {
    "share/blocksize-mcp/docs/assets/agent_demo.gif",
    "share/blocksize-mcp/docs/assets/architecture_diagram.png",
    "share/blocksize-mcp/docs/assets/favicon.ico",
    "share/blocksize-mcp/docs/assets/favicon.png",
    "share/blocksize-mcp/docs/assets/listings/awesome-mcp.svg",
    "share/blocksize-mcp/docs/assets/listings/github.png",
    "share/blocksize-mcp/docs/assets/listings/gitlab.png",
    "share/blocksize-mcp/docs/assets/listings/glama.svg",
    "share/blocksize-mcp/docs/assets/listings/mcp-registry.svg",
    "share/blocksize-mcp/docs/assets/listings/pay-sh.ico",
    "share/blocksize-mcp/docs/assets/listings/smithery.ico",
    "share/blocksize-mcp/docs/assets/listings/x402scan.png",
    "share/blocksize-mcp/docs/assets/logo-square.svg",
    "share/blocksize-mcp/docs/assets/logo.png",
    "share/blocksize-mcp/docs/assets/logo.svg",
    "share/blocksize-mcp/docs/assets/swimlane_diagram.jpg",
    "share/blocksize-mcp/docs/evidence/oracle-lineage-index.html",
    "share/blocksize-mcp/docs/evidence/rwa-coverage-index.html",
    "share/blocksize-mcp/docs/pdf/Blocksize_API_Documentation.pdf",
    "share/blocksize-mcp/docs/pdf/Blocksize_Agent_Manual.pdf",
    "share/blocksize-mcp/docs/pdf/Blocksize_Data_Catalog.pdf",
    "share/blocksize-mcp/docs/pdf/Blocksize_Oracle_Lineage_Index.pdf",
    "share/blocksize-mcp/docs/pdf/Blocksize_Pricing_Guide.pdf",
    "share/blocksize-mcp/docs/pdf/Blocksize_RWA_Coverage_Index.pdf",
    "share/blocksize-mcp/docs/pdf/Blocksize_State_Coverage.pdf",
    "share/blocksize-mcp/docs/pdf/Blocksize_User_Flow.pdf",
}
ALLOWED_PACKAGE_DATA_FILES = ALLOWED_PUBLIC_DOC_FILES | {
    "share/blocksize-mcp/server.json"
}


def verify(
    wheel_path: Path,
    *,
    reproducible_with: Path | None = None,
) -> dict[str, object]:
    with zipfile.ZipFile(wheel_path) as wheel:
        members = set(wheel.namelist())
        installed_names = {
            (name.split(".data/data/", 1)[1] if ".data/data/" in name else name): name
            for name in members
        }
        missing = sorted(
            {
                member
                for member in (*REQUIRED_MEMBERS, *ALLOWED_PUBLIC_DOC_FILES)
                if member not in installed_names
            }
        )
        packaged_data = {
            name
            for name in installed_names
            if name.startswith("share/blocksize-mcp/")
        }
        unexpected_docs = sorted(
            name
            for name in packaged_data
            if name not in ALLOWED_PACKAGE_DATA_FILES
        )
        metadata_name = next(
            (name for name in members if name.endswith(".dist-info/METADATA")),
            None,
        )
        if metadata_name is None:
            raise ValueError("wheel does not contain dist-info/METADATA")
        metadata = BytesParser().parsebytes(wheel.read(metadata_name))
        package_version = str(metadata.get("Version", ""))
        manifest = json.loads(wheel.read(installed_names["share/blocksize-mcp/server.json"]))
        portal = wheel.read(
            installed_names["share/blocksize-mcp/docs/developer_portal.html"]
        ).decode("utf-8")
        advertised_assets: set[str] = set()
        for public_doc in PUBLIC_HTML_FILES:
            archive_name = installed_names.get(public_doc)
            if archive_name is None:
                continue
            document = wheel.read(archive_name).decode("utf-8")
            for reference in re.findall(r'(?:href|src)=["\']([^"\']+)', document):
                clean_reference = reference.split("#", 1)[0].split("?", 1)[0]
                if clean_reference.startswith(("/assets/", "/pdf/", "/evidence/")):
                    advertised_assets.add(
                        f"share/blocksize-mcp/docs{clean_reference}"
                    )

    missing_advertised_assets = sorted(advertised_assets - set(installed_names))
    reproducible = (
        reproducible_with is None
        or wheel_path.read_bytes() == reproducible_with.read_bytes()
    )

    checks = {
        "required_members_present": not missing,
        "only_public_docs_packaged": not unexpected_docs,
        "manifest_version_matches_package": manifest.get("version") == package_version,
        "registry_description_valid": 1 <= len(str(manifest.get("description", ""))) <= 100,
        "portal_has_no_missing_font_requests": "/fonts/" not in portal,
        "advertised_static_assets_present": not missing_advertised_assets,
        "reproducible_build": reproducible,
    }
    return {
        "wheel": str(wheel_path),
        "version": package_version,
        "missing_members": missing,
        "unexpected_docs": unexpected_docs,
        "missing_advertised_assets": missing_advertised_assets,
        "reproducible_with": str(reproducible_with) if reproducible_with else None,
        "checks": checks,
        "passed": all(checks.values()),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--reproducible-with", type=Path)
    args = parser.parse_args()
    result = verify(args.wheel, reproducible_with=args.reproducible_with)
    print(json.dumps(result, indent=2, sort_keys=True))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
