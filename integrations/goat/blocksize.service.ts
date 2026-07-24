import { Tool } from "@goat-sdk/core";

import { getBlocksizeBidAsk, getBlocksizeVwap } from "../typescript/blocksize.js";
import { BlocksizePairParameters } from "./blocksize.parameters.js";

export class BlocksizeService {
  constructor(private readonly agentId: string) {}

  @Tool({
    name: "blocksize_get_vwap",
    description: "Get a live Blocksize VWAP with source timestamp and citation metadata.",
  })
  getVwap(parameters: BlocksizePairParameters) {
    return getBlocksizeVwap(parameters.pair, this.agentId);
  }

  @Tool({
    name: "blocksize_get_bid_ask",
    description: "Get live Blocksize bid/ask data for a supported crypto or equity instrument.",
  })
  getBidAsk(parameters: BlocksizePairParameters) {
    return getBlocksizeBidAsk(parameters.pair, this.agentId);
  }
}
