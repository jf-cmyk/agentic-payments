# Status and open tasks, 2026-10-06

One list for everything still open across the Claude chats on this project, plus where production, the registries, usage and payments stand today. Numbers come from a live read of the production dashboard (`/internal/observability/usage` and `/stats`) at 19:16 UTC on 6 October, with monitors and test traffic excluded. Earlier numbers come from the dated docs and reports in this folder.

## 1. Where we stand

### Production

| Item | State |
|---|---|
| Service | `mcp.blocksize.info`, healthy, `/readyz` ready, commit `afeff0e` (PR #66), version string still `0.6.23` |
| Free tier | 30,000 credits a month, 60 a minute, 4,000 a day, 400,000 a day across all users, verified email required |
| Clerk | Production instance live since 30 Sep. Claude sign-in verified 1 Oct. ChatGPT reached the connector on 1 Oct (95 requests, 68 credit calls that day) after the `openid` scope fix. Cursor never tested on production |
| Railway volume | 7.4 GB of 10 GB used. `usage_events` has no retention or roll-up. This is the nearest operational cliff |
| Open PRs | None. Last merged: #66 |
| Uncommitted | Three docs in `docs/gtm/` (agent-auth acceptance, Clerk cutover runbook, free-tier manual steps) and `reports/mcp_usage_revenue_2026-09-30.html` |
| Leftover branches | `feat/data-quality-gate`, `feat/plugin-docs-to-main`, `feat/unified-credit-pricing`, `fix/lifecycle-email-milestones` and their worktrees. All content is on `main`; they can be deleted |

### Registries and listings

| Listing | State on 6 Oct | Gap |
|---|---|---|
| Official MCP Registry | `info.blocksize.mcp/agentic-payments` 0.6.23, active | Fine. Republish only when `server.json` changes |
| Smithery | Live, 97/100 typed output | Description still says 15,000 credits; tool list predates the 18 connector tools |
| Glama | Healthy, 99.7% uptime over 45 days | README text still says 15,000 credits. Wrapper repo listing not yet claimed by jf-cmyk |
| Pay.sh | Listed, 4 endpoints, `has_free_tier: false`, old description | pay-skills PR #208 (solana-foundation/pay-skills) open since 24 Sep, review still required |
| Claude plugin directory | Plugin bundle submitted 30 Sep (tag `agent-skill-0.7.0`) | Awaiting Anthropic review. MCP connector itself never submitted to the Connectors Directory |
| ChatGPT / OpenAI plugin directory | Not submitted | `.well-known/openai-apps-challenge` returns 404, so `OPENAI_APPS_CHALLENGE_TOKEN` is unset. Business verification and a no-MFA demo account still needed |
| Cursor marketplace and cursor.directory | Plugin repo at 1.5.0, not submitted | Needs Johann's Cursor and GitHub sign-in |
| x402scan, x402 directory, GitHub, GitLab, Awesome MCP | Listed | No action |

### Usage and payments, live

| Metric | Last 7 days | Last 30 days |
|---|---:|---:|
| Calls excluding monitors | 14,070 | 60,494 |
| Monitor share of all calls | 57% | 56% |
| MCP tool calls | 1,094 | 5,375 |
| Unique clients | 822 | 2,088 |
| Paid x402 calls | 41 | 147 |
| Recognised revenue (USDC) | $0.103 | $0.381 |
| Paying wallets | 9 | 19 |
| Payment proof success | 89% | 70% |
| Server error rate | 0.8% | 0.6% |
| Verified identities | 11 | 22 |
| Claude / ChatGPT / Cursor calls | 2 / 95 / 0 | 3 / 95 / 0 |

Reading:

- Paid traffic recovered after the mid-September burst: 15 paid calls on 29 Sep and 2 to 9 a day since. Nine wallets paid in the last week, none marked internal, so the "is this our own test wallet" question is still open.
- Payment reliability improved in the last week (89%) but the 30-day rate (70%) still trips the P0 alert. The 39 "not a valid bound x402 v2 signature" failures are mostly from before 28 Sep; the new failure since then is "Accepted payment requirement extra does not match" (2) plus facilitator outages (3).
- MCP tool calls are discovery only: `search_pairs`, `search`, `list_instruments`, `fetch`, endpoint builders. `get_vwap` was called 48 times in 30 days, `get_credit_balance` 5 times.
- Demand mirrors the examples: BTC-USD, EUR-USD and XAU-USD are 96% of live-data requests. AAPLX-USD is the only other ticker with real volume (636 calls).
- The free tier has one grant: the owner account (261 credits used this month). Two Clerk signups in 14 days, both ours. 141 clicks on `/go/free-trial` and 135 on `/go/pricing` in 14 days, from a 402-driven upgrade prompt shown 47,364 times.
- Agent registration works in production (10 registrations, 1 approved, all from the 29 Sep acceptance test).
- Some pollers are counted as real x402 traffic: `AgenticMarketplacePoller (agents.circle.com)` 4,076 calls and `402explorer` 307 calls in 7 days. Adding them to the monitor list would lower "live-data requests" and raise the paid conversion rate shown.
- Client errors: 17% of non-monitor HTTP requests are 4xx. The top three are `/mcp/server` 406 (clients without the right Accept header), `/mcp/server` 400, and `/openai/mcp` 401 (563 in 7 days, ChatGPT retrying without a token).

### Data quality

- VWAP coverage gate dated 28 Sep: 7,216 catalogued, 2,216 hidden as unknown to the engine, 2,133 live.
- The weekly audit on 5 Oct did not run to completion: the scheduled session is stuck at its first command (a permission prompt nobody answered). The 28 Sep run was done by hand in another chat and merged as PR #48.
- The upstream catalog asks (remove engine-unknown tickers, add an activity flag, add `asset_class` on bid/ask) are drafted as an email in `free_tier_manual_steps_2026-09-24.md`, section 7. No record that it was sent.

### Email

- Operator signup alert and daily digest work (Resend built-in sender).
- Welcome, two-day nudge and 80/100% threshold emails to users fail with `http_403`: no Resend DNS records exist on `blocksize.info` or `blocksize-capital.com` (checked 6 Oct). They retry and fail quietly.

### Scheduled tasks

| Task | State |
|---|---|
| `blocksize-free-tier-week1-review` (1 Oct 09:00) | Stuck at the stats pull, no report produced. This document covers what it would have reported |
| `blocksize-weekly-instrument-audit` (Mondays) | 28 Sep run was rejected and redone by hand; 5 Oct run stuck at the worktree command |

Both sessions still show as running in the sidebar. They need to be stopped, and the tasks need the `railway variables` and `git worktree` commands pre-approved or they will keep stalling.

## 2. What the dashboard assessment says, and the fix for each

Live output of `build_assessment` for the 30-day window, in its own severity order, with the fix we will apply.

| Sev | Finding (30 days) | Fix |
|---|---|---|
| P0 | 29.7% of payment attempts fail (147 of 209 settled) | (a) Return a precise error body naming the expected x402 v2 fields when the "extra" or signature does not bind; (b) publish copy-paste Solana and Base client examples on the 402 body and the docs; (c) retry the facilitator once more and fail over before rejecting. #45 added one retry and logging; extend it |
| P0 | Live-data requests almost never become paid calls (0.4%) | Treat the 402 as the landing page: free sample value, exact price, one-line client snippet, and a link to the free signed-in connector for agents without a wallet. First reclassify the pollers below so the denominator is honest |
| P1 | Monitors are 56% of calls | Add `AgenticMarketplacePoller`, `402explorer`, and any other probe in the top user agents to `KNOWN_MONITOR_USER_AGENT_MARKERS`; serve known probes a cached response |
| P1 | Directory attribution missing for 99.8% of calls | Give each listing its own URL carrying `utm_source` (Smithery, Glama, Pay.sh, MCP Registry, x402 directories), and store the avenue on payment records so revenue can be credited to a marketplace |
| P1 | Signed-in connectors barely used (98 calls, 22 identities) | Lead every listing with the free connector and 30,000 credits, one-click install links per client, and finish the directory submissions in section 3 |
| P2 | Top three tickers are 96% of demand | Rotate example symbols in docs and listings; tag example paths with `selection_source=published_example_path` |
| P2 | 15.8% client errors | Accept the Accept headers MCP clients send (406), add helpful bodies for the common 400s on `/v1/receipts/price` and `/v1/indicators/state-divergence`, and make `/openai/mcp` 401 point at the sign-in flow |
| P2 | 2.7% of requests rate limited (429) | Check whether the limited clients are monitors; raise discovery limits for identified agents |

Structural items from the dashboard's improvement plan, unchanged and still open:

1. Identify the bursty payer wallets on-chain; set `OBSERVABILITY_INTERNAL_PAYER_WALLETS` for any that are ours (none configured today).
2. Carry the client hash and `utm_source` onto MCP tool-call events.
3. Retention and roll-up job for `usage_events` (volume at 7.4 of 10 GB).
4. Move the RWA pilot block out of `/internal/observability/stats` (3.1 MB payload, 3.0 MB of it RWA).
5. Make verified identities the user north star; keep IP clients as reach.
6. Ingest Smithery, Glama and Pay.sh metrics (`platforms_configured` is still empty).

## 3. Unified open tasks, across every chat

Source chats: "Chat states, progress, and open tasks", "Clerk production environment setup", "Blocksize connector health check failure", "MCP usage and revenue overview", "MCP server observability dashboard", "Dashboard observability assessment", "MCP pricing unification", "Release plugin skills", "Plugin icon and metadata", "MCP server health audit", "Free tier dev checkpoint", "MCP data marketing strategy", "Blocksize data sales growth strategy", "Railway billing investigation", and the two scheduled tasks.

### A. Engineering, Claude can do without accounts

| # | Task | Origin | Size |
|---|---|---|---|
| A1 | Retention and roll-up for `usage_events`; alert when the volume passes 80% | Dashboard plan; volume 7.4/10 GB | M |
| A2 | Reclassify `AgenticMarketplacePoller`, `402explorer` and other probes as monitors | This review | S |
| A3 | x402 proof rejections: precise error bodies, client examples, facilitator retry and failover | P0 alert since 25 Sep | M |
| A4 | Per-listing `utm_source` URLs and avenue on payment records | Dashboard P1; usage report 30 Sep | M |
| A5 | Client hash and `utm_source` on MCP tool-call events | Dashboard plan | S |
| A6 | Add `OPENAI_ALLOWED_CLIENT_REDIRECT_URIS` to `.railway/railway.ts` (set on Railway, missing from the config file) | Clerk cutover chat | S |
| A7 | 406 and 400 handling on `/mcp/server`; helpful 401 on `/openai/mcp` | Dashboard P2 | S |
| A8 | Make the 402 body a landing page: sample value, price, snippet, connector link | Dashboard P0 | M |
| A9 | Rotate example tickers in docs and listings; tag example paths | Dashboard P2 | S |
| A10 | Split the RWA pilot out of the stats payload | Dashboard plan | S |
| A11 | Stuck-charge recovery does not refund shared-pool credits (fails safe) | Health audit 24 Sep | S |
| A12 | Bump version to 0.6.24, refresh `server.json` and listing text to 30,000 credits and the 18 tools, republish to the MCP Registry | Pricing chat; release 0.7.0 | S |
| A13 | Commit the three untracked docs and the 30 Sep report; delete the four merged branches and their worktrees | This review | S |
| A14 | Re-run the instrument audit by hand and open the data PR if the gate moved; fix the scheduled task so it stops stalling | Weekly audit 5 Oct | S |
| A15 | Smithery usage ingestion once `SMITHERY_API_KEY` and `SMITHERY_QUALIFIED_NAME` exist | Usage report 30 Sep | S |

### B. Ops and configuration, Claude with your go-ahead

| # | Task | Origin |
|---|---|---|
| B1 | Set `OBSERVABILITY_INTERNAL_PAYER_WALLETS` after you confirm which of the 19 paying wallets are ours | Dashboard plan |
| B2 | Decide the free-tier worst case: keep the 400,000 daily global cap (about $12,000 a month) or lower `FREE_TIER_GLOBAL_DAILY_CAP_CREDITS` | Pricing chat |
| B3 | Stop the two stuck scheduled sessions and pre-approve their read-only commands | Scheduled tasks |
| B4 | Rebuild the 30-day usage and revenue report with current data and publish it as a private page if you want to share it | Usage report chat |

### C. Only you can do these (accounts, sign-ins, sending)

| # | Task | Where | Origin |
|---|---|---|---|
| C1 | Resend DNS: add the MX and two TXT records for `blocksize.info` at NS1, or `blocksize-capital.com` at OVH, then restart verification. Until then no user emails go out | resend.com/domains | Chat states, 29 Sep |
| C2 | Cursor connector: connect `https://mcp.blocksize.info/cursor/mcp/` on production, sign in, call `get_credit_balance` | Cursor | Clerk cutover |
| C3 | ChatGPT connector: confirm the 1 Oct sign-in works end to end (ask for the credit balance). The 563 recent 401s suggest a stale connection | ChatGPT settings | Clerk cutover |
| C4 | Smithery: replace the listing description with the 30,000-credit text and the new tools | smithery.ai, GitHub jf-cmyk | Manual steps 24 Sep, pricing chat |
| C5 | Glama: claim the wrapper repo listing and trigger a re-sync; update its README to 30,000 | glama.ai, GitHub jf-cmyk | Manual steps, health audit |
| C6 | Pay.sh: chase a reviewer on solana-foundation/pay-skills PR #208 | GitHub | Health audit 24 Sep |
| C7 | Claude: submit the MCP connector to the Connectors Directory and pair it with the plugin; watch for the plugin review result | claude.ai/directory/manage | Plugin chat |
| C8 | OpenAI: business verification, demo account in Clerk, set `OPENAI_APPS_CHALLENGE_TOKEN` on Railway, then submit the form | platform.openai.com/plugins | Manual steps, section 5 |
| C9 | Cursor: submit the plugin repo to cursor.com/marketplace/publish and cursor.directory | Cursor, GitHub | Manual steps, section 6 |
| C10 | Send the web-team email (free tier on the pricing page, UTM capture on matrix trials) | Your mailbox | Manual steps, section 1 |
| C11 | Send the data-ops email (three catalog fixes) | Your mailbox | Manual steps, section 7 |
| C12 | Clerk: recreate the Anthropic reviewer account on the production instance; optionally enable Google sign-in | Clerk dashboard | Clerk cutover |
| C13 | Confirm whether the 14 to 17 Sep and 29 Sep payment bursts came from your own wallets | On-chain or your records | Usage report |
| C14 | Store `AGENT_AUTH_SECRET` and the Clerk dev backup in your password manager | Local | Observability chat 29 Sep |
| C15 | Switch `gh` back to your usual account when finished with jf-cmyk work: `gh auth switch --user JoFoRe` | Terminal | Plugin chat |

### D. Waiting on third parties

- Anthropic: plugin bundle review (submitted 30 Sep).
- Solana Foundation: pay-skills PR #208 review.
- Blocksize data ops: catalog fixes, once C11 is sent.
- Web team: pricing page and UTM capture, once C10 is sent.

### E. Other project, listed for completeness

The Gleis sessions (`~/Documents/ChatGPT/X402 and Pay.sh Revenue`) are blocked on Azure budget, HSM signing, the pen test and legal review. Nothing there touches this repo.

## 4. Suggested order

Week of 6 Oct:

1. A13 commit and clean up; A1 retention job (the volume is the one thing that can take production down).
2. A2 monitor reclassification and A6 config file, together in one small PR.
3. C1 Resend DNS, C2 and C3 connector checks, C13 wallet confirmation and then B1.
4. A3 payment errors and A4 attribution URLs, one PR each.
5. C4, C5, C6 listing refresh with the 30,000-credit text, backed by A12.

Week of 13 Oct:

6. A8 and A9 conversion work on the 402 body and examples.
7. C7, C8, C9 directory submissions.
8. A5, A7, A10, A11 clean-ups.
9. Re-run this review with `days=7` and compare against the table above.

## 5. Questions

1. Which of the paying wallets are ours? Without that, every revenue number includes our own tests.
2. Keep the 400,000-credit daily global cap, or lower it?
3. Resend: NS1 for `blocksize.info` or OVH for `blocksize-capital.com`?
4. Were the web-team and data-ops emails sent? If not, say so and I will leave them in the list.
5. May I commit the three untracked docs and this file, and delete the four merged branches and worktrees?
