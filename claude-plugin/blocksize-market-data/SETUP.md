# Blocksize Market Data Setup

This plugin references the hosted Blocksize Claude MCP connector:

```text
https://mcp.blocksize.info/anthropic/mcp/
```

## Controlled local install

The public repository mirrors do not yet contain this `0.3.0` plugin and its
marketplace manifest. Until the corrected release is published, load the
complete source directory:

```sh
claude --plugin-dir /absolute/path/to/claude-plugin/blocksize-market-data
```

To test the deterministic local ZIP instead, verify its SHA-256 against
`deliverables/agent-skill-release-0.4.0.json`, extract it, and load the
extracted plugin directory:

```sh
unzip deliverables/blocksize-market-data-claude-plugin-0.3.0.zip -d /absolute/path/to/unpacked
claude --plugin-dir /absolute/path/to/unpacked/blocksize-market-data
```

The current archive is an unsigned local build; this path is for controlled QA,
not a claim of public marketplace availability.

## Future remote install gate

The only planned remote source is the canonical GitHub repository:

```text
https://github.com/jf-cmyk/agentic-payments
```

Do not run a remote marketplace install until that public repository contains
this plugin and `.claude-plugin/marketplace.json` at the accepted release
commit, the published checksum matches the local release manifest, and a clean
Claude install smoke test passes. Once all gates pass, the release instructions
may use:

```sh
claude plugin marketplace add https://github.com/jf-cmyk/agentic-payments.git
claude plugin install blocksize-market-data@blocksize-plugins
```

Installation is an explicit opt-in to the external OAuth and credit-using
service. The connector remains unusable until the user completes OAuth.

## Acceptance

After loading or installing the plugin in Claude Code or Cowork:

1. Open the MCP/connectors panel.
2. Select `blocksize-market-data`.
3. Complete the Blocksize OAuth sign-in flow.
4. Verify tool discovery shows the read-only Blocksize market-data tools.
5. Try: "Search Blocksize for BTC market data instruments."

If authentication fails, disconnect only the `blocksize-market-data` connector
through Claude's MCP settings and try the OAuth flow again. Do not delete a
shared credential directory or paste tokens into chat. If Claude reports that
the server cannot be reached, check the health page:

```text
https://mcp.blocksize.info/health
```
