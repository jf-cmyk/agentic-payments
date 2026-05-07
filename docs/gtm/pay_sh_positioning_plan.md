# Pay.sh Positioning Plan

Prepared: 2026-05-07

Status: local prep only. Do not submit a Pay.sh provider application, open a `pay-skills` PR, deploy a gateway, or make external commitments without Johann's explicit approval.

## Why Pay.sh matters now

Pay.sh is a newly visible agent-payment surface for exactly the workflow Blocksize is trying to own: agents discover an API, see a price, pay per request, and receive the response without human account setup.

The important shift is that Pay.sh is not only a protocol story. It is also a directory and routing surface:

- Pay.sh positions itself as pay-as-you-go API access for agents and command-line workflows.
- The catalog is searchable by Claude, Codex, and CLI flows.
- The `pay-skills` registry is open source and accepts provider entries through PRs.
- Solana Foundation says Pay.sh launched in collaboration with Google Cloud on 2026-05-05, supports stablecoin payments on Solana, and is built on x402 plus MPP.
- The current catalog already includes crypto/onchain adjacent providers, including StableCrypto and CoinGecko Onchain DEX API, so Blocksize should enter with a differentiated financial-data message rather than generic crypto coverage.

## Strategic fit

Pay.sh should be treated as a Tier 1 distribution channel for the agentic payments initiative.

Why:

- It puts Blocksize in the place where agents are explicitly searching for paid APIs.
- It validates the thesis that paid data calls can be discovered, priced, authorized, and consumed inside an agent workflow.
- It creates a practical buyer path before larger enterprise marketplace surfaces mature.
- It reinforces the x402 narrative without depending on Coinbase-only distribution.
- It gives Blocksize a concrete "listed paid API" artifact for outreach to Solana, x402, Circle, agent IDEs, and agent infrastructure teams.

## Recommended position

Short position:

> Institutional market data that agents can buy one call at a time.

Catalog position:

> Blocksize gives agents live crypto, FX, and metals market data through accountless pay-per-call HTTP endpoints. Use it when an agent needs priced, structured financial data without creating an API key, subscribing to a plan, or asking a human to provision access.

Differentiation:

- Not a general crypto data wrapper.
- Not another dashboard API with a wallet bolted on.
- Not a payments company.
- Blocksize is the agent-readable institutional market-data seller: x402-native, multi-asset, structured, and built around auditability.

## Recommended listing shape

Provider FQN:

- `blocksize/market-data`

Title:

- `Blocksize Market Data`

Category:

- `finance`

Service URL:

- `https://mcp.blocksize.info`

OpenAPI URL:

- `https://mcp.blocksize.info/openapi.json`

Primary endpoints to highlight:

- `GET /v1/search`
- `GET /v1/instruments/{service}`
- `GET /v1/vwap/{pair}`
- `GET /v1/bidask/{pair}`
- `GET /v1/fx/{pair}`
- `GET /v1/metal/{ticker}`
- `GET /v1/batch`

Suggested usage examples:

- "Get BTC-USD VWAP before a trading-analysis step."
- "Fetch a bid/ask snapshot for a crypto pair before quoting a user."
- "Compare EURUSD and XAUUSD in an autonomous market brief."
- "Use free discovery first, then make the smallest useful paid call."

## Submission readiness

What already exists in the repo:

- Public HTTPS base URL.
- Public OpenAPI JSON.
- Free discovery endpoints for search and instrument lists.
- Paid HTTP endpoints returning HTTP 402 challenges.
- Solana as the primary payment rail and USDC as the paid-call currency.
- x402 discovery metadata at `/.well-known/x402`.
- Developer portal, quickstart, prompt examples, privacy, and support pages.

Light live check on 2026-05-07:

- `https://mcp.blocksize.info/openapi.json` returned HTTP 200.
- `https://mcp.blocksize.info/v1/fx/EURUSD` returned HTTP 402 with an x402Version 2 challenge, Solana mainnet USDC as the first accepted payment option, and Base as fallback.
- The live response still reported the challenge resource URL with an `http://` scheme through the reverse proxy. The local code now canonicalizes this field from `PUBLIC_BASE_URL`; deploy that patch before Pay.sh validation.
- This confirms the public surface is reachable, but it does not replace Pay.sh CLI validation.

Checks before submitting:

1. Confirm a production sample endpoint returns a Pay.sh-compatible 402 challenge over Solana mainnet.
2. Confirm a draft `PAY.md` using the filtered Pay.sh OpenAPI sidecar passes `pay catalog check`.
3. Confirm the 402 header names and challenge body are accepted by the current `pay` CLI.
4. Confirm discovery endpoints either remain free or are clearly omitted from paid endpoint probes.
5. Confirm the public OpenAPI only describes endpoints that should be available through Pay.sh.
6. Confirm spend-aware usage notes are precise enough for agents.

Important risk:

- The current Blocksize API is already x402-like, but Pay.sh validation may expect a specific challenge shape. If direct listing fails, route through a Pay.sh gateway spec rather than changing the product narrative.

Validation update on 2026-05-07:

- Installed `pay` CLI 0.16.0.
- Scaffolded `blocksize/market-data` in a local `solana-foundation/pay-skills` clone.
- Full public OpenAPI was not suitable for Pay.sh probing because it includes free docs, MCP, discovery, and credit-management routes.
- Created a filtered Pay.sh-facing OpenAPI sidecar with four paid example routes.
- `pay catalog check providers/blocksize/market-data/PAY.md --no-probe` now passes with 4 endpoints walked.
- Live probe currently fails because production still emits the Solana asset as a CAIP-style string and the reverse-proxy request URL as `http://`. Local code now emits the raw USDC mint/contract in `accepts[].asset` and canonicalizes `resource.url` from `PUBLIC_BASE_URL`, matching existing Pay.sh provider patterns. Redeploy before the final probe and PR.
- Local staging files are in `docs/gtm/pay_skills_submission/`.

## Route options

### Option A: Native API listing

List Blocksize directly in `pay-skills` with the existing public API and OpenAPI URL.

Best when:

- The current production 402 challenge passes `pay catalog check`.
- We want Blocksize to be discoverable as the direct paid API provider.
- We want minimal operational moving parts.

### Option B: Pay gateway proxy

Run a Pay.sh gateway in front of Blocksize's upstream API using an explicit provider spec.

Best when:

- Pay.sh requires a stricter challenge format than the current native API emits.
- We want gateway-managed payment receipts, filtered OpenAPI, or Pay.sh-native routing.
- We want to test in sandbox before mainnet publication.

### Option C: Official provider application plus registry PR

Submit the Pay.sh API provider application and open a `pay-skills` PR.

Best when:

- The direct integration is validated.
- We want Solana Foundation / Pay.sh ecosystem visibility.
- We have a demo command and a clear partner ask ready.

## Draft Pay.sh registry entry

This is a local draft for a future `providers/blocksize/market-data/PAY.md` entry:

```markdown
---
name: market-data
title: "Blocksize Market Data"
description: "Agent-native institutional crypto, FX, and metals market data with x402-paid per-call endpoints and free discovery."
use_case: "Use when an AI agent needs live market prices, VWAP, bid/ask snapshots, FX, metals, or compact data for financial workflows without creating an API account."
category: finance
service_url: https://mcp.blocksize.info
openapi:
  url: https://mcp.blocksize.info/openapi.json
---

Blocksize Market Data gives agents accountless access to live financial market data through free discovery endpoints and x402-paid HTTP calls.

Use the free search and instrument-list endpoints before paying for live data. Prefer a narrow lookup such as one VWAP pair, one bid/ask symbol, one FX pair, or one metals ticker before making batch calls.

## Spend-aware usage

- Search available instruments before making a paid market-data request.
- Prefer one-symbol calls for exploratory tasks.
- Use `/v1/batch` only when the user needs several prices in the same workflow.
- Reuse returned symbols exactly instead of guessing unsupported pair formats.
- Avoid polling unless the user has explicitly approved repeated paid calls.
```

## Partner-facing ask

Short ask:

> We have an x402-native market-data API that looks like a strong Pay.sh seller use case. Could your team review whether Blocksize should be listed as `blocksize/market-data` and tell us what would make it credible as a launch-quality financial-data provider?

Slightly warmer version:

> Pay.sh is almost exactly the discovery surface we have been building toward: an agent finds a paid API, sees a price, pays once, and gets structured data back. Blocksize can bring a focused institutional market-data use case to the catalog. We would love a quick integration review before submitting the provider entry.

## 7-day action plan

Day 1:

- Build the local draft provider entry.
- Run a production 402 compatibility smoke test against one low-cost endpoint.
- Decide native listing vs gateway proxy.

Day 2:

- Install or run the `pay` CLI in sandbox.
- Run `pay catalog scaffold` or adapt the draft `PAY.md`.
- Run `pay catalog check` locally.

Day 3:

- Fix OpenAPI or challenge-shape issues found by Pay.sh validation.
- Confirm spend-aware usage notes.

Day 4:

- Create a 90-second demo script: `pay skills search`, endpoint inspection, one paid data call.
- Add Pay.sh as a named GTM target in outreach materials.

Day 5:

- Prepare Pay.sh provider application answers.
- Prepare a `pay-skills` PR branch, but do not submit without approval.

Days 6-7:

- Send approved Pay.sh / Solana Foundation integration review outreach.
- Use the listing draft as proof in Coinbase/x402, Circle, and agent-infra conversations.

## Sources

- Pay.sh: https://pay.sh/
- Pay.sh docs overview: https://pay.sh/docs
- Pay.sh install docs and registry links: https://pay.sh/docs/get-started/install
- Pay.sh provider docs: https://pay.sh/docs/accept-payments
- Pay.sh provider spec docs: https://pay.sh/docs/accept-payments/provider-spec
- Pay.sh publish docs: https://pay.sh/docs/accept-payments/publish-to-pay-skills
- Pay Skills registry: https://github.com/solana-foundation/pay-skills
- Solana Foundation Pay.sh launch, published 2026-05-05: https://solana.com/uk/news/solana-foundation-launches-pay-sh-in-collaboration-with-google-cloud
