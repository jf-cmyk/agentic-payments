# Blocksize Capital Remote MCP and Paid API

Blocksize Capital provides:

- A public remote MCP discovery server for agent builders
- A paid HTTP market data API for live production data
- A 50-credit starter allowance for eligible authenticated connector users

## Public discovery MCP

- Remote MCP URL: `https://mcp.blocksize.info/mcp/server/`
- Manifest: `https://mcp.blocksize.info/mcp/manifest.json`
- Quickstart: `https://mcp.blocksize.info/quickstart/remote-mcp`
- Prompt examples: `https://mcp.blocksize.info/prompt-examples`

The public MCP server is read-only and exposes:

- `search_pairs`
- `list_instruments`
- `get_pricing_info`
- `get_product_catalog`
- `get_workflow_endpoint`
- `get_market_data_endpoint`
- `search`
- `fetch`

## Paid HTTP API

Live market data is available via the HTTP API and documented here:

- Swagger UI: `https://mcp.blocksize.info/docs`
- OpenAPI JSON: `https://mcp.blocksize.info/openapi.json`

Supported paid endpoints:

- `GET /v1/vwap/{pair}`
- `GET /v1/bidask/{pair}` for crypto pairs and supported equity tickers
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

- `GET /instruments` - human-searchable catalog with canonical symbols, readiness,
  per-call price, and copyable purchase requests
- `GET /v1/search`, including `GET /v1/search?q=AAPL&asset_class=equity`
- `GET /v1/instruments/{service}`; use `bidask` for the shared bid/ask namespace
- `GET /v1/samples/pre-trade` - illustrative pre-trade product output with no
  payment and no live-data claim
- `GET /v1/samples/macro-snapshot` - illustrative multi-asset macro package
  output with an attributed purchase handoff and no live-data claim
- `GET /health`

## Official Python x402 buyer

Base and Solana buyer examples are maintained in `examples/x402/`. The Base
Python and TypeScript clients filter to Base mainnet USDC and enforce an explicit
spend cap. The Solana canary reads its key file once and validates the network,
mint, recipient, amount, returned market-data payload, and idempotent replay.
Discover the canonical URL first at `/instruments`, then run one explicitly
bounded Solana call:

```bash
python scripts/run_funded_x402_canary.py \
  "/Volumes/YOUR_USB/Test.json" \
  --url "https://mcp.blocksize.info/v1/bidask/AAPLXUSD" \
  --max-usdc "0.008"
```

On macOS, local key files are rejected unless the caller explicitly supplies
`--allow-local-key-file`. The command never prints or copies private-key material.

## Pricing

Eligible authenticated connector users start with 50 live data credits. This
is a starter allowance, not a free-forever tier. Raw caller-selected identity
headers do not grant production credits. After credits are exhausted or rate
limits are hit, agents use signed x402 v2 payment or contact Blocksize about an
authenticated account plan.

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

The public pricing guide documents direct x402 rates, authenticated connector
starter credits, and how to discuss an authenticated account plan:

- `https://mcp.blocksize.info/pdf/Blocksize_Pricing_Guide.pdf`

## Policy and support

- Privacy policy: `https://mcp.blocksize.info/privacy`
- Support: `https://mcp.blocksize.info/support`
