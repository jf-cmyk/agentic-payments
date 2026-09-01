# Marketplace metrics ingestion

The Product Usage Command Center can combine first-party referral attribution with
authorized marketplace-side snapshots. These sources stay separate: listing views,
installs, hosted invocations, and first-party calls do not share one denominator.

## Automated public listing health

Production checks each catalogued distribution URL once per day by default. The
collector stores only reachability, HTTP status, latency, content type, and check
time. It never stores response bodies and it labels every row
`metric_scope=listing_health`.

Listing health answers “can a user reach our listing?” It does **not** answer “how
many users viewed, installed, invoked, or paid?” The command center therefore keeps
`listing_health_platforms` separate from `performance_platforms`; a healthy listing
cannot satisfy the alert for missing marketplace performance data.

The production-safe defaults can be overridden with:

- `MARKETPLACE_LISTING_CHECKS_ENABLED`
- `MARKETPLACE_LISTING_CHECK_INITIAL_DELAY_SECONDS`
- `MARKETPLACE_LISTING_CHECK_INTERVAL_SECONDS` (minimum one hour)
- `MARKETPLACE_LISTING_CHECK_TIMEOUT_SECONDS` (maximum 30 seconds)

## Smithery runtime metrics

Set `SMITHERY_QUALIFIED_NAME` and store the Smithery API credential as
`SMITHERY_API_KEY`. The collector calls Smithery's runtime-logs API and retains only
aggregate invocation, success, failure, and tool counts. It does not store prompts,
arguments, responses, credentials, or invocation payloads.

```sh
SMITHERY_QUALIFIED_NAME='owner/server-name' \
python3 scripts/ingest_marketplace_metrics.py \
  --service-url https://mcp.blocksize.info --dry-run
```

Remove `--dry-run` after validating the shape. The production ingest also needs
`OBSERVABILITY_DASHBOARD_TOKEN`.

## Generic feeds and Pay.sh exports

`MARKETPLACE_METRICS_FEEDS_JSON` maps a platform id to an authorized JSON endpoint.
`MARKETPLACE_METRICS_TOKEN_ENVS_JSON` maps that id to the name of the environment
variable holding its bearer token. No public Pay.sh marketplace analytics API is
assumed; use an authorized JSON feed or offline export when one is available.

```sh
MARKETPLACE_METRICS_FEEDS_JSON='{"pay_sh":"https://example.invalid/metrics"}'
MARKETPLACE_METRICS_TOKEN_ENVS_JSON='{"pay_sh":"PAY_SH_METRICS_TOKEN"}'
PAY_SH_METRICS_TOKEN='stored-in-the-secret-manager'
```

Offline exports can be validated and ingested with `--input export.json`. The file
must map platform ids to metric objects, optionally below a `platforms` key.

## Operating checks

Run authorized performance-feed ingestion daily. A non-zero exit means a configured source or ingest failed. The command
prints platform ids, safe metric keys, and error classes only. After ingestion,
confirm the marketplace row in `/internal/observability` has a recent snapshot while
first-party calls remain separately attributed.

Until platform API credentials or reviewed exports are configured, the service will
honestly report public listing health and first-party referral activity while leaving
upstream performance coverage incomplete.
