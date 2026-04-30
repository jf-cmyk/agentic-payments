# Anthropic MCP Beta Connector Runbook

This runbook covers the Anthropic-safe Blocksize MCP endpoint:

```text
https://anthropic-mcp-beta-production.up.railway.app/anthropic/mcp/
```

The endpoint is intentionally separate from the main GitLab-backed production
deployment. It reuses the same codebase and exposes only read-only market data
tools with server-side daily credits.

## Current Readiness

Ready now:

- Public HTTPS Railway endpoint.
- Streamable HTTP MCP transport at `/anthropic/mcp/`.
- Read-only tool surface:
  - `search_pairs`
  - `list_instruments`
  - `get_credit_balance`
  - `get_vwap`
  - `get_bid_ask`
  - `get_fx_rate`
  - `get_metal_price`
- Default daily limit of 50 credits per user per UTC day.
- Beta-token authentication for private API testing.
- OAuth provider hooks for Clerk, Auth0, or Supabase.

Not ready for wider Claude.ai custom-connector use until complete:

- Rotate out any pasted or shared beta tokens.
- Configure a real OAuth provider so each Claude user has their own identity.
- Confirm a persistent Railway volume or managed database if beta state must
  survive container replacement.

## Railway Variables

Minimum beta deployment variables:

```text
ANTHROPIC_ONLY_MODE=true
ANTHROPIC_DAILY_CREDITS=50
ANTHROPIC_ENTITLEMENT_DB_PATH=anthropic_entitlements.db
ANTHROPIC_MCP_PUBLIC_URL=https://anthropic-mcp-beta-production.up.railway.app/anthropic/mcp
ANTHROPIC_AUTH_PROVIDER=none
ANTHROPIC_BETA_TOKENS={"<random-token>":{"user_id":"johann-beta-001","email":"jf@blocksize-capital.com"}}
```

Use `ANTHROPIC_AUTH_PROVIDER=none` only for the closed beta-token phase. For
Claude.ai custom connectors, use one OAuth provider:

```text
ANTHROPIC_AUTH_PROVIDER=clerk
CLERK_DOMAIN=<your-clerk-domain>
CLERK_CLIENT_ID=<your-clerk-client-id>
CLERK_CLIENT_SECRET=<your-clerk-client-secret-if-required>
```

or:

```text
ANTHROPIC_AUTH_PROVIDER=auth0
AUTH0_CONFIG_URL=https://<tenant>/.well-known/openid-configuration
AUTH0_CLIENT_ID=<auth0-client-id>
AUTH0_CLIENT_SECRET=<auth0-client-secret>
AUTH0_AUDIENCE=<auth0-api-audience>
```

or:

```text
ANTHROPIC_AUTH_PROVIDER=supabase
SUPABASE_PROJECT_URL=https://<project-ref>.supabase.co
```

## Smoke Tests

Public no-auth check:

```bash
.venv/bin/python scripts/test_anthropic_mcp_connector.py
```

Private beta-token check without spending live-data credits:

```bash
BETA_TOKEN="<current-beta-token>" .venv/bin/python scripts/test_anthropic_mcp_connector.py
```

Rotated-token check. This should pass only when the old token is blocked:

```bash
BETA_TOKEN="<old-token>" .venv/bin/python scripts/test_anthropic_mcp_connector.py --expect-auth-fail
```

Full beta-token check that spends up to 6 credits:

```bash
BETA_TOKEN="<current-beta-token>" .venv/bin/python scripts/test_anthropic_mcp_connector.py --spend-live
```

Expected behavior:

- Tool discovery succeeds.
- `search_pairs` and `list_instruments` work without auth.
- `get_credit_balance` and live data tools return `AUTH_REQUIRED` without auth.
- Authenticated credit balance returns the configured beta user.
- Invalid symbols return `INVALID_SYMBOL` and do not spend credits.
- Paid live calls return market data and include remaining daily credits.

## Claude Connector Path

For a quick Messages API integration, use the beta-token phase by passing the
token as the MCP server `authorization_token`. Current Anthropic API examples
should use the `anthropic-beta: mcp-client-2025-11-20` header.

For Claude.ai custom connectors, prefer OAuth:

1. Configure Clerk, Auth0, or Supabase in Railway.
2. Redeploy the beta service.
3. Allow the Claude OAuth callback URL in the provider if the provider requires
   explicit callback allowlisting:

   ```text
   https://claude.ai/api/mcp/auth_callback
   ```

4. Run the smoke tests.
5. In Claude, add a custom connector using:

   ```text
   https://anthropic-mcp-beta-production.up.railway.app/anthropic/mcp/
   ```

6. Complete the OAuth connection flow in Claude.
7. Ask Claude to call `get_credit_balance`, then a low-cost live tool such as
   `get_vwap` for `BTCUSD`.

## Release Blockers

Before adding more users:

- Remove old beta tokens from `ANTHROPIC_BETA_TOKENS`.
- Redeploy after token changes.
- Verify old tokens fail.
- Keep `ANTHROPIC_DAILY_CREDITS=50`.
- Decide whether the beta entitlement database can be ephemeral. If not, attach
  a Railway volume or move entitlements to a managed database.
