# Get your first live Blocksize price

Blocksize has two integration layers. The authenticated Claude and Cursor MCP connectors can return live read-only market data using included server-side credits. The public ChatGPT-compatible MCP endpoint currently provides discovery and endpoint construction only; use the HTTP call below for the live price.

The hosted quickstart at `https://mcp.blocksize.info/quickstart/first-price` also includes a one-click live BTC-USD request. It keeps one browser-scoped agent identity in local storage so the starter allowance cannot be silently reset on every click.

## Claude

1. Add `https://mcp.blocksize.info/anthropic/mcp/` as a custom connector.
2. Sign in with Blocksize.
3. Ask: `Use Blocksize to get the latest BTC-USD VWAP and include the source timestamp.`

Expected live tool: `get_vwap`. The first successful call consumes one included data credit and returns the remaining balance.

## Cursor

Add the authenticated connector to `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "blocksize-live": {
      "url": "https://mcp.blocksize.info/cursor/mcp/"
    }
  }
}
```

Complete OAuth when Cursor prompts, then ask: `Use Blocksize get_vwap for BTC-USD and report the source timestamp.`

## ChatGPT

The public remote endpoint `https://mcp.blocksize.info/mcp/server/` supports symbol discovery, documentation, pricing inspection, and exact endpoint construction. It does not currently expose the live-price tools in ChatGPT.

For a live first price now, call the HTTP API with an explicit agent identity:

```bash
curl -sS \
  -H 'X-AGENT-ID: chatgpt-quickstart-0001' \
  'https://mcp.blocksize.info/v1/vwap/btc-usd'
```

Eligible new identities start with 50 live-data credits. When those credits are unavailable or exhausted, the same URL returns an x402 payment challenge.

## Verify the result

A successful response should include the instrument, price/VWAP, currency, source timestamp, provider context, and citation or methodology metadata. Do not treat discovery output as a live price.
