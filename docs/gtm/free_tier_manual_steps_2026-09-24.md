# Free tier launch: remaining manual steps

Date: 2026-09-24. Production runs v0.6.23 (`fd57ed9`) with the free tier live.
These steps need Johann's own accounts. Everything else from
`free_tier_launch_checklist_2026-09-23.md` is done: MCP Registry v0.6.23
published, Glama wrapper v0.2.0 pushed, pay-skills PR #208 updated and review
requested, and a one-week review scheduled for 2026-10-01 09:00.

## 1. Email to the web team (ready to send)

Subject: blocksize.info pricing page: add the free MCP tier

> Hi team,
>
> Since 24 September, the Blocksize MCP connectors for Claude, Cursor and
> ChatGPT include a free tier. Users with a verified email get 15,000 live-data
> credits every calendar month. The licence is for evaluation only, and
> "Data by Blocksize" attribution is required. Could you make four changes to
> blocksize.info?
>
> 1. Pricing page: add a "Free" column before Developer with this copy:
>    "15,000 free live-data credits every month via the MCP connectors.
>    Evaluation licence, attribution required. Start free trial."
>    The button should link to https://mcp.blocksize.info/go/free-trial,
>    which forwards to the trial signup on matrix.blocksize.capital.
> 2. Trial signup on matrix.blocksize.capital: please store `utm_source`,
>    `utm_medium`, `utm_campaign`, `utm_content` and `utm_term` from the
>    incoming URL with each new trial. Trials started from the connectors
>    arrive with `utm_medium=product` and `utm_campaign=free-tier-upgrade`;
>    `utm_source` names the connector and `utm_content` the suggested plan.
>    We need these to match trial starts to clicks from the connectors.
> 3. Keep the plan prices exactly as they are: Developer EUR 49, Start-Up
>    EUR 299, Business EUR 799 per month, annual 15% off. The connectors quote
>    these numbers, so tell me before any of them change.
> 4. Next to the free tier, link the data terms:
>    https://blocksize.info/terms-conditions-data/
>
> Thanks,
> Johann

## 2. Smithery

- Listing: https://smithery.ai/servers/blocksize/agentic-payments
- Sign in: https://smithery.ai/login with GitHub as **jf-cmyk**, never JoFoRe.
- Smithery takes its description from its own dashboard, not from the repo.
  Open the listing's settings and replace the description with:

```text
Read-only MCP discovery for Blocksize real-time market data: multi-venue crypto
VWAP, bid/ask, FX, metals and tokenized equities with provenance receipts.
Search instruments, check coverage and freshness, and build exact HTTP requests.
Eligible authenticated connector users receive 15,000 free live-data credits
every calendar month (evaluation licence, "Data by Blocksize" attribution
required), then subscription plans from EUR 49/month with a free trial. Direct
public HTTP uses signed x402. Enterprise terms: contact Blocksize sales.
```

## 3. Glama

- Connector listing (production server): https://glama.ai/mcp/connectors/info.blocksize.mcp/agentic-payments
- Server listing to claim (GitHub wrapper): https://glama.ai/mcp/servers/jf-cmyk/blocksize-agentic-payments-mcp
- Sign in at https://glama.ai with GitHub as **jf-cmyk**, open the server
  listing and use "Claim". The repository's `glama.json` already names jf-cmyk
  as maintainer, and v0.2.0 with the free-tier text is pushed.
- Status on 2026-09-25: Smithery shows the new description. Glama still shows
  the listing as unclaimed ("Claim" next to jf-cmyk) and still renders the old
  README with "50 live-data credits"; the README on GitHub already has the
  free-tier text, so Glama has not re-synced yet. After claiming, open the
  listing's Admin tab and trigger a re-sync or rebuild if it offers one.
- Glama reads the description from the repository README. If it asks for one:

```text
Read-only MCP package for Blocksize real-time market data discovery: crypto
VWAP, bid/ask, FX, metals and tokenized equities, with pricing, coverage and
x402 endpoint building. Authenticated connector users get 15,000 free
live-data credits every month; subscriptions from EUR 49/month.
```

## 4. Claude plugin directory

Release gate status: plugin and marketplace validate, a clean install from
GitHub matches the release zip byte for byte, and tag `agent-skill-0.6.0` is
public on `fd57ed9`. One step is left before submitting, the GitHub release
(the permission policy blocks Claude Code from publishing it):

```bash
gh release create agent-skill-0.6.0 -R jf-cmyk/agentic-payments --verify-tag --title "Agent skill and plugins 0.6.0 (server v0.6.23)" --notes-file tmp/release-agent-skill-0.6.0/release-notes.md deliverables/blocksize-market-data-claude-plugin-0.5.0.zip deliverables/blocksize-market-data-openai-plugin-0.6.0.zip deliverables/blocksize-market-data-cursor-plugin-1.5.0.zip deliverables/use-blocksize-market-data-universal-skill-0.6.0.zip deliverables/agent-skill-release-0.6.0.json tmp/release-agent-skill-0.6.0/SHA256SUMS
```

Requirements (Claude docs, checked 2026-09-25): a paid claude.ai plan (Pro,
Max, Team or Enterprise; free accounts cannot submit), and the GitHub account
jf-cmyk connected on claude.ai in the organization you submit from. The listing
belongs to that organization, and the first organization to submit a
repository folder keeps it, so submit from the Blocksize account
(jf@blocksize-capital.com), not a personal one.

Portal flow at https://claude.ai/directory/manage, "Submit new":
1. What would you like to submit: **Plugin bundle**.
2. Source: Repository `jf-cmyk/agentic-payments`, Plugin path
   `claude-plugin/blocksize-market-data`, Branch or tag `main`. Select
   **Validate**.
3. Listing details: read from `plugin.json` and the README; nothing to type.
4. Data handling: no personal data stored by the plugin; data goes only to the
   declared connector `https://mcp.blocksize.info/anthropic/mcp/`; nothing
   retained by the plugin; not intended for under-18s.
5. Compliance: contact email `jf@blocksize-capital.com`, tick the four
   acknowledgements.
6. Review and submit: keep **GitHub push webhook**, then **Submit for review**.

Then submit the server itself: "Submit new", **MCP connector**, URL
`https://mcp.blocksize.info/anthropic/mcp/`, from the same organization, so the
two listings can be paired.

Note: https://claude.ai/directory/manage shows no MCP server submitted to the
Claude Connectors Directory from this account. If the connector was never
submitted, "Submit a server" on that page is the next distribution step.

## 5. OpenAI (ChatGPT and Codex plugin directory)

Package check: the OpenAI plugin 0.6.0 installs cleanly with
`codex plugin marketplace add jf-cmyk/agentic-payments@agent-skill-0.6.0` and
matches the release zip byte for byte. The MCP tools already carry read-only
annotations.

Prerequisites before the form:
1. **Verified identity.** In the OpenAI Platform, complete business
   verification for Blocksize Capital GmbH (legal name, registered address,
   tax or registration number). The submitter needs "Apps Management" write
   permission in the organization.
2. **Demo account.** OpenAI requires working sign-in credentials without MFA,
   SMS or email confirmation. Create a dedicated demo user in Clerk with a
   verified email and share it only in the form.
3. **Domain verification.** Merge
   https://github.com/jf-cmyk/agentic-payments/pull/43, then set the token the
   form shows as `OPENAI_APPS_CHALLENGE_TOKEN` on Railway. It is served at
   `https://mcp.blocksize.info/.well-known/openai-apps-challenge`.

Form: https://platform.openai.com/plugins

| Field | Value |
|---|---|
| Plugin name | Blocksize Market Data |
| Category | Data & Analytics |
| MCP server URL | `https://mcp.blocksize.info/openai/mcp/` |
| Authentication | OAuth (Clerk), demo account from step 2 |
| Website | `https://mcp.blocksize.info/` |
| Support URL | `https://mcp.blocksize.info/support` |
| Privacy policy URL | `https://mcp.blocksize.info/privacy` |
| Terms URL | `https://blocksize.info/terms-conditions-data/` |
| Logo | `blocksize-cursor-plugin/plugins/blocksize-market-data/assets/logo.png` (196 px square) or `logo.svg` |
| Skill bundle | `use-blocksize-market-data` from `deliverables/blocksize-market-data-openai-plugin-0.6.0.zip` |

Short description:

```text
Live multi-venue crypto VWAP, bid/ask, FX, metals and tokenized equities with provenance.
```

Long description:

```text
Blocksize Market Data gives ChatGPT and Codex read-only access to Blocksize's institutional real-time market data. Search the instrument catalog, check coverage and freshness, then fetch multi-venue crypto VWAP, bid/ask quotes, FX rates, metals prices and tokenized equities, each with timestamps and provenance. Signed-in users with a verified email receive 15,000 free live-data credits every calendar month under an evaluation licence with "Data by Blocksize" attribution; subscriptions start at EUR 49/month with a free trial. The plugin never trades, moves funds, signs wallet messages or gives investment advice.
```

Starter prompts:

```text
What is the current multi-venue VWAP for BTC/USD?
Compare the bid/ask spreads for ETH/USD and SOL/USD.
What are the latest EUR/USD rate and gold price?
How many free Blocksize credits do I have left this month?
```

Positive test cases (tool, prompt, expected result):
1. `search_pairs`: "Find Blocksize instruments for Solana." Returns SOLUSD and related pairs.
2. `get_vwap`: "Get the BTC/USD VWAP." Returns a price with timestamp, venue data and the monthly credit line.
3. `get_bid_ask`: "Show the ETH/USD bid and ask." Returns bid, ask and spread.
4. `get_fx_rate` and `get_metal_price`: "Latest EUR/USD and gold (XAU/USD)." Returns both snapshots.
5. `get_credit_balance`: "How many credits do I have left?" Returns the monthly limit, remaining credits and reset date.

Negative test cases:
1. "Buy 1 BTC for me." The plugin declines: it is read-only and cannot trade.
2. "Get the VWAP for FAKECOIN/USD." Search finds no instrument and no paid call is made.
3. "Should I invest in ETH?" The plugin gives data only and declines investment advice.

## 6. Cursor

The public plugin repository https://github.com/jf-cmyk/blocksize-cursor-plugin
now holds release 1.5.0 (commit `22a2031`): the skill, the free-tier response
codes and the monthly free-tier wording. Its marketplace file is at the
repository root, which is what Cursor expects.

Two separate places to submit, each with its own review:
- **Official Cursor Marketplace:** https://cursor.com/marketplace/publish.
  Sign in with your Cursor account and submit the repository URL
  `https://github.com/jf-cmyk/blocksize-cursor-plugin`. Cursor reviews by hand
  and replies by email.
- **Community directory:** https://cursor.directory/plugins/new. Sign in with
  GitHub as jf-cmyk. This is faster and self-managed, but listings appear only
  on cursor.directory.

Suggested description for both:

```text
Read-only Blocksize real-time market data in Cursor: multi-venue crypto VWAP, bid/ask, FX, metals and tokenized equities with provenance, over Clerk-authenticated MCP. Includes a skill for safe market-data workflows. 15,000 free live-data credits every month for signed-in users; subscriptions from EUR 49/month.
```

## 7. Email to Blocksize data operations (ready to send)

Subject: Three catalog fixes for vwap_instruments and bidask_instruments

> Hi team,
>
> We audited every instrument the Blocksize API lists on 24 and 28 September
> (on the 28th: 7,216 VWAP, 2,765 bid/ask, 387 state pairs). The MCP connectors now hide what
> cannot be delivered, but three fixes at the source would help every client:
>
> 1. **vwap_instruments lists 2,216 tickers that vwap_latest does not know.**
>    They return `-32603 internal error: ticker X not found`, and
>    vwap_subscribe sends no snapshot for them. Most are single-pool DEX cross
>    pairs. Could they be removed from the list or flagged? The full list is in
>    `src/data/vwap_live_coverage.json` (key `unavailable`) in
>    github.com/jf-cmyk/agentic-payments.
> 2. **vwap_instruments has no activity signal.** A further 2,867 pairs return
>    a real VWAP whose last trade is older than five minutes. A last-trade time
>    or an activity flag per entry would let clients show only live pairs
>    without running a full audit.
> 3. **bidask_instruments has no asset class.** Clients have to guess from the
>    symbol whether AAPLXUSD is a tokenized stock and AVAXUSD is crypto, and
>    naming rules get this wrong for about 90 pairs. An `asset_class` field
>    (crypto, equity, fx, metal) per entry would remove the guesswork.
>
> Evidence: `docs/gtm/instrument_quality_audit_2026-09-28.csv` and
> `docs/gtm/vwap_coverage_findings_2026-09-24.md` in the same repository.
>
> Thanks,
> Johann
