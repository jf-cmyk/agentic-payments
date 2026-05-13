---
description: Use when the user wants Blocksize market data in Claude Code or Cowork, including instrument lookup, daily credit checks, crypto VWAP, bid/ask snapshots, FX rates, or metal prices.
---

# Blocksize Market Data Workflow

Use the Blocksize Market Data MCP connector for read-only market-data lookups.
The connector is designed for bounded data snapshots and discovery, not
transaction execution.

## Safe Workflow

1. Start with `search_pairs` when the user gives a vague symbol, asset name, or
   market-data request.
2. Use `list_instruments` when the user asks what a Blocksize service namespace
   supports.
3. Use `get_credit_balance` before live-data calls if the user asks about
   remaining allowance or if several paid snapshot calls are likely.
4. Use the narrowest live-data tool that matches the requested asset class:
   `get_vwap` for crypto VWAP, `get_bid_ask` for crypto or supported equities,
   `get_fx_rate` for FX, and `get_metal_price` for metals.
5. Keep results concise. Summarize the symbol, timestamp/source fields when
   returned, bid/ask or VWAP values, and remaining credits when available.

## Safety Boundary

Do not use this plugin to trade, move funds, submit payment proofs, request
wallet signatures, or mutate an account. If a user asks for execution, explain
that the Claude connector is read-only and can only provide market-data
snapshots.

## Useful Prompts

- "Search Blocksize for BTC market data instruments."
- "Show my remaining Blocksize data credits."
- "Get the latest BTC-USD VWAP from Blocksize."
- "Get the current EURUSD FX snapshot."
- "Get the latest XAUUSD metal price."
