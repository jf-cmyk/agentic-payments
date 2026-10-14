# Claude Connectors Directory Submission Packet

Prepared: 2026-05-13. Updated: 2026-10-14.

**Submitted on 2026-10-14 via https://claude.ai/directory/manage. Status: in
review.** Portal name "Blocksize - Blockchain Market Data", slug
`blocksize-blockchain-market-data`. The submission record, the final portal
copy, the reviewer test instructions and the rules that apply while the review
is open live in
[claude_connector_launch_status.md](claude_connector_launch_status.md).

The complementary Claude Code/Cowork plugin packet lives in
[claude_plugin_submission/README.md](claude_plugin_submission/README.md). Submit
the remote MCP connector first; the plugin is optional and should be submitted
as a separate plugin directory entry.

## Goal

Get Blocksize Market Data listed as a Claude remote MCP connector while keeping the
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

Name (as submitted):

```text
Blocksize - Blockchain Market Data
```

One-liner (as submitted):

```text
Read-only real-time crypto, equity ticker, FX and metals market data for Claude: VWAP, bid/ask, AMM state prices, market briefs, pre-trade checks and trader indicators.
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

Auth type (as detected by the portal):

```text
Always required. OAuth 2.0 with Client ID Metadata Document (the server also
supports Dynamic Client Registration), PKCE S256, scopes email and profile.
Open self sign-up through Clerk with an email code.
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

All 18 tools in `src/authenticated_mcp_server.py` are read-only, use
`READ_ONLY_TOOL_ANNOTATIONS` and carry their title inside the annotations (PR
#106). Credits are the listed free-tier price per call; raw crypto tools charge 4
credits for long-tail pairs and `get_bid_ask` charges 8 for equity tickers.

| Tool | Title | Credits | Requires auth | Notes |
| --- | --- | ---: | --- | --- |
| `search_pairs` | Instrument Search | 0 | No | Metadata only. |
| `list_instruments` | Instrument List | 0 | No | Metadata only. |
| `get_credit_balance` | Credit Balance | 0 | Yes | Remaining free-tier live-data credits. |
| `get_vwap` | Crypto VWAP Snapshot | 2 | Yes | Latest crypto VWAP for one pair. |
| `get_bid_ask` | Bid Ask Snapshot | 2 | Yes | Crypto pair or supported equity ticker. |
| `get_fx_rate` | FX Snapshot | 5 | Yes | Bid, ask and mid rate for one FX pair. |
| `get_metal_price` | Metal Snapshot | 5 | Yes | Spot price for one metal ticker. |
| `get_state_price` | AMM State Price | 2 | Yes | Pool-derived state price; use MSOLUSD. |
| `get_vwap_30m` | 30-Minute VWAP | 2 | Yes | 30-minute closing VWAP. |
| `get_vwap_24h` | 24-Hour VWAP | 2 | Yes | Fixed 24-hour VWAP. |
| `run_pre_trade_check` | Pre-Trade Sanity Check | 100 | Yes | Pass or caution verdict; never trades. |
| `get_market_brief` | Market Brief | 250 | Yes | Up to 8 instruments with provenance receipt. |
| `create_price_receipt` | Price Receipt | 250 | Yes | One price with an audit-grade receipt. |
| `get_token_quality` | Token Quality Indicator | 500 | Yes | Market-quality score for one token. |
| `get_state_divergence` | State Divergence Indicator | 500 | Yes | AMM state versus VWAP, in basis points. |
| `get_macro_snapshot` | Macro Snapshot | 1000 | Yes | Up to 12 instruments in one call. |
| `get_solana_token_brief` | Solana Token Brief | 1000 | Yes | Signal brief for up to 10 Solana tokens. |
| `get_trader_alpha_pack` | Trader Alpha Pack | 2500 | Yes | Combined trader signal package, up to 12 symbols. |

Free-tier pacing matters for a review run: the free tier admits a call above 60
credits only after an idle 60-second window, so the seven workflow and indicator
tools between 100 and 1000 credits run one per idle minute, and
`get_trader_alpha_pack` needs a fresh UTC day because of the 4,000-credit daily
soft cap.

## Review Prompt Examples

Prompts submitted as review examples:

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

## Screenshots and Promotional Materials

Screenshot upload:

```text
Upload these real Claude.ai PNG screenshots captured after connecting the live
Blocksize MCP custom connector:

deliverables/claude_screenshots/claude_blocksize_01_btc_instruments.png
deliverables/claude_screenshots/claude_blocksize_02_remaining_credits.png
deliverables/claude_screenshots/claude_blocksize_03_btc_usd_vwap.png
deliverables/claude_screenshots/claude_blocksize_04_eurusd_fx.png
deliverables/claude_screenshots/claude_blocksize_05_xauusd_metal.png
```

Link to Promotional Materials:

```text
https://mcp.blocksize.info/claude-connector
```

Local promotional packet:

```text
deliverables/claude_promotional_materials/
```

Suggested promotional prompt/screenshot pairs:

```text
1. Search Blocksize for BTC market data instruments.
2. Show my remaining Blocksize data credits.
3. Get the latest BTC-USD VWAP from Blocksize.
4. Get the current EURUSD FX snapshot.
5. Get the latest XAUUSD metal price.
```

## Reviewer Test Account

No reviewer account was pre-created. Sign-up is open: the reviewer creates their
own Blocksize account during the OAuth flow with an email code from Clerk and
receives the standard free-tier allowance. Keep Clerk Device Trust off while the
review is open so that flow stays a single email-code step.

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

Result on 2026-10-14: 17 of 18 tools ran from Claude and returned correct
results. `get_trader_alpha_pack` only returned `FREE_TIER_DAILY_CAP_REACHED`
and is not yet verified end to end; rerun it on a fresh UTC day.

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
  - `ROOT_OAUTH_CONNECTOR=anthropic`
- [x] Confirm Clerk OAuth app callback allowlist:
  - `https://mcp.blocksize.info/anthropic/mcp/auth/callback`
- [x] Confirm persistent entitlement storage path/volume for the main service.
- [x] Confirm standalone Claude beta service uses OAuth and persistent
  entitlement storage.
- [x] Prepare optional Claude Code/Cowork plugin package:
  - `claude-plugin/blocksize-market-data`
- [x] Reviewer access: open self sign-up through Clerk (no pre-created account).
- [x] Test as a Claude custom connector (17 of 18 tools verified on 2026-10-14).
- [x] Add the tool title inside tool annotations (PR #106, commit `10d45c4`).
- [x] Submit through https://claude.ai/directory/manage on 2026-10-14.
- [ ] Verify `get_trader_alpha_pack` end to end on a fresh UTC day.
- [ ] Await Anthropic's decision; keep Device Trust, free-tier limits, public
      URLs and tool names unchanged until then.
- [ ] After the decision: fix the `get_bid_ask` description (`AAPLXUSD` versus
      the live `AAPLUSD` symbol) and re-enable Clerk Device Trust.
- [ ] Optional: validate and submit the plugin through Claude's plugin form.
