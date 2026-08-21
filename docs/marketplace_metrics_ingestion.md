# Marketplace metrics ingestion

The Product Usage Command Center can combine first-party request attribution with
authorized marketplace-side snapshots. Local referrals alone do not prove listing
views, installs, hosted calls, or marketplace conversion, so external metrics remain
explicitly unconfigured until a snapshot is ingested.

## Configure feeds

Set `MARKETPLACE_METRICS_FEEDS_JSON` to a JSON object whose keys match an onboarded
platform id, such as `pay_sh` or `smithery`, and whose values are authorized JSON
metrics endpoints. If a feed needs authentication, store its token in a separate
secret environment variable and map the platform id to that variable's name with
`MARKETPLACE_METRICS_TOKEN_ENVS_JSON`.

Example configuration (the token value itself is not shown):

```sh
MARKETPLACE_METRICS_FEEDS_JSON='{"pay_sh":"https://example.invalid/metrics"}'
MARKETPLACE_METRICS_TOKEN_ENVS_JSON='{"pay_sh":"PAY_SH_METRICS_TOKEN"}'
PAY_SH_METRICS_TOKEN='replace-in-secret-store'
OBSERVABILITY_DASHBOARD_TOKEN='replace-in-secret-store'
```

## Validate and ingest

Validate response shapes and the safe metric keys without posting a snapshot:

```sh
python3 scripts/ingest_marketplace_metrics.py --dry-run
```

Ingest into the configured public service:

```sh
python3 scripts/ingest_marketplace_metrics.py --service-url https://mcp.blocksize.info
```

An offline marketplace export can be ingested with `--input export.json`. The file
must map platform ids to metric objects, optionally below a top-level `platforms`
key. Secret-like fields are removed and metric objects are bounded before posting.

Run the collector on the same cadence as the source data (daily is sufficient for
most marketplace reporting). Alert on a non-zero exit code. The script reports only
platform ids, safe metric keys, and error classes; it never prints feed or dashboard
tokens.

## Acceptance checks

After ingestion, confirm that the platform row in
`/internal/observability/command-center` changes
from `no_local_activity` or `watching` to `configured`, displays a recent snapshot,
and retains first-party calls separately. Never add marketplace views or installs to
first-party request counts; they have different definitions and evidence sources.
