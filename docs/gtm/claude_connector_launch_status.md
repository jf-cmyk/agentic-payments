# Claude Connector Launch Status

Updated: 2026-10-14

## Current State

Blocksize Market Data is live as a Claude-compatible remote MCP custom connector
and was submitted to the Anthropic connector directory on 2026-10-14.

Status: **in review**.

| Item | Value |
| --- | --- |
| Submitted | 2026-10-14 via https://claude.ai/directory/manage |
| Portal name | Blocksize - Blockchain Market Data |
| Slug | `blocksize-blockchain-market-data` (permanent, cannot be changed) |
| Server URL | `https://mcp.blocksize.info/anthropic/mcp/` |
| Deployed build | commit `10d45c4` (PR #106: tool title inside tool annotations) |
| Tools exposed | 18, all read-only |

PR #106 added the tool title inside the tool annotations so that the portal's
"Missing title annotation" suggestion cleared before submission.

An optional Claude Code/Cowork plugin wrapper is prepared in:

```text
claude-plugin/blocksize-market-data
```

Use the remote MCP connector listing for Claude-wide discovery. Use the plugin
submission only for the complementary Claude Code/Cowork plugin track.

Public documentation:

```text
https://mcp.blocksize.info/claude-connector
```

## Rules While the Review Is Open

Do not change anything the reviewer may be testing against:

1. Keep Clerk Device Trust **off**. The reviewer signs up with an email code
   only; a device-trust challenge would block the OAuth flow.
2. Keep `FREE_TIER_PER_MINUTE_CREDITS` and `FREE_TIER_DAILY_SOFT_CAP_CREDITS`
   unchanged. The reviewer test instructions below are written against the
   current values (60-credit idle-minute admission, 4,000-credit daily soft cap).
3. Keep the three public URLs live and unchanged:
   `https://mcp.blocksize.info/claude-connector`,
   `https://mcp.blocksize.info/support`,
   `https://mcp.blocksize.info/privacy`.
4. Do not rename or remove tools. Adding a tool is also best deferred until the
   decision, because the portal snapshot lists the 18 tools below.

Re-enable Device Trust and revisit the free-tier limits only after Anthropic's
decision.

## Final Portal Copy

Name:

```text
Blocksize - Blockchain Market Data
```

One-liner:

```text
Read-only real-time crypto, equity ticker, FX and metals market data for Claude: VWAP, bid/ask, AMM state prices, market briefs, pre-trade checks and trader indicators.
```

Author:

```text
Blocksize Capital / https://blocksize.info
```

Links:

```text
Documentation: https://mcp.blocksize.info/claude-connector
Support:       https://mcp.blocksize.info/support
Privacy:       https://mcp.blocksize.info/privacy
```

Declarations:

```text
Read/write capability: read-only
API ownership:         first-party (Blocksize-controlled market-data infrastructure)
Health data:           none
Sponsored content:     none
```

## Authentication as Detected by the Portal

The portal probed the server and recorded:

```text
Authentication: always required
Flow:           OAuth 2.0 with Client ID Metadata Document
                (the server also supports Dynamic Client Registration)
PKCE:           S256
Scopes:         email, profile
Sign-up:        open self sign-up through Clerk with an email code
```

No reviewer credentials were supplied. The reviewer creates their own account
during the OAuth flow.

## Reviewer Test Instructions (as submitted)

```text
1. Add the connector and click Connect. Sign up during the OAuth flow with any
   email address; Clerk sends a one-time email code. No pre-created account is
   needed.

2. Free tools (no credits): search_pairs, list_instruments, get_credit_balance.
   Example: "Search Blocksize for BTC market data instruments."

3. Raw snapshot tools (2-5 credits each) may run back to back:
   get_vwap BTCUSD, get_bid_ask BTCUSD, get_bid_ask AAPL, get_fx_rate EURUSD,
   get_metal_price XAUUSD, get_vwap_30m BTCUSD, get_vwap_24h BTCUSD,
   get_state_price MSOLUSD.

4. Workflow and indicator tools must run ONE PER IDLE MINUTE. The free tier
   admits a call above 60 credits only after an idle 60-second window, so wait
   about a minute between these seven calls:
   run_pre_trade_check (100), create_price_receipt (250), get_market_brief (250),
   get_token_quality (500), get_state_divergence (500), get_macro_snapshot (1000),
   get_solana_token_brief (1000).

5. get_trader_alpha_pack (2500 credits) exceeds what remains of the 4,000-credit
   daily soft cap after step 4. Run it on the next UTC day, or run it first on a
   fresh day. When the cap is reached the tool returns
   FREE_TIER_DAILY_CAP_REACHED and charges nothing.

6. State tools (get_state_price, get_state_divergence, get_token_quality) use
   MSOLUSD. ETH-USD and SOL-USD have no AMM pool coverage.

7. Invalid input: ask for "../bad" VWAP. The connector returns INVALID_SYMBOL
   and charges no credits.
```

## Verification Record

Run from Claude on 2026-10-14 against the live connector, signed in through the
Clerk OAuth flow:

| Result | Tools |
| --- | --- |
| Correct results | 17 of 18: `search_pairs`, `list_instruments`, `get_credit_balance`, `get_vwap`, `get_bid_ask`, `get_fx_rate`, `get_metal_price`, `get_state_price`, `get_vwap_30m`, `get_vwap_24h`, `get_market_brief`, `run_pre_trade_check`, `create_price_receipt`, `get_macro_snapshot`, `get_token_quality`, `get_state_divergence`, `get_solana_token_brief` |
| Not yet verified end to end | `get_trader_alpha_pack` returned `FREE_TIER_DAILY_CAP_REACHED` only, because the other workflow tools had already consumed most of the 4,000-credit daily soft cap. |

Follow-up: run `get_trader_alpha_pack` on a fresh UTC day before the other
workflow tools and record the result here.

Open nit for a later PR (after the decision, since it touches a tool
description): `get_bid_ask`'s description cites `AAPLXUSD` as the equity
example, while the live response for `AAPL` returns the symbol `AAPLUSD`.

## Deployed Services

Main production service:

```text
https://mcp.blocksize.info
```

The standalone Claude beta service (`anthropic-mcp-beta`) was retired on
2026-09-30: it was frozen at 0.6.4, received no real traffic, and did not run
the 29-day Claude data retention job. The main production service is the only
Claude connector endpoint and the URL used for submission.

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
GET /.well-known/oauth-authorization-server -> Anthropic MCP issuer for Claude fallback discovery
GET /.well-known/oauth-protected-resource/cursor/mcp/ -> Cursor metadata unchanged
GET /openapi.json -> 200 for Pay.sh/OpenAPI consumers
```

Protocol check:

```bash
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

## Tool Inventory (as listed in the portal)

All 18 tools are defined in `src/authenticated_mcp_server.py`, carry
`readOnlyHint: true` and a title inside their annotations. Credits are the listed
free-tier price per call (1 credit = $0.001 USDC, the same amount each call costs
over x402).

| Tool | Title | Credits | Capability |
| --- | --- | ---: | --- |
| `search_pairs` | Instrument Search | 0 | Search supported crypto, equity ticker, FX and metal instruments by symbol or name. Metadata only. |
| `list_instruments` | Instrument List | 0 | List supported instruments for one Blocksize service. Metadata only. |
| `get_credit_balance` | Credit Balance | 0 | Show the signed-in user's remaining free-tier live-data credits. |
| `get_vwap` | Crypto VWAP Snapshot | 2 | Latest institutional crypto VWAP for one pair (4 for long-tail pairs). |
| `get_bid_ask` | Bid Ask Snapshot | 2 | Latest bid, ask and spread for one crypto pair or supported equity ticker (4 long-tail, 8 equities). |
| `get_fx_rate` | FX Snapshot | 5 | Latest bid, ask and mid rate for one FX pair. |
| `get_metal_price` | Metal Snapshot | 5 | Latest spot price for one supported metal ticker. |
| `get_state_price` | AMM State Price | 2 | Pool-derived AMM state price for one covered crypto pair, such as MSOLUSD (4 long-tail). |
| `get_vwap_30m` | 30-Minute VWAP | 2 | 30-minute closing VWAP for one crypto pair (4 long-tail). |
| `get_vwap_24h` | 24-Hour VWAP | 2 | Fixed 24-hour VWAP for one crypto pair (4 long-tail). |
| `run_pre_trade_check` | Pre-Trade Sanity Check | 100 | Freshness, spread and deviation check for one instrument with a pass or caution verdict. Never places a trade. |
| `get_market_brief` | Market Brief | 250 | Decision-ready brief for up to 8 instruments with freshness, spread, quality flags and a provenance receipt. |
| `create_price_receipt` | Price Receipt | 250 | One live price with an audit-grade receipt: request and response hashes, source endpoints, public lookup URL. |
| `get_token_quality` | Token Quality Indicator | 500 | Market-quality score for one crypto token from price, bid/ask, state and VWAP-window feeds. |
| `get_state_divergence` | State Divergence Indicator | 500 | AMM state price versus market VWAP divergence for one token, in basis points. |
| `get_macro_snapshot` | Macro Snapshot | 1000 | Snapshot of up to 12 instruments across crypto, FX and metals in one call. |
| `get_solana_token_brief` | Solana Token Brief | 1000 | Solana-oriented signal brief for up to 10 tokens. |
| `get_trader_alpha_pack` | Trader Alpha Pack | 2500 | Token quality, state divergence and VWAP windows combined for up to 12 symbols. |

Resources:

```text
blocksize://anthropic-info
```

Prompts:

```text
No MCP prompts are exposed by this connector.
```

Policy statement (as submitted):

```text
This Claude connector is read-only. It does not transfer money, cryptocurrency,
or other financial assets; does not execute financial transactions; does not
place trades or orders; does not submit wallet signatures; and does not submit
x402 payment proofs. Blocksize's x402 paid HTTP API is a separate integration
surface and is not exposed as a Claude MCP tool.
```

## Promotional Materials

Promotional link submitted:

```text
https://mcp.blocksize.info/claude-connector
```

Local screenshot and promotional packets:

```text
deliverables/claude_screenshots/
deliverables/claude_promotional_materials/
```

## Optional Claude Plugin Track

Plugin package:

```text
claude-plugin/blocksize-market-data
```

Plugin submission packet:

```text
docs/gtm/claude_plugin_submission/README.md
```

Plugin forms:

```text
https://claude.ai/settings/plugins/submit
https://platform.claude.com/plugins/submit
```

Plugin positioning:

```text
This plugin wraps the Blocksize remote MCP connector with setup instructions and
a market-data workflow skill for Claude Code and Cowork. It points to the same
read-only OAuth-protected endpoint submitted to the connector directory.
```
