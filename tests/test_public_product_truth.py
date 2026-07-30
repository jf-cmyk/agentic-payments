"""Regression checks for truthful public payment and credit positioning."""

from __future__ import annotations

import json
from pathlib import Path
import re
import tomllib
from zipfile import ZipFile

from src import public_metadata, resource_server


ROOT = Path(__file__).resolve().parents[1]

PUBLIC_COPY_FILES = (
    Path("README.md"),
    Path("server.json"),
    Path("src/public_metadata.py"),
    Path("src/mcp_server.py"),
    Path("src/public_mcp_server.py"),
    Path("src/anthropic_mcp_server.py"),
    Path("src/cursor_mcp_server.py"),
    Path("src/openai_mcp_server.py"),
    Path("scripts/generate_blocksize_pdfs.py"),
    Path("docs/README_EXTERNAL.md"),
    Path("docs/api_agent_manual.md"),
    Path("docs/blocksize_agent_skill.md"),
    Path("docs/claude_connector.html"),
    Path("docs/developer_portal.html"),
    Path("docs/first_price_quickstart.html"),
    Path("docs/first_price_quickstart.md"),
    Path("docs/prompt_examples.html"),
    Path("docs/remote_mcp_quickstart.html"),
    Path("docs/smithery_manifest.json"),
    Path("docs/support.html"),
    Path("docs/gtm/claude_connector_submission.md"),
    Path("docs/gtm/claude_plugin_submission/README.md"),
    Path("docs/gtm/pay_skills_submission/README.md"),
    Path("docs/gtm/pay_skills_submission/providers/blocksize/market-data/PAY.md"),
    Path("docs/gtm/pay_skills_submission/providers/blocksize/market-data/openapi.json"),
    Path("pay-skills/providers/blocksize/market-data/PAY.md"),
    Path("pay-skills/providers/blocksize/market-data/openapi.json"),
)

PUBLIC_COPY_TREES = (
    Path("openai-plugin/blocksize-market-data"),
    Path("claude-plugin/blocksize-market-data"),
    Path("blocksize-cursor-plugin/plugins/blocksize-market-data"),
)

FORBIDDEN_PRODUCTION_CLAIMS = (
    re.compile(r"\bprepaid credits?\b", re.IGNORECASE),
    re.compile(r"\bprepaid credit top[- ]ups?\b", re.IGNORECASE),
    re.compile(r"\bwallet[- ]credits?\b", re.IGNORECASE),
    re.compile(r"\bbulk credit tiers?\b", re.IGNORECASE),
    re.compile(r"\bcredit top[- ]ups?\b", re.IGNORECASE),
    re.compile(r"\bself-serve credit purchases?\b", re.IGNORECASE),
    re.compile(r"\b(?:starter pouch|growth pack|institutional vault)\b", re.IGNORECASE),
    re.compile(r"\b(?:1,000|10,000|100,000) credits\b", re.IGNORECASE),
    re.compile(r"\$(?:0\.90|8(?:\.00)?|60(?:\.00)?)\b", re.IGNORECASE),
)


def _public_copy_paths() -> list[Path]:
    paths = {ROOT / relative_path for relative_path in PUBLIC_COPY_FILES}
    for relative_root in PUBLIC_COPY_TREES:
        for path in (ROOT / relative_root).rglob("*"):
            if path.suffix.lower() in {".json", ".md", ".yaml", ".yml"}:
                paths.add(path)
    return sorted(paths)


def _false_claims(text: str) -> list[str]:
    return [pattern.pattern for pattern in FORBIDDEN_PRODUCTION_CLAIMS if pattern.search(text)]


def _assert_complete_access_model(text: str) -> None:
    normalized = " ".join(text.lower().replace("-", " ").split())
    assert "signed x402" in normalized
    assert "direct public http" in normalized or text == public_metadata.PUBLIC_REGISTRY_DESCRIPTION
    assert "starter credit" in normalized or "starter allowance" in normalized
    assert "authenticated connector" in normalized
    assert re.search(
        r"(?:only.{0,45}authenticated connector|authenticated connector.{0,45}only)",
        normalized,
    )
    assert (
        "contact sales" in normalized
        or "contact blocksize sales" in normalized
        or "contacting blocksize sales" in normalized
    )
    assert "authenticated account plan" in normalized


def test_public_copy_does_not_advertise_hidden_self_serve_credit_products() -> None:
    failures: dict[str, list[str]] = {}
    for path in _public_copy_paths():
        assert path.is_file(), path
        matches = _false_claims(path.read_text(encoding="utf-8"))
        if matches:
            failures[str(path.relative_to(ROOT))] = matches

    runtime_surfaces = {
        "server.json": json.dumps(public_metadata.build_server_json()),
        "llms.txt": public_metadata.build_llms_txt(),
        "data-packages.json": json.dumps(public_metadata.build_data_packages_json()),
        "category-hubs.json": json.dumps(public_metadata.build_category_hubs_json()),
        "seo-pages": "\n".join(
            public_metadata.build_seo_landing_page(slug)
            for slug in public_metadata.SEO_LANDING_PAGES
        ),
        "fastapi-description": resource_server.app.description or "",
        "openapi.json": json.dumps(resource_server.app.openapi()),
    }
    for label, text in runtime_surfaces.items():
        matches = _false_claims(text)
        if matches:
            failures[f"runtime:{label}"] = matches

    assert failures == {}


def test_legacy_credit_routes_are_not_in_the_production_openapi_contract() -> None:
    paths = resource_server.app.openapi()["paths"]

    assert "/v1/credits/purchase" not in paths
    assert "/v1/credits/claim" not in paths
    assert not any(path.startswith("/v1/credits/balance/") for path in paths)


def test_machine_readable_access_surfaces_state_the_complete_boundary() -> None:
    smithery = json.loads(
        (ROOT / "docs/smithery_manifest.json").read_text(encoding="utf-8")
    )
    smithery_access = json.dumps(
        {
            "description": smithery["description"],
            "capabilities": smithery["capabilities"],
        }
    )
    project_description = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )["project"]["description"]
    surfaces = {
        "Python project metadata": project_description,
        "registry description": public_metadata.build_server_json()["description"],
        "public description": public_metadata.PUBLIC_DESCRIPTION,
        "smithery": smithery_access,
        "llms routing": public_metadata.build_llms_txt(),
        "data package routing": json.dumps(
            public_metadata.build_data_packages_json()["routing"]
        ),
        "pricing document metadata": json.dumps(
            public_metadata.STATIC_DOCUMENTS["pricing"]
        ),
        "manual document metadata": json.dumps(public_metadata.STATIC_DOCUMENTS["manual"]),
        "SEO landing page": public_metadata.build_seo_landing_page(
            "x402-market-data-api"
        ),
    }

    for label, text in surfaces.items():
        try:
            _assert_complete_access_model(str(text))
        except AssertionError as exc:
            raise AssertionError(label) from exc

    assert "Credit Drawdown" not in smithery_access


def test_superseded_gtm_credit_hypotheses_cannot_look_like_current_guidance() -> None:
    historical_claim_files = (
        ROOT / "docs/gtm/agent_native_premium_products_plan.md",
        ROOT / "docs/gtm/icp_offer_memo.md",
        ROOT / "docs/gtm/product_readiness_scorecard.md",
    )

    for path in historical_claim_files:
        header = "\n".join(path.read_text(encoding="utf-8").splitlines()[:12])
        assert "Superseded" in header, path
        assert "do not use" in header.lower(), path
        access_notice = "\n".join(
            line.removeprefix("> ") for line in header.splitlines()
        )
        _assert_complete_access_model(access_notice)


def test_pay_skill_sidecars_are_direct_x402_only() -> None:
    sidecars = (
        ROOT / "pay-skills/providers/blocksize/market-data/openapi.json",
        ROOT
        / "docs/gtm/pay_skills_submission/providers/blocksize/market-data/openapi.json",
    )

    for path in sidecars:
        document = json.loads(path.read_text(encoding="utf-8"))
        serialized = json.dumps(document)
        assert "starterCredits" not in serialized
        assert "/v1/credits/purchase" not in document["paths"]
        assert "/v1/credits/claim" not in document["paths"]


def test_latest_universal_skill_archive_matches_truthful_source() -> None:
    archive_path = (
        ROOT / "deliverables/use-blocksize-market-data-universal-skill-0.4.0.zip"
    )
    source_root = (
        ROOT
        / "openai-plugin/blocksize-market-data/skills/use-blocksize-market-data"
    )
    relative_files = (
        Path("SKILL.md"),
        Path("references/tool-surfaces.md"),
        Path("references/response-contract.md"),
    )

    with ZipFile(archive_path) as archive:
        for relative_file in relative_files:
            archived = archive.read(
                str(Path("use-blocksize-market-data") / relative_file)
            ).decode("utf-8")
            expected = (source_root / relative_file).read_text(encoding="utf-8")
            assert archived == expected
            assert _false_claims(archived) == []


def test_latest_plugin_archives_do_not_reintroduce_false_credit_claims() -> None:
    archives = (
        ROOT / "deliverables/blocksize-market-data-claude-plugin-0.3.0.zip",
        ROOT / "deliverables/blocksize-market-data-cursor-plugin-1.3.0.zip",
        ROOT / "deliverables/blocksize-market-data-openai-plugin-0.4.0.zip",
    )

    for archive_path in archives:
        with ZipFile(archive_path) as archive:
            failures: dict[str, list[str]] = {}
            for name in archive.namelist():
                if Path(name).suffix.lower() not in {".json", ".md", ".yaml", ".yml"}:
                    continue
                matches = _false_claims(archive.read(name).decode("utf-8"))
                if matches:
                    failures[name] = matches
            assert failures == {}, archive_path


def test_explicit_historical_and_local_qa_text_remains_allowed() -> None:
    historical = (ROOT / "docs/gtm/agent_native_premium_products_plan.md").read_text(
        encoding="utf-8"
    )
    local_qa = (ROOT / "src/resource_server.py").read_text(encoding="utf-8")

    assert "Historical planning proposal" in historical
    assert "prepaid credit top-ups" in historical
    assert "Local-QA-only legacy challenge for unverified wallet credits" in local_qa
