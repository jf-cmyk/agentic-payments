# Clerk production cutover, 2026-09-30

The Claude, Cursor and ChatGPT connectors on `https://mcp.blocksize.info` sign users in
through Clerk. Until this cutover they used the Clerk **development** instance
`guided-stag-57.clerk.accounts.dev`, which is capped at 100 users, prefixes its emails
with "development", and is not meant for production traffic. This runbook moves them to
a Clerk **production** instance.

No secrets are recorded here.

## Findings on the development instance

- 3 users in total: two owner addresses and the Anthropic reviewer account
  (`mcp-review@anthropic.com`). Nobody else had signed up, which is why the usage report
  showed 0 free-tier grants.
- The signup webhook pointed at `https://mcp.blocksize.info/` (the site root) instead of
  `/internal/clerk/webhook`, subscribed to all 37 events, and had never delivered a
  message. Signup alerts could not have arrived.
- The OAuth application ("Blocksize Cursor MCP") had redirect URIs for Claude and Cursor
  only; the ChatGPT callback was missing.

## Production instance

| Item | Value |
|---|---|
| Clerk app | Blocksize Market Data (`app_3D6DMBLMk8mG40cw97fqzvvIM1f`) |
| Production instance | `ins_3K3uqa0oge9gfrGAFlH45obdqTO`, cloned from development |
| Application domain | `mcp.blocksize.info`, set up as a secondary application |
| Frontend API (`CLERK_DOMAIN`) | `clerk.mcp.blocksize.info` |
| Account portal | `accounts.mcp.blocksize.info` |
| Sign-in methods | Email with verification code, password. Google is disabled (needs own Google Cloud credentials; revisit later) |
| OAuth application | "Blocksize Market Data", client ID `MxnNZ2QJ3Nw5bCVm`, confidential, consent screen on, scopes `email profile offline_access` |
| Redirect URIs | `https://mcp.blocksize.info/anthropic/mcp/auth/callback`, `.../cursor/mcp/auth/callback`, `.../openai/mcp/auth/callback` |
| Webhook | `https://mcp.blocksize.info/internal/clerk/webhook`, event `user.created` only |

### DNS (Netlify DNS, zone `blocksize.info`)

| Name | CNAME value |
|---|---|
| `clerk.mcp` | `frontend-api.clerk.services` |
| `accounts.mcp` | `accounts.clerk.services` |
| `clkmail.mcp` | `mail.b8ji4b4b3emv.clerk.services` |
| `clk._domainkey.mcp` | `dkim1.b8ji4b4b3emv.clerk.services` |
| `clk2._domainkey.mcp` | `dkim2.b8ji4b4b3emv.clerk.services` |

All five were added and verified by Clerk on 2026-09-30.

## Switching Railway

The production service reads four variables. Change them together; the last command
without `--skip-deploys` triggers the redeploy.

| Variable | Production value |
|---|---|
| `CLERK_DOMAIN` | `clerk.mcp.blocksize.info` |
| `CLERK_CLIENT_ID` | `MxnNZ2QJ3Nw5bCVm` |
| `CLERK_CLIENT_SECRET` | From the OAuth application. Regenerate it before copying, because the value shown at creation was exposed in a screenshot |
| `CLERK_WEBHOOK_SECRET` | The production webhook endpoint's signing secret (`whsec_...`) |

Secrets are piped from the clipboard so they never appear on the command line:
`pbpaste | tr -d '\n' | railway variable set CLERK_CLIENT_SECRET --stdin --skip-deploys`.

## Rollback

Set the development values again and redeploy:

- `CLERK_DOMAIN=guided-stag-57.clerk.accounts.dev`
- `CLERK_CLIENT_ID=10GUTkmbnqbmSrkc`
- `CLERK_CLIENT_SECRET` and `CLERK_WEBHOOK_SECRET`: the development values, saved before
  the switch in `~/clerk-dev-backup-2026-09-30.json` (owner's machine, mode 600).

## Effects of the switch

- Users do not carry over between Clerk instances. Existing users sign up again and get
  new Clerk user IDs.
- Per-user identity (`principal_id`) hashes the issuer, audience and user ID
  (`src/connector_auth.py`), so entitlement rows keyed on it start fresh. The free-tier
  pool is keyed by a salted email hash (`src/free_tier.py`), so free-tier usage carries
  over for the same email.
- Existing Claude, Cursor and ChatGPT connections must reconnect.
- The Anthropic reviewer account must be recreated in production with the same email
  and password that were submitted to the connector directories.

## Cutover result

- Railway deployment `bb049f8c` (commit `afeff0e`) went live on 2026-09-30 with the four
  production values; it waited for GitHub CI first, and the older queued deployment was
  superseded.
- `/health` reports the Anthropic connector healthy with `oauth_available = true`.
- Smoke test for each connector (dynamic client registration, then `/authorize` without
  signing in): Claude, Cursor and ChatGPT all redirect to
  `https://clerk.mcp.blocksize.info/oauth/authorize` with client `MxnNZ2QJ3Nw5bCVm` and
  their own `/auth/callback`, and Clerk accepts each request
  (`302 -> /oauth/authorize/continue`). The test left a few inert client registrations
  named `cutover-smoke-test` in the OAuth client store.
- The account portal (`https://accounts.mcp.blocksize.info/sign-in`) shows "Sign in to
  Blocksize Market Data" with email sign-in only and no development banner.
- `CLERK_WEBHOOK_SECRET` was first overwritten by a stale clipboard (the reviewer email),
  so Clerk's deliveries got HTTP 500 (`webhook_secret_invalid`). It was reset through a
  guarded command that only accepts a `whsec_` value, and redeployed as `1fcd6283` on
  2026-10-01. Recovering the failed messages then returned HTTP 200: the operator signup
  alert was sent and the signup stored.
- `CLERK_CLIENT_SECRET` was also wrong on the first attempt: the first real sign-up
  created the Clerk user and sent the alert, then failed at the callback with
  `invalid_client` from Clerk's token endpoint. The secret was regenerated, set through a
  guarded command, and tested before deploying by calling
  `https://clerk.mcp.blocksize.info/oauth/token_info` with the client ID and stored
  secret (HTTP 200 means accepted, 401 means wrong). Redeployed as `8378faaf`.
- First production sign-in through the Claude connector succeeded on 2026-10-01:
  `/authorize` 302, `/auth/callback` 302, `/token` 200, then MCP requests 200.
- Free tier confirmed on the new identity: after one `get_vwap` call (2 credits) the
  owner account showed 29,998 of 30,000 credits, resetting on November 1.
- Still to do: reconnect and test the Cursor and ChatGPT connectors with a real sign-in
  (their `/authorize` redirects were verified, their token exchange was not).
- ChatGPT could not connect, for a reason that predates the cutover:
  `OPENAI_ALLOWED_CLIENT_REDIRECT_URIS` was unset, so the OpenAI surface only accepted
  loopback redirects and answered ChatGPT's `/authorize` with
  `Redirect URI ... does not match allowed patterns`. It is now set to
  `https://chatgpt.com/connector/oauth/*,https://chatgpt.com/connector_platform_oauth_redirect,http://localhost:*,http://127.0.0.1:*`
  (deployment `cedd8e4f`). The variable is not yet listed in `.railway/railway.ts`.
- ChatGPT's next attempt failed at Clerk with `The OAuth 2.0 Client is not allowed to
  request scope 'openid'`. The OpenAI surface requests `openid email profile
  offline_access`, but the Clerk OAuth application only had `email profile
  offline_access` (the development application had the same gap). `openid` was added to
  the production OAuth application's scopes on 2026-10-01. Clerk raises this error only
  after the user signs in, so the unauthenticated smoke test cannot detect it.
- Open issue, not caused by the cutover: the user welcome email is rejected by Resend
  (`http_403`). `USER_EMAIL_FROM` names a sender Resend will not send from, most likely
  because its domain is not verified in Resend.

## Post-switch checks

1. `https://clerk.mcp.blocksize.info/.well-known/openid-configuration` returns the
   production issuer.
2. Connect the Claude connector and complete sign-up with a fresh email; the sign-in
   page must not show a development banner.
3. The operator signup alert and the welcome email arrive; the Clerk webhook endpoint
   shows a successful `user.created` delivery.
4. A live tool call records `free_tier_grant_created`.
5. Repeat the connect step for Cursor and ChatGPT.
