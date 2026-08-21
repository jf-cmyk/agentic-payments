from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
OPENAI_SKILL = (
    ROOT
    / "openai-plugin"
    / "blocksize-market-data"
    / "skills"
    / "use-blocksize-market-data"
)
CLAUDE_SKILL = (
    ROOT
    / "claude-plugin"
    / "blocksize-market-data"
    / "skills"
    / "use-blocksize-market-data"
)
CURSOR_SKILL = (
    ROOT
    / "blocksize-cursor-plugin"
    / "plugins"
    / "blocksize-market-data"
    / "skills"
    / "use-blocksize-market-data"
)


def test_agent_skill_content_is_synchronized_across_hosts() -> None:
    relative_files = (
        Path("SKILL.md"),
        Path("references/tool-surfaces.md"),
        Path("references/response-contract.md"),
    )

    for relative_file in relative_files:
        expected = (OPENAI_SKILL / relative_file).read_text(encoding="utf-8")
        assert (CLAUDE_SKILL / relative_file).read_text(encoding="utf-8") == expected
        assert (CURSOR_SKILL / relative_file).read_text(encoding="utf-8") == expected
        assert "TODO" not in expected


def test_openai_plugin_bundles_public_blocksize_mcp() -> None:
    plugin_root = ROOT / "openai-plugin" / "blocksize-market-data"
    manifest = json.loads(
        (plugin_root / ".codex-plugin/plugin.json").read_text(encoding="utf-8")
    )
    mcp = json.loads((plugin_root / ".mcp.json").read_text(encoding="utf-8"))

    assert manifest["name"] == "blocksize-market-data"
    assert manifest["version"] == "0.3.0"
    assert manifest["skills"] == "./skills/"
    assert manifest["mcpServers"] == "./.mcp.json"
    assert (
        mcp["mcpServers"]["blocksize-market-data"]["url"]
        == "https://mcp.blocksize.info/openai/mcp/"
    )


def test_live_connector_packages_include_the_shared_skill() -> None:
    claude_manifest = json.loads(
        (
            ROOT
            / "claude-plugin/blocksize-market-data/.claude-plugin/plugin.json"
        ).read_text(encoding="utf-8")
    )
    cursor_manifest = json.loads(
        (
            ROOT
            / "blocksize-cursor-plugin/plugins/blocksize-market-data/.cursor-plugin/plugin.json"
        ).read_text(encoding="utf-8")
    )

    assert claude_manifest["version"] == "0.2.0"
    assert cursor_manifest["version"] == "1.2.0"
    assert CLAUDE_SKILL.joinpath("SKILL.md").is_file()
    assert CURSOR_SKILL.joinpath("SKILL.md").is_file()
