# Get your first live Blocksize price

Blocksize has two integration layers. The authenticated Claude and Cursor MCP connectors can return live read-only market data using included server-side credits. The public ChatGPT-compatible MCP endpoint currently provides discovery and endpoint construction only; use the HTTP call below for the live price.

The hosted quickstart at `https://mcp.blocksize.info/quickstart/first-price`
demonstrates the same fail-closed HTTP flow: an unpaid request receives an x402
v2 challenge, and an official x402 client retries with a signed
`PAYMENT-SIGNATURE`. Browser-selected identity headers do not grant production
credits.

## Claude

1. Add `https://mcp.blocksize.info/anthropic/mcp/` as a custom connector.
2. Sign in with Blocksize.
3. Ask: `Use Blocksize to get the latest BTC-USD VWAP and include the source timestamp.`

Expected live tool: `get_vwap`. The first successful call consumes one included data credit and returns the remaining balance.

## Cursor

Add the authenticated connector to `.cursor/mcp.json`:

```json
{
  "mcpServers": {
    "blocksize-live": {
      "url": "https://mcp.blocksize.info/cursor/mcp/"
    }
  }
}
```

Complete OAuth when Cursor prompts, then ask: `Use Blocksize get_vwap for BTC-USD and report the source timestamp.`

## ChatGPT

The public remote endpoint `https://mcp.blocksize.info/mcp/server/` supports symbol discovery, documentation, pricing inspection, and exact endpoint construction. It does not currently expose the live-price tools in ChatGPT.

The HTTP endpoint returns a standards-based x402 v2 challenge when no payment
is attached:

```bash
curl -i -sS \
  'https://mcp.blocksize.info/v1/vwap/btc-usd'
```

Use an official x402 client to select one of the advertised requirements, sign
it, and retry with the resulting `PAYMENT-SIGNATURE` header. Caller-selected
identity headers do not grant or spend production credits. Included starter
credits are available through authenticated connector identities.

The maintained end-to-end Solana reference is
`scripts/run_funded_x402_canary.py`. It uses the official x402 v2 client,
validates the exact network, USDC mint, amount, recipient, product payload, and
idempotent replay. Legacy examples that submitted a standalone transfer hash
are disabled because that hash is not an x402 authorization and could transfer
funds without unlocking data.

Runnable, spend-capped buyer examples are published in `examples/x402/`:

- `buy_with_base.py` uses the official Python client for Base mainnet USDC.
- `typescript/buy-with-base.ts` uses the official TypeScript fetch client for Base.
- `scripts/run_funded_x402_canary.py` remains the hardened Solana reference.

Each example filters the challenge to the expected network and USDC asset and
refuses an advertised amount above its configured cap. A rejected or expired
signature should be rebuilt from the fresh `PAYMENT-REQUIRED` challenge returned
by Blocksize.

## Verify the result

A successful response should include the instrument, price/VWAP, currency, source timestamp, provider context, and citation or methodology metadata. Do not treat discovery output as a live price.
