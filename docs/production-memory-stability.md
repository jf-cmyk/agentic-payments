# Production memory incident and v0.6.20–0.6.21

## Evidence (2026-09-18 UTC)

Production reached approximately 24 GB before a container restart, then grew
again. A read-only SSH inspection found approximately 14 GB resident in Python
anonymous memory, only 18 open file descriptors, and a 2 GB pilot depth JSONL
history. CPU and aggregate request rates were low (3,109 HTTP requests in six
hours, roughly nine per minute); requests include health checks and discovery,
not just customers. Current-container OOM counters alone cannot establish the
previous container's termination cause.

The background RWA pilot loaded this entire file with read_text/splitlines,
decoded every full raw replay payload, and retained that collection in the
scheduler frame during its 30-minute sleep. This created both temporary full-file
copies and a large persistent Python object graph unrelated to paid API traffic.

A separate read-only production process tested the new reader against the actual
2,057,196,681-byte file: 1,014 reports became 895,977 bytes of compact statistics,
in 7.55 seconds, with peak process RSS of 85,000 KiB. This validates the reader;
it is not a substitute for observing the deployed service over multiple cycles.

## Fix and safety boundaries

- Stream legacy JSONL one record at a time, rejecting records over 16 MiB.
- Retain only the scalar inputs actually used by historical outlier statistics.
- Bound the rolling outlier baseline to the latest 4,096 valid report objects
  (over 85 days at the normal 30-minute cadence). Expose this scope in the report.
- Evaluate history and statistics in a worker thread and release the baseline
  before returning to the async scheduler. No large history remains in its frame.
- Preserve raw evidence files, authoritative SQLite monitoring history, payment
  verification, promotion restrictions, and all public coverage.
- Fail a capture on an oversized record rather than exhausting the API process.

Tests compare pre-/post-projection statistics, exercise both native and pool
volume inputs, prohibit whole-file reads, check bounded recent retention, and
cover malformed, incomplete, missing, and oversized history records.

## Production verification

Deploy through the existing GitHub-main pipeline. Check the exact deployed
commit, /health, /readyz, unpaid 402 responses, public catalog and MCP discovery.
Observe RSS and cgroup memory through multiple completed pilot cycles, not just
immediately after startup. Readiness alone does not prove memory stability.

No paid requests, wallet access, staging environment, or evidence deletion are
required for this release. Roll back through Railway to the prior known commit
if functional regression occurs, understanding that v0.6.19 retains this memory
defect and is not a durable memory fix.

## Capacity and visibility follow-up

Distinguish customer deliveries, payment prompts, crawler checks, and malicious
probes before sizing capacity. Track memory slope, restarts, CPU, concurrency,
latency and 5xx alongside paid deliveries, unique payers and settled revenue.
Only scale when sustained measured demand warrants it; extra RAM alone masks
this defect. Before adding replicas, review local SQLite/persistent-volume state,
payment idempotency and session routing so scaling cannot duplicate settlement.

After stability verification, refresh public registry release metadata and
test discovery-to-data journeys across covered assets. Do not advertise candidate
RWA feeds as production coverage or count payment prompts as revenue.

Raw depth and promotion history still grows on disk. A separate archival/retention
policy remains a storage follow-up; do not silently delete existing evidence.

## v0.6.20 production QA and v0.6.21 follow-up

v0.6.20 deployed at commit f9fc06ea94575d84e0bb3d35901ecc8bb395649e.
The first full pilot cycle finished at 05:44:28 UTC on September 18, recording
3/3 feeds and all 1,014 historical reports, with no automatic promotion.
At 05:45:06 UTC Python RSS was 372,948 KiB (about 382 MB), compared with
16,568,056 KiB (about 17 GB) before deployment. Container memory was about 2.52 GB,
including reclaimable file cache. This is early verification, not a long soak.

Cold-start archive scanning took 136 seconds inside the running service and
coincided with slow public requests and a 30-second preview timeout. After that
scan, all 13 unsigned API/discovery checks passed, with preview response time
0.225 seconds. Passing after warmup is insufficient to dismiss the cold-start issue.

v0.6.21 adds a disposable, bounded statistics sidecar next to the raw JSONL.
It is valid only when archive inode, byte size, and nanosecond modification time
match, with the same schema and report limit. Normal report persistence appends
raw evidence and atomically refreshes the compact sidecar. Cache hits avoid
opening the raw archive entirely. Changed/truncated/replaced archives and invalid
or oversized caches trigger a bounded-memory rebuild; cache-write failures do not
delete evidence or fail an otherwise valid capture. Unexpected concurrent appends
leave the old cache invalid rather than silently omitting records from a new one.

Seed the derived cache with the tested code before deploying v0.6.21, while the
current production history is already warm. Verify a cache hit and completed pilot
capture after deployment, plus repeated unsigned API checks during the job.
Neither release signs payments or creates a staging environment.
