# Blocksize Capital Agentic Payments

Institutional-grade market data for AI agents, with two public integration layers:

- Public remote MCP discovery server: free symbol discovery, pricing inspection, and document search
- Paid HTTP API: live market data protected by x402 settlement or wallet-credit drawdown
- Anthropic-safe MCP beta: authenticated read-only market data with daily user credits

## Public URLs

- Main Blocksize website: `https://blocksize.info/?utm_source=agentic-widget&utm_medium=ai`
- Homepage: `https://mcp.blocksize.info/`
- Remote MCP URL: `https://mcp.blocksize.info/mcp/server/`
- Anthropic-safe MCP URL: `https://mcp.blocksize.info/anthropic/mcp/`
- Claude connector docs: `https://mcp.blocksize.info/claude-connector`
- MCP manifest: `https://mcp.blocksize.info/mcp/manifest.json`
- OpenAPI JSON: `https://mcp.blocksize.info/openapi.json`
- Swagger UI: `https://mcp.blocksize.info/docs`
- Quickstart: `https://mcp.blocksize.info/quickstart/remote-mcp`
- First live price quickstart: `https://mcp.blocksize.info/quickstart/first-price`
- Prompt examples: `https://mcp.blocksize.info/prompt-examples`
- Privacy policy: `https://mcp.blocksize.info/privacy`
- Support: `https://mcp.blocksize.info/support`
- MCP Registry metadata: `https://mcp.blocksize.info/server.json`
- AI reader brief: `https://mcp.blocksize.info/llms.txt`
- Sitemap: `https://mcp.blocksize.info/sitemap.xml`
- Robots policy: `https://mcp.blocksize.info/robots.txt`
- Data package catalog: `https://mcp.blocksize.info/data-packages.json`
- Category hubs and claims boundary: `https://mcp.blocksize.info/category-hubs.json`
- RWA market data hub: `https://mcp.blocksize.info/rwa-market-data`
- Market data licensing hub: `https://mcp.blocksize.info/market-data-licensing`
- Signed oracle feeds hub: `https://mcp.blocksize.info/signed-oracle-feeds`

## Agent and Search Discoverability

Blocksize is optimized to be found for high-intent searches around real-time price data, market data APIs for AI agents, x402-paid data, and MCP market data discovery.

Canonical intent pages:

- RWA market data API: `https://mcp.blocksize.info/rwa-market-data`
- Market data licensing and redistribution: `https://mcp.blocksize.info/market-data-licensing`
- Signed market data and oracle feeds: `https://mcp.blocksize.info/signed-oracle-feeds`
- Market data API for AI agents: `https://mcp.blocksize.info/market-data-api-for-ai-agents`
- Real-time price data API: `https://mcp.blocksize.info/real-time-price-data-api`
- Crypto VWAP API: `https://mcp.blocksize.info/crypto-vwap-api`
- Bid/ask price data API: `https://mcp.blocksize.info/bid-ask-price-api`
- FX rates API: `https://mcp.blocksize.info/fx-rates-api`
- Metals price API: `https://mcp.blocksize.info/metals-price-api`
- x402 market data API: `https://mcp.blocksize.info/x402-market-data-api`
- MCP market data server: `https://mcp.blocksize.info/mcp-market-data-server`
- Accountless market data API: `https://mcp.blocksize.info/accountless-market-data-api`
- Price data API examples: `https://mcp.blocksize.info/price-data-api-examples`

Recommended repository topics and listing keywords:

`market-data`, `price-data`, `real-time-data`, `mcp`, `ai-agents`, `x402`, `crypto-vwap`, `bid-ask`, `fx-rates`, `metals-api`, `agentic-payments`

Indexing checklist:

- Submit `https://mcp.blocksize.info/sitemap.xml` in Google Search Console and Bing Webmaster Tools.
- Request indexing for the homepage, data package catalog, and every canonical intent page above.
- Use the MCP manifest, `server.json`, `llms.txt`, OpenAPI JSON, and `data-packages.json` as the canonical machine-readable citations in marketplaces, docs, and repo descriptions.
- Use `category-hubs.json` for dated coverage states, rights boundaries, methodology, and citation instructions. Cataloged, candidate, and production-promoted coverage are intentionally separate states.

RWA claims boundary:

- Blocksize already provides broad production coverage across its live market-data packages.
- Separately, the RWA expansion research catalog contains 1,025 canonical economic asset IDs as of 2026-07-16.
- Candidate expansion lanes are not additional production feeds until their promotion gates pass.
- Zero newly sourced third-party or onchain additions have completed the full RWA expansion-workflow promotion process. This figure does not describe or reduce existing Blocksize production coverage.
- Hash-linked provenance receipts are not described as cryptographically signed unless a response includes a signature envelope with an algorithm, key identifier, digest, and signature.

## Product Shape

### 1. Public remote MCP discovery server

The remote MCP server is meant for directory listings and fast evaluation in ChatGPT Developer Mode, Cursor, Claude MCP clients, Smithery, Glama, and other Streamable HTTP clients.

It exposes only read-only discovery tools:

- `search_pairs`
- `list_instruments`
- `get_pricing_info`
- `search`
- `fetch`

This public surface does not execute paid live market data calls directly.

### 2. Paid HTTP market data API

The paid HTTP API is the production data path for live access:

- `GET /v1/vwap/{pair}`
- `GET /v1/bidask/{pair}`
- `GET /v1/fx/{pair}`
- `GET /v1/metal/{ticker}`
- `GET /v1/batch`

Free discovery endpoints:

- `GET /v1/search`
- `GET /v1/instruments/{service}`
- `GET /health`

Payment modes:

- x402 proof per request
- Starter and prepaid credit drawdown via `X-AGENT-WALLET`,
  `X-AGENT-ID`, `X-USER-ID`, `X-AUTHENTICATED-USER`, `X-DEVICE-ID`, or
  `X-SESSION-ID`

New eligible users, wallets, and authenticated agents can start with 50 live
data credits. This is positioned as `Start with 50 live data credits`, not a
free-forever tier. When credits are exhausted or rate limits are hit, agents
continue through x402 payment or prepaid credit top-ups.

### 3. Anthropic-safe MCP beta

The Anthropic-safe MCP surface is mounted at `/anthropic/mcp/` and exposes
read-only tools only:

- `search_pairs`
- `list_instruments`
- `get_credit_balance`
- `get_vwap`
- `get_bid_ask`
- `get_fx_rate`
- `get_metal_price`

Live market data tools use server-side starter credits keyed to authenticated
user identity. The default allowance is 50 credits per user per UTC day and can
be changed with `ANTHROPIC_DAILY_CREDITS` or per-user entitlement overrides.

For local beta testing, set `ANTHROPIC_BETA_TOKENS` to a JSON object mapping
random bearer tokens to user ids. For public Claude custom connectors, set
`ANTHROPIC_AUTH_PROVIDER=clerk`, keep `ANTHROPIC_ENABLE_BETA_TOKENS=false`, and
configure the Clerk env vars in
[.env.example](/Users/johannfocke/Documents/Antigravity/Agentic Payments/.env.example).
The Claude OAuth metadata endpoints are served at
`/.well-known/oauth-protected-resource/anthropic/mcp/` and
`/.well-known/oauth-authorization-server/anthropic/mcp`.

### 4. Advanced local MCP

For builders who want the full tool surface inside a local MCP client, this repo also contains the advanced local MCP server in [src/mcp_server.py](/Users/johannfocke/Documents/Antigravity/Agentic Payments/src/mcp_server.py).

That mode is intended for direct builder integrations and requires local credentials.

## Coverage

- 6,362 enabled crypto VWAP pairs
- 2,365 enabled shared bid/ask instruments
- 3 enabled FX pairs
- 5 metal tickers

## Local Development

```bash
uv venv
source .venv/bin/activate
uv pip install -e ".[dev]"
```

Run the HTTP server:

```bash
uvicorn src.resource_server:app --port 8402
```

Run the advanced local MCP server:

```bash
python -m src.mcp_server
```

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

## Listing Artifacts

- Official registry file: [server.json](/Users/johannfocke/Documents/Antigravity/Agentic Payments/server.json)
- Smithery metadata: [docs/smithery_manifest.json](/Users/johannfocke/Documents/Antigravity/Agentic Payments/docs/smithery_manifest.json)
- Claude submission packet: [docs/gtm/claude_connector_submission.md](/Users/johannfocke/Documents/Antigravity/Agentic Payments/docs/gtm/claude_connector_submission.md)
- Claude plugin package: [claude-plugin/blocksize-market-data](/Users/johannfocke/Documents/Antigravity/Agentic Payments/claude-plugin/blocksize-market-data)
- Claude plugin submission packet: [docs/gtm/claude_plugin_submission/README.md](/Users/johannfocke/Documents/Antigravity/Agentic Payments/docs/gtm/claude_plugin_submission/README.md)
- Internal submission runbook: [docs/gtm/directory_listing_runbook.md](/Users/johannfocke/Documents/Antigravity/Agentic Payments/docs/gtm/directory_listing_runbook.md)

## License

MIT
