# Blocksize agent framework integrations

These examples expose Blocksize production market data as read-only tools for six prioritized agent frameworks. Every example uses a stable caller-supplied agent identity, preserves Blocksize timestamps and citation metadata, and surfaces an HTTP `402` as an explicit payment-required condition.

| Framework | Example | Runtime |
| --- | --- | --- |
| LangChain | `langchain/blocksize_tool.py` | Python |
| LlamaIndex | `llamaindex/blocksize_tool.py` | Python |
| OpenAI Agents SDK | `openai_agents/blocksize_tool.py` | Python |
| Vercel AI SDK | `vercel-ai-sdk/blocksize-tool.ts` | TypeScript |
| GOAT | `goat/blocksize.plugin.ts` | TypeScript |
| Solana Agent Kit | `solana-agent-kit/blocksize-tools.ts` | TypeScript |

The shared clients live in `python/blocksize_http.py` and `typescript/blocksize.ts`. Start with a stable `BLOCKSIZE_AGENT_ID`; eligible new identities receive the standard starter allowance. When it is unavailable or exhausted, the same request returns an x402 challenge.

Python versions validated for these examples on 2026-07-23 are recorded in `python/requirements.txt`. TypeScript versions are pinned in `typescript/package.json`; Zod 3.25.76 is the compatible intersection for the current AI SDK, GOAT, and Solana Agent Kit packages.

These packages are implementation-ready repository integrations. Publishing them to framework marketplaces or separate package registries remains an external release action.
