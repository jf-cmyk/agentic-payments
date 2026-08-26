#!/usr/bin/env node

import { access } from "node:fs/promises";

import {
  FAILED_STATUSES,
  NONTERMINAL_STATUSES,
  PRE_RUNTIME_STATUSES,
  UUID_PATTERN,
  atomicWriteJson,
  deploymentHasRunningInstance,
  deploymentIsStopped,
  deploymentOccupiesActiveSet,
  fail,
  fetchHealthSnapshot,
  listDeployments as controlListDeployments,
  parseDeploymentList,
  readExactServiceVariable,
  railwayApi as controlRailwayApi,
  readJson,
  recentMessageMatches,
  requireDeploymentTarget,
  setExactServiceVariable,
  sleep,
  validateLegacyBridgeReadiness,
  validateLegacyBridgeState,
} from "./railway_release_control.mjs";

const recoveryBudgetMs = Number(process.env.RELEASE_RECOVERY_TIMEOUT_MS || "900000");
if (
  !Number.isSafeInteger(recoveryBudgetMs)
  || recoveryBudgetMs < 60_000
  || recoveryBudgetMs > 960_000
) {
  fail("RELEASE_RECOVERY_TIMEOUT_MS must be between 60000 and 960000");
}
const recoveryReadAttemptTimeoutMs = Number(
  process.env.RELEASE_RECOVERY_READ_ATTEMPT_TIMEOUT_MS || "10000",
);
if (
  !Number.isSafeInteger(recoveryReadAttemptTimeoutMs)
  || recoveryReadAttemptTimeoutMs < 250
  || recoveryReadAttemptTimeoutMs > 10_000
) {
  fail("RELEASE_RECOVERY_READ_ATTEMPT_TIMEOUT_MS must be between 250 and 10000");
}
const recoveryDeadline = Date.now() + recoveryBudgetMs;
const FINALIZATION_RESERVE_MS = 30_000;
const RAILWAY_TERMINATION_GRACE_MS = 2_500;

function operationalTimeRemaining() {
  return recoveryDeadline - FINALIZATION_RESERVE_MS - Date.now();
}

function boundedDeadline(requestedMs) {
  const remaining = operationalTimeRemaining();
  if (remaining <= 0) fail("Railway release recovery exceeded its global deadline");
  return Date.now() + Math.min(requestedMs, remaining);
}

async function recoverySleep(milliseconds) {
  const remaining = operationalTimeRemaining();
  if (remaining <= 0) fail("Railway release recovery exceeded its global deadline");
  await sleep(Math.min(milliseconds, remaining));
}

function railwayCallTimeout(maximumMs = 30_000) {
  const available = operationalTimeRemaining() - RAILWAY_TERMINATION_GRACE_MS;
  if (available < 250) {
    fail("Railway release recovery has no time left for another Railway request");
  }
  return Math.max(1, Math.floor(Math.min(maximumMs, available)));
}

function fetchCallTimeout(localDeadline, maximumMs = 15_000) {
  const available = Math.min(localDeadline - Date.now(), operationalTimeRemaining()) - 100;
  if (available < 100) return null;
  return Math.max(1, Math.floor(Math.min(maximumMs, available)));
}

async function railwayApi(query, variables = {}, maximumMs = 30_000) {
  return controlRailwayApi(query, variables, railwayCallTimeout(maximumMs));
}

async function retryRecoveryRead(
  operation,
  label,
  timeoutMs = 45_000,
  perAttemptTimeoutMs = recoveryReadAttemptTimeoutMs,
) {
  const deadline = boundedDeadline(timeoutMs);
  let lastError = null;
  while (Date.now() < deadline) {
    const attemptTimeoutMs = railwayReadTimeout(deadline, perAttemptTimeoutMs);
    if (attemptTimeoutMs == null) break;
    try {
      return await operation(attemptTimeoutMs);
    } catch (error) {
      lastError = error;
      await sleepBefore(deadline, 2_000);
    }
  }
  fail(`${label} failed after bounded retries: ${lastError?.message || "unknown error"}`);
}

function railwayReadTimeout(deadline, maximumMs = recoveryReadAttemptTimeoutMs) {
  const available = Math.min(deadline - Date.now(), operationalTimeRemaining())
    - RAILWAY_TERMINATION_GRACE_MS;
  if (available < 250) return null;
  return Math.max(1, Math.floor(Math.min(maximumMs, available)));
}

async function listDeployments(target, maximumMs = 30_000) {
  return controlListDeployments(target, railwayCallTimeout(maximumMs));
}

async function getDeployment(deploymentId, maximumMs = 30_000) {
  if (!UUID_PATTERN.test(deploymentId || "")) fail("invalid Railway deployment id");
  const data = await railwayApi(
    "query ExactDeployment($id: String!) { deployment(id: $id) { id projectId environmentId serviceId snapshotId status deploymentStopped canRollback createdAt meta instances { id status } } }",
    { id: deploymentId },
    maximumMs,
  );
  const deployment = data?.deployment || null;
  if (deployment && deployment.id !== deploymentId) {
    fail("Railway exact deployment query returned a different deployment id");
  }
  return deployment;
}

async function getActiveDeployments(target, maximumMs = 30_000) {
  const data = await railwayApi(
    "query ActiveDeployments($environmentId: String!, $serviceId: String!) { serviceInstance(environmentId: $environmentId, serviceId: $serviceId) { activeDeployments { id projectId environmentId serviceId snapshotId status deploymentStopped canRollback createdAt meta instances { id status } } } }",
    { environmentId: target.environment, serviceId: target.service },
    maximumMs,
  );
  const active = data?.serviceInstance?.activeDeployments;
  if (!Array.isArray(active)) {
    fail("Railway active-deployments query did not return an array");
  }
  return parseDeploymentList(JSON.stringify(active));
}

async function requestExactDeploymentEnd(deploymentId, mutationName) {
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

async function verifyTargetBaseUrl(target, baseUrl, maximumMs = 30_000) {
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
    maximumMs,
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
  if (!domains.includes(parsedBaseUrl.hostname.toLowerCase())) {
    fail("release base URL is not attached to the exact Railway target");
  }
  return {
    custom: [...new Set(customDomains.map((entry) => String(entry.domain).toLowerCase()))].sort(),
    service: [...new Set(serviceDomains.map((entry) => String(entry.domain).toLowerCase()))].sort(),
  };
}

async function sleepBefore(deadline, milliseconds) {
  const remaining = deadline - Date.now();
  if (remaining <= 0) return;
  await recoverySleep(Math.min(milliseconds, remaining));
}

async function fetchRecoveryHealth(baseUrl, path, deadline) {
  const timeoutMs = fetchCallTimeout(deadline);
  if (timeoutMs == null) fail("health verification exceeded its bounded deadline");
  return fetchHealthSnapshot(baseUrl, path, timeoutMs);
}

async function verifyHealthRestored(state, timeoutMs = 120_000) {
  const deadline = boundedDeadline(timeoutMs);
  let lastProblem = "no response";
  let consecutiveMatches = 0;
  while (Date.now() < deadline) {
    try {
      const health = await fetchRecoveryHealth(state.baseUrl, "/health", deadline);
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
          const readiness = await fetchRecoveryHealth(state.baseUrl, "/readyz", deadline);
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
            await sleepBefore(deadline, 5_000);
            continue;
          }
        }
        consecutiveMatches += 1;
        if (consecutiveMatches >= 3) return;
        lastProblem = `health matched ${consecutiveMatches}/3 consecutive probes`;
        await sleepBefore(deadline, 2_000);
        continue;
      }
      consecutiveMatches = 0;
      lastProblem = `health returned ${health.status} version ${health.version || "missing"}`;
    } catch (error) {
      consecutiveMatches = 0;
      lastProblem = String(error);
    }
    await sleepBefore(deadline, 5_000);
  }
  fail(`prior release health was not restored: ${lastProblem}`);
}

async function endExactDeployment({ deploymentId, lastStatus, pollMs = 5_000 }) {
  console.error(`Ending unaudited Railway deployment ${deploymentId} exactly.`);
  const actionDeadline = boundedDeadline(30_000);
  let fallbackStatus = String(lastStatus || "").toUpperCase();
  let acknowledgedBy = null;
  let removalAlreadyInProgress = false;
  while (Date.now() < actionDeadline) {
    let observed = null;
    try {
      const attemptTimeoutMs = railwayReadTimeout(actionDeadline);
      if (attemptTimeoutMs == null) break;
      observed = await getDeployment(deploymentId, attemptTimeoutMs);
    } catch {
      // Retry boundedly before choosing any mutation from stale status.
    }
    if (observed && deploymentIsStopped(observed)) return;
    const status = String(observed?.status || fallbackStatus).toUpperCase();
    if (status === "REMOVING") {
      removalAlreadyInProgress = true;
      break;
    }
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
    await sleepBefore(actionDeadline, Math.min(pollMs, 2_000));
  }
  if (!acknowledgedBy && !removalAlreadyInProgress) {
    fail(`Railway did not acknowledge an exact cancel or stop for ${deploymentId}`);
  }
  if (acknowledgedBy) {
    console.error(`Railway acknowledged ${acknowledgedBy} for ${deploymentId}.`);
  }

  const deadline = boundedDeadline(90_000);
  while (Date.now() < deadline) {
    try {
      const attemptTimeoutMs = railwayReadTimeout(deadline);
      if (attemptTimeoutMs == null) break;
      const deployment = await getDeployment(deploymentId, attemptTimeoutMs);
      if (deploymentIsStopped(deployment)) {
        console.error(`Verified unaudited deployment ${deploymentId} is stopped.`);
        return;
      }
    } catch {
      // Retry boundedly; cleanup requires a positive stopped state.
    }
    await sleepBefore(deadline, Math.min(pollMs, 5_000));
  }
  fail(`could not verify that exact deployment ${deploymentId} stopped`);
}

function parseArguments(argv) {
  const values = new Map();
  for (let index = 0; index < argv.length; index += 2) {
    const flag = argv[index];
    const value = argv[index + 1];
    const name = flag?.slice(2);
    if (
      !flag?.startsWith("--")
      || value === undefined
      || !["state-file", "expected-commit", "drain-attestation-sha256"].includes(name)
      || values.has(name)
    ) {
      fail("invalid recovery arguments");
    }
    values.set(name, value);
  }
  if (!values.get("state-file")) {
    fail("--state-file is required");
  }
  const expectedCommit = values.get("expected-commit") || null;
  if (expectedCommit && !/^[0-9a-f]{40}$/.test(expectedCommit)) {
    fail("--expected-commit must be a full lowercase Git commit SHA");
  }
  const expectedDigest = values.get("drain-attestation-sha256") || null;
  if (expectedDigest && !/^[0-9a-f]{64}$/.test(expectedDigest)) {
    fail("--drain-attestation-sha256 must be a lowercase SHA-256");
  }
  return {
    stateFile: values.get("state-file"),
    expectedCommit,
    expectedDigest,
  };
}

function activeContenders(deployments) {
  return deployments.filter(deploymentOccupiesActiveSet);
}

function productionMessageBindsPrior(message, priorId) {
  const parts = String(message || "").split(":");
  return (
    parts.length === 7
    && parts[0] === "bsmcp"
    && parts[1] === "production"
    && /^\d+$/.test(parts[2])
    && /^\d+$/.test(parts[3])
    && /^[0-9a-f]{40}$/.test(parts[4])
    && parts[5] === "prev"
    && parts[6].toLowerCase() === String(priorId || "").toLowerCase()
  );
}

function productionMessageCommit(message) {
  const parts = String(message || "").split(":");
  return parts.length === 7 && /^[0-9a-f]{40}$/.test(parts[4]) ? parts[4] : null;
}

function validateState(state, options, { requireUnexpired = true } = {}) {
  if (
    state?.schemaVersion !== 1
    || !["staging", "production"].includes(state?.mode)
    || !state?.target
    || !UUID_PATTERN.test(state.target.project || "")
    || !UUID_PATTERN.test(state.target.environment || "")
    || !UUID_PATTERN.test(state.target.service || "")
    || !Array.isArray(state.beforeDeploymentIds)
    || !state.beforeDeploymentIds.every((id) => UUID_PATTERN.test(id || ""))
    || new Set(state.beforeDeploymentIds).size !== state.beforeDeploymentIds.length
    || !state?.message
    || !Number.isFinite(Number(state?.startedAtEpochMs))
    || Number(state.startedAtEpochMs) <= 0
    || !state?.candidate
    || (
      state.candidate.id != null
      && !UUID_PATTERN.test(state.candidate.id)
    )
  ) {
    fail("Railway release state is invalid");
  }
  if (state.mode === "production") {
    if (
      !UUID_PATTERN.test(state?.prior?.id || "")
      || state.prior.projectId !== state.target.project
      || state.prior.environmentId !== state.target.environment
      || state.prior.serviceId !== state.target.service
      || state.prior.canRollback !== true
      || !state.prior.imageDigest
      || !UUID_PATTERN.test(state.prior.snapshotId || "")
      || !state.prior.health?.version
      || !state.backup?.backupId
      || !state.beforeDeploymentIds.includes(state.prior.id)
      || state.beforeDeploymentIds.includes(state?.candidate?.id)
      || !productionMessageBindsPrior(state.message, state.prior.id)
    ) {
      fail("production release state has no verified rollback and backup record");
    }
    const bridgeRequired = (
      state.prior.health?.version === "0.6.2"
      || state.prior.readiness?.legacyTransactionBridge?.economic_writes_locked === true
    );
    if (Boolean(state.legacyTransactionBridge) !== bridgeRequired) {
      fail("production release state is missing its required transaction bridge");
    }
    if (state.legacyTransactionBridge) {
      validateLegacyBridgeState(state.legacyTransactionBridge, {
        target: state.target,
        prior: state.prior,
        expectedCommit: options.expectedCommit || productionMessageCommit(state.message),
        expectedDigest: options.expectedDigest,
        requireUnexpired,
      });
      if (
        !state.bridgeVariable
        || state.bridgeVariable.name !== "LEGACY_TRANSACTION_BRIDGE_LOCK"
        || !Array.isArray(state.bridgeVariable.priorValues)
        || state.bridgeVariable.priorValues.length === 0
        || state.bridgeVariable.priorValues.some(
          (value) => value !== null && !["true", "false"].includes(value),
        )
        || new Set(state.bridgeVariable.priorValues).size
          !== state.bridgeVariable.priorValues.length
        || !Object.prototype.hasOwnProperty.call(
          state.bridgeVariable,
          "observedPriorValue",
        )
        || !state.bridgeVariable.priorValues.includes(
          state.bridgeVariable.observedPriorValue,
        )
        || !["true", "false"].includes(state.bridgeVariable.desiredValue)
        || typeof state.bridgeVariable.changeArmed !== "boolean"
        || typeof state.bridgeVariable.verified !== "boolean"
        || typeof state.bridgeVariable.restored !== "boolean"
      ) {
        fail("production release state has an invalid transaction bridge variable record");
      }
    }
  }
}

async function failLockBridgeVariable(state) {
  if (!state.legacyTransactionBridge || state.bridgeVariable?.changeArmed !== true) return;
  const current = await readExactServiceVariable(
    state.target,
    state.bridgeVariable.name,
    railwayCallTimeout(),
  );
  if (current !== "true") {
    await setExactServiceVariable(
      state.target,
      state.bridgeVariable.name,
      "true",
      railwayCallTimeout(),
    );
  }
  state.bridgeVariable.restored = state.bridgeVariable.desiredValue === "false";
  state.bridgeVariable.verified = state.bridgeVariable.desiredValue === "true";
  await atomicWriteJson(state.stateFile, state);
}

async function verifyBridgeRecoveryMode(state) {
  if (!state.legacyTransactionBridge) return;
  const current = await readExactServiceVariable(
    state.target,
    state.bridgeVariable.name,
    railwayCallTimeout(),
  );
  if (current !== "true") {
    fail("recovery did not retain the fail-closed transaction bridge variable");
  }
  if (state.legacyTransactionBridge.phase === "bridge_unlock") {
    const readiness = await fetchHealthSnapshot(
      state.baseUrl,
      "/readyz",
      railwayCallTimeout(15_000),
    );
    validateLegacyBridgeReadiness(readiness, state.legacyTransactionBridge, {
      expectedLocked: true,
    });
  } else {
    fail(
      "legacy bridge candidate may have mounted the shared volume; public v0.6.2 cannot "
      + "certify it after rollback, so independent logical-ledger inspection or backup "
      + "restore is required before recovery can be marked complete. Candidate status: "
      + `${String(state.candidate?.status || "unknown").toUpperCase()}. `
      + "A Railway terminal status is not proof that startup never ran. "
    );
  }
}

function validateCandidateIdentity(state, candidate) {
  if (!candidate) fail("exact Railway candidate could not be inspected");
  requireDeploymentTarget(candidate, state.target, "exact Railway candidate");
  if (state.beforeDeploymentIds.includes(candidate.id)) {
    fail("refusing recovery because the candidate predates this release attempt");
  }
  const createdAt = Date.parse(candidate.createdAt || "");
  if (
    !Number.isFinite(createdAt)
    || createdAt < Number(state.startedAtEpochMs) - 120_000
    || candidate?.meta?.cliMessage !== state.message
  ) {
    fail("refusing recovery because the exact candidate identity is not proven");
  }
  return candidate;
}

function validateArmedRollbackState(state, candidate) {
  const rollback = state.recovery;
  const expectedBeforeIds = new Set([...state.beforeDeploymentIds, candidate.id]);
  const armedAt = Number(rollback?.armedAtEpochMs);
  const candidateCreatedAt = Date.parse(candidate?.createdAt || "");
  if (
    rollback?.action !== "rollback_armed"
    || rollback.priorDeploymentId !== state.prior.id
    || rollback.candidateDeploymentId !== candidate.id
    || !Array.isArray(rollback.beforeDeploymentIds)
    || rollback.beforeDeploymentIds.length !== expectedBeforeIds.size
    || !rollback.beforeDeploymentIds.every((id) => UUID_PATTERN.test(id || ""))
    || new Set(rollback.beforeDeploymentIds).size !== rollback.beforeDeploymentIds.length
    || rollback.beforeDeploymentIds.some((id) => !expectedBeforeIds.has(id))
    || [...expectedBeforeIds].some((id) => !rollback.beforeDeploymentIds.includes(id))
    || !Number.isFinite(armedAt)
    || armedAt < Number(state.startedAtEpochMs) - 5_000
    || armedAt > Date.now() + 120_000
    || !Number.isFinite(Number(rollback.candidateCreatedAtEpochMs))
    || Number(rollback.candidateCreatedAtEpochMs) !== candidateCreatedAt
    || typeof rollback.mutationAcknowledged !== "boolean"
    || (
      rollback.mutationAcknowledged === true
      && rollback.mutationAttempted !== true
    )
    || (
      rollback.mutationAttempted != null
      && typeof rollback.mutationAttempted !== "boolean"
    )
    || (
      rollback.rollbackDeploymentId != null
      && (
        !UUID_PATTERN.test(rollback.rollbackDeploymentId)
        || expectedBeforeIds.has(rollback.rollbackDeploymentId)
      )
    )
  ) {
    fail("Railway rollback recovery state is invalid");
  }
  return rollback.beforeDeploymentIds;
}

async function resolveCandidate(state) {
  if (UUID_PATTERN.test(state?.candidate?.id || "")) return state.candidate.id;
  const earliest = Number(state.startedAtEpochMs) - 120_000;
  const deadline = boundedDeadline(120_000);
  while (Date.now() < deadline) {
    try {
      const attemptTimeoutMs = railwayReadTimeout(deadline);
      if (attemptTimeoutMs == null) break;
      const rows = await listDeployments(state.target, attemptTimeoutMs);
      const matches = recentMessageMatches(rows, state.message, earliest)
        .filter((row) => !state.beforeDeploymentIds.includes(row.id));
      if (matches.length > 1) fail("multiple Railway deployments matched the CI message");
      if (matches.length === 1) {
        state.candidate.id = matches[0].id;
        state.candidate.status = matches[0].status;
        state.candidate.imageDigest = matches[0]?.meta?.imageDigest || null;
        state.phase = "candidate_bound";
        return matches[0].id;
      }
    } catch (error) {
      if (String(error.message).includes("multiple Railway deployments")) throw error;
    }
    await sleepBefore(deadline, 5_000);
  }
  return null;
}

async function exactPriorIsActive(state) {
  const active = await retryRecoveryRead(
    (attemptTimeoutMs) => getActiveDeployments(state.target, attemptTimeoutMs),
    "active prior inspection",
  );
  for (const deployment of active) {
    requireDeploymentTarget(deployment, state.target, "active Railway deployment");
  }
  const contenders = activeContenders(active);
  const allowedIds = [
    state.prior.id,
    state?.candidate?.id,
    state?.recovery?.rollbackDeploymentId,
  ].filter(Boolean);
  if (contenders.some((deployment) => !allowedIds.includes(deployment.id))) {
    fail("an unexpected production deployment is active during recovery");
  }
  const restored = contenders.filter(
    (deployment) => (
      deployment.id === state.prior.id
      && String(deployment.status || "").toUpperCase() === "SUCCESS"
      && deployment.deploymentStopped !== true
      && deployment?.meta?.imageDigest === state.prior.imageDigest
      && deploymentHasRunningInstance(deployment)
    ),
  );
  if (contenders.length === 1 && restored.length === 1) return restored[0];
  return null;
}

async function getValidatedActiveDeployments(state, maximumMs = 30_000) {
  const active = await getActiveDeployments(state.target, maximumMs);
  for (const deployment of active) {
    requireDeploymentTarget(deployment, state.target, "active Railway deployment");
  }
  return activeContenders(active);
}

async function verifySoleActiveDeployment(state, deploymentId, imageDigest) {
  await retryRecoveryRead(
    (attemptTimeoutMs) => verifyTargetBaseUrl(state.target, state.baseUrl, attemptTimeoutMs),
    "restored target-domain verification",
  );
  const exact = requireDeploymentTarget(
    await retryRecoveryRead(
      (attemptTimeoutMs) => getDeployment(deploymentId, attemptTimeoutMs),
      "restored exact deployment inspection",
    ),
    state.target,
    "deployment verified after health restoration",
  );
  const active = await retryRecoveryRead(
    (attemptTimeoutMs) => getValidatedActiveDeployments(state, attemptTimeoutMs),
    "restored active deployment inspection",
  );
  if (
    String(exact?.status || "").toUpperCase() !== "SUCCESS"
    || exact?.deploymentStopped === true
    || exact?.meta?.imageDigest !== imageDigest
    || !(exact.instances || []).some(
      (instance) => String(instance?.status || "").toUpperCase() === "RUNNING",
    )
    || active.length !== 1
    || active[0].id !== deploymentId
  ) {
    fail("restored Railway deployment is no longer the sole active release");
  }
}

async function discoverRollbackDeployment(state, beforeIds, candidateId, timeoutMs) {
  const deadline = boundedDeadline(timeoutMs);
  let rollbackId = UUID_PATTERN.test(state?.recovery?.rollbackDeploymentId || "")
    ? state.recovery.rollbackDeploymentId
    : null;
  while (Date.now() < deadline) {
    if (!rollbackId) {
      let rows;
      try {
        const attemptTimeoutMs = railwayReadTimeout(deadline);
        if (attemptTimeoutMs == null) break;
        rows = await listDeployments(state.target, attemptTimeoutMs);
      } catch {
        await sleepBefore(deadline, 5_000);
        continue;
      }
      validatedHistoryIds(rows);
      const newRows = rows.filter((row) => !beforeIds.includes(row.id));
      if (newRows.length > 1) {
        fail("multiple deployments appeared while binding the exact rollback");
      }
      if (newRows.length === 1) {
        const createdAt = Date.parse(newRows[0]?.createdAt || "");
        if (
          !Number.isFinite(createdAt)
          || createdAt < Math.max(
            Number(state?.recovery?.candidateCreatedAtEpochMs || 0),
            Number(state?.recovery?.armedAtEpochMs || 0) - 5_000,
          )
        ) {
          fail("rollback deployment delta predates the armed rollback request");
        }
        rollbackId = newRows[0].id;
        if (rollbackId === candidateId) fail("rollback resolved back to the failed candidate");
        state.recovery.rollbackDeploymentId = rollbackId;
        await atomicWriteJson(state.stateFile, state);
        console.error(`Bound exact Railway rollback deployment ${rollbackId}.`);
      }
    }
    if (!rollbackId) {
      await sleepBefore(deadline, 5_000);
      continue;
    }

    let rollback;
    try {
      const attemptTimeoutMs = railwayReadTimeout(deadline);
      if (attemptTimeoutMs == null) break;
      rollback = await getDeployment(rollbackId, attemptTimeoutMs);
    } catch {
      await sleepBefore(deadline, 5_000);
      continue;
    }
    requireDeploymentTarget(rollback, state.target, "exact rollback deployment");
    const digest = rollback?.meta?.imageDigest || null;
    if (digest && digest !== state.prior.imageDigest) {
      fail("exact rollback deployment has an unexpected image digest");
    }
    const status = String(rollback?.status || "").toUpperCase();
    if (FAILED_STATUSES.has(status)) {
      fail(`exact rollback deployment reached terminal status ${status}`);
    }
    if (status === "SUCCESS") {
      const contenders = activeContenders([rollback]);
      if (
        contenders.length !== 1
        || rollback.deploymentStopped === true
        || !deploymentHasRunningInstance(rollback)
        || digest !== state.prior.imageDigest
      ) {
        fail("exact rollback deployment is not running the recorded prior image");
      }
      return rollback;
    }
    if (!NONTERMINAL_STATUSES.has(status)) {
      fail(`exact rollback deployment reported unknown status ${status || "missing"}`);
    }
    await sleepBefore(deadline, 5_000);
  }
  fail("timed out binding or activating the exact Railway rollback deployment");
}

function validatedHistoryIds(rows) {
  if (!Array.isArray(rows)) fail("Railway deployment history is not an array");
  const ids = rows.map((row) => row?.id);
  if (
    !ids.every((id) => UUID_PATTERN.test(id || ""))
    || new Set(ids).size !== ids.length
  ) {
    fail("Railway deployment history contains invalid or duplicate deployment ids");
  }
  return ids;
}

async function waitForExactPreRollbackHistory(state, candidate, timeoutMs = 60_000) {
  const expected = new Set([...state.beforeDeploymentIds, candidate.id]);
  const deadline = boundedDeadline(timeoutMs);
  let lastMissing = [...expected];
  while (Date.now() < deadline) {
    let rows;
    try {
      const attemptTimeoutMs = railwayReadTimeout(deadline);
      if (attemptTimeoutMs == null) break;
      rows = await listDeployments(state.target, attemptTimeoutMs);
    } catch {
      await sleepBefore(deadline, 5_000);
      continue;
    }
    const ids = validatedHistoryIds(rows);
    const extras = ids.filter((id) => !expected.has(id));
    if (extras.length > 0) {
      fail("an unrelated deployment appeared before the rollback was armed");
    }
    lastMissing = [...expected].filter((id) => !ids.includes(id));
    if (lastMissing.length === 0 && ids.length === expected.size) return ids;
    await sleepBefore(deadline, 5_000);
  }
  fail(
    `Railway deployment history remained incomplete before rollback (${lastMissing.length} missing)`,
  );
}

async function waitForExactPostRollbackHistory(
  state,
  beforeIds,
  rollbackId,
  timeoutMs = 30_000,
) {
  const expected = new Set([...beforeIds, rollbackId]);
  const deadline = boundedDeadline(timeoutMs);
  let lastMissing = [rollbackId];
  while (Date.now() < deadline) {
    let rows;
    try {
      const attemptTimeoutMs = railwayReadTimeout(deadline);
      if (attemptTimeoutMs == null) break;
      rows = await listDeployments(state.target, attemptTimeoutMs);
    } catch {
      await sleepBefore(deadline, 5_000);
      continue;
    }
    const ids = validatedHistoryIds(rows);
    const extras = ids.filter((id) => !expected.has(id));
    if (extras.length > 0) {
      fail("an unrelated deployment appeared during rollback recovery");
    }
    lastMissing = [...expected].filter((id) => !ids.includes(id));
    if (lastMissing.length === 0 && ids.length === expected.size) return;
    await sleepBefore(deadline, 5_000);
  }
  fail(
    `Railway deployment history remained incomplete after rollback (${lastMissing.length} missing)`,
  );
}

async function waitForCandidateStopped(state, candidateId, timeoutMs = 90_000) {
  const deadline = boundedDeadline(timeoutMs);
  while (Date.now() < deadline) {
    let candidate;
    try {
      const attemptTimeoutMs = railwayReadTimeout(deadline);
      if (attemptTimeoutMs == null) break;
      candidate = await getDeployment(candidateId, attemptTimeoutMs);
    } catch {
      await sleepBefore(deadline, 5_000);
      continue;
    }
    const exact = validateCandidateIdentity(state, candidate);
    if (deploymentIsStopped(exact)) return exact;
    await sleepBefore(deadline, 5_000);
  }
  fail("could not positively verify that the superseded production candidate stopped");
}

async function rollbackProduction(state, candidate) {
  const rollbackAlreadyArmed = state?.recovery?.action === "rollback_armed";
  let beforeIds = rollbackAlreadyArmed
    ? validateArmedRollbackState(state, candidate)
    : null;
  const alreadyRestored = rollbackAlreadyArmed ? null : await exactPriorIsActive(state);
  if (
    alreadyRestored
    && deploymentIsStopped(candidate)
  ) {
    await waitForExactPreRollbackHistory(state, candidate, 30_000);
    await verifyHealthRestored(state, 120_000);
    await verifySoleActiveDeployment(state, alreadyRestored.id, state.prior.imageDigest);
    console.error("The recorded prior production image is already active and healthy.");
    return { action: "prior_already_active", deploymentId: alreadyRestored.id };
  }

  const exactPrior = requireDeploymentTarget(
    await retryRecoveryRead(
      (attemptTimeoutMs) => getDeployment(state.prior.id, attemptTimeoutMs),
      "exact prior inspection",
    ),
    state.target,
    "recorded prior deployment",
  );
  if (
    exactPrior?.id !== state.prior.id
    || (!rollbackAlreadyArmed && exactPrior?.canRollback !== true)
    || exactPrior?.meta?.imageDigest !== state.prior.imageDigest
    || (state.prior.snapshotId && exactPrior?.snapshotId !== state.prior.snapshotId)
  ) {
    fail("recorded prior deployment is no longer an exact rollback target");
  }

  const rollbackWasArmed = rollbackAlreadyArmed;
  if (!rollbackWasArmed) {
    beforeIds = await waitForExactPreRollbackHistory(state, candidate);
    state.recovery = {
      action: "rollback_armed",
      at: new Date().toISOString(),
      armedAtEpochMs: Date.now(),
      candidateCreatedAtEpochMs: Date.parse(candidate.createdAt),
      priorDeploymentId: state.prior.id,
      candidateDeploymentId: candidate.id,
      beforeDeploymentIds: beforeIds,
      mutationAttempted: false,
      mutationAcknowledged: false,
      rollbackDeploymentId: null,
    };
    await atomicWriteJson(state.stateFile, state);
    await waitForExactPreRollbackHistory(state, candidate, 30_000);
  } else if (state.recovery.mutationAttempted !== false) {
    const resumeDeadline = boundedDeadline(60_000);
    let newRows = [];
    while (Date.now() < resumeDeadline) {
      let rows;
      try {
        const attemptTimeoutMs = railwayReadTimeout(resumeDeadline);
        if (attemptTimeoutMs == null) break;
        rows = await listDeployments(state.target, attemptTimeoutMs);
      } catch {
        await sleepBefore(resumeDeadline, 5_000);
        continue;
      }
      validatedHistoryIds(rows);
      newRows = rows.filter((row) => !beforeIds.includes(row.id));
      if (newRows.length > 1) fail("multiple deployments appeared after rollback was armed");
      if (newRows.length === 1) {
        const createdAt = Date.parse(newRows[0]?.createdAt || "");
        if (
          !Number.isFinite(createdAt)
          || createdAt < Math.max(
            Number(state.recovery.candidateCreatedAtEpochMs || 0),
            Number(state.recovery.armedAtEpochMs || 0) - 5_000,
          )
        ) {
          fail("deployment delta predates the armed rollback request");
        }
        if (
          UUID_PATTERN.test(state.recovery?.rollbackDeploymentId || "")
          && state.recovery.rollbackDeploymentId !== newRows[0].id
        ) {
          fail("armed rollback id does not match the unique deployment delta");
        }
      }
      if (newRows.length === 1 || state.recovery?.mutationAcknowledged === true) break;
      await sleepBefore(resumeDeadline, 5_000);
    }
    if (
      state.recovery?.mutationAcknowledged !== true
      && !UUID_PATTERN.test(state.recovery?.rollbackDeploymentId || "")
      && newRows.length === 0
    ) {
      fail("rollback mutation outcome remains unknown; manual recovery is required");
    }
    if (newRows.length === 1 && !state.recovery.rollbackDeploymentId) {
      state.recovery.rollbackDeploymentId = newRows[0].id;
      await atomicWriteJson(state.stateFile, state);
    }
    console.error("Resuming an already armed Railway rollback without issuing it twice.");
  }

  if (!rollbackWasArmed || state.recovery.mutationAttempted === false) {
    await waitForExactPreRollbackHistory(state, candidate, 30_000);
    state.recovery.mutationAttempted = true;
    await atomicWriteJson(state.stateFile, state);
    try {
      const rollbackData = await railwayApi(
        "mutation RollbackRelease($id: String!) { deploymentRollback(id: $id) }",
        { id: state.prior.id },
      );
      state.recovery.mutationAcknowledged = rollbackData?.deploymentRollback === true;
    } catch (error) {
      state.recovery.mutationError = String(error.message || error);
      state.recovery.mutationAcknowledged = false;
    }
    await atomicWriteJson(state.stateFile, state);
    console.error(`Railway rollback to ${state.prior.id} was requested; binding its result.`);
  }

  const restored = await discoverRollbackDeployment(
    state,
    beforeIds,
    candidate.id,
    300_000,
  );
  const activeDeadline = boundedDeadline(90_000);
  let active = [];
  while (Date.now() < activeDeadline) {
    try {
      const attemptTimeoutMs = railwayReadTimeout(activeDeadline);
      if (attemptTimeoutMs == null) break;
      active = await getValidatedActiveDeployments(state, attemptTimeoutMs);
    } catch {
      await sleepBefore(activeDeadline, 5_000);
      continue;
    }
    const unexpected = active.filter(
      (deployment) => ![state.prior.id, candidate.id, restored.id].includes(deployment.id),
    );
    if (unexpected.length > 0) {
      fail("an unexpected deployment became active during rollback cutover");
    }
    if (active.length === 1 && active[0].id === restored.id) break;
    await sleepBefore(activeDeadline, 5_000);
  }
  if (active.length !== 1 || active[0].id !== restored.id) {
    fail("exact rollback deployment did not become the sole active production deployment");
  }
  requireDeploymentTarget(active[0], state.target, "active rollback deployment");
  await verifyHealthRestored(state, 120_000);
  await verifySoleActiveDeployment(state, restored.id, state.prior.imageDigest);
  await waitForCandidateStopped(state, candidate.id, 90_000);
  await waitForExactPostRollbackHistory(state, beforeIds, restored.id, 30_000);
  await verifySoleActiveDeployment(state, restored.id, state.prior.imageDigest);
  console.error(`Verified rollback deployment ${restored.id} and prior public health.`);
  return { action: "rolled_back", deploymentId: restored.id };
}

async function endCandidate(state, candidate) {
  state.recovery = {
    action: "candidate_end_armed",
    at: new Date().toISOString(),
    candidateDeploymentId: candidate.id,
  };
  await atomicWriteJson(state.stateFile, state);
  await endExactDeployment({
    deploymentId: candidate.id,
    lastStatus: candidate.status,
    pollMs: 5_000,
  });
  return { action: "candidate_ended", deploymentId: candidate.id };
}

function recordedPriorIsSoleActive(state, active) {
  return (
    active.length === 1
    && active[0].id === state.prior.id
    && String(active[0].status || "").toUpperCase() === "SUCCESS"
    && active[0].deploymentStopped !== true
    && active[0]?.meta?.imageDigest === state.prior.imageDigest
    && deploymentHasRunningInstance(active[0])
  );
}

async function confirmCandidateEndedWithPrior(state, candidate) {
  const exact = validateCandidateIdentity(state, candidate);
  if (!deploymentIsStopped(exact)) {
    fail("production candidate is not positively stopped");
  }
  await waitForExactPreRollbackHistory(state, exact, 30_000);
  await verifyHealthRestored(state, 120_000);
  await verifySoleActiveDeployment(state, state.prior.id, state.prior.imageDigest);
  return { action: "candidate_ended", deploymentId: exact.id };
}

async function waitForRemovingProductionCandidate(state, candidate, timeoutMs = 90_000) {
  const deadline = boundedDeadline(timeoutMs);
  let observed = candidate;
  while (Date.now() < deadline) {
    let inspected;
    try {
      const attemptTimeoutMs = railwayReadTimeout(deadline);
      if (attemptTimeoutMs == null) break;
      inspected = await getDeployment(candidate.id, attemptTimeoutMs);
    } catch {
      await sleepBefore(deadline, 5_000);
      continue;
    }
    observed = validateCandidateIdentity(state, inspected);
    if (deploymentIsStopped(observed)) {
      return confirmCandidateEndedWithPrior(state, observed);
    }
    const status = String(observed.status || "").toUpperCase();
    let active;
    try {
      const attemptTimeoutMs = railwayReadTimeout(deadline);
      if (attemptTimeoutMs == null) break;
      active = await getValidatedActiveDeployments(state, attemptTimeoutMs);
    } catch {
      await sleepBefore(deadline, 5_000);
      continue;
    }
    const candidateIsActive = active.some((deployment) => deployment.id === candidate.id);
    if (
      status === "SUCCESS"
      || status === "DEPLOYING"
      || status === "SLEEPING"
      || candidateIsActive
      || !recordedPriorIsSoleActive(state, active)
    ) {
      return rollbackProduction(state, observed);
    }
    if (status !== "REMOVING" && !PRE_RUNTIME_STATUSES.has(status)) {
      fail(`production candidate entered unknown removal status ${status || "missing"}`);
    }
    await sleepBefore(deadline, 5_000);
  }
  fail("production candidate remained REMOVING without reaching a stopped state");
}

async function cancelInactiveProductionCandidate(state, candidate) {
  const freshCandidate = validateCandidateIdentity(
    state,
    await retryRecoveryRead(
      (attemptTimeoutMs) => getDeployment(candidate.id, attemptTimeoutMs),
      "exact candidate inspection before cancellation",
    ),
  );
  const hasArmedCancellation = state?.recovery?.action === "candidate_cancel_armed";
  if (
    hasArmedCancellation
    && (
      state.recovery.candidateDeploymentId !== candidate.id
      || (
        state.recovery.mutationAttempted != null
        && typeof state.recovery.mutationAttempted !== "boolean"
      )
      || (
        state.recovery.mutationAcknowledged != null
        && typeof state.recovery.mutationAcknowledged !== "boolean"
      )
    )
  ) {
    fail("armed candidate cancellation state is invalid");
  }
  const status = String(freshCandidate.status || "").toUpperCase();
  const active = await retryRecoveryRead(
    (attemptTimeoutMs) => getValidatedActiveDeployments(state, attemptTimeoutMs),
    "active deployment inspection before cancellation",
  );
  const candidateIsActive = active.some((deployment) => deployment.id === candidate.id);
  const priorIsSoleActive = recordedPriorIsSoleActive(state, active);
  if (
    status === "SUCCESS"
    || candidateIsActive
    || !priorIsSoleActive
  ) {
    return rollbackProduction(state, freshCandidate);
  }
  if (status === "DEPLOYING" || status === "SLEEPING") {
    return rollbackProduction(state, freshCandidate);
  }
  if (status === "REMOVING") {
    return waitForRemovingProductionCandidate(state, freshCandidate);
  }
  if (!PRE_RUNTIME_STATUSES.has(status)) {
    fail("production candidate reached a non-cancellable state; recovery must be rerun");
  }

  const cancellationAlreadyArmed = hasArmedCancellation;
  if (!cancellationAlreadyArmed) {
    state.recovery = {
      action: "candidate_cancel_armed",
      at: new Date().toISOString(),
      candidateDeploymentId: candidate.id,
      mutationAttempted: false,
      mutationAcknowledged: false,
    };
    await atomicWriteJson(state.stateFile, state);
  }
  if (!cancellationAlreadyArmed || state.recovery.mutationAttempted === false) {
    await waitForExactPreRollbackHistory(state, freshCandidate, 30_000);
    state.recovery.mutationAttempted = true;
    await atomicWriteJson(state.stateFile, state);
    state.recovery.mutationAcknowledged = await requestExactDeploymentEnd(
      candidate.id,
      "deploymentCancel",
    );
    await atomicWriteJson(state.stateFile, state);
  } else {
    console.error("Resuming an armed candidate cancellation without issuing it twice.");
  }

  const deadline = boundedDeadline(90_000);
  while (Date.now() < deadline) {
    let observed;
    try {
      const attemptTimeoutMs = railwayReadTimeout(deadline);
      if (attemptTimeoutMs == null) break;
      observed = await getDeployment(candidate.id, attemptTimeoutMs);
    } catch {
      await sleepBefore(deadline, 5_000);
      continue;
    }
    observed = validateCandidateIdentity(state, observed);
    if (deploymentIsStopped(observed)) {
      return confirmCandidateEndedWithPrior(state, observed);
    }
    const observedStatus = String(observed?.status || "").toUpperCase();
    let currentActive;
    try {
      const attemptTimeoutMs = railwayReadTimeout(deadline);
      if (attemptTimeoutMs == null) break;
      currentActive = await getValidatedActiveDeployments(state, attemptTimeoutMs);
    } catch {
      await sleepBefore(deadline, 5_000);
      continue;
    }
    if (
      observedStatus === "SUCCESS"
      || observedStatus === "DEPLOYING"
      || observedStatus === "SLEEPING"
      || currentActive.some((deployment) => deployment.id === candidate.id)
      || !recordedPriorIsSoleActive(state, currentActive)
    ) {
      return rollbackProduction(state, observed);
    }
    if (observedStatus === "REMOVING") {
      return waitForRemovingProductionCandidate(state, observed, 90_000);
    }
    if (!PRE_RUNTIME_STATUSES.has(observedStatus)) {
      fail(`production candidate entered unknown cancellation status ${observedStatus || "missing"}`);
    }
    await sleepBefore(deadline, 5_000);
  }
  fail("candidate cancellation outcome remains unresolved; manual recovery is required");
}

async function recover() {
  const options = parseArguments(process.argv.slice(2));
  const stateFile = options.stateFile;
  try {
    await access(stateFile);
  } catch {
    console.error("No Railway release state exists; no deployment recovery is needed.");
    return;
  }
  const state = await readJson(stateFile);
  state.stateFile = stateFile;
  validateState(state, options, { requireUnexpired: false });
  if (state.accepted === true && state.phase === "accepted") {
    console.error("Exact Railway release was accepted; no recovery is needed.");
    return;
  }
  if (state.mode === "production") {
    await failLockBridgeVariable(state);
  }
  if ([
    "candidate_ended",
    "candidate_already_ended",
    "rolled_back",
    "prior_already_active",
  ]
    .includes(state?.recovery?.action)) {
    console.error("Railway release recovery was already completed.");
    return;
  }

  const recoveryDomains = await retryRecoveryRead(
    (attemptTimeoutMs) => verifyTargetBaseUrl(
      state.target,
      state.baseUrl,
      attemptTimeoutMs,
    ),
    "target-domain verification",
  );
  if (state.legacyTransactionBridge) {
    validateLegacyBridgeState(state.legacyTransactionBridge, {
      target: state.target,
      targetDomains: recoveryDomains,
      prior: state.prior,
      expectedCommit: options.expectedCommit || productionMessageCommit(state.message),
      expectedDigest: options.expectedDigest,
      requireUnexpired: false,
    });
  }
  const candidateId = await resolveCandidate(state);
  if (!candidateId) {
    if (state.mode === "production") {
      const priorActive = await exactPriorIsActive(state);
      if (priorActive) {
        await verifyHealthRestored(state, 120_000);
        await verifySoleActiveDeployment(state, state.prior.id, state.prior.imageDigest);
        state.recovery = { action: "candidate_unresolved", at: new Date().toISOString() };
        await atomicWriteJson(stateFile, state);
        fail(
          "no candidate is visible yet; prior production is healthy but recovery remains armed",
        );
      }
    }
    state.recovery = { action: "candidate_unresolved", at: new Date().toISOString() };
    await atomicWriteJson(stateFile, state);
    fail("no exact Railway candidate was visible after the bounded recovery window");
  }
  await atomicWriteJson(stateFile, state);

  const candidate = validateCandidateIdentity(
    state,
    await retryRecoveryRead(
      (attemptTimeoutMs) => getDeployment(candidateId, attemptTimeoutMs),
      "initial exact candidate inspection",
    ),
  );
  state.candidate.status = String(candidate.status || "").toUpperCase();
  state.candidate.imageDigest = candidate?.meta?.imageDigest || state.candidate.imageDigest;
  await atomicWriteJson(stateFile, state);

  let result;
  if (state.mode === "production") {
    if (state?.recovery?.action === "rollback_armed") {
      result = await rollbackProduction(state, candidate);
    } else {
      const priorActive = await exactPriorIsActive(state);
      if (state.candidate.status === "SUCCESS") {
        result = await rollbackProduction(state, candidate);
      } else if (priorActive && candidate.id !== priorActive.id) {
        if (deploymentIsStopped(candidate)) {
          await confirmCandidateEndedWithPrior(state, candidate);
          result = { action: "candidate_already_ended", deploymentId: candidate.id };
        } else {
          result = await cancelInactiveProductionCandidate(state, candidate);
        }
      } else {
        result = await rollbackProduction(state, candidate);
      }
    }
  } else if (deploymentIsStopped(candidate)) {
    console.error(`Exact candidate ${candidateId} is already stopped.`);
    result = { action: "candidate_already_ended", deploymentId: candidate.id };
  } else if (
    NONTERMINAL_STATUSES.has(state.candidate.status)
    || state.candidate.status === "SUCCESS"
  ) {
    result = await endCandidate(state, candidate);
  } else {
    fail(`exact Railway candidate has unknown status ${candidate.status}`);
  }

  if (state.mode === "production") {
    await verifyBridgeRecoveryMode(state);
  }
  state.recovery = { ...result, at: new Date().toISOString() };
  state.phase = "recovered";
  delete state.stateFile;
  await atomicWriteJson(stateFile, state);
}

try {
  await recover();
} catch (error) {
  console.error(`CRITICAL: Railway release recovery failed: ${error.message}`);
  process.exitCode = 1;
}
