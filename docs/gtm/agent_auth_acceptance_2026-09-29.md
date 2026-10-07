# Agent registration: production acceptance test, 2026-09-29

Owner account: jf@blocksize-capital.com (Clerk). Connector: https://mcp.blocksize.info/anthropic/mcp/.
Claude ran the API side; Johann did every sign-in and decision in a browser. No paid calls.

## Fixes found by the test (all merged and live)

| PR | Failure seen | Fix |
|---|---|---|
| #49 | `verified_account_required` with no detail | Response and log now name the failed check |
| #50 | `token_without_expiry` | Clerk introspection has no expiry; session is bounded by the issued token lifetime (max 1 h) |
| #51 | `invalid_csrf` on the consent form | Pages served with `Referrer-Policy: same-origin`; `no-referrer` made browsers send `Origin: null` |

## Results (production commit 021e8ed)

| Case | Result |
|---|---|
| Owner sign-in, code, approve | Approved; token expires in 3587 s, scope `email profile` |
| Agent reads `get_credit_balance` over MCP | 15,000 / 15,000 credits, shared pool active, `Data by Blocksize` attribution present |
| Agent self-revokes its token | MCP returns 401 afterwards |
| Identity assertion after self-revoke | Exchanges for a replacement token (by design) |
| Owner revokes on `/agent/agents` | Replacement token 401; assertion exchange refused (400) |
| Deny on the Blocksize consent page | `access_denied` at the claim grant |
| Deny on Clerk's own consent screen | Clerk returns `access_denied`; no session, no access |
| Wrong code, once | Registration stays pending, no access, expires after 10 min |
| Expired code (three registrations left unused) | `expired_token` at the claim grant |

Rate limits observed: 10 registrations per email per hour; 8 used during the test.

## Follow-ups

- Rename the Clerk OAuth application from "Blocksize Cursor MCP" to "Blocksize Market Data"; its name shows on every connector and agent sign-in.
- Owner's own connector balance was not compared side by side (Johann did not send the line); the agent read the same shared pool the free tier uses, at its full 15,000.
