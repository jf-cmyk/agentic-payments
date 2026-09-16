# Account-linked agent registration

This release implements the Auth.md `service_auth` profile on the Anthropic Clerk
connector. Public MCP discovery and accountless signed x402 payments are unchanged.
It accepts no anonymous registration and trusts no external ID-JAG issuers.

## Enable in staging first

Set `AGENT_AUTH_ENABLED=true`, a new environment-specific `AGENT_AUTH_SECRET`
(at least 32 cryptographically random bytes encoded as base64url), and
`AGENT_AUTH_DB_PATH=/data/agent_auth.sqlite3` on the existing persistent volume.
Never reuse the OAuth signing key. Keep the secret stable across deployments;
rotation invalidates encrypted records, so disable registration and replace the
agent database before rotating. Back up the volume and retain the old secret with
its backup. Do not print secrets or put them in checked-in files.

The Anthropic connector must use Clerk with `email profile` scopes (optional
`openid`). Missing configuration fails startup when explicitly enabled. When the
feature is disabled, routes and agent_auth metadata are absent. The ordinary OAuth
flow remains available. This SQLite implementation requires a single service
instance sharing one persistent database; do not scale to independent volumes.

No new Clerk upstream callback is needed: the existing OAuth proxy handles that
callback. The internal consent client adds exactly
`<ANTHROPIC_MCP_PUBLIC_URL>/agent/callback` to the local redirect allowlist.

## Human acceptance check before production enablement

1. POST to `<connector>/agent/identity` with type `service_auth`, the owner's email,
   and a recognizable agent name. Keep the claim token private.
2. Open the returned verification URI. The owner signs in at Clerk, verifies the
   account email, enters the code shown to the agent, and explicitly approves
   spending existing account credits. Do not automate human consent.
3. Poll `<connector>/token` with the claim grant. Use the returned delegated token
   to initialize MCP and read the credit balance. Compare the ledger subject/balance
   against the same account's existing connector access. Do not make paid calls.
4. Open `<connector>/agent/agents` and revoke. Confirm the token gets 401 and its
   assertion can no longer be exchanged. Repeat with Deny, wrong email, expired code.
5. Run the hosted release audit and the unpaid Coinbase x402 validator. Enable
   production only after this authenticated staging check succeeds.

## Lifetimes and revocation

Claims expire after ten minutes, allow five wrong codes, and poll every five seconds.
Access and assertions expire within one hour and never outlive the underlying OAuth
token. No agent refresh token is issued. Registration is limited globally and by
email; token exchange is also limited. Email matching alone never grants access.

The encrypted SQLite store contains short-lived browser sessions, delegation
records, and 30-day audit events. Claim handles and session IDs are SHA-256 indexed;
all record bodies are encrypted. Logs redact claim handles, OAuth state and codes.
Cleanup runs when new registrations arrive; expired records are never accepted.

Token revocation at `<connector>/agent/revoke` invalidates that token. An unexpired
assertion can obtain a replacement token. Owner revocation at the management page
invalidates the entire delegation, including assertions. Every new MCP request
checks local revocation and the original Clerk-backed credential. Requests already
in progress can complete. No automatic purchase or balance refill is permitted.

## Rollback

Set `AGENT_AUTH_ENABLED=false` and redeploy to remove registration and reject all
`bsa_` tokens; retain the database for incident review. Existing connector OAuth and
signed x402 remain available. Do not delete or migrate the existing credit ledger.
