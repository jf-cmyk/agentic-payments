# Product Readiness Scorecard

Assessment date: 2026-04-21

## Overall status

Readiness for developer activation: **62/100**.

The product has the right wedge: agent-readable financial data, free discovery, HTTP 402 payment challenges, credit drawdown, MCP discoverability, and dual-network settlement language. The first demo-hardening pass aligned docs/tests/scripts to the implemented surface and restored local test confidence. The remaining blockers are production-grade payment verification, persistent replay/credit controls, and trust artifacts for enterprise buyers.

## Strengths

- Clear self-serve wedge: agents can discover instruments for free and pay per data call.
- Useful coverage narrative: crypto, equities, FX, metals, and analytics. Rates/Treasury endpoints are intentionally out of portfolio.
- Existing REST server at `src/resource_server.py` with HTTP 402 middleware, credit drawdown, batch query, MCP manifest, and health/pricing metadata.
- Existing MCP server at `src/mcp_server.py` with agent-facing tools and token-efficient response summaries.
- Bulk credit model already exists in `src/credit_manager.py`, including starter/pro/institutional packages.
- Payment economics are easy to explain: data calls priced around $0.001 to $0.008 and bulk credits discount usage.
- Demo assets exist in `scripts/`, `docs/developer_portal.html`, and `docs/api_agent_manual.md`.
- Local test suite passes cleanly: 49 tests green on 2026-04-21.

## Completed on 2026-04-21

- Removed rates/Treasury from the offered portfolio in docs, MCP descriptions, tests, and HTTP expectations.
- Aligned REST quickstart claims with the currently offered HTTP surface.
- Updated demo scripts to parse `payTo` and emit the current base64 JSON `PAYMENT-SIGNATURE` format.
- Added local mock x402 demo support with explicit `X402_ALLOW_MOCK_PAYMENTS=true` gating.
- Pinned `cachetools>=5.3.0` so FastMCP tests collect in the local venv.
- Hardened client parsing for direct-dict and wrapped `snapshot` response shapes.

## Blockers before polished demos

| Blocker | Evidence | Revenue risk | Recommended action |
| --- | --- | --- | --- |
| Missing image artifacts | `docs/api_agent_manual.md` references `assets/architecture_diagram.png` and `assets/swimlane_diagram.png`, but local `docs/assets` does not include them | Manual looks broken to evaluators | Generate or remove these assets |
| Payment verification is proof-of-transaction, not amount/recipient validation yet | `_verify_payment` checks transaction existence/status, not decoded transfer amount/payTo/asset | Enterprise trust claims are premature | Add amount, recipient, asset, freshness, and network checks |
| Replay protection is process-local | `_SEEN_TX_HASHES` is in memory | Replay protection resets on deploy/restart | Persist used proofs in SQLite/Postgres/Redis |
| Credit database defaults to local `credits.db` | `CreditManager(db_path="credits.db")` | Production stateless/cloud story conflicts with local SQLite | Move to managed persistence or document local-only status |
| `X-AGENT-WALLET` does not prove wallet ownership | Credit path uses a wallet header plus balance/history checks | Free-rider abuse risk | Add signed nonce or wallet proof |

## Docs gaps

- Need a single "Known-good demo path" that is promoted as the main first-run path.
- Need a copy-paste cURL walkthrough that shows the exact `PAYMENT-REQUIRED` header shape.
- Need a short enterprise trust appendix that states what is verified today versus planned.
- Need architecture and swimlane images or the manual should stop referencing them.
- Need golden JSON response fixtures for main paid endpoints.

## Test results

| Test command | Result | Notes |
| --- | --- | --- |
| `.venv/bin/python -m pytest tests/ -q` | 49 passed | Clean run |
| `.venv/bin/python -m pytest tests/test_resource_server.py -q` | 16 passed | Rates intentionally return 404 |
| `.venv/bin/python -m pytest tests/test_blocksize_client.py -q` | 23 passed | Response-shape parser fixed |
| `.venv/bin/python -m pytest tests/test_mcp_tools.py -q` | 10 passed | Pricing labels aligned |

## Top 10 local backlog items

| Rank | Item | Revenue rationale |
| --- | --- | --- |
| 1 | Add amount/recipient/asset/freshness validation to payment proofs | Upgrades from prototype to enterprise-trust posture |
| 2 | Persist replay protection and credit balances outside process memory/local SQLite for hosted deployments | Makes payment reliability credible |
| 3 | Harden `X-AGENT-WALLET` with signed nonce or wallet proof | Protects credits and free trials from abuse |
| 4 | Promote the known-good `/v1/vwap/BTC-USD` paid-call path as the main first-run demo | Converts curiosity into first activation |
| 5 | Add usage event logging for 402 challenges, paid successes, credit draws, and failures | Enables revenue dashboard |
| 6 | Add public "verified today / planned next" trust matrix | Avoids overclaiming and improves enterprise credibility |
| 7 | Generate architecture and swimlane images or remove broken references | Prevents evaluator friction |
| 8 | Build golden JSON response fixtures for the main paid endpoints | Stabilizes demos and tests |
| 9 | Convert first ICP into named accounts and interview asks | Starts revenue motion |
| 10 | Draft Circle/Nevermined/Skyfire partner briefs | Partnership leverage |
