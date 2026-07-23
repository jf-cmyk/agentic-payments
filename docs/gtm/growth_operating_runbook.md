# Growth operating runbook

Date started: 2026-07-23

## Objective

Turn Blocksize agent discovery into repeat live-price usage and verified paid usage while advancing a deliberately small RWA candidate pilot through evidence gates.

The landing-page message remains the existing Blocksize live-market-data offer. RWA is supporting authority and a monitored expansion lane, not the primary landing-page claim.

## Weekly operating metrics

The protected production command center at `/internal/observability/command-center` is the source of truth. Its underlying source is the configured `UsageEventStore` SQLite database on the Railway volume.

| KPI | Exact definition | Provisional target | Decision |
| --- | --- | ---: | --- |
| Activation rate | Explicit identities with `first_live_price_delivered` / eligible explicit identities in the selected window | Establish baseline, then improve weekly | Fix discovery or first-price friction when this falls |
| First price under 3 minutes | Attributed activations reached within 180 seconds of first eligible event | 50% | Simplify quickstart and identity/payment instructions |
| Seven-day repeat | Mature activated identities with at least two successful delivery events during their first seven days / mature activated identities | 25% | Improve recurring workflows and product utility |
| Starter-to-paid | Starter-credit activated identities later tied to verified x402 payment or prepaid credit claim / starter-credit activated identities | 5% | Improve upgrade prompts, payment support and offer packaging |
| Server error rate | HTTP `5xx` responses / HTTP requests; payment prompts, auth challenges, rate limits and client/protocol `4xx` responses are reported separately | Below 1% | Stop acquisition work and fix server reliability if breached |
| Post-credit failure rate | Charged HTTP or MCP delivery failures / successful plus failed charged deliveries | 0% | Refund, retry and fix before scaling acquisition |
| Unsupported demand | Bounded zero-result symbol searches, ranked by request count | No fixed target | Prioritize source expansion by demonstrated demand |

Targets are provisional operating thresholds, not external benchmarks. Replace them after four complete weekly cohorts exist.

## RWA pilot

The pilot contains exactly three candidate feeds:

| Feed | Runtime source | Monitoring lane |
| --- | --- | --- |
| AAPL/USDC | Hyperliquid public order-book API | Venue API order book |
| PAXG/USDC | Uniswap pool state through Ethereum RPC | Ethereum onchain pool state |
| EURC/USDC | Aerodrome pool state through Base RPC | Base onchain pool state |

`scripts/run_rwa_growth_pilot.py` captures replayable raw observations and writes a bounded readiness status. The production scheduler is enabled through `RWA_GROWTH_PILOT_ENABLED`, runs every 30 minutes and persists its history and latest status on the Railway volume.

Monitoring thresholds:

- At least 14 elapsed days.
- At least 672 observations per feed.
- At least 99% source success.
- At least 99% freshness compliance.
- 100% bid/ask sanity for successful samples.

Even when every monitoring threshold passes, promotion remains blocked until independent benchmark alignment, depth/manipulation review, source-independence review, rights/redistribution signoff and explicit human approval are complete. The scheduler cannot promote a feed.

The latest production capture on 2026-07-23 succeeded for all three feeds. The first production PAXG attempt failed because the Ethereum RPC variable was absent; the configured RPC was added and the following capture passed 3/3. This proves current reachability only; the sample/window gates remain open and production-promoted expansion feeds remain zero.

## Weekly cadence

Monday:

1. Review Growth Funnel, Payment Funnel, Data Popularity and RWA Pilot in the protected command center.
2. Export the top unsupported-symbol opportunities.
3. Select one acquisition, conversion and reliability action for the week.

Wednesday:

1. Check directory/referral attribution and marketplace metric ingestion.
2. Review failed payment, exhausted-credit and charged-delivery events.
3. Check RWA freshness/success drift without changing promotion state.

Friday:

1. Record KPI movement and evidence quality.
2. Update the Jira import/reconciliation pack.
3. Decide whether to continue, change or stop each active experiment.

## Evidence and guardrails

- Identities are stored only as salted hashes; IP fingerprints are not used as funnel identities.
- Direct x402 or prepaid credit verification is required for recognized revenue.
- Synthetic/test traffic must remain distinguishable from customer traffic.
- A point-in-time RWA source success is not a production promotion.
- Tiingo is not a runtime dependency for the three-feed RWA pilot.
- Directory submissions, marketplace metrics, outreach and Jira synchronization require the relevant connected account or owner.
