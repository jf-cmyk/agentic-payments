# Protocol And Partner Memo

Assessment date: 2026-04-21

## Strategic read

The market is splitting into three layers:

- Open standards: x402, AP2, ACP.
- Rails/platforms: Stripe, Circle, Coinbase/Base, wallet infrastructure.
- Control wrappers: Nevermined, Skyfire/KYA, Payman-style approval workflows.

Blocksize should position as the **neutral data, policy, metering, and audit layer for data-for-agents**, not as a single-rail payment vendor.

Current market signal: x402.org reports meaningful live activity in the protocol ecosystem, including tens of millions of recent transactions and more than $20M in recent volume as of this assessment date. Treat exact public counters as volatile and refresh them before using in investor, sales, or public materials.

## Comparison

| Surface | Positioning signal | What it means for Blocksize | Priority |
| --- | --- | --- | --- |
| x402 | HTTP-native payment negotiation over 402 with optional facilitators | Best wedge for pay-per-call data/API monetization | Integrate and lead |
| AP2 | Agent payment trust layer using mandates, verifiable credentials, and MCP/A2A alignment | Useful for enterprise authorization and audit posture | Watch and map adapter |
| ACP | OpenAI/Stripe checkout-oriented protocol for in-context commerce | Strong for merchant checkout, weaker for data APIs | Monitor for distribution |
| Nevermined | x402 plus smart accounts, credits, subscriptions, and delegated execution | Direct benchmark and possible partner for monetized agents | Partner/watch |
| Skyfire/KYA | Agent identity, verified access, selective disclosure, KYA+Pay | Useful for premium/identity-gated data access | Partner/watch |
| Stripe | Agentic commerce, shared payment tokens, x402/machine-payment work | Distribution giant; can mainstream or commoditize payment layer | High-priority watch |
| Circle Nanopayments | USDC sub-cent/gas-free payment primitive, testnet as of March 2026 | Strong fit for Blocksize micropayment economics | High-priority partner |
| Payman-style workflows | Human approval queues and policy thresholds | Complement for enterprise guardrails | Use as control concept |

## Positioning implication

Use this line:

**Blocksize is the agent-readable financial data and trust layer that can monetize through x402 today and adapt to AP2, ACP, Circle, Stripe, identity, and approval-controlled flows as the market standardizes.**

## Integration priority

1. Harden x402-compatible pay-per-call flow and docs.
2. Add policy metadata and audit artifacts around every paid call.
3. Track Circle Nanopayments as a settlement option for sub-cent economics.
4. Map AP2 mandates to Blocksize payment policies for enterprise pilots.
5. Track Stripe/ACP for merchant distribution but avoid becoming only a checkout plugin.
6. Explore Skyfire/KYA only when identity-gated access becomes a prospect requirement.

## Sources

- x402: https://www.x402.org/
- Google AP2: https://cloud.google.com/blog/products/ai-machine-learning/announcing-agents-to-payments-ap2-protocol
- OpenAI ACP: https://openai.com/blog/buy-it-in-chatgpt/
- Stripe agentic commerce: https://docs.stripe.com/agentic-commerce
- Nevermined x402: https://docs.nevermined.app/docs/development-guide/nevermined-x402
- Skyfire KYA: https://docs.skyfire.xyz/docs/know-your-agent-kya-copy
- Circle Nanopayments: https://www.circle.com/blog/circle-nanopayments-launches-on-testnet-as-the-core-primitive-for-agentic-economic-activity
- McKinsey payments: https://www.mckinsey.com/industries/financial-services/our-insights/global-payments-report
- McKinsey agentic commerce: https://www.mckinsey.com/capabilities/quantumblack/our-insights/the-automation-curve-in-agentic-commerce
