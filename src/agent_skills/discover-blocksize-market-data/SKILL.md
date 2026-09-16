---
name: discover-blocksize-market-data
description: Discover Blocksize instruments, check data readiness, and choose MCP or paid HTTP access.
---

# Discover Blocksize market data

Use the public remote MCP server at {{PUBLIC_BASE_URL}}/mcp/server/ for free
instrument discovery, pricing, endpoint selection, and documentation. Retrieve
its current tool list before choosing a tool; this server does not fetch paid live data.

1. Resolve the user's instrument and requested product. Prefer `resolve_market_data`
   for natural-language requests. Use `search_pairs` to resolve ambiguity and
   `list_instruments` with an explicit service for catalog browsing. Confirm the
   instrument identity and quote currency rather than guessing ticker strings.
2. Consult {{PUBLIC_BASE_URL}}/openapi.json for the selected route and request schema.
   Check {{PUBLIC_BASE_URL}}/v1/cache/status before choosing cached products;
   use the documented capabilities check when a product needs a readiness decision.
   Catalog presence alone does not establish live readiness.
3. Use {{PUBLIC_BASE_URL}}/auth.md to choose authenticated connector access or direct
   x402 HTTP. Check connector credit balance before using live tools. For direct
   HTTP, inspect the current unpaid challenge and obtain payment authorization
   within the user's budget before signing. Never send private keys or seed phrases.
4. Preserve observation timestamps, units, source, methodology, and stale/error flags
   in the answer. Do not present catalog records as live observations, or describe
   unsigned provenance receipts as cryptographic attestations. Report unavailable
   data rather than substituting a different product or inventing a quote.

Read {{PUBLIC_BASE_URL}}/quickstart/first-price for a first observation and
{{PUBLIC_BASE_URL}}/category-hubs.json for coverage and usage boundaries.
