# Blocksize Market Data for Claude

Blocksize Market Data is a Claude plugin that wraps Blocksize's hosted remote
MCP connector with practical market-data workflow guidance for Claude Code and
Cowork.

The plugin references this public OAuth-protected MCP endpoint:

```text
https://mcp.blocksize.info/anthropic/mcp/
```

The connector is read-only. It can search supported instruments, inspect starter
live-data credits, and retrieve market-data snapshots. It cannot place trades,
execute wallet transactions, transfer funds, submit wallet signatures, or submit
x402 payment proofs.

## What It Adds

- A remote HTTP MCP server reference for Blocksize Market Data.
- The `/blocksize-market-data:use-blocksize-market-data` skill for safe symbol
  lookup, credit checks, and bounded snapshot requests.
- Setup and review instructions for Claude Code and Cowork users.

## Use The Skill

After installing the plugin, invoke:

```text
/blocksize-market-data:use-blocksize-market-data
```

You can also ask Claude for a Blocksize VWAP, bid/ask, FX, metal,
instrument-search, or credit-status workflow. The skill will prefer live tools
when the authenticated connector exposes them and will clearly label
discovery-only or route-only results.

## Available Tools

The hosted MCP endpoint exposes these read-only tools:

| Tool | Purpose |
| --- | --- |
| `search_pairs` | Search supported crypto, equity, FX, and metal instruments. |
| `list_instruments` | List supported instruments for a Blocksize service namespace. |
| `get_credit_balance` | Show the signed-in user's remaining starter live-data credits. |
| `get_vwap` | Fetch a crypto VWAP snapshot for one supported pair. |
| `get_bid_ask` | Fetch bid/ask data for one supported crypto pair or equity ticker. |
| `get_fx_rate` | Fetch a supported FX pair snapshot. |
| `get_metal_price` | Fetch a supported metal spot-price snapshot. |

## Authentication

Claude handles OAuth for the remote MCP server. On first use, open `/mcp` in
Claude Code or the connector settings in Cowork, then complete the
Blocksize/Clerk sign-in flow.

OAuth metadata:

```text
https://mcp.blocksize.info/.well-known/oauth-protected-resource/anthropic/mcp/
https://mcp.blocksize.info/.well-known/oauth-authorization-server/anthropic/mcp
```

Hosted Claude surfaces should use:

```text
https://claude.ai/api/mcp/auth_callback
```

Claude Code uses a local loopback OAuth callback with an ephemeral port.

## Example Prompts

```text
Search Blocksize for BTC market data instruments.
```

```text
Show my remaining Blocksize data credits.
```

```text
Get the latest BTC-USD VWAP from Blocksize.
```

```text
Get the current EURUSD FX snapshot.
```

```text
Get the latest XAUUSD metal price.
```

## Safety Boundary

This plugin and the Claude MCP connector are separate from Blocksize's x402 paid
HTTP API. The Claude surface does not expose payment proof submission, wallet
operations, credit purchases, order placement, or account mutation tools.
Eligible users receive the starter allowance reported by the server; production
usage can continue through direct Blocksize x402 outside Claude or an
authenticated account plan arranged with Blocksize.

## Links

- Connector docs: https://mcp.blocksize.info/claude-connector
- Privacy policy: https://mcp.blocksize.info/privacy
- Data terms: https://mcp.blocksize.info/terms
- Support: https://mcp.blocksize.info/support
- Production health: https://mcp.blocksize.info/health
