# Blocksize Market Data for Cursor

Blocksize Market Data connects Cursor agents to Blocksize's hosted MCP server at
`mcp.blocksize.info` for read-only market-data discovery and live crypto,
supported equity ticker, FX, and metals snapshots.

This Cursor plugin uses the dedicated Clerk-authenticated MCP endpoint:

```text
https://mcp.blocksize.info/cursor/mcp/
```

After sign-in, Cursor can call Blocksize tools directly from an agent workflow.
The integration is read-only and gives eligible users the starter allowance
reported by the server. This is not a free-forever tier; production
usage can continue through direct Blocksize x402 outside Cursor or an
authenticated account plan arranged with Blocksize.

The plugin also installs the `/use-blocksize-market-data` Agent Skill. Invoke it
directly or ask Cursor for a Blocksize VWAP, bid/ask, FX, metal,
instrument-search, or credit-status workflow.

## What Is Possible

Cursor agents can use this plugin to:

- Search supported crypto, equity ticker, FX, and metals instruments.
- List supported instrument namespaces.
- Check the signed-in user's remaining daily Blocksize data credits.
- Fetch live crypto VWAP snapshots.
- Fetch live crypto and supported equity/stock ticker bid/ask snapshots.
- Fetch live FX bid, ask, and mid-rate snapshots.
- Fetch supported metal spot-price snapshots.

Eligible signed-in Cursor users receive the starter allowance reported by the
server. Discovery tools are available for finding instruments and metadata;
live data tools spend starter credits according to the server's active
tool-cost configuration.

Typical prompts:

```text
Search Blocksize for BTC market data instruments.
```

```text
Search Blocksize for AAPL with asset_class=equity, then get the current AAPL bid/ask snapshot.
```

```text
Get the latest BTC-USD VWAP from Blocksize.
```

```text
Show my remaining Blocksize data credits.
```

```text
Get the current EURUSD FX snapshot.
```

```text
Get the latest XAUUSD metal price.
```

## Data Offerings

Blocksize's hosted MCP server exposes these market-data surfaces to Cursor:

| Offering | Tooling | Notes |
| --- | --- | --- |
| Crypto discovery | `search_pairs`, `list_instruments` | Search supported pairs and metadata before making live calls. |
| Crypto VWAP | `get_vwap` | Institutional VWAP snapshots for supported crypto pairs such as `BTC-USD`. |
| Crypto bid/ask | `get_bid_ask` | Latest bid, ask, and spread for supported crypto pairs. |
| Equities bid/ask | `search_pairs`, `get_bid_ask` | Supported stock tickers such as `AAPL`; search with `asset_class=equity`, then fetch through the shared bid/ask surface. |
| FX | `get_fx_rate` | Bid, ask, and mid-rate snapshots for supported FX pairs such as `EURUSD`. |
| Metals | `get_metal_price` | Spot snapshots for supported metal tickers such as `XAUUSD`. |
| Credits | `get_credit_balance` | Shows the authenticated user's current starter-credit balance. |

The public health endpoint at `https://mcp.blocksize.info/health` also exposes
current service metadata, supported links, pricing categories, and the active
Cursor connector configuration.

## MCP Tools

This plugin currently exposes these read-only MCP tools:

- `search_pairs`: Search supported crypto, equity/stock ticker, FX, and metal
  instruments by symbol or asset name. Use `asset_class=equity` for stocks.
- `list_instruments`: List supported instruments for a Blocksize service
  namespace.
- `get_credit_balance`: Show remaining daily data credits for the signed-in
  Cursor user.
- `get_vwap`: Fetch a crypto VWAP snapshot for one supported pair.
- `get_bid_ask`: Fetch bid/ask data for one supported crypto pair or equity
  ticker such as `AAPL`.
- `get_fx_rate`: Fetch a supported FX pair snapshot.
- `get_metal_price`: Fetch a supported metal spot-price snapshot.

All tools are read-only. The plugin cannot write orders, execute trades, move
funds, or mutate user accounts.

## Access Model

Cursor uses this endpoint:

```text
https://mcp.blocksize.info/cursor/mcp/
```

The endpoint supports OAuth discovery for MCP clients:

```text
https://mcp.blocksize.info/.well-known/oauth-protected-resource/cursor/mcp/
https://mcp.blocksize.info/.well-known/oauth-authorization-server/cursor/mcp
```

Users sign in with Blocksize through Clerk. The MCP client receives OAuth tokens
and then calls the hosted MCP server over Streamable HTTP. Live data calls spend
from the current allowance reported by the server. Cursor does not submit
payment proofs, initiate wallet transactions, or spend funds directly.

Blocksize does not use Cursor Plugin Data or User Content to train
machine-learning or generative-AI models. See the privacy policy and data terms
linked below for the service's collection, retention, and use boundaries.

## Install The Complete Plugin

After marketplace publication, use Cursor's `/add-plugin` flow and select
`blocksize-market-data`. For prerelease acceptance, place this complete plugin
folder at `~/.cursor/plugins/local/blocksize-market-data`, restart Cursor, and
verify both the MCP server and `/use-blocksize-market-data` skill appear.

## MCP-Only Fallback

The following config or deeplink installs only the MCP server. It does not
install the skill or the complete plugin:

Add this MCP server config in Cursor:

```json
{
  "mcpServers": {
    "blocksize-market-data": {
      "url": "https://mcp.blocksize.info/cursor/mcp/"
    }
  }
}
```

Cursor deeplink:

```text
cursor://anysphere.cursor-deeplink/mcp/install?name=blocksize-market-data&config=eyJ1cmwiOiJodHRwczovL21jcC5ibG9ja3NpemUuaW5mby9jdXJzb3IvbWNwLyJ9
```

With a complete plugin installation, invoke:

```text
/use-blocksize-market-data
```

## What This Is Not

This Cursor plugin does not:

- Execute wallet transactions.
- Submit payment proofs.
- Spend funds directly from Cursor.
- Place trades or orders.
- Change accounts, balances, positions, or payment settings.

For Blocksize's public paid API and registry-oriented integrations, use:

```text
https://mcp.blocksize.info/mcp/server/
```

## Smoke Test

Use Cursor's native plugin and MCP panels:

1. Confirm one `blocksize-market-data` plugin and one MCP server appear.
2. Confirm `/use-blocksize-market-data` appears in skill discovery.
3. Open the connector and complete Clerk OAuth without copying tokens.
4. Confirm exactly the seven read-only tools listed above appear.
5. Run a free `search_pairs` request before explicitly authorizing one live call.

If auth must be reset, disconnect only this connector through Cursor settings.
Do not delete a shared MCP auth directory or run an unpinned test client.

## Links

- Blocksize MCP server: https://mcp.blocksize.info
- Cursor MCP endpoint: https://mcp.blocksize.info/cursor/mcp/
- OAuth callback: https://mcp.blocksize.info/cursor/mcp/auth/callback
- OAuth protected-resource metadata: https://mcp.blocksize.info/.well-known/oauth-protected-resource/cursor/mcp/
- OAuth authorization-server metadata: https://mcp.blocksize.info/.well-known/oauth-authorization-server/cursor/mcp
- Public MCP endpoint: https://mcp.blocksize.info/mcp/server/
- OpenAPI reference: https://mcp.blocksize.info/openapi.json
- Privacy policy: https://mcp.blocksize.info/privacy
- Data terms: https://mcp.blocksize.info/terms
- Support: https://mcp.blocksize.info/support

## License

MIT
