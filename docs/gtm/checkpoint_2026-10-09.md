# Checkpoint, 2026-10-09 (written 05:00 UTC)

Start a new chat from this file. It supersedes `checkpoint_2026-10-08b.md` and records what is live, what the OpenAI connector dashboard does now, what was learned about the server log, and how to continue.

## 1. Production state

| Item | Value |
|---|---|
| Version | 0.6.24, commit 9bedf90 at the time of writing (cf5b6d2, the docs merge, was building), healthy, `/readyz` ready |
| Merged this session, all deployed | #97 OpenAI listen stream closes after 2 s · #99 bundle: `server/discover` shim on all four MCP apps, honest token-endpoint errors plus a failed-grant log line, stateless public MCP, #97 folded in · #98 checkpoint for the second 8 October session |
| Open PRs | none at the time of writing (this checkpoint's PR is opened with it) |
| Railway | auto-deploys main; every merge redeploys; verify with `/readyz` (`commit_sha`) |
| OpenAI plugin directory | **In review** (plugin `plugin_asdk_app_6ac6c3f81bb08191a09faa3cb61a2df3`, manifest 0.7.1). See section 2 |
| Claude plugin directory | plugin 0.6.0 published; MCP connector submission in Anthropic review |
| Clerk | Device Trust still off for the OpenAI reviewers; re-enable after OpenAI's decision |

## 2. The OpenAI connector dashboard (where the thread ended)

Use this URL; the `/apps/<id>` form in earlier checkpoints shows OpenAI's logged-out chooser even with a session:

`https://platform.openai.com/plugins/manage/plugin_asdk_app_6ac6c3f81bb08191a09faa3cb61a2df3?tab=mcp&version=appsub_6ac6c3f81bec8191a81c93820beb3426&mcp=server%3Ablocksize-market-data`

The tab's details call (`GET api.openai.com/v2/dashapi/plugins/<id>`) can sit pending for 30 to 60 seconds. A Rescan result is only visible after a page reload. A Rescan click right after a reload sometimes does not register; check that the Issues panel switches to "Starting MCP scan…".

State after two scans on 9 October (23:36 and 04:08 UTC): MCP configuration **Configured**, Authentication **Authorized**, Domain verified, Review status In review. Tools: the ten core tools show a fresh "Last checked", the eight premium tools (`get_market_brief` to `get_trader_alpha_pack`) and the server instructions show "—". Every tool shows "Not live", which OpenAI's docs define as "new tools remain unavailable until approved". The Issues panel keeps **"Complete MCP setup: Connect your MCP server and complete discovery of its tools and widget metadata."**

What the server log showed for the 04:08 scan, after #99 deployed:
1. `POST /openai/mcp/token` 200 (the connected account's refresh grant works).
2. `server/discover` answered by the shim (client name `openai-mcp`, protocol 2026-07-28, mcp-app profile in client capabilities).
3. `tools/list` without any initialize, answered 200.
4. One unauthenticated POST (401 `invalid_token`, identical to a curl without a token) followed by the three OAuth metadata GETs. That is OpenAI's auth-discovery probe, not a failure.

The 23:36 scan (before #99) went the legacy way: `server/discover` got "Method not found", then initialize, initialized, tools/list, the same probe. Both scans produced the same dashboard. The server exposes all 18 tools (checked locally and in the plugin package), instructions are returned by both `initialize` and `server/discover`, and there are no `MCP transport 400` lines on the OpenAI app. Conclusion: nothing left to change server-side; the finding and the eight unevaluated tools sit with OpenAI's review. Next move, Johann's call: contact OpenAI plugin support with the plugin id and the two scan times above.

## 3. Findings from the server log

- Requests from Johann's own network reach Railway as `70.112.67.46`. The 36-cycle `/openai/mcp/token` 401 loop at 22:49 UTC on 8 October (OAuth metadata, token 401, `GET /openai/mcp/` 405, 53 seconds) came from there, not from OpenAI. OpenAI's scanner uses `104.210.139.225` to `.239`; Anthropic's probes `160.79.106.x`; the MCP registry's scanner `65.21.92.231`.
- Those token 401s are `invalid_client` (the client id in the refresh request is unknown to the connector's client store). Until #99 the sign-in-hint middleware rewrote them to "No bearer token was sent". Now `/openai/mcp/token` keeps its own body and a `WARNING src.mcp_transport_diagnostics: OAuth token endpoint 401 on openai_mcp: grant_type=… client_id=<12 chars>… error=…` line names the grant and client prefix. Grep for `OAuth token endpoint` to see which client keeps failing.
- `invalid_token` INFO lines from FastMCP's middleware appear for every unauthenticated POST (about 20 per hour); most are clients' first contact before OAuth, not errors.
- Public MCP before #99: 15 of 22 listen GETs and all session-less `tools/list` posts in a 2.5-hour window got 400. After #99 the public server is stateless: session-less posts answer 200, the listen GET gets the bounded empty stream, DELETE answers 405.
- `server/discover` is now also answered on `/anthropic/mcp/`, `/cursor/mcp/` and `/mcp/server/` (SDK versions only; the stateless OpenAI app also advertises 2026-07-28). Claude Code 2.1.295 probes `/anthropic/mcp/` with it.

## 4. Numbers (8 Oct, from the previous checkpoint; not re-measured)

Public MCP tool calls per day: 148 (6 Oct), 237 (7 Oct); 30-day average 189. No 429s in 14 days. Payment-proof 7-day failure rate 12 percent. Unique clients 981 per 7 days. Re-measure from `/internal/observability/usage` when numbers are needed.

## 5. Remaining engineering (none urgent)

1. Diagnose the `/openai/mcp/token` `invalid_client` refreshes once the new log line has named the client (which connector path registered it, whether the client store lost it).
2. A second Solana facilitator, if Johann names a vendor; the verify path is ready for a fallback URL.
3. Static example tickers in connector prompts and plugin READMEs still lead with BTC. Deferred on purpose: the shared tool descriptions are under OpenAI review and a change resets it. Do it after the decision.
4. Stats endpoint (`UsageEventStore.summarize`) still loads every event of the window into memory and makes about twenty passes over the list; a proper fix is SQL aggregation per panel, roughly a day with the dashboard tests.
5. SDK upgrade: PyPI has mcp 2.3.0 (dual-era, answers `server/discover` natively) and fastmcp 4.1.0; the repo pins mcp 1.28.1 and fastmcp 3.2.4. The shim in `src/mcp_transport_compat.py` can go when the upgrade lands.

## 6. Working rules confirmed this session

- Per-change branches, one bundle PR (#99 pattern). When an open PR is squash-merged while the bundle still carries its branch, the bundle shows the same test-file conflict again after `git fetch ghssh main`; merge `ghssh/main` into the bundle, keep both sides, push. `gh pr update-branch N` handles docs-only PRs.
- Run tests in a worktree with the main checkout's venv by absolute path; stage files explicitly. Full suite: 1366 tests, about five minutes locally; `node tests/test_agent_tools.mjs` and `scripts/check_release_contracts.py --expected-version 0.6.24 --require-clean` complete the CI set.
- `railway logs --lines N` is the way to read the server log; group by source IP before attributing a loop.
- A `pytest … | tail` pipeline hides failures; use `set -o pipefail` or read the passed/failed line.
- Commit identity `-c user.name="Johann Focke" -c user.email="jf@blocksize-capital.com"`; push over SSH to `git@github.com:jf-cmyk/agentic-payments.git`; gh as `GH_TOKEN=$(gh auth token --user jf-cmyk)`.

## 7. Johann's open actions

1. Decide whether to contact OpenAI plugin support about the "Complete MCP setup" finding (plugin id above, scans at 23:36 and 04:08 UTC on 9 October, all 18 tools served, instructions returned).
2. Watch for the OpenAI and Anthropic review mails; re-enable Clerk Device Trust after OpenAI's decision.
3. Name a second Solana facilitator vendor (optional) and say when the BTC-first examples may change.
4. The main checkout is clean at cf5b6d2; no worktrees remain except this checkpoint's.

## 8. Kickoff prompt for the new chat

```text
Read docs/gtm/checkpoint_2026-10-09.md. First check the deploy and the open PRs, then grep the Railway log for "OAuth token endpoint" and "server/discover answered" lines and report what clients they name. Then continue with section 5 in order, one PR each, bundling green branches before handing them over. Do not merge, deploy or submit anything external without asking.
```
