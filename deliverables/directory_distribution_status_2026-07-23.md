# Agentic data distribution status

Verified: 2026-07-23

All six existing public discovery URLs returned HTTP 200. This establishes reachability, not marketplace analytics completeness.

| Surface | Public URL | Verified status | Remaining work |
| --- | --- | --- | --- |
| Official MCP Registry | https://registry.modelcontextprotocol.io/v0/servers?search=blocksize | Live URL | Ingest registry-side usage metrics if an export/API is available. |
| Glama | https://glama.ai/mcp/connectors/info.blocksize.mcp/agentic-payments | Live URL | Ingest marketplace metrics; account required for future edits. |
| Smithery | https://smithery.ai/servers/blocksize/agentic-payments?capability=tools | Live URL | Ingest marketplace metrics; account required for future edits. |
| Pay.sh | https://pay.sh/services/blocksize/market-data | Live URL | Validate downstream paid-call analytics and ingest marketplace metrics. |
| x402scan | https://www.x402scan.com/server/3d0ad7cd-9e98-473a-8409-25813530df66 | Live URL | Ingest listing/referral metrics if available. |
| Awesome MCP Servers | https://github.com/punkpeye/awesome-mcp-servers/pull/7790 | Merged 2026-06-19 | Monitor referral attribution. |

Repository discovery metadata, public landing-page links and referral attribution are already implemented. No external listing was modified during this release; future account-side edits remain owner actions.
