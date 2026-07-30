# Safeguard remediation status — 2026-07-30

## Overall decision

Safeguards 1–8 have been implemented and accepted for the curated 0.6.5
candidate, with explicit production-promotion gates still open for dynamic RWA
evidence and external clients. Safeguard 9's clean-worktree technical gates
pass, but the overall release remains blocked by protected CI, staging, and
live-readiness requirements.

| # | Safeguard | Local status | Remaining external gate |
| --- | --- | --- | --- |
| 1 | Freeze and provenance | Accepted | Keep production frozen until the release sequence passes |
| 2 | RWA and payment boundaries | Accepted | Revalidate representative boundaries in staging |
| 3 | Credentials and privacy | Accepted | Complete signed-client OAuth acceptance |
| 4 | Payment lifecycle, accounting, and product truth | Accepted | Reconcile real staged delivery and accounting evidence |
| 5 | Deterministic packaging, readiness, promotion, rollback | Accepted locally | Prove in protected CI/staging and exercise rollback |
| 6 | RWA identity, registries, ledger, payload, freshness, and bounds | Accepted locally | Refresh stale dynamic catalogs, obtain a second daily snapshot, migrate schema-v3 history, and complete 14-day/human gates |
| 7 | OpenAI, Claude, Cursor, and universal Agent Skill | Accepted locally | Sign packages and run each real client against staging |
| 8 | Registries, mirrors, crawlers, docs, and command-center truth | Accepted locally | Align production and external platforms only after 0.6.5 is live |
| 9 | Dependencies, CI, isolated artifact, staging, and release | Clean-worktree gates pass; release no-go | Protected CI, separate staging, live `/readyz`, and exact-artifact evidence |

## Current truth

- Curated release branch: `codex/safeguard-release-0.6.5`, based on canonical
  GitHub `main` and containing only the reviewed safeguard scope. The original
  mixed `codex/safeguard-remediation-0.6.5` worktree remains untouched for
  forensic reference.
- Production: online and healthy at version 0.6.2, but `/readyz` returns `404`.
- Official MCP Registry: latest published version 0.6.3.
- Canonical GitHub `main`: version 0.6.4, unprotected, and missing the local CI.
- GitLab mirror: version 0.6.2 at the 2026-05-20 mirror commit.
- Candidate packages: OpenAI 0.4.0, Claude 0.3.0, Cursor 1.3.0, and universal
  skill 0.4.0; deterministic but intentionally unsigned and unpublished.

## Next execution priority

Publish the curated branch as a draft pull request, let its clean-checkout CI
establish the required check names, protect `main`, and then create the isolated
Railway staging service. Production promotion remains a later manual decision
after staged HTTP/MCP, data, payment-boundary, command-center, and signed-client
acceptance.
