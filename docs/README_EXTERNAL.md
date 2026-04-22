# Blocksize Capital Remote MCP and Paid API

Blocksize Capital provides:

- A public remote MCP discovery server for agent builders
- A paid HTTP market data API for live production data

## Public discovery MCP

- Remote MCP URL: `https://agentic-payments-production.up.railway.app/mcp/server/`
- Manifest: `https://agentic-payments-production.up.railway.app/mcp/manifest.json`
- Quickstart: `https://agentic-payments-production.up.railway.app/quickstart/remote-mcp`
- Prompt examples: `https://agentic-payments-production.up.railway.app/prompt-examples`

The public MCP server is read-only and exposes:

- `search_pairs`
- `list_instruments`
- `get_pricing_info`
- `search`
- `fetch`

## Paid HTTP API

Live market data is available via the HTTP API and documented here:

- Swagger UI: `https://agentic-payments-production.up.railway.app/docs`
- OpenAPI JSON: `https://agentic-payments-production.up.railway.app/openapi.json`

Supported paid endpoints:

- `GET /v1/vwap/{pair}`
- `GET /v1/bidask/{pair}`
- `GET /v1/fx/{pair}`
- `GET /v1/metal/{ticker}`
- `GET /v1/batch`

Free discovery endpoints:

- `GET /v1/search`
- `GET /v1/instruments/{service}`
- `GET /health`

## Pricing

| Service | Price |
| --- | --- |
| Core crypto | $0.002 |
| Extended crypto and shared bid/ask | $0.004 |
| FX and metals | $0.005 |

Bulk credit tiers are documented in the public pricing guide:

- `https://agentic-payments-production.up.railway.app/pdf/Blocksize_Pricing_Guide.pdf`

## Policy and support

- Privacy policy: `https://agentic-payments-production.up.railway.app/privacy`
- Support: `https://agentic-payments-production.up.railway.app/support`
