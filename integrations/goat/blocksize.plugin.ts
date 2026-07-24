import { PluginBase } from "@goat-sdk/core";

import { BlocksizeService } from "./blocksize.service.js";

export class BlocksizePlugin extends PluginBase {
  constructor(agentId: string) {
    super("blocksize", [new BlocksizeService(agentId)]);
  }

  supportsChain = () => true;
}

export function blocksize(agentId: string) {
  return new BlocksizePlugin(agentId);
}
