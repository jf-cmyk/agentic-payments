# Blocksize Capital Remote MCP and Paid API

Blocksize Capital provides:

- A public remote MCP discovery server for agent builders
- A paid HTTP market data API for live production data

## Public discovery MCP

- Remote MCP URL: `https://mcp.blocksize.info/mcp/server/`
- Manifest: `https://mcp.blocksize.info/mcp/manifest.json`
- Quickstart: `https://mcp.blocksize.info/quickstart/remote-mcp`
- Prompt examples: `https://mcp.blocksize.info/prompt-examples`

The public MCP server is read-only and exposes:

- `search_pairs`
- `list_instruments`
- `get_pricing_info`
- `search`
- `fetch`

## Paid HTTP API

Live market data is available via the HTTP API and documented here:

- Swagger UI: `https://mcp.blocksize.info/docs`
- OpenAPI JSON: `https://mcp.blocksize.info/openapi.json`

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

- `https://mcp.blocksize.info/pdf/Blocksize_Pricing_Guide.pdf`

## Policy and support

- Privacy policy: `https://mcp.blocksize.info/privacy`
- Support: `https://mcp.blocksize.info/support`
