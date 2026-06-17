# End-to-End User and Security QA

Date: 2026-06-10

Scope: main Blocksize web service, public MCP discovery server, paid x402 HTTP API, Claude MCP connector, Cursor MCP connector, Pay.sh/pay-skills, official MCP Registry, Glama, Smithery, x402scan, GitHub PR/listing state, and focused security probes.

## Executive Result

Overall production user flows are healthy.

Passed:

- Local regression suite after fixes: `143 passed`.
- Homepage, docs, OpenAPI, privacy, support, prompt examples, quickstart, MCP manifest, and Glama claim file return 200.
- Homepage renders the expected product signal and all images completed after full page load.
- Public remote MCP initializes and exposes only read-only discovery/documentation tools.
- Claude and Cursor MCP endpoints require OAuth and return proper 401 challenges when unauthenticated.
- Paid HTTP market-data endpoints return structured 402 x402 challenges without serving data.
- Pay.sh provider validation passes locally and against live endpoints.
- Pay.sh live catalog search shows `blocksize/market-data`.
- GitHub and GitLab remotes are reachable and point to the same `main` HEAD.
- Browser security headers are now covered locally for free responses and 402 payment responses.
- Local secret/credential ignore coverage now includes wallet files, entitlement DBs, registry keys, `.DS_Store`, and the downloaded publisher binary.
- Awesome MCP now has a GitHub-hosted package repo and replacement PR that passes that repository's automated submission check.

Needs attention:

- Official MCP Registry is stale: registry API still marks version `0.6.1` as latest, while live `/server.json` serves `0.6.2`. Local `server.json` validation passes, but publish is blocked by expired registry auth.
- Smithery listing is live at `https://smithery.ai/servers/blocksize/agentic-payments?capability=tools#performance`; the older checked slug `info.blocksize.mcp/agentic-payments` returns 404.
- Awesome MCP Servers PR `#5564` remains closed without merge because that list only accepts GitHub-hosted MCP servers; replacement PR `#7790` is open and passes automated submission checks.
- Awesome Agentic Commerce PR remains closed without merge as "too thin for the curated list right now."
- New GitHub-hosted Awesome MCP package uses the expected Glama badge format, but the badge endpoint can return 404 until Glama evaluates the new repository.

## Commands and Results

Local checks:

- `.venv/bin/python -m pytest tests/ -q` -> `143 passed, 1 warning`.
- `.venv/bin/python -m pytest tests/test_resource_server.py -q` -> `65 passed`.
- `python3 -m json.tool server.json` -> valid JSON.
- `python3 -m json.tool docs/smithery_manifest.json` -> valid JSON.
- `python3 -m json.tool blocksize-cursor-plugin/plugins/blocksize-market-data/mcp.json` -> valid JSON.
- `python3 -m json.tool claude-plugin/blocksize-market-data/.claude-plugin/plugin.json` -> valid JSON.
- `tools/mcp-publisher validate` -> `server.json is valid`.
- `tools/mcp-publisher publish server.json` -> blocked by `401 Invalid or expired Registry JWT token`.

Live web/docs:

- `GET /health` -> 200 JSON.
- `GET /` -> 200 HTML.
- `GET /openapi.json` -> 200 JSON.
- `GET /server.json` -> 200 JSON, version `0.6.2`.
- `GET /docs` -> 200 HTML.
- `GET /quickstart/remote-mcp` -> 200 HTML.
- `GET /prompt-examples` -> 200 HTML.
- `GET /privacy` -> 200 HTML.
- `GET /support` -> 200 HTML.
- `GET /claude-connector` -> 200 HTML.
- `GET /mcp/manifest.json` -> 200 JSON.
- `GET /.well-known/glama.json` -> 200 JSON.

Paid API and CORS:

- `GET /v1/search?q=BTC&limit=5` -> 200 JSON.
- `GET /v1/instruments/vwap` -> 200 JSON.
- `GET /v1/vwap/BTC-USD` -> 402 JSON.
- `GET /v1/bidask/BTC-USD` -> 402 JSON.
- `GET /v1/fx/EURUSD` -> 402 JSON.
- `GET /v1/metal/XAUUSD` -> 402 JSON.
- 402 challenge includes Solana and Base accept legs, raw USDC asset IDs, canonical HTTPS resource URL, `accepts[].resource`, `accepts[].extra.resource`, `Payment-Required`, and `Cache-Control: no-store`.
- With `Origin: https://mcp.blocksize.info`, 402 response includes `Access-Control-Allow-Origin` and exposes payment response headers.
- OPTIONS preflight allows `content-type,x-payment,payment-signature,authorization`.

MCP client checks:

- Public MCP tools: `fetch`, `get_market_data_endpoint`, `get_pricing_info`, `list_instruments`, `search`, `search_pairs`.
- Public MCP `search_pairs`, `list_instruments`, `get_pricing_info`, `get_market_data_endpoint`, `search`, and `fetch` worked.
- `get_market_data_endpoint` returns the REST URL and states `returns_live_data=false`, `starts_payment=false`, `side_effects=none`.
- Claude MCP client unauthenticated check -> 401 OAuth challenge.
- Cursor MCP client unauthenticated check -> 401 OAuth challenge.
- Anthropic OAuth metadata -> 200 with issuer `https://mcp.blocksize.info/anthropic/mcp`.
- Cursor OAuth metadata -> 200 with issuer `https://mcp.blocksize.info/cursor/mcp`.
- Root OAuth authorization-server metadata currently points to Anthropic, as intended for Claude fallback discovery.

Security probes:

- HTTP `http://mcp.blocksize.info/` -> 301 redirect to HTTPS.
- `GET /.env` -> 404.
- `GET /.git/config` -> 404.
- `GET /.well-known/security.txt` -> 404.
- `GET /v1/vwap/..%2Fbad` -> 400 JSON with validation message.
- `GET /v1/instruments/../../etc/passwd` -> 404 JSON.
- Script-like search query returns 200 JSON with zero matches, not HTML execution.
- Fake bearer token on Claude MCP -> 401 with OAuth metadata pointer, no stack trace.
- Cursor MCP unauthenticated -> 401 with Cursor OAuth metadata pointer.
- Malformed public MCP POST without required Accept headers -> 406 JSON, no stack trace.
- Focused local secret grep found environment-variable references and test tokens only; no literal private keys or obvious production API keys were found in the checked files.
- Local free responses include `Strict-Transport-Security`, `X-Content-Type-Options: nosniff`, `Referrer-Policy: strict-origin-when-cross-origin`, and `Permissions-Policy: camera=(), microphone=(), geolocation=()`.
- Local 402 responses preserve `Payment-Required`, `Cache-Control: no-store`, CORS expose headers, and the browser hardening headers.

Distribution and repo state:

- Pay.sh local no-probe check passes.
- Pay.sh live probe passes 4/4 endpoints and reports Solana-compatible x402 gates.
- `pay skills search blocksize` shows `blocksize/market-data` in the live catalog.
- `solana-foundation/pay-skills#41` is merged and approved as of 2026-06-10.
- `punkpeye/awesome-mcp-servers#5564` is closed without merge; maintainer reason: only GitHub-hosted MCP servers are accepted.
- New GitHub-hosted package: `https://github.com/jf-cmyk/blocksize-agentic-payments-mcp`.
- Replacement Awesome MCP Servers PR: `punkpeye/awesome-mcp-servers#7790`, open with `has-glama`, `has-emoji`, `valid-name`, clean merge state, and successful `check-submission`.
- `Merit-Systems/awesome-agentic-commerce#161` is closed without merge; maintainer reason: listing was too thin for the curated list.
- Official MCP Registry search returns active entries, but latest is still `0.6.1`.
- Glama connector page returns 200 and contains Blocksize/agentic-payments/healthy signals.
- x402scan server page returns 200 and contains Blocksize signals.
- Smithery listing returns 200 at `https://smithery.ai/servers/blocksize/agentic-payments?capability=tools#performance`.
- GitHub remote `github` and GitLab remote `origin` both resolve to `02740e08941b678990ef354519c9335ab6ef67e5` on `main`, matching local HEAD.

## Recommended Follow-Ups

1. Refresh MCP Registry auth with a valid hex private key or interactive login, then republish so latest is `0.6.2` and metadata matches live `/server.json`.
2. Submit or wait for Glama to evaluate `jf-cmyk/blocksize-agentic-payments-mcp` so the Awesome MCP score badge resolves with a quality score.
3. Rework Awesome Agentic Commerce only if there is a stronger proof package, such as Pay.sh listing, x402scan listing, concrete paid usage, and launch-quality screenshots.
