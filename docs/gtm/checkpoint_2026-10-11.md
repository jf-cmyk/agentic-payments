# Checkpoint, 2026-10-11 (written 19:55 UTC)

Start a new chat from this file. It supersedes `checkpoint_2026-10-09.md` and records the OpenAI connector thread to its conclusion (the problem is on OpenAI's side), what was changed on the way, the open support case, and how to continue.

## 1. Production state

| Item | Value |
|---|---|
| Version | 0.6.24, commit 5438b32 (#103) with `OPENAI_CONNECTOR_TOOLS=all` set in Railway at 19:50 UTC and redeployed (SUCCESS at 19:52 UTC). Verify with `/readyz` (`commit_sha`) |
| Merged since the 9 Oct checkpoint, all deployed | #101 nullable parameters as type lists, OpenAI instructions within 512 characters · #102 OpenAI connector answers JSON, not an event stream · #103 OpenAI connector tool allowlist (default: ten tools; `OPENAI_CONNECTOR_TOOLS=all` restores all, now set) |
| Open PRs | none at the time of writing (this checkpoint's PR is opened with it) |
| OpenAI plugin directory | **In review**, plugin `plugin_asdk_app_6ac6c3f81bb08191a09faa3cb61a2df3`, manifest 0.7.1. Dashboard finding "Complete MCP setup" is a platform-side snapshot bug; support case open, see section 2 |
| Claude plugin directory | plugin 0.6.0 published; MCP connector submission in Anthropic review |
| Clerk | Device Trust still off for the OpenAI reviewers; re-enable after OpenAI's decision |

## 2. The OpenAI connector dashboard: conclusion

Dashboard URL (the `/apps/<id>` form shows a logged-out chooser):
`https://platform.openai.com/plugins/manage/plugin_asdk_app_6ac6c3f81bb08191a09faa3cb61a2df3?tab=mcp&version=appsub_6ac6c3f81bec8191a81c93820beb3426&mcp=server%3Ablocksize-market-data`

Dashboard state, unchanged across every scan: MCP configuration Configured, Authentication Authorized, Domain verified; the ten core tools (`search_pairs` … `get_vwap_24h`) get a fresh "Last checked" on each scan; the eight premium tools and the server instructions never do; everything "Not live" (expected while in review); the Findings panel keeps "Complete MCP setup: Connect your MCP server and complete discovery of its tools and widget metadata."

Seven scans from 8 to 11 October, every one received and answered on our side (log: `server/discover answered on openai_mcp for client openai-mcp`, then `tools/list` 200, then one unauthenticated probe 401 and the three OAuth metadata GETs), covering:
- the legacy initialize flow and the 2026-07-28 `server/discover` flow (#99),
- pydantic `anyOf` nullable parameters and `type: [t, "null"]` (#101),
- 609- and 485-character instructions (#101),
- event-stream and single-JSON answers to `tools/list` (#102),
- eighteen and exactly ten tools (#103, scan at 19:47:29 UTC on 11 Oct),
- plus one Reconnect through our Clerk consent, which came back to the same finding.

Decisive detail: with ten tools served, the dashboard did not remove the eight missing tools, which OpenAI's docs say a scan does at once. The scan result is not written back on their side; only the "Last checked" stamp on known entries is. Nothing is left to change on the server.

Support case (help.openai.com chat, AI support agent, then presumably a human): they confirmed no documented tool limit, called it a platform-side persistence bug, asked for the tool list order, the `tools/list` response headers (sent), and the dashboard's request details (sent: `POST …/continuous-review/rescan` 200 and `GET …/continuous-review` 200, no error body visible). The last message asked them to escalate to the dashboard team with the 19:47:29 UTC scan and to say whether review can proceed on the server as it stands. Waiting for their answer.

Dashboard handling notes: the Rescan button only fires when the tab is in the foreground and the button was hovered first; confirm in the browser's network log that `POST …/continuous-review/rescan` returned 200 and the panel shows "Starting MCP scan…". The scan finishes within 20 seconds; reload to see results. A tab in the background makes screenshots come back black and clicks go nowhere.

## 3. Findings from the server log

- Refresh-grant 401s on `/openai/mcp/token` are `invalid_grant` for client `33930b82-505…`, all from Johann's own network (`70.112.67.46`), in bursts (17 in 12 seconds at 18:06 UTC on 9 Oct, right after a deploy). Token stores are on `/data` (FastMCP keeps tokens in `client_storage`, the same encrypted filetree as client registrations), so deploys do not wipe them. FastMCP logged no reason, which leaves "refresh token not found in the store" (expired, rotated by a parallel refresh, or written under an earlier encryption key). The client is a ChatGPT connection on Johann's machine; reconnecting it replaces the token. Log line to grep: `OAuth token endpoint`.
- OpenAI's scanner refreshes its own token successfully before each scan (`POST /openai/mcp/token` 200), so the connected reviewer account is healthy.
- Public MCP stateless since #99: no `transport 400` lines for session-less clients any more.
- Source IPs: `70.112.67.46` is this machine; `104.210.139.225` to `.239` is OpenAI's scanner; `160.79.106.x` Anthropic; `65.21.92.231` the MCP registry scanner.

## 4. Numbers

Not re-measured this session. Use `/internal/observability/usage` when needed.

## 5. Remaining engineering (none urgent)

1. Log why a refresh grant is refused (store miss versus client mismatch) with the client prefix: a small override of `load_refresh_token` on the connector providers. Then the item-3 burst explains itself.
2. A second Solana facilitator, if Johann names a vendor.
3. Static example tickers lead with BTC in connector prompts and plugin READMEs; still deferred until OpenAI's decision, since tool descriptions are under review.
4. Stats endpoint (`UsageEventStore.summarize`) loads every event of the window; SQL aggregation per panel, about a day with the dashboard tests.
5. SDK upgrade to mcp 2.x / fastmcp 4.x; retires the `server/discover` shim and the FastMCP-internal component-map rewrite in `create_authenticated_market_data_mcp`.
6. If OpenAI keeps the ten-tool snapshot after approval, decide whether to keep `OPENAI_CONNECTOR_TOOLS=all` or upload a 0.7.2 package whose skill matches whatever set is served.

## 6. Working rules confirmed this session

- A PR the user calls merged may still be open: check `gh pr view N --json state` before acting on "merged".
- `railway variable set NAME=value` redeploys; wait for `railway deployment list` to show SUCCESS and `/readyz` the expected commit.
- The dashboard's `continuous-review` endpoints cannot be fetched from page scripts (CORS); use the browser's network log for statuses.
- Everything else from the 9 Oct checkpoint still holds: per-change branches bundled into one PR when several are ready, worktrees with the main checkout's venv by absolute path, commit identity flags, SSH push, `GH_TOKEN=$(gh auth token --user jf-cmyk)`.

## 7. Johann's open actions

1. Watch for OpenAI support's answer and the review mails; paste the support reply into the next chat.
2. Reconnect the Blocksize connector in ChatGPT on this machine (jf@blocksize-capital.com) so the refresh loop stops; say if `OAuth token endpoint 401` lines keep appearing.
3. Re-enable Clerk Device Trust after OpenAI's decision.
4. Name a Solana facilitator vendor (optional).

## 8. Kickoff prompt for the new chat

```text
Read docs/gtm/checkpoint_2026-10-11.md. First check the deploy, OPENAI_CONNECTOR_TOOLS and the open PRs; if OpenAI support has answered, act on their reply. Then continue with section 5 in order, one PR each, bundling green branches before handing them over. Do not merge, deploy or submit anything external without asking.
```
