# Claude Connector Launch Status

Updated: 2026-05-13

## Current State

Blocksize Market Data is live as a Claude-compatible remote MCP custom connector.
It is not yet listed in Claude's Connectors Directory.

Install URL for custom connector testing:

```text
https://mcp.blocksize.info/anthropic/mcp/
```

Public documentation:

```text
https://mcp.blocksize.info/claude-connector
```

Submission form:

```text
https://clau.de/mcp-directory-submission
```

## Deployed Services

Main production service:

```text
https://mcp.blocksize.info
```

Claude beta service:

```text
https://anthropic-mcp-beta-production.up.railway.app
```

Both services have the Claude connector routes deployed. The main production
service is the URL intended for submission.

## Verified Production Behavior

Main production health reports:

```text
anthropic_connector.auth_provider=clerk
anthropic_connector.beta_tokens_enabled=false
anthropic_connector.mcp_url=https://mcp.blocksize.info/anthropic/mcp
cursor_connector.auth_provider=clerk
cursor_connector.beta_tokens_enabled=false
```

Public endpoints verified:

```text
GET /claude-connector -> 200
GET /.well-known/oauth-protected-resource/anthropic/mcp/ -> 200
GET /.well-known/oauth-authorization-server/anthropic/mcp -> 200
GET /anthropic/mcp/ without token -> 401 with WWW-Authenticate resource metadata
```

Isolation checks:

```text
GET /server.json -> unchanged MCP Registry metadata
GET /.well-known/oauth-authorization-server -> Cursor issuer outside ANTHROPIC_ONLY_MODE
GET /.well-known/oauth-protected-resource/cursor/mcp/ -> Cursor metadata unchanged
GET /openapi.json -> 200 for Pay.sh/OpenAPI consumers
```

Local and remote QA:

```bash
.venv/bin/python -m pytest tests/ -q
# 125 passed

.venv/bin/python scripts/test_anthropic_mcp_connector.py \
  --url https://mcp.blocksize.info/anthropic/mcp/ \
  --expect-oauth-required
# PASS oauth challenge required: status=401
```

Claude-origin checks:

```text
Origin: https://claude.ai does not block:
- OAuth protected-resource metadata
- OAuth authorization-server metadata
- MCP transport 401 OAuth challenge
```

## Remaining Blocking Items

These require access outside the repo/deploy shell.

1. Create or confirm a fully populated reviewer account.
   - Suggested email: `claude-reviewer@blocksize-capital.com`
   - The account should complete the Clerk OAuth flow.
   - It should have at least 50 daily credits or an override high enough for review.
   - Credentials must be shared only through Anthropic's private submission form.

2. Test as a Claude custom connector.
   - Add `https://mcp.blocksize.info/anthropic/mcp/`.
   - Complete Clerk OAuth.
   - Enable the connector in a chat.
   - Exercise every tool listed below.

3. Test in MCP Inspector.
   - Use the production MCP URL.
   - Complete OAuth in the inspector.
   - Confirm tool discovery and all valid tool calls.

4. Submit the form.
   - Use `https://clau.de/mcp-directory-submission`.
   - Use the form answers below.

## Submission Form Answers

Server name:

```text
Blocksize Market Data
```

Server URL:

```text
https://mcp.blocksize.info/anthropic/mcp/
```

Tagline:

```text
Read-only real-time crypto, equity, FX, and metals market data for Claude.
```

Short description:

```text
Blocksize Market Data gives Claude read-only access to supported real-time
market-data tools for crypto, supported equity tickers, FX, and metals. Users
connect with Blocksize through OAuth and receive server-side daily data credits.
The connector does not place trades, execute wallet transactions, transfer funds,
or submit x402 payment proofs.
```

Use cases:

```text
- Search supported instruments before requesting live data.
- Check remaining daily Blocksize data credits.
- Retrieve crypto VWAP and bid/ask snapshots.
- Retrieve supported equity bid/ask snapshots.
- Retrieve FX and metal price snapshots for market analysis.
```

Auth type:

```text
OAuth 2.0 with Dynamic Client Registration / Client ID Metadata Document support.
```

Transport:

```text
Streamable HTTP
```

Read/write capability:

```text
Read-only.
```

Connection requirements:

```text
Users connect through Claude's standard OAuth flow and sign in to Blocksize via
Clerk. The connector is publicly reachable over HTTPS from Anthropic's cloud.
```

Allowed link URIs:

```text
https://mcp.blocksize.info
https://blocksize.info
```

Data and compliance:

```text
The connector processes OAuth identity, request metadata, requested market-data
symbols, daily credit usage, and operational logs needed to provide the service.
It does not collect Claude conversations, Claude memory, user files, wallet
private keys, or payment proofs through the Claude connector. Live market data is
served by Blocksize-controlled market-data infrastructure.
```

Third-party connections:

```text
Clerk is used for OAuth sign-in. Blocksize-controlled market-data infrastructure
is used for market-data retrieval. The Claude connector itself does not call
payment networks, wallets, or x402 facilitators.
```

Health data:

```text
No health data is accessed.
```

Category:

```text
Finance / Market data / Data analytics
```

Documentation:

```text
https://mcp.blocksize.info/claude-connector
```

Privacy policy:

```text
https://mcp.blocksize.info/privacy
```

Support:

```text
https://mcp.blocksize.info/support
```

Logo:

```text
https://mcp.blocksize.info/assets/logo.png
```

Tools:

| Tool | Human-readable name | Capability |
| --- | --- | --- |
| `search_pairs` | Instrument Search | Search supported instruments and metadata. |
| `list_instruments` | Instrument List | List supported instruments for one service namespace. |
| `get_credit_balance` | Credit Balance | Show remaining daily data credits for the signed-in user. |
| `get_vwap` | Crypto VWAP Snapshot | Fetch read-only crypto VWAP for one pair. |
| `get_bid_ask` | Crypto Bid Ask Snapshot | Fetch read-only bid/ask for one crypto pair or supported equity ticker. |
| `get_fx_rate` | FX Snapshot | Fetch read-only FX bid, ask, and mid-rate snapshot. |
| `get_metal_price` | Metal Snapshot | Fetch read-only metal spot-price snapshot. |

Tool annotations:

```text
All tools include titles and read-only annotations. No destructive tools are
exposed.
```

Resources:

```text
blocksize://anthropic-info
```

Prompts:

```text
No MCP prompts are exposed by this connector.
```

Reviewer setup instructions:

```text
1. Add https://mcp.blocksize.info/anthropic/mcp/ as a Claude custom connector.
2. Click Connect and complete the Blocksize/Clerk OAuth flow using the supplied
   reviewer credentials.
3. Enable the connector in a chat.
4. Ask: "Search Blocksize for BTC market data instruments."
5. Ask: "Show my remaining Blocksize data credits."
6. Ask: "Get the latest BTC-USD VWAP from Blocksize."
7. Ask: "Get the current EURUSD FX snapshot."
8. Ask: "Get the latest XAUUSD metal price."
9. Optional invalid-input check: ask for "../bad" VWAP and confirm the connector
   returns INVALID_SYMBOL without spending credits.
```

Policy statement:

```text
This Claude connector is read-only. It does not transfer money, cryptocurrency,
or other financial assets; does not execute financial transactions; does not
place trades or orders; does not submit wallet signatures; and does not submit
x402 payment proofs. Blocksize's x402 paid HTTP API is a separate integration
surface and is not exposed as a Claude MCP tool.
```

Launch readiness:

```text
Production remote MCP endpoint is deployed. Public documentation, privacy, and
support pages are live. OAuth metadata and 401 challenge behavior are verified.
Claude custom-connector OAuth testing and MCP Inspector testing should be
completed with the reviewer account before submitting the form.
```
