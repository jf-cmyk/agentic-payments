from __future__ import annotations

import asyncio
import json
from pathlib import Path, PurePosixPath
import re
from zipfile import ZipFile

import yaml

from scripts import build_agent_skill_packages
from src.public_mcp_server import public_mcp


ROOT = Path(__file__).resolve().parents[1]
OPENAI_ROOT = ROOT / "openai-plugin/blocksize-market-data"
CLAUDE_ROOT = ROOT / "claude-plugin/blocksize-market-data"
CURSOR_ROOT = ROOT / "blocksize-cursor-plugin/plugins/blocksize-market-data"
OPENAI_SKILL = OPENAI_ROOT / "skills/use-blocksize-market-data"
CLAUDE_SKILL = CLAUDE_ROOT / "skills/use-blocksize-market-data"
CURSOR_SKILL = CURSOR_ROOT / "skills/use-blocksize-market-data"
SHARED_SKILL_FILES = (
    Path("SKILL.md"),
    Path("references/tool-surfaces.md"),
    Path("references/response-contract.md"),
)
AUTHENTICATED_TOOLS = {
    "search_pairs",
    "list_instruments",
    "get_credit_balance",
    "get_vwap",
    "get_bid_ask",
    "get_fx_rate",
    "get_metal_price",
}
PUBLIC_DISCOVERY_TOOLS = (
    "search_pairs",
    "list_instruments",
    "get_pricing_info",
    "get_product_catalog",
    "get_workflow_endpoint",
    "get_market_data_endpoint",
    "search",
    "fetch",
)
CANONICAL_REPOSITORY = "https://github.com/jf-cmyk/agentic-payments"
STALE_GITLAB_REPOSITORY = "https://gitlab.com/jfocke/agentic-payments"


def _json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _frontmatter(path: Path) -> dict:
    text = path.read_text(encoding="utf-8")
    match = re.match(r"\A---\n(.*?)\n---\n", text, flags=re.DOTALL)
    assert match is not None
    return yaml.safe_load(match.group(1))


def _backtick_inventory(path: Path, start: str, end: str) -> list[str]:
    text = path.read_text(encoding="utf-8")
    block = text.split(start, 1)[1].split(end, 1)[0]
    return re.findall(r"`([a-z][a-z0-9_]*)`", block)


def _html_code_inventory(path: Path, element_id: str) -> list[str]:
    text = path.read_text(encoding="utf-8")
    match = re.search(
        rf'<ul id="{re.escape(element_id)}">(.*?)</ul>',
        text,
        flags=re.DOTALL,
    )
    assert match is not None
    return re.findall(r"<code>([a-z][a-z0-9_]*)</code>", match.group(1))


def test_agent_skill_content_is_synchronized_across_hosts() -> None:
    for relative_file in SHARED_SKILL_FILES:
        expected = (OPENAI_SKILL / relative_file).read_bytes()
        assert (CLAUDE_SKILL / relative_file).read_bytes() == expected
        assert (CURSOR_SKILL / relative_file).read_bytes() == expected


def test_public_tool_inventories_match_fastmcp_contract() -> None:
    live_tools = [tool.name for tool in asyncio.run(public_mcp.list_tools())]

    assert live_tools == list(PUBLIC_DISCOVERY_TOOLS)
    inventories = {
        "README.md": _backtick_inventory(
            ROOT / "README.md",
            "It exposes only read-only discovery tools:",
            "This public surface does not execute paid live market data calls directly.",
        ),
        "docs/README_EXTERNAL.md": _backtick_inventory(
            ROOT / "docs/README_EXTERNAL.md",
            "The public MCP server is read-only and exposes:",
            "## Paid HTTP API",
        ),
        "docs/remote_mcp_quickstart.html": _html_code_inventory(
            ROOT / "docs/remote_mcp_quickstart.html",
            "public-mcp-tool-inventory",
        ),
    }
    for skill_root in (OPENAI_SKILL, CLAUDE_SKILL, CURSOR_SKILL):
        inventories[str(skill_root.relative_to(ROOT) / "SKILL.md")] = _backtick_inventory(
            skill_root / "SKILL.md",
            "- If only ",
            " are available, use the discovery workflow.",
        )
        inventories[
            str(skill_root.relative_to(ROOT) / "references/tool-surfaces.md")
        ] = _backtick_inventory(
            skill_root / "references/tool-surfaces.md",
            "The public server exposes exactly eight read-only tools:",
            "They cover catalog search",
        )

    assert inventories == {label: live_tools for label in inventories}


def test_skill_contract_is_bounded_and_fail_closed() -> None:
    skill_text = OPENAI_SKILL.joinpath("SKILL.md").read_text(encoding="utf-8")
    response_contract = OPENAI_SKILL.joinpath("references/response-contract.md").read_text(
        encoding="utf-8"
    )

    assert _frontmatter(OPENAI_SKILL / "SKILL.md").keys() == {
        "name",
        "description",
    }
    assert len(skill_text.splitlines()) < 500
    assert "TODO" not in skill_text
    for marker in (
        "untrusted data",
        "future-dated",
        "Before more than 10",
        "CREDIT_FINALIZATION_FAILED",
        "Never expose credentials",
        "Omit direct account identifiers",
        "only when that tool is available",
    ):
        assert marker in skill_text
    for status in (
        "live_observation",
        "catalog_metadata",
        "integration_route",
        "invalid_future",
    ):
        assert status in response_contract


def test_openai_plugin_uses_distinct_live_and_public_mcp_identities() -> None:
    manifest = _json(OPENAI_ROOT / ".codex-plugin/plugin.json")
    bundled_mcp = _json(OPENAI_ROOT / ".mcp.json")["mcpServers"]
    skill_metadata = yaml.safe_load(
        (OPENAI_SKILL / "agents/openai.yaml").read_text(encoding="utf-8")
    )
    dependency = skill_metadata["dependencies"]["tools"][0]

    assert manifest["version"] == "0.4.0"
    assert manifest["skills"] == "./skills/"
    assert manifest["mcpServers"] == "./.mcp.json"
    assert bundled_mcp == {
        "blocksize-market-data": {
            "type": "http",
            "url": "https://mcp.blocksize.info/openai/mcp/",
        }
    }
    assert dependency["value"] == "blocksize-market-data-public"
    assert dependency["url"] == "https://mcp.blocksize.info/mcp/server/"
    assert dependency["value"] not in bundled_mcp
    assert "verified" not in skill_metadata["interface"]["default_prompt"].lower()


def test_openai_repo_marketplace_resolves_to_the_plugin() -> None:
    marketplace = _json(ROOT / ".agents/plugins/marketplace.json")
    entry = marketplace["plugins"][0]

    assert marketplace["name"] == "blocksize-plugins"
    assert entry["name"] == "blocksize-market-data"
    assert entry["source"] == {
        "source": "local",
        "path": "./openai-plugin/blocksize-market-data",
    }
    assert entry["policy"] == {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL",
    }
    assert ROOT.joinpath(entry["source"]["path"]).resolve() == OPENAI_ROOT.resolve()


def test_claude_plugin_mcp_and_repo_marketplace_are_installable() -> None:
    manifest = _json(CLAUDE_ROOT / ".claude-plugin/plugin.json")
    mcp = _json(CLAUDE_ROOT / ".mcp.json")
    marketplace = _json(ROOT / ".claude-plugin/marketplace.json")
    submission_packet = (
        ROOT / "docs/gtm/claude_plugin_submission/README.md"
    ).read_text(encoding="utf-8")

    assert manifest["version"] == "0.3.0"
    assert "defaultEnabled" not in manifest
    assert mcp == {
        "mcpServers": {
            "blocksize-market-data": {
                "type": "http",
                "url": "https://mcp.blocksize.info/anthropic/mcp/",
            }
        }
    }
    assert marketplace["name"] == "blocksize-plugins"
    assert marketplace["plugins"] == [
        {
            "name": "blocksize-market-data",
            "source": "./claude-plugin/blocksize-market-data",
        }
    ]
    assert ROOT.joinpath(marketplace["plugins"][0]["source"]).resolve() == (CLAUDE_ROOT.resolve())
    archive_versions = re.findall(
        r"deliverables/blocksize-market-data-claude-plugin-(\d+\.\d+\.\d+)\.zip",
        submission_packet,
    )
    assert archive_versions == [manifest["version"], manifest["version"]]
    assert ROOT.joinpath(
        f"deliverables/blocksize-market-data-claude-plugin-{manifest['version']}.zip"
    ).is_file()


def test_plugin_install_guidance_does_not_claim_stale_remote_availability() -> None:
    openai_readme = (OPENAI_ROOT / "README.md").read_text(encoding="utf-8")
    claude_setup = (CLAUDE_ROOT / "SETUP.md").read_text(encoding="utf-8")
    submission_packet = (
        ROOT / "docs/gtm/claude_plugin_submission/README.md"
    ).read_text(encoding="utf-8")

    for text in (openai_readme, claude_setup, submission_packet):
        assert STALE_GITLAB_REPOSITORY not in text
        assert CANONICAL_REPOSITORY in text

    assert "codex plugin marketplace add /absolute/path/to/agentic-payments" in openai_readme
    assert "blocksize-market-data-openai-plugin-0.4.0.zip" in openai_readme
    assert "Future Remote Install Gate" in openai_readme
    assert "claude --plugin-dir /absolute/path/to/claude-plugin/blocksize-market-data" in (
        claude_setup
    )
    assert "blocksize-market-data-claude-plugin-0.3.0.zip" in claude_setup
    assert "Future remote install gate" in claude_setup

    assert _json(OPENAI_ROOT / ".codex-plugin/plugin.json")["repository"] == (
        CANONICAL_REPOSITORY
    )
    assert _json(CLAUDE_ROOT / ".claude-plugin/plugin.json")["repository"] == (
        CANONICAL_REPOSITORY
    )


def test_cursor_manifests_and_scoped_oauth_docs_are_consistent() -> None:
    manifest = _json(CURSOR_ROOT / ".cursor-plugin/plugin.json")
    marketplace = _json(ROOT / "blocksize-cursor-plugin/.cursor-plugin/marketplace.json")
    mcp = _json(CURSOR_ROOT / "mcp.json")
    readme = (CURSOR_ROOT / "README.md").read_text(encoding="utf-8")

    assert "$schema" not in manifest
    assert manifest["version"] == "1.3.0"
    assert marketplace["metadata"]["version"] == manifest["version"]
    assert marketplace["plugins"][0]["source"] == "./plugins/blocksize-market-data"
    assert mcp == {
        "mcpServers": {"blocksize-market-data": {"url": "https://mcp.blocksize.info/cursor/mcp/"}}
    }
    scoped_authorization_server = (
        "https://mcp.blocksize.info/.well-known/"
        "oauth-authorization-server/cursor/mcp"
    )
    assert readme.count(scoped_authorization_server) >= 2
    assert re.search(
        r"https://mcp\.blocksize\.info/\.well-known/"
        r"oauth-authorization-server(?!/)",
        readme,
    ) is None
    assert "rm -rf ~/.mcp-auth" not in readme
    assert "mcp-remote@latest" not in readme
    assert "--debug" not in readme
    assert "installs only the MCP server" in readme


def test_provider_docs_match_the_authenticated_tool_inventory() -> None:
    reference = OPENAI_SKILL.joinpath("references/tool-surfaces.md").read_text(encoding="utf-8")
    authenticated_section = reference.split(
        "## Authenticated provider connectors",
        1,
    )[1].split("## Public discovery fallback", 1)[0]
    documented = {
        match.group(1)
        for match in re.finditer(
            r"^- `([^`]+)`$",
            authenticated_section,
            flags=re.MULTILINE,
        )
    }

    assert documented == AUTHENTICATED_TOOLS
    assert "seven read-only tools" in reference
    assert "do not expose route builders" in reference
    assert "signed-in call" in reference


def test_connector_privacy_and_publisher_disclosures_are_present() -> None:
    privacy = (ROOT / "docs/privacy_policy.html").read_text(encoding="utf-8")
    cursor_readme = (CURSOR_ROOT / "README.md").read_text(encoding="utf-8")

    assert "authenticated OpenAI, Claude, and Cursor MCP connectors" in privacy
    assert "does not use MCP connector request content" in privacy
    for url in (
        "https://mcp.blocksize.info/privacy",
        "https://mcp.blocksize.info/terms",
        "https://mcp.blocksize.info/support",
    ):
        assert url in cursor_readme
    assert "does not use Cursor Plugin Data or User Content to train" in cursor_readme


def test_package_builder_is_reproducible_and_allowlisted(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first_result = build_agent_skill_packages.build_all(first)
    build_agent_skill_packages.build_all(second)

    assert first_result["signature_status"] == "unsigned-local-build"
    for spec in build_agent_skill_packages.package_specs():
        first_archive = first / spec.filename
        second_archive = second / spec.filename
        assert first_archive.read_bytes() == second_archive.read_bytes()

        expected_names = {str(PurePosixPath(spec.archive_root) / member) for member in spec.members}
        with ZipFile(first_archive) as archive:
            assert len(archive.namelist()) == len(set(archive.namelist()))
            assert set(archive.namelist()) == expected_names
            for info in archive.infolist():
                path = PurePosixPath(info.filename)
                assert not path.is_absolute()
                assert ".." not in path.parts
                assert info.date_time == build_agent_skill_packages.FIXED_ZIP_TIME


def test_versioned_release_artifacts_match_reproducible_build(tmp_path: Path) -> None:
    build_agent_skill_packages.build_all(tmp_path)
    release_version = "0.4.0"

    for spec in build_agent_skill_packages.package_specs():
        assert (ROOT / "deliverables" / spec.filename).read_bytes() == (
            tmp_path / spec.filename
        ).read_bytes()
    assert _json(ROOT / f"deliverables/agent-skill-release-{release_version}.json") == (
        _json(tmp_path / f"agent-skill-release-{release_version}.json")
    )
