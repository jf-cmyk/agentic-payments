# Safeguard 9 — Dependency, artifact, CI, staging, and release readiness

Date: 2026-08-03

## Decision

The curated 0.6.5 release candidate passes the implemented dependency,
secret-hygiene, version, test, packaging, and reproducibility gates in a clean
worktree based on canonical GitHub `main`. The staging-diagnostic predecessor
`8871a5f06d6b2ed162f5f74d79f00ab3af7b1f65` passed clean-checkout CI, and the
physically separate Railway staging service built its image and completed
application startup. The current branch also contains the proxy-identity and
public-readiness privacy fixes found by that hosted audit. The overall release
remains a **no-go** because `/readyz` correctly stayed non-200 with the
configured public development facilitator, so Railway did not activate the
candidate and the full hosted and signed-client acceptance sequence could not
run.

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
- Corrected two Railway build assumptions discovered by the isolated diagnostic:
  the Railpack frontend tag requires the leading `v`, and the cached dependency
  layer must install a dependency-only requirements export before source is
  copied into the image.
- Replaced Uvicorn's `X-Forwarded-For`-only normalization with an application
  boundary that accepts one validated Railway `X-Real-IP` only from the narrow
  configured raw-peer range. Hosted readiness rejects a missing, wildcard, or
  overbroad Railway trust range.
- Redacted internal database, entitlement, and OAuth filesystem locations from
  the unauthenticated `/readyz` response while preserving internal validation,
  categorical blockers, public URL diagnostics, and the 200/503 decision.

## Accepted locally

| Gate | Result |
| --- | --- |
| Full repository suite | Pass; 848 tests |
| Release safeguard suite after final isolation changes | Pass; 76 tests |
| Staging-predecessor GitHub Actions CI | Pass; run `30848975204` at `8871a5f` |
| Networked fresh installed-release smoke in CI | Pass |
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
| Registry, public-product, marketplace, and command-center regressions | Pass; 134 credential-free checks |
| Proxy, payment, and public-readiness privacy regressions | Pass; 184 focused safeguard checks |
| Isolated Railway image build and application startup | Pass; deployment `339901eb-b448-4a01-813f-ce92513b975b` |
| Railway readiness activation | Fail closed; production-ineligible facilitator configuration |

The exact-head clean-checkout run installed the exact dependencies from the
network, rebuilt two byte-identical wheels, verified the artifact, and completed
the fresh installed-release smoke. The hosted staging result is diagnostic
evidence only: successful build and startup do not constitute staging
acceptance while readiness is red.

The Safeguard 6 notebook was also linted with a minimal annotation for its
intentional imports after repository-path setup and executed top to bottom with
all eight code cells succeeding.

## Current release blockers

1. The original mixed worktree still has 503 changed path entries: 89 modified,
   one deleted, and 413 untracked. It remains quarantined as forensic source;
   the reviewed release scope has been curated separately onto
   `codex/safeguard-release-0.6.5` from canonical GitHub `main`.
2. GitHub `main` remains unprotected. The branch workflow is now exercised and
   green, but it is not yet a required merge check on `main`.
3. Staging needs a dedicated production-capable facilitator and its own
   authenticated facilitator credentials. The currently configured
   `x402.org` facilitator is development-only and is deliberately rejected
   under `APP_ENV=production`.
4. Production is running version 0.6.2 and returns `404` from `/readyz`. Railway
   reports no configured healthcheck path or timeout and allows ten restart
   retries.
5. The production deployment was uploaded from a local source context. Hosted
   commit stamping detects provenance drift but does not yet prove that deployed
   bytes are the exact wheel verified in CI.
6. Package signing, SBOM/provenance attestation, and an exercised automatic or
   one-command rollback remain future release-hardening work.
7. Full hosted HTTP/MCP, live-data, x402, persistence, command-center, and
   signed-in OpenAI, Claude, and Cursor acceptance has not run against an
   activated staged 0.6.5 deployment.

## Required release sequence

1. Keep draft pull request 12 on the curated release branch and require its
   clean-checkout CI, including the `--require-clean` release contract.
2. Protect GitHub `main`, require the new CI, and keep GitLab as a mirror unless
   it is intentionally restored as a release source.
3. Keep the physically separate Railway staging environment, service, volume,
   URLs, state paths, and security material isolated from production.
4. Provision dedicated staging upstream and production-capable facilitator
   credentials. Do not make routine staging depend on production credentials.
5. Redeploy the exact reviewed commit, require green `/readyz`, then run hosted
   HTTP/MCP, data, payment-boundary, command-center, persistence, and
   signed-client acceptance.
6. Record the last-known-good production deployment and test the rollback
   command before promotion.
7. Manually promote production, verify `/readyz`, version, commit SHA, manifest,
   all connector challenges, and representative free and paid boundaries, then
   watch logs and health before publishing registries and packages.

## Release boundary

The curated branch may be committed and reviewed, but it must not be merged or
published to registries until protected CI and isolated staging acceptance pass.
An isolated staging diagnostic was deployed on 2026-08-03; no production
deployment or configuration was changed. Ten production-derived values were
used only for the bounded authorized diagnostic, removed immediately afterward,
and never printed or written to disk. Production remained healthy on its
rolled-back 0.6.2 baseline.
