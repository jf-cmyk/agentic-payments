---
name: market-data
title: "Blocksize Market Data"
description: "Agent-native institutional market data and workflow products through direct x402-paid routes, with starter credits available separately in authenticated connectors."
use_case: "Use when an AI agent needs live prices, VWAP, bid/ask snapshots, FX, metals, market briefs, pre-trade checks, audit receipts, macro snapshots, or trader signal indicators without creating a traditional API account."
category: finance
service_url: https://mcp.blocksize.info
openapi:
  path: openapi.json
---

Blocksize Market Data gives agents accountless access to live financial market
data through direct x402-paid HTTP calls. Eligible authenticated connector users
can separately start with 50 live data credits. The Pay.sh HTTP routes use
direct x402; they do not expose self-serve purchase routes.

Use it for market-aware agent workflows that need crypto VWAP, crypto bid/ask,
state price, 30-minute close, 24h fixed VWAP, FX, metals, market briefs,
pre-trade sanity checks, audit-grade receipts,
macro snapshots, token quality indicators, state divergence checks, Solana token
briefs, or trader signal packs. Prefer a narrow lookup or packaged workflow with explicit user
approval for repeated paid calls.

## Starter allowance

- Position as: `Start with 50 live data credits`.
- This is not a free-forever tier.
- Raw VWAP, bid/ask, state, 30-minute close, and 24h fixed VWAP calls cost 1 credit.
- FX and metals calls cost 1-2 credits.
- Market briefs cost 10 credits.
- Pre-trade sanity checks cost 5 credits.
- Audit-grade price receipts cost 10 credits.
- Multi-asset macro snapshots cost 25 credits.
- Token market quality indicators cost 15 credits.
- State divergence indicators cost 15 credits.
- Solana token briefs cost 25 credits.
- Trader alpha signal packs cost 50 credits.
- Provenance lookups are free when tied to a prior paid or credited call.
- Data-readiness checks through `/v1/capabilities/check` are free.
- When authenticated connector starter credits are exhausted, use direct x402
  or contact Blocksize about an authenticated account plan.

## Premium workflow products

- Agent Market Brief: decision-ready market package with live prices, freshness,
  spread checks, provenance, and next-action context.
- Pre-Trade Sanity Check: guardrail response before a trade, swap, conversion,
  treasury action, or quote acceptance.
- Audit-Grade Price Receipt: reproducible evidence package for a price used in
  an automated workflow.
- Multi-Asset Macro Snapshot: one-call context bundle across crypto, FX, metals,
  and risk signals.
- Agent Data Provenance Layer: receipt lookup and source metadata for previous
  paid or credited calls.
- Spend-Controlled Market Monitor: immediate bounded monitor evaluation with
  explicit credit budget metadata.
- Token Market Quality Indicator: transparent trader score from live VWAP,
  bid/ask spread, optional state_pool price, freshness, and VWAP-window drift.
- Oracle / State Price Divergence Indicator: signed market-vs-state basis for
  detecting stale, divergent, or dislocated prices on symbols with
  state_instruments pool coverage.
- Solana Token Brief: Solana-oriented watchlist ranking for supported token
  symbols, with explicit coverage misses for unsupported protocol/pool feeds.
- Trader Alpha Signal Pack: bounded watchlist bundle of token quality rankings,
  state divergence, spread quality, and provenance. This is decision support,
  not investment advice or guaranteed returns.

## Optional feed policy

- Optional trader feeds are opt-in.
- Do not request state coverage, state price, 30-minute close, or 24-hour VWAP
  unless the user or agent workflow explicitly needs them.
- Use `/v1/capabilities/check` before paid indicator calls to confirm required
  and optional feed coverage for the requested symbols.
- Current confirmed default trader data is live VWAP and bid/ask. State
  instrument/pool coverage is available when requested. State price reads from
  the `state_subscribe` stream cache when covered and falls back to documented
  `state_instruments` + `state_pool` HTTP calls for matching protocol/pool
  symbols such as `MSOLUSD`, `JUPSOLUSD`, and `WSTETHUSD`.
  30-minute close is available through `/v1/vwap30m/{pair}` backed by
  `closingprice_list`; pass `include_trades=true` for `closingprice_trades`
  evidence. `/v1/vwap24h/{pair}` is backed by the `fixedvwap_subscribe`
  websocket cache and no longer presents latest VWAP as 24-hour data.

## Spend-aware usage

- Prefer one-symbol calls for exploratory tasks.
- Use the listed route examples exactly as shown in the OpenAPI sidecar.
- Avoid polling unless the user has explicitly approved repeated paid calls.
