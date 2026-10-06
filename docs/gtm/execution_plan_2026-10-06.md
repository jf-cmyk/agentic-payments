# Execution plan, 2026-10-06

Companion to `status_and_open_tasks_2026-10-06.md`. Part 1 answers the questions asked on 6 October with the evidence found. Part 2 is Claude's work, in order. Part 3 is Johann's to-do list, in order, with the exact steps. Part 4 lists the decisions still needed.

## 1. Answers

### Why the volume exists and what fills it

The Railway volume at `/data` is where the server keeps everything that must survive a redeploy: credit balances, the free-tier ledger, x402 payment records and replay protection, OAuth tokens for the Claude, Cursor and ChatGPT connectors, entitlements, agent-auth delegations, Clerk signups and the usage-event log behind the dashboard. Without it, every deploy would reset balances and tokens and allow replayed payments.

Measured over SSH on 6 October (`df` reports 6.1 of 9.1 GB used; Railway's meter shows 7.4 of 10 GB):

| File | Size | Status |
|---|---:|---|
| `rwa_growth_pilot_depth_history.jsonl` | 2.6 GB | Dead. Last written 24 Sep 21:22, the moment the pilot was switched off (`RWA_GROWTH_PILOT_ENABLED=false`) |
| `rwa_growth_pilot_promotion_history.jsonl` | 2.6 GB | Dead. Same timestamp. A second copy of the same raw snapshots |
| `usage_events.db` | 0.9 GB | Live. 1.18 million rows since 23 June. September added 586,000 rows, roughly 450 MB a month at today's rate |
| `rwa_observations.v2.db` | 52 MB | Live ledger of the pilot (the authoritative one) |
| Everything else (credits, ledgers, OAuth, payments, signups) | under 10 MB combined | Live |

So 85% of the volume is two files from the research experiment that ended in September. The usage log is the only thing that still grows, and a roll-up of rows older than 90 days keeps it flat.

**Done on 6 October, 20:27 UTC, with Johann's approval.** Nothing in the running service reads the two files (the pilot loop is disabled and the dashboard reads the small `_latest.json` status files). Both were compressed on the server into `/data/pilot_backup_2026-10-06/` (0.8 GB, checksums in `SHA256SUMS`, gzip integrity verified), then the raw originals were deleted. A copy to the Mac was attempted but the SSH link runs at about 85 KB/s and truncated the stream, so the archives stay on the volume for now. Result: `/data` went from 6.1 GB to 1.8 GB used (20%). `/health` healthy, `/readyz` ready, RWA report checks passing. Railway's meter lags and still showed the old figure at that time.

### The 563 unauthenticated requests on `/openai/mcp`

They are not ChatGPT and not failed calls. Every one of the 401s since 1 October comes from the user agent `codex-mcp-client/0.159.2`, which is OpenAI Codex (the CLI and desktop app), not the ChatGPT web app:

| Day | 401s | 200s | Distinct IPs |
|---|---:|---:|---:|
| 1 Oct | 137 | 719 | 1 |
| 2 Oct | 32 | 528 | 1 |
| 3 Oct | 96 | 1,154 | 1 |
| 5 Oct | 132 | 1,246 | 1 |
| 6 Oct | 168 | 1,517 | 7 |

Each time Codex starts a session it opens the MCP connection without a token first, gets 401, runs the OAuth exchange, and continues with 200s. The large number of 200 GETs is the streamable-HTTP event stream reconnecting. One IP a day until 5 October points at a single machine, most likely the Codex install on Johann's Mac with the Blocksize plugin. Seven distinct IPs on 6 October is new and may be other people, or one laptop on several networks. Confirmation needed (question 1 below).

The ChatGPT test itself, on 1 October, worked as intended:

| What happened | Count |
|---|---:|
| Symbol searches (`search_pairs`) | 24 |
| Live calls delivered (`get_vwap` 47, `get_bid_ask` 15, `get_fx_rate` 5) | 67 |
| Symbols sampled, 5 snapshots each at about 3.7-second intervals (about 15 seconds per symbol) | 13 |
| Credits used | 261 |
| Denials, rate limits, failures | 0 |

The 13 symbols were BTC, ETH, SOL, BNB, XRP, TRX, ZEC, USDT, USDC, EUR/USD, and the tokenized equities AAPLX, NVDAX, TSLAX. The grant shows 29,739 of 30,000 credits left. So the free-tier path works end to end for one signed-in person; what has not happened yet is anyone else signing up.

### Emails and sign-up

The sign-up chain is Clerk `user.created` webhook, store the signup, alert Johann, send the welcome. The first three steps work: two `clerk_user_created` events in the last 14 days, both stored and alerted. The welcome, the two-day nudge and the 80/100% emails all fail because the sender `hello@blocksize.info` is on a domain Resend has not verified. `blocksize.info` is hosted at NS1 (confirmed by its nameservers). No Resend records exist on either domain. This is a DNS task only; no code change is needed. Note that the welcome is skipped, not retried, so the two existing signups will not receive one afterwards; a fresh test signup is the way to confirm.

### Pay.sh PR 208

State: open, mergeable, CI green, last touched 24 September. The maintainer's only request (signed commits) was met on 16 September and the review was dismissed. Nobody has looked since. The maintainers are active (a provider PR merged on 30 September).

What is stale in the PR:

- Access section says 15,000 credits; production is 30,000, with one price list where 1 credit equals $0.001.
- Text says contracts were checked against v0.6.21; production is v0.6.23.
- Tokenized equities are not named.

What is fine: all x402 prices in the sidecar match production. The PR's OpenAPI even carries prices for the nine POST workflow routes that production's own `openapi.json` omits, which is a small fix for us.

The repository copy at `pay-skills/providers/blocksize/market-data/PAY.md` has drifted the other way (30,000 credits but the older route list), so it must be re-synced to the PR version after the update. The `pay` CLI (0.16.0) is installed locally and the fork `jf-cmyk/pay-skills` exists; the signing key is in the SSH agent and matches the signing key registered on GitHub on 31 July.

### Stale listings

| Listing | What it says | Who fixes it |
|---|---|---|
| Smithery | 15,000 credits (dashboard text) | Johann, in the Smithery dashboard |
| Glama | 15,000 credits (from the wrapper repo README, v0.2.0) | Claude pushes README v0.2.1; Johann claims the listing and triggers a re-sync |
| Pay.sh | Old listing, 4 endpoints, no free tier | Claude updates PR 208 |
| MCP Registry | 0.6.23, no free-tier mention | Claude, on the next version bump |
| Claude plugin | Already 30,000 (repo-driven, webhook delivering) | Nothing |

### Stuck scheduled tasks

Both runs stalled on their first shell command because a routine runs in a new session with only the approvals previously granted to that routine, and none had been granted. The one-week review never pulled its stats; the 5 October audit never created its worktree. Both stuck turns were stopped on 6 October. The fix is one approval pass by Johann (part 3, step 3) plus rewriting the prompts so every command is identical run to run and can be allowed once.

## 2. Claude's plan, in order

Each step is a separate PR unless noted. Nothing is merged, deployed, deleted or posted without Johann's go.

| # | Step | What it contains | Needs from Johann |
|---|---|---|---|
| 1 | Clean up | Commit the three untracked docs, the 30 Sep report and the two status docs; delete the four merged branches and worktrees | Go |
| 2 | Volume | Done 6 Oct: raw pilot files deleted, compressed archives kept on the volume. Still to do as a PR: nightly roll-up of `usage_events` rows older than 90 days into daily aggregates, plus a volume-usage line on `/health` | Go for the PR |
| 3 | Scheduled tasks | Rewrite the two routine prompts: turn the one-time review into a Monday "free tier and usage weekly review" that writes a dated report, and keep the audit; both with fixed commands. Then run each once while Johann approves the commands | Johann present for one run of each |
| 4 | Pay.sh | Clone the fork, set repo-local SSH signing, update PAY.md (30,000 credits, price list, v0.6.23, tokenized equities), run `pay catalog check` on the provider, push one signed commit, draft the PR comment to the maintainer; re-sync the repo copy and the submission mirror | Go to push; approval of the comment text before posting |
| 5 | Glama | Update the wrapper repo README to 30,000 credits and the subscription ladder, bump to 0.2.1, push | Go to push; Johann claims the listing |
| 6 | Smithery and Claude text | Hand over the exact description texts (below) | Johann pastes them |
| 7 | Monitors | Add `AgenticMarketplacePoller`, `402explorer` and the other probes in the top user agents to the monitor markers; add `OPENAI_ALLOWED_CLIENT_REDIRECT_URIS` to `.railway/railway.ts` | Go |
| 8 | Payments | Precise error body for the "payment requirement extra does not match" and unbound-signature rejections, with the expected x402 v2 fields and a client example; second facilitator retry with failover; prices for the nine POST routes in production `openapi.json` | Go |
| 9 | Attribution | Per-listing `utm_source` URLs; store the avenue on payment records; carry client hash and `utm_source` onto MCP tool-call events | Go |
| 10 | Email verification | Once Resend shows "Verified": send one test welcome through the production sender, confirm on the next real signup, and check the nudge and threshold jobs clear their retry queue | Resend verified |
| 11 | Release | Version 0.6.24 with the above, `server.json` description mentioning the free tier, republish to the MCP Registry | Go |
| 12 | Conversion | 402 body as a landing page (sample value, price, snippet, connector link); rotate example tickers | Go |

Steps 1, 3 and 4 can start today. Steps 2 and 5 wait on a decision or a claim. Steps 7 to 9 are one PR each and can run in parallel in worktrees.

## 3. Johann's to-dos, in order, with steps

### Today

**1. Answer four questions** (part 4). The Codex and wallet answers change what the dashboard counts as real demand; the deletion answer unblocks the volume work.

**2. Resend DNS for `blocksize.info` at NS1.** This turns on the welcome, nudge and upgrade emails.

1. Open https://resend.com/domains and click `blocksize.info`. Keep the page open; it lists three records with copy buttons.
2. Log in at https://my.nsone.net, open Zones, then `blocksize.info`.
3. Add each record with "Add Record". NS1 wants the full name, so append `.blocksize.info`:

   | Resend shows | NS1 type | NS1 name | NS1 answer |
   |---|---|---|---|
   | MX, name `send` | MX | `send.blocksize.info` | priority and mail server from Resend |
   | TXT, name `send` | TXT | `send.blocksize.info` | the `v=spf1 ...` text from Resend |
   | TXT, name `resend._domainkey` | TXT | `resend._domainkey.blocksize.info` | the long `p=...` key from Resend |

4. Only add records. Do not edit or delete anything that exists on the apex (`blocksize.info` itself); those carry the company mail and site verification.
5. Back in Resend, click "Restart verification". It usually turns green within minutes.
6. Tell Claude "Resend verified".

**3. Unstick the two routines** (about five minutes, needs you at the keyboard).

1. In the sidebar open "Blocksize weekly instrument audit" (the 5 October run). Its stopped turn shows the pending shell command. Nothing to do there; close it.
2. Tell Claude "rewrite the routines". Claude updates both prompts.
3. Then say "run the audit now". A new session starts and asks for the worktree command; choose "Allow always" for that routine. It asks once more for the audit script and once for the pytest run. The run takes about six minutes.
4. Repeat with "run the weekly review now". It asks for the `railway variables` command (the token is read into a shell variable and never printed) and for the stats download. Allow both.
5. From then on both routines run on Monday mornings without prompts.

**4. Smithery description.** Sign in at https://smithery.ai/login with GitHub as **jf-cmyk** (not JoFoRe). Open https://smithery.ai/servers/blocksize/agentic-payments, Settings, and replace the description with:

```text
Read-only MCP discovery for Blocksize real-time market data: multi-venue crypto
VWAP, bid/ask, FX, metals and tokenized equities with provenance receipts. Search
instruments, check coverage and freshness, and build exact HTTP requests. Signed-in
users of the Claude, ChatGPT and Cursor connectors receive 30,000 free live-data
credits every calendar month (1 credit = $0.001; evaluation licence, "Data by
Blocksize" attribution required), then subscription plans from EUR 49/month with a
free trial. Direct public HTTP uses signed x402 on Solana and Base. Enterprise
terms: contact Blocksize sales.
```

### This week

**5. Glama.** After Claude confirms the README push: sign in at https://glama.ai with GitHub as jf-cmyk, open https://glama.ai/mcp/servers/jf-cmyk/blocksize-agentic-payments-mcp, click "Claim", then in the Admin tab trigger a re-sync or rebuild if offered. Tell Claude if the page still shows 15,000 a day later.

**6. Pay.sh.** When Claude reports the signed commit is pushed, read the drafted comment and reply "post it". Claude posts it on PR 208 as jf-cmyk. If there is no reply within a week, a short message to the maintainer on the Solana Foundation Discord is the next nudge.

**7. Test a real sign-up after Resend is verified.**

1. In Claude (claude.ai), add a custom connector with `https://mcp.blocksize.info/anthropic/mcp/` and sign up with an email you have not used before.
2. Ask it "How many Blocksize credits do I have left?" and then "What is the BTC/USD VWAP?".
3. Check that you received the welcome email at that address, and the operator alert at jf@blocksize-capital.com.
4. Tell Claude the result. Claude checks the webhook delivery, the `clerk_user_created` event, the grant and the `user_email_sent` event.

**8. Cursor connector on production.** In Cursor, add the MCP server `https://mcp.blocksize.info/cursor/mcp/`, sign in, and ask for the credit balance. Tell Claude "Cursor connected" or paste the error.

**9. ChatGPT.** Open a new ChatGPT chat with the Blocksize connector enabled and ask for your remaining credits. If it asks to reconnect, reconnect; the OAuth scopes were corrected on 1 October.

**10. Merge the PRs from part 2 as their CI turns green**, in the order Claude lists them. Use the jf-cmyk token without switching accounts:

```bash
GH_TOKEN=$(gh auth token --user jf-cmyk) gh pr merge PRNUMBER -R jf-cmyk/agentic-payments --squash
```

Replace `PRNUMBER` with the number Claude gives you.

### Next week

**11. Directory submissions**, each needing your own sign-in (full steps in `free_tier_manual_steps_2026-09-24.md`, sections 4 to 6):

1. Claude Connectors Directory: https://claude.ai/directory/manage, "Submit new", MCP connector, URL `https://mcp.blocksize.info/anthropic/mcp/`, from the Blocksize organisation.
2. OpenAI: complete business verification for Blocksize Capital GmbH, create a demo user in Clerk without MFA, copy the challenge token from the form into Railway with the clipboard command below, then submit at https://platform.openai.com/plugins.

   ```bash
   pbpaste | tr -d '\n' | railway variable set OPENAI_APPS_CHALLENGE_TOKEN --stdin
   ```

   Copy the token from the OpenAI form first, then run the command.
3. Cursor: https://cursor.com/marketplace/publish and https://cursor.directory/plugins/new with the repository `https://github.com/jf-cmyk/blocksize-cursor-plugin`.

**12. Send the two prepared emails** from `free_tier_manual_steps_2026-09-24.md`: section 1 to the web team (pricing page and UTM capture), section 7 to Blocksize data operations (catalog fixes). If either was already sent, say so.

**13. Housekeeping.** Store `AGENT_AUTH_SECRET` and the Clerk development backup in your password manager; recreate the Anthropic reviewer account on the production Clerk instance; switch `gh` back to JoFoRe when you are done with jf-cmyk work.

## 4. Decisions needed

1. **Is the Codex traffic yours?** Do you have OpenAI Codex installed with the Blocksize plugin on your Mac? If yes, the 401s are your own sessions and the seven IPs today are probably you on different networks. If no, those are outside users and Claude should look closer.
2. **Keep the 400,000-credit daily global cap** (worst case about $12,000 a month if the free tier is abused), or lower it?

Settled on 6 October: the paying wallets are not Johann's (no test payments in the last month), so every settlement counts as customer revenue and no internal-wallet list is needed. The pilot files were deleted with archives kept on the volume.

## 5. Resend records at NS1, in detail

Resend's Records tab shows three groups. Only the first two are required for verification; the third is recommended.

| Resend group | Resend type | Resend name | Resend value | Required |
|---|---|---|---|---|
| DKIM | TXT | `resend._domainkey` | a long string starting `p=MIGf...` | Yes |
| SPF (also labelled Return-Path or Sending) | MX | `send` | `feedback-smtp.<region>.amazonses.com`, priority `10` | Yes |
| SPF | TXT | `send` | `v=spf1 include:amazonses.com ~all` | Yes |
| DMARC | TXT | `_dmarc` | `v=DMARC1; p=none;` | Recommended |

NS1 offers many record types (A, AAAA, CNAME, MX, TXT, and so on). You only use MX and TXT. In NS1's zone view for `blocksize.info`, click "Add Record" and fill in:

| # | Type (NS1 dropdown) | Name field | Answer field | TTL |
|---|---|---|---|---|
| 1 | TXT | `resend._domainkey` | paste the DKIM value exactly, no added quotes | leave default |
| 2 | MX | `send` | priority `10`, mail server exactly as Resend shows (ends in `amazonses.com`) | leave default |
| 3 | TXT | `send` | `v=spf1 include:amazonses.com ~all` | leave default |
| 4 | TXT | `_dmarc` | `v=DMARC1; p=none;` | leave default |

Notes:

- NS1 appends the zone to the Name field. Type only the short name (`send`, not `send.blocksize.info`). After saving, the record list must show `send.blocksize.info`, `resend._domainkey.blocksize.info`, `_dmarc.blocksize.info`. If a record shows up as `send.blocksize.info.blocksize.info`, delete that one and re-add it with the short name.
- For the MX record, NS1 has two boxes: "Priority" (or "Preference") takes `10`, "Answer" takes the host name. Do not put the `10` inside the Answer box.
- Copy the DKIM value with Resend's copy button. Do not add quotation marks and do not let the editor wrap or truncate it.
- Add only; do not change or delete the existing records on the apex. The apex currently holds Google site-verification TXT records and the company mail setup, and the Resend records live on the `send` subdomain so they cannot collide.
- Region: all of Resend's values must name the same AWS region. That is automatic if you copy from one domain page.
- Back in Resend, click "Restart verification". NS1 publishes within seconds; Resend usually turns green within minutes, sometimes up to an hour. Tell Claude "Resend verified".

If the Resend page shows a different layout from the table above (for example a CNAME-based DKIM, or a different return-path subdomain), paste the records table from the page into the chat. None of those values are secret, and Claude will map them to NS1 fields one by one.
