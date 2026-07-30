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
| Starter-to-paid | Starter-credit activated verified identities later tied to finalized x402 settlement / starter-credit activated verified identities | 5% | Improve upgrade prompts, payment support and offer packaging |
| Server error rate | HTTP `5xx` responses / HTTP requests; payment prompts, auth challenges, rate limits and client/protocol `4xx` responses are reported separately | Below 1% | Stop acquisition work and fix server reliability if breached |
| Post-credit failure rate | Charged HTTP or MCP delivery failures / successful plus failed charged deliveries | 0% | Refund, retry and fix before scaling acquisition |
| Unsupported demand | Bounded zero-result symbol searches, ranked by request count | No fixed target | Prioritize source expansion by demonstrated demand |

Targets are provisional operating thresholds, not external benchmarks. Replace them after four complete weekly cohorts exist.

## RWA pilot

The pilot contains exactly three candidate feeds:

| Feed | Runtime source | Monitoring lane |
| --- | --- | --- |
| AAPL/USDC | Hyperliquid public order-book API and venue-native rolling activity | Native L2 plus 24-hour base/notional volume |
| PAXG/USDC | Uniswap pool state through Ethereum RPC | Block-pinned state, initialized ticks and decoded Swap logs |
| EURC/USDC | Aerodrome pool state through Base RPC | Block-pinned state, initialized ticks and decoded Swap logs |

`scripts/run_rwa_growth_pilot.py` captures replayable outcomes into the same authoritative `RWAObservationStore` SQLite ledger used by the operator service. The production scheduler is enabled through `RWA_GROWTH_PILOT_ENABLED`, runs every 30 minutes, records both successes and failures, and derives dashboard status and freshness from that ledger. Source-specific volume, depth, initialized-tick and benchmark evidence is retained with each applicable observation. These are evidence inputs only: proxy semantics, stale timestamps, lineage, independence and rights remain explicit blockers, and the scheduler can never promote a feed automatically. It does not read or write a parallel JSONL/status source of truth.

The runtime data boundary is explicit:

- AAPL book and rolling volume come directly from Hyperliquid public Info endpoints.
- PAXG and EURC pool state, initialized liquidity ticks and Swap events come from EVM JSON-RPC.
- When an RPC plan limits log ranges, a per-pool cache on the Railway volume starts with a bounded 30-minute window and appends only unseen blocks each cycle. It cannot count as a complete 24-hour window until at least 23 hours are continuously covered.
- Tiingo is not used by any of the three runtime lanes.
- Synthetic pool levels are excluded from executable-depth evidence.
- A failed 24-hour log backfill does not erase successfully captured block-pinned tick replay; the missing volume window remains a separate failed gate.

Before the first v3 deployment, migrate any legacy production JSONL once and
without a live probe:

```bash
python scripts/run_rwa_growth_pilot.py \
  --db-path /data/rwa_observations.v2.db \
  --legacy-history /data/rwa_growth_pilot_history.jsonl \
  --import-only
```

The migration is idempotent and reports imported, duplicate, and rejected
rows. Preserve the legacy file as rollback evidence until the v3 ledger and
dashboard have passed staging acceptance.

Monitoring thresholds:

- At least 14 elapsed days.
- At least 672 observations per feed.
- At least 99% source success.
- At least 99% freshness compliance.
- 100% bid/ask sanity for successful samples.

Even when every monitoring threshold passes, promotion remains blocked until independent benchmark alignment, depth/manipulation review, source-independence review, rights/redistribution signoff and explicit human approval are complete. The scheduler cannot promote a feed.

The live pre-deployment validation on 2026-07-24 captured both EVM pool states and exact initialized-tick replay. EURC also produced a complete 24-hour decoded Swap-event window with more than $3.2 million in quote turnover and passed the point-in-time $10,000 block check. AAPL reported zero venue-native 24-hour volume and insufficient point-in-time depth. PAXG retained a 128-tick replay. Its provider permits five-block log ranges, so production accumulates the 24-hour window incrementally instead of treating the missing backfill as zero volume. These observations do not complete a sustained gate; production-promoted expansion feeds remain zero.

Blocksize reference snapshots are captured concurrently with the pilot observations. Depth and tick replay run afterward so their latency cannot create an artificial benchmark timestamp gap. A comparison still fails alignment when the benchmark's own source timestamp is stale; thresholds are not relaxed to conceal source cadence.

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
- A deduplicated x402 settlement plus durable local finalization is required for recognized revenue.
- Synthetic/test traffic must remain distinguishable from customer traffic.
- A point-in-time RWA source success is not a production promotion.
- Tiingo is not a runtime dependency for the three-feed RWA pilot.
- Directory submissions, marketplace metrics, outreach and Jira synchronization require the relevant connected account or owner.
