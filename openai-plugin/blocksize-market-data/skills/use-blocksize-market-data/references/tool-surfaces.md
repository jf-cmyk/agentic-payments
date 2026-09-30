# Blocksize tool surfaces

Use this reference only when selecting a connector, explaining installation, or
handling a tool that is unavailable in the current host.

## Authenticated provider connectors

| Host | MCP URL |
| --- | --- |
| OpenAI / ChatGPT / Codex | `https://mcp.blocksize.info/openai/mcp/` |
| Claude | `https://mcp.blocksize.info/anthropic/mcp/` |
| Cursor | `https://mcp.blocksize.info/cursor/mcp/` |

Each authenticated connector exposes 18 read-only tools, in four groups.

Discovery and credit status:

- `search_pairs`
- `list_instruments`
- `get_credit_balance`

Live snapshots:

- `get_vwap`
- `get_bid_ask`
- `get_fx_rate`
- `get_metal_price`
- `get_state_price`
- `get_vwap_30m`
- `get_vwap_24h`

Agent workflow products:

- `get_market_brief`
- `run_pre_trade_check`
- `create_price_receipt`
- `get_macro_snapshot`

Trader indicators:

- `get_token_quality`
- `get_state_divergence`
- `get_solana_token_brief`
- `get_trader_alpha_pack`

Live calls use the signed-in account's current connector allowance. Workflow
products and trader indicators cost far more per call than a live snapshot.
Do not hard-code an allowance or tool cost; use the cost in the tool
description and the balance returned by the server.
These connectors do not expose route builders, documentation search, payment,
wallet, trading, or account-mutation tools. `run_pre_trade_check` only
evaluates a planned trade; it never places one. `create_price_receipt` stores
the request, including any `purpose` note, behind a public lookup URL, so keep
account identifiers and private details out of that note.

## Public discovery fallback

MCP URL: `https://mcp.blocksize.info/mcp/server/`

The public server exposes exactly nine read-only tools:

- `search_pairs`
- `list_instruments`
- `get_pricing_info`
- `get_product_catalog`
- `recommend_account_plan`
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
- The free tier is a recurring monthly evaluation allowance (30,000 live-data
  credits) with required "Data by Blocksize" attribution; production commercial
  use needs a subscription (free trial at /go/free-trial, plans at /go/pricing).
- Production usage can use direct x402 outside the connector or an authenticated
  account plan arranged with Blocksize.
- Preserve distinctions among VWAP, spot/mid, bid/ask, state price, and
  fixed-window measurements.
