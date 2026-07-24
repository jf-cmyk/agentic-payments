# Blocksize growth and RWA production baseline

Generated from the protected production command center on 2026-07-23 at 19:28:35 UTC. The reporting window is 30 days, but identity-attributed growth events began with the 2026-07-23 release. Legacy traffic must not be treated as a comparable conversion cohort.

## Executive readout

- Distribution is producing meaningful discovery traffic: 13,403 registry requests and 45,191 free discovery calls were observed in the 30-day window.
- Recognized monetization remains small: 2 paid calls and $0.018 USDC in estimated recognized revenue.
- The new explicit-identity cohort contains 5 eligible identities, 0 attributable activations and 2 exhausted-credit identities. One older activation exists without identity attribution. The displayed 0% activation rate is an instrumentation-era baseline, not evidence that all historical acquisition failed.
- The only unsupported-symbol row is `DATA-API-FOR-AI`, requested once through local MCP. It is prose-like telemetry noise, not valid ticker demand, so no sourcing action is justified.
- The latest RWA production capture passed 3/3. Production-promoted expansion feeds remain zero.

## Growth baseline

| Metric | Current value | Interpretation |
| --- | ---: | --- |
| Eligible explicit identities | 5 | Post-release attribution cohort only |
| Attributable activations | 0 | No valid conversion conclusion yet |
| Legacy/unattributed activations | 1 | Keep separate; do not backfill from IP fingerprints |
| Credits exhausted | 2 | First immediate upgrade-friction cohort to inspect |
| Mature seven-day cohort | 0 | Repeat-rate denominator is not yet available |
| Starter-to-paid cohort | 0 | Too early for a conversion rate |
| Paid calls | 2 | Verified paid or credit-backed calls in the window |
| Estimated recognized revenue | $0.018 USDC | Not gross marketplace revenue |
| Unique client fingerprints | 4,772 | Abuse/traffic signal, not a growth identity count |

## Acquisition attribution

| Source | Locally recorded calls | External metric state |
| --- | ---: | --- |
| x402scan | 4,908 | Local attribution only |
| Pay.sh | 2,484 | Marketplace metrics not ingested |
| MCP Registry | 240 | Local attribution only |
| GitHub | 240 | Repository referrals only |
| Glama | 217 | Local attribution only |
| Awesome MCP | 90 | Submission referrals only |
| Smithery | 67 | Marketplace metrics not ingested |
| GitLab | 64 | Repository referrals only |

## Reliability caveat

The legacy 26.1% HTTP error rate excluding `402 Payment Required` is not a safe product-reliability KPI. The status population is dominated by MCP negotiation, crawler probes, rate limiting and malformed requests (`404`, `400`, `429`, `405`, `406`, `422`). Only one `500` is present in the full status mix. The accompanying instrumentation change separates expected client/protocol responses from server failures and post-credit delivery failures before this metric drives acquisition decisions.

## RWA production monitor

| Feed | Samples | Successful samples | Latest state |
| --- | ---: | ---: | --- |
| AAPL/USDC via Hyperliquid | 2 | 2 | Latest capture passed |
| PAXG/USDC via Ethereum RPC and Uniswap | 2 | 1 | Initial missing-RPC failure fixed; latest capture passed |
| EURC/USDC via Base RPC and Aerodrome | 2 | 2 | Latest capture passed |

Monitoring readiness and promotion readiness remain false. Each feed still needs at least 14 elapsed days, 672 samples, source success/freshness/sanity thresholds, benchmark alignment, manipulation/depth review, source-independence review, rights signoff and explicit human approval.

## Prioritized actions

1. Treat 2026-07-23 as cohort day zero and collect identity-attributed events without IP-based identity substitution.
2. Segment expected protocol/client responses from real server and post-credit failures.
3. Review the two exhausted-credit identities as an aggregate cohort and improve upgrade guidance if no verified conversion follows.
4. Ingest Pay.sh and Smithery marketplace-side metrics or retain their status as explicitly unknown.
5. Reject prose-like unsupported searches from the sourcing backlog; source only repeated, valid symbol demand.
6. Continue the three-feed RWA monitor without changing promotion state.
