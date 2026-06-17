# Blocksize Capital Remote MCP and Paid API

Blocksize Capital provides:

- A public remote MCP discovery server for agent builders
- A paid HTTP market data API for live production data
- A 50-credit starter allowance for new eligible users, wallets, and
  authenticated agents

## Public discovery MCP

- Remote MCP URL: `https://mcp.blocksize.info/mcp/server/`
- Manifest: `https://mcp.blocksize.info/mcp/manifest.json`
- Quickstart: `https://mcp.blocksize.info/quickstart/remote-mcp`
- Prompt examples: `https://mcp.blocksize.info/prompt-examples`

The public MCP server is read-only and exposes:

- `search_pairs`
- `list_instruments`
- `get_pricing_info`
- `get_market_data_endpoint`
- `search`
- `fetch`

## Paid HTTP API

Live market data is available via the HTTP API and documented here:

- Swagger UI: `https://mcp.blocksize.info/docs`
- OpenAPI JSON: `https://mcp.blocksize.info/openapi.json`

Supported paid endpoints:

- `GET /v1/vwap/{pair}`
- `GET /v1/bidask/{pair}`
- `GET /v1/state/{pair}`
- `GET /v1/vwap30m/{pair}`
- `GET /v1/vwap24h/{pair}`
- `GET /v1/fx/{pair}`
- `GET /v1/metal/{ticker}`
- `GET /v1/batch`

Premium workflow endpoints:

- `POST /v1/capabilities/check` - free data-readiness check before spending
  credits on trader or indicator products
- `POST /v1/briefs/market` - Agent Market Brief, 10 credits
- `POST /v1/checks/pre-trade` - Pre-Trade Sanity Check, 5 credits
- `POST /v1/receipts/price` - Audit-Grade Price Receipt, 10 credits
- `POST /v1/snapshots/macro` - Multi-Asset Macro Snapshot, 25 credits
- `POST /v1/monitors/evaluate` - Spend-Controlled Market Monitor evaluation,
  10 credits
- `POST /v1/indicators/token-quality` - Token Market Quality Indicator,
  15 credits
- `POST /v1/indicators/state-divergence` - Oracle / State Price Divergence
  Indicator, 15 credits
- `POST /v1/signals/solana-token-brief` - Solana Token Brief, 25 credits
- `POST /v1/signals/trader-alpha-pack` - Trader Alpha Signal Pack, 50 credits
- `GET /v1/provenance/{receipt_id}` - free when tied to a prior paid or
  credited call

Optional trader feeds are opt-in. Current paid indicator defaults use live VWAP
and bid/ask only. Request `include_state_coverage`, `include_state_price`, or
`include_windows` explicitly when a workflow needs those fields; use
`/v1/capabilities/check` first to verify availability for the requested symbols.
State price reads from the aggregate `state_subscribe` stream cache when the
cache covers the symbol, and falls back to the documented `state_instruments`
plus `state_pool` HTTP feeds. It is available for protocol/pool symbols with
matching state coverage such as `MSOLUSD`, `JUPSOLUSD`, and `WSTETHUSD`, not
for every market symbol such as plain `SOLUSD`.

Feed mapping from the upstream Blocksize documentation:

| Capability | Upstream method | Current product use |
| --- | --- | --- |
| Real-time VWAP | `vwap_latest` | Raw VWAP, briefs, indicators |
| Bid/ask snapshot | `bidask_getSnapshot` | Raw bid/ask, spreads, checks |
| State pool price | `state_subscribe`, `state_instruments` + `state_pool` | Raw `/v1/state/{pair}`, optional state divergence |
| 30-minute close | `closingprice_list`, optional `closingprice_trades` | Raw `/v1/vwap30m/{pair}`, optional trade evidence |
| 24-hour fixed VWAP | `fixedvwap_subscribe` | Raw `/v1/vwap24h/{pair}` served from the websocket-backed fixed-VWAP cache |

Free discovery endpoints:

- `GET /v1/search`
- `GET /v1/instruments/{service}`
- `GET /health`

## Pricing

Start with 50 live data credits. This is a starter allowance, not a
free-forever tier. After credits are exhausted or rate limits are hit, agents
upgrade through x402 payment or prepaid credit top-ups.

| Service | Price |
| --- | --- |
| Core crypto | $0.002 |
| Extended crypto and shared bid/ask | $0.004 |
| FX and metals | $0.005 |
| Supported equity tickers via bid/ask | $0.008 |

Credit costs:

| Product | Credits |
| --- | ---: |
| Raw VWAP or bid/ask | 1 |
| Raw state, 30-minute close, or 24h fixed VWAP | 1 |
| FX or metals | 1-2 |
| Market brief | 10 |
| Pre-trade sanity check | 5 |
| Audit-grade price receipt | 10 |
| Multi-asset macro snapshot | 25 |
| Prior-call provenance lookup | 0 |

Bulk credit tiers are documented in the public pricing guide:

- `https://mcp.blocksize.info/pdf/Blocksize_Pricing_Guide.pdf`

## Policy and support

- Privacy policy: `https://mcp.blocksize.info/privacy`
- Support: `https://mcp.blocksize.info/support`
