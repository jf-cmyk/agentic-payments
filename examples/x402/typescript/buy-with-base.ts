import { ExactEvmScheme } from "@x402/evm/exact/client";
import { wrapFetchWithPayment, x402Client, x402HTTPClient } from "@x402/fetch";
import { privateKeyToAccount } from "viem/accounts";

const BASE_MAINNET = "eip155:8453";
const BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
const DEFAULT_URL =
  "https://mcp.blocksize.info/v1/vwap/BTCUSD" +
  "?selection_source=published_example_path" +
  "&utm_source=github&utm_medium=buyer_example&utm_campaign=first_price";

async function main(): Promise<void> {
  const rawKey = process.env.EVM_PRIVATE_KEY;
  if (!rawKey || !/^0x[0-9a-fA-F]{64}$/.test(rawKey)) {
    throw new Error("EVM_PRIVATE_KEY must be one 0x-prefixed 32-byte private key");
  }
  const evmPrivateKey = rawKey as `0x${string}`;

  const maxAtomic = BigInt(process.env.MAX_USDC_ATOMIC ?? "2000");
  if (maxAtomic <= 0n) throw new Error("MAX_USDC_ATOMIC must be positive");
  const url = process.env.BLOCKSIZE_URL ?? DEFAULT_URL;
  const method = process.env.REQUEST_METHOD === "POST" ? "POST" : "GET";
  const body = process.env.REQUEST_JSON;
  if (method === "POST" && !body) {
    throw new Error("REQUEST_JSON is required when REQUEST_METHOD=POST");
  }
  if (body) JSON.parse(body);

  const client = new x402Client();
  client.register(BASE_MAINNET, new ExactEvmScheme(privateKeyToAccount(evmPrivateKey)));
  client.registerPolicy((_version, requirements) =>
    requirements.filter(
      requirement =>
        requirement.network === BASE_MAINNET &&
        requirement.asset.toLowerCase() === BASE_USDC.toLowerCase() &&
        BigInt(requirement.amount) <= maxAtomic,
    ),
  );

  const fetchWithPayment = wrapFetchWithPayment(fetch, client);
  const response = await fetchWithPayment(url, {
    method,
    headers: body ? { "content-type": "application/json" } : undefined,
    body,
  });
  const payload: unknown = await response.json();
  if (!response.ok) {
    throw new Error(`Blocksize returned HTTP ${response.status}: ${JSON.stringify(payload)}`);
  }

  const settlement = new x402HTTPClient(client).getPaymentSettleResponse(name =>
    response.headers.get(name),
  );
  console.log(JSON.stringify({ passed: true, data: payload, settlement }, null, 2));
}

main().catch(error => {
  console.error(JSON.stringify({ passed: false, error: String(error) }));
  process.exit(1);
});
