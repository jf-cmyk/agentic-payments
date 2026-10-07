# Checkpoint, 2026-10-07 (end of day)

Start a new chat from this file. It records what is live, what is in flight, what is waiting on whom, and how to continue. The longer background is in `status_and_open_tasks_2026-10-06.md` (the assessment) and `execution_plan_2026-10-06.md` (the plan and the Resend records).

## 1. Production state

| Item | Value |
|---|---|
| Version | 0.6.24, commit 21f923d, healthy, `/readyz` ready |
| Railway | auto-deploys every push to main, **no "Wait for CI"** (switched off 7 Oct after the GitHub outage broke the gate twice). Re-trigger with `railway redeploy --from-source -y`. Volume 2.7 of 10 GB |
| MCP Registry | 0.6.24 published, remote `https://mcp.blocksize.info/mcp/server/?utm_source=mcp-registry` |
| Smithery | release published with `?utm_source=smithery`; description says 30,000 credits |
| Glama | claimed by jf-cmyk, description and README at 30,000 credits |
| Pay.sh | solana-foundation/pay-skills PR 208 updated (30,000 credits, v0.6.23), signed, CI green, comment posted; waiting for the maintainer |
| Cursor marketplace | publisher application submitted 7 Oct (handle `blocksize`); waiting for Cursor |
| Claude directories | plugin bundle submitted 30 Sep; MCP connector submitted by Johann on 7 Oct from the Blocksize organisation; both in Anthropic review |
| Free tier | 30,000 credits a month, one grant (owner), 261 credits used. No outside signups yet |

## 2. Merged today (all deployed)

#68 multidict CVE and calendar-proof retention test · #69 docs · #70 crawlers counted as monitors · #71 usage-event roll-up past 185 days and `storage` block on `/health` · #72 payment rejection diagnosis, Retry-After on outages, two facilitator retries, workflow prices in OpenAPI · #73 listing attribution and client labels on public MCP tool calls · #74 release 0.6.24 · #75 Railway gate off in the config file · #76 pending-charge recovery no longer blocks balance reads.

## 3. In flight

- **Resend (user emails).** Netlify DNS (zone blocksize.info; Netlify DNS runs on NS1, hence Resend's "NS1" label) now holds `resend._domainkey` TXT, `rsend` and `send` CNAMEs to `*.forge.rmta.net`, and `track` CNAME to `links2.resend-dns.com`. The obsolete `send` SPF TXT was deleted. Resend's tracking subdomain was moved from `mcp` (the production host; never add that record) to `track`; click tracking on, open tracking off. Public resolvers answer all records. Resend's last recorded check was "DNS invalid" at 12:58 local, before the records existed; restarts after that had not produced a new event by 13:50. Next: open https://resend.com/domains, click Restart on blocksize.info, wait for "Verified", then send one test email from the production sender (`USER_EMAIL_FROM`, via httpx on the server; urllib is blocked by Cloudflare) and confirm the welcome mail on the next signup.
- **OpenAI plugin directory.** Johann: business verification for Blocksize Capital GmbH, a demo user in Clerk without MFA. Claude: set `OPENAI_APPS_CHALLENGE_TOKEN` on Railway (clipboard pattern), fill the form per `free_tier_manual_steps_2026-09-24.md` section 5.
- **Emails to the web team and data ops**: parked by Johann.

## 4. Remaining engineering (none urgent)

1. 402 as the landing page: sample value, exact price, one-line client snippet, link to the free connector.
2. Rotate example tickers in docs and listings; tag copied example paths (`selection_source=published_example_path`).
3. Client errors: accept the Accept headers MCP clients send (406 on `/mcp/server`), helpful bodies for the common 400s, sign-in hint on `/openai/mcp` 401.
4. Move the RWA pilot block out of `/internal/observability/stats`.
5. Stuck-charge recovery refunds the shared free-tier pool (September health audit).
6. Align Railpack pins: `.railway/railway.ts` says 0.38.0, `RAILPACK_VERSION` env says 0.36.2, builds use 0.40.1.
7. Smithery usage ingestion once `SMITHERY_API_KEY` and `SMITHERY_QUALIFIED_NAME` exist.

## 5. Routines

Both scheduled tasks run one scripted command (`run.sh` next to their SKILL.md) and will ask once for approval on their first unattended run (Monday): the instrument audit (opens a PR on material change; it did so today as #67) and the weekly usage and free-tier review (writes `docs/gtm/weekly_review_<date>.md`).

## 6. Working rules learned today

- Main is protected: PR only, `test` must pass on a branch up to date with main; merging several PRs is serial (`gh pr update-branch N` after each merge). Push over SSH as jf-cmyk; gh via `GH_TOKEN=$(gh auth token --user jf-cmyk)`; never the JoFoRe account.
- In worktrees, run the main checkout's `.venv/bin/python` and `.venv/bin/ruff` by absolute path; never symlink `.venv` and never `git add -A`.
- After any Railway settings change, check that "Auto deploys when pushed to GitHub" is still enabled; applying a staged change once switched it off.
- Codex (OpenAI's CLI) traffic on `/openai/mcp` is Johann's own; its 401s are the OAuth handshake.

## 7. Johann's open actions

1. Finish the Claude Connectors Directory submission if not done; watch for Anthropic's review mails (plugin and connector).
2. OpenAI prerequisites (section 3).
3. Delete the merged local branches and worktrees in the main checkout (`.claude/worktrees/*`, `feat/data-quality-gate`, `feat/plugin-docs-to-main`, `feat/unified-credit-pricing`, `fix/lifecycle-email-milestones`, `claude/*`).
4. Monday: "Allow always" when each routine asks for its command.

## 8. Kickoff prompt for the new chat

```text
Read docs/gtm/checkpoint_2026-10-07.md, then docs/gtm/execution_plan_2026-10-06.md section 5. First check Resend: open https://resend.com/domains in Chrome, restart verification of blocksize.info if it still says Failed, and once Verified send one test email from the production sender. Then start the remaining engineering items from checkpoint section 4 in order, one PR each, following the working rules in section 6. Do not merge, deploy or submit anything external without asking; Johann merges.
```
