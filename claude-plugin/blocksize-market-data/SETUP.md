# Blocksize Market Data Setup

This plugin references the hosted Blocksize Claude MCP connector:

```text
https://mcp.blocksize.info/anthropic/mcp/
```

After installing the plugin in Claude Code or Cowork:

1. Open the MCP/connectors panel.
2. Select `blocksize-market-data`.
3. Complete the Blocksize OAuth sign-in flow.
4. Verify tool discovery shows the read-only Blocksize market-data tools.
5. Try: "Search Blocksize for BTC market data instruments."

If authentication fails, clear cached MCP authentication for the connector and
try the OAuth flow again. If Claude reports that the server cannot be reached,
check that the health page is available:

```text
https://mcp.blocksize.info/health
```
