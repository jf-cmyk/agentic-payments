import { createVercelAITools, type SolanaAgentKit } from "solana-agent-kit";

import { createBlocksizeTools } from "../vercel-ai-sdk/blocksize-tool.js";

export function createSolanaAgentToolsWithBlocksize(agent: SolanaAgentKit, agentId: string) {
  return {
    ...createVercelAITools(agent, agent.actions),
    ...createBlocksizeTools(agentId),
  };
}
