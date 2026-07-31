# Safeguard 9 — Dependency, artifact, CI, staging, and release readiness

Date: 2026-07-30

## Decision

The curated 0.6.5 release candidate passes the implemented dependency,
secret-hygiene, version, test, packaging, and reproducibility gates in a clean
worktree based on canonical GitHub `main`. The overall release remains a
**no-go** because protected CI has not yet accepted the branch, there is no
separate Railway staging environment, and the live service is not
readiness-gated.

## Defects remediated

- Updated nine vulnerable runtime packages. The installed-environment audit
  moved from 32 known advisories across nine packages to zero known
  vulnerabilities.
- Added explicit safe dependency floors and regenerated a fully exact-pinned,
  runtime-only `requirements.txt` from the locked dependency graph.
- Added a tracked-file secret-hygiene scanner that uses high-confidence patterns
  and never prints discovered values. The local registry key remains ignored,
  mode `0600`, and absent from Git history.
- Added release-contract checks for the 0.6.5 version, canonical repository,
  generated registry metadata, Railway readiness configuration, and clean-tree
  enforcement in CI.
- Added deterministic double wheel builds and a strict installed-data allowlist.
  The verifier rejects missing public assets and any unlisted packaged file.
- Reworked the installed-release smoke gate to create a fresh virtual
  environment, install every exact runtime pin, install the wheel separately,
  run `pip check`, remove host Python paths, and assert FastAPI, FastMCP, HTTPX,
  Pydantic, and Starlette all load from that isolated environment.
- Hardened GitHub and GitLab CI with pinned action/tool versions, concurrency,
  timeouts, linting, secret scanning, dependency audit, full tests, clean release
  contracts, deterministic artifact verification, and installed-wheel smoke.
- Added a distinct staging job and a manual, serialized production job with
  required environment separation and a post-deployment hosted audit.
- Added release stamping and hosted verification so a deployment must report the
  expected commit before promotion is accepted.

## Accepted locally

| Gate | Result |
| --- | --- |
| Full repository suite | Pass; 829 tests |
| Release safeguard suite after final isolation changes | Pass; 73 tests |
| Ruff | Pass; repository-wide |
| Python compilation | Pass; release safeguard scripts |
| Diff whitespace check | Pass |
| Secret hygiene | Pass |
| Lock integrity | Pass; 143 packages resolved |
| Installed dependency compatibility | Pass; 137 packages compatible |
| Known-vulnerability audit | Pass; zero known vulnerabilities |
| 0.6.5 release contracts without clean-tree requirement | Pass |
| Two isolated wheel builds | Pass; byte-identical |
| Wheel contents and public-data allowlist | Pass |
| Source-free installed-target smoke | Pass |
| Fresh-venv implementation regressions | Pass |

The final networked fresh-venv download could not run in this restricted
workspace because outbound dependency installation was denied. The gate fails
closed and is wired into both protected CI systems; its real clean-machine run
remains mandatory evidence, not an assumed pass.

The Safeguard 6 notebook was also linted with a minimal annotation for its
intentional imports after repository-path setup and executed top to bottom with
all eight code cells succeeding.

## Current release blockers

1. The original mixed worktree still has 503 changed path entries: 89 modified,
   one deleted, and 413 untracked. It remains quarantined as forensic source;
   the reviewed release scope has been curated separately onto
   `codex/safeguard-release-0.6.5` from canonical GitHub `main`.
2. GitHub `main` is unprotected and has no `.github/workflows` directory in the
   public branch. The new local CI has therefore never protected a merge.
3. Railway has only `production`; there is no separate staging environment or
   service on which to prove the candidate.
4. Production is running version 0.6.2 and returns `404` from `/readyz`. Railway
   reports no configured healthcheck path or timeout and allows ten restart
   retries.
5. The production deployment was uploaded from a local source context. Hosted
   commit stamping detects provenance drift but does not yet prove that deployed
   bytes are the exact wheel verified in CI.
6. Package signing, SBOM/provenance attestation, and an exercised automatic or
   one-command rollback remain future release-hardening work.
7. Signed-in OpenAI, Claude, and Cursor client acceptance has not run against a
   staged 0.6.5 deployment.

## Required release sequence

1. Publish the curated release branch as a draft pull request and require its
   clean-checkout CI, including the `--require-clean` release contract.
2. Protect GitHub `main`, require the new CI, and keep GitLab as a mirror unless
   it is intentionally restored as a release source.
3. Create a physically separate Railway staging environment/service and set the
   distinct staging variables required by the pipeline.
4. Run protected CI, including the real networked fresh-venv smoke, dependency
   audit, deterministic double build, and wheel verification.
5. Deploy that reviewed artifact to staging; run hosted HTTP/MCP, data,
   payment-boundary, command-center, and signed-client acceptance.
6. Record the last-known-good production deployment and test the rollback
   command before promotion.
7. Manually promote production, verify `/readyz`, version, commit SHA, manifest,
   all connector challenges, and representative free and paid boundaries, then
   watch logs and health before publishing registries and packages.

## Release boundary

The curated branch may be committed and reviewed, but it must not be merged or
published to registries until protected CI and isolated staging acceptance pass.
No deployment or production mutation was performed during this acceptance pass;
the existing production service remains online on its rolled-back 0.6.2
baseline.
