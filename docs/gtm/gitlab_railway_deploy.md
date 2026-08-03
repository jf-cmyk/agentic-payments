# GitLab To Railway Deploy Setup

This repo can now deploy to the existing Railway service from GitLab CI.

## What the pipeline expects

Configure these GitLab CI/CD variables:

- `STAGING_RAILWAY_TOKEN`
- `PRODUCTION_RAILWAY_TOKEN`
- `RAILWAY_PROJECT_ID`
- `RAILWAY_ENVIRONMENT`
- `RAILWAY_SERVICE_NAME`
- `PUBLIC_BASE_URL`
- `STAGING_RAILWAY_PROJECT_ID`
- `STAGING_RAILWAY_ENVIRONMENT`
- `STAGING_RAILWAY_SERVICE_NAME`
- `STAGING_BASE_URL`

Scope `STAGING_RAILWAY_TOKEN` only to the GitLab `staging` environment and
`PRODUCTION_RAILWAY_TOKEN` only to `production`; mark both masked and protected.
The remaining non-secret target identifiers may be project-level protected
variables. Do not configure either token with an all-environments (`*`) scope.

The staging Railway target and URL must be different from production. A `main`
release deploys and passes the exact-commit hosted audit in staging before the
production job is eligible to run. Production is audited again after deploy.

The pipeline uses Railway's project-token flow and runs:

```bash
railway up --ci --project "$RAILWAY_PROJECT_ID" --environment "$RAILWAY_ENVIRONMENT" --service "$RAILWAY_SERVICE_NAME"
```

## Recommended GitLab settings

- Keep the project private.
- Mark the Railway variables as masked and protected.
- Protect `main`.
- Only allow deployments from `main`.
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
  authority for the production service managed by this pipeline.
- Verify the active Railway service manifest uses `healthcheckPath=/readyz` on
  every controlled target. A source-connected service with no readiness health
  check is not eligible for cutover.
- Verify Railpack selects Python 3.12 from the tracked `.python-version`. CI and
  the deployed runtime must stay on the same tested interpreter line; Python
  3.13 is not certified by this release gate. Do not set a conflicting
  `RAILPACK_PYTHON_VERSION` override. The tracked Railway configuration also
  pins the published Railpack `v0.35.0` frontend image and its `RAILPACK`
  builder. Keep the leading `v`: Railway passes the configured version through
  to the GHCR frontend tag, and the unprefixed `0.35.0` tag does not exist.
- Keep `requirements.txt` dependency-only and generate it with
  `uv export --no-emit-project`. Railpack installs that file in a cached layer
  before copying the application source, so a local editable project entry
  would make package metadata depend on source files that are not present yet.
- Attach independent persistent `/data` volumes and configure all production
  secrets before the first staging candidate is deployed.
- Record the currently active, successful production deployment id before
  approving promotion and confirm it is still eligible for Railway rollback.

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

## Cutover checklist

1. Create and configure the distinct Railway staging environment/service.
2. Disable or pause Railway GitHub/repository auto-deploy on every production
   service; verify GitLab is the sole production deploy authority.
3. Verify each controlled Railway target reports `healthcheckPath=/readyz` in
   its active service manifest and builds with Python 3.12.
4. Add the staging and production GitLab variables.
5. Push the pipeline to GitLab and watch the first `main` pipeline.
6. Confirm staging passes `/readyz`, the exact-commit hosted audit, OAuth route
   calls, every x402 discovery resource, and MCP initialize.
7. Record the active successful production deployment id, then have an allowed
   release operator approve the blocking manual `deploy_production` job.
8. Confirm production passes the same hosted audit after deployment.

## Rollback procedure

Railway uses `/readyz` only while activating a deployment. A 200 response keeps
the previous deployment active until the candidate is ready, but Railway does
not continue monitoring that healthcheck after activation. If the broader
production hosted audit fails, treat the release as an incident:

1. Stop all further deployment and configuration changes. Preserve the failed
   job output and the candidate commit SHA.
2. In Railway, open the production service's deployment history and select
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
