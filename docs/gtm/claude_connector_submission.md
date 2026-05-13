# Claude Connectors Directory Submission Packet

Prepared: 2026-05-13

Current launch status and copy-ready submission answers now live in
[claude_connector_launch_status.md](claude_connector_launch_status.md).

The complementary Claude Code/Cowork plugin packet lives in
[claude_plugin_submission/README.md](claude_plugin_submission/README.md). Submit
the remote MCP connector first; the plugin is optional and should be submitted
as a separate plugin directory entry.

## Goal

Submit Blocksize Market Data as a Claude remote MCP connector while keeping the
Claude-facing surface compliant with Anthropic's current directory posture:
read-only tools, OAuth for user identity, no wallet/payment execution, clear
support/privacy docs, and reviewer-ready examples.

## Current Claude Requirements Reviewed

Sources checked on 2026-05-13:

- Connectors Directory overview: https://claude.com/docs/connectors/directory
- Remote MCP connector guide: https://claude.com/docs/connectors/custom/remote-mcp
- Submission guide: https://claude.com/docs/connectors/building/submission
- Pre-submission checklist: https://claude.com/docs/connectors/building/review-criteria
- Authentication guide: https://claude.com/docs/connectors/building/authentication
- Testing guide: https://claude.com/docs/connectors/building/testing
- MCP connector API docs: https://platform.claude.com/docs/en/agents-and-tools/mcp-connector

Practical requirements for this repo:

- Remote MCP server must be public HTTPS and use Streamable HTTP or SSE.
- Authenticated services must use OAuth 2.0, not URL query tokens or
  machine-only client credentials.
- Every tool needs a title plus `readOnlyHint` or `destructiveHint`.
- Tool names must be 64 characters or fewer.
- Tool descriptions must narrowly match actual behavior.
- Tools must return useful validation errors and bounded responses.
- Public docs, privacy policy, support channel, and reviewer test credentials
  must be available.
- Reviewers test every tool via MCP Inspector and as a Claude custom connector.
- Connectors that transfer money, cryptocurrency, or other financial assets are
  not accepted, so the Claude connector must stay separate from x402 payment
  execution.

## Submission Positioning

Name:

```text
Blocksize Market Data
```

Tagline:

```text
Read-only real-time crypto, equity, FX, and metals market data for Claude.
```

Remote MCP URL:

```text
https://mcp.blocksize.info/anthropic/mcp/
```

Public documentation:

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

Allowed link URIs:

```text
https://mcp.blocksize.info
https://blocksize.info
```

Auth type:

```text
OAuth 2.0 with Dynamic Client Registration or Client ID Metadata Document support.
```

Transport:

```text
Streamable HTTP
```

Read/write capability:

```text
Read-only. No trades, orders, wallet transactions, x402 proof submission, account
mutation, payment setting changes, or funds movement.
```

## Tool Inventory

All tools are read-only and use `READ_ONLY_TOOL_ANNOTATIONS`.

| Tool | Human name | Requires auth | Notes |
| --- | --- | --- | --- |
| `search_pairs` | Instrument Search | No | Metadata only. |
| `list_instruments` | Instrument List | No | Metadata only. |
| `get_credit_balance` | Credit Balance | Yes | Shows server-side daily data credits. |
| `get_vwap` | Crypto VWAP Snapshot | Yes | Uses daily credits. |
| `get_bid_ask` | Crypto Bid Ask Snapshot | Yes | Uses daily credits. |
| `get_fx_rate` | FX Snapshot | Yes | Uses daily credits. |
| `get_metal_price` | Metal Snapshot | Yes | Uses daily credits. |

## Review Prompt Examples

Use these in the submission form:

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

## Reviewer Test Account

Create a Blocksize reviewer account before submission:

```text
Email: claude-reviewer@blocksize-capital.com
Daily credits: 50 or higher
Sample access: BTCUSD VWAP, BTCUSD bid/ask, EURUSD FX, XAUUSD metal price
```

Credentials should be supplied only through Anthropic's private review form, not
committed to this repo.

## Pre-Submission QA

Required local or staging checks:

```bash
.venv/bin/python -m pytest tests/test_anthropic_auth.py tests/test_anthropic_mcp_tools.py tests/test_resource_server.py -q
```

Protocol checks:

```bash
.venv/bin/python scripts/test_anthropic_mcp_connector.py --url https://mcp.blocksize.info/anthropic/mcp/ --expect-oauth-required
```

OAuth/manual checks:

1. Add `https://mcp.blocksize.info/anthropic/mcp/` as a Claude custom connector.
2. Complete OAuth.
3. Confirm `search_pairs` and `list_instruments` work.
4. Confirm `get_credit_balance` works after auth.
5. Confirm `get_vwap BTCUSD`, `get_fx_rate EURUSD`, and `get_metal_price XAUUSD`
   return data and remaining credits.
6. Confirm invalid input, such as `../bad`, returns `INVALID_SYMBOL` and does not
   spend credits.

## Open Submission Risks

The main policy risk is brand adjacency to x402 and crypto payment flows. The
mitigation is product separation:

- Submit only `/anthropic/mcp/`, not the public paid HTTP API.
- State clearly that the Claude connector is read-only.
- Do not expose payment proof, wallet, purchase, or trade tools through Claude.
- Explain server-side daily credits as an access allowance, not an on-chain
  transaction path.

## Finish-Line Checklist

- [x] Deploy the Anthropic OAuth metadata routes.
- [x] Configure OAuth provider for production:
  - `ANTHROPIC_AUTH_PROVIDER=clerk`
  - `ANTHROPIC_ENABLE_BETA_TOKENS=false`
  - `ANTHROPIC_MCP_PUBLIC_URL=https://mcp.blocksize.info/anthropic/mcp`
- [x] Register callback:
  - `https://mcp.blocksize.info/anthropic/mcp/auth/callback`
- [x] Confirm persistent entitlement storage path/volume for the main service.
- [x] Confirm standalone Claude beta service uses OAuth and persistent
  entitlement storage.
- [x] Prepare optional Claude Code/Cowork plugin package:
  - `claude-plugin/blocksize-market-data`
- [ ] Create reviewer account and sample data access.
- [ ] Run MCP Inspector against the production URL.
- [ ] Test as a Claude custom connector.
- [ ] Fill out Anthropic's MCP directory submission form.
- [ ] Optional: validate and submit the plugin through Claude's plugin form.
