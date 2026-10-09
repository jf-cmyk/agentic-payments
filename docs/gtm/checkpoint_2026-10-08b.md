# Checkpoint, 2026-10-08 (second session, written 05:10 UTC)

Start a new chat from this file. It supersedes `checkpoint_2026-10-08.md` (written a few hours earlier) and records what is live, what is in flight, what is waiting on whom, and how to continue.

## 1. Production state

| Item | Value |
|---|---|
| Version | 0.6.24, commit ebf1e80, healthy, `/readyz` ready |
| Merged this session, all deployed | #88 OpenAI server instructions without pricing · #92 bundle (facilitator resilience, MCP rate-limit scope, premium tool descriptions) · #93 OpenAI connector stateless · #94 transport-400 diagnostics · #95 accept newer MCP protocol versions · #96 listen GET on the stateless OpenAI connector |
| Open PR | **#97** closes the OpenAI listen stream after 2 s (`OPENAI_LISTEN_STREAM_MAX_SECONDS`); CI was running at the time of writing. Merge it first thing |
| Railway | auto-deploys main; `RAILPACK_VERSION=0.40.1` matches the tracked pin. Every variable change redeploys |
| Resend | `blocksize.info` Verified; sender test and a real welcome mail delivered on 7 Oct |
| Smithery | usage ingestion live (`SMITHERY_QUALIFIED_NAME=blocksize/agentic-payments`, API key set) |
| Claude plugin directory | plugin 0.6.0 published; MCP connector submission in Anthropic review |
| OpenAI plugin directory | **In review** (plugin `plugin_asdk_app_6ac6c3f81bb08191a09faa3cb61a2df3`, manifest 0.7.1). See section 2 |
| Clerk | **Device Trust is switched off** for the OpenAI reviewers; re-enable after OpenAI's decision (Protect, Rules, Device Trust, Manage) |

## 2. The OpenAI connector dashboard problem (the open thread)

Symptom: on the plugin's MCPs tab, "MCP configuration: Unavailable" and "Loading app details…" forever; no Rescan button; before that, "Complete MCP setup" with a reconnect loop.

What the server log showed, in order, and what was done:
1. OpenAI's discovery posts `tools/list` and `resources/list` without a session; the stateful transport answered 400 "Missing session ID" → #93 made `/openai/mcp/` stateless.
2. OpenAI's platform opens the listen `GET /openai/mcp/`; FastMCP serves a stateless app on POST/DELETE only → 405 → "Unavailable" → #96 answers an authenticated listen GET with an empty event stream.
3. The dashboard's app-details call (`GET https://api.openai.com/v1/dashapi/apps/asdk_app_…`) stays pending and **no request from OpenAI reaches our server at all** since #96 deployed (checked twice over 10 minutes). #97 shortens the stream to 2 s in case their backend waits for it to end, but the evidence says their side holds a stored failure state or is slow.

Next moves, in order:
- Merge #97, reload the MCPs tab (full URL with `?tab=mcp&version=appsub_6ac6c3f81bec8191a81c93820beb3426&mcp=server%3Ablocksize-market-data`).
- If still stuck: on the Metadata & Skills tab, "Upload new version" and re-upload `deliverables/blocksize-market-data-openai-plugin-0.7.1.zip` (unchanged); a re-upload started a fresh scan earlier in the day. Watch `railway logs` for POST/GET `/openai/mcp/` from 104.210.139.x / 9.129.45.x / 52.190.139.x and for `MCP transport 400` lines.
- If still stuck after that: contact OpenAI plugin support with the plugin id; review status itself is "In review" and the earlier tool findings are cleared.

Verified facts for that work: the transport-400 diagnostic (#94) logs method, JSON-RPC method, protocol version and a body excerpt for every transport 400, never headers. Session-less POSTs and an authenticated listen GET answer 200 on production (curl-checked). An unauthenticated GET gets 405.

## 3. Other findings from the diagnostics (first hour)

- Anthropic's Toolbox scanner (Claude connector review) sent `MCP-Protocol-Version: 2026-07-28` and got 400 from the pinned SDK (knows up to 2025-11-25). Fixed by #95 for all MCP apps. Its later probes show metadata reads and a 401 on the bare POST, no 400s.
- Public MCP: real clients post `tools/list` and `notifications/initialized` without ever initializing (several per minute). They now get the session hint 400; making the public MCP stateless would make them work. Not done: it changes `create_public_http_app` (idle cleanup, session hint) and its tests.
- `POST /openai/mcp/token` returned 401 seven times in an hour (token refresh). OAuth client storage is on `/data`, so deploys do not wipe registrations; cause not yet found. Related log lines: "Auth error returned: invalid_token" from FastMCP.

## 4. Numbers worth knowing (8 Oct, 03:00 UTC)

- Public MCP tool calls per day: 148 (6 Oct), 237 (7 Oct), 41 in the first three hours of 8 Oct. 30-day average 189.
- Since the client-error fixes: zero 406s on `/mcp/server`; recent window 118×200, 35×202, 10×400 (session hint).
- 429s: none in the last 14 days; the 3,342 in the 30-day window are from 8–23 September. The MCP transport now has its own budget (240/min, 5,000/day).
- Payment proofs: 7-day failure rate 12 percent (down from 29 over 30 days), mostly facilitator outages; #92 added fee-payer grace, probe retries with a last-good snapshot, Retry-After on both 503s.
- Unique clients: 981 per 7 days (+61 percent week over week); Smithery-attributed calls 77 (from 24).

## 5. Remaining engineering (none urgent)

1. Public MCP stateless (see section 3), then retire the session-hint branch of `_ClientFriendlyTransport` and the idle-cleanup tests.
2. Diagnose the `/openai/mcp/token` 401s (refresh grant).
3. A second Solana facilitator, if Johann names a vendor; the verify path is ready for a fallback URL.
4. Static example tickers in connector prompts and plugin READMEs still lead with BTC.
5. Stats endpoint still loads every event of the window into memory.

## 6. Working rules confirmed this session

- Pipelines hide failures: `pytest … | grep … | tail` always exits 0, so `&&` continued past a failing test twice. Check the "passed/failed" line, or use `set -o pipefail`.
- The listen-stream test hung because the `test_client` fixture carries the observability bearer on every request and the stream lasted five minutes; `tests/conftest.py` now bounds the stream for all tests.
- When two open PRs create the same new file, merging the first makes the second DIRTY; resolve with keep-both and watch indentation when a dedented block lands inside a loop.
- The OpenAI dashboard's "Copy issues" list is a snapshot of the last scan; always rescan after a deploy before reading it.
- Bundle independent green branches into one PR before handing them over (done as #92). Commit identity `-c user.name="Johann Focke" -c user.email="jf@blocksize-capital.com"`; fetch GitHub main from `ghssh`.
- Any new file under `docs/evidence` must be added to `ALLOWED_PUBLIC_DOC_FILES` in `scripts/verify_release_artifact.py`.

## 7. Johann's open actions

1. Merge #97, then reload the OpenAI MCPs tab; if the Rescan button is back, press it.
2. Watch for the OpenAI and Anthropic review mails; re-enable Clerk Device Trust after OpenAI's decision.
3. Decide on a second Solana facilitator vendor (optional).
4. Monday: "Allow always" when each routine asks for its command.
5. The main checkout is clean at ebf1e80; one worktree (`.claude/worktrees/openai-stream2`, branch `fix/openai-listen-stream-short`) remains for #97 and can be removed after merge.

## 8. Kickoff prompt for the new chat

```text
Read docs/gtm/checkpoint_2026-10-08b.md. First check PR 97 and the deploy, then open the OpenAI plugin's MCPs tab in Chrome and follow section 2 until the connector shows Configured and a scan has run; read the server log for OpenAI's requests and any "MCP transport 400" lines. Then continue with section 5 in order, one PR each, bundling green branches before handing them over. Do not merge, deploy or submit anything external without asking.
```
