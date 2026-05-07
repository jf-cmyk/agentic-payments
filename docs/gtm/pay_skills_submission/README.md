# Pay Skills Submission Staging

This folder mirrors the files intended for a future `solana-foundation/pay-skills`
PR.

Target registry path:

- `providers/blocksize/market-data/PAY.md`
- `providers/blocksize/market-data/openapi.json`

Current validation status:

- `pay catalog check providers/blocksize/market-data/PAY.md --no-probe` passes.
- Live probe is blocked until `mcp.blocksize.info` is redeployed with the local x402 challenge compatibility patch in `src/resource_server.py`.

Commands used:

```bash
pay catalog scaffold blocksize/market-data https://mcp.blocksize.info/openapi.json --output-dir providers
pay catalog check providers/blocksize/market-data/PAY.md --no-probe
pay catalog check providers/blocksize/market-data/PAY.md -v --probe-timeout 20
```

The staged OpenAPI is intentionally filtered to four paid market-data example
routes. The full public Blocksize OpenAPI includes free discovery, docs, MCP,
and credit-management endpoints that are not the right surface for Pay.sh's
paid-provider probe.
