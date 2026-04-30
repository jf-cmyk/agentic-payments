# Anthropic MCP Connector Runbook

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
- OAuth provider support for public Claude custom connectors.

Use Clerk for the first public launch. FastMCP's Clerk provider supports OAuth
proxying with DCR/CIMD, which aligns with Claude's public connector flow.

Before directory submission:

- Configure Clerk OAuth and disable beta-token fallback.
- Confirm a persistent Railway volume or managed database for entitlement state.
- Prepare a fully populated test account for Anthropic reviewers.
- Publish a short public help page or blog post for setup/support.

## Railway Variables

Minimum public OAuth deployment variables:

```text
ANTHROPIC_ONLY_MODE=true
ANTHROPIC_DAILY_CREDITS=50
ANTHROPIC_ENTITLEMENT_DB_PATH=/data/anthropic_entitlements.db
ANTHROPIC_MCP_PUBLIC_URL=https://anthropic-mcp-beta-production.up.railway.app/anthropic/mcp
ANTHROPIC_AUTH_PROVIDER=clerk
ANTHROPIC_ENABLE_BETA_TOKENS=false
ANTHROPIC_OAUTH_REDIRECT_PATH=/auth/callback
ANTHROPIC_OAUTH_JWT_SIGNING_KEY=<long-random-secret>
ANTHROPIC_ALLOWED_CLIENT_REDIRECT_URIS=https://claude.ai/api/mcp/auth_callback,http://localhost:*,http://127.0.0.1:*
CLERK_DOMAIN=<your-clerk-domain>
CLERK_CLIENT_ID=<your-clerk-client-id>
CLERK_CLIENT_SECRET=<your-clerk-client-secret>
ANTHROPIC_BETA_TOKENS=
```

Register this callback URL in the Clerk OAuth application:

```text
https://anthropic-mcp-beta-production.up.railway.app/anthropic/mcp/auth/callback
```

If the public MCP URL changes, the Clerk callback must change with it. For
example, using `https://mcp.blocksize.info/anthropic/mcp` requires:

```text
https://mcp.blocksize.info/anthropic/mcp/auth/callback
```

Auth0 is also supported if needed:

```text
ANTHROPIC_AUTH_PROVIDER=auth0
ANTHROPIC_ENABLE_BETA_TOKENS=false
AUTH0_CONFIG_URL=https://<tenant>/.well-known/openid-configuration
AUTH0_CLIENT_ID=<auth0-client-id>
AUTH0_CLIENT_SECRET=<auth0-client-secret>
AUTH0_AUDIENCE=<auth0-api-audience>
```

Supabase is supported for private testing, but Clerk is preferred for the first
public launch because Supabase's FastMCP provider warns about resource-specific
audience validation limitations.

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

OAuth mode check:

1. Open Claude and add the custom connector URL.
2. Complete the Clerk consent flow.
3. Ask Claude to call `get_credit_balance`.
4. Ask Claude to call `get_vwap` for `BTCUSD`.
5. Confirm `/health` reports:

   ```text
   auth_provider=clerk
   beta_tokens_enabled=false
   daily_credits=50
   tool_surface=read-only
   ```

Expected behavior:

- Tool discovery succeeds.
- `search_pairs` and `list_instruments` work without auth.
- `get_credit_balance` and live data tools return `AUTH_REQUIRED` without auth.
- Authenticated credit balance returns the configured beta user.
- Invalid symbols return `INVALID_SYMBOL` and do not spend credits.
- Paid live calls return market data and include remaining daily credits.

## Claude Connector Path

For a private Messages API integration, beta tokens can still be passed as the
MCP server `authorization_token`. Current Anthropic API examples should use the
`anthropic-beta: mcp-client-2025-11-20` header.

For Claude.ai custom connectors and public consumption, use OAuth:

1. Configure Clerk in Railway.
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

- Set `ANTHROPIC_AUTH_PROVIDER=clerk`.
- Set `ANTHROPIC_ENABLE_BETA_TOKENS=false`.
- Remove old beta tokens from `ANTHROPIC_BETA_TOKENS`.
- Redeploy after auth changes.
- Verify old tokens fail with `--expect-auth-fail`.
- Keep `ANTHROPIC_DAILY_CREDITS=50`.
- Attach a Railway volume at `/data` or move entitlements to a managed database.
