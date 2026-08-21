#!/usr/bin/env node

/**
 * Narrow, fail-closed production controller for the Coinbase x402 hotfix.
 *
 * This controller intentionally has no funded-payment command.  It can only:
 *   1. attest the fixed production target and legacy rollback point;
 *   2. upload an exact Git head with payments locked in shadow mode;
 *   3. promote that exact image to enforce mode; or
 *   4. recover according to the recorded economic rollback boundary.
 *
 * It never reads payment credentials and never creates/restores a volume backup.
 */

import { spawn } from "node:child_process";
import { randomUUID } from "node:crypto";
import {
  access,
  chmod,
  mkdir,
  mkdtemp,
  open,
  readFile,
  rename,
  rm,
  stat,
  unlink,
} from "node:fs/promises";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { pathToFileURL } from "node:url";

export const TARGET = Object.freeze({
  project: "9fc6c062-6d58-4cb9-af11-df68670bfca5",
  environment: "9d51961d-759c-441b-be1d-186515b9ed7f",
  service: "8853c53e-521e-4876-a796-f94c1adf5700",
});

export const LEGACY = Object.freeze({
  deploymentId: "a676ba77-412b-4ae4-8606-87ade7c9ff53",
  imageDigest: "sha256:435dc858af3fcb3eb44b4e249e0d8e4a917f62f174881fd320f8df1d57c5d6c3",
});

export const DOMAINS = Object.freeze([
  "mcp.blocksize.info",
  "agentic-payments-production.up.railway.app",
]);

export const SHADOW_VARIABLES = Object.freeze({
  X402_PAYMENT_MODE: "shadow",
  X402_PAYMENT_DB_PATH: "/data/x402_payments.sqlite3",
  X402_PAYMENT_MAX_CACHED_RESPONSE_BYTES: "524288",
  X402_FACILITATOR_READINESS_TIMEOUT_SECONDS: "5",
  X402_FACILITATOR_READINESS_MAX_AGE_SECONDS: "180",
  X402_FACILITATOR_REFRESH_INTERVAL_SECONDS: "60",
  X402_PAYMENT_VERIFICATION_LEASE_SECONDS: "120",
  X402_PAYMENT_REPLAY_TTL_SECONDS: "3600",
  X402_PAYMENT_REPLAY_MAX_ENTRIES: "500",
  X402_PAYMENT_RATE_LIMIT_PER_MINUTE: "12",
  X402_PAYMENT_RATE_LIMIT_PER_DAY: "200",
  X402_FACILITATOR_MAX_INFLIGHT: "4",
  X402_BASE_USDC_NAME: "USD Coin",
  X402_BASE_USDC_VERSION: "2",
  X402_ENFORCE_GET_ROUTES: "v1_vwap",
});

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const SHA = /^[0-9a-f]{40}$/;
const DIGEST = /^sha256:[0-9a-f]{64}$/;
const SOLANA_ADDRESS = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/;
const EVM_ADDRESS = /^0x[0-9a-fA-F]{40}$/;
const TERMINAL_FAILURES = new Set([
  "FAILED",
  "CRASHED",
  "REMOVED",
  "SKIPPED",
  "CANCELED",
  "CANCELLED",
]);
const RETRYABLE_RECOVERY_FAILURES = new Set(TERMINAL_FAILURES);
const ROLLBACK_SOURCE_STATUSES = new Set(["SUCCESS", "REMOVED", "CRASHED"]);
const IN_FLIGHT = new Set([
  "INITIALIZING",
  "QUEUED",
  "WAITING",
  "NEEDS_APPROVAL",
  "BUILDING",
  "DEPLOYING",
  "REMOVING",
  "SLEEPING",
]);
const FUNDED_COMMANDS = new Set([
  "funded-test",
  "funded",
  "pay",
  "payment",
  "verify",
  "settle",
  "restore-volume",
]);
const ALLOWED_COMMANDS = new Set([
  "preflight",
  "deploy-shadow",
  "promote-enforce",
  "recover",
]);
const REDEPLOY_PHASE_BY_PURPOSE = Object.freeze({
  shadow_identity: "shadow_identity_redeploy_armed",
  enforce: "enforce_redeploy_armed",
  legacy_recovery: "legacy_recovery_redeploy_armed",
  same_commit_shadow_recovery: "same_commit_shadow_recovery_redeploy_armed",
});
const RECOVERY_REDEPLOY_PURPOSES = new Set([
  "legacy_recovery",
  "same_commit_shadow_recovery",
]);
const MAX_CAPTURE = 16 * 1024 * 1024;
export const TARGET_LOCK_PATH = join(
  tmpdir(),
  `.blocksize-direct-prod-${TARGET.project}-${TARGET.environment}-${TARGET.service}`,
  "controller.lock",
);

function fail(message) {
  throw new Error(message);
}

function assert(condition, message) {
  if (!condition) fail(message);
}

function canonicalDigest(value) {
  const text = String(value || "").toLowerCase();
  if (/^[0-9a-f]{64}$/.test(text)) return `sha256:${text}`;
  return text;
}

function parsePositiveInteger(value, label, { minimum = 1, maximum = 86_400 } = {}) {
  assert(/^\d+$/.test(String(value || "")), `${label} must be an integer`);
  const number = Number(value);
  assert(number >= minimum && number <= maximum, `${label} is outside its safe range`);
  return number;
}

export function parseArguments(argv) {
  const [command, ...rest] = argv;
  const normalizedCommand = String(command || "").toLowerCase();
  if (
    FUNDED_COMMANDS.has(normalizedCommand)
    || /(?:funded|payment|verify|settle|(?:^|-)pay(?:-|$)|volume.*restore|restore.*volume)/
      .test(normalizedCommand)
  ) {
    fail("funded payment, verify, settle, and volume-restore actions are deliberately absent");
  }
  assert(ALLOWED_COMMANDS.has(command), usage());
  const booleanFlags = new Set(["yes"]);
  const allowed = new Set([
    "state",
    "commit",
    "volume-instance-id",
    "solana-pay-to",
    "base-pay-to",
    "artifact-root",
    "reason",
    "timeout-seconds",
    "poll-seconds",
    "soak-seconds",
    "yes",
  ]);
  const values = new Map();
  for (let index = 0; index < rest.length; index += 1) {
    const flag = rest[index];
    assert(flag?.startsWith("--"), `unexpected positional argument ${flag || ""}`);
    const name = flag.slice(2);
    assert(allowed.has(name) && !values.has(name), `unexpected or duplicate argument ${flag}`);
    if (booleanFlags.has(name)) {
      values.set(name, true);
    } else {
      const value = rest[index + 1];
      assert(value !== undefined && !value.startsWith("--"), `${flag} requires a value`);
      values.set(name, value);
      index += 1;
    }
  }
  const stateFile = values.get("state");
  assert(stateFile, "--state is required");
  assert(resolve(stateFile) === stateFile, "--state must be an absolute path");
  const commit = String(values.get("commit") || "").toLowerCase();
  if (command !== "recover") {
    assert(SHA.test(commit), "--commit must be a full lowercase 40-character Git SHA");
  } else if (commit) {
    assert(SHA.test(commit), "--commit must be a full lowercase 40-character Git SHA");
  }
  if (command === "preflight") {
    assert(UUID.test(values.get("volume-instance-id") || ""), "--volume-instance-id is required");
    assert(SOLANA_ADDRESS.test(values.get("solana-pay-to") || ""),
      "--solana-pay-to is required and must be a Solana address");
    assert(EVM_ADDRESS.test(values.get("base-pay-to") || "")
      && !/^0x0{40}$/i.test(values.get("base-pay-to") || ""),
    "--base-pay-to is required and must be a nonzero EVM address");
  }
  if (command !== "preflight") {
    assert(!values.has("volume-instance-id"), "--volume-instance-id is accepted only by preflight");
    assert(!values.has("solana-pay-to") && !values.has("base-pay-to"),
      "payment recipients are accepted only by preflight");
  }
  const mutating = command !== "preflight";
  assert(!mutating || values.get("yes") === true, `${command} requires --yes`);
  assert(command !== "preflight" || !values.has("yes"), "preflight is read-only and does not accept --yes");
  const reason = String(values.get("reason") || "").trim();
  if (command === "recover") {
    assert(reason.length >= 4 && reason.length <= 200 && !/[\r\n]/.test(reason),
      "recover requires a single-line --reason (4-200 characters)");
  } else {
    assert(!values.has("reason"), "--reason is accepted only by recover");
  }
  return {
    command,
    stateFile,
    commit: commit || null,
    volumeInstanceId: values.get("volume-instance-id") || null,
    paymentRecipients: command === "preflight" ? {
      solana: values.get("solana-pay-to"),
      base: values.get("base-pay-to"),
    } : null,
    artifactRoot: resolve(values.get("artifact-root") || process.cwd()),
    reason: reason || null,
    timeoutMs: parsePositiveInteger(values.get("timeout-seconds") || "900", "--timeout-seconds", {
      minimum: 60,
      maximum: 1_800,
    }) * 1000,
    pollMs: parsePositiveInteger(values.get("poll-seconds") || "5", "--poll-seconds", {
      minimum: 1,
      maximum: 30,
    }) * 1000,
    soakSeconds: parsePositiveInteger(values.get("soak-seconds") || "370", "--soak-seconds", {
      minimum: 360,
      maximum: 1_800,
    }),
  };
}

function usage() {
  return [
    "usage:",
    "  direct_prod_coinbase_release.mjs preflight --state ABS --commit SHA --volume-instance-id UUID --solana-pay-to ADDRESS --base-pay-to ADDRESS",
    "  direct_prod_coinbase_release.mjs deploy-shadow --state ABS --commit SHA [--artifact-root DIR] --yes",
    "  direct_prod_coinbase_release.mjs promote-enforce --state ABS --commit SHA --yes",
    "  direct_prod_coinbase_release.mjs recover --state ABS --reason TEXT --yes",
    "funded payment execution is intentionally unsupported",
  ].join("\n");
}

export function defaultRun(argv, { cwd = process.cwd(), stdin = null, timeoutMs = 30_000 } = {}) {
  return new Promise((resolvePromise) => {
    const child = spawn(argv[0], argv.slice(1), {
      cwd,
      env: process.env,
      shell: false,
      stdio: [stdin == null ? "ignore" : "pipe", "pipe", "pipe"],
    });
    let stdout = "";
    let stderr = "";
    let exceeded = false;
    let timedOut = false;
    let spawnError = null;
    let killTimer = null;
    const stop = () => {
      child.kill("SIGTERM");
      if (!killTimer) killTimer = setTimeout(() => child.kill("SIGKILL"), 2_000);
    };
    const capture = (current, chunk) => {
      if (Buffer.byteLength(current) + chunk.length > MAX_CAPTURE) {
        exceeded = true;
        stop();
        return current;
      }
      return current + chunk.toString("utf8");
    };
    child.stdout.on("data", (chunk) => { stdout = capture(stdout, chunk); });
    child.stderr.on("data", (chunk) => { stderr = capture(stderr, chunk); });
    child.on("error", (error) => { spawnError = error; });
    if (stdin != null) child.stdin.end(stdin);
    const timer = setTimeout(() => { timedOut = true; stop(); }, timeoutMs);
    child.on("close", (code, signal) => {
      clearTimeout(timer);
      if (killTimer) clearTimeout(killTimer);
      resolvePromise({ code, signal, stdout, stderr, exceeded, timedOut, spawnError });
    });
  });
}

function commandOutput(result, label) {
  assert(
    result && result.code === 0 && !result.spawnError && !result.exceeded && !result.timedOut,
    `${label} failed`,
  );
  return result.stdout;
}

function parseJsonOutput(output, label) {
  try {
    return JSON.parse(String(output || "").trim());
  } catch {
    fail(`${label} did not return valid JSON`);
  }
}

async function runJson(deps, argv, label, options = {}) {
  return parseJsonOutput(
    commandOutput(await deps.run(argv, options), label),
    label,
  );
}

async function railwayApi(deps, query, variables = {}) {
  const args = ["railway", "api", query];
  for (const [key, value] of Object.entries(variables)) {
    args.push("--raw-var", `${key}=${value}`);
  }
  args.push("--compact");
  const payload = await runJson(deps, args, "Railway GraphQL request");
  assert(!payload?.errors?.length, "Railway GraphQL returned errors");
  return payload?.data;
}

async function resolveTarget(deps) {
  const payload = await runJson(
    deps,
    ["railway", "status", "--json", "--project", TARGET.project, "--environment", TARGET.environment],
    "Railway target resolution",
  );
  assert(payload?.id === TARGET.project, "Railway returned a different project");
  const environments = (payload?.environments?.edges || []).map((edge) => edge?.node);
  const environment = environments.filter((item) => item?.id === TARGET.environment);
  assert(environment.length === 1, "Railway returned a different or ambiguous environment");
  const services = (environment[0]?.serviceInstances?.edges || []).map((edge) => edge?.node);
  const service = services.filter((item) => item?.serviceId === TARGET.service);
  assert(service.length === 1 && service[0]?.environmentId === TARGET.environment,
    "Railway returned a different or ambiguous service");
}

async function getDomains(deps) {
  const data = await railwayApi(
    deps,
    "query DirectProdDomains($environmentId:String!,$serviceId:String!){serviceInstance(environmentId:$environmentId,serviceId:$serviceId){environmentId serviceId domains{customDomains{domain environmentId serviceId syncStatus status{verified certificateStatus}} serviceDomains{domain environmentId serviceId syncStatus}}} tcpProxies(environmentId:$environmentId,serviceId:$serviceId){id domain environmentId serviceId syncStatus proxyPort applicationPort}}",
    { environmentId: TARGET.environment, serviceId: TARGET.service },
  );
  const instance = data?.serviceInstance;
  assert(instance?.environmentId === TARGET.environment && instance?.serviceId === TARGET.service,
    "domain inventory is not bound to the fixed target");
  const custom = instance?.domains?.customDomains;
  const service = instance?.domains?.serviceDomains;
  assert(Array.isArray(custom) && Array.isArray(service), "Railway returned incomplete domains");
  const tcpProxies = data?.tcpProxies;
  assert(Array.isArray(tcpProxies), "Railway returned incomplete TCP proxy inventory");
  assert(tcpProxies.every((entry) => entry?.environmentId === TARGET.environment
    && entry?.serviceId === TARGET.service), "TCP proxy inventory is not bound to the fixed target");
  assert(tcpProxies.length === 0, "production has an attached TCP proxy");
  for (const entry of custom) {
    assert(
      entry?.environmentId === TARGET.environment
        && entry?.serviceId === TARGET.service
        && entry?.syncStatus === "ACTIVE"
        && entry?.status?.verified === true
        && entry?.status?.certificateStatus === "CERTIFICATE_STATUS_TYPE_VALID",
      "custom domain is not active, verified, and target-bound",
    );
  }
  for (const entry of service) {
    assert(
      entry?.environmentId === TARGET.environment
        && entry?.serviceId === TARGET.service
        && entry?.syncStatus === "ACTIVE",
      "Railway service domain is not active and target-bound",
    );
  }
  const actual = [...custom, ...service].map((entry) => String(entry.domain).toLowerCase()).sort();
  assert(JSON.stringify([...new Set(actual)]) === JSON.stringify([...DOMAINS].sort()),
    "production domain inventory differs from the two reviewed domains");
  return actual;
}

async function assertNoTriggers(deps) {
  const data = await railwayApi(
    deps,
    "query DirectProdAuthority($serviceId:String!){service(id:$serviceId){id projectId repoTriggers(first:100){edges{node{id projectId environmentId serviceId branch repository provider}} pageInfo{hasNextPage}}}}",
    { serviceId: TARGET.service },
  );
  const service = data?.service;
  assert(service?.id === TARGET.service && service?.projectId === TARGET.project,
    "repository-trigger evidence is not target-bound");
  assert(Array.isArray(service?.repoTriggers?.edges)
    && service.repoTriggers.pageInfo?.hasNextPage === false,
  "repository-trigger inventory is incomplete");
  assert(service.repoTriggers.edges.length === 0, "production has a repository auto-deploy trigger");
}

function validateDeployment(row, label) {
  assert(row && UUID.test(row.id || ""), `${label} has no valid deployment id`);
  assert(row.projectId === TARGET.project && row.environmentId === TARGET.environment
    && row.serviceId === TARGET.service, `${label} is not bound to the fixed target`);
  assert(Number.isFinite(Date.parse(row.createdAt || "")), `${label} has no creation timestamp`);
  return row;
}

async function getActive(deps) {
  const data = await railwayApi(
    deps,
    "query DirectProdActive($environmentId:String!,$serviceId:String!){serviceInstance(environmentId:$environmentId,serviceId:$serviceId){activeDeployments{id projectId environmentId serviceId snapshotId status deploymentStopped canRollback createdAt meta instances{id status}}}}",
    { environmentId: TARGET.environment, serviceId: TARGET.service },
  );
  const rows = data?.serviceInstance?.activeDeployments;
  assert(Array.isArray(rows), "Railway active deployment inventory is incomplete");
  return rows.map((row) => validateDeployment(row, "active deployment"));
}

async function getExactDeployment(deps, id) {
  assert(UUID.test(id || ""), "invalid exact deployment id");
  const data = await railwayApi(
    deps,
    "query DirectProdExact($id:String!){deployment(id:$id){id projectId environmentId serviceId snapshotId status deploymentStopped canRollback createdAt meta instances{id status}}}",
    { id },
  );
  if (!data?.deployment) return null;
  assert(data.deployment.id === id, "Railway exact query returned a different deployment id");
  return validateDeployment(data.deployment, "exact deployment");
}

async function listDeployments(deps, requiredNewRows = 0) {
  const rows = await runJson(
    deps,
    [
      "railway", "deployment", "list", "--json", "--limit", "1000",
      "--project", TARGET.project, "--environment", TARGET.environment, "--service", TARGET.service,
    ],
    "Railway deployment history",
  );
  assert(Array.isArray(rows) && rows.length < 1000, "Railway deployment history is incomplete");
  const ids = new Set();
  for (const row of rows) {
    assert(UUID.test(row?.id || "") && Number.isFinite(Date.parse(row?.createdAt || "")),
      "Railway deployment history contains an invalid row");
    assert(!ids.has(row.id), "Railway deployment history contains duplicate ids");
    ids.add(row.id);
  }
  assert(Number.isSafeInteger(requiredNewRows) && requiredNewRows >= 0,
    "invalid deployment-history headroom request");
  assert(rows.length + requiredNewRows < 1000,
    "Railway deployment history lacks release and recovery headroom");
  return rows;
}

function runningSuccess(row) {
  return String(row?.status || "").toUpperCase() === "SUCCESS"
    && row?.deploymentStopped !== true
    && (row?.instances || []).filter((item) => String(item?.status).toUpperCase() === "RUNNING").length === 1;
}

function imageDigest(row) {
  return canonicalDigest(row?.meta?.imageDigest || "");
}

async function assertOneActive(deps, expectedId, expectedDigest = null) {
  const active = await getActive(deps);
  assert(active.length === 1, "production must have exactly one active deployment");
  const row = active[0];
  assert(row.id === expectedId, `active deployment is ${row.id}, expected ${expectedId}`);
  assert(runningSuccess(row), "the exact active deployment is not one running success");
  if (expectedDigest) {
    assert(imageDigest(row) === canonicalDigest(expectedDigest), "active image digest changed");
  }
  return row;
}

async function waitForOneActiveConvergence(
  deps,
  expectedId,
  expectedDigest,
  options,
  retiringDeploymentIds = [],
) {
  assert(UUID.test(expectedId || "") && DIGEST.test(canonicalDigest(expectedDigest)),
    "active convergence requires one exact immutable deployment");
  const retiring = new Set(retiringDeploymentIds.filter(Boolean));
  assert(!retiring.has(expectedId) && [...retiring].every((id) => UUID.test(id)),
    "active convergence has an invalid retiring deployment set");
  const allowed = new Set([expectedId, ...retiring]);
  const attempts = Math.max(1, Math.ceil(options.timeoutMs / options.pollMs));
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const active = await getActive(deps);
    assert(new Set(active.map((row) => row.id)).size === active.length,
      "active convergence returned duplicate deployment ids");
    const unexpected = active.filter((row) => !allowed.has(row.id));
    assert(unexpected.length === 0,
      `active convergence found unknown deployment ${unexpected[0]?.id || ""}`);
    const expected = active.find((row) => row.id === expectedId);
    if (expected) {
      assert(imageDigest(expected) === canonicalDigest(expectedDigest),
        "converging active deployment image digest changed");
      const status = String(expected.status || "").toUpperCase();
      assert(runningSuccess(expected) || IN_FLIGHT.has(status),
        "converging deployment is terminal or unhealthy");
    }
    if (active.length === 1 && expected && runningSuccess(expected)) {
      const history = await listDeployments(deps);
      const unsafeInFlight = history.filter((row) => {
        const status = String(row.status || "").toUpperCase();
        return IN_FLIGHT.has(status) && !retiring.has(row.id);
      });
      assert(unsafeInFlight.length === 0,
        `unrelated deployment ${unsafeInFlight[0]?.id || ""} is still in flight`);
      const retiringInFlight = history.some((row) => (
        retiring.has(row.id) && IN_FLIGHT.has(String(row.status || "").toUpperCase())
      ));
      if (!retiringInFlight) return expected;
    }
    if (attempt + 1 < attempts) await deps.sleep(options.pollMs);
  }
  fail(`timed out waiting for deployment ${expectedId} to become the sole active deployment`);
}

function assertRollbackCapableSource(row, expectedId, expectedDigest, label) {
  assert(row?.id === expectedId
    && imageDigest(row) === canonicalDigest(expectedDigest)
    && UUID.test(row?.snapshotId || "")
    && row?.canRollback === true,
  `${label} is not the exact rollback-capable immutable deployment`);
  return row;
}

async function getBackupEvidence(deps, volumeInstanceId, now) {
  const data = await railwayApi(
    deps,
    "query DirectProdBackup($id:String!){volumeInstance(id:$id){id environmentId serviceId mountPath state isPendingDeletion} volumeInstanceBackupList(volumeInstanceId:$id){id externalId name createdAt expiresAt usedMB referencedMB volumeInstanceSizeMB scheduleId} volumeInstanceBackupScheduleList(volumeInstanceId:$id){id kind cron}}",
    { id: volumeInstanceId },
  );
  const volume = data?.volumeInstance;
  assert(volume?.id === volumeInstanceId && volume?.environmentId === TARGET.environment
    && volume?.serviceId === TARGET.service && volume?.mountPath === "/data"
    && volume?.state === "READY" && volume?.isPendingDeletion !== true,
  "backup evidence is not bound to the ready production /data volume");
  const schedules = data?.volumeInstanceBackupScheduleList;
  assert(Array.isArray(schedules)
    && schedules.some((item) => String(item?.kind || "").toUpperCase() === "DAILY"),
  "production /data volume has no daily backup schedule");
  const usable = (data?.volumeInstanceBackupList || []).filter((item) => {
    const created = Date.parse(item?.createdAt || "");
    return UUID.test(item?.id || "") && Number.isFinite(created)
      && created >= now - 60 * 60 * 1000
      && created <= now + 2 * 60 * 1000
      && typeof item?.externalId === "string"
      && item.externalId.length > 0
      && String(item?.name || "").startsWith("coinbase-x402-hotfix-")
      && item?.scheduleId == null
      && item?.expiresAt == null
      && Number.isFinite(item?.usedMB)
      && item.usedMB >= 0;
  }).sort((left, right) => Date.parse(right.createdAt) - Date.parse(left.createdAt));
  assert(usable.length > 0,
    "production /data has no completed, locked, named on-demand backup from the last hour");
  return {
    volumeInstanceId,
    backupId: usable[0].id,
    createdAt: usable[0].createdAt,
    expiresAt: usable[0].expiresAt || null,
    backupKind: "MANUAL_LOCKED",
    dailyScheduleVerified: true,
  };
}

async function assertExactGitHead(deps, artifactRoot, expectedCommit) {
  const head = commandOutput(
    await deps.run(["git", "rev-parse", "HEAD"], { cwd: artifactRoot }),
    "Git HEAD check",
  ).trim().toLowerCase();
  assert(head === expectedCommit, `artifact HEAD ${head} does not match ${expectedCommit}`);
  const statusOutput = commandOutput(
    await deps.run(["git", "status", "--porcelain", "--untracked-files=all"], { cwd: artifactRoot }),
    "Git cleanliness check",
  );
  assert(statusOutput.trim() === "", "artifact worktree is not clean");
  const ignoredOutput = commandOutput(
    await deps.run(
      [
        "git", "ls-files", "--others", "--ignored", "--exclude-standard", "-z", "--",
        ":(icase,glob)**/.env",
        ":(icase,glob)**/.env.*",
        ":(icase,glob)**/*.db",
        ":(icase,glob)**/*.db-wal",
        ":(icase,glob)**/*.db-shm",
        ":(icase,glob)**/*.sqlite",
        ":(icase,glob)**/*.sqlite-wal",
        ":(icase,glob)**/*.sqlite-shm",
        ":(icase,glob)**/*.sqlite3",
        ":(icase,glob)**/*.sqlite3-wal",
        ":(icase,glob)**/*.sqlite3-shm",
        ":(icase,glob)**/*.pem",
        ":(icase,glob)**/*.key",
        ":(icase,glob)**/*.p12",
        ":(icase,glob)**/*.pfx",
        ":(icase,glob)**/*credential*",
        ":(icase,glob)**/*secret*",
        ":(icase,glob)**/*token*",
        ":(exclude,glob).venv/**",
        ":(exclude,glob)**/.venv/**",
        ":(exclude,glob)node_modules/**",
        ":(exclude,glob)**/node_modules/**",
        ":(exclude,glob)**/__pycache__/**",
        ":(exclude,glob)**/.pytest_cache/**",
      ],
      { cwd: artifactRoot },
    ),
    "ignored-file inventory",
  );
  const sensitiveIgnored = ignoredOutput.split("\0").filter(Boolean);
  assert(sensitiveIgnored.length === 0,
    "artifact root contains ignored database, environment, key, token, or credential state");
  const tree = commandOutput(
    await deps.run(["git", "rev-parse", `${expectedCommit}^{tree}`], { cwd: artifactRoot }),
    "Git tree check",
  ).trim().toLowerCase();
  assert(SHA.test(tree), "exact artifact Git tree is invalid");
  return tree;
}

async function prepareExactArchive(deps, artifactRoot, commit) {
  const temporaryRoot = await mkdtemp(join(tmpdir(), "blocksize-coinbase-release-"));
  const archivePath = join(temporaryRoot, "source.tar");
  const sourceRoot = join(temporaryRoot, "source");
  await mkdir(sourceRoot, { mode: 0o700 });
  try {
    commandOutput(await deps.run([
      "git", "archive", "--format=tar", "--output", archivePath, commit,
    ], { cwd: artifactRoot }), "exact Git archive creation");
    commandOutput(await deps.run([
      "tar", "-xf", archivePath, "-C", sourceRoot,
    ]), "exact Git archive extraction");
    return {
      sourceRoot,
      async cleanup() { await rm(temporaryRoot, { recursive: true, force: true }); },
    };
  } catch (error) {
    await rm(temporaryRoot, { recursive: true, force: true }).catch(() => {});
    throw error;
  }
}

export async function atomicWriteState(path, value) {
  await mkdir(dirname(path), { recursive: true, mode: 0o700 });
  const temporary = `${path}.${randomUUID()}.tmp`;
  let handle = null;
  try {
    handle = await open(temporary, "wx", 0o600);
    await handle.writeFile(`${JSON.stringify(value, null, 2)}\n`, "utf8");
    await handle.sync();
    await handle.close();
    handle = null;
    await chmod(temporary, 0o600);
    await rename(temporary, path);
    await chmod(path, 0o600);
    const directory = await open(dirname(path), "r");
    try { await directory.sync(); } finally { await directory.close(); }
  } catch (error) {
    if (handle) await handle.close().catch(() => {});
    await unlink(temporary).catch(() => {});
    throw error;
  }
}

export async function readState(path) {
  const details = await stat(path);
  assert(details.isFile() && (details.mode & 0o777) === 0o600,
    "release state must be a mode-0600 regular file");
  const value = JSON.parse(await readFile(path, "utf8"));
  assert(value?.schemaVersion === 1, "unsupported release state schema");
  assert(JSON.stringify(value.target) === JSON.stringify(TARGET), "release state target drifted");
  assert(value?.legacy?.deploymentId === LEGACY.deploymentId
    && canonicalDigest(value?.legacy?.imageDigest) === LEGACY.imageDigest,
  "release state legacy rollback point drifted");
  assert(SHA.test(value?.commit || "") && SHA.test(value?.artifactTree || ""),
    "release state artifact identity is invalid");
  assert(SOLANA_ADDRESS.test(value?.paymentRecipients?.solana || "")
    && EVM_ADDRESS.test(value?.paymentRecipients?.base || "")
    && !/^0x0{40}$/i.test(value?.paymentRecipients?.base || ""),
  "release state payment recipients are invalid");
  assert(JSON.stringify([...(value?.domains || [])].sort()) === JSON.stringify([...DOMAINS].sort()),
    "release state domain inventory drifted");
  assert(value.fundedExecutionSupported === false && value.volumeRestoreAllowed === false,
    "release state expanded the controller's authority");
  assert(["legacy_allowed_before_enforce", "same_commit_shadow_only"].includes(
    value.rollbackBoundary,
  ), "release state rollback boundary is invalid");
  if (value.phase === "shadow_upload_armed") validateUploadIntent(value);
  if (value.pendingRedeploy) validateRedeployIntent(value);
  return value;
}

function validateUploadIntent(state) {
  const prefix = `coinbase-x402-shadow:${state.commit}:`;
  assert(typeof state.uploadMessage === "string"
    && state.uploadMessage.startsWith(prefix)
    && UUID.test(state.uploadMessage.slice(prefix.length)),
  "release state upload intent is invalid");
  assert(state.uploadPriorActiveDeploymentId === LEGACY.deploymentId,
    "release state upload baseline drifted");
  assert(Array.isArray(state.uploadBaselineDeploymentIds)
    && state.uploadBaselineDeploymentIds.length > 0
    && new Set(state.uploadBaselineDeploymentIds).size
      === state.uploadBaselineDeploymentIds.length
    && state.uploadBaselineDeploymentIds.every((id) => UUID.test(id)),
  "release state upload history baseline is invalid");
  assert(Number.isFinite(Date.parse(state.uploadArmedAt || "")),
    "release state upload intent has no timestamp");
  assert(state.rollbackBoundary === "legacy_allowed_before_enforce"
    && state.fundedAttemptMayHaveStarted !== true,
  "release state upload intent crossed the funded-attempt boundary");
  return state.uploadMessage;
}

function expectedRedeployBinding(state, purpose) {
  if (purpose === "shadow_identity") {
    return {
      sourceDeploymentId: state.candidate?.buildDeploymentId,
      expectedImageDigest: state.candidate?.imageDigest,
    };
  }
  if (purpose === "enforce" || purpose === "same_commit_shadow_recovery") {
    return {
      sourceDeploymentId: state.candidate?.shadowDeploymentId,
      expectedImageDigest: state.candidate?.imageDigest,
    };
  }
  if (purpose === "legacy_recovery") {
    return {
      sourceDeploymentId: LEGACY.deploymentId,
      expectedImageDigest: LEGACY.imageDigest,
    };
  }
  fail("release state contains an unknown exact-redeploy purpose");
}

function validateRedeployIntent(state) {
  const intent = state.pendingRedeploy;
  assert(intent && Object.hasOwn(REDEPLOY_PHASE_BY_PURPOSE, intent.purpose),
    "release state exact-redeploy intent is invalid");
  assert(state.phase === REDEPLOY_PHASE_BY_PURPOSE[intent.purpose],
    "release state exact-redeploy phase does not match its intent");
  assert(UUID.test(intent.sourceDeploymentId || "")
    && DIGEST.test(canonicalDigest(intent.expectedImageDigest))
    && intent.expectedCommit === state.commit
    && intent.mutationKind === (RECOVERY_REDEPLOY_PURPOSES.has(intent.purpose)
      ? "rollback"
      : "redeploy")
    && UUID.test(intent.sourceSnapshotId || "")
    && (intent.priorActiveDeploymentId === null
      || UUID.test(intent.priorActiveDeploymentId || ""))
    && Array.isArray(intent.baselineDeploymentIds)
    && intent.baselineDeploymentIds.length > 0
    && new Set(intent.baselineDeploymentIds).size === intent.baselineDeploymentIds.length
    && intent.baselineDeploymentIds.every((id) => UUID.test(id))
    && Number.isFinite(Date.parse(intent.armedAt || "")),
  "release state exact-redeploy binding is invalid");
  const expected = expectedRedeployBinding(state, intent.purpose);
  assert(intent.sourceDeploymentId === expected.sourceDeploymentId
    && canonicalDigest(intent.expectedImageDigest)
      === canonicalDigest(expected.expectedImageDigest),
  "release state exact-redeploy source or digest drifted");
  if (RECOVERY_REDEPLOY_PURPOSES.has(intent.purpose)) {
    assert(state.recovery?.sourceDeploymentId === intent.sourceDeploymentId
      && canonicalDigest(state.recovery?.expectedImageDigest)
        === canonicalDigest(intent.expectedImageDigest),
    "release state recovery intent drifted");
  }
  if (intent.purpose === "enforce" || intent.purpose === "same_commit_shadow_recovery") {
    assert(state.rollbackBoundary === "same_commit_shadow_only"
      && state.fundedAttemptMayHaveStarted === true,
    "post-boundary exact-redeploy intent crossed back to legacy recovery");
  } else {
    assert(state.rollbackBoundary === "legacy_allowed_before_enforce"
      && state.fundedAttemptMayHaveStarted !== true,
    "pre-boundary exact-redeploy intent crossed the funded-attempt boundary");
  }
  return intent;
}

async function prepareKernelLockFile(lockPath, label) {
  await mkdir(dirname(lockPath), { recursive: true, mode: 0o700 });
  const lock = await open(lockPath, "a+", 0o600);
  try {
    const details = await lock.stat();
    assert(details.isFile() && (details.mode & 0o777) === 0o600,
      `${label} kernel lock is not a mode-0600 regular file`);
    if (typeof process.getuid === "function") {
      assert(details.uid === process.getuid(), `${label} kernel lock has a different owner`);
    }
    return lock;
  } catch (error) {
    await lock.close().catch(() => {});
    throw error;
  }
}

function kernelLockCommand() {
  if (process.platform === "darwin") {
    return ["/usr/bin/lockf", ["-s", "-t", "0", "3"]];
  }
  if (process.platform === "linux") {
    return ["flock", ["-n", "3"]];
  }
  fail(`kernel-backed release locking is unsupported on ${process.platform}`);
}

async function acquireKernelLock(lockPath, label) {
  const lock = await prepareKernelLockFile(lockPath, label);
  const [command, args] = kernelLockCommand();
  const child = spawn(command, args, {
    shell: false,
    stdio: ["ignore", "pipe", "pipe", lock.fd],
  });
  let stderr = "";
  const result = await new Promise((resolveResult, rejectResult) => {
    const timer = setTimeout(() => {
      child.kill("SIGTERM");
      rejectResult(new Error(`timed out acquiring the ${label} kernel lock`));
    }, 5_000);
    child.once("error", (error) => {
      clearTimeout(timer);
      rejectResult(error);
    });
    child.stderr.on("data", (chunk) => { stderr += chunk.toString("utf8"); });
    child.once("exit", (code, signal) => {
      clearTimeout(timer);
      resolveResult({ code, signal });
    });
  }).catch(async (error) => {
    await lock.close().catch(() => {});
    throw error;
  });
  if (result.code !== 0 || result.signal != null) {
    await lock.close();
    fail(`another release controller holds the ${label} lock`
      + (stderr.trim() ? `: ${stderr.trim()}` : ` (lock attempt exited ${result.code})`));
  }
  try {
    await lock.truncate(0);
    await lock.writeFile(`${process.pid}\n`, "utf8");
    await lock.sync();
  } catch (error) {
    await lock.close().catch(() => {});
    throw error;
  }
  return lock;
}

async function withExclusiveLock(lockPath, label, operation) {
  const lock = await acquireKernelLock(lockPath, label);
  try {
    return await operation();
  } finally {
    await lock.close();
  }
}

async function setVariable(deps, name, value) {
  const result = await deps.run([
    "railway", "variable", "set", name, "--stdin", "--skip-deploys", "--json",
    "--project", TARGET.project, "--environment", TARGET.environment, "--service", TARGET.service,
  ], { stdin: `${value}\n` });
  commandOutput(result, `Railway ${name} staging`);
}

async function stageVariables(deps, values) {
  for (const [name, value] of Object.entries(values)) await setVariable(deps, name, value);
}

function parseUploadDeploymentId(output) {
  try {
    const parsed = JSON.parse(output.trim());
    return UUID.test(parsed?.deploymentId || "") ? parsed.deploymentId : null;
  } catch {
    return null;
  }
}

async function waitForDeployment(deps, id, options) {
  const deadline = deps.now() + options.timeoutMs;
  let lastStatus = "not visible";
  while (deps.now() < deadline) {
    const exact = await getExactDeployment(deps, id);
    if (!exact) {
      await deps.sleep(options.pollMs);
      continue;
    }
    lastStatus = String(exact.status || "").toUpperCase();
    if (lastStatus === "SUCCESS") {
      assert(runningSuccess(exact), "successful deployment has no single running instance");
      assert(DIGEST.test(imageDigest(exact)), "successful deployment has no immutable image digest");
      return exact;
    }
    if (TERMINAL_FAILURES.has(lastStatus)) fail(`deployment ${id} failed with ${lastStatus}`);
    assert(IN_FLIGHT.has(lastStatus), `deployment ${id} has unknown status ${lastStatus}`);
    await deps.sleep(options.pollMs);
  }
  fail(`timed out waiting for deployment ${id}: ${lastStatus}`);
}

async function runAudit(deps, state, mode, options, checks) {
  const exactArchive = await prepareExactArchive(deps, state.artifactRoot, state.commit);
  let result;
  try {
    result = await deps.run([
      process.execPath,
      resolve(exactArchive.sourceRoot, "scripts/audit_coinbase_hotfix.mjs"),
      "--mode", mode,
      "--deployment-id", state.current.deploymentId,
      "--commit", state.commit,
      "--expected-image-digest", state.current.imageDigest,
      "--expected-solana-pay-to", state.paymentRecipients.solana,
      "--expected-base-pay-to", state.paymentRecipients.base,
      "--checks", String(checks),
      "--interval-seconds", checks > 1 ? String(options.soakSeconds) : "0",
    ], { timeoutMs: Math.max(options.timeoutMs, (options.soakSeconds + 120) * 1000) });
  } finally {
    await exactArchive.cleanup();
  }
  const output = commandOutput(result, `${mode} hosted audit`).trim();
  const payload = parseJsonOutput(output.split(/\r?\n/).at(-1), `${mode} hosted audit`);
  assert(payload?.passed === true && payload?.mode === mode, `${mode} hosted audit did not attest success`);
  return payload;
}

async function preflight(options, deps) {
  await access(options.stateFile).then(
    () => fail("release state already exists; archive it before a new preflight"),
    (error) => {
      if (error?.code !== "ENOENT") throw error;
    },
  );
  const artifactTree = await assertExactGitHead(deps, options.artifactRoot, options.commit);
  await resolveTarget(deps);
  const domains = await getDomains(deps);
  await assertNoTriggers(deps);
  // Shadow upload, identity-bound shadow redeploy, enforce redeploy, and one
  // recovery redeploy must all fit without hitting Railway's 1000-row cap.
  const history = await listDeployments(deps, 4);
  assert(!history.some((row) => IN_FLIGHT.has(String(row?.status || "").toUpperCase())),
    "production already has an in-flight deployment");
  const prior = await assertOneActive(deps, LEGACY.deploymentId, LEGACY.imageDigest);
  assert(prior.canRollback === true && UUID.test(prior.snapshotId || ""),
    "legacy deployment is not image-rollback capable");
  const exactPrior = await getExactDeployment(deps, LEGACY.deploymentId);
  assertRollbackCapableSource(
    exactPrior,
    LEGACY.deploymentId,
    LEGACY.imageDigest,
    "exact legacy deployment",
  );
  assert(exactPrior.snapshotId === prior.snapshotId,
    "active and exact legacy deployment snapshots disagree");
  const backup = await getBackupEvidence(deps, options.volumeInstanceId, deps.now());
  const state = {
    schemaVersion: 1,
    phase: "preflight_passed",
    rollbackBoundary: "legacy_allowed_before_enforce",
    fundedExecutionSupported: false,
    volumeRestoreAllowed: false,
    target: TARGET,
    domains,
    commit: options.commit,
    artifactTree,
    artifactRoot: options.artifactRoot,
    legacy: LEGACY,
    backup,
    paymentRecipients: options.paymentRecipients,
    deploymentHistoryCount: history.length,
    current: { deploymentId: LEGACY.deploymentId, imageDigest: LEGACY.imageDigest, mode: "legacy" },
    candidate: null,
    createdAt: new Date(deps.now()).toISOString(),
    updatedAt: new Date(deps.now()).toISOString(),
  };
  await atomicWriteState(options.stateFile, state);
  return state;
}

async function deployShadow(options, deps) {
  const state = await readState(options.stateFile);
  assert(state.phase === "preflight_passed", "deploy-shadow requires a fresh passed preflight");
  assert(state.commit === options.commit, "deploy-shadow commit differs from preflight");
  const artifactTree = await assertExactGitHead(deps, options.artifactRoot, options.commit);
  assert(state.artifactTree === artifactTree, "artifact Git tree differs from preflight");
  assert(state.artifactRoot === options.artifactRoot, "artifact root differs from preflight");
  await resolveTarget(deps);
  await getDomains(deps);
  await assertNoTriggers(deps);
  await assertOneActive(deps, LEGACY.deploymentId, LEGACY.imageDigest);
  const before = await listDeployments(deps, 4);
  assert(!before.some((row) => IN_FLIGHT.has(String(row?.status || "").toUpperCase())),
    "production acquired an in-flight deployment after preflight");
  state.backup = await getBackupEvidence(
    deps,
    state.backup.volumeInstanceId,
    deps.now(),
  );
  state.backupRevalidatedAt = new Date(deps.now()).toISOString();
  state.phase = "shadow_variables_staging";
  state.updatedAt = new Date(deps.now()).toISOString();
  await atomicWriteState(options.stateFile, state);
  await stageVariables(deps, {
    ...SHADOW_VARIABLES,
    RELEASE_GIT_COMMIT: state.commit,
  });
  await assertOneActive(deps, LEGACY.deploymentId, LEGACY.imageDigest);
  const finalRows = await listDeployments(deps, 4);
  assert(JSON.stringify(finalRows.map((row) => row.id).sort())
    === JSON.stringify(before.map((row) => row.id).sort()),
  "deployment history changed while shadow variables were staged");
  const message = `coinbase-x402-shadow:${state.commit}:${randomUUID()}`;
  state.phase = "shadow_upload_armed";
  state.uploadMessage = message;
  state.uploadPriorActiveDeploymentId = LEGACY.deploymentId;
  state.uploadBaselineDeploymentIds = before.map((row) => row.id);
  state.uploadArmedAt = new Date(deps.now()).toISOString();
  state.updatedAt = new Date(deps.now()).toISOString();
  await atomicWriteState(options.stateFile, state);
  const exactArchive = await prepareExactArchive(deps, options.artifactRoot, state.commit);
  let upload;
  try {
    upload = await deps.run([
      "railway", "up", exactArchive.sourceRoot, "--detach", "--json", "--message", message,
      "--project", TARGET.project, "--environment", TARGET.environment, "--service", TARGET.service,
    ], { cwd: exactArchive.sourceRoot, timeoutMs: 180_000 });
  } finally {
    await exactArchive.cleanup();
  }
  const uploadOutput = commandOutput(upload, "Railway shadow upload");
  let deploymentId = parseUploadDeploymentId(uploadOutput);
  if (!deploymentId) {
    const after = await listDeployments(deps);
    const beforeIds = new Set(before.map((row) => row.id));
    const matches = after.filter((row) => !beforeIds.has(row.id) && row?.meta?.cliMessage === message);
    assert(matches.length === 1, "shadow upload did not bind exactly one new deployment id");
    deploymentId = matches[0].id;
  }
  assert(!before.some((row) => row.id === deploymentId), "shadow upload returned an old deployment id");
  state.candidate = {
    buildDeploymentId: deploymentId,
    imageDigest: null,
    shadowDeploymentId: null,
  };
  state.uploadSource = {
    kind: "git_archive",
    commit: state.commit,
    tree: state.artifactTree,
    ignoredWorkspaceFilesIncluded: false,
  };
  state.current = { deploymentId, imageDigest: null, mode: "shadow_pending" };
  state.phase = "shadow_deployment_bound";
  state.updatedAt = new Date(deps.now()).toISOString();
  await atomicWriteState(options.stateFile, state);
  const exact = await waitForDeployment(deps, deploymentId, options);
  const digest = imageDigest(exact);
  assert(digest !== LEGACY.imageDigest, "hotfix candidate unexpectedly reused the legacy image");
  state.candidate.imageDigest = digest;
  state.current = { deploymentId, imageDigest: digest, mode: "shadow_identity_unbound" };
  state.phase = "shadow_identity_variable_staging";
  state.updatedAt = new Date(deps.now()).toISOString();
  await atomicWriteState(options.stateFile, state);

  // Railway's immutable image digest only exists after the source upload.  Bind
  // it into the runtime by staging the observed digest without a deploy, then
  // redeploy that exact deployment ID/image.  The hosted audit runs only on
  // this second, fully identity-bound shadow revision.
  await setVariable(deps, "RELEASE_IMAGE_DIGEST", digest);
  await waitForOneActiveConvergence(
    deps,
    deploymentId,
    digest,
    options,
    [LEGACY.deploymentId],
  );
  await armExactRedeploy(
    state,
    options,
    deps,
    "shadow_identity",
    deploymentId,
    digest,
  );
  const shadowDeploymentId = await exactRedeploy(deps, deploymentId);
  bindAcceptedRedeploy(state, shadowDeploymentId, false, deps.now());
  state.updatedAt = new Date(deps.now()).toISOString();
  await atomicWriteState(options.stateFile, state);
  const shadowExact = await waitForDeployment(deps, shadowDeploymentId, options);
  assert(imageDigest(shadowExact) === digest,
    "identity-bound shadow redeploy did not use the exact uploaded image");
  state.current = { deploymentId: shadowDeploymentId, imageDigest: digest, mode: "shadow" };
  state.phase = "shadow_soak_pending";
  state.updatedAt = new Date(deps.now()).toISOString();
  await atomicWriteState(options.stateFile, state);
  await waitForOneActiveConvergence(
    deps,
    shadowDeploymentId,
    digest,
    options,
    [deploymentId],
  );
  const audit = await runAudit(deps, state, "shadow", options, 2);
  state.phase = "shadow_validated";
  state.shadowAudit = audit;
  state.updatedAt = new Date(deps.now()).toISOString();
  await atomicWriteState(options.stateFile, state);
  return state;
}

async function exactRedeploy(deps, sourceDeploymentId) {
  const data = await railwayApi(
    deps,
    "mutation DirectProdRedeploy($id:String!){deploymentRedeploy(id:$id,usePreviousImageTag:true){id status}}",
    { id: sourceDeploymentId },
  );
  const result = data?.deploymentRedeploy;
  assert(UUID.test(result?.id || "") && result.id !== sourceDeploymentId,
    "exact image redeploy did not return one new deployment id");
  return result.id;
}

async function exactRollback(deps, sourceDeploymentId) {
  const data = await railwayApi(
    deps,
    "mutation DirectProdRollback($id:String!){deploymentRollback(id:$id){id status}}",
    { id: sourceDeploymentId },
  );
  const result = data?.deploymentRollback;
  assert(UUID.test(result?.id || "") && result.id !== sourceDeploymentId,
    "exact deployment rollback did not return one new deployment id");
  return result.id;
}

async function dispatchArmedDeploymentMutation(deps, intent) {
  if (intent.mutationKind === "rollback") {
    return exactRollback(deps, intent.sourceDeploymentId);
  }
  assert(intent.mutationKind === "redeploy", "unknown armed deployment mutation kind");
  return exactRedeploy(deps, intent.sourceDeploymentId);
}

async function armExactRedeploy(
  state,
  options,
  deps,
  purpose,
  sourceDeploymentId,
  expectedImageDigest,
) {
  assert(Object.hasOwn(REDEPLOY_PHASE_BY_PURPOSE, purpose),
    "cannot arm an unknown exact-redeploy purpose");
  const digest = canonicalDigest(expectedImageDigest);
  assert(UUID.test(sourceDeploymentId || "") && DIGEST.test(digest),
    "cannot arm an exact redeploy without an immutable source");
  const source = await getExactDeployment(deps, sourceDeploymentId);
  const recoveryMutation = RECOVERY_REDEPLOY_PURPOSES.has(purpose);
  assert(source && imageDigest(source) === digest && UUID.test(source.snapshotId || ""),
    "deployment mutation source is not the reviewed immutable snapshot");
  if (recoveryMutation) {
    assert(source.canRollback === true
      && ROLLBACK_SOURCE_STATUSES.has(String(source.status || "").toUpperCase()),
    "recovery source is not an exact rollback-capable historical deployment");
  } else {
    assert(runningSuccess(source),
      "exact-redeploy source is not the active reviewed running-success image");
  }
  const active = await getActive(deps);
  assert(active.length <= 1, "cannot arm an exact redeploy with multiple active deployments");
  if (recoveryMutation && active.length === 1) {
    assertKnownActiveIdentity(
      state,
      active[0],
      active[0].id,
      "recovery deployment-mutation baseline",
    );
    assert(!IN_FLIGHT.has(String(active[0].status || "").toUpperCase()),
      "recovery exact redeploy has an in-flight active baseline");
  } else if (!recoveryMutation) {
    assert(active.length === 1
      && active[0].id === sourceDeploymentId
      && active[0].snapshotId === source.snapshotId
      && imageDigest(active[0]) === digest
      && runningSuccess(active[0]),
    "exact-redeploy source is no longer the sole reviewed active deployment");
  }
  const history = await listDeployments(deps, 1);
  assert(history.every((row) => !IN_FLIGHT.has(String(row.status || "").toUpperCase())),
    "cannot arm an exact redeploy while deployment history is in flight");
  const finalActive = await getActive(deps);
  assert(finalActive.length === active.length
    && finalActive.every((row, index) => row.id === active[index]?.id),
  "active deployment changed while exact-redeploy intent was being armed");
  if (recoveryMutation && finalActive.length === 1) {
    assertKnownActiveIdentity(
      state,
      finalActive[0],
      finalActive[0].id,
      "final recovery deployment-mutation baseline",
    );
    assert(!IN_FLIGHT.has(String(finalActive[0].status || "").toUpperCase()),
      "final recovery baseline became in flight");
  } else if (!recoveryMutation) {
    assert(finalActive.length === 1
      && finalActive[0].id === sourceDeploymentId
      && finalActive[0].snapshotId === source.snapshotId
      && imageDigest(finalActive[0]) === digest
      && runningSuccess(finalActive[0]),
    "exact-redeploy active source drifted while intent was being armed");
  }
  state.pendingRedeploy = {
    purpose,
    mutationKind: recoveryMutation ? "rollback" : "redeploy",
    sourceDeploymentId,
    sourceSnapshotId: source.snapshotId,
    expectedImageDigest: digest,
    expectedCommit: state.commit,
    priorActiveDeploymentId: finalActive[0]?.id || null,
    baselineDeploymentIds: history.map((row) => row.id),
    armedAt: new Date(deps.now()).toISOString(),
  };
  state.phase = REDEPLOY_PHASE_BY_PURPOSE[purpose];
  state.updatedAt = new Date(deps.now()).toISOString();
  validateRedeployIntent(state);
  await atomicWriteState(options.stateFile, state);
  return state.pendingRedeploy;
}

function bindAcceptedRedeploy(state, deploymentId, reconciled, now) {
  const intent = validateRedeployIntent(state);
  assert(UUID.test(deploymentId || "")
    && deploymentId !== intent.sourceDeploymentId
    && deploymentId !== intent.priorActiveDeploymentId
    && !intent.baselineDeploymentIds.includes(deploymentId),
  "exact redeploy did not bind one new deployment id");
  if (intent.purpose === "shadow_identity") {
    state.candidate.shadowDeploymentId = deploymentId;
    state.current = {
      deploymentId,
      imageDigest: intent.expectedImageDigest,
      mode: "shadow_pending",
    };
    state.phase = "shadow_identity_deployment_bound";
  } else if (intent.purpose === "enforce") {
    state.enforceDeploymentId = deploymentId;
    state.current = {
      deploymentId,
      imageDigest: intent.expectedImageDigest,
      mode: "enforce_pending",
    };
    state.phase = "enforce_deployment_bound";
  } else {
    const mode = intent.purpose === "legacy_recovery" ? "legacy" : "shadow";
    state.recovery.deploymentId = deploymentId;
    state.current = {
      deploymentId,
      imageDigest: intent.expectedImageDigest,
      mode: `${mode}_pending`,
    };
    state.phase = "recovery_deployment_bound";
  }
  state.lastAcceptedRedeploy = {
    purpose: intent.purpose,
    mutationKind: intent.mutationKind,
    sourceDeploymentId: intent.sourceDeploymentId,
    sourceSnapshotId: intent.sourceSnapshotId,
    expectedImageDigest: intent.expectedImageDigest,
    expectedCommit: intent.expectedCommit,
    priorActiveDeploymentId: intent.priorActiveDeploymentId,
    baselineDeploymentCount: intent.baselineDeploymentIds.length,
    armedAt: intent.armedAt,
    deploymentId,
    reconciledAfterCrash: reconciled,
    boundAt: new Date(now).toISOString(),
  };
  delete state.pendingRedeploy;
}

async function promoteEnforce(options, deps) {
  const state = await readState(options.stateFile);
  assert(state.phase === "shadow_validated", "promote-enforce requires a validated shadow soak");
  assert(state.commit === options.commit, "promote-enforce commit differs from shadow");
  await assertExactGitHead(deps, state.artifactRoot, options.commit);
  await resolveTarget(deps);
  await getDomains(deps);
  await assertNoTriggers(deps);
  const activeShadow = await assertOneActive(
    deps,
    state.candidate.shadowDeploymentId,
    state.candidate.imageDigest,
  );
  assertRollbackCapableSource(
    activeShadow,
    state.candidate.shadowDeploymentId,
    state.candidate.imageDigest,
    "validated shadow active deployment",
  );
  const exactShadow = await getExactDeployment(deps, state.candidate.shadowDeploymentId);
  assertRollbackCapableSource(
    exactShadow,
    state.candidate.shadowDeploymentId,
    state.candidate.imageDigest,
    "validated shadow exact deployment",
  );
  assert((await listDeployments(deps, 2)).every(
    (row) => !IN_FLIGHT.has(String(row.status).toUpperCase()),
  ),
    "production has an in-flight deployment before enforce promotion");
  const finalShadow = await assertOneActive(
    deps,
    state.candidate.shadowDeploymentId,
    state.candidate.imageDigest,
  );
  assertRollbackCapableSource(
    finalShadow,
    state.candidate.shadowDeploymentId,
    state.candidate.imageDigest,
    "validated shadow final active deployment",
  );
  assertRollbackCapableSource(
    await getExactDeployment(deps, state.candidate.shadowDeploymentId),
    state.candidate.shadowDeploymentId,
    state.candidate.imageDigest,
    "validated shadow final exact deployment",
  );

  // Close the legacy rollback boundary before the first enforce mutation.  From
  // here onward a payment may arrive even though this tool never submits one.
  state.phase = "enforce_promotion_armed";
  state.rollbackBoundary = "same_commit_shadow_only";
  state.fundedAttemptMayHaveStarted = true;
  state.updatedAt = new Date(deps.now()).toISOString();
  await atomicWriteState(options.stateFile, state);

  await stageVariables(deps, {
    ...SHADOW_VARIABLES,
    X402_PAYMENT_MODE: "enforce",
    RELEASE_GIT_COMMIT: state.commit,
    RELEASE_IMAGE_DIGEST: state.candidate.imageDigest,
  });
  await assertOneActive(deps, state.candidate.shadowDeploymentId, state.candidate.imageDigest);
  await armExactRedeploy(
    state,
    options,
    deps,
    "enforce",
    state.candidate.shadowDeploymentId,
    state.candidate.imageDigest,
  );
  const deploymentId = await exactRedeploy(deps, state.candidate.shadowDeploymentId);
  bindAcceptedRedeploy(state, deploymentId, false, deps.now());
  state.updatedAt = new Date(deps.now()).toISOString();
  await atomicWriteState(options.stateFile, state);
  const exact = await waitForDeployment(deps, deploymentId, options);
  assert(imageDigest(exact) === state.candidate.imageDigest,
    "enforce redeploy did not use the exact shadow image");
  state.current = { deploymentId, imageDigest: state.candidate.imageDigest, mode: "enforce" };
  state.phase = "enforce_unfunded_audit_pending";
  state.updatedAt = new Date(deps.now()).toISOString();
  await atomicWriteState(options.stateFile, state);
  await waitForOneActiveConvergence(
    deps,
    deploymentId,
    state.candidate.imageDigest,
    options,
    [state.candidate.shadowDeploymentId],
  );
  const audit = await runAudit(deps, state, "enforce", options, 1);
  state.phase = "enforce_unfunded_validated";
  state.enforceAudit = audit;
  state.updatedAt = new Date(deps.now()).toISOString();
  await atomicWriteState(options.stateFile, state);
  return state;
}

function knownDeploymentIds(state) {
  return new Set([
    LEGACY.deploymentId,
    state.candidate?.buildDeploymentId,
    state.candidate?.shadowDeploymentId,
    state.enforceDeploymentId,
    state.recovery?.deploymentId,
    state.current?.deploymentId,
    state.lastAcceptedUpload?.deploymentId,
    state.lastAcceptedRedeploy?.deploymentId,
    state.lastFailedMutation?.deploymentId,
    state.lastFailedRecovery?.deploymentId,
    ...(state.recovery?.failedAttempts || []).map((attempt) => attempt?.deploymentId),
  ].filter(Boolean));
}

function expectedKnownImageDigest(state, deploymentId) {
  if (deploymentId === LEGACY.deploymentId) return LEGACY.imageDigest;
  if ([
    state.candidate?.buildDeploymentId,
    state.candidate?.shadowDeploymentId,
    state.enforceDeploymentId,
  ].includes(deploymentId) && DIGEST.test(canonicalDigest(state.candidate?.imageDigest))) {
    return canonicalDigest(state.candidate.imageDigest);
  }
  for (const binding of [
    state.lastAcceptedUpload,
    state.lastAcceptedRedeploy,
    state.lastFailedMutation,
    state.lastFailedRecovery,
    ...(state.recovery?.failedAttempts || []),
  ]) {
    const digest = canonicalDigest(
      binding?.expectedImageDigest || binding?.imageDigest || "",
    );
    if (binding?.deploymentId === deploymentId && DIGEST.test(digest)) return digest;
  }
  if (state.current?.deploymentId === deploymentId
    && DIGEST.test(canonicalDigest(state.current?.imageDigest))) {
    return canonicalDigest(state.current.imageDigest);
  }
  return null;
}

function assertKnownActiveIdentity(state, row, expectedId, label) {
  assert(row?.id === expectedId
    && knownDeploymentIds(state).has(expectedId)
    && UUID.test(row?.snapshotId || ""),
  `${label} is not an exact known deployment`);
  const expectedDigest = expectedKnownImageDigest(state, expectedId);
  if (expectedDigest) {
    assert(imageDigest(row) === expectedDigest, `${label} image digest drifted`);
    return row;
  }
  const failedUpload = state.lastFailedMutation;
  if (failedUpload?.kind === "shadow_upload"
    && failedUpload.deploymentId === expectedId
    && failedUpload.expectedImageDigest == null) {
    const status = String(row?.status || "").toUpperCase();
    assert(TERMINAL_FAILURES.has(status)
      && status === failedUpload.providerStatus
      && status === failedUpload.status
      && row?.meta?.cliMessage === failedUpload.message
      && failedUpload.message === state.uploadMessage
      && failedUpload.commit === state.commit
      && state.rollbackBoundary === "legacy_allowed_before_enforce"
      && state.fundedAttemptMayHaveStarted !== true
      && !imageDigest(row)
      && (row.instances || []).every(
        (instance) => String(instance?.status || "").toUpperCase() !== "RUNNING",
      ),
    `${label} is not the exact stopped no-image upload failure`);
    return row;
  }
  assert(expectedId === state.candidate?.buildDeploymentId
    && row?.meta?.cliMessage === state.uploadMessage
    && state.uploadSource?.commit === state.commit
    && state.uploadSource?.tree === state.artifactTree
    && DIGEST.test(imageDigest(row))
    && imageDigest(row) !== LEGACY.imageDigest,
  `${label} has no immutable reviewed image binding`);
  return row;
}

function assertDeploymentCreatedForIntent(row, armedAt, now, label) {
  const createdAt = Date.parse(row?.createdAt || "");
  const armed = Date.parse(armedAt || "");
  assert(Number.isFinite(createdAt) && Number.isFinite(armed)
    && createdAt >= armed - 5 * 60 * 1000
    && createdAt <= now + 2 * 60 * 1000,
  `${label} deployment timestamp is outside the armed intent window`);
}

async function uniquePostIntentDeployment(deps, baselineIds, label) {
  const history = await listDeployments(deps, 1);
  const currentIds = new Set(history.map((row) => row.id));
  assert(baselineIds.every((id) => currentIds.has(id)),
    `${label} deployment-history baseline is no longer complete`);
  const baseline = new Set(baselineIds);
  const additions = history.filter((row) => !baseline.has(row.id));
  assert(additions.length === 1,
    additions.length === 0
      ? `${label} mutation outcome is not yet observable; refusing to retry`
      : `${label} mutation has an ambiguous deployment-history delta`);
  return additions[0];
}

function rememberFailedRecovery(state, record) {
  const attempts = Array.isArray(state.recovery?.failedAttempts)
    ? state.recovery.failedAttempts
    : [];
  state.recovery.failedAttempts = [...attempts, record];
  state.lastFailedRecovery = record;
}

function failedDeploymentOutcome(row) {
  const status = String(row?.status || "").toUpperCase();
  if (RETRYABLE_RECOVERY_FAILURES.has(status)) return status;
  if (status === "SUCCESS" && !runningSuccess(row)) return "SUCCESS_UNHEALTHY";
  return null;
}

function preserveObservedActive(state, active) {
  if (active.length === 0) {
    state.current = null;
    return;
  }
  const row = active[0];
  const priorMode = state.current?.deploymentId === row.id
    ? state.current.mode
    : "known_recovery_baseline";
  state.current = {
    deploymentId: row.id,
    imageDigest: imageDigest(row) || null,
    mode: priorMode,
  };
}

function assertIntentBaselineStillActive(
  state,
  active,
  priorActiveDeploymentId,
  label,
  { allowZero = false } = {},
) {
  if (allowZero && active.length === 0) return;
  if (priorActiveDeploymentId === null) {
    assert(active.length === 0, `${label} unexpectedly has an active deployment`);
    return;
  }
  assert(active.length === 1
    && !IN_FLIGHT.has(String(active[0].status || "").toUpperCase()),
  `${label} no longer has its exact known non-in-flight active baseline`);
  assertKnownActiveIdentity(state, active[0], priorActiveDeploymentId, label);
}

function assertFailedOutcomeActive(
  state,
  active,
  failedDeploymentId,
  priorActiveDeploymentId,
  outcome,
  label,
  { allowTerminalFailedActive = false, expectedImageDigest = null } = {},
) {
  if (outcome !== "SUCCESS_UNHEALTHY") {
    if (active.length === 1 && active[0].id === failedDeploymentId) {
      assert(allowTerminalFailedActive
        && String(active[0].status || "").toUpperCase() === outcome
        && !IN_FLIGHT.has(outcome)
        && !runningSuccess(active[0])
        && (!expectedImageDigest
          || imageDigest(active[0]) === canonicalDigest(expectedImageDigest))
        && (active[0].instances || []).every(
          (instance) => String(instance?.status || "").toUpperCase() !== "RUNNING",
        ),
      `${label} terminal active deployment is not the exact stopped failed attempt`);
      return;
    }
    assert(!active.some((row) => row.id === failedDeploymentId),
      `${label} terminal deployment is still active; refusing to retry`);
    assertIntentBaselineStillActive(
      state,
      active,
      priorActiveDeploymentId,
      label,
      { allowZero: true },
    );
    return;
  }
  if (active.length === 0) return;
  assert(active.length === 1
    && !IN_FLIGHT.has(String(active[0].status || "").toUpperCase()),
  `${label} has an ambiguous or in-flight active deployment`);
  if (active[0].id === failedDeploymentId) {
    assert(String(active[0].status || "").toUpperCase() === "SUCCESS"
      && !runningSuccess(active[0])
      && (!expectedImageDigest
        || imageDigest(active[0]) === canonicalDigest(expectedImageDigest))
      && (active[0].instances || []).every(
        (instance) => String(instance?.status || "").toUpperCase() !== "RUNNING",
      ),
    `${label} active outcome is not the exact unhealthy deployment`);
    return;
  }
  assert(active[0].id === priorActiveDeploymentId
    && knownDeploymentIds(state).has(active[0].id),
  `${label} no longer has its exact known active baseline`);
}

async function reconcileArmedMutation(state, active, options, deps) {
  assert(active.length <= 1, "recovery refuses an ambiguous multi-active target");
  if (state.phase === "shadow_upload_armed") {
    const message = validateUploadIntent(state);
    const addition = await uniquePostIntentDeployment(
      deps,
      state.uploadBaselineDeploymentIds,
      "shadow upload",
    );
    assert(addition?.meta?.cliMessage === message,
      "post-intent deployment is not the uniquely marked shadow upload");
    assertDeploymentCreatedForIntent(addition, state.uploadArmedAt, deps.now(), "shadow upload");
    const exact = await getExactDeployment(deps, addition.id);
    const providerStatus = String(exact?.status || "").toUpperCase();
    const outcome = failedDeploymentOutcome(exact);
    if (outcome) {
      const observedDigest = imageDigest(exact);
      const stopped = (exact?.instances || []).every(
        (instance) => String(instance?.status || "").toUpperCase() !== "RUNNING",
      );
      assertFailedOutcomeActive(
        state,
        active,
        addition.id,
        state.uploadPriorActiveDeploymentId,
        outcome,
        "failed shadow upload",
        {
          allowTerminalFailedActive: true,
          expectedImageDigest: observedDigest || null,
        },
      );
      assert(exact?.id === addition.id
        && exact?.meta?.cliMessage === message
        && stopped
        && (observedDigest
          ? DIGEST.test(observedDigest) && observedDigest !== LEGACY.imageDigest
          : TERMINAL_FAILURES.has(providerStatus)),
      "failed shadow upload is not the exact stopped marker-bound build outcome");
      state.lastFailedMutation = {
        kind: "shadow_upload",
        deploymentId: addition.id,
        status: outcome,
        providerStatus,
        message,
        commit: state.commit,
        expectedImageDigest: observedDigest || null,
        reconciledAt: new Date(deps.now()).toISOString(),
      };
      state.phase = "shadow_upload_failed";
      preserveObservedActive(state, active);
      state.updatedAt = new Date(deps.now()).toISOString();
      await atomicWriteState(options.stateFile, state);
      return state;
    }
    assert(active.length === 1 && addition.id === active[0].id
      && exact?.id === active[0].id && runningSuccess(exact) && runningSuccess(active[0]),
    "marked shadow upload is not the exact active running-success deployment");
    const digest = imageDigest(exact);
    assert(DIGEST.test(digest) && digest !== LEGACY.imageDigest,
      "reconciled shadow upload has no new immutable image");
    state.candidate = {
      buildDeploymentId: addition.id,
      imageDigest: digest,
      shadowDeploymentId: null,
    };
    state.uploadSource = {
      kind: "git_archive",
      commit: state.commit,
      tree: state.artifactTree,
      ignoredWorkspaceFilesIncluded: false,
    };
    state.current = { deploymentId: addition.id, imageDigest: digest, mode: "shadow_pending" };
    state.phase = "shadow_deployment_bound";
    state.lastAcceptedUpload = {
      deploymentId: addition.id,
      message,
      commit: state.commit,
      imageDigest: digest,
      reconciledAfterCrash: true,
      boundAt: new Date(deps.now()).toISOString(),
    };
  } else {
    const intent = validateRedeployIntent(state);
    const addition = await uniquePostIntentDeployment(
      deps,
      intent.baselineDeploymentIds,
      `${intent.purpose} exact redeploy`,
    );
    assert(addition.id !== intent.sourceDeploymentId
      && addition.id !== intent.priorActiveDeploymentId,
    "post-intent exact redeploy did not create a new deployment id");
    assertDeploymentCreatedForIntent(
      addition,
      intent.armedAt,
      deps.now(),
      `${intent.purpose} exact redeploy`,
    );
    const source = await getExactDeployment(deps, intent.sourceDeploymentId);
    const exact = await getExactDeployment(deps, addition.id);
    const sourceStatus = String(source?.status || "").toUpperCase();
    assert(source
      && imageDigest(source) === canonicalDigest(intent.expectedImageDigest)
      && source.snapshotId === intent.sourceSnapshotId
      && !IN_FLIGHT.has(sourceStatus)
      && (intent.mutationKind === "rollback"
        ? source.canRollback === true && ROLLBACK_SOURCE_STATUSES.has(sourceStatus)
        : ["SUCCESS", "REMOVED", "CRASHED"].includes(sourceStatus)),
    "reconciled deployment source no longer matches its immutable snapshot");
    const providerStatus = String(exact?.status || "").toUpperCase();
    const outcome = failedDeploymentOutcome(exact);
    if (outcome) {
      assertFailedOutcomeActive(
        state,
        active,
        addition.id,
        intent.priorActiveDeploymentId,
        outcome,
        `failed ${intent.purpose} exact redeploy`,
        {
          allowTerminalFailedActive: true,
          expectedImageDigest: intent.expectedImageDigest,
        },
      );
      assert(exact?.id === addition.id
        && imageDigest(exact) === canonicalDigest(intent.expectedImageDigest),
      "failed exact redeploy is target-drifted or image-drifted");
      const failure = {
        kind: "exact_redeploy",
        purpose: intent.purpose,
        mutationKind: intent.mutationKind,
        deploymentId: addition.id,
        sourceDeploymentId: intent.sourceDeploymentId,
        sourceSnapshotId: intent.sourceSnapshotId,
        expectedImageDigest: canonicalDigest(intent.expectedImageDigest),
        expectedCommit: state.commit,
        status: outcome,
        providerStatus,
        failedAt: new Date(deps.now()).toISOString(),
      };
      if (RECOVERY_REDEPLOY_PURPOSES.has(intent.purpose)) {
        rememberFailedRecovery(state, failure);
        state.phase = intent.purpose === "legacy_recovery"
          ? "legacy_recovery_armed"
          : "same_commit_shadow_recovery_armed";
      } else {
        state.lastFailedMutation = failure;
        state.phase = `${intent.purpose}_deployment_failed`;
      }
      delete state.pendingRedeploy;
      preserveObservedActive(state, active);
      state.updatedAt = new Date(deps.now()).toISOString();
      await atomicWriteState(options.stateFile, state);
      return state;
    }
    assert(active.length === 1 && exact && exact.id === active[0].id
      && addition.id === active[0].id && runningSuccess(exact) && runningSuccess(active[0])
      && imageDigest(exact) === canonicalDigest(intent.expectedImageDigest)
      && imageDigest(active[0]) === canonicalDigest(intent.expectedImageDigest),
    "reconciled exact redeploy does not match the intent-bound immutable image");
    bindAcceptedRedeploy(state, addition.id, true, deps.now());
    state.lastCrashReconciliation = {
      kind: "exact_redeploy",
      purpose: intent.purpose,
      mutationKind: intent.mutationKind,
      deploymentId: addition.id,
      sourceDeploymentId: intent.sourceDeploymentId,
      sourceSnapshotId: intent.sourceSnapshotId,
      expectedImageDigest: canonicalDigest(intent.expectedImageDigest),
      expectedCommit: state.commit,
      reconciledAt: new Date(deps.now()).toISOString(),
    };
  }
  state.updatedAt = new Date(deps.now()).toISOString();
  await atomicWriteState(options.stateFile, state);
  return state;
}

function recoveryBinding(state) {
  const afterBoundary = state.rollbackBoundary === "same_commit_shadow_only"
    || state.fundedAttemptMayHaveStarted === true;
  assert(afterBoundary
    ? state.rollbackBoundary === "same_commit_shadow_only"
      && state.fundedAttemptMayHaveStarted === true
    : state.rollbackBoundary === "legacy_allowed_before_enforce"
      && state.fundedAttemptMayHaveStarted !== true,
  "release state has an inconsistent funded-attempt rollback boundary");
  return {
    afterBoundary,
    sourceDeploymentId: afterBoundary
      ? state.candidate?.shadowDeploymentId
      : LEGACY.deploymentId,
    expectedImageDigest: canonicalDigest(afterBoundary
      ? state.candidate?.imageDigest
      : LEGACY.imageDigest),
    mode: afterBoundary ? "shadow" : "legacy",
    purpose: afterBoundary ? "same_commit_shadow_recovery" : "legacy_recovery",
  };
}

function assertRecoveryBinding(state, binding) {
  assert(UUID.test(binding.sourceDeploymentId || "")
    && DIGEST.test(binding.expectedImageDigest),
  "release state has no exact recovery image");
  assert(state.recovery?.sourceDeploymentId === binding.sourceDeploymentId
    && canonicalDigest(state.recovery?.expectedImageDigest) === binding.expectedImageDigest
    && state.recovery?.volumeRestoreAttempted === false,
  "persisted recovery no longer matches the phase-aware rollback image");
}

async function resetTerminalBoundRecovery(state, binding, active, options, deps) {
  assertRecoveryBinding(state, binding);
  const deploymentId = state.recovery?.deploymentId;
  assert(UUID.test(deploymentId || ""), "bound recovery has no exact deployment id");
  const exact = await getExactDeployment(deps, deploymentId);
  const providerStatus = String(exact?.status || "").toUpperCase();
  const outcome = failedDeploymentOutcome(exact);
  if (!outcome) return false;
  const accepted = state.lastAcceptedRedeploy;
  const source = await getExactDeployment(deps, binding.sourceDeploymentId);
  assert(exact?.id === deploymentId
    && imageDigest(exact) === binding.expectedImageDigest
    && accepted?.deploymentId === deploymentId
    && accepted?.purpose === binding.purpose
    && accepted?.mutationKind === "rollback"
    && accepted?.sourceDeploymentId === binding.sourceDeploymentId
    && accepted?.sourceSnapshotId === source?.snapshotId
    && canonicalDigest(accepted?.expectedImageDigest) === binding.expectedImageDigest
    && accepted?.expectedCommit === state.commit
    && imageDigest(source) === binding.expectedImageDigest
    && source?.canRollback === true
    && ROLLBACK_SOURCE_STATUSES.has(String(source?.status || "").toUpperCase()),
  "terminal bound recovery is not the exact intent-bound target image");
  assertFailedOutcomeActive(
    state,
    active,
    deploymentId,
    accepted.priorActiveDeploymentId,
    outcome,
    "terminal bound recovery",
    {
      allowTerminalFailedActive: true,
      expectedImageDigest: binding.expectedImageDigest,
    },
  );
  const failure = {
    kind: "deployment_rollback",
    purpose: binding.purpose,
    mutationKind: "rollback",
    deploymentId,
    sourceDeploymentId: binding.sourceDeploymentId,
    sourceSnapshotId: accepted.sourceSnapshotId,
    expectedImageDigest: binding.expectedImageDigest,
    expectedCommit: state.commit,
    status: outcome,
    providerStatus,
    failedAt: new Date(deps.now()).toISOString(),
  };
  rememberFailedRecovery(state, failure);
  delete state.recovery.deploymentId;
  preserveObservedActive(state, active);
  state.phase = binding.afterBoundary
    ? "same_commit_shadow_recovery_armed"
    : "legacy_recovery_armed";
  state.updatedAt = new Date(deps.now()).toISOString();
  await atomicWriteState(options.stateFile, state);
  return true;
}

async function finishBoundRecovery(state, binding, options, deps) {
  assert(state.phase === "recovery_deployment_bound"
    && UUID.test(state.recovery?.deploymentId || "")
    && state.current?.deploymentId === state.recovery.deploymentId,
  "recovery deployment is not durably bound");
  assertRecoveryBinding(state, binding);
  const deploymentId = state.recovery.deploymentId;
  const exact = await waitForDeployment(deps, deploymentId, options);
  assert(imageDigest(exact) === binding.expectedImageDigest, "recovery image digest changed");
  state.current = {
    deploymentId,
    imageDigest: binding.expectedImageDigest,
    mode: binding.mode,
  };
  const activeRecovery = await waitForOneActiveConvergence(
    deps,
    deploymentId,
    binding.expectedImageDigest,
    options,
    [state.lastAcceptedRedeploy?.priorActiveDeploymentId].filter(Boolean),
  );
  if (binding.afterBoundary) {
    state.recovery.audit = await runAudit(deps, state, "shadow", options, 1);
  } else {
    state.recovery.legacyIdentityAudit = {
      deploymentId,
      imageDigest: binding.expectedImageDigest,
      railwayStatus: String(activeRecovery.status || "").toUpperCase(),
      runningInstances: 1,
      domains: [...state.domains],
      rollbackRestoredSnapshotVariables: true,
      legacyX402MayRemainLive: true,
      auditedAt: new Date(deps.now()).toISOString(),
    };
  }
  state.phase = binding.afterBoundary ? "recovered_same_commit_shadow" : "recovered_legacy";
  state.recovery.completedAt = new Date(deps.now()).toISOString();
  state.updatedAt = new Date(deps.now()).toISOString();
  await atomicWriteState(options.stateFile, state);
  return state;
}

async function recover(options, deps) {
  let state = await readState(options.stateFile);
  await resolveTarget(deps);
  await getDomains(deps);
  await assertNoTriggers(deps);
  let active = await getActive(deps);
  assert(active.length <= 1, "recovery refuses an ambiguous multi-active target");
  if (state.phase === "shadow_upload_armed" || state.pendingRedeploy) {
    state = await reconcileArmedMutation(state, active, options, deps);
    active = await getActive(deps);
  }
  assert(!active[0]?.id || knownDeploymentIds(state).has(active[0].id),
    "recovery refuses an unknown active deployment");
  assert((await listDeployments(deps, 1)).every(
    (row) => !IN_FLIGHT.has(String(row.status).toUpperCase()),
  ), "recovery refuses while another deployment is in flight");

  const binding = recoveryBinding(state);
  if (state.phase === "recovery_deployment_bound") {
    if (!await resetTerminalBoundRecovery(state, binding, active, options, deps)) {
      return finishBoundRecovery(state, binding, options, deps);
    }
  }
  if (state.pendingRedeploy && RECOVERY_REDEPLOY_PURPOSES.has(
    state.pendingRedeploy.purpose,
  )) {
    validateRedeployIntent(state);
    fail("armed recovery mutation outcome is not uniquely observable; refusing to retry");
  }

  state.phase = binding.afterBoundary
    ? "same_commit_shadow_recovery_armed"
    : "legacy_recovery_armed";
  const failedAttempts = Array.isArray(state.recovery?.failedAttempts)
    ? state.recovery.failedAttempts
    : [];
  state.recovery = {
    reason: options.reason,
    sourceDeploymentId: binding.sourceDeploymentId,
    expectedImageDigest: binding.expectedImageDigest,
    volumeRestoreAttempted: false,
    rollbackRestoresSnapshotVariables: true,
    legacyX402MayRemainLive: !binding.afterBoundary,
    armedAt: new Date(deps.now()).toISOString(),
    ...(failedAttempts.length > 0 ? { failedAttempts } : {}),
  };
  delete state.pendingRedeploy;
  state.updatedAt = new Date(deps.now()).toISOString();
  await atomicWriteState(options.stateFile, state);
  if (binding.afterBoundary) {
    await stageVariables(deps, {
      ...SHADOW_VARIABLES,
      RELEASE_GIT_COMMIT: state.commit,
      RELEASE_IMAGE_DIGEST: state.candidate.imageDigest,
    });
  }
  await armExactRedeploy(
    state,
    options,
    deps,
    binding.purpose,
    binding.sourceDeploymentId,
    binding.expectedImageDigest,
  );
  const deploymentId = await dispatchArmedDeploymentMutation(deps, state.pendingRedeploy);
  bindAcceptedRedeploy(state, deploymentId, false, deps.now());
  state.updatedAt = new Date(deps.now()).toISOString();
  await atomicWriteState(options.stateFile, state);
  return finishBoundRecovery(state, binding, options, deps);
}

export async function executeReleaseCommand(argv, injected = {}) {
  const options = parseArguments(argv);
  const deps = {
    run: injected.run || defaultRun,
    now: injected.now || (() => Date.now()),
    sleep: injected.sleep || ((milliseconds) => new Promise((done) => setTimeout(done, milliseconds))),
  };
  return withExclusiveLock(TARGET_LOCK_PATH, "fixed production target", () => (
    withExclusiveLock(`${options.stateFile}.lock`, "release state", async () => {
      if (options.command === "preflight") return preflight(options, deps);
      if (options.command === "deploy-shadow") return deployShadow(options, deps);
      if (options.command === "promote-enforce") return promoteEnforce(options, deps);
      return recover(options, deps);
    })
  ));
}

async function main() {
  const state = await executeReleaseCommand(process.argv.slice(2));
  process.stdout.write(`${JSON.stringify({
    ok: true,
    phase: state.phase,
    deploymentId: state.current?.deploymentId || null,
    imageDigest: state.current?.imageDigest || null,
    rollbackBoundary: state.rollbackBoundary,
    fundedExecutionSupported: false,
    volumeRestoreAttempted: false,
  })}\n`);
}

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
  main().catch((error) => {
    process.stderr.write(`direct production release refused: ${error.message}\n`);
    process.exitCode = 1;
  });
}
