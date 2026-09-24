# Free-tier and subscription-funnel: development checkpoint

Date: 2026-09-23
Repo branch at checkpoint: `codex/safeguard-remediation-0.6.5` (main branch: `main`)
Owner: Johann (jfocke13@gmail.com)
Status: planning complete, no code changed yet.

Read this whole file before touching code. Decisions in section 1 are locked; do not
re-litigate them. Section 9 lists the few decisions still open, each with a
recommended default so work can proceed.

## 0. Why we are doing this

- The agentic x402 / pay.sh surface earns almost nothing per call ($0.001-$0.008), and
  x402 demand is thin market-wide (about $28k/day network volume in March 2026, roughly
  half of it wash trading, per CoinDesk). Millions of calls would be about $2k of revenue.
- The revenue lever is Blocksize data subscriptions (MRR EUR 40k, target EUR 100k). The
  MCP/agent surface should therefore be a **funnel into subscriptions**, not a product.
- CoinGecko's free Demo tier gives 10k calls/month (30/min, attribution required).
  We answer with **15,000 free live-data credits per month**, better data, and a clear
  path to the paid plans on `https://blocksize.info/crypto-market-data/pricing/`.

Website plan ladder (source of truth, EUR): Developer 49/mo (5 feeds, 7-day history,
1 seat) - Start-Up 299/mo (50 feeds, 1-month history, 5 seats) - Business 799/mo (350
feeds, 3-month history, 10 seats, State Prices, Slack SLA) - Enterprise (custom,
unlimited feeds, commodities, SLA). Annual = 15% off.

## 1. Locked decisions (Johann, 2026-09-23)

1. **Scope:** all production data may be in the free scope, provided the abuse controls
   in section 4 and the data-rights gate in section 5 are in place.
2. **Price:** x402 per-call prices stay exactly as they are. The free tier does the work.
   (The earlier idea of cutting core crypto to $0.001 is dropped.)
3. **Attribution:** required on free-tier responses.
4. **Order of work:** A (foundations) -> B (free tier) -> D (funnel) -> E (website/docs).
   Pricing work "C" is dropped. A prepaid credit pack stays a design note only.

## 2. Verified facts about the current code

These were checked against the repo on 2026-09-23. Re-verify anything you rely on.

1. **The "50 credits" are a one-time lifetime cap, not daily.** `EntitlementManager`
   sums `credits_spent` across all dates (`src/entitlement_manager.py`, `spend()` and
   `status()` use `SUM(credits_spent) ... WHERE user_id = ?`), and
   `tests/test_entitlement_manager.py::test_starter_usage_does_not_reset_by_day` asserts
   it. `README.md:143` and `docs/anthropic_beta_connector_runbook.md:34` wrongly say
   "per UTC day". A correction to an earlier claim in the planning chat, which assumed
   about 1,500/month. Consequence: 15k/month is roughly 300x more generous than today and
   is the first time the allowance recurs.
2. **Free credits exist only on the authenticated connector path** (Claude, Cursor,
   OpenAI via Clerk OAuth), built by the shared factory in
   `src/authenticated_mcp_server.py`. On direct HTTP, raw identity headers are disabled in
   production (`_starter_credit_subject`, `src/resource_server.py:3286`), so HTTP callers
   can only pay via x402. The legacy `CreditManager` starter path (`credits.db`, wallet and
   device trial history) is effectively dev-only in production.
3. **Ledgers are per connector.** `connector_entitlement_db_path()` gives each connector
   its own SQLite file, and `ConnectorIdentity.ledger_subject` is issuer/connector-scoped.
   Today one person can hold a separate allowance in Claude, Cursor and OpenAI. At 15k
   that is a 3x leak unless deduplicated (see D1 in section 9).
4. **Credit costs** (`TOOL_COSTS` in `src/authenticated_mcp_server.py:96`,
   `CREDIT_COSTS` in `src/credit_manager.py:1359`): raw VWAP, bid/ask, state price, 30m
   and 24h VWAP = 1; FX and metals = 2; pre_trade_check 5; market_brief and audit_receipt
   10; token_quality and state_divergence indicators 15; macro_snapshot and
   solana_token_brief 25; trader_alpha_pack 50; rwa_blocksize_benchmark 10. Search and
   list_instruments are free (0).
5. **x402 prices** (`src/config.py:262`): core crypto $0.002, extended crypto $0.004,
   FX/metals $0.005, equities $0.008, analytics $0.001. Unchanged by this work.
6. **Upgrade CTA is weak.** `upgrade_recommendation()` in `src/commercial_plans.py:130`
   fires only when 10 or fewer credits remain and only links to `/go/contact` ("contact
   sales"). `ACCOUNT_PLANS` there is a USD ladder (49/249/999 with credit counts) that
   does not match the website (EUR 49/299/799/Enterprise). `recommend_account_plan()`
   picks a plan from call counts, but the real plan boundaries are feed counts (5/50/350).
7. **A tracked redirect already exists.** `/go/{destination}`
   (`src/resource_server.py:2113`) with `OUTBOUND_DESTINATIONS` (line 438):
   `free-trial` -> `https://matrix.blocksize.capital/`, `pricing` -> the website pricing
   page, `contact` -> the website contact page. It records `outbound_conversion_click`
   and forwards UTM labels. Use `/go/free-trial` and `/go/pricing` as CTAs.
   `MAIN_WEBSITE_PRICING_URL` is in `src/public_metadata.py:82`.
8. **"50 credits" is hard-coded in about 20 places.** Known hits: `resource_server.py`
   (lines about 1849, 5041, 8695, 8725, 13070, 13127), `anthropic_mcp_server.py:61`,
   `cursor_mcp_server.py:66`, `openai_mcp_server.py:66`, `public_metadata.py:64`,
   `public_mcp_server.py:767`, `authenticated_mcp_server.py:1145`, `README.md:120,143`,
   `docs/README_EXTERNAL.md:7,98`, `docs/developer_portal.html:1492`,
   `docs/anthropic_beta_connector_runbook.md:34`, and
   `pay-skills/providers/blocksize/market-data/PAY.md:14,26`. Also re-grep for
   "starter" and "50" in the plugin folders and `docs/gtm/pay_skills_submission`.
9. **The working tree is very dirty.** 91 modified files, about +221k/-50k lines against
   HEAD, plus many untracked files. The exact files we need to change already carry large
   uncommitted diffs: `resource_server.py` (+6.6k), `authenticated_mcp_server.py` (+745),
   `entitlement_manager.py` (+539), `credit_manager.py` (+983), `config.py`,
   `public_metadata.py`, `README.md`, `docs/README_EXTERNAL.md`,
   `docs/developer_portal.html`. See D0 in section 9.
10. **Baseline tests:** this passes today (45 tests):
    `.venv/bin/python -m pytest tests/test_entitlement_manager.py tests/test_commercial_growth.py tests/test_public_product_truth.py tests/test_anthropic_mcp_tools.py -q`
    Full suite (`tests/`) has not been run in this session; run it first to get a baseline.
11. Secrets and databases (`mcp-registry-key.pem`, `credits.db`, `usage_events.db`,
    `anthropic_entitlements.db`) are gitignored, so not a repo hygiene issue. They do sit
    in the project folder; keep them out of any zip, deliverable or shared artifact.

## 3. What 15,000 credits means in real-time terms

Unit: **15,000 credits per calendar month (UTC) per verified identity.** One credit is
one raw price call, so 15,000 credits is about 15,000 raw calls. FX and metals cost 2 and
analytics packs cost 10-50, so heavier products drain the pool faster.

| Usage pattern | Calls/month | What 15k covers |
|---|---|---|
| 1 feed polled every second | 2.59M | about 6 minutes of continuous streaming |
| 1 feed polled every minute | 43,200 | about 35% of the month (about 8 hours a day) |
| 1 feed polled every 5 minutes | 8,640 | the full month, with room for about 1.7 feeds |
| 5 feeds (Developer plan size) every 15 min | 14,400 | fits, only just |
| Ad hoc agent lookups, 10-50 a day | 300-1,500 | fits easily |

Averaged: 500 a day, about 21 an hour, one call every 2.9 minutes. That is enough to
prototype and evaluate an agent workflow, and not enough to run a live production
feed, which is exactly the upgrade trigger we want. List value is about $30 per user per
month at $0.002; marginal cost is near zero for cached data.

## 4. Abuse-control spec ("all data free, but not abusable")

Honest answer: abuse can be **bounded and made detectable**, not eliminated. Build these
layers. Every layer needs a test.

1. **Identity.** Free credits only for a verified Clerk identity (already the case for
   connectors; never trust raw headers). Require a verified email. Normalize emails
   (lowercase, strip `+tag`, fold Gmail dots) and store only a salted hash as the grant
   key. One grant per normalized email across **all** connectors (see D1). Block
   disposable-email domains via a maintained blocklist file with an allow-list override.
2. **Per-identity rate limits.** Default 30 credits/minute sustained and a soft daily cap
   of 2,000 credits (about 13% of the month), so one account cannot drain the pool in a
   day or hammer upstream. Return a clear, non-5xx "rate limited" payload with a retry
   time. Reuse the `check_rate_limit` pattern from `CreditManager`
   (`src/credit_manager.py:1179`).
3. **No amplification.** Batch and multi-item calls charge the **sum of item costs**.
   Cap free-tier batch size (default 5 items; the paid `/v1/batch` max is 20). No
   bulk-dump, export or history endpoints in free scope. Existing discovery rate limits
   (60/min, 1,000/day) stay.
4. **Global circuit breaker.** `FREE_TIER_ENABLED` kill switch and
   `FREE_TIER_GLOBAL_DAILY_CAP_CREDITS` across all identities. When hit, return a
   "free tier at capacity" payload pointing to x402 and the trial, never a 5xx. Pick the
   default only after confirming upstream Blocksize API request limits and the cache hit
   rate (stream cache TTL default is 3600 s in `src/config.py:85`).
5. **Detect and suspend.** Flag and auto-suspend (`users.status != 'active'`; `spend()`
   already blocks non-active users and records a `blocked` event) when: more than 80% of
   the pool is used within 24 hours of first call; a sequential sweep across many distinct
   symbols in a short window (scraping pattern); or many new grants share a hashed network
   fingerprint. Emit `free_tier_abuse_flagged`. Check `docs/privacy_policy.html` before
   using any IP-derived signal; the growth runbook states IPs are not used as funnel
   identities, and abuse control must stay consistent with the published policy.
6. **Licence and attribution.** Free tier is an **evaluation and prototyping licence**:
   internal use, no resale, no redistribution, no public redisplay without attribution,
   and production commercial use requires a subscription. Return an `attribution` field
   ("Data by Blocksize", with a link) and a licence header on every free-tier response.
   The licence is governed by the existing Blocksize data terms
   (`https://blocksize.info/terms-conditions-data/`, the Crypto Data License
   Agreement; `/terms` redirects there). Legal only confirms the evaluation,
   attribution, and no-redistribution clauses are covered (see section 8).
7. **x402 bypass stays open.** Once the pool is exhausted or capped, x402 payment still
   works; that is part of the upgrade path.

Worst-case exposure: per identity 15,000 credits (about $30 list value); across all
identities bounded by the global daily cap. Both must be visible in the command center.

## 5. Data-rights gate (blocks "all data")

"All data" means **all production-promoted packages**: crypto VWAP, bid/ask, state price,
30-minute and 24-hour VWAP, FX, metals, supported equity tickers via shared bid/ask, and
the analytics packs.

- **Exclude RWA candidate and pilot feeds.** Per the README claims boundary and
  `src/rwa_source_rights.py`, zero newly sourced RWA additions have passed promotion and
  redistribution sign-off. The three pilot feeds (AAPL/USDC, PAXG/USDC, EURC/USDC) stay
  out of free scope.
- **Before launch, get written confirmation from Blocksize data ops or legal** that free
  redistribution to authenticated evaluators is permitted for equities, FX, metals and
  any third-party-sourced series. This repo cannot answer that. Until confirmed, ship
  the free scope for crypto only and add the rest when cleared. Make free scope a
  config allow-list (`FREE_TIER_ALLOWED_SERVICES`), not hard-coded, so it can widen
  without a code change.

## 6. Work plan (order A -> B -> D -> E)

### A. Foundations
1. Resolve D0 (dirty tree) first.
2. Run the full test suite for a baseline and record the result.
3. Single source of truth for the allowance: new `FREE_TIER_*` settings (monthly credits,
   per-minute cap, per-day soft cap, batch cap, global cap, kill switch, allowed
   services), read once in `src/config.py`. Generate all user-facing text from it. Replace
   every hard-coded "50 credits" listed in section 2.8. Fix the "per UTC day" claims.

### B. Free tier (monthly pool plus guards)
1. `src/entitlement_manager.py`: monthly accounting. `daily_usage` already stores one row
   per day, so the monthly total is a sum over `usage_date` with a `YYYY-MM` prefix; the
   day total gives the soft daily cap. Rename or alias `daily_limit` semantics carefully:
   existing rows hold 50, and `set_daily_limit()` supports per-user subscriber overrides
   that must survive. Plan a migration: users still on the old default move to the new
   allowance; explicit overrides are preserved.
2. Bump `ENTITLEMENT_SCHEMA_VERSION` (currently 2) and `ENTITLEMENT_SCHEMA_COLUMNS` if you
   add tables or columns, and update the schema tests
   (`test_schema_status_*` in `tests/test_entitlement_manager.py`).
3. Add the guards from section 4: per-minute limiter, daily soft cap, per-email
   dedupe, batch cap, global cap and kill switch, disposable-email block, suspension.
4. `src/authenticated_mcp_server.py`: enforce free-scope allow-list and batch charging in
   the shared factory so Claude, Cursor and OpenAI connectors all inherit it. Add
   `attribution`, `credits_remaining`, `monthly_limit` and `resets_at` to responses.
5. Keep the legacy HTTP starter headers (`X-Blocksize-Starter-Allowance`,
   `STARTER_CREDIT_ALLOWANCE`) consistent or remove them deliberately; do not leave them
   advertising 50.

### D. Funnel to subscriptions
1. Replace "contact sales" with real CTAs. Use `/go/free-trial` (the matrix trial) as the
   primary action and `/go/pricing` as secondary, with `utm_source=mcp` style labels via
   the existing attribution allow-list (`ATTRIBUTION_QUERY_KEYS`, `resource_server.py:443`).
2. Rewrite `upgrade_recommendation()` to trigger on consumption thresholds (50, 80, 95 and
   100% of the monthly pool) and on **distinct instruments used in the trailing 30 days**,
   which maps to plan boundaries: more than 5 -> Start-Up, more than 50 -> Business, more
   than 350 -> Enterprise. `usage_events.subject` already stores the symbol.
3. Align `ACCOUNT_PLANS` and `recommend_account_plan()` in `src/commercial_plans.py` with
   the website ladder (EUR, feeds, seats, history). Keep the `/v1/account-plans*` API
   backward compatible, and update `tests/test_commercial_growth.py`.
4. Put the CTA in: 402 responses, free-tier-exhausted and rate-limited payloads, and the
   `get_credit_balance` tool output. Add an `upgrade_cta_shown` event with `plan_id` and
   `trigger`.
5. New observability events (`record_usage_event`, `src/observability.py:2210`):
   `free_tier_grant_created`, `free_tier_threshold_crossed`, `free_tier_exhausted`,
   `free_tier_rate_limited`, `free_tier_abuse_flagged`, `upgrade_cta_shown`. Add them to
   the funnel and event lists in `observability.py` (see the lists around lines 581,
   1066 and 1410) so they appear in the command center, and add a Free-tier panel: grants,
   pool consumed, exhaustion rate, CTA impressions, `/go/*` clicks, trial starts.

### E. Website and docs (repo-controlled surfaces)
1. Update: `README.md`, `docs/README_EXTERNAL.md`, `docs/developer_portal.html`,
   `docs/anthropic_beta_connector_runbook.md`, `src/public_metadata.py`, `llms.txt` and
   the manifests that derive from it. New line: "15,000 free live-data credits every
   month, then from EUR 49/month." Keep the claims boundary and RWA wording intact.
2. Regenerate PDFs with `scripts/generate_blocksize_pdfs.py` at the end.
3. Update `tests/test_public_product_truth.py`, `tests/test_registry_integrity.py`,
   `tests/test_release_safeguards.py` and `tests/test_resource_server.py` (lines about
   3442-3444 assert "Contact sales" in `upgrade_path`; line about 6107 tests spending
   the full starter allowance) to the new truth.

### Definition of done for today
- Free tier live behind `FREE_TIER_ENABLED` on a branch, full suite green, new tests for
  every guard in section 4.
- One config block drives all user-facing allowance text; no stale "50 credits" left.
- Every upgrade surface links to `/go/free-trial` or `/go/pricing` with attribution.
- Command center shows the new events and a Free-tier panel.
- A list of external updates (section 8) is prepared, **not executed**.

## 7. Constraints: Solana Foundation, x402 and registry dependencies

- **Do not change the x402 wire format**: the `PAYMENT-REQUIRED` header, the
  `X402_RESPONSE` schema, payment requirement construction (`src/config.py:357`), and the
  network, asset and facilitator readiness settings (`resource_server.py` around 702-800).
  Pay.sh, x402scan and the facilitator depend on them.
- The free tier lives on the connector rail, not on x402. Keep `x-payment-info` in
  `openapi.json` describing accountless pay-per-call only.
- Price-neutral change means `X402_WELL_KNOWN_RESOURCES`, `openapi.json` and
  `server.json` prices need no edit. Verify with the x402 and resource-server tests
  before and after.
- The Pay.sh listing text **does** mention "50 live data credits"
  (`pay-skills/providers/blocksize/market-data/PAY.md:14,26`). Update the local copy so
  the repo is consistent, but do not open a PR upstream (see section 8).

## 8. External updates that need Johann's explicit approval (do not execute)

Per `docs/gtm/pay_sh_positioning_plan.md`, nothing external is submitted without approval.
Prepare a diff or copy for each and stop:

1. `pay-skills` registry entry (Solana Foundation / Pay.sh): new starter wording in
   `PAY.md`; PR upstream.
2. Official MCP Registry (`server.json`), Smithery, Glama, the Claude connector directory
   and plugin packages (Claude, Cursor, OpenAI): description text with the new offer.
3. `blocksize.info` pricing and product pages (outside this repo, web team): add "Free:
   15,000 live credits/month via MCP" above the Developer plan, a "start trial" CTA that
   preserves `source_channel=mcp`, and a link to the existing data terms.
4. Confirm the existing Blocksize data terms (`/terms-conditions-data/`) cover the
   free-tier evaluation licence, attribution, and no-resale clauses; amend that
   page if not. No separate Terms of Use document. **Blocks launch.**
5. Data-rights confirmation from data ops or legal (section 5). **Blocks the non-crypto
   free scope.**

## 9. Open decisions (defaults are what to build unless Johann says otherwise)

- **D0. Dirty working tree.** 91 modified files including every file we need. Ask Johann
  whether that work is deployed or in progress. Recommended: get their OK to commit it as
  its own commit on `codex/safeguard-remediation-0.6.5`, then branch
  `feat/free-tier-monthly-pool` from it. Do not commit, stash, reset or push without asking.
- **D1. One grant per person across connectors.** Recommended: a shared free-tier grant
  ledger (single SQLite file on the Railway volume, `FREE_TIER_LEDGER_DB_PATH`) keyed by
  hashed normalized email, so Claude, Cursor and OpenAI draw from one 15,000 pool.
  Alternative: keep per-connector ledgers and accept up to 3x.
- **D2. Licence scope.** Recommended: evaluation/prototyping, internal use; commercial
  production use requires a subscription.
- **D3. Global daily cap default.** Recommended placeholder 200,000 credits/day, to be
  calibrated after checking upstream limits and cache hit rates.
- **D4. Free API key for direct HTTP.** Out of scope today. Note for later: the natural
  CoinGecko-style Demo plan for scripts and developers who are not on an MCP client.

## 10. Metrics to watch after launch

North star: qualified trial starts from agent surfaces (`/go/free-trial` clicks and
matrix.blocksize.capital signups tagged `source_channel=mcp`). Supporting: free-tier
grants, activation (first live price), pool consumption distribution, exhaustion rate,
CTA impressions to clicks, abuse flags per 1,000 grants, and the cap-hit count. Review
weekly in the command center per `docs/gtm/growth_operating_runbook.md`.

## 11. Out of scope for today

Price changes, prepaid credit packs, new RWA feeds or promotion work, new registry
submissions, the free API key for HTTP, and any subscription sales-motion work (LinkedIn
follow-up, ICP expansion, Enterprise packaging), which run in parallel outside this repo.

## 12. Kickoff prompt for the new chat

> Read `docs/gtm/free_tier_dev_checkpoint_2026-09-23.md` in full, then start with
> section 6 step A. Locked decisions are in section 1; do not reopen them. First
> resolve D0 with me (the working tree has 91 uncommitted files, including every file we
> need to change), run the full test suite for a baseline, and then implement A and B
> using the recommended defaults in section 9. Do not commit, push, deploy or submit
> anything external without asking. Show me the diff for A before starting B.

## 13. Session log (2026-09-23, step A)

- **D0 resolved.** Production (`/health` on mcp.blocksize.info) runs v0.6.22 from
  `github/main` commit `23eba0b` (2026-09-18). The local checkout was a stale,
  partially synced ~v0.6.5 tree with no git activity since 2026-07-29 and about
  25k lines behind production. It was committed as WIP checkpoint `8686abd` on
  `codex/safeguard-remediation-0.6.5` (code, tests, docs, configs only;
  `reports/` and `deliverables/` left untracked) so its unpushed local-only
  code is preserved. Work continues on `feat/free-tier-monthly-pool`, branched
  from `github/main`. The nested `pay-skills` and `blocksize-cursor-plugin` git
  repos and 12 conflicting `deliverables/` files were moved to
  `../Agentic Payments_moved_aside_2026-09-23/`.
- **Facts re-verified against main.** `ENTITLEMENT_SCHEMA_VERSION` is 3 (not 2);
  `identity_aliases` table exists; `users` rows are inserted positionally by
  rollback tests, so new state must live in new tables, not new `users` columns.
  Connector identities carry `email` but not `email_verified` today.
- **Baseline (main, full suite):** 1044 passed, 29 failed. All 29 are Node-based
  Railway release-helper tests in `tests/test_release_safeguards.py` (subprocess
  timeouts and a path error from the space in `Agentic Payments`); unrelated to
  this work and unchanged by it.
- **Step A done:** `FreeTierSettings` in `src/config.py`, `src/free_tier.py`
  generates all allowance text, `DEFAULT_DAILY_CREDITS` and
  `STARTER_CREDIT_ALLOWANCE` now derive from `FREE_TIER_MONTHLY_CREDITS`; the
  legacy `*_DAILY_CREDITS` and `STARTER_CREDIT_ALLOWANCE` env vars are ignored
  (warning helper in `free_tier.legacy_allowance_env_warnings`). All repo copy
  in section 2.8 updated; `tests/test_free_tier_config.py` guards against
  regressions. Plugin package folders (Claude/OpenAI/Cursor skills) were left
  untouched because tests compare them byte-for-byte with zipped deliverables;
  they join the section 8 approval list.

## 14. Session log (2026-09-23, baseline fix and step B)

- **Baseline failures fixed.** The 29 `tests/test_release_safeguards.py` failures
  had one cause: the fake `railway` CLI the tests install used a
  `#!{sys.executable}` shebang, and the checkout path contains a space
  (`Agentic Payments`), so the kernel truncated the interpreter path. No
  production code was involved; the release helper scripts are unchanged. The
  tests now install the Python body as `railway.py` behind a `/bin/sh` wrapper
  that quotes the interpreter path (`_install_fake_railway`). Result: 120/120
  pass in about 55 s instead of 29 failures after 5m44s of timeouts.
- **Step B design.** Two ledgers cooperate:
  - `EntitlementManager` (per connector, rollback-compatible) now sums
    `daily_usage` over the `YYYY-MM` prefix, exposes `credits_spent_today` and
    `resets_at`, and keeps the charge lifecycle. Schema v4 adds
    `allowance_overrides`; rows on the retired 50 default follow
    `FREE_TIER_MONTHLY_CREDITS`, any other value found at first migration is
    preserved as an override (`set_daily_limit` writes overrides,
    `clear_allowance_override` removes them). `set_status` suspends users.
    No new `users` columns, so rollback tests and bridge fingerprints hold.
  - `FreeTierLedger` (`src/free_tier_ledger.py`, one SQLite file at
    `FREE_TIER_LEDGER_DB_PATH`) is keyed by the salted hash of the normalized
    email and is shared by Claude, Cursor, and OpenAI (D1). It enforces the
    monthly pool, credit-weighted per-minute limit, soft daily cap, global
    daily cap, kill switch, and detect-and-suspend (fast drain, symbol sweep).
    Effective remaining credits are the lower of the two ledgers.
- **Gate order in the factory:** economic-writes lock, identity, kill switch,
  eligibility (email present, `email_verified` not false, domain not disposable
  unless allow-listed), data-rights scope (`service_for_tool`; unknown bid/ask
  symbols lean equity, so long-tail bare tickers need the pair form), shared
  ledger reserve, connector ledger spend. Refunds release both ledgers.
  Bid/ask symbols are classified from the live instrument catalog
  (`BlocksizeClient.classify_symbol`, cached one hour): the catalog's own
  asset-class metadata decides crypto vs equity vs FX vs metal, so a bare
  long-tail crypto ticker is crypto. The naming heuristic in
  `free_tier.looks_like_equity_symbol` is only the fallback when the catalog
  cannot be read, so an upstream outage never turns cleared crypto data into a
  scope refusal.
- **Client-facing codes:** `DAILY_CREDIT_LIMIT_REACHED` is kept for the
  exhausted monthly pool because published agent skills handle it; new codes
  are `FREE_TIER_RATE_LIMITED`, `FREE_TIER_DAILY_CAP_REACHED`,
  `FREE_TIER_AT_CAPACITY`, `FREE_TIER_SUSPENDED`, `FREE_TIER_DISABLED`,
  `FREE_TIER_INELIGIBLE`, `FREE_TIER_SCOPE_EXCLUDED`. Every denial is a normal
  tool result with `retry_after_seconds` where relevant, never a 5xx. Every
  live response ends with the monthly balance line and
  `Data by Blocksize: <url>`; `get_credit_balance` returns `monthly_limit`,
  `credits_spent_today`, `resets_at`, `shared_pool`, `attribution`, `licence`.
- **Events emitted now** (command-center lists are step D):
  `free_tier_grant_created`, `free_tier_threshold_crossed` (50/80/95/100),
  `free_tier_exhausted`, `free_tier_rate_limited`, `free_tier_abuse_flagged`.
- **Not done, on purpose:** the shared-network-fingerprint signal (section 4.5)
  is not built because the published privacy policy and the growth runbook rule
  out IP-derived identities; that is a policy commitment, not a code question,
  so it stays out unless the policy changes. The legacy HTTP starter path only
  gained the free-tier batch cap (it is dev-only in production).
  `email_verified` is carried from OIDC claims; the gate fails open when the
  claim is absent and refuses only when it is explicitly false, so nothing is
  blocked on Clerk. Confirm the claim on a real token after deploy.

## 15. Session log (2026-09-23, step D)

- **Plan ladder** (`src/commercial_plans.py`) now mirrors the website in EUR:
  Developer 49 (5 feeds, 7-day history, 1 seat), Start-Up 299 (50 feeds,
  1 month, 5 seats), Business 799 (350 feeds, 3 months, 10 seats, State
  Prices, Slack SLA), Enterprise (custom). Annual is 15% off. Legacy ids
  `production` and `institutional` alias to `startup` and `business` so stored
  links keep resolving. `/v1/account-plans*` keeps its shape and gains
  `currency`, `annual_discount_pct`, `pricing_url`, `free_trial_path`, and a
  `ctas` block.
- **Recommendation** is feed-based (the real plan boundary): more than 5
  instruments in the trailing 30 days -> Start-Up, more than 50 -> Business,
  more than 350 or an SLA or more than 10 seats -> Enterprise. Call volume is
  only a fallback estimate (8,640 calls a month is about one feed polled every
  five minutes). `EntitlementManager.distinct_subjects` supplies the breadth.
- **Upgrade trigger** fires at 50, 80, 95, and 100 percent of the monthly
  pool (`consumption_50` ... `consumption_100`) or on breadth
  (`instruments_<plan>`), and returns primary `/go/free-trial` and secondary
  `/go/pricing` CTAs with `utm_source`, `utm_medium=product`,
  `utm_campaign=free-tier-upgrade`, `utm_content=<plan>`, `utm_term=<trigger>`.
  Enterprise adds the tracked `/go/contact` sales CTA. Legacy callers without
  a monthly limit keep the old 10-credit rule.
- **CTA placement:** every live connector response from 50 percent onward,
  `get_credit_balance`, exhausted and rate-limited denials, HTTP 402 bodies,
  `/v1/products`, the MCP manifest, `/health`, and the plan recommender.
  "Contact sales" remains only as the Enterprise path and in the public
  descriptions that `tests/test_public_product_truth.py` pins (step E).
- **Events:** `upgrade_cta_shown` (metadata `plan_id`, `trigger`) is recorded
  once per identity, trigger, and UTC day on connectors and on every HTTP 402.
  `account_plan_upgrade_triggered` is kept for existing dashboards.
- **Command center:** `summarize()` now returns a `free_tier` panel (grants,
  threshold crossings, exhaustion rate, denials, abuse flags, CTA impressions
  by trigger and plan, `/go` clicks by destination, trial starts, CTA
  click-through). `/internal/observability/stats` adds the shared-ledger
  exposure (`worst_case_exposure_credits`, global cap remaining today) and the
  live config; the dashboard has a "Free Tier" section. `free_tier_exhausted`
  now also feeds `credits_exhausted_identities` in the growth funnel.
- **Trial starts** are tracked `/go/free-trial` clicks. Signups on
  matrix.blocksize.capital tagged `source_channel=mcp` live outside this repo
  and must be reconciled by the web team (section 8.3).

## 15b. Approvals recorded (Johann, 2026-09-23)

- Data terms: the existing blocksize.info data terms govern the free tier.
- Data rights: all production packages (crypto VWAP, bid/ask, state, VWAP
  windows, equities via shared bid/ask, FX, metals, analytics) cleared for the
  free scope; `FREE_TIER_ALLOWED_SERVICES` defaults to all nine. RWA pilot
  feeds remain excluded (they are not a connector service).
- External submissions (section 16): approved, to be executed after deploy.

## 16. External updates prepared, not executed (section 8 list)

Nothing below has been submitted. Each item is a copy change that must be
approved by Johann before it leaves the repo.

1. **Pay.sh / pay-skills registry** (Solana Foundation): the local
   `pay-skills/providers/blocksize/market-data/PAY.md` now reads "15,000 free
   live data credits every calendar month" and positions the allowance as a
   recurring evaluation licence. Upstream PR to `jf-cmyk/pay-skills` ->
   Solana Foundation is pending approval.
2. **Registries and connector directories**: `server.json`,
   `docs/smithery_manifest.json`, Glama, and the Claude connector listing still
   carry the neutral "starter credit: authenticated connector only" line.
   Proposed description: "15,000 free live-data credits every month for
   authenticated connector users, then from EUR 49/month; signed x402 for
   direct HTTP." Resubmission to the MCP Registry, Smithery, and Glama pending.
3. **Plugin packages** (Claude 0.5.0, OpenAI 0.6.0, Cursor 1.5.0): skill text,
   error-code contract, and Cursor README updated locally; deliverables rebuilt
   reproducibly (`scripts/build_agent_skill_packages.py`, release
   `agent-skill-0.6.0`). Resubmission to each directory is pending, after deploy.
4. **blocksize.info** (web team): add "Free: 15,000 live credits/month via
   MCP" above Developer on the pricing page, a "Start free trial" CTA that
   preserves `source_channel=mcp` and the `utm_*` labels forwarded by
   `/go/free-trial`, and a link to the existing data terms.
5. **Data terms check** (Johann, 2026-09-23: the same terms as blocksize.info,
   no separate document). Confirm `https://blocksize.info/terms-conditions-data/`
   covers the free-tier evaluation licence, "Data by Blocksize" attribution, no
   resale or redistribution, and production use requiring a subscription; amend
   that page if any clause is missing. The API already links to it from every
   licence payload (`terms_url`). Blocks launch.
6. **Data-rights confirmation** from Blocksize data ops or legal for equities,
   FX, metals, and analytics. Until received `FREE_TIER_ALLOWED_SERVICES`
   stays `crypto_vwap,crypto_bidask`. Blocks the non-crypto free scope only.

## 17. Session log (2026-09-23, step E)

- **One offer line everywhere.** `public_metadata.FREE_TIER_OFFER_LINE` renders
  "15,000 free live-data credits every month, then from EUR 49/month" from
  `FREE_TIER_MONTHLY_CREDITS` and the Developer plan price, and is used by the
  registry description (`server.json`), `PUBLIC_DESCRIPTION`, llms.txt routing,
  data-packages routing, the SEO landing pages, the pricing and manual document
  metadata, the public MCP instructions and pricing tool, and `server_info`.
- **Docs updated:** README, external README (EUR ladder with feeds, seats,
  history, 15% annual discount, trial and pricing paths), developer portal
  (welcome count fixed from the stale "50" to 15,000, EUR plan table with a
  free-tier row, Start Free Trial and See Plans buttons ahead of Contact
  Sales), Smithery manifest, pyproject description, prompt examples, agent
  skill doc, agent framework integrations, and the three superseded GTM
  headers. Claims boundary and RWA wording untouched.
- **PDFs regenerated** with `scripts/generate_blocksize_pdfs.py` (needs
  `fpdf2`, installed into the local venv; it is not a runtime dependency).
- **Truth test tightened:** `_assert_complete_access_model` in
  `tests/test_public_product_truth.py` now requires the monthly allowance
  number, a subscription CTA (EUR 49 or free trial), and rejects "per UTC day"
  and "50 credit" on every machine-readable access surface. Contact sales is
  still required as the Enterprise path.
- **Left for the section 8 approval list:** plugin package folders and their
  zipped deliverables, registry resubmissions, the Pay.sh upstream PR, the
  website pages, the data-terms check, and the data-rights confirmation.
- **Registry description cap.** MCP registries (and `/readyz`) cap the
  registry description at 100 characters, so `PUBLIC_REGISTRY_DESCRIPTION`
  is the compressed form ("Signed x402; starter credit: authenticated
  connector only; 15,000/mo free, EUR 49+; contact sales", 97 chars, asserted
  at import). Every longer surface carries the full offer line.
- **Ledger path hygiene.** `free_tier_ledger.db` is gitignored; production
  must set `FREE_TIER_LEDGER_DB_PATH=/data/free_tier_ledger.db` (already in
  `.env.example`) so the shared pool lives on the Railway volume.
