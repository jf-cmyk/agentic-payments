export class BlocksizePaymentRequired extends Error {
  constructor(public readonly paymentRequired: string | null) {
    super("Blocksize starter credits are unavailable or exhausted; complete the returned x402 challenge.");
  }
}

function instrument(value: string): string {
  const clean = value.trim().toLowerCase().replaceAll("_", "-").replaceAll("/", "-");
  if (!clean || !/^[a-z0-9-]+$/.test(clean)) {
    throw new Error("instrument must contain only letters, numbers, slash, underscore, or hyphen");
  }
  return encodeURIComponent(clean);
}

async function requestBlocksize(path: string, agentId: string): Promise<Record<string, unknown>> {
  if (agentId.trim().length < 8) throw new Error("agentId must be a stable identifier with at least 8 characters");
  const response = await fetch(`https://mcp.blocksize.info${path}`, {
    headers: {
      Accept: "application/json",
      "X-Agent-ID": agentId,
      "User-Agent": "blocksize-framework-integration/1.0",
    },
    cache: "no-store",
  });
  if (response.status === 402) {
    throw new BlocksizePaymentRequired(response.headers.get("PAYMENT-REQUIRED"));
  }
  if (!response.ok) throw new Error(`Blocksize returned HTTP ${response.status}: ${(await response.text()).slice(0, 800)}`);
  return await response.json() as Record<string, unknown>;
}

export function getBlocksizeVwap(pair: string, agentId: string) {
  return requestBlocksize(`/v1/vwap/${instrument(pair)}`, agentId);
}

export function getBlocksizeBidAsk(pair: string, agentId: string) {
  return requestBlocksize(`/v1/bidask/${instrument(pair)}`, agentId);
}
