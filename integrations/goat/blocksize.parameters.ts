import { createToolParameters } from "@goat-sdk/core";
import { z } from "zod";

export class BlocksizePairParameters extends createToolParameters(
  z.object({ pair: z.string().describe("Instrument such as BTC-USD, ETH-USD, or AAPL") }),
) {}
