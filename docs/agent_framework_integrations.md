# Blocksize integrations for agent frameworks

Blocksize production market data is available as read-only tools for six prioritized agent frameworks. The examples keep a stable `X-Agent-ID`, return Blocksize timestamps and provenance unchanged, and surface HTTP `402 Payment Required` explicitly instead of silently substituting data.

| Framework | Implementation | Current validated version |
| --- | --- | --- |
| LangChain | `integrations/langchain/blocksize_tool.py` | 1.3.14 |
| LlamaIndex | `integrations/llamaindex/blocksize_tool.py` | 0.14.23 |
| OpenAI Agents SDK | `integrations/openai_agents/blocksize_tool.py` | 0.18.3 |
| Vercel AI SDK | `integrations/vercel-ai-sdk/blocksize-tool.ts` | 7.0.37 |
| GOAT | `integrations/goat/blocksize.plugin.ts` | 0.5.0 |
| Solana Agent Kit | `integrations/solana-agent-kit/blocksize-tools.ts` | 2.0.10 |

Start with `https://mcp.blocksize.info/quickstart/first-price` to verify a live response, then copy the matching implementation from the public repository. New eligible agent identities receive the standard starter allowance. If the allowance is unavailable or exhausted, the tool returns the original x402 challenge so the caller can pay or top up.

These examples are implementation-ready and validated in this repository. Separate marketplace listings and package-registry publication require the relevant external accounts and are not represented as complete here.
