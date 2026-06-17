# GitLab To Railway Deploy Setup

This repo can now deploy to the existing Railway service from GitLab CI.

## What the pipeline expects

Add these masked GitLab CI/CD variables at the project level:

- `RAILWAY_TOKEN`
- `RAILWAY_PROJECT_ID`
- `RAILWAY_ENVIRONMENT`
- `RAILWAY_SERVICE_NAME`

The pipeline uses Railway's project-token flow and runs:

```bash
railway up --ci --project "$RAILWAY_PROJECT_ID" --environment "$RAILWAY_ENVIRONMENT" --service "$RAILWAY_SERVICE_NAME"
```

## Recommended GitLab settings

- Keep the project private.
- Mark the Railway variables as masked and protected.
- Protect `main`.
- Only allow deployments from `main`.

## Railway setup

In Railway, keep the current service and environment. Create a project token for the environment that should receive GitLab deployments, then copy the project id, environment name, and service name into GitLab CI/CD variables.

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

1. Add the GitLab variables.
2. Push this pipeline to GitLab.
3. Watch the first `main` pipeline.
4. Verify the Railway deployment succeeds.
5. Disable GitHub auto-deploys in Railway after the GitLab deploy is proven.
