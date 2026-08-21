# Blocksize market-data agent skill

The `use-blocksize-market-data` skill teaches ChatGPT/Codex, Claude, Cursor,
and other Agent Skills-compatible hosts how to discover and retrieve Blocksize
market data without confusing catalog metadata, live observations, or paid HTTP
routes.

## Universal standalone installation

Extract the `use-blocksize-market-data` skill folder from the universal archive
and place it in the host's project or user skill directory:

| Host | Project skill location | Invocation |
| --- | --- | --- |
| ChatGPT/Codex | `.agents/skills/use-blocksize-market-data/` | `$use-blocksize-market-data` |
| Claude Code | `.claude/skills/use-blocksize-market-data/` | `/use-blocksize-market-data` |
| Cursor | `.cursor/skills/use-blocksize-market-data/` | `/use-blocksize-market-data` |

The skill needs a Blocksize MCP connection to call tools. A standalone skill
does not install that connection by itself.

## OpenAI / ChatGPT / Codex plugin

The OpenAI plugin package bundles the skill with the authenticated Streamable HTTP MCP
server:

```text
https://mcp.blocksize.info/openai/mcp/
```

This surface supports instrument discovery, credit status, and live VWAP,
bid/ask, FX, and metal observations after OAuth authentication. The public
discovery server at `https://mcp.blocksize.info/mcp/server/` remains available
for catalog, pricing, documentation search, and endpoint construction.

## Claude plugin

For local plugin validation or development:

```sh
claude --plugin-dir ./claude-plugin/blocksize-market-data
```

Invoke `/blocksize-market-data:use-blocksize-market-data`. The plugin connects
to `https://mcp.blocksize.info/anthropic/mcp/` and exposes authenticated,
read-only live market-data tools with the current starter-credit allowance.

## Cursor plugin

The Cursor package connects to:

```text
https://mcp.blocksize.info/cursor/mcp/
```

The plugin bundles the same skill plus the authenticated, read-only MCP server.
After the plugin is installed, invoke `/use-blocksize-market-data` or ask Cursor
for a Blocksize VWAP, bid/ask, FX, metal, instrument-search, or credit-status
workflow.

## Safety and product boundary

- All connector tools are read-only.
- The skill never trades, moves funds, signs wallet messages, or submits
  payment proofs.
- Discovery does not prove live readiness.
- A generated HTTP route is not a retrieved market-data observation.
- Starter credits are for evaluation; production access can require x402 or
  prepaid credits outside the connector.

## Release checklist

1. Validate every skill with the Agent Skills validator.
2. Validate the OpenAI and Claude plugin manifests.
3. Test the public MCP tool list and one representative discovery workflow.
4. Test authenticated live retrieval separately in OpenAI, Claude, and Cursor.
5. Submit each plugin package to its vendor marketplace only after its hosted
   connector and OAuth flow pass production checks.
