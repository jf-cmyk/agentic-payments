# Blocksize Agent-Native Premium Products Plan

> Superseded. Historical planning proposal; do not use it as current product or
> sales guidance. References below to prepaid top-ups, bulk tiers, wallet
> eligibility, and example package prices are not current production
> availability. Current access is signed x402 for direct public HTTP,
> starter credits only for eligible authenticated connector users, and a
> contact-sales authenticated account plan. Use `docs/README_EXTERNAL.md` for
> current public guidance.

Date: 2026-06-12

## Positioning

Raw data remains the foundation. Packaged, auditable, decision-ready intelligence becomes the product.

Primary promise: Start with 50 live data credits, then upgrade through x402 payment or prepaid credit top-ups. This is not a free-forever tier.

The starter allowance applies across raw data, market briefs, macro snapshots, pre-trade checks, audit receipts, and provenance lookups. It stops when the 50-credit balance is exhausted or rate limits are hit. Abuse controls should combine wallet, authenticated user, agent id, IP, device id, session id, user agent, wallet stake, wallet age, and duplicate-trial fingerprints where available.

Implementation status: MVP HTTP routes are live in the resource server for Agent Market Brief, Pre-Trade Sanity Check, Audit-Grade Price Receipt, Multi-Asset Macro Snapshot, Spend-Controlled Market Monitor evaluation, and receipt provenance lookup. Public MCP discovery includes `get_product_catalog` and `get_workflow_endpoint`; Claude/Cursor remain read-only raw-data connectors until premium tool wrappers are added.

## Credit Model

| Product | Credit cost | Paid price after credits |
| --- | ---: | --- |
| Raw VWAP or bid/ask | 1 | Existing endpoint price, currently $0.002-$0.008 |
| FX or metals | 1-2 | Existing endpoint price, currently $0.005 |
| Batch | Sum of included calls | Existing dynamic batch price |
| Agent Market Brief | 10 | Recommended $0.25-$0.50 |
| Pre-Trade Sanity Check | 5 | Recommended $0.10-$0.25 |
| Audit-Grade Price Receipt | 10 | Recommended $0.25-$0.75 |
| Multi-Asset Macro Snapshot | 25 | Recommended $1.00-$2.50 |
| Provenance lookup | 0 when tied to prior paid or credited call | Free tied lookup; charge for bulk exports later |

## Premium Products

### 1. Agent Market Brief

Target user: Trading agents, portfolio copilots, treasury operators, research assistants, and humans asking "what changed and what should I know before acting?"

Pain solved: A raw price answers only "what is BTC?" A brief answers "is this price fresh, normal, liquid enough, and relevant to my decision?"

Agent/human workflow: User asks for a brief on one or more instruments. Agent calls Blocksize once, receives a compact decision package, cites provenance, and decides whether to continue, watch, or request a deeper check.

Inputs:

```json
{
  "symbols": ["BTCUSD", "ETHUSD", "EURUSD"],
  "horizon": "intraday",
  "intent": "portfolio_update",
  "include": ["vwap", "bidask", "fx", "metals", "provenance"]
}
```

Output JSON shape:

```json
{
  "status": "ok",
  "product": "agent_market_brief",
  "credit_cost": 10,
  "as_of": "2026-06-12T18:30:00Z",
  "summary": {
    "headline": "BTC is trading near current VWAP with normal spread.",
    "market_state": "normal",
    "actionability": "usable_for_small_decision"
  },
  "instruments": [
    {
      "symbol": "BTCUSD",
      "asset_class": "crypto",
      "vwap": {},
      "bidask": {},
      "freshness_ms": 420,
      "spread_bps": 3.1,
      "quality_flags": []
    }
  ],
  "risks": [
    {
      "severity": "low",
      "code": "normal_spread",
      "message": "Spread is within configured threshold."
    }
  ],
  "provenance": {
    "receipt_id": "rcpt_...",
    "source_endpoints": ["/v1/vwap/BTCUSD", "/v1/bidask/BTCUSD"]
  },
  "credits": {
    "spent": 10,
    "remaining": 40,
    "upgrade_path": "x402 or prepaid credits"
  }
}
```

Better than raw lookup: It bundles multiple raw calls, quality checks, summary language, and provenance into one decision-ready package.

Reuses: `/v1/vwap/{pair}`, `/v1/bidask/{pair}`, `/v1/fx/{pair}`, `/v1/metal/{ticker}`, `/v1/batch`, search/instrument metadata, credit ledger, x402 middleware.

### 2. Pre-Trade Sanity Check

Target user: Agents or humans preparing a trade, swap, quote acceptance, treasury conversion, or portfolio rebalance.

Pain solved: Prevents agents from acting on stale, wide, unsupported, or obviously mismatched prices.

Agent/human workflow: Before execution elsewhere, the agent asks Blocksize to validate price context and returns a go/caution/block result. Blocksize does not execute the trade.

Inputs:

```json
{
  "symbol": "BTCUSD",
  "side": "buy",
  "notional_usd": 2500,
  "reference_price": 67250.12,
  "max_spread_bps": 25,
  "max_age_ms": 5000
}
```

Output JSON shape:

```json
{
  "status": "ok",
  "product": "pre_trade_sanity_check",
  "credit_cost": 5,
  "decision": "caution",
  "checks": {
    "instrument_supported": true,
    "quote_fresh": true,
    "spread_within_limit": true,
    "reference_price_drift_bps": 18.4,
    "reference_price_within_limit": false
  },
  "market": {
    "symbol": "BTCUSD",
    "bid": 67240.1,
    "ask": 67248.8,
    "mid": 67244.45,
    "vwap": 67243.2,
    "timestamp": "2026-06-12T18:30:00Z"
  },
  "recommendation": {
    "message": "Refresh the execution quote before proceeding.",
    "blocking": false
  },
  "provenance": {
    "receipt_id": "rcpt_..."
  }
}
```

Better than raw lookup: It turns market data into a guardrail result an agent can branch on.

Reuses: Bid/ask, VWAP, FX/metals as needed, existing symbol normalization, credit ledger, x402 middleware.

### 3. Audit-Grade Price Receipt

Target user: Agent platforms, finance ops, compliance reviewers, treasury teams, and workflow builders who need evidence for why a price was used.

Pain solved: Raw APIs are hard to defend later. Receipts create a replayable evidence trail for automated decisions.

Agent/human workflow: Agent asks for a receipt when a price enters a downstream action. Later, a human or agent can retrieve provenance by receipt id.

Inputs:

```json
{
  "service": "vwap",
  "symbol": "BTCUSD",
  "purpose": "treasury_rebalance_reference",
  "client_reference_id": "run_2026_06_12_001"
}
```

Output JSON shape:

```json
{
  "status": "ok",
  "product": "audit_grade_price_receipt",
  "credit_cost": 10,
  "receipt": {
    "receipt_id": "rcpt_...",
    "created_at": "2026-06-12T18:30:00Z",
    "client_reference_id": "run_2026_06_12_001",
    "request_hash": "sha256:...",
    "response_hash": "sha256:...",
    "provider": "Blocksize Capital"
  },
  "price": {
    "service": "vwap",
    "symbol": "BTCUSD",
    "value": 67243.2,
    "currency": "USD",
    "timestamp": "2026-06-12T18:30:00Z"
  },
  "provenance": {
    "source_endpoints": ["/v1/vwap/BTCUSD"],
    "terms_url": "https://blocksize.info/terms-conditions-data/"
  }
}
```

Better than raw lookup: It makes price data usable in workflows that need audit, reproducibility, or customer-visible evidence.

Reuses: Raw data endpoints, payment proof persistence, observability event store pattern, credit ledger.

### 4. Multi-Asset Macro Snapshot

Target user: Portfolio agents, wealth copilots, market monitors, research bots, and humans who need context across asset classes.

Pain solved: Agents waste calls stitching together crypto, FX, and metals context. A macro snapshot answers the cross-market question in one call.

Agent/human workflow: Agent requests a snapshot for a portfolio or watchlist. Blocksize returns market state, instrument readings, and risk flags.

Inputs:

```json
{
  "universe": ["BTCUSD", "ETHUSD", "EURUSD", "XAUUSD"],
  "theme": "risk_on_risk_off",
  "include_brief": true
}
```

Output JSON shape:

```json
{
  "status": "ok",
  "product": "multi_asset_macro_snapshot",
  "credit_cost": 25,
  "as_of": "2026-06-12T18:30:00Z",
  "market_regime": {
    "label": "mixed",
    "confidence": 0.72,
    "drivers": ["crypto_spreads_normal", "gold_available", "eurusd_available"]
  },
  "assets": [
    {
      "symbol": "BTCUSD",
      "asset_class": "crypto",
      "latest": {},
      "quality_flags": []
    }
  ],
  "brief": {
    "headline": "Crypto majors are orderly; FX and gold context available.",
    "watch_items": ["Refresh before execution if spread widens."]
  },
  "provenance": {
    "receipt_id": "rcpt_..."
  }
}
```

Better than raw lookup: It compresses several asset classes into a single context package that agents can use for routing, reporting, or monitoring.

Reuses: `/v1/batch`, all raw service families, instrument metadata, receipts.

### 5. Agent Data Provenance Layer

Target user: Agent platforms, enterprise workflow builders, internal tools teams, and compliance-oriented finance apps.

Pain solved: Agents need to show what data they used, when, and under which paid/credited call. Raw responses disappear into logs.

Agent/human workflow: Every paid or credited call can emit a receipt id. Later calls fetch receipt metadata for free if tied to a prior call.

Inputs:

```json
{
  "receipt_id": "rcpt_..."
}
```

Output JSON shape:

```json
{
  "status": "ok",
  "product": "agent_data_provenance",
  "credit_cost": 0,
  "receipt_id": "rcpt_...",
  "created_at": "2026-06-12T18:30:00Z",
  "request": {
    "endpoint": "/v1/vwap/BTCUSD",
    "method": "GET",
    "subject": "BTCUSD"
  },
  "payment": {
    "mode": "starter_credits",
    "credits_spent": 1
  },
  "hashes": {
    "request_hash": "sha256:...",
    "response_hash": "sha256:..."
  },
  "retention": {
    "lookup_free_with_prior_call": true
  }
}
```

Better than raw lookup: It turns Blocksize into the evidence layer for agent decisions, not just the price source.

Reuses: Credit/payment events, response metadata, raw endpoints, observability storage.

### 6. Spend-Controlled Market Monitor

Target user: Agent teams and humans who want market-aware workflows without uncontrolled polling spend.

Pain solved: Paid APIs make autonomous monitoring risky unless spend caps, cadence, and triggers are first-class.

Agent/human workflow: Agent creates a bounded monitoring request with max credits, cadence, symbols, and trigger rules. Blocksize returns the first snapshot and monitor plan. A 2-4 week MVP can implement stateless "evaluate now" first, then persisted monitors later.

Inputs:

```json
{
  "symbols": ["BTCUSD", "ETHUSD"],
  "rules": [{"metric": "spread_bps", "operator": ">", "value": 50}],
  "max_credits": 20,
  "cadence": "manual_or_15m"
}
```

Output JSON shape:

```json
{
  "status": "ok",
  "product": "spend_controlled_market_monitor",
  "credit_cost": 10,
  "mode": "evaluate_now",
  "matches": [],
  "spend_control": {
    "max_credits": 20,
    "credits_spent": 10,
    "remaining_budget": 10
  },
  "next_allowed_check": "2026-06-12T18:45:00Z"
}
```

Better than raw lookup: It gives autonomous agents a bounded spending primitive.

Reuses: Batch, credit ledger, rate limits, observability.

### 7. Market-Data Monetization Toolkit

Target user: Other API vendors, MCP publishers, and data owners who want to monetize agent access.

Pain solved: Vendors need examples for x402, starter credits, receipts, Pay.sh listings, OpenAPI, MCP discovery, and abuse controls.

Agent/human workflow: Vendor uses Blocksize as the reference implementation or paid setup package. The product is a hosted template plus integration review, not just an endpoint.

Inputs:

```json
{
  "provider_name": "Example Data Co",
  "api_base_url": "https://api.example.com",
  "priced_routes": ["/v1/data/{symbol}"],
  "starter_credits": 50,
  "payment_networks": ["solana", "base"]
}
```

Output JSON shape:

```json
{
  "status": "ok",
  "product": "market_data_monetization_toolkit",
  "deliverables": [
    "x402_payment_middleware",
    "starter_credit_policy",
    "mcp_discovery_manifest",
    "pay_sh_listing",
    "openapi_extensions",
    "receipt_schema"
  ],
  "estimated_build_days": 10
}
```

Better than raw lookup: It creates a higher-ticket B2B product from the infrastructure itself.

Reuses: Existing x402 middleware, MCP metadata, Pay.sh listing, docs, OpenAPI patterns, observability.

## Exact Product/API Adaptations

Add these endpoints without removing existing routes:

| Endpoint | Method | Credits | Notes |
| --- | --- | ---: | --- |
| `/v1/briefs/market` | POST | 10 | Uses batch/raw calls, returns brief and provenance |
| `/v1/checks/pre-trade` | POST | 5 | Read-only; never executes trades |
| `/v1/receipts/price` | POST | 10 | Stores receipt metadata and hashes |
| `/v1/snapshots/macro` | POST | 25 | Bounded universe with service caps |
| `/v1/provenance/{receipt_id}` | GET | 0 | Free only for tied prior paid or credited call |
| `/v1/monitors/evaluate` | POST | 10 | MVP monitor evaluation without persisted scheduler |
| `/v1/products` | GET | 0 | Machine-readable catalog for agents |

All paid responses should include:

```json
{
  "credits": {
    "spent": 10,
    "remaining": 40,
    "starter_allowance_credits": 50,
    "upgrade_path": "x402 or prepaid credits"
  },
  "provenance": {
    "receipt_id": "rcpt_...",
    "source_endpoints": []
  }
}
```

All 402 responses should include `starter_credits.positioning = "Start with 50 live data credits"` plus accepted identity headers and the x402 upgrade path.

## MCP Tool Additions

Public discovery MCP:

- `get_product_catalog` - added as free read-only discovery.
- `get_workflow_endpoint(product)` - added; builds paid HTTP route and example body without fetching data.
- `explain_starter_credits` - optional convenience tool if marketplace reviewers keep missing the allowance.

Authenticated Claude/Cursor MCP:

- Keep current raw live tools.
- Add premium read-only tools only after HTTP products exist:
  - `get_market_brief`
  - `run_pre_trade_sanity_check`
  - `create_price_receipt`
  - `get_macro_snapshot`
  - `lookup_price_receipt`
- Do not expose wallet, credit purchase, payment proof, or trade execution tools in Claude/Cursor.

## Claude/Cursor Connector Changes

- Rename user-facing copy from "daily credits" to "starter live-data credits" where possible.
- `get_credit_balance` should return `positioning`, `allowance_credits`, `credits_remaining`, and `upgrade_path`.
- Add examples:
  - "Start with a BTC market brief."
  - "Run a pre-trade sanity check for buying $2,500 of BTC."
  - "Create an audit-grade receipt for the BTC VWAP used in this workflow."
- Keep connectors read-only and payment-safe. For top-ups, link to HTTP x402/prepaid docs outside the connector.

## Website/Docs Positioning

Homepage/developer portal copy:

- Hero/supporting line: "Start with 50 live data credits. Upgrade with x402 or prepaid credits when your agent is ready for production."
- Product line: "Raw data is the foundation. Market briefs, risk checks, receipts, and provenance are the agent-native products."
- Avoid "free API" language.

Docs:

- Add a `Starter credits` section to quickstart, Claude connector, Cursor connector, Pay.sh listing, and API manual.
- Add a `Premium workflows` page with one copy-paste request and response per product.
- Add OpenAPI examples for the premium endpoints even before full SDK work.

API responses:

- Include credit metadata in successful JSON responses and response headers.
- Include starter-credit guidance in 402 bodies.

## Pay.sh/Catalog Changes

- Change listing title/description from raw market data only to "market data and agent workflow products."
- Add `starter allowance` section with exact credit costs.
- Add premium product list and use cases.
- Keep raw endpoints in the OpenAPI sidecar.
- Add the premium endpoint examples once implemented.

## First Demo Flow

1. Agent connects to the public MCP discovery server.
2. Agent calls `get_product_catalog` and sees "Start with 50 live data credits."
3. Agent sets `X-AGENT-ID` or signs in through Claude/Cursor.
4. Agent calls a raw VWAP or bid/ask request and spends 1 credit.
5. Agent calls Agent Market Brief and spends 10 credits.
6. Agent calls Multi-Asset Macro Snapshot and spends 25 credits.
7. Responses include provenance receipt ids and remaining credits.
8. Agent calls provenance lookup for a receipt at 0 credits.
9. Agent sees that 14 credits remain and receives the upgrade path: x402 payment or prepaid credits.
10. Demo ends by showing a 402 challenge after exhaustion or a top-up purchase challenge.

## Success Metrics

Activation:

- Public MCP `get_product_catalog` calls.
- Starter-credit grants by subject type.
- First credited live-data call rate.
- Time from discovery to first paid/credited response.

Conversion:

- Starter users who spend 10+ credits.
- Starter users who call at least one premium workflow.
- Starter users who exhaust 50 credits.
- Exhausted users who complete x402 payment or prepaid top-up.

Product value:

- Brief to raw lookup ratio.
- Pre-trade checks per active agent.
- Receipt creation and lookup rate.
- Macro snapshot repeat usage by account/agent.

Quality:

- 4xx/5xx rate by product.
- Refunds from upstream errors.
- Median and p95 latency by product.
- Abuse blocks by heuristic.

## 2-4 Week Build Plan

Week 1:

- Finish universal starter-credit visibility across HTTP, MCP metadata, docs, Pay.sh, and connector copy.
- Add `/v1/products`.
- Add receipt id generation and provenance schema for existing raw endpoints.
- Add tests for starter credit drawdown, 402 starter guidance, and credit-cost mapping.

Week 2:

- Build `POST /v1/briefs/market` and `POST /v1/checks/pre-trade`.
- Add OpenAPI examples and public MCP endpoint builder support.
- Add demo script using `X-AGENT-ID`.

Week 3:

- Build `POST /v1/receipts/price`, `GET /v1/provenance/{receipt_id}`, and `POST /v1/snapshots/macro`.
- Add observability events and dashboard slices by premium product.
- Add Claude/Cursor premium tool wrappers if HTTP endpoints are stable.

Week 4:

- Polish docs, Pay.sh listing, website copy, and demo.
- Run abuse/rate-limit QA and hosted smoke tests.
- Recruit 3-5 design partners around market briefs, pre-trade checks, and receipts.
