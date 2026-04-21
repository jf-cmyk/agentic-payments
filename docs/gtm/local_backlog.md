# Local GTM Backlog

This is Linear-ready but intentionally local until Johann approves Linear team/project details.

## Completed locally on 2026-04-21

| ID | Task | Result |
| --- | --- | --- |
| GTM-002 | De-scope broken REST claims | README/manual/tests/API agree that rates are not offered and state/vwap30m are not in the HTTP quickstart |
| GTM-003 | Resolve FastMCP dependency test collection | Full test suite collects and passes locally |
| GTM-004 | Refresh demo scripts | Scripts parse `payTo` and current payment requirements correctly |
| GTM-005 | Add mock/testnet payment mode | Local mock mode can complete the 402 flow when explicitly enabled |
| GTM-013 | Convert target categories into named accounts | 25 named targets drafted with segment, priority, rationale, ask, and contact-path hypothesis |
| GTM-014 | Draft first outreach email | Initial email, DM, segment variants, follow-ups, and call agenda drafted locally |
| GTM-020 | Package first-batch outreach artifacts | Route decision, account briefs, Hunter.io workflow, message pack, meeting template, tracker, and handoff email prepared locally |
| GTM-021 | Run Hunter enrichment for first 5 accounts | 25 candidate lead rows generated for Johann review; no messages sent |

## P0 - Demo readiness

| ID | Task | Acceptance criteria | Revenue logic |
| --- | --- | --- | --- |
| GTM-001 | Promote known-good paid-call demo path | `/v1/vwap/BTC-USD` walkthrough is the first-run demo in README/manual/portal | First activation |
| GTM-016 | Add production payment proof validation | Verify amount, recipient, asset, network, freshness, and transaction status | Enterprise trust |
| GTM-017 | Persist proof replay protection | Used proofs survive process restarts | Payment reliability |
| GTM-018 | Harden credit wallet ownership | `X-AGENT-WALLET` requires signed nonce or equivalent proof | Abuse prevention |
| GTM-019 | Resolve manual image artifacts | Architecture/swimlane images exist or references are removed | Demo polish |

## P1 - Revenue instrumentation

| ID | Task | Acceptance criteria | Revenue logic |
| --- | --- | --- | --- |
| GTM-006 | Add event logging spec to implementation plan | Events cover discovery, 402, proof, success, failure, credits | Revenue dashboard |
| GTM-007 | Build local metrics summary script | Daily paid calls, active wallets, failures, endpoint mix visible | Operating cadence |
| GTM-008 | Add payment failure reason taxonomy | 402 failures are grouped by actionable cause | Conversion improvement |

## P1 - Developer activation

| ID | Task | Acceptance criteria | Revenue logic |
| --- | --- | --- | --- |
| GTM-009 | Publish local 5-minute quickstart draft | One page can onboard a developer to the demo flow | Developer conversion |
| GTM-010 | Draft SDK/client wrapper plan | Clear TypeScript/Python wrapper scope | Time to first paid call |
| GTM-011 | Create "verified today vs planned" matrix | Prospects know what is production, testnet, or roadmap | Trust |

## P1 - Sales and partners

| ID | Task | Acceptance criteria | Revenue logic |
| --- | --- | --- | --- |
| GTM-012 | Pick first ICP | Johann approves one ICP for 14-day sprint | Focus |
| GTM-015 | Draft Circle/Nevermined/Skyfire partner briefs | One paragraph value prop and ask for each | Partnership leverage |
