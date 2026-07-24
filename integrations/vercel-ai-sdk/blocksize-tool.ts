import { tool } from "ai";
import { z } from "zod";

import { getBlocksizeBidAsk, getBlocksizeVwap } from "../typescript/blocksize.js";

export function createBlocksizeTools(agentId: string) {
  return {
    blocksizeVwap: tool({
      description: "Get a live Blocksize VWAP with source timestamp and citation metadata.",
      inputSchema: z.object({ pair: z.string().describe("Instrument such as BTC-USD") }),
      execute: async ({ pair }) => getBlocksizeVwap(pair, agentId),
    }),
    blocksizeBidAsk: tool({
      description: "Get live Blocksize bid/ask data for a supported crypto or equity instrument.",
      inputSchema: z.object({ pair: z.string().describe("Instrument such as ETH-USD or AAPL") }),
      execute: async ({ pair }) => getBlocksizeBidAsk(pair, agentId),
    }),
  };
}
