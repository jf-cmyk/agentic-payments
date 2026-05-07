# Agentic Marketplaces Memo

Prepared: 2026-04-21

Updated: 2026-05-07 to add Pay.sh as a dedicated agent-paid API marketplace and registry surface.

## Why this matters

The target list should not only be "companies that might like Blocksize." It should be anchored in the places where agents already discover tools, APIs, connectors, and paid capabilities. If Blocksize wants agentic payments to become the main revenue driver, then distribution has to follow the agent's native path to discovery.

## Working definition

For Blocksize, an "agentic marketplace" is one of four things:

1. A place where users discover and invoke agent-native tools.
2. A protocol or connector directory where agents can discover external capabilities.
3. A developer ecosystem where agent builders select APIs and paid services.
4. A commerce or monetization layer that can turn agent demand into paid usage.

## Current highest-signal marketplaces and distribution surfaces

### 1. OpenAI GPT Store

Why it matters:

- The GPT Store is a true agent-discovery surface with very large end-user distribution.
- OpenAI states users have created more than 3 million GPTs, and public GPTs are discoverable in the GPT Store.
- Free-tier users can discover and use GPTs, while builders on paid plans can create and publish GPTs.

Implication for Blocksize:

- This is likely the largest current consumer-facing distribution surface for agent-like tool experiences.
- It is strong for discovery and workflow exposure, but weaker as a direct monetization rail because GPTs with Apps cannot be published publicly in the store.
- Blocksize should treat it as a top-of-funnel discovery channel, not the primary monetization channel.

### 2. Anthropic Connectors Directory / MCP Directory

Why it matters:

- Anthropic's Connectors Directory is a curated discovery hub for MCP servers that work with Claude across Claude web, desktop, mobile, Claude Code, and the API.
- This is one of the clearest marketplaces for agent connectors and external tool access.

Implication for Blocksize:

- This is one of the most relevant marketplaces for Blocksize's MCP-native distribution.
- If Blocksize wants to reach technical agent users quickly, MCP directory presence is strategically important.
- It is a better fit than the GPT Store for serious tool and data workflows.

### 3. x402 Ecosystem / Protocol Showcase Surfaces

Why it matters:

- x402 is one of the most relevant payment-discovery layers for agent-native APIs.
- Coinbase's x402 docs and product materials position HTTP 402 as a pay-per-use API and AI-agent payment primitive.

Implication for Blocksize:

- This is less of a consumer marketplace and more of a protocol-aligned ecosystem.
- It matters because Blocksize can become a canonical "seller" example: financial market data that agents can buy per call.
- This is strategically valuable even if volume is smaller today, because it aligns directly with revenue architecture.

### 4. Pay.sh API Catalog / pay-skills Registry

Why it matters:

- Pay.sh is a live catalog for pay-as-you-go APIs that agents and command-line workflows can discover and call.
- It is built around the exact flow Blocksize wants to own: discover an API, inspect price and endpoint metadata, pay per request, and receive a response without account setup.
- The `pay-skills` registry is open source and accepts provider listings through PRs once endpoints return valid Solana 402 challenges.
- Pay.sh launched with Solana Foundation and Google Cloud visibility, making it a high-signal ecosystem surface rather than a small standalone directory.

Implication for Blocksize:

- Pay.sh should move into the highest-priority distribution plan, alongside x402 ecosystem outreach and MCP directories.
- Blocksize should enter as `blocksize/market-data`, not as a generic crypto API, because the current catalog already includes crypto/onchain providers.
- The near-term goal is a validated listing path and a Pay.sh-native demo command.

### 5. Agent infrastructure ecosystems

Examples:

- Browserbase
- Firecrawl
- Exa
- Alchemy
- QuickNode
- Dune
- Flipside

Why they matter:

- These are not marketplaces in the strict app-store sense, but they are where agent builders choose which tools to wire into workflows.
- They are often closer to actual buying behavior than public directories.

Implication for Blocksize:

- For near-term revenue, these may be more important than large public stores.
- They are where an agent-paid data primitive can become a default tool inside real workflows.
- These should stay high on the target list.

### 6. Emerging premium content / publisher marketplaces

Why it matters:

- Microsoft publicly described a two-sided marketplace for premium content and publisher compensation for the "agentic web" on April 21, 2026.
- This is an important signal that large platforms are thinking in marketplace terms about agent access to premium data.

Implication for Blocksize:

- This validates the strategic direction: agents will need to pay for premium data and content.
- It is probably too early to be a direct GTM channel today, but it strongly supports Blocksize's long-term thesis.

## Best practical framing

There is not one dominant "agent marketplace" yet. There are three layers:

1. Discovery marketplaces:
   - GPT Store
   - Anthropic Connectors Directory

2. Workflow ecosystems:
   - Browserbase
   - Firecrawl
   - Exa
   - Dune
   - Alchemy
   - QuickNode

3. Monetization and protocol layers:
   - Pay.sh / pay-skills
   - x402
   - Circle programmable payments
   - emerging publisher marketplaces

For Blocksize, the real opportunity is to win in layer 2 and layer 3 first, then use layer 1 for visibility.

## Revised targeting priority

### Tier 1: Direct agent workflow distribution

- Browserbase
- Firecrawl
- Exa
- Dune
- Alchemy
- QuickNode

Why:

- These surfaces are closest to agent builders and actual tool composition.
- They can create repeat usage, not just visibility.

### Tier 2: Protocol and monetization leverage

- Pay.sh / pay-skills registry
- Coinbase Developer Platform / x402
- Circle
- Skyfire

Why:

- These companies shape how agents pay, authenticate, and route value.
- Winning here strengthens Blocksize's monetization credibility.
- Pay.sh is also a direct discovery surface, so it deserves both protocol-partner and marketplace treatment.

### Tier 3: Connector and directory discovery

- Anthropic Connectors Directory / MCP distribution
- GPT Store discovery strategy

Why:

- These are important for discovery, trust, and inbound awareness.
- They are not yet the clearest direct revenue path, but they matter a lot for distribution.

### Tier 4: Data API sellers and enterprise data platforms

- Kaiko
- CoinMetrics
- CoinGecko
- CoinAPI
- Polygon.io
- Intrinio
- Twelve Data
- Alpha Vantage
- Covalent
- Nansen
- Flipside
- The Graph

Why:

- These are still good design-partner targets.
- But they should be approached as packaging and monetization research targets, not assumed marketplaces.

## Recommended GTM adjustment

Shift the narrative from:

> "Let's reach out to data companies."

to:

> "Let's target the ecosystems where agents already discover, compose, and pay for capabilities."

That means the best immediate target cluster is:

1. Browserbase
2. Firecrawl
3. Exa
4. Dune
5. Coinbase x402
6. Pay.sh / pay-skills
7. Circle
8. Anthropic MCP directory path
9. Alchemy
10. QuickNode
11. Skyfire

## Immediate actions

1. Re-rank the first outreach batch around workflow ecosystems and protocol leverage, not generic data vendors.
2. Create one marketplace/distribution-specific email variant for:
   - MCP/connectors
   - workflow/infrastructure ecosystems
   - protocol/payment partners
3. Prepare a Blocksize MCP directory listing strategy.
4. Prepare a GPT Store discovery strategy, but do not treat it as the core monetization plan.
5. Add a "where agents discover Blocksize" section to the GTM operating loop.
6. Prepare and validate a Pay.sh registry listing draft for `blocksize/market-data`.

## Sources

- OpenAI GPT Store launch: https://openai.com/blog/introducing-the-gpt-store
- OpenAI GPTs help article: https://help.openai.com/en/articles/8798620
- OpenAI GPTs FAQ: https://help.openai.com/en/articles/8554407-gpts-faq
- Anthropic Connectors Directory FAQ: https://support.claude.com/en/articles/11596036-anthropic-mcp-directory-faq
- Coinbase x402 HTTP 402 docs: https://docs.cdp.coinbase.com/x402/docs/http-402
- Coinbase x402 core concepts: https://docs.cdp.coinbase.com/x402/core-concepts/how-it-works
- Coinbase x402 product page: https://www.coinbase.com/developer-platform/products/x402/
- Microsoft premium content marketplace reporting, published April 21, 2026: https://www.axios.com/2026/04/21/microsoft-ai-marketplace-publishers
- Dune APIs and connectors: https://dune.com/apis-and-connectors/
- Browserbase docs: https://docs.browserbase.com/
- Firecrawl site: https://www.firecrawl.dev/
- Exa API: https://exa.ai/exa-api
- Flipside docs: https://docs.flipsidecrypto.com/
- Pay.sh: https://pay.sh/
- Pay.sh docs overview: https://pay.sh/docs
- Pay Skills registry: https://github.com/solana-foundation/pay-skills
- Solana Foundation Pay.sh launch, published 2026-05-05: https://solana.com/uk/news/solana-foundation-launches-pay-sh-in-collaboration-with-google-cloud
