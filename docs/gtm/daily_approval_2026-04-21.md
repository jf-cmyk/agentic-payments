# Daily Approval Log - 2026-04-21

## Approval received

Johann approved the initial GTM operating plan and added one standing constraint:

> Do not modify anything on GitHub throughout this initiative.

Interpretation:

- Local workspace files may be created and updated for planning, strategy, and execution artifacts.
- GitHub API, GitHub PRs, GitHub issues, GitHub commits, pushes, and repo modifications are out of scope unless Johann later changes this standing rule.
- Linear remains local-plan only until team/project details are explicitly approved.
- External outreach, publishing, production deploys, wallet/fund movement, and pricing changes still require explicit approval.

## Executed today

| Priority | Owner | Todo | Status | Artifact |
| --- | --- | --- | --- | --- |
| P0 | Agent President | Confirm approval rules and local command center | Done | [README.md](README.md) |
| P0 | Product Readiness Agent | Audit repo, docs, endpoints, payment flow, and demo readiness | Done locally | [product_readiness_scorecard.md](product_readiness_scorecard.md) |
| P0 | Analytics Agent | Define minimum KPI instrumentation | Done | [kpi_instrumentation_spec.md](kpi_instrumentation_spec.md) |
| P1 | Developer Growth Agent | Draft 5-minute quickstart and demo flow | Done | [quickstart_demo_outline.md](quickstart_demo_outline.md) |
| P1 | Revenue Strategy Agent | Produce ICP and offer recommendation | Done | [icp_offer_memo.md](icp_offer_memo.md) |
| P1 | Sales Pipeline Agent | Build 25 design-partner target categories/examples | Done | [design_partner_targets.md](design_partner_targets.md) |
| P2 | Protocol Intelligence Agent | Compare protocol/partner landscape | Done | [protocol_partner_memo.md](protocol_partner_memo.md) |
| P0 | Agent President | Run local demo-hardening sprint after approval | Done locally | README, API manual, scripts, tests |
| P1 | Sales Pipeline Agent | Start named-account outreach prep after approval | Done locally | [named_account_outreach_prep.md](named_account_outreach_prep.md), [outreach_sequences.md](outreach_sequences.md) |

## Worker-agent synthesis

- Product Readiness Agent scored developer activation readiness at 42/100. Main issue: docs and demo scripts are ahead of the verified implementation surface.
- Revenue Strategy Agent recommended starting with crypto/onchain data and market-data vendors/builders, then payments/risk-ops data, then research/compliance intelligence. Rates/Treasury remain out of portfolio.
- Protocol Intelligence Agent recommended leading with x402 for data APIs while staying protocol-agnostic above settlement and tracking AP2, ACP, Stripe, Circle, Nevermined, Skyfire, and approval workflows.

## Local verification

Commands run before the demo-hardening sprint:

```text
python -m pytest tests/ -q
```

Result: system Python had no `python` binary, then `python3` had no `pytest`.

```text
.venv/bin/python -m pytest tests/ -q
```

Result: collection failed because the current FastMCP dependency path expects `py-key-value-aio[memory]` behavior and cannot import `TLRUCache` from the installed `cachetools`.

```text
.venv/bin/python -m pytest tests/test_resource_server.py -q
```

Result: 15 passed, 1 failed. The failing test expected `/v1/rate/10Y` to require payment, but rates have been intentionally removed from the offered portfolio.

```text
.venv/bin/python -m pytest tests/test_blocksize_client.py -q
```

Result: 19 passed, 5 failed. Failures are concentrated around mocked response shapes for bid/ask, equity, FX, and now-removed treasury-rate support.

Commands run after the demo-hardening sprint:

```text
.venv/bin/python -m pytest tests/test_blocksize_client.py -q
```

Result: 23 passed.

```text
.venv/bin/python -m pytest tests/test_resource_server.py -q
```

Result: 16 passed.

```text
.venv/bin/python -m pytest tests/test_mcp_tools.py -q
```

Result: 10 passed.

```text
.venv/bin/python -m pytest tests/ -q
```

Result: 49 passed.

## Demo-hardening changes

- Rates/Treasury endpoints are now consistently treated as intentionally not offered.
- REST quickstart docs now focus on the currently offered HTTP surface.
- Demo scripts now parse `payTo` from payment requirements and send base64 JSON payment signatures.
- Local mock x402 mode is available through `X402_ALLOW_MOCK_PAYMENTS=true` and `scripts/mock_x402_demo.py`.
- FastMCP test collection is restored by pinning `cachetools>=5.3.0`.
- Client parsing now accepts direct-dict and wrapped `snapshot` response shapes.

## Decisions needed next

1. Pick the first ICP for the 14-day sprint: crypto/onchain data, payments/risk-ops data, or research/compliance intelligence.
2. Decide whether to prioritize payment-proof hardening or named-account pipeline next.
3. Do you want Linear tickets created now? If yes, provide the Linear team/project target.
4. External outreach remains draft-only until Johann explicitly approves sending.
