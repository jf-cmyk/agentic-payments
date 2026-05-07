# MCP Distribution QA

Date: 2026-05-07

Scope: public remote MCP server, Anthropic-safe MCP endpoint, listing metadata,
Glama, Smithery, official MCP Registry, OpenAI-style discovery tools, Pay.sh
staging, and public docs linked from directory metadata.

## Executive status

Local workspace is healthy for public MCP distribution after the fixes in this
QA pass.

Live production is mostly healthy for the public remote MCP surface, but it is
not yet running all local changes. A redeploy is required before Pay.sh live
validation and before promoting the Anthropic-safe MCP endpoint.

## Local checks

Passed:

- `python3 -m json.tool server.json`
- `.venv/bin/python -m pytest tests/ -q`
- `pay catalog check docs/gtm/pay_skills_submission/providers/blocksize/market-data/PAY.md --no-probe`
- `git diff --check`
- Local public MCP protocol initialize and tools/list
- Local Anthropic MCP protocol initialize and tools/list
- Local listing endpoints:
  - `/health`
  - `/server.json`
  - `/.well-known/glama.json`
  - `/.well-known/mcp-registry-auth`
  - `/.well-known/x402`
  - `/mcp/manifest.json`
  - `/privacy`
  - `/support`

Local public MCP tools exposed:

- `search_pairs`
- `list_instruments`
- `get_pricing_info`
- `search`
- `fetch`

Local Anthropic-safe MCP tools exposed:

- `search_pairs`
- `list_instruments`
- `get_credit_balance`
- `get_vwap`
- `get_bid_ask`
- `get_fx_rate`
- `get_metal_price`

## Live checks

Passed on `https://mcp.blocksize.info`:

- `/health` returns 200.
- `/server.json` returns 200 with `info.blocksize.mcp/agentic-payments`.
- `/.well-known/glama.json` returns 200 with maintainer email.
- `/.well-known/mcp-registry-auth` returns 200.
- `/.well-known/x402` returns 200 with paid endpoint examples.
- `/mcp/manifest.json` returns 200.
- `/privacy` and `/support` return 200.
- `/mcp/server/` supports Streamable HTTP initialize.
- Live public MCP `tools/list` returns the expected five read-only tools.

Live blockers:

- `/anthropic/mcp/` returns 404 in production, even though it works locally.
  This means the Anthropic-safe endpoint is not deployed on the live service.
- Live paid HTTP 402 challenges still emit:
  - `resource.url` with `http://mcp.blocksize.info/...`
  - `accepts[].asset` as a CAIP-style asset string
  These are fixed locally, but production needs redeploy before Pay.sh live
  probe will pass.

## Directory posture

### Official MCP Registry

Status: active, but needs optional metadata refresh.

The registry API currently returns active entries for `info.blocksize.mcp/agentic-payments`,
including version `0.6.1` as latest. The registry still shows repository metadata
from a prior publish, while live `/server.json` now omits repository metadata by
default. This is not a functionality blocker, but republish if the public
registry listing should match the current privacy posture.

### Smithery

Status: technically ready for URL/external submission once production is
redeployed.

The public Streamable HTTP endpoint initializes successfully over HTTPS and
exposes only read-only discovery tools. Smithery-specific submission still
requires account/API-key action outside this repo.

### Glama

Status: claim-ready.

The live `/.well-known/glama.json` file is present and matches Glama's expected
claim-file shape. Manual sign-in/claim is still required.

### Anthropic Connectors Directory

Status: product-policy risky and live endpoint blocked.

The public `/mcp/server/` surface is listing-safe and read-only. The separate
`/anthropic/mcp/` surface works locally but is not live. Anthropic's current MCP
Directory Policy also says MCP servers may not be used to transfer money,
cryptocurrency, or other financial assets, so submit carefully and position the
public connector as discovery/read-only rather than transaction execution.

### OpenAI / ChatGPT Remote MCP

Status: usable as a remote MCP discovery server.

The public MCP endpoint initializes over Streamable HTTP and exposes OpenAI-style
`search` and `fetch` tools along with instrument discovery and pricing.

### Pay.sh / pay-skills

Status: local staging ready, live probe blocked until redeploy.

The staged provider files live at:

- `docs/gtm/pay_skills_submission/providers/blocksize/market-data/PAY.md`
- `docs/gtm/pay_skills_submission/providers/blocksize/market-data/openapi.json`

The filtered sidecar OpenAPI is intentional. The full public OpenAPI includes
free docs, MCP, discovery, and credit routes that are not suitable for Pay.sh's
paid-provider probe.

## Fixes made during QA

- Canonicalized x402 challenge `resource.url` from `PUBLIC_BASE_URL`.
- Normalized public x402 `accepts[].asset` to raw USDC mint/contract while
  preserving CAIP-style legacy requirements.
- Staged Pay.sh provider entry and filtered sidecar OpenAPI.
- Removed public docs claims for unsupported equities/AAPL/broad commodities.
- Corrected public API manual asset-class and service lists.
- Corrected developer portal market coverage cards to match actual routes.
- Regenerated public PDFs from the corrected generator.

## Remaining action checklist

1. Deploy current local changes to production.
2. Re-run live Pay.sh probe:

   ```bash
   pay catalog check docs/gtm/pay_skills_submission/providers/blocksize/market-data/PAY.md -v --probe-timeout 20
   ```

3. Confirm `https://mcp.blocksize.info/anthropic/mcp/` initializes after deploy.
4. Decide whether to republish official MCP Registry metadata to remove old
   repository metadata from the public registry listing.
5. Claim/enrich Glama listing manually.
6. Submit Smithery URL/external listing manually.
7. Open the `solana-foundation/pay-skills` PR only after the live probe passes.

## Sources checked

- Anthropic MCP Directory Policy: https://support.anthropic.com/en/articles/11697096-anthropic-mcp-directory-policy
- Anthropic Connectors Directory FAQ: https://support.anthropic.com/en/articles/11596036-anthropic-mcp-directory-faq
- Glama connector claim-file guidance: https://glama.ai/mcp/connectors/dev.continue/docs
- Smithery publish API docs: https://www.smithery.ai/docs/api-reference/servers/publish-a-server
- Official MCP Registry API: https://registry.modelcontextprotocol.io/v0/servers?search=blocksize
- Pay Skills registry contribution docs: https://github.com/solana-foundation/pay-skills
