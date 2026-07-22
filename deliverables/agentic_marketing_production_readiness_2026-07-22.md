# Agentic marketing production readiness

Date: 2026-07-22
Production service: `Blocksize-Real-Time-Market-Data-MCP`
Production URL: `https://mcp.blocksize.info`
Observed production version: `0.6.2`
Observed Railway deployment: `0fe773b2-32bf-4dfd-ae19-192af968a7b3` (`SUCCESS`, deployed 2026-06-25)

## Currently live and functioning

- Main Blocksize x402 resource server and health endpoint.
- Public remote MCP discovery server.
- Claude and Cursor authenticated read-only MCP connector metadata.
- Starter allowance positioning: 50 live-data credits.
- Existing crypto VWAP, shared bid/ask/equity, FX, and metals product metadata.
- Existing `llms.txt`, OpenAPI, manifest, server metadata, prompt examples, support, privacy, and Claude connector documentation.

Production live-price validation on 2026-07-22: `/v1/vwap/btc-usd` returned `200`, a timestamped Blocksize BTCUSD VWAP response, and a correct one-credit starter drawdown with 49/50 credits remaining for the dedicated smoke-test identity.

## Release-qualified and pending production deployment

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

## Verified production gap

The current production deployment returns `404` for:

- `/quickstart/first-price`
- `/rwa-market-data`
- `/market-data-licensing`
- `/signed-oracle-feeds`
- `/category-hubs.json`

These features are therefore **release-qualified but not present in the observed production deployment**. The exact reviewed commit will be deployed after the archive verification below passes.

## Deployment gate

Do not run `railway up` from the working directory. The workspace contains unrelated modified and untracked files beyond the release package. Deployment must use a clean archive of the reviewed commit.

Completed qualification:

1. Reviewed and staged the intended file allowlist on the production-readiness branch.
2. Resolved the RWA consensus regression by preserving the conservative basis-anchor guard.
3. Full test suite passed: 275 tests, with one third-party Authlib deprecation warning.
4. Ruff, Python compilation, local hosted smoke checks, and live read-only RWA source checks passed.
5. Staged secret scan returned no matches.

Remaining release execution:

1. Commit the reviewed package and rerun qualification from a clean archive of that commit.
2. Push the reviewed branch.
3. Deploy that exact archive to the Railway production service.
4. Run `scripts/run_agentic_marketing_smoke_checks.sh https://mcp.blocksize.info`.
5. Verify the production RWA coverage and read-only sourcing routes, paid Blocksize route, and Railway logs.

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
