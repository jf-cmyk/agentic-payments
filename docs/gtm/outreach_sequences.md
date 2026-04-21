# Outreach Sequences

Prepared: 2026-04-21

Status: draft only. Do not send without Johann's explicit approval.

## Sequence Rules

- Goal: design-partner conversations, not immediate selling.
- Tone: technical, concise, and specific.
- Ask: 20 minutes or a 5-minute demo review.
- Do not claim production-grade payment verification until amount, recipient, asset, network, freshness, replay persistence, and wallet ownership controls are implemented.
- Do not mention rates/Treasury as an offered product.
- Do not post, deploy, or automate outreach from this Mac.

## Core Email

Subject: Agent-paid market data over HTTP 402

Hi {{first_name}},

We are testing a Blocksize flow where an AI agent can request financial data, receive an HTTP 402 payment challenge, pay per call over x402, and get the data back without account setup or an API-key sales loop.

The first demo is intentionally narrow: crypto market data over HTTP/MCP, with FX, metals, equities, and analytics in the broader portfolio. Rates/Treasury are intentionally out of scope.

Would you be open to a 20-minute design-partner call? I am mainly trying to learn whether your agents, customers, or developers would prefer per-call payments, prepaid credits, or standard API subscriptions for high-value data.

If useful, I can show the 5-minute demo and you can tell me what breaks.

Best,
Johann

## Short Founder/Partner DM

We are testing agent-paid financial data for Blocksize: an agent requests a market-data endpoint, gets a 402, pays via x402, and receives the response over HTTP/MCP. Looking for design-partner feedback, not selling yet. Open to a 20-min teardown?

## Agent Tooling Variant

Subject: Should browser/search agents be able to buy data per call?

Hi {{first_name}},

Your users are already building agents that search, browse, fetch, and act. We are testing the financial-data side of that loop: an agent hits a paid endpoint, receives a 402 challenge, pays per call, and gets institutional market data back.

I would love your take on whether this should exist as a tool/integration for agent builders, and what trust controls would be required before it is useful.

Would you review a 5-minute demo this week?

## Data API Variant

Subject: Can agents buy one premium data query at a time?

Hi {{first_name}},

We are testing whether data APIs can turn agent traffic into paid usage without forcing agents through accounts, subscriptions, or manual API-key provisioning.

The Blocksize demo is a narrow x402/MCP flow: request data, receive HTTP 402, pay per call, receive the result. We are using crypto market data as the first wedge, with FX, metals, equities, and analytics as the broader portfolio.

The question I would love your feedback on: which one premium query would you expose if an agent could pay for it instantly?

Open to a 20-minute design-partner call?

## Protocol Partner Variant

Subject: Financial-data seller use case for x402

Hi {{first_name}},

Blocksize is turning agentic payments into a focused financial-data seller use case: x402-gated market data over HTTP/MCP, clear per-call prices, and a trust roadmap for payment proof, replay protection, and policy controls.

I would like to sanity-check the flow against the current x402 ecosystem and understand what would make it credible as a showcase integration.

Could I get 20 minutes for a technical teardown?

## Follow-Up 1

Subject: Re: agent-paid market data

Quick bump. The useful feedback for us is very specific:

- Would agents in your ecosystem pay per call for data?
- What price shape makes sense: per-call, prepaid credits, or monthly commit?
- What proof/audit controls would block adoption?

Happy to show the 5-minute demo or just send the one-page quickstart.

## Follow-Up 2

Subject: closing the loop

Closing the loop here. If agent-paid data is not relevant right now, no worries.

If it is interesting later, the core test is simple: can an agent buy one high-value financial-data response over HTTP without an account, invoice, or manual checkout?

I would still value a quick "this is useful / this is wrong" reply.

## Call Agenda

1. Two-minute context: Blocksize, x402, MCP, current demo.
2. Five-minute demo: request, 402 challenge, payment/mock payment, response.
3. Ten-minute discovery:
   - Where are agents touching your product today?
   - What data calls are high-value enough for per-call pricing?
   - What payment/proof controls would you require?
   - Would you distribute, buy, or ignore this?
4. Three-minute next step:
   - Design partner demo access.
   - Pricing experiment.
   - Technical integration feedback.

## Reply Routing

Positive reply:

> Great. I will send a short agenda and keep it to 20 minutes. The demo is intentionally narrow so we can focus on whether the payment/data primitive is useful.

Technical skeptic:

> That is exactly the feedback we want. The current local demo proves the 402 flow; the production trust roadmap is payment amount/recipient validation, replay persistence, and signed wallet ownership. I would value a teardown of what is missing.

Pricing concern:

> Agreed. We are testing three shapes: direct per-call, prepaid credits, and monthly commit plus overage. The goal is to find the point where agents can buy one useful answer without procurement overhead.

Not relevant:

> Helpful, thank you. Is there a team in your orbit that is closer to agent/API monetization or paid data access?
