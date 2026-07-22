# Pay Skills Submission Staging

This folder mirrors the files intended for a future `solana-foundation/pay-skills`
PR.

Target registry path:

- `providers/blocksize/market-data/PAY.md`
- `providers/blocksize/market-data/openapi.json`

Current validation status:

- `pay catalog check providers/blocksize/market-data/PAY.md --no-probe` passes with 16 endpoints walked.
- `pay catalog check providers/blocksize/market-data/PAY.md -v --probe-timeout 20` passes against the live service with 16/16 Solana-compatible x402 gates.

Commands used:

```bash
pay catalog scaffold blocksize/market-data https://mcp.blocksize.info/openapi.json --output-dir providers
pay catalog check providers/blocksize/market-data/PAY.md --no-probe
pay catalog check providers/blocksize/market-data/PAY.md -v --probe-timeout 20
```

The staged OpenAPI is intentionally filtered to paid market-data and agent
product routes. The full public Blocksize OpenAPI includes free discovery,
docs, MCP, readiness, and provenance endpoints that are not the right surface
for Pay.sh's paid-provider probe.
