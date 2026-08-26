# KPI Instrumentation Spec

Goal: track whether Blocksize Agentic Payments is becoming a repeatable revenue engine.

## North-star metric

**Weekly paid agent data calls from non-founder wallets or approved design partners.**

Why: it combines activation, payment success, and repeatable product value. Revenue can be derived from it, but paid calls are the earliest strong signal.

## Minimum event model

| Event | Trigger | Required fields |
| --- | --- | --- |
| `free_discovery_call` | `/v1/coverage`, `/v1/search`, `/v1/instruments`, RWA discovery | timestamp, endpoint, normalized query/service, IP hash, user agent, referrer |
| `catalog_search_completed` | A paginated search completes | timestamp, normalized query, asset class, total matches, returned matches, limit, offset, has_more |
| `coverage_catalog_view` | Unified coverage is requested | timestamp, available namespace count, RWA canonical count, decision-grade count, economic-write lock state |
| `payment_required` | Paid endpoint returns 402 | timestamp, endpoint, asset, price_usdc, networks_offered, IP hash |
| `payment_proof_submitted` | `PAYMENT-SIGNATURE` received | timestamp, endpoint, attempt_id, proof hash |
| `payment_authorization_verified` | Facilitator accepts authorization, before settlement | timestamp, endpoint, attempt_id, payment_id, network, price_usdc |
| `payment_settled` | Settlement succeeds and the replayable response is durably finalized | timestamp, endpoint, attempt_id, payment_id, network, price_usdc, verified payer hash |
| `payment_failed` | Proof rejected or RPC unavailable | timestamp, endpoint, network, reason, latency_ms |
| `credit_trial_granted` | Welcome credits granted | timestamp, wallet, IP hash, balance check result, history check result |
| `credit_drawdown_success` | Credits spent | timestamp, wallet, endpoint, credits_spent, balance_after |
| `credit_drawdown_failed` | Insufficient credits or eligibility fail | timestamp, wallet, endpoint, reason |

## Core dashboard

| Metric | Definition | Cadence |
| --- | --- | --- |
| Paid calls | Correlated terminal `data_delivered` and `mcp_data_delivered` events only | Daily/weekly |
| Net revenue | Deduplicated `payment_settled` prices by payment_id | Daily/weekly |
| Active paying wallets | Unique wallets with paid success | Weekly |
| First paid-call conversion | Unique wallets with first paid success / unique wallets or users that saw 402 | Weekly |
| Time to first paid call | First discovery or 402 to first success | Weekly |
| 402 challenge volume | Count of `payment_required` | Daily |
| Payment success rate | Correlated `payment_settled` attempt_ids / submitted attempt_ids | Daily |
| Credit usage rate | Credit drawdown successes / total paid data requests | Weekly |
| Endpoint mix | Paid calls by endpoint and asset class | Weekly |
| Gross margin per call | Revenue less RPC/payment/data costs | Weekly |
| Demo completion | Demo starts to known-good paid-call success | Weekly |
| Discovery-to-402 rate | Trusted identities that receive a 402 after search or coverage discovery / trusted discovery identities | Weekly |
| Coverage-to-delivery rate | Trusted identities with a delivered live result after viewing unified coverage / trusted coverage viewers | Weekly |

Search-result pagination and starter credits are separate concepts. Search is
free and `total_matches` measures catalog matches; the default 50 search rows
is only a page size. Starter credits are consumed by live-data products at the
rates published by `/v1/products` and `/v1/coverage`, regardless of how many
symbols search can discover.

## First implementation path

1. Add structured JSON logging for the event model in `src/resource_server.py`.
2. Persist events to a local SQLite table first, then move to Postgres/hosted analytics after the event shape stabilizes.
3. Add a local script to summarize daily metrics from the event table.
4. Add dashboard screenshots or CSV export for daily approval briefs.
5. Only after proof: wire metrics into a hosted dashboard.

## Do not track yet

- Raw wallet private data.
- Full IP addresses.
- Unredacted payment signatures.
- Secrets or upstream API keys.
- Any compliance-sensitive user identity fields unless approved and legally reviewed.
