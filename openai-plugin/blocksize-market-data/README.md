# Blocksize Market Data for ChatGPT and Codex

This plugin gives ChatGPT and Codex a portable Agent Skill plus Blocksize's
authenticated, read-only MCP live-data server.

## What It Does

- Searches Blocksize instruments and product metadata.
- Finds pricing, workflow documentation, and exact paid HTTP routes.
- Distinguishes discovery results, generated routes, and live observations.
- Preserves timestamps, provenance, units, and freshness metadata when the
  connected surface returns them.

Invoke the bundled skill with:

```text
$use-blocksize-market-data
```

## OpenAI Connector

The bundled MCP server is:

```text
https://mcp.blocksize.info/openai/mcp/
```

After OAuth authentication, the connector exposes instrument discovery, credit
status, and live VWAP, bid/ask, FX, and metal tools. Eligible users receive the
starter allowance reported by the server. Routes not exposed as live MCP tools
continue to use the public endpoint builder and are clearly labeled as routes
rather than observations.

## Safety

The plugin is read-only. It does not trade, transfer funds, sign wallet
messages, submit payment proofs, or provide investment advice.

## Install Locally Now

The public repository mirrors do not yet contain this `0.4.0` plugin and its
marketplace manifest. Do not use a remote marketplace URL for the current local
release candidate.

For controlled source testing, use a local checkout that contains
`.agents/plugins/marketplace.json`:

```sh
codex plugin marketplace add /absolute/path/to/agentic-payments
codex plugin add blocksize-market-data@blocksize-plugins
```

The deterministic local archive is:

```text
deliverables/blocksize-market-data-openai-plugin-0.4.0.zip
```

That archive contains the plugin, not a standalone marketplace. To test the
ZIP, extract it under a dedicated local marketplace root as
`plugins/blocksize-market-data`, add a local marketplace entry whose
`source.path` is `./plugins/blocksize-market-data`, then add that root with
`codex plugin marketplace add /absolute/path/to/local-marketplace`. Verify its
SHA-256 against `deliverables/agent-skill-release-0.4.0.json` first; the current
archive is an unsigned local build.

## Future Remote Install Gate

The only planned remote source is the canonical GitHub repository:

```text
https://github.com/jf-cmyk/agentic-payments
```

Do not add it as a marketplace until the public repository contains this
plugin and `.agents/plugins/marketplace.json` at one immutable release tag, the
published checksum matches the local release manifest, and a clean install
smoke test passes. After those gates pass, install from that pinned tag rather
than an unpinned branch.

The local marketplace is available to supported ChatGPT Work and Codex
surfaces in the ChatGPT desktop app. ChatGPT web access requires a published
universal plugin listing or a separately registered custom connector; copying
a Codex folder alone does not configure ChatGPT web.

For skill-only local authoring, copy the standalone skill folder to:

```text
.agents/skills/use-blocksize-market-data/
```

The standalone skill includes the public discovery dependency but does not add
the authenticated live connector. Install the complete plugin for live tools.

## Links

- Blocksize MCP server: https://mcp.blocksize.info/
- Agent market-data page: https://mcp.blocksize.info/market-data-api-for-ai-agents
- Privacy policy: https://mcp.blocksize.info/privacy
- Data terms: https://mcp.blocksize.info/terms
- Support: https://mcp.blocksize.info/support

## License

MIT
