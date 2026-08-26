# Pay Skills Submission Staging

This folder mirrors the files intended for a future `solana-foundation/pay-skills`
PR.

Target registry path:

- `providers/blocksize/market-data/PAY.md`
- `providers/blocksize/market-data/openapi.json`

Current validation status:

- `pay catalog check providers/blocksize/market-data/PAY.md --no-probe` passes
  with 16 endpoints walked as of 2026-07-29.
- A live probe with valid example bodies for required-symbol POST routes passed
  previously against production. That result is point-in-time evidence only:
  re-run the live probe against the release candidate before submission. This
  staging note does not claim a current end-to-end production result.

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
