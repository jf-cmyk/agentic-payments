# Blocksize Capital Agentic Payments

Institutional-grade market data for AI agents, with two public integration layers:

- Public remote MCP discovery server: free symbol discovery, pricing inspection, and document search
- Paid HTTP API: live market data protected by x402 settlement or wallet-credit drawdown
- Anthropic-safe MCP beta: authenticated read-only market data with daily user credits

## Public URLs

- Homepage: `https://mcp.blocksize.info/`
- Remote MCP URL: `https://mcp.blocksize.info/mcp/server/`
- Anthropic-safe MCP URL: `https://mcp.blocksize.info/anthropic/mcp/`
- MCP manifest: `https://mcp.blocksize.info/mcp/manifest.json`
- OpenAPI JSON: `https://mcp.blocksize.info/openapi.json`
- Swagger UI: `https://mcp.blocksize.info/docs`
- Quickstart: `https://mcp.blocksize.info/quickstart/remote-mcp`
- Prompt examples: `https://mcp.blocksize.info/prompt-examples`
- Privacy policy: `https://mcp.blocksize.info/privacy`
- Support: `https://mcp.blocksize.info/support`
- MCP Registry metadata: `https://mcp.blocksize.info/server.json`

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
- Wallet-credit drawdown via `X-AGENT-WALLET`

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

Live market data tools use server-side daily credits keyed to authenticated user
identity. The default allowance is 50 credits per user per UTC day and can be
changed with `ANTHROPIC_DAILY_CREDITS` or per-user entitlement overrides.

For local beta testing, set `ANTHROPIC_BETA_TOKENS` to a JSON object mapping
random bearer tokens to user ids. For production, set `ANTHROPIC_AUTH_PROVIDER`
to `clerk`, `auth0`, or `supabase` and configure the provider env vars in
[.env.example](/Users/johannfocke/Documents/Antigravity/Agentic Payments/.env.example).

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
- Internal submission runbook: [docs/gtm/directory_listing_runbook.md](/Users/johannfocke/Documents/Antigravity/Agentic Payments/docs/gtm/directory_listing_runbook.md)

## License

MIT
