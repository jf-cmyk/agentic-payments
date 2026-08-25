#!/usr/bin/env node

/**
 * Narrow, fail-closed production controller for the Coinbase x402 hotfix.
 *
 * This controller intentionally has no funded-payment command.  It can only:
 *   1. attest the fixed production target and legacy rollback point;
 *   2. connect an exact, immutable GitHub branch with payments locked in shadow mode;
 *   3. promote that exact image to enforce mode; or
 *   4. recover according to the recorded economic rollback boundary.
 *
 * It never reads payment credentials and never creates/restores a volume backup.
 */

import { spawn } from "node:child_process";
import { createHash, randomUUID } from "node:crypto";
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

import { isDirectExecution } from "./direct_execution.mjs";

export const TARGET = Object.freeze({
  project: "9fc6c062-6d58-4cb9-af11-df68670bfca5",
  environment: "9d51961d-759c-441b-be1d-186515b9ed7f",
  service: "8853c53e-521e-4876-a796-f94c1adf5700",
});

export const LEGACY = Object.freeze({
  deploymentId: "66920048-282d-45e6-a302-db6da1702058",
  imageDigest: "sha256:435dc858af3fcb3eb44b4e249e0d8e4a917f62f174881fd320f8df1d57c5d6c3",
  snapshotId: "e37a3aeb-562f-4712-9d37-f68c59c8c648",
});

export const GITHUB_SOURCE = Object.freeze({
  repository: "jf-cmyk/agentic-payments",
  branch: "codex/production-x402",
  provider: "github",
});
const GITHUB_WORKFLOW = "coinbase-x402-hotfix.yml";
const SOURCE_CONNECT_QUIESCENCE_MS = 60_000;

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
    "repo",
    "branch",
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
    assert(values.get("repo") === GITHUB_SOURCE.repository,
      `--repo must be the fixed production repository ${GITHUB_SOURCE.repository}`);
    assert(values.get("branch") === GITHUB_SOURCE.branch,
      `--branch must be the fixed production branch ${GITHUB_SOURCE.branch}`);
  }
  if (command !== "preflight") {
    assert(!values.has("volume-instance-id"), "--volume-instance-id is accepted only by preflight");
    assert(!values.has("solana-pay-to") && !values.has("base-pay-to"),
      "payment recipients are accepted only by preflight");
    assert(!values.has("repo") && !values.has("branch"),
      "repository source is accepted only by preflight");
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
    githubSource: command === "preflight" ? {
      repository: values.get("repo"),
      branch: values.get("branch"),
      provider: GITHUB_SOURCE.provider,
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
    "  direct_prod_coinbase_release.mjs preflight --state ABS --commit SHA --volume-instance-id UUID --solana-pay-to ADDRESS --base-pay-to ADDRESS --repo jf-cmyk/agentic-payments --branch codex/production-x402",
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

function railwayDiagnosticMarkers(...values) {
  const sample = values.map((value) => {
    const text = String(value || "");
    return `${text.slice(0, 2_048)}\n${text.slice(-2_048)}`;
  }).join("\n");
  const patterns = [
    ["dns", /\b(?:dns|resolve|resolution|unknown host)\b/i],
    ["timeout", /\b(?:timeout|timed out|deadline)\b/i],
    ["connection", /\b(?:connection|socket|broken pipe|reset by peer|eof)\b/i],
    ["authentication", /\b(?:unauthorized|not authenticated|authentication)\b/i],
    ["authorization", /\b(?:forbidden|not authorized|permission denied)\b/i],
    ["payload", /\b(?:payload too large|request entity too large|http 413)\b/i],
    ["rate_limit", /\b(?:rate limit|too many requests|http 429)\b/i],
    ["tls", /\b(?:tls|certificate|ssl)\b/i],
  ];
  const markers = patterns.filter(([, pattern]) => pattern.test(sample)).map(([name]) => name);
  return markers.length > 0 ? markers.join(",") : "none";
}

function parseStructuredRailwayFailure(output) {
  const raw = String(output || "");
  const trimmed = raw.length <= 8_192 ? raw.trim() : "";
  const tail = raw.slice(-8_192).trim();
  const lastLine = tail.slice(Math.max(tail.lastIndexOf("\n"), tail.lastIndexOf("\r")) + 1);
  const candidates = [...new Set([trimmed, lastLine].filter(Boolean))];
  for (const candidate of candidates) {
    if (candidate.length > 8_192) continue;
    if (!candidate.startsWith("{") || !candidate.endsWith("}")) continue;
    let value;
    try { value = JSON.parse(candidate); } catch { continue; }
    if (!value || Array.isArray(value) || typeof value !== "object"
      || !/^[A-Z0-9_]{1,64}$/.test(value.code || "")
      || typeof value.error !== "string"
      || !(value.hint == null || typeof value.hint === "string")) continue;
    return {
      code: value.code,
      markers: railwayDiagnosticMarkers(value.error, value.hint),
      errorBytes: Buffer.byteLength(value.error),
      hintBytes: Buffer.byteLength(value.hint || ""),
      hasHint: typeof value.hint === "string" && value.hint.length > 0,
    };
  }
  return null;
}

function railwayCommandFailure(result, label) {
  if (result && result.code === 0 && !result.spawnError && !result.exceeded && !result.timedOut) {
    return null;
  }
  const stdout = String(result?.stdout || "");
  const stderr = String(result?.stderr || "");
  const signal = /^[A-Z0-9]{1,16}$/.test(result?.signal || "") ? result.signal : "none";
  const processEvidence = [
    `exit=${Number.isInteger(result?.code) ? result.code : "unknown"}`,
    `signal=${signal}`,
    `spawnError=${Boolean(result?.spawnError)}`,
    `timedOut=${result?.timedOut === true}`,
    `exceeded=${result?.exceeded === true}`,
    `stdoutBytes=${Buffer.byteLength(stdout)}`,
    `stderrBytes=${Buffer.byteLength(stderr)}`,
  ].join(",");
  const structured = parseStructuredRailwayFailure(stdout);
  if (structured) {
    return `${label} failed (${processEvidence}; code=${structured.code}; markers=${
      structured.markers
    }; errorBytes=${structured.errorBytes}; hintBytes=${structured.hintBytes}; hasHint=${
      structured.hasHint
    })`;
  }
  return `${label} failed (${processEvidence}; unstructuredMarkers=${
    railwayDiagnosticMarkers(stdout, stderr)
  })`;
}

function railwaySourceConnectFailure(result) {
  return railwayCommandFailure(result, "Railway GitHub source connection");
}

function railwaySourceConnectOutput(result) {
  const failure = railwaySourceConnectFailure(result);
  if (failure) fail(failure);
  const value = parseJsonOutput(result.stdout, "Railway production source patch");
  assert(value?.staged === true && value?.committed === true
    && value?.environmentId === TARGET.environment
    && value?.environmentName === "production",
  "Railway production source patch acknowledgement is not target-bound");
  return value;
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

function railwayGraphqlData(result, label) {
  const payload = parseJsonOutput(commandOutput(result, label), label);
  assert(!payload?.errors?.length, `${label} returned GraphQL errors`);
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

async function getSourceAuthority(deps) {
  const data = await railwayApi(
    deps,
    "query DirectProdAuthority($projectId:String!,$environmentId:String!,$serviceId:String!){serviceInstance(environmentId:$environmentId,serviceId:$serviceId){environmentId serviceId source{repo image}} service(id:$serviceId){id projectId} deploymentTriggers(projectId:$projectId,environmentId:$environmentId,serviceId:$serviceId,first:100){edges{node{id projectId environmentId serviceId branch repository provider checkSuites validCheckSuites}} pageInfo{hasNextPage}}}",
    {
      projectId: TARGET.project,
      environmentId: TARGET.environment,
      serviceId: TARGET.service,
    },
  );
  const service = data?.service;
  const instance = data?.serviceInstance;
  const triggers = data?.deploymentTriggers;
  assert(service?.id === TARGET.service && service?.projectId === TARGET.project,
    "repository-trigger evidence is not target-bound");
  assert(instance?.environmentId === TARGET.environment && instance?.serviceId === TARGET.service
    && instance?.source && Object.hasOwn(instance.source, "repo")
    && Object.hasOwn(instance.source, "image"),
  "repository-source evidence is incomplete or not target-bound");
  assert(Array.isArray(triggers?.edges)
    && triggers.pageInfo?.hasNextPage === false,
  "repository-trigger inventory is incomplete");
  return {
    source: instance.source,
    triggers: triggers.edges.map((edge) => edge?.node),
  };
}

function triggerMatchesSource(trigger, source) {
  return trigger?.projectId === TARGET.project
    && trigger?.environmentId === TARGET.environment
    && trigger?.serviceId === TARGET.service
    && trigger?.repository === source.repository
    && trigger?.branch === source.branch
    && String(trigger?.provider || "").toLowerCase() === source.provider;
}

function assertExpectedSourceMetadata(authority, source, label) {
  assert(JSON.stringify(source) === JSON.stringify(GITHUB_SOURCE),
    "release state GitHub source drifted");
  assert(authority.source.repo === source.repository && authority.source.image == null
    && authority.triggers.length <= 1
    && authority.triggers.every((trigger) => triggerMatchesSource(trigger, source)),
  label);
  return authority;
}

async function assertInertDeploySourceAuthority(
  deps,
  source,
  expectedMetadataRetained = null,
) {
  assert(JSON.stringify(source) === JSON.stringify(GITHUB_SOURCE),
    "release state GitHub source drifted");
  const authority = await getSourceAuthority(deps);
  const sourceMetadataRetained = authority.source.repo === source.repository;
  assert((authority.source.repo == null || sourceMetadataRetained)
    && authority.source.image == null,
  "production has an unexpected service source");
  assert(authority.triggers.length === 0,
    "production has a repository auto-deploy trigger");
  if (expectedMetadataRetained != null) {
    assert(typeof expectedMetadataRetained === "boolean"
      && sourceMetadataRetained === expectedMetadataRetained,
    "production inert GitHub source metadata changed after preflight");
  }
  return { authority, sourceMetadataRetained };
}

async function assertNoSourceAuthority(deps) {
  const authority = await getSourceAuthority(deps);
  assert(authority.source.repo == null && authority.source.image == null,
    "production already has a service source");
  assert(authority.triggers.length === 0, "production has a repository auto-deploy trigger");
  return authority;
}

async function assertExpectedSourceAuthority(deps, source, { requireWaitForCi = true } = {}) {
  const authority = await getSourceAuthority(deps);
  assertExpectedSourceMetadata(
    authority,
    source,
    "production GitHub source differs from the release-bound repository",
  );
  assert(authority.triggers.length === 1
    && triggerMatchesSource(authority.triggers[0], source),
  "production repository trigger differs from the release-bound GitHub branch");
  if (requireWaitForCi) {
    assert(authority.triggers[0].checkSuites === true,
      "production repository trigger is not waiting for GitHub checks");
    assert(Number.isSafeInteger(authority.triggers[0].validCheckSuites)
      && authority.triggers[0].validCheckSuites >= 1,
    "production repository trigger has no valid GitHub check suite");
  }
  return authority;
}

async function enableWaitForCi(deps, source) {
  let authority = await assertExpectedSourceAuthority(
    deps,
    source,
    { requireWaitForCi: false },
  );
  if (authority.triggers[0].checkSuites !== true) {
    const data = await railwayApi(
      deps,
      "mutation DirectProdWaitForCI($id:String!){deploymentTriggerUpdate(id:$id,input:{checkSuites:true}){id checkSuites}}",
      { id: authority.triggers[0].id },
    );
    assert(data?.deploymentTriggerUpdate?.id === authority.triggers[0].id
      && data.deploymentTriggerUpdate.checkSuites === true,
    "Railway did not enable Wait for CI on the exact production trigger");
  }
  authority = await assertExpectedSourceAuthority(deps, source);
  return authority.triggers[0];
}

async function ensureExpectedSourceTrigger(deps, state, sourceDeployment, options) {
  validateUploadIntent(state);
  assertSourceDeploymentIntent(sourceDeployment, state, deps.now());
  let authority = await getSourceAuthority(deps);
  assertExpectedSourceMetadata(
    authority,
    state.githubSource,
    "production GitHub source differs before trigger creation",
  );
  const [history, staged] = await Promise.all([
    listDeployments(deps),
    getProductionStagedPatch(deps),
  ]);
  const uploadBaseline = new Set(state.uploadBaselineDeploymentIds);
  const additions = history.filter((row) => !uploadBaseline.has(row.id));
  assert(additions.length === 1 && additions[0].id === sourceDeployment.id,
    "trigger creation requires the one exact source-connected deployment");
  let intent = {
    projectId: TARGET.project,
    environmentId: TARGET.environment,
    serviceId: TARGET.service,
    repository: state.githubSource.repository,
    branch: state.githubSource.branch,
    provider: state.githubSource.provider,
    checkSuites: true,
    sourceDeploymentId: sourceDeployment.id,
    baselineDeploymentIds: history.map((row) => row.id),
    stagedPatchId: staged.id,
    stagedPatchFingerprint: stagedPatchFingerprint(staged),
    armedAt: new Date(deps.now()).toISOString(),
  };
  state.pendingTriggerCreate = intent;
  state.updatedAt = new Date(deps.now()).toISOString();
  await atomicWriteState(options.stateFile, state);

  const stagedBeforeMutation = await getProductionStagedPatch(deps);
  assert(stagedBeforeMutation.id === intent.stagedPatchId
    && stagedPatchFingerprint(stagedBeforeMutation) === intent.stagedPatchFingerprint,
  "production staged patch changed after trigger-create intent was armed");
  const preDispatchDelta = await postIntentDeploymentDelta(
    deps,
    intent.baselineDeploymentIds,
    "production trigger creation",
  );
  assert(preDispatchDelta.length === 0,
    "production deployment history changed before trigger creation");
  authority = await getSourceAuthority(deps);
  assertExpectedSourceMetadata(
    authority,
    state.githubSource,
    "production source drifted before trigger creation",
  );

  let providerFailure = null;
  let origin = "source_patch";
  if (authority.triggers.length === 0) {
    origin = "explicit_create";
    intent = {
      ...intent,
      dispatchArmedAt: new Date(deps.now()).toISOString(),
    };
    state.pendingTriggerCreate = intent;
    state.updatedAt = new Date(deps.now()).toISOString();
    await atomicWriteState(options.stateFile, state);
    const input = {
      projectId: TARGET.project,
      environmentId: TARGET.environment,
      serviceId: TARGET.service,
      repository: state.githubSource.repository,
      branch: state.githubSource.branch,
      provider: state.githubSource.provider,
      checkSuites: true,
    };
    const result = await deps.run([
      "railway", "api",
      "mutation DirectProdCreateTrigger($input:DeploymentTriggerCreateInput!){deploymentTriggerCreate(input:$input){id projectId environmentId serviceId repository branch provider checkSuites validCheckSuites}}",
      "--variables", JSON.stringify({ input }),
      "--compact",
    ]);
    providerFailure = railwayCommandFailure(
      result,
      "Railway production trigger creation",
    );
    if (!providerFailure) {
      const acknowledged = railwayGraphqlData(
        result,
        "Railway production trigger creation",
      )?.deploymentTriggerCreate;
      assertExpectedSourceTriggerRecord(
        acknowledged,
        state.githubSource,
        "Railway trigger-create acknowledgement is not target-bound",
      );
      intent = {
        ...intent,
        observedTriggerId: acknowledged.id,
        providerReportedSuccess: true,
      };
    } else {
      intent = { ...intent, providerReportedSuccess: false };
    }
    state.pendingTriggerCreate = intent;
    state.updatedAt = new Date(deps.now()).toISOString();
    await atomicWriteState(options.stateFile, state);
  } else {
    intent = { ...intent, observedTriggerId: authority.triggers[0].id };
    state.pendingTriggerCreate = intent;
    state.updatedAt = new Date(deps.now()).toISOString();
    await atomicWriteState(options.stateFile, state);
    await enableWaitForCi(deps, state.githubSource);
  }

  const attempts = Math.ceil(SOURCE_CONNECT_QUIESCENCE_MS / options.pollMs) + 1;
  let trigger = null;
  let sawTrigger = false;
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    authority = await getSourceAuthority(deps);
    assertExpectedSourceMetadata(
      authority,
      state.githubSource,
      "production source drifted during trigger-create quiescence",
    );
    if (authority.triggers.length === 1) {
      const current = authority.triggers[0];
      if (intent.observedTriggerId == null) {
        intent = {
          ...intent,
          observedTriggerId: current.id,
        };
        state.pendingTriggerCreate = intent;
        state.updatedAt = new Date(deps.now()).toISOString();
        await atomicWriteState(options.stateFile, state);
      }
      assert(current.id === intent.observedTriggerId,
        "production trigger was replaced during trigger-create quiescence");
      trigger = current;
      sawTrigger = true;
    } else {
      assert(!sawTrigger,
        "production trigger disappeared during trigger-create quiescence");
      trigger = null;
    }
    const [currentStaged, delta, active] = await Promise.all([
      getProductionStagedPatch(deps),
      postIntentDeploymentDelta(
        deps,
        intent.baselineDeploymentIds,
        "production trigger creation",
      ),
      getActive(deps),
    ]);
    assert(currentStaged.id === intent.stagedPatchId
      && stagedPatchFingerprint(currentStaged) === intent.stagedPatchFingerprint,
    "production staged patch changed during trigger-create quiescence");
    assert(delta.length === 0,
      "production trigger creation changed deployment history");
    assert(active.length <= 2 && active.every((row) => (
      row.id === LEGACY.deploymentId || row.id === intent.sourceDeploymentId
    )), "production active deployment changed during trigger creation");
    for (const row of active) {
      if (row.id === LEGACY.deploymentId) {
        assert(imageDigest(row) === LEGACY.imageDigest,
          "legacy image changed during trigger creation");
      } else {
        assert(sourceDeploymentMatches(row, state),
          "source deployment changed during trigger creation");
      }
    }
    if (attempt + 1 < attempts) await deps.sleep(options.pollMs);
  }
  if (!trigger) {
    if (providerFailure) fail(providerFailure);
    fail("Railway production trigger creation was not durably observable");
  }
  assertExpectedSourceTriggerRecord(
    trigger,
    state.githubSource,
    "production trigger is not waiting for a valid GitHub check suite",
    { requireValidCheckSuite: true },
  );
  return {
    ...trigger,
    origin,
    providerReportedSuccess: intent.providerReportedSuccess ?? null,
    mutationMayHaveBeenAttempted: intent.dispatchArmedAt != null,
    reconciledAt: new Date(deps.now()).toISOString(),
  };
}

function emptyEnvironmentPatch(patch) {
  return patch && !Array.isArray(patch) && typeof patch === "object"
    && Object.keys(patch).length === 0;
}

async function getProductionStagedPatch(deps) {
  const data = await railwayApi(
    deps,
    "query DirectProdStagedPatch($environmentId:String!){environmentStagedChanges(environmentId:$environmentId){id environmentId status appliedAt patch(decryptVariables:false)}}",
    { environmentId: TARGET.environment },
  );
  const staged = data?.environmentStagedChanges;
  assert(staged?.environmentId === TARGET.environment
    && staged.status === "STAGED" && staged.appliedAt == null
    && staged.patch && !Array.isArray(staged.patch) && typeof staged.patch === "object",
  "production staged-patch evidence is incomplete or not target-bound");
  return staged;
}

async function assertNoProductionStagedPatch(deps) {
  const staged = await getProductionStagedPatch(deps);
  assert(emptyEnvironmentPatch(staged.patch),
    "production has an unrelated staged environment patch");
  return staged;
}

function canonicalJson(value) {
  if (Array.isArray(value)) return value.map((item) => canonicalJson(item));
  if (value && typeof value === "object") {
    return Object.fromEntries(Object.keys(value).sort().map(
      (key) => [key, canonicalJson(value[key])],
    ));
  }
  return value;
}

function stagedPatchFingerprint(staged) {
  return createHash("sha256")
    .update(JSON.stringify(canonicalJson(staged.patch)))
    .digest("hex");
}

function assertExpectedSourceTriggerRecord(trigger, source, label, {
  requireValidCheckSuite = false,
} = {}) {
  assert(UUID.test(trigger?.id || "")
    && triggerMatchesSource(trigger, source)
    && trigger.checkSuites === true
    && Number.isSafeInteger(trigger.validCheckSuites)
    && trigger.validCheckSuites >= (requireValidCheckSuite ? 1 : 0),
  label);
  return trigger;
}

function validateTriggerCreateIntent(intent, state) {
  assert(intent
    && intent.projectId === TARGET.project
    && intent.environmentId === TARGET.environment
    && intent.serviceId === TARGET.service
    && intent.repository === state.githubSource.repository
    && intent.branch === state.githubSource.branch
    && intent.provider === state.githubSource.provider
    && intent.checkSuites === true
    && UUID.test(intent.sourceDeploymentId || "")
    && Array.isArray(intent.baselineDeploymentIds)
    && new Set(intent.baselineDeploymentIds).size === intent.baselineDeploymentIds.length
    && intent.baselineDeploymentIds.every((id) => UUID.test(id))
    && intent.baselineDeploymentIds.includes(intent.sourceDeploymentId)
    && state.uploadBaselineDeploymentIds.every(
      (id) => intent.baselineDeploymentIds.includes(id),
    )
    && intent.baselineDeploymentIds.length
      === state.uploadBaselineDeploymentIds.length + 1
    && typeof intent.stagedPatchId === "string"
    && /^[0-9a-f]{64}$/.test(intent.stagedPatchFingerprint || "")
    && Number.isFinite(Date.parse(intent.armedAt || ""))
    && (intent.dispatchArmedAt == null
      || Number.isFinite(Date.parse(intent.dispatchArmedAt)))
    && (intent.observedTriggerId == null
      || UUID.test(intent.observedTriggerId || ""))
    && (intent.providerReportedSuccess == null
      || typeof intent.providerReportedSuccess === "boolean"),
  "pending production trigger-create intent is invalid or target-drifted");
  return intent;
}

function validateSourceTriggerRecord(record, source) {
  assert(record
    && UUID.test(record.id || "")
    && record.projectId === TARGET.project
    && record.environmentId === TARGET.environment
    && record.serviceId === TARGET.service
    && record.repository === source.repository
    && record.branch === source.branch
    && record.provider === source.provider
    && record.checkSuites === true
    && Number.isSafeInteger(record.validCheckSuites)
    && record.validCheckSuites >= 1
    && ["source_patch", "explicit_create"].includes(record.origin)
    && (record.providerReportedSuccess == null
      || typeof record.providerReportedSuccess === "boolean")
    && typeof record.mutationMayHaveBeenAttempted === "boolean"
    && Number.isFinite(Date.parse(record.reconciledAt || "")),
  "completed production trigger-create evidence is invalid");
  return record;
}

function activeIdentity(rows) {
  return rows.map((row) => ({
    id: row.id,
    imageDigest: imageDigest(row) || null,
    status: String(row.status || "").toUpperCase(),
    deploymentStopped: row.deploymentStopped === true,
    instances: (row.instances || []).map((instance) => ({
      id: instance.id,
      status: String(instance.status || "").toUpperCase(),
    })).sort((left, right) => left.id.localeCompare(right.id)),
  })).sort((left, right) => left.id.localeCompare(right.id));
}

function assertInertExpectedSourceAuthority(authority, source, label) {
  assert((authority.source.repo == null || authority.source.repo === source.repository)
    && authority.source.image == null
    && authority.triggers.length <= 1
    && authority.triggers.every((trigger) => triggerMatchesSource(trigger, source)),
  label);
}

function validateTriggerDisableIntent(intent, source) {
  assert(intent && (intent.triggerId == null || UUID.test(intent.triggerId || ""))
    && intent.projectId === TARGET.project
    && intent.environmentId === TARGET.environment
    && intent.serviceId === TARGET.service
    && intent.repository === source.repository
    && intent.branch === source.branch
    && intent.provider === source.provider
    && Array.isArray(intent.baselineDeploymentIds)
    && new Set(intent.baselineDeploymentIds).size === intent.baselineDeploymentIds.length
    && intent.baselineDeploymentIds.every((id) => UUID.test(id))
    && Array.isArray(intent.priorActiveIdentity)
    && intent.priorActiveIdentity.length <= 1
    && /^[0-9a-f]{64}$/.test(intent.stagedPatchFingerprint || "")
    && Number.isFinite(Date.parse(intent.armedAt || ""))
    && (intent.deleteDispatchArmedAt == null
      || Number.isFinite(Date.parse(intent.deleteDispatchArmedAt))),
  "pending production trigger-disable intent is invalid or target-drifted");
  return intent;
}

function validateCompletedTriggerControl(control) {
  assert(control?.githubTriggerDisabled === true
    && (control.triggerId == null || UUID.test(control.triggerId || ""))
    && (control.handledLateDeploymentId == null
      || UUID.test(control.handledLateDeploymentId || ""))
    && typeof control.triggerDeletionMayHaveBeenAttempted === "boolean"
    && typeof control.sourceMetadataRetained === "boolean"
    && typeof control.stagedPatchId === "string"
    && /^[0-9a-f]{64}$/.test(control.stagedPatchFingerprint || "")
    && Number.isFinite(Date.parse(control.disabledAt || "")),
  "completed production trigger-disable evidence is invalid");
  return control;
}

function assertKnownTriggerDisableActive(state, active) {
  assert(active.length <= 1
    && active.every((row) => knownDeploymentIds(state).has(row.id)),
  "production trigger disable ended with an unknown active deployment");
  for (const row of active) {
    assertKnownActiveIdentity(
      state,
      row,
      row.id,
      "production trigger-disable active deployment",
    );
  }
}

async function disableExpectedSourceTrigger(deps, state, options) {
  let authority = await getSourceAuthority(deps);
  assertInertExpectedSourceAuthority(
    authority,
    state.githubSource,
    "refusing to disable an unexpected production source or trigger",
  );
  let intent = state.pendingTriggerDisable
    ? validateTriggerDisableIntent(state.pendingTriggerDisable, state.githubSource)
    : null;
  if (!intent && authority.triggers.length === 0
    && state.sourceTriggerControl?.githubTriggerDisabled === true) {
    const completed = validateCompletedTriggerControl(state.sourceTriggerControl);
    const sourceMetadataStillMatches = completed.sourceMetadataRetained
      === (authority.source.repo === state.githubSource.repository);
    if (!options.forceFreshTriggerControl && sourceMetadataStillMatches) {
      await assertCompletedTriggerControlAuthority(
        deps,
        state,
        "completed production trigger-disable authority drifted",
      );
      if (state.phase !== "github_source_connect_armed") {
        assertKnownTriggerDisableActive(state, await getActive(deps));
      }
      return completed;
    }
    assert(sourceMetadataStillMatches || state.phase === "github_source_connect_armed",
      "completed trigger-disable source metadata evidence drifted");
  }
  if (!intent && authority.triggers.length === 0) {
    const ambiguousSourceConnect = state.phase === "github_source_connect_armed"
      || authority.source.repo === state.githubSource.repository;
    if (!ambiguousSourceConnect) {
      if (state.phase !== "github_source_connect_armed") {
        assertKnownTriggerDisableActive(state, await getActive(deps));
      }
      return {
        githubTriggerDisabled: true,
        triggerDeletionMayHaveBeenAttempted: false,
        triggerId: null,
        sourceMetadataRetained: false,
        providerReportedSuccess: null,
        disabledAt: new Date(deps.now()).toISOString(),
      };
    }
  }

  assert(options.timeoutMs >= SOURCE_CONNECT_QUIESCENCE_MS,
    "recovery timeout must allow the full 60-second trigger-disable quiescence window");
  if (!intent) {
    if (state.sourceTriggerControl?.githubTriggerDisabled === true) {
      validateCompletedTriggerControl(state.sourceTriggerControl);
      delete state.sourceTriggerControl;
    }
    const trigger = authority.triggers[0];
    assert(!trigger || UUID.test(trigger.id || ""),
      "production trigger has no immutable id");
    const [history, active, staged] = await Promise.all([
      listDeployments(deps),
      getActive(deps),
      getProductionStagedPatch(deps),
    ]);
    intent = {
      triggerId: trigger?.id || null,
      projectId: TARGET.project,
      environmentId: TARGET.environment,
      serviceId: TARGET.service,
      repository: state.githubSource.repository,
      branch: state.githubSource.branch,
      provider: state.githubSource.provider,
      baselineDeploymentIds: history.map((row) => row.id),
      priorActiveIdentity: activeIdentity(active),
      stagedPatchId: staged.id,
      stagedPatchFingerprint: stagedPatchFingerprint(staged),
      armedAt: new Date(deps.now()).toISOString(),
    };
    state.pendingTriggerDisable = intent;
    state.updatedAt = new Date(deps.now()).toISOString();
    await atomicWriteState(options.stateFile, state);
  }

  const stagedBeforeMutation = await getProductionStagedPatch(deps);
  assert(stagedBeforeMutation.id === intent.stagedPatchId
    && stagedPatchFingerprint(stagedBeforeMutation) === intent.stagedPatchFingerprint,
  "production staged patch changed after trigger-disable intent was armed");
  authority = await getSourceAuthority(deps);
  assertInertExpectedSourceAuthority(
    authority,
    state.githubSource,
    "production source drifted while disabling its trigger",
  );
  if (authority.triggers.length === 1) {
    if (intent.triggerId == null) {
      assert(UUID.test(authority.triggers[0].id || ""),
        "delayed production trigger has no immutable id");
      intent = {
        ...intent,
        triggerId: authority.triggers[0].id,
        triggerObservedAt: new Date(deps.now()).toISOString(),
      };
      state.pendingTriggerDisable = intent;
      state.updatedAt = new Date(deps.now()).toISOString();
      await atomicWriteState(options.stateFile, state);
      fail("a production trigger appeared after the absence intent; rerun recovery");
    }
    assert(authority.triggers[0].id === intent.triggerId,
      "production trigger was replaced after trigger-disable intent was armed");
  }

  let providerFailure = null;
  const dispatchedThisRun = authority.triggers.length === 1;
  if (dispatchedThisRun) {
    if (intent.deleteDispatchArmedAt == null) {
      intent = {
        ...intent,
        deleteDispatchArmedAt: new Date(deps.now()).toISOString(),
      };
      state.pendingTriggerDisable = intent;
      state.updatedAt = new Date(deps.now()).toISOString();
      await atomicWriteState(options.stateFile, state);
    }
    const result = await deps.run([
      "railway", "api",
      "mutation DirectProdDisableTrigger($id:String!){deploymentTriggerDelete(id:$id)}",
      "--raw-var", `id=${intent.triggerId}`,
      "--compact",
    ]);
    providerFailure = railwayCommandFailure(
      result,
      "Railway production trigger disable",
    );
    if (!providerFailure) {
      const deleted = railwayGraphqlData(
        result,
        "Railway production trigger disable",
      )?.deploymentTriggerDelete;
      if (deleted !== true) {
        providerFailure = "Railway production trigger disable was not acknowledged";
      }
    }
  }

  const attempts = Math.ceil(SOURCE_CONNECT_QUIESCENCE_MS / options.pollMs) + 1;
  let sawAbsent = authority.triggers.length === 0;
  let additions = [];
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    authority = await getSourceAuthority(deps);
    assertInertExpectedSourceAuthority(
      authority,
      state.githubSource,
      "production source drifted during trigger-disable quiescence",
    );
    if (authority.triggers.length === 1) {
      if (intent.triggerId == null) {
        assert(UUID.test(authority.triggers[0].id || ""),
          "delayed production trigger has no immutable id");
        intent = {
          ...intent,
          triggerId: authority.triggers[0].id,
          triggerObservedAt: new Date(deps.now()).toISOString(),
        };
        state.pendingTriggerDisable = intent;
        state.updatedAt = new Date(deps.now()).toISOString();
        await atomicWriteState(options.stateFile, state);
        fail("a production trigger appeared during absence quiescence; rerun recovery");
      }
      assert(!sawAbsent && authority.triggers[0].id === intent.triggerId,
        "production trigger was replaced or reappeared during disable quiescence");
    } else {
      sawAbsent = true;
    }
    const staged = await getProductionStagedPatch(deps);
    assert(staged.id === intent.stagedPatchId
      && stagedPatchFingerprint(staged) === intent.stagedPatchFingerprint,
    "production staged patch changed during trigger-disable quiescence");
    additions = await postIntentDeploymentDelta(
      deps,
      intent.baselineDeploymentIds,
      "production trigger disable",
    );
    assert(additions.length <= 1,
      "production trigger disable observed an ambiguous deployment-history delta");
    if (attempt + 1 < attempts) await deps.sleep(options.pollMs);
  }

  if (authority.triggers.length !== 0) {
    if (providerFailure) fail(providerFailure);
    fail("timed out disabling the exact production GitHub trigger");
  }
  let handledLateDeploymentId = null;
  if (additions.length === 1) {
    const addition = additions[0];
    const lateSourceIntentDeployment = state.phase === "github_source_connect_armed"
      && sourceDeploymentMatches(addition, state);
    assert((knownDeploymentIds(state).has(addition.id) || lateSourceIntentDeployment)
      && sourceDeploymentMatches(addition, state),
    "production trigger disable observed an unhandled late source deployment");
    assert(!lateSourceIntentDeployment
      || !IN_FLIGHT.has(String(addition.status || "").toUpperCase()),
    "production trigger disable observed a late source deployment still in flight");
    assertDeploymentCreatedForIntent(
      addition,
      lateSourceIntentDeployment ? state.uploadArmedAt : intent.armedAt,
      deps.now(),
      "production trigger disable",
    );
    handledLateDeploymentId = addition.id;
  }
  const active = await getActive(deps);
  if (!handledLateDeploymentId) {
    assert(JSON.stringify(activeIdentity(active)) === JSON.stringify(intent.priorActiveIdentity),
      "production active deployment changed while disabling its GitHub trigger");
  } else {
    assert(active.length <= 1
      && active.every((row) => knownDeploymentIds(state).has(row.id)
        || row.id === handledLateDeploymentId),
    "production has an unknown active deployment after trigger disable");
  }
  if (state.phase !== "github_source_connect_armed") {
    assertKnownTriggerDisableActive(state, active);
  }

  const control = {
    githubTriggerDisabled: true,
    triggerDeletionMayHaveBeenAttempted: intent.deleteDispatchArmedAt != null,
    triggerId: intent.triggerId,
    sourceMetadataRetained: authority.source.repo === state.githubSource.repository,
    providerReportedSuccess: dispatchedThisRun ? providerFailure === null : null,
    handledLateDeploymentId,
    stagedPatchId: intent.stagedPatchId,
    stagedPatchFingerprint: intent.stagedPatchFingerprint,
    disabledAt: new Date(deps.now()).toISOString(),
  };
  state.sourceTriggerControl = control;
  delete state.pendingTriggerDisable;
  state.updatedAt = new Date(deps.now()).toISOString();
  await atomicWriteState(options.stateFile, state);
  return control;
}

async function assertCompletedTriggerControlAuthority(deps, state, label) {
  const control = validateCompletedTriggerControl(state.sourceTriggerControl);
  const [authority, staged] = await Promise.all([
    getSourceAuthority(deps),
    getProductionStagedPatch(deps),
  ]);
  assertInertExpectedSourceAuthority(authority, state.githubSource, label);
  assert(authority.triggers.length === 0, label);
  assert(staged.id === control.stagedPatchId
    && stagedPatchFingerprint(staged) === control.stagedPatchFingerprint,
  "production staged patch changed after trigger-disable completion");
  assert(control.sourceMetadataRetained
    === (authority.source.repo === state.githubSource.repository),
  "completed trigger-disable source metadata evidence drifted");
  return authority;
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

async function assertRemoteBranchHead(deps, source, expectedCommit) {
  assert(JSON.stringify(source) === JSON.stringify(GITHUB_SOURCE),
    "remote verification source is not the fixed production GitHub branch");
  const ref = `refs/heads/${source.branch}`;
  const output = commandOutput(await deps.run([
    "git", "ls-remote", "--exit-code", "--heads",
    `https://github.com/${source.repository}.git`, ref,
  ]), "GitHub production branch check").trim();
  const lines = output.split(/\r?\n/).filter(Boolean);
  assert(lines.length === 1, "GitHub production branch is missing or ambiguous");
  const [commit, actualRef, ...extra] = lines[0].split(/\s+/);
  assert(extra.length === 0 && actualRef === ref && String(commit).toLowerCase() === expectedCommit,
    "GitHub production branch does not resolve to the exact reviewed commit");
}

async function assertGitHubReleaseGuardrails(deps, source, expectedCommit) {
  assert(JSON.stringify(source) === JSON.stringify(GITHUB_SOURCE),
    "GitHub guardrail source is not the fixed production branch");
  const runs = await runJson(
    deps,
    [
      "gh", "api", "-X", "GET",
      `repos/${source.repository}/actions/workflows/${GITHUB_WORKFLOW}/runs`,
      "-f", `branch=${source.branch}`,
      "-f", `head_sha=${expectedCommit}`,
      "-f", "event=push",
      "-f", "per_page=100",
    ],
    "GitHub production workflow evidence",
  );
  const matchingRuns = Array.isArray(runs?.workflow_runs) ? runs.workflow_runs : [];
  assert(matchingRuns.length >= 1
    && matchingRuns.every((run) => run?.head_sha === expectedCommit
      && run?.head_branch === source.branch
      && run?.event === "push"
      && run?.status === "completed"
      && run?.conclusion === "success"
      && Number.isSafeInteger(run?.id)),
  "exact production branch commit lacks a completed successful push workflow");

  const effectiveRules = await runJson(
    deps,
    [
      "gh", "api",
      `repos/${source.repository}/rules/branches/${encodeURIComponent(source.branch)}`,
    ],
    "GitHub production branch rules",
  );
  assert(Array.isArray(effectiveRules), "GitHub production branch rules are incomplete");
  const immutableRule = effectiveRules.find((rule) => rule?.type === "update");
  const ruleTypes = new Set(effectiveRules.map((rule) => rule?.type));
  assert(Number.isSafeInteger(immutableRule?.ruleset_id)
    && ruleTypes.has("deletion")
    && ruleTypes.has("non_fast_forward"),
  "GitHub production branch is not immutable and deletion-protected");
  const ruleset = await runJson(
    deps,
    ["gh", "api", `repos/${source.repository}/rulesets/${immutableRule.ruleset_id}`],
    "GitHub production immutability ruleset",
  );
  const expectedRef = `refs/heads/${source.branch}`;
  assert(ruleset?.id === immutableRule.ruleset_id
    && ruleset?.target === "branch"
    && ruleset?.enforcement === "active"
    && Array.isArray(ruleset?.bypass_actors)
    && ruleset.bypass_actors.length === 0
    && JSON.stringify(ruleset?.conditions?.ref_name?.include) === JSON.stringify([expectedRef])
    && JSON.stringify(ruleset?.conditions?.ref_name?.exclude) === JSON.stringify([])
    && Array.isArray(ruleset?.rules)
    && ["update", "deletion", "non_fast_forward"].every(
      (type) => ruleset.rules.some((rule) => rule?.type === type),
    ),
  "GitHub production branch immutability ruleset is missing, bypassable, or drifted");
  return {
    workflow: GITHUB_WORKFLOW,
    successfulPushRunIds: matchingRuns.map((run) => run.id).sort((left, right) => left - right),
    rulesetId: ruleset.id,
    immutable: true,
    attestedAt: new Date(deps.now()).toISOString(),
  };
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
  assert(value?.schemaVersion === 2, "unsupported release state schema");
  assert(JSON.stringify(value.target) === JSON.stringify(TARGET), "release state target drifted");
  assert(value?.legacy?.deploymentId === LEGACY.deploymentId
    && canonicalDigest(value?.legacy?.imageDigest) === LEGACY.imageDigest
    && value?.legacy?.snapshotId === LEGACY.snapshotId,
  "release state legacy rollback point drifted");
  assert(SHA.test(value?.commit || "") && SHA.test(value?.artifactTree || ""),
    "release state artifact identity is invalid");
  assert(JSON.stringify(value?.githubSource) === JSON.stringify(GITHUB_SOURCE),
    "release state GitHub source drifted");
  assert(value.preflightSourceMetadataRetained == null
    || typeof value.preflightSourceMetadataRetained === "boolean",
  "release state inert source baseline is invalid");
  assert(value?.githubRelease?.workflow === GITHUB_WORKFLOW
    && Array.isArray(value.githubRelease.successfulPushRunIds)
    && value.githubRelease.successfulPushRunIds.length >= 1
    && value.githubRelease.successfulPushRunIds.every((id) => Number.isSafeInteger(id))
    && Number.isSafeInteger(value.githubRelease.rulesetId)
    && value.githubRelease.immutable === true
    && Number.isFinite(Date.parse(value.githubRelease.attestedAt || "")),
  "release state GitHub workflow or immutability attestation is invalid");
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
  if (value.phase === "github_source_connect_armed") validateUploadIntent(value);
  if (value.pendingTriggerCreate) validateTriggerCreateIntent(value.pendingTriggerCreate, value);
  if (value.sourceTrigger) validateSourceTriggerRecord(value.sourceTrigger, value.githubSource);
  if (value.pendingTriggerDisable) {
    validateTriggerDisableIntent(value.pendingTriggerDisable, value.githubSource);
  }
  if (value.sourceTriggerControl?.githubTriggerDisabled === true) {
    validateCompletedTriggerControl(value.sourceTriggerControl);
  }
  if (value.pendingRedeploy) validateRedeployIntent(value);
  return value;
}

function validateUploadIntent(state) {
  assert(JSON.stringify(state.githubSource) === JSON.stringify(GITHUB_SOURCE),
    "release state source-connect intent is invalid");
  assert(state.uploadPriorActiveDeploymentId === LEGACY.deploymentId,
    "release state source-connect baseline drifted");
  assert(Array.isArray(state.uploadBaselineDeploymentIds)
    && state.uploadBaselineDeploymentIds.length > 0
    && new Set(state.uploadBaselineDeploymentIds).size
      === state.uploadBaselineDeploymentIds.length
    && state.uploadBaselineDeploymentIds.every((id) => UUID.test(id)),
  "release state source-connect history baseline is invalid");
  assert(Number.isFinite(Date.parse(state.uploadArmedAt || "")),
    "release state source-connect intent has no timestamp");
  assert(state.rollbackBoundary === "legacy_allowed_before_enforce"
    && state.fundedAttemptMayHaveStarted !== true,
  "release state source-connect intent crossed the funded-attempt boundary");
  return state.githubSource;
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
  if (intent.purpose === "legacy_recovery") {
    assert(intent.sourceSnapshotId === LEGACY.snapshotId,
      "release state legacy recovery snapshot drifted");
  }
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

function sourceDeploymentMatches(row, state) {
  return row?.meta?.repo === state.githubSource.repository
    && row?.meta?.branch === state.githubSource.branch
    && String(row?.meta?.commitHash || "").toLowerCase() === state.commit;
}

function assertSourceDeploymentIntent(row, state, now) {
  assert(sourceDeploymentMatches(row, state),
    "source-connected deployment does not match the exact repository, branch, and commit");
  assertDeploymentCreatedForIntent(
    row,
    state.uploadArmedAt,
    now,
    "GitHub source connection",
  );
  return row;
}

async function waitForSourceDeployment(deps, state, options) {
  const baseline = new Set(state.uploadBaselineDeploymentIds);
  const attempts = Math.max(1, Math.ceil(options.timeoutMs / options.pollMs));
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const additions = (await listDeployments(deps)).filter((row) => !baseline.has(row.id));
    assert(additions.length <= 1,
      "GitHub source connection created an ambiguous deployment-history delta");
    if (additions.length === 1) {
      return assertSourceDeploymentIntent(additions[0], state, deps.now());
    }
    if (attempt + 1 < attempts) await deps.sleep(options.pollMs);
  }
  fail("GitHub source connection produced no uniquely observable deployment; refusing a second deploy");
}

async function observeSourceConnectDeltaThroughQuiescence(deps, state, options, label) {
  const attempts = Math.max(
    2,
    Math.ceil(Math.min(options.timeoutMs, SOURCE_CONNECT_QUIESCENCE_MS) / options.pollMs) + 1,
  );
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const additions = await postIntentDeploymentDelta(
      deps,
      state.uploadBaselineDeploymentIds,
      label,
    );
    assert(additions.length <= 1, `${label} has an ambiguous deployment-history delta`);
    if (additions.length === 1) return additions;
    if (attempt + 1 < attempts) await deps.sleep(options.pollMs);
  }
  return [];
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
  await assertRemoteBranchHead(deps, options.githubSource, options.commit);
  const githubRelease = await assertGitHubReleaseGuardrails(
    deps,
    options.githubSource,
    options.commit,
  );
  await resolveTarget(deps);
  const domains = await getDomains(deps);
  const inertSource = await assertInertDeploySourceAuthority(
    deps,
    options.githubSource,
  );
  await assertNoProductionStagedPatch(deps);
  // Source build, identity-bound shadow redeploy, enforce redeploy, and one
  // recovery redeploy must all fit without hitting Railway's 1000-row cap.
  const history = await listDeployments(deps, 4);
  assert(!history.some((row) => IN_FLIGHT.has(String(row?.status || "").toUpperCase())),
    "production already has an in-flight deployment");
  const prior = await assertOneActive(deps, LEGACY.deploymentId, LEGACY.imageDigest);
  assert(prior.canRollback === true && prior.snapshotId === LEGACY.snapshotId,
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
  assert(exactPrior.snapshotId === LEGACY.snapshotId,
    "exact legacy deployment snapshot changed");
  const backup = await getBackupEvidence(deps, options.volumeInstanceId, deps.now());
  const state = {
    schemaVersion: 2,
    phase: "preflight_passed",
    rollbackBoundary: "legacy_allowed_before_enforce",
    fundedExecutionSupported: false,
    volumeRestoreAllowed: false,
    target: TARGET,
    domains,
    commit: options.commit,
    artifactTree,
    artifactRoot: options.artifactRoot,
    githubSource: options.githubSource,
    preflightSourceMetadataRetained: inertSource.sourceMetadataRetained,
    githubRelease,
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
  assert(!state.pendingTriggerCreate
    && !state.pendingTriggerDisable
    && state.sourceTriggerControl?.githubTriggerDisabled !== true,
  "deploy-shadow refuses an active production trigger-disable recovery");
  assert(state.phase === "preflight_passed", "deploy-shadow requires a fresh passed preflight");
  assert(state.commit === options.commit, "deploy-shadow commit differs from preflight");
  const artifactTree = await assertExactGitHead(deps, options.artifactRoot, options.commit);
  assert(state.artifactTree === artifactTree, "artifact Git tree differs from preflight");
  assert(state.artifactRoot === options.artifactRoot, "artifact root differs from preflight");
  await assertRemoteBranchHead(deps, state.githubSource, state.commit);
  state.githubRelease = await assertGitHubReleaseGuardrails(
    deps,
    state.githubSource,
    state.commit,
  );
  await resolveTarget(deps);
  await getDomains(deps);
  await assertInertDeploySourceAuthority(
    deps,
    state.githubSource,
    state.preflightSourceMetadataRetained,
  );
  await assertNoProductionStagedPatch(deps);
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
    RELEASE_GIT_REPOSITORY: state.githubSource.repository,
    RELEASE_GIT_BRANCH: state.githubSource.branch,
    RELEASE_IMAGE_DIGEST: "unbound",
    RELEASE_IDENTITY_PHASE: "source_build",
  });
  await assertRemoteBranchHead(deps, state.githubSource, state.commit);
  state.githubRelease = await assertGitHubReleaseGuardrails(
    deps,
    state.githubSource,
    state.commit,
  );
  await assertOneActive(deps, LEGACY.deploymentId, LEGACY.imageDigest);
  await assertInertDeploySourceAuthority(
    deps,
    state.githubSource,
    state.preflightSourceMetadataRetained,
  );
  await assertNoProductionStagedPatch(deps);
  const finalRows = await listDeployments(deps, 4);
  assert(JSON.stringify(finalRows.map((row) => row.id).sort())
    === JSON.stringify(before.map((row) => row.id).sort()),
  "deployment history changed while shadow variables were staged");
  state.phase = "github_source_connect_armed";
  state.uploadPriorActiveDeploymentId = LEGACY.deploymentId;
  state.uploadBaselineDeploymentIds = before.map((row) => row.id);
  state.uploadArmedAt = new Date(deps.now()).toISOString();
  state.updatedAt = new Date(deps.now()).toISOString();
  await atomicWriteState(options.stateFile, state);
  const productionSourcePatch = {
    services: {
      [TARGET.service]: {
        source: {
          repo: state.githubSource.repository,
          branch: state.githubSource.branch,
          commitSha: state.commit,
          checkSuites: true,
        },
      },
    },
  };
  const connection = await deps.run([
    "railway", "environment", "edit", "--json",
    "--project", TARGET.project, "--environment", TARGET.environment,
    "--message", `Connect protected GitHub production source ${state.commit}`,
  ], { timeoutMs: 180_000, stdin: `${JSON.stringify(productionSourcePatch)}\n` });
  const connectionFailure = railwaySourceConnectFailure(connection);
  let sourceDeployment;
  if (connectionFailure) {
    const additions = await observeSourceConnectDeltaThroughQuiescence(
      deps,
      state,
      options,
      "GitHub source connection after provider-reported failure",
    );
    if (additions.length === 0) fail(connectionFailure);
    sourceDeployment = assertSourceDeploymentIntent(additions[0], state, deps.now());
  } else {
    railwaySourceConnectOutput(connection);
    sourceDeployment = await waitForSourceDeployment(deps, state, options);
  }
  const deploymentId = sourceDeployment.id;
  const sourceTrigger = await ensureExpectedSourceTrigger(
    deps,
    state,
    sourceDeployment,
    options,
  );
  state.candidate = {
    buildDeploymentId: deploymentId,
    imageDigest: null,
    shadowDeploymentId: null,
  };
  state.uploadSource = {
    kind: "github",
    repository: state.githubSource.repository,
    branch: state.githubSource.branch,
    commit: state.commit,
    tree: state.artifactTree,
    waitForCi: true,
  };
  state.sourceTrigger = sourceTrigger;
  delete state.pendingTriggerCreate;
  state.sourceConnectProcess = {
    providerReportedSuccess: connectionFailure === null,
    exactMutationObservedAfterProviderFailure: connectionFailure !== null,
    reconciledAt: new Date(deps.now()).toISOString(),
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

  // Railway's immutable image digest only exists after the source build. Bind
  // it into the runtime by staging the observed digest without a deploy, then
  // redeploy that exact deployment ID/image.  The hosted audit runs only on
  // this second, fully identity-bound shadow revision.
  await setVariable(deps, "RELEASE_IMAGE_DIGEST", digest);
  await setVariable(deps, "RELEASE_IDENTITY_PHASE", "bound");
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
    "identity-bound shadow redeploy did not use the exact source-built image");
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

function assertRollbackSourceBinding(source, intent, label) {
  const status = String(source?.status || "").toUpperCase();
  assert(source?.id === intent.sourceDeploymentId
    && imageDigest(source) === canonicalDigest(intent.expectedImageDigest)
    && source.snapshotId === intent.sourceSnapshotId
    && source.canRollback === true
    && ROLLBACK_SOURCE_STATUSES.has(status),
  `${label} source no longer matches its rollback-capable immutable snapshot`);
  return source;
}

async function assertRollbackPostIntentBinding(deps, intent, addition, label) {
  assert(addition?.id !== intent.sourceDeploymentId
    && addition?.id !== intent.priorActiveDeploymentId
    && !intent.baselineDeploymentIds.includes(addition?.id),
  `${label} did not create one new deployment id`);
  assertDeploymentCreatedForIntent(addition, intent.armedAt, deps.now(), label);
  assert(addition?.meta?.reason === "rollback",
    `${label} history row is not strictly marked as a rollback`);
  assert(imageDigest(addition) === canonicalDigest(intent.expectedImageDigest),
    `${label} history row does not match the intent-bound image`);
  const source = assertRollbackSourceBinding(
    await getExactDeployment(deps, intent.sourceDeploymentId),
    intent,
    label,
  );
  const exact = await getExactDeployment(deps, addition.id);
  assert(exact?.id === addition.id
    && exact.snapshotId === intent.sourceSnapshotId
    && imageDigest(exact) === canonicalDigest(intent.expectedImageDigest)
    && exact?.meta?.reason === "rollback",
  `${label} exact deployment is target-, image-, snapshot-, or reason-drifted`);
  return { source, exact };
}

async function waitForPostIntentRollback(deps, intent, options) {
  const attempts = Math.max(1, Math.ceil(options.timeoutMs / options.pollMs));
  for (let attempt = 0; attempt < attempts; attempt += 1) {
    const additions = await postIntentDeploymentDelta(
      deps,
      intent.baselineDeploymentIds,
      `${intent.purpose} rollback`,
    );
    assert(additions.length <= 1,
      `${intent.purpose} rollback mutation has an ambiguous deployment-history delta`);
    if (additions.length === 1) return additions[0];
    if (attempt + 1 < attempts) await deps.sleep(options.pollMs);
  }
  fail(`${intent.purpose} rollback mutation outcome is not yet observable; refusing to retry`);
}

async function exactRollback(deps, intent, options) {
  assert(intent?.mutationKind === "rollback", "exact rollback requires an armed rollback intent");
  const data = await railwayApi(
    deps,
    "mutation DirectProdRollback($id:String!){deploymentRollback(id:$id)}",
    { id: intent.sourceDeploymentId },
  );
  const result = data?.deploymentRollback;
  assert(result === true, "exact deployment rollback was not accepted");
  const addition = await waitForPostIntentRollback(deps, intent, options);
  await assertRollbackPostIntentBinding(
    deps,
    intent,
    addition,
    `${intent.purpose} rollback`,
  );
  return addition.id;
}

async function dispatchArmedDeploymentMutation(deps, intent, options) {
  if (intent.mutationKind === "rollback") {
    return exactRollback(deps, intent, options);
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
    if (purpose === "legacy_recovery") {
      assert(source.snapshotId === LEGACY.snapshotId,
        "legacy recovery source snapshot changed");
    }
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
  assert(!state.pendingTriggerCreate
    && !state.pendingTriggerDisable
    && state.sourceTriggerControl?.githubTriggerDisabled !== true,
  "promote-enforce refuses an active production trigger-disable recovery");
  assert(state.phase === "shadow_validated", "promote-enforce requires a validated shadow soak");
  assert(state.commit === options.commit, "promote-enforce commit differs from shadow");
  await assertExactGitHead(deps, state.artifactRoot, options.commit);
  await assertRemoteBranchHead(deps, state.githubSource, state.commit);
  state.githubRelease = await assertGitHubReleaseGuardrails(
    deps,
    state.githubSource,
    state.commit,
  );
  await resolveTarget(deps);
  await getDomains(deps);
  await assertExpectedSourceAuthority(deps, state.githubSource);
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
    RELEASE_IDENTITY_PHASE: "bound",
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
    state.pendingTriggerCreate?.sourceDeploymentId,
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
  if (failedUpload?.kind === "github_source"
    && failedUpload.deploymentId === expectedId
    && failedUpload.expectedImageDigest == null) {
    const status = String(row?.status || "").toUpperCase();
    assert(TERMINAL_FAILURES.has(status)
      && status === failedUpload.providerStatus
      && status === failedUpload.status
      && sourceDeploymentMatches(row, state)
      && failedUpload.commit === state.commit
      && state.rollbackBoundary === "legacy_allowed_before_enforce"
      && state.fundedAttemptMayHaveStarted !== true
      && !imageDigest(row)
      && (row.instances || []).every(
        (instance) => String(instance?.status || "").toUpperCase() !== "RUNNING",
      ),
    `${label} is not the exact stopped no-image source-build failure`);
    return row;
  }
  assert(expectedId === state.candidate?.buildDeploymentId
    && sourceDeploymentMatches(row, state)
    && state.uploadSource?.kind === "github"
    && state.uploadSource?.repository === state.githubSource.repository
    && state.uploadSource?.branch === state.githubSource.branch
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

async function postIntentDeploymentDelta(deps, baselineIds, label) {
  const history = await listDeployments(deps, 1);
  const currentIds = new Set(history.map((row) => row.id));
  assert(baselineIds.every((id) => currentIds.has(id)),
    `${label} deployment-history baseline is no longer complete`);
  const baseline = new Set(baselineIds);
  return history.filter((row) => !baseline.has(row.id));
}

async function uniquePostIntentDeployment(deps, baselineIds, label) {
  const additions = await postIntentDeploymentDelta(deps, baselineIds, label);
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
  if (state.phase === "github_source_connect_armed") {
    validateUploadIntent(state);
    const addition = await uniquePostIntentDeployment(
      deps,
      state.uploadBaselineDeploymentIds,
      "GitHub source connection",
    );
    assert(sourceDeploymentMatches(addition, state),
      "post-intent deployment is not bound to the exact GitHub source");
    assertDeploymentCreatedForIntent(
      addition,
      state.uploadArmedAt,
      deps.now(),
      "GitHub source connection",
    );
    const recoveryAuthority = await getSourceAuthority(deps);
    let triggerDisableIsInert = false;
    if (state.pendingTriggerDisable
      || state.sourceTriggerControl?.githubTriggerDisabled === true
      || recoveryAuthority.triggers.length === 0
      || (recoveryAuthority.triggers.length === 1
        && (recoveryAuthority.triggers[0].checkSuites !== true
          || !Number.isSafeInteger(recoveryAuthority.triggers[0].validCheckSuites)
          || recoveryAuthority.triggers[0].validCheckSuites < 1))) {
      await disableExpectedSourceTrigger(deps, state, options);
      await assertCompletedTriggerControlAuthority(
        deps,
        state,
        "completed trigger-disable authority drifted during source reconciliation",
      );
      triggerDisableIsInert = true;
    }
    if (!triggerDisableIsInert) {
      await assertExpectedSourceAuthority(
        deps,
        state.githubSource,
        { requireWaitForCi: false },
      );
      await enableWaitForCi(deps, state.githubSource);
    }
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
        "failed GitHub source deployment",
        {
          allowTerminalFailedActive: true,
          expectedImageDigest: observedDigest || null,
        },
      );
      assert(exact?.id === addition.id
        && sourceDeploymentMatches(exact, state)
        && stopped
        && (observedDigest
          ? DIGEST.test(observedDigest) && observedDigest !== LEGACY.imageDigest
          : TERMINAL_FAILURES.has(providerStatus)),
      "failed GitHub source deployment is not the exact stopped source-bound build outcome");
      state.lastFailedMutation = {
        kind: "github_source",
        deploymentId: addition.id,
        status: outcome,
        providerStatus,
        repository: state.githubSource.repository,
        branch: state.githubSource.branch,
        commit: state.commit,
        expectedImageDigest: observedDigest || null,
        reconciledAt: new Date(deps.now()).toISOString(),
      };
      state.phase = "github_source_deploy_failed";
      preserveObservedActive(state, active);
      state.updatedAt = new Date(deps.now()).toISOString();
      await atomicWriteState(options.stateFile, state);
      return state;
    }
    assert(active.length === 1 && addition.id === active[0].id
      && exact?.id === active[0].id && runningSuccess(exact) && runningSuccess(active[0]),
    "source-bound shadow build is not the exact active running-success deployment");
    const digest = imageDigest(exact);
    assert(DIGEST.test(digest) && digest !== LEGACY.imageDigest,
      "reconciled GitHub source build has no new immutable image");
    state.candidate = {
      buildDeploymentId: addition.id,
      imageDigest: digest,
      shadowDeploymentId: null,
    };
    state.uploadSource = {
      kind: "github",
      repository: state.githubSource.repository,
      branch: state.githubSource.branch,
      commit: state.commit,
      tree: state.artifactTree,
      waitForCi: true,
    };
    state.current = { deploymentId: addition.id, imageDigest: digest, mode: "shadow_pending" };
    state.phase = "shadow_deployment_bound";
    state.lastAcceptedUpload = {
      deploymentId: addition.id,
      repository: state.githubSource.repository,
      branch: state.githubSource.branch,
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
    let source;
    let exact;
    if (intent.mutationKind === "rollback") {
      ({ source, exact } = await assertRollbackPostIntentBinding(
        deps,
        intent,
        addition,
        `${intent.purpose} rollback reconciliation`,
      ));
    } else {
      assertDeploymentCreatedForIntent(
        addition,
        intent.armedAt,
        deps.now(),
        `${intent.purpose} exact redeploy`,
      );
      source = await getExactDeployment(deps, intent.sourceDeploymentId);
      exact = await getExactDeployment(deps, addition.id);
      const sourceStatus = String(source?.status || "").toUpperCase();
      assert(source
        && imageDigest(source) === canonicalDigest(intent.expectedImageDigest)
        && source.snapshotId === intent.sourceSnapshotId
        && !IN_FLIGHT.has(sourceStatus)
        && ["SUCCESS", "REMOVED", "CRASHED"].includes(sourceStatus),
      "reconciled deployment source no longer matches its immutable snapshot");
    }
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
      && imageDigest(active[0]) === canonicalDigest(intent.expectedImageDigest)
      && (intent.mutationKind !== "rollback"
        || (active[0].snapshotId === intent.sourceSnapshotId
          && active[0]?.meta?.reason === "rollback")),
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
  const triggerControlAfterRecovery = await disableExpectedSourceTrigger(
    deps,
    state,
    options,
  );
  state.recovery.githubTriggerDisabled = true;
  state.recovery.triggerDeletionMayHaveBeenAttempted =
    triggerControlAfterRecovery.triggerDeletionMayHaveBeenAttempted
    || state.sourceTriggerControl?.triggerDeletionMayHaveBeenAttempted === true;
  state.recovery.sourceMetadataRetained = triggerControlAfterRecovery.sourceMetadataRetained;
  state.recovery.githubTriggerDisabledAt = triggerControlAfterRecovery.disabledAt;
  state.phase = binding.afterBoundary ? "recovered_same_commit_shadow" : "recovered_legacy";
  state.recovery.completedAt = new Date(deps.now()).toISOString();
  state.updatedAt = new Date(deps.now()).toISOString();
  await atomicWriteState(options.stateFile, state);
  return state;
}

function containPendingTriggerCreate(state, now) {
  if (!state.pendingTriggerCreate) return;
  const intent = validateTriggerCreateIntent(state.pendingTriggerCreate, state);
  const control = validateCompletedTriggerControl(state.sourceTriggerControl);
  state.triggerCreateProcess = {
    sourceDeploymentId: intent.sourceDeploymentId,
    triggerId: control.triggerId || intent.observedTriggerId || null,
    mutationMayHaveBeenAttempted: intent.dispatchArmedAt != null,
    providerReportedSuccess: intent.providerReportedSuccess ?? null,
    containedBeforeRecovery: true,
    containedAt: new Date(now).toISOString(),
  };
  delete state.pendingTriggerCreate;
}

async function recover(options, deps) {
  let state = await readState(options.stateFile);
  await resolveTarget(deps);
  await getDomains(deps);
  let authority = await getSourceAuthority(deps);
  assert((authority.source.repo == null || authority.source.repo === state.githubSource.repository)
    && authority.source.image == null
    && authority.triggers.length <= 1
    && authority.triggers.every((trigger) => triggerMatchesSource(trigger, state.githubSource)),
  "recovery refuses an unexpected production source or trigger");
  let active = await getActive(deps);
  assert(active.length <= 1, "recovery refuses an ambiguous multi-active target");
  assert((await listDeployments(deps, 1)).every(
    (row) => !IN_FLIGHT.has(String(row.status).toUpperCase()),
  ), "recovery refuses while another deployment is in flight");
  if (state.phase === "github_source_connect_armed"
    && (state.pendingTriggerCreate
      || state.pendingTriggerDisable
      || state.sourceTriggerControl?.githubTriggerDisabled === true)) {
    await disableExpectedSourceTrigger(deps, state, options);
    authority = await getSourceAuthority(deps);
    active = await getActive(deps);
  }
  if (state.phase === "github_source_connect_armed" || state.pendingRedeploy) {
    if (state.phase === "github_source_connect_armed") {
      const additions = await observeSourceConnectDeltaThroughQuiescence(
        deps,
        state,
        options,
        "GitHub source recovery",
      );
      if (additions.length === 0) {
        assert(active.length === 1 && active[0].id === LEGACY.deploymentId
          && runningSuccess(active[0]) && imageDigest(active[0]) === LEGACY.imageDigest,
        "source-connect recovery without a deployment requires the exact legacy active image");
        const triggerControl = await disableExpectedSourceTrigger(
          deps,
          state,
          { ...options, forceFreshTriggerControl: true },
        );
        assert(triggerControl.handledLateDeploymentId == null,
          "production trigger disable observed a late source-connected deployment; rerun recovery");
        await assertCompletedTriggerControlAuthority(
          deps,
          state,
          "production trigger authority reappeared after disable quiescence",
        );
        const finalSourceAdditions = await postIntentDeploymentDelta(
          deps,
          state.uploadBaselineDeploymentIds,
          "GitHub source recovery final fence",
        );
        assert(finalSourceAdditions.length === 0,
          "a source-connected deployment appeared after the zero-deploy observation; rerun recovery");
        const finalActive = await getActive(deps);
        assert(finalActive.length === 1
          && finalActive[0].id === LEGACY.deploymentId
          && runningSuccess(finalActive[0])
          && imageDigest(finalActive[0]) === LEGACY.imageDigest,
        "legacy production changed during source-connect recovery quiescence");
        state.recovery = {
          reason: options.reason,
          sourceDeploymentId: LEGACY.deploymentId,
          expectedImageDigest: LEGACY.imageDigest,
          rollbackPerformed: false,
          githubTriggerDisabled: true,
          triggerDeletionMayHaveBeenAttempted:
            triggerControl.triggerDeletionMayHaveBeenAttempted,
          sourceMetadataRetained: triggerControl.sourceMetadataRetained,
          githubTriggerDisabledAt: triggerControl.disabledAt,
          volumeRestoreAttempted: false,
          completedAt: new Date(deps.now()).toISOString(),
        };
        state.current = {
          deploymentId: LEGACY.deploymentId,
          imageDigest: LEGACY.imageDigest,
          mode: "legacy",
        };
        state.phase = "recovered_legacy";
        state.updatedAt = new Date(deps.now()).toISOString();
        await atomicWriteState(options.stateFile, state);
        return state;
      }
      active = await getActive(deps);
    }
    state = await reconcileArmedMutation(state, active, options, deps);
    active = await getActive(deps);
  }
  const triggerControlBeforeRecovery = await disableExpectedSourceTrigger(
    deps,
    state,
    options,
  );
  containPendingTriggerCreate(state, deps.now());
  if (triggerControlBeforeRecovery.githubTriggerDisabled) {
    state.githubTriggerDisabledBeforeRecoveryAt = triggerControlBeforeRecovery.disabledAt;
    state.updatedAt = new Date(deps.now()).toISOString();
    await atomicWriteState(options.stateFile, state);
  }
  assert(!active[0]?.id || knownDeploymentIds(state).has(active[0].id),
    "recovery refuses an unknown active deployment");
  const noRollbackSourceFailure = [
    "github_source_connect_armed",
    "github_source_deploy_failed",
    "shadow_deployment_bound",
  ].includes(state.phase);
  if (noRollbackSourceFailure
    && state.rollbackBoundary === "legacy_allowed_before_enforce"
    && active.length === 1
    && active[0].id === LEGACY.deploymentId
    && runningSuccess(active[0])
    && imageDigest(active[0]) === LEGACY.imageDigest) {
    const triggerControl = await disableExpectedSourceTrigger(deps, state, options);
    state.recovery = {
      reason: options.reason,
      sourceDeploymentId: LEGACY.deploymentId,
      expectedImageDigest: LEGACY.imageDigest,
      rollbackPerformed: false,
      githubTriggerDisabled: true,
      triggerDeletionMayHaveBeenAttempted:
        triggerControl.triggerDeletionMayHaveBeenAttempted
        || triggerControlBeforeRecovery.triggerDeletionMayHaveBeenAttempted,
      sourceMetadataRetained: triggerControl.sourceMetadataRetained,
      githubTriggerDisabledAt: triggerControl.disabledAt,
      volumeRestoreAttempted: false,
      completedAt: new Date(deps.now()).toISOString(),
    };
    state.current = {
      deploymentId: LEGACY.deploymentId,
      imageDigest: LEGACY.imageDigest,
      mode: "legacy",
    };
    state.phase = "recovered_legacy";
    state.updatedAt = new Date(deps.now()).toISOString();
    await atomicWriteState(options.stateFile, state);
    return state;
  }

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
      RELEASE_IDENTITY_PHASE: "bound",
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
  const deploymentId = await dispatchArmedDeploymentMutation(
    deps,
    state.pendingRedeploy,
    options,
  );
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

export function summarizeReleaseState(state) {
  const triggerControl = state.sourceTriggerControl;
  return {
    ok: true,
    phase: state.phase,
    deploymentId: state.current?.deploymentId || null,
    imageDigest: state.current?.imageDigest || null,
    rollbackBoundary: state.rollbackBoundary,
    fundedExecutionSupported: false,
    volumeRestoreAttempted: false,
    githubTriggerDisabled: state.recovery?.githubTriggerDisabled === true
      || triggerControl?.githubTriggerDisabled === true,
    sourceMetadataRetained: state.recovery?.sourceMetadataRetained
      ?? triggerControl?.sourceMetadataRetained
      ?? null,
    triggerDeletionMayHaveBeenAttempted:
      state.recovery?.triggerDeletionMayHaveBeenAttempted === true
      || triggerControl?.triggerDeletionMayHaveBeenAttempted === true,
    triggerId: triggerControl?.triggerId || null,
    triggerDisableProviderReportedSuccess: triggerControl?.providerReportedSuccess ?? null,
    triggerCreateMayHaveBeenAttempted: state.sourceTrigger?.mutationMayHaveBeenAttempted === true
      || state.pendingTriggerCreate?.dispatchArmedAt != null
      || state.triggerCreateProcess?.mutationMayHaveBeenAttempted === true,
    triggerCreateProviderReportedSuccess: state.sourceTrigger?.providerReportedSuccess
      ?? state.pendingTriggerCreate?.providerReportedSuccess
      ?? state.triggerCreateProcess?.providerReportedSuccess
      ?? null,
    githubTriggerDisabledAt: state.recovery?.githubTriggerDisabledAt
      || triggerControl?.disabledAt
      || null,
  };
}

async function main() {
  const state = await executeReleaseCommand(process.argv.slice(2));
  process.stdout.write(`${JSON.stringify(summarizeReleaseState(state))}\n`);
}

if (isDirectExecution(process.argv[1], import.meta.url)) {
  main().catch((error) => {
    process.stderr.write(`direct production release refused: ${error.message}\n`);
    process.exitCode = 1;
  });
}
