# KPI Instrumentation Spec

Goal: track whether Blocksize Agentic Payments is becoming a repeatable revenue engine.

## North-star metric

**Weekly paid agent data calls from non-founder wallets or approved design partners.**

Why: it combines activation, payment success, and repeatable product value. Revenue can be derived from it, but paid calls are the earliest strong signal.

## Minimum event model

| Event | Trigger | Required fields |
| --- | --- | --- |
| `free_discovery_call` | `/v1/search`, `/v1/instruments`, pricing info, manifest | timestamp, endpoint, query/service, IP hash, user agent, referrer |
| `payment_required` | Paid endpoint returns 402 | timestamp, endpoint, asset, price_usdc, networks_offered, IP hash, agent wallet if present |
| `payment_proof_submitted` | `PAYMENT-SIGNATURE` received | timestamp, endpoint, network, proof hash, wallet if known |
| `payment_verified` | Proof accepted | timestamp, endpoint, network, price_usdc, latency_ms, wallet/payor, asset, pay_to |
| `payment_failed` | Proof rejected or RPC unavailable | timestamp, endpoint, network, reason, latency_ms |
| `credit_trial_granted` | Welcome credits granted | timestamp, wallet, IP hash, balance check result, history check result |
| `credit_drawdown_success` | Credits spent | timestamp, wallet, endpoint, credits_spent, balance_after |
| `credit_drawdown_failed` | Insufficient credits or eligibility fail | timestamp, wallet, endpoint, reason |
| `bulk_credit_challenge` | `/v1/credits/purchase` 402 emitted | timestamp, tier, price_usdc, credits |
| `bulk_credit_claimed` | `/v1/credits/claim` accepted | timestamp, tier, wallet, credits_added, tx_hash |

## Core dashboard

| Metric | Definition | Cadence |
| --- | --- | --- |
| Paid calls | Count of `payment_verified` plus successful credit drawdowns | Daily/weekly |
| Net revenue | Sum of verified paid-call prices and credit purchases | Daily/weekly |
| Active paying wallets | Unique wallets with paid success | Weekly |
| First paid-call conversion | Unique wallets with first paid success / unique wallets or users that saw 402 | Weekly |
| Time to first paid call | First discovery or 402 to first success | Weekly |
| 402 challenge volume | Count of `payment_required` | Daily |
| Payment success rate | `payment_verified` / `payment_proof_submitted` | Daily |
| Credit usage rate | Credit drawdown successes / total paid data requests | Weekly |
| Endpoint mix | Paid calls by endpoint and asset class | Weekly |
| Gross margin per call | Revenue less RPC/payment/data costs | Weekly |
| Demo completion | Demo starts to known-good paid-call success | Weekly |

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

