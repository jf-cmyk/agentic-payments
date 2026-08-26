import { spawn } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
import { open, readFile, rename, unlink } from "node:fs/promises";
import { dirname } from "node:path";

export const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
export const NONTERMINAL_STATUSES = new Set([
  "INITIALIZING",
  "QUEUED",
  "WAITING",
  "NEEDS_APPROVAL",
  "BUILDING",
  "DEPLOYING",
  "REMOVING",
  "SLEEPING",
]);
export const PRE_RUNTIME_STATUSES = new Set([
  "INITIALIZING",
  "QUEUED",
  "WAITING",
  "NEEDS_APPROVAL",
  "BUILDING",
]);
export const FAILED_STATUSES = new Set([
  "FAILED",
  "CRASHED",
  "REMOVED",
  "SKIPPED",
  "CANCELED",
  "CANCELLED",
]);

const MAX_CAPTURE_BYTES = 16 * 1024 * 1024;
const DEPLOYMENT_LIST_STATUSES = new Set([
  ...NONTERMINAL_STATUSES,
  ...FAILED_STATUSES,
  "SUCCESS",
]);

export function fail(message) {
  throw new Error(message);
}

const LEGACY_BRIDGE_KIND = "blocksize_legacy_transaction_drain_v1";
const LEGACY_BRIDGE_CONNECTORS = ["anthropic", "cursor", "openai"];
const LEGACY_BRIDGE_PAYMENT_COUNT_KEYS = [
  "total",
  "pending",
  "settled",
  "settlement_unknown",
  "released",
  "finalized",
  "unknown",
  "finalized_cached_responses",
  "recent_finalized_cached_responses",
];
const LEGACY_USAGE_SAMPLE_KEYS = ["row_count", "credits_spent_total"];

function requireExactObjectKeys(value, keys, label) {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    fail(`${label} must be an object`);
  }
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (
    actual.length !== expected.length
    || actual.some((key, index) => key !== expected[index])
  ) {
    fail(`${label} has an unexpected schema`);
  }
  return value;
}

function canonicalJson(value) {
  if (Array.isArray(value)) return `[${value.map(canonicalJson).join(",")}]`;
  if (value && typeof value === "object") {
    return `{${Object.keys(value).sort().map(
      (key) => `${JSON.stringify(key)}:${canonicalJson(value[key])}`,
    ).join(",")}}`;
  }
  return JSON.stringify(value);
}

function requireNonnegativeInteger(value, label) {
  if (!Number.isSafeInteger(value) || value < 0) {
    fail(`${label} must be a non-negative integer`);
  }
  return value;
}

function requireSha256(value, label) {
  if (!/^[0-9a-f]{64}$/.test(value || "")) fail(`${label} must be a lowercase SHA-256`);
  return value;
}

function requireDomainInventory(value, label) {
  const domains = requireExactObjectKeys(value, ["custom", "service"], label);
  for (const kind of ["custom", "service"]) {
    if (
      !Array.isArray(domains[kind])
      || domains[kind].some(
        (domain) => (
          typeof domain !== "string"
          || domain !== domain.toLowerCase()
          || !/^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$/.test(domain)
        ),
      )
      || new Set(domains[kind]).size !== domains[kind].length
      || canonicalJson(domains[kind]) !== canonicalJson([...domains[kind]].sort())
    ) {
      fail(`${label} has invalid or non-canonical ${kind} domains`);
    }
  }
  if (domains.custom.length + domains.service.length === 0) {
    fail(`${label} contains no active domains`);
  }
  return domains;
}

export function legacyBridgeRequiredForVersion(version) {
  return String(version || "").trim() === "0.6.2";
}

export function versionAtLeast065(version) {
  const match = /^(\d+)\.(\d+)\.(\d+)$/.exec(String(version || "").trim());
  if (!match) return false;
  const parts = match.slice(1).map(Number);
  return (
    parts[0] > 0
    || parts[1] > 6
    || (parts[1] === 6 && parts[2] >= 5)
  );
}

export function expectedLegacyBridgePhase(prior) {
  const version = prior?.health?.version;
  if (legacyBridgeRequiredForVersion(version)) return "legacy_lock";
  if (!versionAtLeast065(version)) {
    fail("production prior version is outside the supported release bridge");
  }
  const priorBridge = prior?.readiness?.legacyTransactionBridge;
  if (
    !priorBridge
    || priorBridge.configuration_valid !== true
    || typeof priorBridge.economic_writes_locked !== "boolean"
  ) {
    fail("v0.6.5-or-newer production prior has no valid transaction bridge status");
  }
  return priorBridge.economic_writes_locked ? "bridge_unlock" : null;
}

export function requireExecutableLegacyBridgePhase(phase) {
  if (phase === "legacy_lock" || phase === "bridge_unlock") {
    fail(
      `production ${phase} transaction bridge phase is operationally blocked before mutation: `
      + "no reviewed all-domain ingress freeze/private validation control path exists",
    );
  }
  if (phase !== null) {
    fail("unsupported legacy transaction bridge phase");
  }
  return phase;
}

export function validateLegacyBridgeState(
  bridge,
  {
    target,
    targetDomains = null,
    prior,
    expectedCommit,
    expectedDigest,
    now = Date.now(),
    requireUnexpired = true,
  },
) {
  requireExactObjectKeys(
    bridge,
    ["required", "phase", "sourceSha256", "attestation"],
    "legacy transaction bridge state",
  );
  if (
    bridge.required !== true
    || !["legacy_lock", "bridge_unlock"].includes(bridge.phase)
    || !/^[0-9a-f]{64}$/.test(bridge.sourceSha256 || "")
  ) {
    fail("legacy transaction bridge state is not cryptographically bound");
  }
  if (
    !/^[0-9a-f]{64}$/.test(expectedDigest || "")
    || bridge.sourceSha256 !== expectedDigest
  ) {
    fail("legacy drain attestation digest is absent or does not match release state");
  }
  const attestation = requireExactObjectKeys(
    bridge.attestation,
    [
      "schemaVersion",
      "kind",
      "attestedBy",
      "candidateCommit",
      "target",
      "prior",
      "freeze",
      "directCounts",
    ],
    "legacy drain attestation",
  );
  const attestationSha256 = createHash("sha256")
    .update(`${canonicalJson(attestation)}\n`)
    .digest("hex");
  if (attestationSha256 !== bridge.sourceSha256) {
    fail("legacy drain attestation content does not match its release-state SHA-256");
  }
  if (
    attestation.schemaVersion !== 1
    || attestation.kind !== LEGACY_BRIDGE_KIND
    || typeof attestation.attestedBy !== "string"
    || !attestation.attestedBy.trim()
    || attestation.attestedBy.length > 120
    || attestation.candidateCommit !== expectedCommit
  ) {
    fail("legacy drain attestation identity or candidate binding is invalid");
  }
  const attestedTarget = requireExactObjectKeys(
    attestation.target,
    ["project", "environment", "service", "domains"],
    "legacy drain attestation target",
  );
  if (
    attestedTarget.project !== target.project
    || attestedTarget.environment !== target.environment
    || attestedTarget.service !== target.service
  ) {
    fail("legacy drain attestation is bound to a different Railway target");
  }
  const attestedDomains = requireDomainInventory(
    attestedTarget.domains,
    "legacy drain attestation domain inventory",
  );
  if (
    targetDomains
    && canonicalJson(attestedDomains)
      !== canonicalJson(requireDomainInventory(targetDomains, "live Railway domain inventory"))
  ) {
    fail("legacy drain attestation does not cover every active Railway domain");
  }
  const attestedPrior = requireExactObjectKeys(
    attestation.prior,
    [
      "deploymentId",
      "imageDigest",
      "snapshotId",
      "version",
      "compatibilityFixtureCommit",
      "liveSchemaBehaviorAuditSha256",
    ],
    "legacy drain attestation prior",
  );
  if (
    attestedPrior.compatibilityFixtureCommit
      !== "1791c5c9c46163cdcc1c9b69613f2855bee4d7a1"
  ) {
    fail("legacy drain attestation does not name the reviewed compatibility fixture");
  }
  requireSha256(
    attestedPrior.liveSchemaBehaviorAuditSha256,
    "direct live schema and behavior audit digest",
  );
  if (!legacyBridgeRequiredForVersion(attestedPrior.version)) {
    fail("legacy drain attestation is bound to a different prior release");
  }
  if (bridge.phase === "legacy_lock") {
    if (
      attestedPrior.deploymentId !== prior.id
      || attestedPrior.version !== prior.health?.version
      || attestedPrior.imageDigest !== prior.imageDigest
      || attestedPrior.snapshotId !== prior.snapshotId
    ) {
      fail("legacy drain attestation is bound to a different prior release");
    }
  } else if (
    !versionAtLeast065(prior.health?.version)
    || prior.readiness?.legacyTransactionBridge?.economic_writes_locked !== true
    || prior.health?.commitSha !== expectedCommit
    || prior.readiness?.commitSha !== expectedCommit
    || prior.readiness?.version !== prior.health?.version
    || !prior.readiness?.legacyTransactionBridge?.direct_counts
  ) {
    fail("bridge unlock requires the same exact locked v0.6.5-or-newer artifact");
  }
  const freeze = requireExactObjectKeys(
    attestation.freeze,
    [
      "ingressFrozen",
      "economicWritesFrozen",
      "startedAt",
      "drainWaitCompletedAt",
      "minimumDrainSeconds",
      "expiresAt",
      "enforcement",
      "stableLedgerSamples",
    ],
    "legacy drain attestation freeze",
  );
  const startedAt = Date.parse(freeze.startedAt || "");
  const drainWaitCompletedAt = Date.parse(freeze.drainWaitCompletedAt || "");
  const expiresAt = Date.parse(freeze.expiresAt || "");
  if (
    freeze.ingressFrozen !== true
    || freeze.economicWritesFrozen !== true
    || !Number.isFinite(startedAt)
    || !Number.isFinite(drainWaitCompletedAt)
    || !Number.isFinite(expiresAt)
    || startedAt > now + 120_000
    || freeze.minimumDrainSeconds !== 60
    || drainWaitCompletedAt < startedAt + freeze.minimumDrainSeconds * 1000
    || drainWaitCompletedAt > now + 120_000
    || expiresAt <= startedAt
    || (requireUnexpired && expiresAt <= now)
  ) {
    fail("legacy drain attestation does not prove an active ingress and economic-write freeze");
  }
  const enforcement = requireExactObjectKeys(
    freeze.enforcement,
    ["mechanism", "changeReference", "zeroInFlightObservedAt"],
    "legacy drain ingress enforcement",
  );
  const zeroInFlightObservedAt = Date.parse(enforcement.zeroInFlightObservedAt || "");
  if (
    enforcement.mechanism !== "all_domain_ingress_block"
    || typeof enforcement.changeReference !== "string"
    || !enforcement.changeReference.trim()
    || enforcement.changeReference.length > 200
    || !Number.isFinite(zeroInFlightObservedAt)
    || zeroInFlightObservedAt < drainWaitCompletedAt
    || zeroInFlightObservedAt > now + 120_000
  ) {
    fail("legacy drain attestation does not prove zero in-flight work on all domains");
  }
  if (!Array.isArray(freeze.stableLedgerSamples) || freeze.stableLedgerSamples.length !== 2) {
    fail("legacy drain attestation must contain exactly two stable ledger samples");
  }
  let previousSampleAt = null;
  let previousUsage = null;
  let previousPayment = null;
  for (const [index, sample] of freeze.stableLedgerSamples.entries()) {
    requireExactObjectKeys(
      sample,
      ["sampledAt", "databaseFingerprints", "connector_daily_usage", "payment_proofs"],
      `legacy drain sample ${index + 1}`,
    );
    const sampledAt = Date.parse(sample.sampledAt || "");
    if (
      !Number.isFinite(sampledAt)
      || sampledAt < zeroInFlightObservedAt
      || sampledAt > now + 120_000
      || (previousSampleAt != null && sampledAt < previousSampleAt + 5_000)
    ) {
      fail("legacy drain samples do not prove an ordered stable observation window");
    }
    const fingerprints = requireExactObjectKeys(
      sample.databaseFingerprints,
      ["creditDb", "connectors"],
      `legacy drain sample ${index + 1} database fingerprints`,
    );
    requireSha256(fingerprints.creditDb, "legacy credit DB fingerprint");
    const connectorFingerprints = requireExactObjectKeys(
      fingerprints.connectors,
      LEGACY_BRIDGE_CONNECTORS,
      `legacy drain sample ${index + 1} connector DB fingerprints`,
    );
    for (const connector of LEGACY_BRIDGE_CONNECTORS) {
      requireSha256(connectorFingerprints[connector], `${connector} DB fingerprint`);
    }
    const usage = requireExactObjectKeys(
      sample.connector_daily_usage,
      LEGACY_BRIDGE_CONNECTORS,
      `legacy drain sample ${index + 1} connector usage`,
    );
    for (const connector of LEGACY_BRIDGE_CONNECTORS) {
      const connectorUsage = requireExactObjectKeys(
        usage[connector],
        LEGACY_USAGE_SAMPLE_KEYS,
        `legacy drain sample ${index + 1} ${connector} usage`,
      );
      for (const key of LEGACY_USAGE_SAMPLE_KEYS) {
        requireNonnegativeInteger(connectorUsage[key], `${connector}.${key}`);
      }
    }
    const sampledPayment = requireExactObjectKeys(
      sample.payment_proofs,
      LEGACY_BRIDGE_PAYMENT_COUNT_KEYS,
      `legacy drain sample ${index + 1} payment proofs`,
    );
    for (const key of LEGACY_BRIDGE_PAYMENT_COUNT_KEYS) {
      requireNonnegativeInteger(sampledPayment[key], `sample.payment_proofs.${key}`);
    }
    if (
      previousUsage != null
      && (
        canonicalJson(fingerprints) !== canonicalJson(
          freeze.stableLedgerSamples[index - 1].databaseFingerprints,
        )
        ||
        canonicalJson(usage) !== canonicalJson(previousUsage)
        || canonicalJson(sampledPayment) !== canonicalJson(previousPayment)
      )
    ) {
      fail("legacy drain ledger samples changed while ingress was frozen");
    }
    previousSampleAt = sampledAt;
    previousUsage = usage;
    previousPayment = sampledPayment;
  }
  const directCounts = requireExactObjectKeys(
    attestation.directCounts,
    [
      "connector_pending_charges",
      "connector_pending_charges_by_connector",
      "payment_proofs",
    ],
    "legacy drain attestation direct counts",
  );
  if (requireNonnegativeInteger(
    directCounts.connector_pending_charges,
    "connector pending charge count",
  ) !== 0) {
    fail("legacy drain attestation has connector pending charges");
  }
  const connectorCounts = requireExactObjectKeys(
    directCounts.connector_pending_charges_by_connector,
    LEGACY_BRIDGE_CONNECTORS,
    "legacy drain attestation connector counts",
  );
  for (const connector of LEGACY_BRIDGE_CONNECTORS) {
    if (requireNonnegativeInteger(connectorCounts[connector], `${connector} pending charges`) !== 0) {
      fail("legacy drain attestation has connector pending charges");
    }
  }
  const paymentCounts = requireExactObjectKeys(
    directCounts.payment_proofs,
    LEGACY_BRIDGE_PAYMENT_COUNT_KEYS,
    "legacy drain attestation payment proof counts",
  );
  if (canonicalJson(previousPayment) !== canonicalJson(paymentCounts)) {
    fail("legacy drain stable samples do not match the attested direct payment counts");
  }
  for (const key of LEGACY_BRIDGE_PAYMENT_COUNT_KEYS) {
    requireNonnegativeInteger(paymentCounts[key], `payment_proofs.${key}`);
  }
  if (
    paymentCounts.pending !== 0
    || paymentCounts.settled !== 0
    || paymentCounts.settlement_unknown !== 0
    || paymentCounts.unknown !== 0
    || paymentCounts.total !== paymentCounts.released + paymentCounts.finalized
    || paymentCounts.finalized_cached_responses > paymentCounts.finalized
    || paymentCounts.recent_finalized_cached_responses
      > paymentCounts.finalized_cached_responses
  ) {
    fail("legacy drain attestation payment proof counts are not rollback-safe");
  }
  return attestation;
}

export async function loadLegacyDrainAttestation(
  path,
  expectedDigest,
  context,
) {
  if (!path || !/^[0-9a-f]{64}$/.test(expectedDigest || "")) {
    fail("legacy production baseline requires an attestation file and protected SHA-256");
  }
  const bytes = await readFile(path);
  if (bytes.length === 0 || bytes.length > 64 * 1024) {
    fail("legacy drain attestation file has an invalid size");
  }
  const sourceSha256 = createHash("sha256").update(bytes).digest("hex");
  if (sourceSha256 !== expectedDigest) {
    fail("legacy drain attestation file does not match the protected SHA-256");
  }
  let attestation;
  try {
    attestation = JSON.parse(bytes.toString("utf8"));
  } catch {
    fail("legacy drain attestation is not valid JSON");
  }
  if (bytes.toString("utf8") !== `${canonicalJson(attestation)}\n`) {
    fail("legacy drain attestation must be canonical JSON with one trailing newline");
  }
  const bridge = {
    required: true,
    phase: context.phase,
    sourceSha256,
    attestation,
  };
  validateLegacyBridgeState(bridge, {
    ...context,
    expectedDigest,
    requireUnexpired: true,
  });
  if (Date.parse(attestation.freeze.expiresAt) < Date.now() + 50 * 60 * 1000) {
    fail("legacy drain attestation must remain valid for at least 50 minutes before upload");
  }
  return bridge;
}

export function validateLegacyBridgeReadiness(
  readiness,
  bridge,
  { expectedLocked = bridge.phase === "legacy_lock" } = {},
) {
  const attestation = bridge.attestation;
  const check = readiness?.legacyTransactionBridge;
  if (
    readiness?.status !== 200
    || readiness?.ready !== true
    || !check
    || check.ready !== true
    || check.checked !== true
    || check.configuration_valid !== true
    || check.economic_writes_locked !== expectedLocked
    || check.mode !== (expectedLocked ? "locked" : "unlocked")
    || check.reason != null
    || !Array.isArray(check.blockers)
    || check.blockers.length !== 0
  ) {
    fail("candidate readiness does not prove the expected transaction bridge mode");
  }
  const current = check.direct_counts;
  const baseline = attestation.directCounts;
  const stableSample = attestation.freeze.stableLedgerSamples[1];
  const stableUsage = stableSample.connector_daily_usage;
  if (!current || canonicalJson(current.connector_pending_charges_by_connector)
      !== canonicalJson(baseline.connector_pending_charges_by_connector)) {
    fail("candidate readiness connector counts do not match the drained baseline");
  }
  if (canonicalJson(current.connector_daily_usage) !== canonicalJson(stableUsage)) {
    fail("candidate readiness connector usage changed from the frozen stable samples");
  }
  if (
    canonicalJson(current.database_fingerprints)
      !== canonicalJson(stableSample.databaseFingerprints)
  ) {
    fail("candidate readiness database fingerprints changed from the frozen stable samples");
  }
  if (
    current.connector_pending_charges !== 0
    || current.payment_proofs?.pending !== 0
    || current.payment_proofs?.settled !== 0
    || current.payment_proofs?.settlement_unknown !== 0
    || current.payment_proofs?.unknown !== 0
  ) {
    fail("candidate readiness reports transient economic state");
  }
  for (const key of [
    "total",
    "pending",
    "settled",
    "settlement_unknown",
    "released",
    "finalized",
    "unknown",
    "finalized_cached_responses",
  ]) {
    if (current.payment_proofs?.[key] !== baseline.payment_proofs[key]) {
      fail("candidate readiness payment-proof counts changed from the drained baseline");
    }
  }
  if (
    !Number.isSafeInteger(current.payment_proofs?.recent_finalized_cached_responses)
    || current.payment_proofs.recent_finalized_cached_responses < 0
    || current.payment_proofs.recent_finalized_cached_responses
      > baseline.payment_proofs.recent_finalized_cached_responses
  ) {
    fail("candidate readiness recent finalized-cache evidence is invalid");
  }
  return check;
}

export function sleep(milliseconds) {
  return new Promise((resolve) => setTimeout(resolve, milliseconds));
}

export function targetArgs(target) {
  return [
    "--project",
    target.project,
    "--environment",
    target.environment,
    "--service",
    target.service,
  ];
}

export function deploymentMatchesTarget(deployment, target) {
  return (
    deployment?.projectId === target.project
    && deployment?.environmentId === target.environment
    && deployment?.serviceId === target.service
  );
}

export function requireDeploymentTarget(deployment, target, label = "deployment") {
  if (!deploymentMatchesTarget(deployment, target)) {
    fail(`${label} does not belong to the canonical Railway target`);
  }
  return deployment;
}

export function runRailway(args, timeoutMs = 30_000, stdinText = null) {
  return new Promise((resolve) => {
    const child = spawn("railway", args, {
      env: process.env,
      shell: false,
      stdio: [stdinText == null ? "ignore" : "pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    let captureExceeded = false;
    let timedOut = false;
    let spawnError = null;
    let forceKillTimer = null;
    const terminate = () => {
      child.kill("SIGTERM");
      if (!forceKillTimer) {
        forceKillTimer = setTimeout(() => child.kill("SIGKILL"), 2_000);
      }
    };
    const timer = setTimeout(() => {
      timedOut = true;
      terminate();
    }, timeoutMs);
    const capture = (current, chunk) => {
      if (Buffer.byteLength(current) + chunk.length > MAX_CAPTURE_BYTES) {
        captureExceeded = true;
        terminate();
        return current;
      }
      return current + chunk.toString("utf8");
    };
    child.stdout.on("data", (chunk) => {
      stdout = capture(stdout, chunk);
    });
    child.stderr.on("data", (chunk) => {
      stderr = capture(stderr, chunk);
    });
    child.on("error", (error) => {
      spawnError = error;
    });
    if (stdinText != null) {
      child.stdin.on("error", () => {});
      child.stdin.end(stdinText);
    }
    child.on("close", (code, signal) => {
      clearTimeout(timer);
      if (forceKillTimer) clearTimeout(forceKillTimer);
      resolve({ code, signal, stdout, stderr, captureExceeded, timedOut, spawnError });
    });
  });
}

export async function readExactServiceVariable(target, name, timeoutMs = 30_000) {
  if (!/^[A-Z][A-Z0-9_]{0,127}$/.test(name || "")) {
    fail("invalid Railway service variable name");
  }
  const result = await runRailway(
    ["variable", "list", "--json", ...targetArgs(target)],
    timeoutMs,
  );
  const output = requireSuccessfulCommand(result, "Railway service-variable read");
  let payload;
  try {
    payload = JSON.parse(output);
  } catch {
    fail("Railway service-variable read did not return valid JSON");
  }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    fail("Railway service-variable read returned an unexpected schema");
  }
  return Object.prototype.hasOwnProperty.call(payload, name)
    ? String(payload[name])
    : null;
}

export async function setExactServiceVariable(
  target,
  name,
  value,
  timeoutMs = 30_000,
) {
  if (!/^[A-Z][A-Z0-9_]{0,127}$/.test(name || "") || typeof value !== "string") {
    fail("invalid Railway service variable mutation");
  }
  const result = await runRailway(
    ["variable", "set", name, "--stdin", "--skip-deploys", ...targetArgs(target)],
    timeoutMs,
    `${value}\n`,
  );
  requireSuccessfulCommand(result, "Railway service-variable mutation");
  const observed = await readExactServiceVariable(target, name, timeoutMs);
  if (observed !== value) {
    fail("Railway service-variable mutation could not be verified by exact readback");
  }
}

export function requireSuccessfulCommand(result, label) {
  if (
    result.code !== 0
    || result.spawnError
    || result.timedOut
    || result.captureExceeded
  ) {
    fail(`${label} failed`);
  }
  return result.stdout;
}

function validateDeploymentRows(payload, label, rejectHistoryCap = false) {
  if (!Array.isArray(payload)) fail(`Railway ${label} did not return an array`);
  if (rejectHistoryCap && payload.length >= 1000) {
    fail("Railway deployment history reached the 1000-row query cap");
  }
  const ids = new Set();
  for (const row of payload) {
    const status = typeof row?.status === "string" ? row.status.toUpperCase() : "";
    const createdAt = typeof row?.createdAt === "string"
      ? Date.parse(row.createdAt)
      : Number.NaN;
    if (
      !row
      || typeof row !== "object"
      || Array.isArray(row)
      || !UUID_PATTERN.test(row.id || "")
      || !Number.isFinite(createdAt)
    ) {
      fail(`Railway ${label} contained an invalid deployment row`);
    }
    if (!DEPLOYMENT_LIST_STATUSES.has(status)) {
      fail(`Railway ${label} contained an unknown status`);
    }
    if (ids.has(row.id)) {
      fail(`Railway ${label} contained duplicate deployment ids`);
    }
    ids.add(row.id);
  }
  return payload;
}

export function parseDeploymentList(output) {
  let payload;
  try {
    payload = JSON.parse(output);
  } catch {
    fail("Railway deployment list did not return valid JSON");
  }
  return validateDeploymentRows(payload, "deployment list", true);
}

export async function listDeployments(target, timeoutMs = 30_000) {
  const result = await runRailway(
    ["deployment", "list", "--json", "--limit", "1000", ...targetArgs(target)],
    timeoutMs,
  );
  return parseDeploymentList(requireSuccessfulCommand(result, "Railway deployment-list query"));
}

export function requireReleaseHistoryHeadroom(rows, requiredNewRows = 2) {
  if (
    !Array.isArray(rows)
    || !Number.isSafeInteger(requiredNewRows)
    || requiredNewRows < 1
  ) {
    fail("invalid Railway release-history headroom request");
  }
  if (rows.length + requiredNewRows >= 1000) {
    fail("Railway deployment history lacks headroom for the candidate and rollback");
  }
  return rows;
}

export function validateProductionBackupEvidence(
  data,
  volumeInstance,
  target,
  now = Date.now(),
) {
  const inspectedVolume = data?.volumeInstance;
  if (
    inspectedVolume?.id !== volumeInstance
    || inspectedVolume?.environmentId !== target.environment
    || inspectedVolume?.serviceId !== target.service
    || inspectedVolume?.mountPath !== "/data"
    || inspectedVolume?.state !== "READY"
    || inspectedVolume?.isPendingDeletion === true
  ) {
    fail("production backup evidence is not bound to the ready target /data volume");
  }
  const usable = (data?.volumeInstanceBackupList || []).filter((backup) => {
    const createdAt = Date.parse(backup?.createdAt || "");
    const expiresAt = backup?.expiresAt ? Date.parse(backup.expiresAt) : null;
    return (
      Number.isFinite(createdAt)
      && createdAt >= now - 26 * 60 * 60 * 1000
      && (!expiresAt || expiresAt > now + 30 * 60 * 1000)
    );
  });
  if (usable.length === 0) {
    fail("production volume has no usable Railway backup created within the last 26 hours");
  }
  const schedules = data?.volumeInstanceBackupScheduleList || [];
  if (!schedules.some((schedule) => String(schedule?.kind || "").toUpperCase() === "DAILY")) {
    fail("production volume has no DAILY Railway backup schedule");
  }
  usable.sort((left, right) => Date.parse(right.createdAt) - Date.parse(left.createdAt));
  return {
    volumeInstance,
    backupId: usable[0].id,
    backupCreatedAt: usable[0].createdAt,
    backupExpiresAt: usable[0].expiresAt,
    scheduleKinds: [...new Set(schedules.map((schedule) => schedule.kind))].sort(),
  };
}

export async function requireRailwayCliVersion(expectedVersion = "5.30.1") {
  const result = await runRailway(["--version"], 15_000);
  const output = requireSuccessfulCommand(result, "Railway CLI version check").trim();
  if (output !== `railway ${expectedVersion}`) {
    fail(`Railway CLI must be exactly ${expectedVersion}`);
  }
}

export async function resolveCanonicalTarget(target) {
  const result = await runRailway(
    [
      "status",
      "--json",
      "--project",
      target.project,
      "--environment",
      target.environment,
    ],
    30_000,
  );
  const output = requireSuccessfulCommand(result, "Railway target resolution");
  const payload = JSON.parse(output);
  if (!UUID_PATTERN.test(payload?.id || "")) {
    fail("Railway target resolution returned no canonical project id");
  }
  const environments = (payload?.environments?.edges || []).map((edge) => edge?.node);
  const matchingEnvironments = environments.filter(
    (environment) => (
      environment?.id === target.environment || environment?.name === target.environment
    ),
  );
  if (matchingEnvironments.length !== 1) {
    fail("Railway target resolution did not find exactly one environment");
  }
  const environment = matchingEnvironments[0];
  const serviceInstances = (environment?.serviceInstances?.edges || [])
    .map((edge) => edge?.node);
  const matchingServices = serviceInstances.filter(
    (service) => service?.serviceId === target.service || service?.serviceName === target.service,
  );
  if (matchingServices.length !== 1) {
    fail("Railway target resolution did not find exactly one service instance");
  }
  const service = matchingServices[0];
  if (
    !UUID_PATTERN.test(environment?.id || "")
    || !UUID_PATTERN.test(service?.serviceId || "")
    || service?.environmentId !== environment.id
  ) {
    fail("Railway target resolution returned invalid canonical identifiers");
  }
  return {
    project: payload.id,
    environment: environment.id,
    service: service.serviceId,
  };
}

export async function railwayApi(query, variables = {}, timeoutMs = 30_000) {
  const args = ["api", query];
  for (const [name, value] of Object.entries(variables)) {
    args.push("--raw-var", `${name}=${value}`);
  }
  args.push("--compact");
  const result = await runRailway(args, timeoutMs);
  const output = requireSuccessfulCommand(result, "Railway API request");
  const payload = JSON.parse(output);
  if (Array.isArray(payload?.errors) && payload.errors.length > 0) {
    fail("Railway API returned GraphQL errors");
  }
  return payload?.data;
}

export async function verifyRailwayMutationContracts() {
  const data = await railwayApi(
    "query ReleaseMutationContract { __type(name: \"Mutation\") { fields { name args { name type { kind name ofType { kind name } } } type { kind name ofType { kind name } } } } }",
  );
  const fields = new Map(
    (data?.__type?.fields || []).map((field) => [field?.name, field]),
  );
  for (const name of ["deploymentCancel", "deploymentStop", "deploymentRollback"]) {
    const field = fields.get(name);
    const type = field?.type;
    const idArguments = (field?.args || []).filter((argument) => argument?.name === "id");
    const idType = idArguments[0]?.type;
    if (
      idArguments.length !== 1
      || idType?.kind !== "NON_NULL"
      || idType?.ofType?.kind !== "SCALAR"
      || idType?.ofType?.name !== "String"
      || type?.kind !== "NON_NULL"
      || type?.ofType?.kind !== "SCALAR"
      || type?.ofType?.name !== "Boolean"
    ) {
      fail(`Railway ${name} mutation contract drifted from (id: String!) -> Boolean!`);
    }
  }
}

export async function verifyNoRepositoryDeployTriggers(target) {
  const data = await railwayApi(
    "query ReleaseDeployAuthority($serviceId: String!) { service(id: $serviceId) { id projectId repoTriggers(first: 100) { edges { node { id projectId environmentId serviceId branch repository provider } } pageInfo { hasNextPage } } } }",
    { serviceId: target.service },
  );
  const service = data?.service;
  const triggers = service?.repoTriggers;
  if (
    service?.id !== target.service
    || service?.projectId !== target.project
    || !Array.isArray(triggers?.edges)
    || typeof triggers?.pageInfo?.hasNextPage !== "boolean"
  ) {
    fail("Railway repository-trigger query was not bound to the canonical target");
  }
  if (triggers.pageInfo.hasNextPage) {
    fail("Railway repository-trigger query was truncated");
  }

  const triggerIds = new Set();
  for (const edge of triggers.edges) {
    const trigger = edge?.node;
    if (
      !UUID_PATTERN.test(trigger?.id || "")
      || trigger?.projectId !== target.project
      || trigger?.serviceId !== target.service
      || !UUID_PATTERN.test(trigger?.environmentId || "")
      || typeof trigger?.branch !== "string"
      || !trigger.branch.trim()
      || typeof trigger?.repository !== "string"
      || !trigger.repository.trim()
      || typeof trigger?.provider !== "string"
      || !trigger.provider.trim()
      || triggerIds.has(trigger.id)
    ) {
      fail("Railway repository-trigger query contained an invalid target row");
    }
    triggerIds.add(trigger.id);
  }
  if (triggerIds.size > 0) {
    fail("Railway target has a repository auto-deploy trigger");
  }
}

export async function getDeployment(deploymentId) {
  if (!UUID_PATTERN.test(deploymentId || "")) fail("invalid Railway deployment id");
  const data = await railwayApi(
    "query ExactDeployment($id: String!) { deployment(id: $id) { id projectId environmentId serviceId snapshotId status deploymentStopped canRollback createdAt meta instances { id status } } }",
    { id: deploymentId },
  );
  const deployment = data?.deployment || null;
  if (deployment && deployment.id !== deploymentId) {
    fail("Railway exact deployment query returned a different deployment id");
  }
  return deployment;
}

export async function getActiveDeployments(target) {
  const data = await railwayApi(
    "query ActiveDeployments($environmentId: String!, $serviceId: String!) { serviceInstance(environmentId: $environmentId, serviceId: $serviceId) { activeDeployments { id projectId environmentId serviceId snapshotId status deploymentStopped canRollback createdAt meta instances { id status } } } }",
    { environmentId: target.environment, serviceId: target.service },
  );
  return validateDeploymentRows(
    data?.serviceInstance?.activeDeployments,
    "active-deployments query",
  );
}

export async function verifyTargetBaseUrl(target, baseUrl) {
  const parsedBaseUrl = new URL(baseUrl);
  if (
    parsedBaseUrl.protocol !== "https:"
    || parsedBaseUrl.username
    || parsedBaseUrl.password
    || (parsedBaseUrl.port && parsedBaseUrl.port !== "443")
    || parsedBaseUrl.pathname !== "/"
    || parsedBaseUrl.search
    || parsedBaseUrl.hash
  ) {
    fail("release base URL must be a credential-free HTTPS origin");
  }
  const data = await railwayApi(
    "query TargetDomains($environmentId: String!, $serviceId: String!) { serviceInstance(environmentId: $environmentId, serviceId: $serviceId) { environmentId serviceId domains { customDomains { domain environmentId serviceId syncStatus status { verified certificateStatus } } serviceDomains { domain environmentId serviceId syncStatus } } tcpProxies { id domain proxyPort applicationPort } } }",
    { environmentId: target.environment, serviceId: target.service },
  );
  const instance = data?.serviceInstance;
  if (instance?.environmentId !== target.environment || instance?.serviceId !== target.service) {
    fail("Railway domain query returned a different target");
  }
  const attachedCustom = instance?.domains?.customDomains;
  const attachedService = instance?.domains?.serviceDomains;
  if (!Array.isArray(attachedCustom) || !Array.isArray(attachedService)) {
    fail("Railway domain query returned an incomplete attached-domain inventory");
  }
  if (!Array.isArray(instance?.tcpProxies) || instance.tcpProxies.length !== 0) {
    fail("Railway target has a TCP proxy outside the attested HTTP ingress freeze");
  }
  const customDomains = attachedCustom.filter(
    (entry) => (
      entry?.environmentId === target.environment
      && entry?.serviceId === target.service
      && entry?.syncStatus === "ACTIVE"
      && entry?.status?.verified === true
      && entry?.status?.certificateStatus === "CERTIFICATE_STATUS_TYPE_VALID"
    ),
  );
  const serviceDomains = attachedService.filter(
    (entry) => (
      entry?.environmentId === target.environment
      && entry?.serviceId === target.service
      && entry?.syncStatus === "ACTIVE"
    ),
  );
  if (
    customDomains.length !== attachedCustom.length
    || serviceDomains.length !== attachedService.length
  ) {
    fail("Railway target has an attached domain that is not active and verified");
  }
  const domains = [...customDomains, ...serviceDomains]
    .map((entry) => String(entry?.domain || "").toLowerCase());
  const host = parsedBaseUrl.hostname.toLowerCase();
  if (!domains.includes(host)) {
    fail("release base URL is not attached to the exact Railway target");
  }
  return {
    custom: [...new Set(customDomains.map((entry) => String(entry.domain).toLowerCase()))].sort(),
    service: [...new Set(serviceDomains.map((entry) => String(entry.domain).toLowerCase()))].sort(),
  };
}

export async function atomicWriteJson(path, value) {
  const temporaryPath = `${path}.${randomUUID()}.tmp`;
  let handle;
  try {
    handle = await open(temporaryPath, "wx", 0o600);
    await handle.writeFile(`${JSON.stringify(value, null, 2)}\n`, "utf8");
    await handle.sync();
    await handle.close();
    handle = null;
    await rename(temporaryPath, path);
    const directoryHandle = await open(dirname(path), "r");
    try {
      await directoryHandle.sync();
    } finally {
      await directoryHandle.close();
    }
  } catch (error) {
    if (handle) await handle.close().catch(() => {});
    await unlink(temporaryPath).catch(() => {});
    throw error;
  }
}

export async function readJson(path) {
  return JSON.parse(await readFile(path, "utf8"));
}

export function recentMessageMatches(rows, message, earliestCreatedAt) {
  return rows.filter((row) => {
    const createdAt = Date.parse(row?.createdAt || "");
    return (
      UUID_PATTERN.test(row?.id || "")
      && row?.meta?.cliMessage === message
      && Number.isFinite(createdAt)
      && createdAt >= earliestCreatedAt
    );
  });
}

export async function requestExactDeploymentEnd(deploymentId, mutationName) {
  try {
    const data = await railwayApi(
      `mutation EndDeployment($id: String!) { ${mutationName}(id: $id) }`,
      { id: deploymentId },
    );
    return data?.[mutationName] === true;
  } catch {
    return false;
  }
}

export function deploymentIsStopped(deployment) {
  const status = String(deployment?.status || "").toUpperCase();
  const running = (deployment?.instances || []).some(
    (instance) => String(instance?.status || "").toUpperCase() === "RUNNING",
  );
  return (
    !running
    && (
      deployment?.deploymentStopped === true
      || ["REMOVED", "FAILED", "CRASHED", "CANCELED", "CANCELLED", "SKIPPED"]
        .includes(status)
    )
  );
}

export function deploymentHasRunningInstance(deployment) {
  return (deployment?.instances || []).some(
    (instance) => String(instance?.status || "").toUpperCase() === "RUNNING",
  );
}

export function deploymentOccupiesActiveSet(deployment) {
  return !deploymentIsStopped(deployment);
}

export async function endExactDeployment({ deploymentId, lastStatus, pollMs = 5_000 }) {
  console.error(`Ending unaudited Railway deployment ${deploymentId} exactly.`);
  const actionDeadline = Date.now() + 30_000;
  let fallbackStatus = String(lastStatus || "").toUpperCase();
  let acknowledgedBy = null;
  while (Date.now() < actionDeadline) {
    const observed = await getDeployment(deploymentId).catch(() => null);
    if (observed && deploymentIsStopped(observed)) return;
    const status = String(observed?.status || fallbackStatus).toUpperCase();
    const mutationName = PRE_RUNTIME_STATUSES.has(status)
      ? "deploymentCancel"
      : NONTERMINAL_STATUSES.has(status) || status === "SUCCESS" || status === "SLEEPING"
        ? "deploymentStop"
        : null;
    if (!mutationName) fail(`refusing to end deployment with unknown status ${status || "missing"}`);
    if (await requestExactDeploymentEnd(deploymentId, mutationName)) {
      acknowledgedBy = mutationName;
      break;
    }
    fallbackStatus = status;
    await sleep(Math.min(pollMs, 2_000));
  }
  if (!acknowledgedBy) {
    fail(`Railway did not acknowledge an exact cancel or stop for ${deploymentId}`);
  }
  console.error(`Railway acknowledged ${acknowledgedBy} for ${deploymentId}.`);

  const deadline = Date.now() + 90_000;
  while (Date.now() < deadline) {
    try {
      const deployment = await getDeployment(deploymentId);
      if (deploymentIsStopped(deployment)) {
        console.error(`Verified unaudited deployment ${deploymentId} is stopped.`);
        return;
      }
    } catch {
      // Retry boundedly; cleanup requires a positive stopped state.
    }
    await sleep(Math.min(pollMs, 5_000));
  }
  fail(`could not verify that exact deployment ${deploymentId} stopped`);
}

export function safeDeploymentSnapshot(deployment) {
  if (!deployment) return null;
  return {
    id: deployment.id,
    projectId: deployment.projectId,
    environmentId: deployment.environmentId,
    serviceId: deployment.serviceId,
    snapshotId: deployment.snapshotId || null,
    status: deployment.status,
    deploymentStopped: deployment.deploymentStopped,
    canRollback: deployment.canRollback,
    createdAt: deployment.createdAt,
    imageDigest: deployment.meta?.imageDigest || null,
    runningInstances: (deployment.instances || []).filter(
      (instance) => String(instance?.status || "").toUpperCase() === "RUNNING",
    ).length,
  };
}

export async function fetchHealthSnapshot(baseUrl, path, timeoutMs = 15_000) {
  const response = await fetch(`${baseUrl.replace(/\/$/, "")}${path}`, {
    headers: { "User-Agent": "blocksize-release-smoke/1.0" },
    redirect: "manual",
    signal: AbortSignal.timeout(timeoutMs),
  });
  let body = null;
  try {
    body = await response.json();
  } catch {
    body = null;
  }
  return {
    status: response.status,
    applicationStatus: body?.status || null,
    service: body?.service || null,
    ready: body?.ready === true,
    version: body?.version || null,
    commitSha: body?.commit_sha || null,
    legacyTransactionBridge: (
      body?.checks?.legacy_transaction_bridge
      && typeof body.checks.legacy_transaction_bridge === "object"
      && !Array.isArray(body.checks.legacy_transaction_bridge)
    )
      ? body.checks.legacy_transaction_bridge
      : null,
  };
}

export async function verifyHealthRestored(state, timeoutMs = 120_000, pollMs = 5_000) {
  const deadline = Date.now() + timeoutMs;
  let lastProblem = "no response";
  let consecutiveMatches = 0;
  while (Date.now() < deadline) {
    try {
      const health = await fetchHealthSnapshot(state.baseUrl, "/health");
      const expected = state.prior.health;
      const versionMatches = health.version === expected.version;
      const commitMatches = !expected.commitSha || health.commitSha === expected.commitSha;
      const serviceMatches = !expected.service || health.service === expected.service;
      const statusMatches = (
        !expected.applicationStatus
        || health.applicationStatus === expected.applicationStatus
      );
      if (
        health.status === 200
        && versionMatches
        && commitMatches
        && serviceMatches
        && statusMatches
      ) {
        if (state.prior.readiness?.status === 200) {
          const readiness = await fetchHealthSnapshot(state.baseUrl, "/readyz");
          const expectedReadiness = state.prior.readiness;
          if (
            readiness.status !== 200
            || readiness.ready !== true
            || (
              expectedReadiness.version
              && readiness.version !== expectedReadiness.version
            )
            || (
              expectedReadiness.commitSha
              && readiness.commitSha !== expectedReadiness.commitSha
            )
          ) {
            consecutiveMatches = 0;
            lastProblem = `readiness returned ${readiness.status}`;
            await sleep(pollMs);
            continue;
          }
        }
        consecutiveMatches += 1;
        if (consecutiveMatches >= 3) return;
        lastProblem = `health matched ${consecutiveMatches}/3 consecutive probes`;
        await sleep(Math.min(2_000, pollMs));
        continue;
      }
      consecutiveMatches = 0;
      lastProblem = `health returned ${health.status} version ${health.version || "missing"}`;
    } catch (error) {
      consecutiveMatches = 0;
      lastProblem = String(error);
    }
    await sleep(pollMs);
  }
  fail(`prior release health was not restored: ${lastProblem}`);
}
