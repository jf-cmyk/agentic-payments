# Agentic marketing production readiness

Date: 2026-07-22
Production service: `Blocksize-Real-Time-Market-Data-MCP`
Production URL: `https://mcp.blocksize.info`
Observed production version: `0.6.2`
Release commit: `9434e02147988c9f287c4a27a50576770bf0d0a3`
Railway deployment: `86f02bdc-c782-42e6-bc16-b66391420313` (`SUCCESS`, deployed 2026-07-22)

## Currently live and functioning

- Main Blocksize x402 resource server and health endpoint.
- Public remote MCP discovery server.
- Claude and Cursor authenticated read-only MCP connector metadata.
- Starter allowance positioning: 50 live-data credits.
- Existing crypto VWAP, shared bid/ask/equity, FX, and metals product metadata.
- Existing `llms.txt`, OpenAPI, manifest, server metadata, prompt examples, support, privacy, and Claude connector documentation.
- RWA, licensing, and signed-oracle category authority pages.
- First-price quickstart and machine-readable category claims boundary.
- Public RWA Coverage and Oracle Lineage evidence indexes in HTML and PDF.
- RWA coverage, sourcing, daily discovery, rights, and quality APIs.

Production live-price validation on 2026-07-22: `/v1/vwap/btc-usd` returned `200`, a timestamped Blocksize BTCUSD VWAP response, and a correct one-credit starter drawdown with 49/50 credits remaining for the dedicated smoke-test identity.

## Shipped in this production release

- RWA, licensing, and signed-oracle category authority pages.
- Machine-readable category claims boundary.
- First-price quickstart for Claude, Cursor, ChatGPT discovery, and HTTP.
- Once-per-identity `first_live_price_delivered` activation telemetry.
- Ranked privacy-safe unsupported-symbol opportunity telemetry.
- RWA Coverage Index in HTML, JSON, CSV, and PDF.
- Oracle Lineage and Rights Evidence Index in HTML, JSON, CSV, and PDF.
- Public `/evidence/` and `/pdf/` packaging for the two indexes.
- Deterministic tool-grounding benchmark harness.
- Jira reconciliation/import pack.
- Hosted post-deployment smoke script.

## Closed production gap

The previous production deployment returned `404` for:

- `/quickstart/first-price`
- `/rwa-market-data`
- `/market-data-licensing`
- `/signed-oracle-feeds`
- `/category-hubs.json`

All five routes now return `200` in production. The evidence HTML/PDF routes and their content assertions also pass.

## Deployment record

Deployment used a fresh `git archive` of commit `9434e02`; the working directory and its unrelated files were not uploaded.

Completed qualification:

1. Reviewed and staged the intended file allowlist on the production-readiness branch.
2. Resolved the RWA consensus regression by preserving the conservative basis-anchor guard.
3. Full test suite passed: 275 tests, with one third-party Authlib deprecation warning.
4. Ruff, Python compilation, local hosted smoke checks, and live read-only RWA source checks passed.
5. Staged secret scan returned no matches.

Completed release execution:

1. Clean-archive qualification passed: 275 tests, Ruff, Python compilation, and hosted smoke checks.
2. Branch `codex/agentic-marketing-production-readiness` pushed to GitHub. GitLab push was blocked by unavailable HTTPS credentials.
3. Exact archive deployed to Railway production as deployment `86f02bdc-c782-42e6-bc16-b66391420313`.
4. Production hosted smoke checks passed for all ten route checks and four content assertions.
5. Production RWA coverage returned 5,155 candidate rows / 1,959 unique assets; the daily discovery artifact returned 1,163 assets / 3,429 tokens.
6. A production non-persisting RWA probe returned fresh Hyperliquid AAPL/USDC native L1 bid/ask data with live quality.
7. The seven-source post-deploy matrix succeeded for six sources, including Ethereum and Base RPC pool state; the Raydium label guard safely rejected a Byreal-routed quote.
8. Railway logs showed no application errors or 5xx responses. The Blocksize upstream returned `200` at startup.

The post-deploy paid-route attempt returned the correct x402 `402` challenge because the production anti-abuse control rejected a duplicate starter allowance claim from the already-used IP. Before deployment, the paid BTC route had returned a fresh Blocksize response and drawn the dedicated identity from 50 to 49 credits. The complete tests cover successful credit drawdown and payment challenge behavior.

## Local hosted-style verification

The complete hosted smoke script passed against `http://127.0.0.1:8795` on 2026-07-22:

- 10/10 route status checks returned `200`.
- First-price, RWA claims-boundary, evidence-index, and `llms.txt` content assertions passed.
- The local server started and shut down cleanly.

Live RWA source verification succeeded for 6 of 7 representative probes. Working candidates covered Hyperliquid and Ostium/Gains public venue APIs, Jupiter-routed Solana liquidity, and direct Ethereum/Base EVM RPC pool state. The one Raydium-labeled probe failed safely because Jupiter reported a Byreal route; the route-label guard refused to misattribute the source. These are candidate observations, not production-promoted feeds. Tiingo is implemented as a separate benchmark/provider candidate and is not imported or called by the `/v1/rwa/*` runtime routes.

## External work still requiring connected accounts or business authority

- Authoritative Jira issue creation/update: requires the Jira project and Atlassian connector or API credentials.
- Marketplace submissions and search-console indexing: require the relevant signed-in accounts.
- Named-model empirical benchmark: requires model API access and timestamp-aligned live truth capture.
- Outreach and sales follow-up: require approved ICP list, sender identity, and commercial messaging.
- Customer contracts and redistribution grants: require legal/commercial owners.
