# Free tier: launch checklist and prepared external copy

Date: 2026-09-23
Branch: `feat/free-tier-monthly-pool` (base: production `github/main` 23eba0b, v0.6.22)
Companion: `docs/gtm/free_tier_dev_checkpoint_2026-09-23.md` (design, decisions, session log)

Nothing in this file has been executed. Sections 2 and 3 need Johann's explicit
approval item by item; section 1 is the operator's own checklist.

## 1. Pre-deploy checklist (Railway, staging first)

Follow `docs/gtm/gitlab_railway_deploy.md` for the deploy mechanics. Before the
first staging deploy of this branch:

1. **Environment variables** (from `.env.example`, Free tier block):
   - Set `FREE_TIER_ENABLED=true`, `FREE_TIER_MONTHLY_CREDITS=15000`,
     `FREE_TIER_PER_MINUTE_CREDITS=30`, `FREE_TIER_DAILY_SOFT_CAP_CREDITS=2000`,
     `FREE_TIER_MAX_BATCH_ITEMS=5`, `FREE_TIER_GLOBAL_DAILY_CAP_CREDITS=200000`.
   - Set `FREE_TIER_ALLOWED_SERVICES=crypto_vwap,crypto_bidask,crypto_state,crypto_vwap_30m,crypto_vwap_24h,equity_bidask,fx,metals,analytics`
     (all production packages; legal approved on 2026-09-23).
   - Set `FREE_TIER_LEDGER_DB_PATH=/data/free_tier_ledger.db` (the shared pool
     must live on the persistent volume; `/health.free_tier.ledger_on_persistent_volume`
     verifies it).
   - Set `FREE_TIER_EMAIL_HASH_SALT` to a fresh 48+ character secret, distinct
     from the other privacy salts (falls back to `OBSERVABILITY_HASH_SALT` if empty).
   - Keep `FREE_TIER_REQUIRE_VERIFIED_EMAIL=true`.
   - **Remove** `ANTHROPIC_DAILY_CREDITS`, `CURSOR_DAILY_CREDITS`,
     `OPENAI_DAILY_CREDITS`, and `STARTER_CREDIT_ALLOWANCE`. They are ignored;
     if left set, `/health.free_tier.legacy_env_warnings` lists them and the
     startup log warns.
2. **Verify after deploy**:
   - `GET /health` -> `free_tier.enabled=true`, `monthly_credits=15000`,
     `allowed_services` listing all nine production packages,
     `ledger_on_persistent_volume=true`, `legacy_env_warnings=[]`,
     `version` matches the release.
   - `GET /readyz` -> 200.
   - One authenticated connector call (Claude or Cursor) returns the live price
     with `Credits remaining this month: 14999/15000` and the
     `Data by Blocksize` line; `get_credit_balance` shows `shared_pool`.
   - Confirm the Clerk token carries `email_verified`: call a live tool with a
     freshly signed-in account and check that `/internal/observability/stats`
     records `free_tier_grant_created`. If Clerk omits the claim the gate fails
     open (allowed); if it sends `false` for a real verified user, set
     `FREE_TIER_REQUIRE_VERIFIED_EMAIL=false` and open a Clerk ticket.
   - `/internal/observability` shows the Free Tier section with one grant.
3. **Existing users**: rows still on the retired 50-credit default move to the
   monthly pool automatically; explicit subscriber overrides are preserved as
   `allowance_overrides` (entitlement schema v4). No data migration to run.
4. **Rollback**: the v0.6.22 server reads the same tables. Rolling back keeps
   balances visible; the `allowance_overrides` and free-tier ledger tables are
   ignored by the older release.
5. **Kill switch**: `FREE_TIER_ENABLED=false` stops free grants immediately with
   a `FREE_TIER_DISABLED` payload (never a 5xx); x402 and subscriptions keep working.

## 2. Prepared external copy (approve each before submission)

### 2.1 MCP Registry (`server.json`, 100-character cap)

```
Signed x402; starter credit: authenticated connector only; 15,000/mo free, EUR 49+; contact sales
```

### 2.2 Smithery, Glama, Claude connector directory (long description)

```
Read-only MCP discovery for Blocksize real-time market data, with free synthetic
value previews and attributed x402 purchase handoffs. Eligible authenticated
connector users receive 15,000 free live-data credits every calendar month
(evaluation licence, attribution required), then subscription plans from
EUR 49/month with a free trial. Direct public HTTP uses signed x402. Enterprise
terms: contact Blocksize sales.
```

### 2.3 Pay.sh / pay-skills (Solana Foundation) `PAY.md`

Local copy already updated (`pay-skills/providers/blocksize/market-data/PAY.md`).
Upstream PR title: "blocksize/market-data: monthly free tier and subscription
ladder". Body: the two changed paragraphs (intro sentence and the "Starter
allowance" section). Prices for Pay.sh HTTP routes are unchanged.

### 2.4 Plugin packages (Claude, OpenAI, Cursor)

Replace, in each `skills/use-blocksize-market-data/references/tool-surfaces.md`:

```
- Starter credits are an evaluation allowance, not a free-forever production tier.
```
with
```
- The free tier is a recurring monthly evaluation allowance (15,000 live-data
  credits) with required "Data by Blocksize" attribution; production commercial
  use needs a subscription (free trial at /go/free-trial).
```
and in the Cursor plugin README the sentence "This is not a free-forever tier;
production usage can continue..." with "The allowance renews every calendar
month; production usage continues through a subscription (free trial at
/go/free-trial) or direct Blocksize x402 outside Cursor." Add the new error
codes to `references/response-contract.md`: `FREE_TIER_RATE_LIMITED` (retry
after `retry_after_seconds`), `FREE_TIER_DAILY_CAP_REACHED`,
`FREE_TIER_AT_CAPACITY`, `FREE_TIER_SUSPENDED`, `FREE_TIER_DISABLED`,
`FREE_TIER_INELIGIBLE`, `FREE_TIER_SCOPE_EXCLUDED` (all: stop, report the
boundary and the upgrade CTA, never retry in a loop). Rebuild the three zip
deliverables (tests compare them byte-for-byte), bump the package versions,
and resubmit each package.

### 2.5 blocksize.info (web team)

- Pricing page: add a "Free" column above Developer: "15,000 free live-data
  credits every month via the MCP connectors. Evaluation licence, attribution
  required. Start free trial." linking to matrix.blocksize.capital with
  `source_channel=mcp` preserved.
- The trial signup form must record `source_channel` and the forwarded
  `utm_source`, `utm_medium`, `utm_campaign`, `utm_content`, `utm_term` from
  `/go/free-trial`, so trial starts can be reconciled with the dashboard's
  `/go` clicks.
- Link the Developer, Start-Up, Business rows to `/go/pricing` targets and keep
  the published prices in EUR (49 / 299 / 799, annual 15% off) in sync with
  `src/commercial_plans.py`.
- Add a link to the data terms (`/terms-conditions-data/`) next to the free tier.

## 3. Approvals that block launch

1. **Data terms check**: approved by Johann on 2026-09-23 (the existing
   `https://blocksize.info/terms-conditions-data/` governs the free tier).
2. **Data rights**: approved by Johann on 2026-09-23 for all production
   packages; `FREE_TIER_ALLOWED_SERVICES` now defaults to all nine.
3. **Section 2 submissions**: approved in principle on 2026-09-23; execute only
   after the new server is deployed, so no listing advertises 15,000 credits
   while production still serves the old allowance.

## 4. First-week monitoring (command center)

- North star: `/go/free-trial` clicks (Free Tier panel "Trial starts") and
  reconciled matrix signups tagged `source_channel=mcp`.
- Watch: grants per day, pool consumption distribution (threshold crossings),
  exhaustion rate, CTA impressions to clicks, abuse flags per 1,000 grants,
  global cap remaining (`free_tier.ledger.global_cap_remaining_today`).
- Alerts to expect: `free-tier-global-cap-pressure` (calibrate the cap against
  upstream limits and cache hit rate after one week), `free-tier-abuse-flags`
  (review suspended grants), `free-tier-cta-not-converting`.
- Review weekly per `docs/gtm/growth_operating_runbook.md` with `days=7` and
  synthetic events excluded.
