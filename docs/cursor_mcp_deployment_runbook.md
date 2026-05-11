# Cursor MCP Deployment Runbook

Prepared: 2026-05-11

Scope: Cursor setup only. Pay.sh, `pay-skills`, Smithery, Glama, and the
official MCP Registry are separate tracks and should not drive the Cursor
deployment shape.

## Goal

Ship a Cursor-specific Blocksize MCP setup that uses the same deployed
Blocksize server codebase, but a Cursor-specific access path and signup flow.

The Cursor launch should let a Cursor user install Blocksize Market Data, sign
in with Blocksize through Clerk when required, receive a daily live-data credit
allowance, and use read-only market-data tools inside Cursor.

## Surface Separation

Keep these surfaces distinct:

| Surface | Access path | Purpose | Auth/payment model |
| --- | --- | --- | --- |
| Public MCP registries | `/mcp/server/` | Free discovery, docs, pricing, endpoint builder | No auth, no live data |
| x402 HTTP API | `/v1/*` paid endpoints | Live production data | x402 proof or wallet credits |
| Pay.sh | filtered Pay.sh OpenAPI + paid HTTP endpoints | Pay.sh catalog and PR path | Pay.sh/MPP/x402 probe path |
| Claude | `/anthropic/mcp/` | Claude connector with daily credits | Clerk OAuth, no wallet payments inside MCP |
| Cursor | `/cursor/mcp/` | Cursor plugin with daily credits | Clerk OAuth, no wallet payments inside MCP |

The important product line:

- `/mcp/server/` remains a public discovery MCP for directories.
- `/cursor/mcp/` becomes the authenticated Cursor product surface.
- `/anthropic/mcp/` remains the authenticated Claude product surface.
- Pay.sh continues to use HTTP paid endpoints, not the Cursor MCP package.

## Current State

Ready now:

- Hosted public MCP discovery endpoint:
  `https://mcp.blocksize.info/mcp/server/`
- Local Cursor authenticated MCP mount:
  `/cursor/mcp`
- Cursor plugin scaffold:
  `blocksize-cursor-plugin/`
- Cursor plugin repository:
  `https://github.com/jf-cmyk/blocksize-cursor-plugin`
- Connector-neutral Clerk/OAuth-capable auth helpers:
  `src/connector_auth.py`, `src/cursor_auth.py`, and `src/anthropic_auth.py`
- Shared authenticated MCP live-data surface:
  `src/authenticated_mcp_server.py`
- Daily user credit ledger:
  `src/entitlement_manager.py`
- Live deployment currently initializes `/anthropic/mcp/` and `/mcp/server/`.

Not ready yet:

- `/cursor/mcp/` has not been deployed and QA'd in Cursor yet.
- The Cursor plugin currently points at public discovery `/mcp/server/`, so it
  does not trigger Clerk signup or expose daily-credit live-data tools.
- The Cursor plugin should not be switched to `/cursor/mcp/` until deployed
  OAuth QA passes.

Local verification:

```bash
.venv/bin/python -m pytest tests/ -q
.venv/bin/python -m ruff check src tests
```

## Clerk Signup Evaluation

The previous Clerk plan is still the right plan for Cursor, with one adjustment:
do not reuse the Claude-named `/anthropic/mcp/` endpoint for Cursor.

Cursor supports remote MCP over Streamable HTTP and supports OAuth for remote
servers that require it. FastMCP's OAuth proxy/Clerk provider is a good fit
because it can bridge Clerk into MCP OAuth with dynamic client registration.

Recommended model:

1. Cursor installs a remote MCP server URL.
2. Cursor sees that `/cursor/mcp/` requires OAuth.
3. Cursor starts the MCP OAuth flow.
4. FastMCP proxies that flow to Clerk.
5. User signs up or signs in with Blocksize in Clerk.
6. Cursor stores the resulting MCP access token.
7. Live-data tools spend server-side daily credits keyed by Clerk user identity.

The MCP tools should remain read-only. Cursor should not execute wallet
transactions, submit x402 proofs, or trigger paid HTTP calls automatically from
the authenticated MCP surface.

## Target Cursor Plugin Config

Do not switch the published Cursor plugin to this URL until `/cursor/mcp/`
exists and passes OAuth QA.

```json
{
  "mcpServers": {
    "blocksize-market-data": {
      "url": "https://mcp.blocksize.info/cursor/mcp/"
    }
  }
}
```

For a private pre-launch test, a separate staging name is useful:

```json
{
  "mcpServers": {
    "blocksize-market-data-staging": {
      "url": "https://mcp.blocksize.info/cursor/mcp/"
    }
  }
}
```

## Required Code Work

1. Extract the authenticated daily-credit MCP surface from the
   Anthropic-specific module into connector-neutral helpers. Done locally.

   Suggested shape:

   - `src/connector_auth.py`
   - `src/authenticated_mcp_server.py`
   - keep `src/anthropic_auth.py` and `src/anthropic_mcp_server.py` as wrappers
     or compatibility imports until Claude tests are migrated.

2. Add a Cursor FastMCP app with Cursor-specific instructions and auth config.
   Done locally.

   Target mount:

   ```text
   /cursor/mcp
   ```

3. Keep the tool surface aligned with the Claude authenticated surface:

   - `search_pairs`
   - `list_instruments`
   - `get_credit_balance`
   - `get_vwap`
   - `get_bid_ask`
   - `get_fx_rate`
   - `get_metal_price`

4. Keep public discovery tools separate:

   - Public Cursor discovery plugin may point at `/mcp/server/` only during
     pre-auth preview.
   - The Clerk launch plugin should point at `/cursor/mcp/`.

## Environment Variables

Use Cursor-specific env names for the Cursor product surface.

```text
CURSOR_MCP_PUBLIC_URL=https://mcp.blocksize.info/cursor/mcp
CURSOR_AUTH_PROVIDER=clerk
CURSOR_ENABLE_BETA_TOKENS=false
CURSOR_OAUTH_REDIRECT_PATH=/auth/callback
CURSOR_OAUTH_JWT_SIGNING_KEY=<long-random-secret>
CURSOR_OAUTH_SCOPES=email profile
CURSOR_ALLOWED_CLIENT_REDIRECT_URIS=*
CLERK_DOMAIN=<your-clerk-domain>
CLERK_CLIENT_ID=<your-clerk-client-id>
CLERK_CLIENT_SECRET=<your-clerk-client-secret>
CURSOR_BETA_TOKENS=
```

For private Cursor OAuth QA, start with:

```text
CURSOR_ALLOWED_CLIENT_REDIRECT_URIS=*
```

That allows Cursor's MCP client registration to complete even if its exact
redirect URI shape changes. After the first successful login, inspect server
logs for registered redirect URIs and replace `*` with a tighter allowlist.

For the upstream Clerk OAuth app, register:

```text
https://mcp.blocksize.info/cursor/mcp/auth/callback
```

Entitlement storage should be deliberate:

- Shared credit pool across Claude and Cursor:
  `/data/blocksize_connector_entitlements.db`
- Isolated Cursor beta:
  `/data/cursor_entitlements.db`

Use shared storage for the public product if the same Clerk account should have
one daily credit balance across Claude and Cursor.

## Cursor QA Checklist

Run this after `/cursor/mcp/` is deployed.

1. Add the staging config to `~/.cursor/mcp.json` or a test repo's
   `.cursor/mcp.json`.
2. In Cursor settings, confirm the MCP server appears and asks for login.
3. Complete Clerk signup/login.
4. Confirm tools list includes the seven authenticated tools.
5. Ask Cursor:

   ```text
   Use Blocksize to search for BTC instruments.
   ```

   Expected: metadata-only result, no credit spend.

6. Ask Cursor:

   ```text
   Use Blocksize to show my remaining data credits.
   ```

   Expected: authenticated credit status.

7. Ask Cursor:

   ```text
   Use Blocksize to get BTCUSD VWAP.
   ```

   Expected: live read-only VWAP and daily credits decremented by one.

8. Ask Cursor:

   ```text
   Use Blocksize to get ../bad VWAP.
   ```

   Expected: `INVALID_SYMBOL`, no credit spend.

9. Confirm Cursor never sees wallet private keys, payment proofs, or x402
   transaction instructions from the authenticated MCP surface.

## Deployment Sequence

1. Keep the current public plugin on `/mcp/server/` until Cursor OAuth QA passes.
2. Deploy `/cursor/mcp/` behind the same public domain.
3. Configure Clerk callback for `/cursor/mcp/auth/callback`.
4. Run private Cursor OAuth QA.
5. Update `blocksize-cursor-plugin/mcp.json` to `/cursor/mcp/`.
6. Update the Cursor plugin README to present Clerk signup as the default launch
   path.
7. Tag and publish the Cursor plugin update.

## Launch Positioning

Cursor copy should say:

> Blocksize Market Data gives Cursor agents read-only access to supported
> crypto, equity ticker, FX, and metals market data. Sign in with Blocksize to
> use daily live-data credits. For production paid HTTP access, agents can
> discover x402 endpoint URLs, but payments happen outside the Cursor MCP tools.

Avoid saying that the Cursor plugin itself executes x402 payment or wallet
transactions.
