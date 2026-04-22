# Blocksize Capital Agentic Payments

Institutional-grade market data for AI agents, with two public integration layers:

- Public remote MCP discovery server: free symbol discovery, pricing inspection, and document search
- Paid HTTP API: live market data protected by x402 settlement or wallet-credit drawdown

## Public URLs

- Homepage: `https://agentic-payments-production.up.railway.app/`
- Remote MCP URL: `https://agentic-payments-production.up.railway.app/mcp/server`
- MCP manifest: `https://agentic-payments-production.up.railway.app/mcp/manifest.json`
- OpenAPI JSON: `https://agentic-payments-production.up.railway.app/openapi.json`
- Swagger UI: `https://agentic-payments-production.up.railway.app/docs`
- Quickstart: `https://agentic-payments-production.up.railway.app/quickstart/remote-mcp`
- Prompt examples: `https://agentic-payments-production.up.railway.app/prompt-examples`
- Privacy policy: `https://agentic-payments-production.up.railway.app/privacy`
- Support: `https://agentic-payments-production.up.railway.app/support`
- MCP Registry metadata: `https://agentic-payments-production.up.railway.app/server.json`

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
- `GET /v1/state/{pair}`
- `GET /v1/equity/{ticker}`
- `GET /v1/fx/{pair}`
- `GET /v1/metal/{ticker}`
- `GET /v1/batch`

Free discovery endpoints:

- `GET /v1/search`
- `GET /v1/instruments/{service}`
- `GET /health`

Payment modes:

- x402 proof per request
- Wallet-credit drawdown via `X-AGENT-WALLET`

### 3. Advanced local MCP

For builders who want the full tool surface inside a local MCP client, this repo also contains the advanced local MCP server in [src/mcp_server.py](/Users/johannfocke/Documents/Antigravity/Agentic Payments/src/mcp_server.py).

That mode is intended for direct builder integrations and requires local credentials.

## Coverage

- 9,410 crypto VWAP pairs
- 1,962 crypto bid/ask pairs
- 18,071 equity tickers
- 129 FX pairs
- 5 metal tickers
- 251 30-minute VWAP analytics tickers
- 42 24-hour VWAP pairs

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
- Internal submission runbook: [docs/gtm/directory_listing_runbook.md](/Users/johannfocke/Documents/Antigravity/Agentic Payments/docs/gtm/directory_listing_runbook.md)

## License

MIT
