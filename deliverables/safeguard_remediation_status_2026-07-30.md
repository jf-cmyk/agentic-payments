# Safeguard remediation status — 2026-07-30

## Overall decision

Safeguards 1–8 have been implemented and accepted for the curated 0.6.5
candidate, with explicit production-promotion gates still open for dynamic RWA
evidence and external clients. Safeguard 9's current local gates pass; the
staging predecessor passed exact-head CI and proved image build and application
startup. The current proxy and readiness-privacy patch still requires its own
exact-head CI. The overall release remains blocked by that CI, hosted readiness,
full staged acceptance, and manual production-promotion requirements.

| # | Safeguard | Local status | Remaining external gate |
| --- | --- | --- | --- |
| 1 | Freeze and provenance | Accepted | Keep production frozen until the release sequence passes |
| 2 | RWA and payment boundaries | Accepted | Revalidate representative boundaries in staging |
| 3 | Credentials and privacy | Accepted | Complete signed-client OAuth acceptance |
| 4 | Payment lifecycle, accounting, and product truth | Accepted | Reconcile real staged delivery and accounting evidence |
| 5 | Deterministic packaging, readiness, promotion, rollback | Current local gates pass; predecessor CI and staging build/start proven | Pass current exact-head CI, achieve green hosted readiness, and exercise rollback |
| 6 | RWA identity, registries, ledger, payload, freshness, and bounds | Accepted locally | Refresh stale dynamic catalogs, obtain a second daily snapshot, migrate schema-v3 history, and complete 14-day/human gates |
| 7 | OpenAI, Claude, Cursor, and universal Agent Skill | Accepted locally | Sign packages and run each real client against staging |
| 8 | Registries, mirrors, crawlers, docs, and command-center truth | Accepted locally | Align production and external platforms only after 0.6.5 is live |
| 9 | Dependencies, CI, isolated artifact, staging, and release | Current local suite passes; predecessor exact-head CI and isolated build/start pass; release no-go | Current exact-head CI, production-capable facilitator, green `/readyz`, hosted audit, signed clients, and manual promotion |

## Current truth

- Curated release branch: `codex/safeguard-release-0.6.5`, based on canonical
  GitHub `main` and containing only the reviewed safeguard scope. The original
  mixed `codex/safeguard-remediation-0.6.5` worktree remains untouched for
  forensic reference.
- Staging-diagnostic head: `8871a5f06d6b2ed162f5f74d79f00ab3af7b1f65`;
  GitHub Actions run `30848975204` passed its full clean-checkout release gate.
- Staging deployment `339901eb-b448-4a01-813f-ce92513b975b` built and started
  successfully, then failed closed at `/readyz` with the production-ineligible
  facilitator as a known hard blocker. It was not activated as a release.
- All temporarily borrowed production-derived staging values were removed and
  the independent staging cache configuration was restored. The non-secret
  staging proxy allowlist is now narrowed to `100.64.0.0/10` for the next run.
- Production: online and healthy at version 0.6.2, but `/readyz` returns `404`.
- Official MCP Registry: latest published version 0.6.3.
- Canonical GitHub `main`: version 0.6.4, unprotected, and missing the local CI.
- GitLab mirror: version 0.6.2 at the 2026-05-20 mirror commit.
- Candidate packages: OpenAI 0.4.0, Claude 0.3.0, Cursor 1.3.0, and universal
  skill 0.4.0; deterministic but intentionally unsigned and unpublished.

## Next execution priority

Commit the current patch and require its exact-head CI to pass. Then provision
dedicated staging upstream credentials and a production-capable facilitator,
deploy that newly reviewed SHA, require green `/readyz`, and run the exact-commit
hosted HTTP/MCP, data, payment-boundary, persistence, command-center, and
OpenAI/Claude/Cursor acceptance. Protect `main` with the green workflow as a
required check. Production promotion remains a later manual decision after
those gates and a rollback exercise pass.
