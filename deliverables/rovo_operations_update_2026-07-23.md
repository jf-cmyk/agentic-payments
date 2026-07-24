# Rovo handoff — Operations only

Use this update only in the Operations Team project under epic `BSOPS-1629` (Agentic Data Offering). Do not search, read or modify any other Atlassian project, space, board or issue.

## Important issue updates

| Issue | Proposed status | Concise update and evidence |
| --- | --- | --- |
| BSOPS-1632 | Ready for review | One-click first-live-price flow is live at `/quickstart/first-price` with stable browser identity, live `/v1/vwap/btc-usd` request, result/provenance display and honest HTTP 402 handling. Exact release server delivered a starter-credit VWAP; production correctly returned an x402 challenge for an acquisition environment ineligible for another starter grant. Evidence: `docs/first_price_quickstart.html`, `tests/test_resource_server.py`, deployment `9de0fa96-d8a1-4eb4-afbd-7405f6f7468a`. |
| BSOPS-1633 | Ready for review | LangChain `@tool` integration implemented using the shared Blocksize client. Evidence: `integrations/langchain/blocksize_tool.py`. |
| BSOPS-1634 | Ready for review | LlamaIndex `FunctionTool` integration implemented. Evidence: `integrations/llamaindex/blocksize_tool.py`. |
| BSOPS-1635 | Ready for review | Vercel AI SDK tools implemented with `inputSchema` and typed x402 failure. Evidence: `integrations/vercel-ai-sdk/blocksize-tool.ts`. |
| BSOPS-1636 | Ready for review | OpenAI Agents SDK `@function_tool` integration implemented. Evidence: `integrations/openai_agents/blocksize_tool.py`. |
| BSOPS-1637 | Ready for review | GOAT `PluginBase`/`@Tool` integration implemented. Evidence: `integrations/goat/`. |
| BSOPS-1638 | Ready for review | Solana Agent Kit integration merges native agent actions with Blocksize Vercel tools. Evidence: `integrations/solana-agent-kit/blocksize-tools.ts`. |
| BSOPS-1639 | In progress | Production RWA pilot writes scheduled AAPL/PAXG/EURC captures to replay history and the queryable ledger. Follow-up automation adds timestamp-aware Blocksize comparisons to every 30-minute cycle and persists a separate alignment history. The first controlled snapshot produced timestamp-aligned passes for PAXG/XAU (-9.13 bps) and EURC/EURUSD (+0.86 bps); AAPL was correctly rejected as not timestamp-aligned because its reference was 3,275s older than the live tokenized-venue observation. All remain `candidate_monitoring` and `production_promoted=false`; the 14-day/672-sample, directly matched independent benchmark, rights, depth/manipulation and human-approval gates remain open. Evidence: `scripts/run_rwa_pilot_alignment_snapshot.py`, `reports/agentic_marketing/rwa_pilot_alignment_latest.json`, tests. |

## Epic note for BSOPS-1629

Production release `48d8a6a` now includes a one-click activation path, six current framework integrations, queryable RWA pilot observations, configured RPC fallback hardening and a public framework integration guide. Railway deployment `9de0fa96-d8a1-4eb4-afbd-7405f6f7468a` succeeded; hosted smoke passed; the first RWA scheduler cycle captured and persisted 3/3; and no post-rollout HTTP 5xx responses were found. Existing public listings for the Official MCP Registry, Glama, Smithery, Pay.sh, x402scan and Awesome MCP were independently reachable on 2026-07-23; Awesome MCP PR 7790 is merged. Do not mark framework marketplace publication, real paid conversion, or any RWA feed promotion complete: those remain explicit external/evidence-gated steps.

GitHub PR #1 is merged into `main` at `9344c47`. The next RWA evidence release candidate automates timestamp-aware comparisons and preserves proxy/staleness failures instead of converting them into a completed benchmark gate.

## Rovo execution boundary

Only add the above comments/status suggestions to the named BSOPS issues after confirming they belong to Operations Team and epic BSOPS-1629. If a proposed workflow status does not exist, add the comment and leave status unchanged. Do not create unrelated tickets and do not access any non-Operations Atlassian content.
