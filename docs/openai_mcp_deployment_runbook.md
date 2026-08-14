# OpenAI and ChatGPT MCP deployment runbook

The authenticated OpenAI connector is mounted at:

```text
https://mcp.blocksize.info/openai/mcp/
```

It exposes the same read-only live tools as Claude and Cursor: instrument
search, instrument lists, credit balance, VWAP, bid/ask, FX, and metals.

## Production configuration

```sh
OPENAI_MCP_PUBLIC_URL=https://mcp.blocksize.info/openai/mcp
OPENAI_AUTH_PROVIDER=clerk
OPENAI_ENABLE_BETA_TOKENS=false
OPENAI_DAILY_CREDITS=50
OPENAI_ENTITLEMENT_DB_PATH=/data/openai_entitlements.db
OPENAI_OAUTH_REDIRECT_PATH=/auth/callback
OPENAI_OAUTH_JWT_SIGNING_KEY=<long-random-secret>
OPENAI_OAUTH_STORAGE_DIR=/data/openai_oauth
OPENAI_OAUTH_SCOPES=openid,email,profile,offline_access
OPENAI_BETA_TOKENS=
```

During ChatGPT app setup, copy the exact connector-specific callback URL shown
by ChatGPT and add it to `OPENAI_ALLOWED_CLIENT_REDIRECT_URIS`. Keep loopback
redirects only for local testing. `offline_access` is intentional so the OAuth
provider can issue refresh tokens for persistent connectivity.

Set `ROOT_OAUTH_CONNECTOR=openai` only on a host dedicated to this connector.
The shared production host can keep its existing root metadata setting because
the OpenAI path also publishes path-scoped OAuth metadata.

## Entitlement identity and rollback continuity

Keep `OPENAI_ENTITLEMENT_DB_PATH` stable and isolated from the Claude and Cursor
ledgers. Current releases bind the OpenAI/issuer/audience-scoped principal in
`identity_aliases` while continuing to own balances and charges under the raw
`user_id` read by v0.6.2. The mapping is additive and one-to-one, so an upgrade
does not create another starter allowance and a rollback sees candidate usage.

Do not delete or rewrite identity bindings. If the issuer or audience changes,
a different scoped principal claims an existing raw ID, or an older candidate
already created a scoped balance row, the ledger fails closed. Back up the
database and reconcile the identity deliberately before restoring access.

## Acceptance sequence

1. Confirm `/health` lists `openai_connector` and the expected MCP URL.
2. Confirm the path-scoped protected-resource and authorization-server metadata.
3. In ChatGPT developer mode, create an app with the OpenAI MCP URL and scan tools.
4. Complete OAuth and call `get_credit_balance`.
5. Call `get_vwap` for `BTCUSD`; verify a timestamped observation and a one-credit decrease.
6. Force one provider failure; verify the credit is refunded and the dashboard records `mcp_tool_error`, not `mcp_data_delivered`.
7. Reopen the app after token expiry to verify refresh-token continuity.

Do not publish the app until successful and refunded-failure paths reconcile in
the production command center.
