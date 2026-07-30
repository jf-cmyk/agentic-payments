# Blocksize tool surfaces

Use this reference only when selecting a connector, explaining installation, or
handling a tool that is unavailable in the current host.

## Authenticated provider connectors

| Host | MCP URL |
| --- | --- |
| OpenAI / ChatGPT / Codex | `https://mcp.blocksize.info/openai/mcp/` |
| Claude | `https://mcp.blocksize.info/anthropic/mcp/` |
| Cursor | `https://mcp.blocksize.info/cursor/mcp/` |

Each authenticated connector exposes seven read-only tools:

- `search_pairs`
- `list_instruments`
- `get_credit_balance`
- `get_vwap`
- `get_bid_ask`
- `get_fx_rate`
- `get_metal_price`

Live calls use the signed-in account's current connector allowance. Do not
hard-code an allowance or tool cost; use the value returned by the server.
These connectors do not expose route builders, documentation search, payment,
wallet, trading, or account-mutation tools.

## Public discovery fallback

MCP URL: `https://mcp.blocksize.info/mcp/server/`

The public server exposes exactly eight read-only tools:

- `search_pairs`
- `list_instruments`
- `get_pricing_info`
- `get_product_catalog`
- `get_workflow_endpoint`
- `get_market_data_endpoint`
- `search`
- `fetch`

They cover catalog search, instrument lists, pricing and product metadata,
documentation search/fetch, and exact market-data or workflow route building.
The server returns integration metadata only. It does not authenticate a user,
spend credits, submit x402, or retrieve paid live data.

The standalone skill's OpenAI metadata names this dependency
`blocksize-market-data-public`. The OpenAI plugin separately names the
authenticated connector `blocksize-market-data`, preventing one URL from
overwriting the other. Claude and Cursor packages install only their
authenticated connector unless the user separately configures the public server.

## Operational proof boundary

- `GET /health` proves only that the HTTP application responded with metadata.
- An unauthenticated `401` plus `WWW-Authenticate` proves only that OAuth
  discovery is wired and fail-closed.
- Tool-list inspection proves only the advertised schema.
- Only a signed-in call returning a timestamp, provenance, units, and expected
  credit accounting proves live retrieval on that host.
- Never ask a user to paste OAuth tokens or delete a shared auth directory.

## Shared data boundaries

- Instrument discovery is not proof that a live feed is ready.
- A returned HTTP endpoint is not a returned market-data observation.
- Starter credits are an evaluation allowance, not a free-forever production tier.
- Production usage can use direct x402 outside the connector or an authenticated
  account plan arranged with Blocksize.
- Preserve distinctions among VWAP, spot/mid, bid/ask, state price, and
  fixed-window measurements.
