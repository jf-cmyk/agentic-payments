# ICP And Offer Memo

## Recommendation

Start with **crypto/onchain data and market-data vendors/builders** as the first wedge, then expand into payments/risk-ops data vendors and research/compliance intelligence.

Reason: they already sell APIs, understand usage-based data, have agent-heavy users, and can evaluate the value of pay-per-request data quickly. The current product is shaped like a developer tool: MCP, HTTP, cURL, pay-per-call, and low prices. Enterprise finance is the bigger contract path, but it needs stronger payment verification, policy controls, audit exports, and compliance packaging before it should be the lead motion.

## Ranked ICPs

| Rank | ICP | Why now | Offer |
| --- | --- | --- | --- |
| 1 | Crypto/onchain data and market-data vendors/builders | They already sell data APIs and their users increasingly include agents | 402-gated premium query pilot |
| 2 | Payments, cash-management, and risk-ops data vendors | Repetitive machine queries, clear ROI, and high value of freshness | Credit bundle plus policy/audit preview |
| 3 | Research, compliance, filings, and news intelligence vendors | High-value bursty queries where pay-per-use can beat subscriptions | Premium answer/data slice over MCP plus HTTP |

Enterprise finance/CFO teams should be a design-partner track, not the first self-serve wedge.

## Packaging experiments

| Package | Price test | Buyer | Hypothesis |
| --- | --- | --- | --- |
| Pay-per-call | Current $0.001 to $0.008 calls | Builders and agents | Low-friction pricing drives first activation |
| Premium query | $0.25, $1.00, and $5.00 per call | High-value data vendors and agent teams | Some agent queries can support much higher prices than raw market ticks |
| Starter credits | $0.90 for 1,000 credits | Developers | Prepaid credits remove per-call payment friction |
| Pro credits | $8 for 10,000 credits | Small agent teams | Usage discounts create predictable spend |
| Design partner commit | Monthly minimum plus metered overage | Data vendors and agent teams | Commitment validates business pull beyond novelty usage |
| Builder bundle | $49/month equivalent credits plus support | Agent startups | Developers will pay for reliability and support once demo works |
| Enterprise pilot | Custom, starting with design partner | Finance/platform teams | Audit and policy controls justify larger contract values |

## First revenue experiment

Experiment: **2-week 402-gated premium query pilot**

Audience:

- 10 to 15 design partners from crypto/onchain data, market-data, and data-agent teams.

Promise:

- "Offer one high-value data slice that agents can buy per request over MCP and HTTP 402, with no account, invoice, or sales portal."

Assets needed:

- Known-good cURL flow.
- Testnet/mock payment mode.
- 1-page quickstart.
- Short demo video or screenshot walkthrough.
- Clear limits and "verified today vs planned" trust note.
- Three price tests: $0.25, $1.00, and $5.00 per premium call, or equivalent credit bundles.

Success criteria:

- 5+ paying teams.
- 100+ paid calls.
- 3+ conversations for a larger annual or committed plan.
- Clear signal on the right price floor.

## Founder decisions needed

1. Choose first wedge: crypto/onchain data, payments/risk-ops data, or research/compliance data.
2. Decide the unit of sale: per call, per dataset, credit bundle, or commit plus overage.
3. Decide whether MCP is the primary discovery layer or a companion surface to HTTP.
4. Decide how hard to lean into compliance: KYT, audit logs, SLAs, licensing, and entitlement controls.
5. Decide whether Blocksize stays a data monetization layer or also becomes a discovery marketplace for paid services.
