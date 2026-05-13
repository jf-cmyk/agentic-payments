# Claude Plugin Submission Packet

Prepared: 2026-05-13

This packet is for the optional Claude plugin directory track. The primary
Claude marketplace path remains the remote MCP connector submission at:

```text
https://clau.de/mcp-directory-submission
```

Anthropic's current guidance recommends shipping a remote MCP server first, then
optionally shipping a plugin that wraps the server with skills. The plugin lives
at:

```text
claude-plugin/blocksize-market-data
```

## Plugin Summary

Name:

```text
blocksize-market-data
```

Display description:

```text
Read-only Blocksize market-data workflows for Claude Code and Cowork.
```

Plugin source path:

```text
claude-plugin/blocksize-market-data
```

Public source URL:

```text
https://gitlab.com/jfocke/agentic-payments/-/tree/main/claude-plugin/blocksize-market-data
```

Prepared upload archive:

```text
deliverables/blocksize-market-data-claude-plugin-0.1.0.zip
```

Remote MCP endpoint:

```text
https://mcp.blocksize.info/anthropic/mcp/
```

Plugin submission forms:

```text
https://claude.ai/settings/plugins/submit
https://platform.claude.com/plugins/submit
```

## What The Plugin Contains

- `.claude-plugin/plugin.json` with plugin metadata.
- `.mcp.json` pointing to the production Claude MCP endpoint.
- `skills/market-data-workflow/SKILL.md` with safe market-data workflow guidance.
- `SETUP.md` with installation and OAuth troubleshooting instructions.
- `README.md` and `LICENSE`.

## Submission Notes

- Use this plugin for Claude Code and Cowork discovery.
- Use the Connectors Directory form for Claude.ai/Desktop/mobile-wide remote MCP
  discovery.
- If Anthropic requires a GitHub URL, sync this plugin path to the public GitHub
  remote before using the GitHub option.
- Until GitHub is synced, upload
  `deliverables/blocksize-market-data-claude-plugin-0.1.0.zip`.
- Run `claude plugin validate claude-plugin/blocksize-market-data` before plugin
  submission where the Claude CLI is available.

## Validation Status

Verified on 2026-05-13:

```bash
python3 -m json.tool claude-plugin/blocksize-market-data/.claude-plugin/plugin.json
python3 -m json.tool claude-plugin/blocksize-market-data/.mcp.json
claude plugin validate claude-plugin/blocksize-market-data
```

Result:

```text
Validation passed
```

## Policy Positioning

The plugin references the same read-only Claude MCP endpoint as the directory
connector. It does not ship local executables, hooks, monitors, write tools, or
payment flows. It does not transfer money, cryptocurrency, or other financial
assets.
