# Rovo handoff — Operations only

Use this update only in the Operations Team project under epic `BSOPS-1629` (Agentic Data Offering). Do not search, read or modify any other Atlassian project, space, board or issue.

## Important issue updates

| Issue | Proposed status | Concise update and evidence |
| --- | --- | --- |
| BSOPS-1632 | Ready for production verification | One-click first-live-price flow implemented at `/quickstart/first-price` with stable browser identity, live `/v1/vwap/btc-usd` request, result/provenance display and honest HTTP 402 handling. Evidence: `docs/first_price_quickstart.html`, `tests/test_resource_server.py`. |
| BSOPS-1633 | Ready for review | LangChain `@tool` integration implemented using the shared Blocksize client. Evidence: `integrations/langchain/blocksize_tool.py`. |
| BSOPS-1634 | Ready for review | LlamaIndex `FunctionTool` integration implemented. Evidence: `integrations/llamaindex/blocksize_tool.py`. |
| BSOPS-1635 | Ready for review | Vercel AI SDK tools implemented with `inputSchema` and typed x402 failure. Evidence: `integrations/vercel-ai-sdk/blocksize-tool.ts`. |
| BSOPS-1636 | Ready for review | OpenAI Agents SDK `@function_tool` integration implemented. Evidence: `integrations/openai_agents/blocksize_tool.py`. |
| BSOPS-1637 | Ready for review | GOAT `PluginBase`/`@Tool` integration implemented. Evidence: `integrations/goat/`. |
| BSOPS-1638 | Ready for review | Solana Agent Kit integration merges native agent actions with Blocksize Vercel tools. Evidence: `integrations/solana-agent-kit/blocksize-tools.ts`. |
| BSOPS-1639 | In progress | RWA pilot now writes successful scheduled AAPL/PAXG/EURC captures to the queryable observation ledger; all remain explicitly `candidate_monitoring` and `production_promoted=false`. Ethereum/Base configured RPC fallback lists are supported. The 14-day/672-sample, benchmark, rights, independence and human-approval gates remain open. Evidence: `scripts/run_rwa_growth_pilot.py`, `src/rwa_adapters.py`, `src/rwa_store.py`, tests. |

## Epic note for BSOPS-1629

Repository release candidate now includes a one-click activation path, six current framework integrations, queryable RWA pilot observations, configured RPC fallback hardening and a public framework integration guide. Existing public listings for the Official MCP Registry, Glama, Smithery, Pay.sh, x402scan and Awesome MCP were independently reachable on 2026-07-23; Awesome MCP PR 7790 is merged. Do not mark framework marketplace publication, real paid conversion, or any RWA feed promotion complete: those remain explicit external/evidence-gated steps.

## Rovo execution boundary

Only add the above comments/status suggestions to the named BSOPS issues after confirming they belong to Operations Team and epic BSOPS-1629. If a proposed workflow status does not exist, add the comment and leave status unchanged. Do not create unrelated tickets and do not access any non-Operations Atlassian content.
