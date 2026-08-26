# GitLab To Railway Deploy Setup

This repo can now deploy to the existing Railway service from GitLab CI.

## What the pipeline expects

Configure these GitLab CI/CD variables:

- `STAGING_RAILWAY_TOKEN`
- `PRODUCTION_RAILWAY_TOKEN`
- `RAILWAY_PROJECT_ID`
- `RAILWAY_ENVIRONMENT`
- `RAILWAY_SERVICE_NAME`
- `RAILWAY_SERVICE_ID`
- `PRODUCTION_VOLUME_INSTANCE_ID`
- `PRODUCTION_LEGACY_DRAIN_ATTESTATION`
- `PRODUCTION_LEGACY_DRAIN_ATTESTATION_SHA256`
- `PUBLIC_BASE_URL`
- `STAGING_RAILWAY_PROJECT_ID`
- `STAGING_RAILWAY_ENVIRONMENT`
- `STAGING_RAILWAY_SERVICE_NAME`
- `STAGING_BASE_URL`

Scope `STAGING_RAILWAY_TOKEN` only to the GitLab `staging` environment and
`PRODUCTION_RAILWAY_TOKEN` only to `production`; mark both masked and protected.
The remaining non-secret target identifiers may be project-level protected
variables. Do not configure either token with an all-environments (`*`) scope.
`RAILWAY_PROJECT_ID`, `RAILWAY_ENVIRONMENT`, and `RAILWAY_SERVICE_ID` must be
canonical Railway UUIDs. `RAILWAY_SERVICE_ID` is the production job's actual
service selector and the staging job's forbidden service; the staging job never
queries that production tuple with staging credentials. `RAILWAY_SERVICE_NAME`
is retained only for configuration compatibility and cross-environment sanity
checks.
`PRODUCTION_VOLUME_INSTANCE_ID` is the Railway volume-instance id mounted at
`/data`, not the parent volume id. The production helper checks that instance
has a `DAILY` backup schedule and a usable backup created within the last 26 hours
before it uploads any code.
`PRODUCTION_LEGACY_DRAIN_ATTESTATION` is a protected GitLab **file** variable;
its companion is the protected lowercase SHA-256 of the exact canonical file.
The proof contains identifiers and counts, not credentials. Retain both through
the locked and unlock phases. They are ignored only after the active baseline is
an unlocked 0.6.5-or-newer release. The job requires the path to be a readable
regular file and never prints or passes the JSON contents to Railway. GitLab
cannot classify the live prior before starting the job, so keep these variables
configured even after the one-time bridge; the release helper, not the shell
preflight, decides whether the proof applies.

The staging Railway target and URL must be different from production. A `main`
release deploys and passes the exact-commit hosted audit in staging before the
production job is eligible to run. Production is audited again after deploy.
The helper resolves the authorized job target to canonical Railway UUIDs and
compares it locally with the supplied canonical forbidden production tuple; it
never queries production using staging credentials. It rejects name-versus-id
aliases and compares normalized HTTPS origins rather than raw URL strings.

The pipeline uses Railway's project-token flow. Each job performs one detached
upload with a unique CI message, captures the returned deployment id, polls only
that id to `SUCCESS`, and then reads build logs positionally from that same id:

```bash
node scripts/deploy_railway_exact.mjs \
  --project "$RAILWAY_PROJECT_ID" \
  --environment "$RAILWAY_ENVIRONMENT" \
  --service "$RAILWAY_SERVICE_ID" \
  --message "$UNIQUE_CI_DEPLOYMENT_MESSAGE" \
  --mode production \
  --base-url "$PUBLIC_BASE_URL" \
  --volume-instance "$PRODUCTION_VOLUME_INSTANCE_ID" \
  --expected-commit "$CI_COMMIT_SHA" \
  --drain-attestation-file "$PRODUCTION_LEGACY_DRAIN_ATTESTATION" \
  --drain-attestation-sha256 "$PRODUCTION_LEGACY_DRAIN_ATTESTATION_SHA256" \
  --state-file .railway-release-production.json
```

### Legacy 0.6.2 transaction bridge: current P0 block

The checked-in bridge is fail-closed scaffolding, **not authorization to run the
first cutover today**. The live prior reports only version 0.6.2 and no commit;
the frozen `1791c5c9c46163cdcc1c9b69613f2855bee4d7a1` fixture proves selected
compatibility behavior, not equivalence to that live image. First bind the exact
prior deployment id, snapshot id and image digest, and inventory the real volume
schemas under a maintenance window. The attestation must bind the SHA-256 of a
separately preserved direct live schema-and-behavior audit artifact. The literal
`compatibilityFixtureCommit` is only the reviewed reference and never proves
live provenance by itself. Promotion remains blocked without that direct audit
or independently proven source-to-image provenance.

Railway has no atomic, reversible all-domain ingress pause. A safe external
mechanism must therefore be built and rehearsed before creating an attestation.
It must enumerate every attached custom and Railway service domain, positively
reject TCP proxies and pending/unverified domains, divert or reject **every
economic HTTP and connector route**, terminate or account for keep-alive/SSE and
in-flight requests, while retaining a private or tightly allowlisted control
path for `/health`, `/readyz`, and the hosted audit. A free-form operator
statement does not create or prove this freeze. Until the mechanism, readback,
external economic-route probes, session drain, control path, and restoration
procedure exist, production promotion is **NO-GO**. The deploy helper enforces
that decision before any Railway variable change or source upload; a
syntactically valid attestation cannot override the block. Removing that code
gate requires a reviewed implementation and rehearsal of the control path,
freeze/readback, and restoration contract described here.

Once that external control exists, hold it continuously across both phases:

1. Freeze every public domain and wait at least 60 seconds (twice the reviewed
   legacy upstream timeout). Positively observe zero in-flight requests.
2. Take two read-only logical-ledger samples at least five seconds apart for all
   three connector `daily_usage` tables and the x402 proof lifecycle. Hash the
   complete legacy business projections; record zero pending connector charges,
   zero pending/settled/settlement-unknown proofs, and released/finalized cache
   evidence. The two samples must be identical.
3. Canonicalize the attestation (sorted object keys, compact JSON, exactly one
   trailing newline), compute SHA-256, and place file/digest in the protected
   GitLab variables. Its target includes every active domain and exact Railway
   project/environment/service; its prior includes exact deployment, image and
   snapshot; its freeze evidence includes mechanism/change reference, timestamps,
   two samples and logical fingerprints.
4. Phase A uploads the exact 0.6.5 commit with
   `LEGACY_TRANSACTION_BRIDGE_LOCK=true`. New x402 proof use, credit claims,
   connector credit spending and balance-induced ledger writes return a
   no-store maintenance error. Unsigned x402 discovery remains available on the
   allowlisted control path. Acceptance re-counts the volume directly.
5. Re-run the **same exact commit** as Phase B. The active Phase-A prior must
   report the same full commit SHA and locked drained readiness. Phase B sets the
   lock false without a config-only deploy and revalidates unchanged counts.
6. Only after Phase-B acceptance, explicitly restore every external domain,
   verify restoration readback and public probes, then end the maintenance
   window. The scripts deliberately do not unfreeze an external edge they do not
   control.

The current GitLab pipeline does **not** yet orchestrate those two production
runs safely. A normal pipeline performs staging once and exposes one manual
production job; retrying that job is not an approved Phase-B workflow, and a new
pipeline may redeploy staging, change the commit, or outlive the attestation.
Before executing Phase A, add a reviewed, explicit second manual Phase-B job
that needs the accepted Phase-A state artifact, refuses a different
`CI_COMMIT_SHA`, performs no staging upload, and completes before the same
freeze expires. Until that job and an external unfreeze/readback job exist,
both phases remain operationally blocked; do not approximate them with a job
retry or an ad-hoc Railway deployment.

If Phase A reaches Railway runtime and is not accepted, image rollback alone is
not proof of recovery because the candidate may have migrated or written the
shared volume. Automatic recovery leaves the bridge variable fail-locked and
returns an incident/manual state; independently compare logical fingerprints
and counts (or restore the verified backup) before declaring recovery. Old
0.6.2 ignores `LEGACY_TRANSACTION_BRIDGE_LOCK`.

The helper resolves project/environment/service names to canonical ids, refuses
pre-existing in-flight releases, fails closed, and never blindly retries an
upload. Before uploading production it records the exact prior active image,
health, rollback eligibility, deployment list, and backup evidence. It embeds
the prior id in the unique Railway CLI message, which provides a recovery trail
even if the runner loses its local state artifact.

The job writes its state atomically with mode `0600`. Its `after_script` then
cancels a queued/building candidate, stops an inactive staging candidate, or
rolls any deploying/active but unaccepted production candidate back to the recorded prior
image. It binds the newly created rollback deployment id, requires the recorded
image digest to be the sole active deployment, and verifies the previous public
health before returning. A release is marked accepted only after exact-id
Railpack inspection, the full hosted audit, and one final `/readyz` response
containing the exact 40-character commit SHA.

## Recommended GitLab settings

- Keep the project private.
- Mark the Railway variables as masked and protected.
- Protect `main`.
- Only allow deployments from `main`.
- Use GitLab Runner 17.1.0 or later. Normal cancellation must run
  `after_script`; do not use force-cancel for a Railway release job.
- Protect the GitLab `production` environment and restrict **Allowed to deploy**
  to the designated release operators. The production job is a blocking manual
  action after staging; do not convert it back to an automatic job.

## Hard pre-deploy prerequisites

Do not enable or push the GitLab production pipeline until all of these are true:

- Create a distinct Railway staging environment and service. It must not reuse
  the production project/environment/service tuple or public URL.
- Disable or pause Railway GitHub/repository auto-deploy on every production
  service, including `anthropic-mcp-beta`, unless that service has its own
  equivalent staging-and-smoke promotion gate. GitLab must be the sole deploy
  authority for the production service managed by this pipeline. The release
  helper queries Railway before every upload and rejects a target service with
  any repository deployment trigger or a truncated/malformed trigger response.
- Verify the candidate Railway configuration uses `healthcheckPath=/readyz` on
  every controlled target. The exact candidate manifest is checked before
  acceptance. The known legacy production 0.6.2 deployment is the one-time
  exception because it predates `/readyz`; no release after 0.6.5 may use that
  exception.
- Verify Railpack selects Python 3.12 from the tracked `.python-version`. CI and
  the deployed runtime must stay on the same tested interpreter line; Python
  3.13 is not certified by this release gate. Do not set a conflicting
  `RAILPACK_PYTHON_VERSION` override. The tracked Railway configuration also
  pins Railpack with Railway's documented bare semantic-version syntax:
  `railpackVersion = "0.36.2"`. Railway previously honored a tracked
  `v0.35.0` value, then silently resolved the same manifest to 0.36.2, so the
  manifest alone is not proof of the selected engine. Each deployment job must
  inspect the exact deployment id returned by `railway up` and require its
  driver, prepare step, banner, and frontend log markers all to report 0.36.2.
  Never use `railway logs --latest` in a promotion gate.
- Keep `requirements.txt` dependency-only and generate it with
  `uv export --no-emit-project`. Railpack installs that file in a cached layer
  before copying the application source, so a local editable project entry
  would make package metadata depend on source files that are not present yet.
- Attach independent persistent `/data` volumes and configure dedicated,
  environment-scoped staging credentials before routine acceptance begins.
  Production-derived values may be used only for an explicitly authorized,
  bounded diagnostic; never write them to logs or local disk, never retain them
  beyond that window, and remove them immediately when the diagnostic ends.
- Record the currently active, successful production deployment id before
  approving promotion and confirm it is still eligible for Railway rollback.
- Keep the Railway deployment history below 998 rows before starting a release.
  The helper reserves one row for the candidate and one for an emergency
  rollback so the CLI's 1,000-row limit cannot truncate its causal baseline.
- Require the existing production release to pass `/readyz` before it can be a
  rollback baseline. The sole migration exception is the known legacy 0.6.2
  release, whose `/readyz` route does not exist and returns 404; once 0.6.5 is
  accepted, all future rollback baselines must be dependency-ready.
- Confirm the `/data` volume has a successful backup less than 26 hours old and
  an enabled `DAILY` schedule. Railway image rollback does not restore volume data, so
  schema changes must be additive and backward compatible.

### RWA v1 volume migration

The first 0.6.5 start automatically migrates only the exact legacy v0.6.2
`rwa_observations` schema to schema v3. The migration runs under one SQLite
`BEGIN IMMEDIATE` transaction, preserves every legacy column and row, adds no
destructive DDL, validates legacy JSON and content hashes, and runs
`PRAGMA integrity_check` before stamping `schema_version=3` and
`migration_required=false`. A corrupt row, unexpected table or column, invalid
metadata state, incompatible index, or failed integrity check rolls the entire
migration back and fails startup. Never bypass that refusal by editing
`rwa_store_metadata` manually.

Before production, obtain an application-consistent copy of the real production
`/data` in a new isolated rehearsal volume and record the legacy RWA row count,
file size, and `PRAGMA integrity_check` result. Railway's public backup-restore
operation cannot clone a backup across environments: it targets the source
volume instance and stages a replacement on that same service. Never invoke it
on production for this rehearsal. Use a Railway Support-assisted backup clone,
or create SQLite online backups under a controlled production maintenance
window and import those into a fresh non-production volume. A recursive copy of
live SQLite, WAL, and SHM files is not an application-consistent snapshot.

Require candidate `/readyz` to report the RWA store ready at schema v3, confirm
the same row count and another successful integrity check, and record migration
duration and peak memory inside the 180-second health-check window. Then reopen
a disposable copy with a source-equivalent v0.6.2 build. The legacy named-column
reads and writes remain compatible with the additive v3 table; a later candidate
start safely backfills any rows written during a rollback window. The current
production Railpack digest is Railway-internal and cannot be moved to another
service as a pullable image, so future releases must publish an immutable OCI
image when exact cross-environment image rehearsal is required.

The 2026-08-13 authority audit found that the primary production and staging
MCP services had no repository triggers. The separate production
`anthropic-mcp-beta` service was still connected to GitHub `main`; its
auto-deploy toggle was then paused without unlinking the repository or
restarting its running deployment. Require `serviceInstanceAutoDeployStatus`
to remain `enabled=false` immediately before every release push. Do not
re-enable it until Anthropic receives an equivalent staging, acceptance, and
recovery gate.

## Railway setup

Create environment-scoped project tokens for the distinct staging and
production targets, then copy the project ids, environment names, and service
names into their matching GitLab CI/CD variables.
Never expose the production token to the staging job: the pipeline maps
`STAGING_RAILWAY_TOKEN` and `PRODUCTION_RAILWAY_TOKEN` to `RAILWAY_TOKEN` only
inside their respective jobs.

Set `APP_ENV=production` on both production and staging Railway services. This
does not rename the Railway environment; it applies the production security and
durability checks to every hosted release candidate. `/readyz` deliberately
rejects a hosted service that omits this setting. Attach a persistent volume at
`/data` to both targets and keep each database and OAuth storage path distinct.
Set the staging service's own `PUBLIC_BASE_URL` to the same value as the GitLab
`STAGING_BASE_URL`; set the production service's value to the GitLab
`PUBLIC_BASE_URL`. The hosted audit rejects manifest, x402, or OAuth URLs that
point at another origin, so staging must never inherit the production URL.
Set the connector URLs on staging to
`$STAGING_BASE_URL/anthropic/mcp`, `$STAGING_BASE_URL/cursor/mcp`, and
`$STAGING_BASE_URL/openai/mcp`; use the corresponding `$PUBLIC_BASE_URL` URLs
in production. Hosted readiness rejects connector URLs on another origin.

The 2026-08-03 staging logs observed direct peer `100.64.0.2`; the current
bounded operational assumption is `FORWARDED_ALLOW_IPS=100.64.0.0/10`. Railway
documents `X-Real-IP` but does not publish a stable proxy source-CIDR contract.
The application trusts one validated `X-Real-IP` only in Railway mode and only
from the configured raw peers; otherwise it fails closed to the direct address.
Never use `*` or a broad supernet, and revalidate the configured range in
staging before production promotion.

For the stream-backed market-data products, set these variables on every
Railway service that runs `python -m src.resource_server`:

```text
BLOCKSIZE_STREAM_CACHE_ENABLED=true
BLOCKSIZE_24H_CACHE_TICKERS=BTCUSD,ETHUSD,SOLUSD,JUPUSD,PYTHUSD
BLOCKSIZE_STATE_CACHE_MODE=all
BLOCKSIZE_STATE_CACHE_MAX_TICKERS=250
BLOCKSIZE_STREAM_CACHE_TTL_SECONDS=3600
BLOCKSIZE_STREAM_CACHE_RECONNECT_SECONDS=5
```

## Staging diagnostic record — 2026-08-03

- Exact candidate: `8871a5f06d6b2ed162f5f74d79f00ab3af7b1f65`.
- Exact-head GitHub Actions run `30848975204` passed the clean-checkout tests,
  dependency audit, deterministic artifact checks, and installed-release smoke.
- Railway deployment `339901eb-b448-4a01-813f-ce92513b975b` used the tracked
  historical Railpack `v0.35.0` manifest and actually resolved all four build
  markers to 0.35.0 at that time. It installed the dependency-only
  requirements, copied the full source, built the image, mounted the isolated
  staging volume, and completed application startup on port 8080. This dated
  observation is not the current 0.36.2 release target.
- Its Railway health probes reached the container from direct peer
  `100.64.0.2`; this is the dated evidence for the current proxy allowlist, not
  a published Railway source-CIDR guarantee.
- `/readyz` remained `503`, so Railway correctly refused activation. The
  production-ineligible public development facilitator is a known hard blocker;
  the downstream hosted audit and signed-client checks did not run.
- The ten temporarily authorized production-derived values were removed
  immediately after the readiness window, the staging ticker configuration was
  restored, and production remained healthy and unchanged on 0.6.2.

This record is diagnostic evidence, not staging acceptance. Routine acceptance
requires dedicated staging credentials and a production-capable authenticated
facilitator.

## Cutover checklist

1. Create and configure the distinct Railway staging environment/service.
2. Disable or pause Railway GitHub/repository auto-deploy on every production
   service; verify GitLab is the sole production deploy authority. From the
   staging upload until production acceptance or completed recovery, establish
   a deploy freeze: no Railway UI redeploys, variable-triggered deploys, API
   deploys, or second CI release may run against either controlled target.
3. Verify each controlled Railway target's candidate configuration declares
   `healthcheckPath=/readyz` and builds with Python 3.12; the gate checks the
   exact deployed manifest before acceptance.
4. Add the staging and production GitLab variables.
5. Push the pipeline to GitLab and watch the first `main` pipeline.
6. Confirm the staging job binds to the deployment id returned by its one
   detached upload, reaches `SUCCESS`, proves all four Railpack 0.36.2 log
   markers from that exact id, then passes `/readyz`, the exact-commit hosted
   audit, OAuth route calls, every x402 discovery resource, and MCP initialize.
7. Record the active successful production deployment id, then have an allowed
   release operator approve the blocking manual `deploy_production` job.
8. Confirm production performs one upload only, binds every check to its exact
   deployment id, and passes the same build and hosted audits. A failed or
   timed-out job must leave `after_script` enough time to cancel the exact
   non-running candidate or roll the active candidate back to the recorded
   prior image. Never blindly retry an upload because the prior request may
   already have reached Railway.

The deployment jobs reserve separate runner budgets: 25 minutes for the main
script and 20 minutes for recovery inside a 50-minute job, leaving five minutes
of runner-level margin. The hosted audit has
its own five-minute deadline, deployment polling is bounded, and exact log
retrieval is bounded. Do not reduce these budgets without proving rollback still
fits. GitLab normal cancellation and configured job timeouts run `after_script`;
force-cancel, runner loss, or infrastructure termination cannot be made atomic
by a repository script. If any of those occurs, freeze deploys and run the
checked-in recovery helper from a trusted shell with the saved state artifact:

```bash
RAILWAY_TOKEN="$PRODUCTION_RAILWAY_TOKEN" \
  node scripts/recover_railway_release.mjs \
  --state-file .railway-release-production.json \
  --expected-commit "$CI_COMMIT_SHA" \
  --drain-attestation-sha256 "$PRODUCTION_LEGACY_DRAIN_ATTESTATION_SHA256"
```

If the artifact was lost, locate the unique deployment message
`bsmcp:production:<pipeline>:<job>:<sha>:prev:<prior-id>` in Railway, verify the
target tuple and prior id, and perform the documented manual rollback. Do not
stop an active volume-owning production candidate before starting rollback.

## Rollback procedure

Railway uses `/readyz` only while activating a deployment. A 200 response keeps
the previous deployment active until the candidate is ready, but Railway does
not continue monitoring that healthcheck after activation. The CI `after_script`
automatically attempts the exact recorded rollback if the broader audit fails.
If automatic recovery cannot positively prove the prior image and health, treat
the release as an incident:

1. Stop all further deployment and configuration changes. Preserve the failed
   job output and the candidate commit SHA.
2. First run `scripts/recover_railway_release.mjs` with the saved job artifact.
   If it cannot complete, open Railway's production deployment history and select
   **Rollback** on the previously recorded successful deployment id. Confirm
   that the id, service, environment, image, and stored custom variables match
   the pre-release record before approving the rollback.
3. Verify `/health`, the expected previous version/commit, the public MCP
   initialize flow, OAuth challenges, and a read-only data request. Re-run the
   hosted audit against the restored origin.
4. Verify the SQLite stores and mounted `/data` volume separately. Railway's
   image/variable rollback does **not** roll back volume contents. Database
   changes must remain backward compatible and material migrations require a
   tested backup/restore procedure before promotion.
5. Keep production frozen, document the failure, and repair the candidate in a
   new commit that must pass staging again. Never redeploy the failed candidate
   as the rollback action.
