# Changelog

## 1.6.0

- Documents all 18 read-only connector tools: the original seven plus AMM state
  price, 30-minute and 24-hour VWAP, four agent workflow products, and four
  trader indicators.
- Raises the documented monthly free tier to 30,000 live-data credits.
- Tells agents to read each tool's cost from its description, and to keep
  private details out of the public price-receipt `purpose` note.

## 1.5.0

- Describes the monthly free tier (15,000 live-data credits, attribution
  required) and the subscription trial and pricing paths.
- Documents the `FREE_TIER_*` error codes and the upgrade CTA in tool results.

## 1.4.0

- Documents the public account-plan recommender and sales-assisted conversion boundary.
- Keeps discovery, authenticated live data, and paid x402 execution separate.

## 1.3.0

- Removes an unsupported manifest field so the plugin passes Cursor's official
  schema.
- Corrects provider-scoped OAuth discovery and removes unsafe shared-auth
  deletion and mutable smoke-test instructions.
- Hardens the shared skill against prompt injection, stale/future timestamps,
  credit-draining retries, credential exposure, and direct-identifier output.
- Adds deterministic, source-reconciled release packaging.

## 1.2.0

- Adds the portable `/use-blocksize-market-data` Agent Skill.
- Adds verified response and connector-surface references shared with the
  OpenAI and Claude packages.
- Makes discovery-only, route-only, and live market-data results explicit.

## 1.1.0

- Points the Cursor plugin at the Clerk-authenticated Cursor MCP endpoint.
- Adds live read-only market-data tools backed by signed-in daily credits.
- Keeps wallet payments and direct payment settlement outside the Cursor plugin flow.

## 1.0.0

- Initial Cursor plugin for Blocksize Market Data.
- Adds hosted remote MCP configuration for `https://mcp.blocksize.info/mcp/server/`.
