# Blocksize x402 buyer examples

These examples use the official x402 v2 clients and fail closed at an explicit
USDC spend limit. They make one read-only Blocksize data request and never
execute a trade.

## Python: Base USDC

Install the repository environment, put a dedicated Base wallet private key in
`EVM_PRIVATE_KEY`, and opt in to one bounded payment:

```bash
EVM_PRIVATE_KEY='0x...' \
  uv run python examples/x402/buy_with_base.py --pay --max-usdc 0.002
```

The default request is the $0.002 BTC-USD VWAP. A package POST can be supplied
with `--url`, `--method POST`, and `--json-body`, with a matching higher
`--max-usdc` only when that spend is intended.

## Solana USDC

Use the hardened maintained canary. It reads a key file once, validates the
exact Solana network, mint, recipient, amount, result, and idempotent replay:

```bash
uv run python scripts/run_funded_x402_canary.py \
  --allow-local-key-file /absolute/path/to/dedicated-wallet.json
```

## TypeScript: Base USDC

```bash
cd examples/x402/typescript
npm install
EVM_PRIVATE_KEY='0x...' npm run buy
```

The TypeScript example pins the official x402 packages and filters every
advertised requirement to Base mainnet USDC at or below `MAX_USDC_ATOMIC`
(default: 2,000 atomic units, or $0.002 USDC).

Never use a primary treasury wallet in an example. Use a dedicated low-balance
wallet, review the endpoint and cap, and treat a returned `402` as a request to
fetch and sign a fresh challenge—not as permission to send a standalone token
transfer.
