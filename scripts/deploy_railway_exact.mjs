#!/usr/bin/env node

import {
  FAILED_STATUSES,
  NONTERMINAL_STATUSES,
  UUID_PATTERN,
  atomicWriteJson,
  deploymentHasRunningInstance,
  deploymentIsStopped,
  deploymentOccupiesActiveSet,
  endExactDeployment,
  expectedLegacyBridgePhase,
  fail,
  fetchHealthSnapshot,
  getActiveDeployments,
  getDeployment,
  listDeployments,
  loadLegacyDrainAttestation,
  railwayApi,
  readJson,
  recentMessageMatches,
  readExactServiceVariable,
  requireExecutableLegacyBridgePhase,
  requireReleaseHistoryHeadroom,
  requireDeploymentTarget,
  requireRailwayCliVersion,
  resolveCanonicalTarget,
  runRailway,
  safeDeploymentSnapshot,
  setExactServiceVariable,
  sleep,
  targetArgs,
  validateProductionBackupEvidence,
  validateLegacyBridgeReadiness,
  validateLegacyBridgeState,
  verifyNoRepositoryDeployTriggers,
  verifyRailwayMutationContracts,
  verifyTargetBaseUrl,
} from "./railway_release_control.mjs";

function positiveInteger(value, label) {
  if (!/^\d+$/.test(value || "") || Number(value) <= 0) {
    fail(`${label} must be a positive integer`);
  }
  return Number(value);
}

function parseArguments(argv) {
  const values = new Map();
  const allowed = new Set([
    "project",
    "environment",
    "service",
    "message",
    "mode",
    "base-url",
    "state-file",
    "volume-instance",
    "timeout-seconds",
    "poll-ms",
    "discovery-grace-seconds",
    "upload-timeout-seconds",
    "forbidden-project",
    "forbidden-environment",
    "forbidden-service",
    "forbidden-base-url",
    "drain-attestation-file",
    "drain-attestation-sha256",
    "expected-commit",
  ]);
  for (let index = 0; index < argv.length; index += 2) {
    const flag = argv[index];
    const value = argv[index + 1];
    if (!flag?.startsWith("--") || value === undefined) {
      fail("arguments must be supplied as --name value pairs");
    }
    const name = flag.slice(2);
    if (!allowed.has(name) || values.has(name)) fail(`unexpected argument ${flag}`);
    values.set(name, value);
  }
  for (const name of [
    "project",
    "environment",
    "service",
    "message",
    "mode",
    "base-url",
    "state-file",
  ]) {
    if (!values.get(name)?.trim()) fail(`--${name} is required`);
  }
  const mode = values.get("mode");
  if (!["staging", "production"].includes(mode)) {
    fail("--mode must be staging or production");
  }
  if (mode === "production" && !values.get("volume-instance")?.trim()) {
    fail("--volume-instance is required for production backup verification");
  }
  if (mode === "production" && !/^[0-9a-f]{40}$/.test(values.get("expected-commit") || "")) {
    fail("--expected-commit is required for production and must be a full lowercase SHA");
  }
  if (
    mode === "staging"
    && ["drain-attestation-file", "drain-attestation-sha256", "expected-commit"]
      .some((name) => values.has(name))
  ) {
    fail("legacy drain attestation arguments are production-only");
  }
  const forbiddenNames = [
    "forbidden-project",
    "forbidden-environment",
    "forbidden-service",
    "forbidden-base-url",
  ];
  const suppliedForbidden = forbiddenNames.filter((name) => values.get(name)?.trim());
  if (mode === "staging" && suppliedForbidden.length !== forbiddenNames.length) {
    fail("staging requires a complete forbidden production target and URL");
  }
  if (mode === "production" && suppliedForbidden.length > 0) {
    fail("forbidden production target arguments are staging-only");
  }
  if (
    mode === "staging"
    && !["forbidden-project", "forbidden-environment", "forbidden-service"]
      .every((name) => UUID_PATTERN.test(values.get(name) || ""))
  ) {
    fail("forbidden production target must use canonical Railway UUIDs");
  }
  const message = values.get("message");
  if (message.length > 150 || /[\r\n]/.test(message)) {
    fail("--message must be a single line no longer than 150 characters");
  }
  const baseUrl = new URL(values.get("base-url"));
  if (
    baseUrl.protocol !== "https:"
    || baseUrl.username
    || baseUrl.password
    || (baseUrl.port && baseUrl.port !== "443")
    || baseUrl.pathname !== "/"
    || baseUrl.search
    || baseUrl.hash
  ) {
    fail("--base-url must be a credential-free HTTPS origin");
  }
  let forbiddenBaseUrl = null;
  if (mode === "staging") {
    forbiddenBaseUrl = new URL(values.get("forbidden-base-url"));
    if (
      forbiddenBaseUrl.protocol !== "https:"
      || forbiddenBaseUrl.username
      || forbiddenBaseUrl.password
      || (forbiddenBaseUrl.port && forbiddenBaseUrl.port !== "443")
      || forbiddenBaseUrl.pathname !== "/"
      || forbiddenBaseUrl.search
      || forbiddenBaseUrl.hash
      || forbiddenBaseUrl.origin === baseUrl.origin
    ) {
      fail("staging and production must have distinct credential-free HTTPS origins");
    }
  }
  return {
    target: {
      project: values.get("project"),
      environment: values.get("environment"),
      service: values.get("service"),
    },
    message,
    mode,
    baseUrl: baseUrl.origin,
    stateFile: values.get("state-file"),
    volumeInstance: values.get("volume-instance") || null,
    forbiddenTarget: mode === "staging"
      ? {
          project: values.get("forbidden-project"),
          environment: values.get("forbidden-environment"),
          service: values.get("forbidden-service"),
        }
      : null,
    forbiddenBaseUrl: forbiddenBaseUrl?.origin || null,
    timeoutMs:
      positiveInteger(values.get("timeout-seconds") || "600", "--timeout-seconds") * 1000,
    pollMs: positiveInteger(values.get("poll-ms") || "5000", "--poll-ms"),
    discoveryGraceMs:
      positiveInteger(
        values.get("discovery-grace-seconds") || "45",
        "--discovery-grace-seconds",
      ) * 1000,
    uploadTimeoutMs:
      positiveInteger(
        values.get("upload-timeout-seconds") || "180",
        "--upload-timeout-seconds",
      ) * 1000,
    drainAttestationFile: values.get("drain-attestation-file") || null,
    drainAttestationSha256: values.get("drain-attestation-sha256") || null,
    expectedCommit: values.get("expected-commit") || null,
  };
}

function parseDeploymentId(output) {
  try {
    const payload = JSON.parse(output.trim());
    return UUID_PATTERN.test(payload?.deploymentId || "") ? payload.deploymentId : null;
  } catch {
    return null;
  }
}

async function verifyProductionBackup(volumeInstance, target) {
  const data = await railwayApi(
    "query ReleaseBackups($volumeInstanceId: String!) { volumeInstance(id: $volumeInstanceId) { id environmentId serviceId mountPath state isPendingDeletion } volumeInstanceBackupList(volumeInstanceId: $volumeInstanceId) { id createdAt expiresAt } volumeInstanceBackupScheduleList(volumeInstanceId: $volumeInstanceId) { id kind cron } }",
    { volumeInstanceId: volumeInstance },
  );
  return validateProductionBackupEvidence(data, volumeInstance, target);
}

async function prepareState(options) {
  const active = await getActiveDeployments(options.target);
  for (const deployment of active) {
    requireDeploymentTarget(deployment, options.target, "active prior deployment");
  }
  const activeContenders = active.filter(deploymentOccupiesActiveSet);
  if (options.mode === "production" && activeContenders.length !== 1) {
    fail("production must have exactly one unambiguous active deployment before upload");
  }
  if (activeContenders.length > 1) {
    fail("target has multiple or transitioning active deployments before upload");
  }
  const rows = requireReleaseHistoryHeadroom(await listDeployments(options.target));
  const rowIds = rows
    .map((row) => row?.id)
    .filter((id) => UUID_PATTERN.test(id || ""));
  if (new Set(rowIds).size !== rowIds.length) {
    fail("Railway deployment history contains duplicate deployment ids");
  }
  const inFlight = rows.filter((row) => NONTERMINAL_STATUSES.has(String(row.status).toUpperCase()));
  if (inFlight.length > 0) fail("target already has an in-flight Railway deployment");

  let prior = null;
  if (activeContenders.length === 1) {
    const activePrior = activeContenders[0];
    if (
      String(activePrior.status || "").toUpperCase() !== "SUCCESS"
      || activePrior.deploymentStopped === true
      || !deploymentHasRunningInstance(activePrior)
      || deploymentIsStopped(activePrior)
    ) {
      fail("active prior deployment is not a stable running success");
    }
    const snapshot = safeDeploymentSnapshot(activePrior);
    prior = {
      ...snapshot,
      health: await fetchHealthSnapshot(options.baseUrl, "/health"),
      readiness: await fetchHealthSnapshot(options.baseUrl, "/readyz"),
    };
    if (
      options.mode === "production"
      && (
        prior.canRollback !== true
        || !prior.imageDigest
        || !UUID_PATTERN.test(prior.snapshotId || "")
        || prior.health.status !== 200
        || prior.health.applicationStatus !== "healthy"
        || !prior.health.version
        || !(
          (
            prior.readiness.status === 200
            && prior.readiness.ready === true
            && prior.readiness.version === prior.health.version
            && (
              !prior.health.commitSha
              || prior.readiness.commitSha === prior.health.commitSha
            )
          )
          || (
            prior.health.version === "0.6.2"
            && prior.readiness.status === 404
          )
        )
      )
    ) {
      fail("production prior deployment is not rollback- and health-verifiable");
    }
    if (!rowIds.includes(prior.id)) {
      fail("active production prior is missing from Railway deployment history");
    }
  }
  const backup = options.mode === "production"
    ? await verifyProductionBackup(options.volumeInstance, options.target)
    : null;
  let legacyTransactionBridge = null;
  let bridgeVariable = null;
  if (options.mode === "production") {
    const phase = expectedLegacyBridgePhase(prior);
    if (phase) {
      requireExecutableLegacyBridgePhase(phase);
      legacyTransactionBridge = await loadLegacyDrainAttestation(
        options.drainAttestationFile,
        options.drainAttestationSha256,
        {
          phase,
          target: options.target,
          targetDomains: options.targetDomains,
          prior,
          expectedCommit: options.expectedCommit,
        },
      );
      if (phase === "bridge_unlock") {
        validateLegacyBridgeReadiness(prior.readiness, legacyTransactionBridge, {
          expectedLocked: true,
        });
      }
      bridgeVariable = {
        name: "LEGACY_TRANSACTION_BRIDGE_LOCK",
        priorValues: phase === "legacy_lock" ? [null, "false", "true"] : ["true"],
        desiredValue: phase === "legacy_lock" ? "true" : "false",
        changeArmed: false,
        verified: false,
        restored: false,
      };
    }
  }
  const finalMessage = options.mode === "production"
    ? `${options.message}:prev:${prior.id}`
    : options.message;
  if (finalMessage.length > 200) fail("final Railway CLI message exceeds 200 characters");
  return {
    schemaVersion: 1,
    phase: "armed",
    mode: options.mode,
    baseUrl: options.baseUrl,
    message: finalMessage,
    startedAt: new Date().toISOString(),
    startedAtEpochMs: Date.now(),
    target: options.target,
    beforeDeploymentIds: rowIds,
    prior,
    backup,
    legacyTransactionBridge,
    bridgeVariable,
    candidate: { id: null, status: null, imageDigest: null },
    accepted: false,
    recovery: null,
    error: null,
  };
}

function validateCandidate(deployment, state, earliestCreatedAt) {
  if (!deployment) fail("exact Railway candidate could not be inspected");
  requireDeploymentTarget(deployment, state.target, "exact Railway candidate");
  if (state.beforeDeploymentIds.includes(deployment.id)) {
    fail("Railway returned a deployment that existed before this release attempt");
  }
  const createdAt = Date.parse(deployment.createdAt || "");
  if (!Number.isFinite(createdAt) || createdAt < earliestCreatedAt) {
    fail("exact Railway candidate has an invalid or stale creation time");
  }
  if (deployment?.meta?.cliMessage !== state.message) {
    fail("exact Railway candidate has a different Railway CLI message");
  }
  return deployment;
}

function rememberObservedCandidate(state, deployment, fallbackStatus = null) {
  if (!deployment || deployment.id !== state.candidate.id) return;
  state.candidate.status = String(deployment.status || fallbackStatus || "").toUpperCase();
  state.candidate.imageDigest = deployment?.meta?.imageDigest || state.candidate.imageDigest;
  state.candidate.snapshotId = deployment?.snapshotId || state.candidate.snapshotId;
  state.phase = "candidate_bound";
}

async function main() {
  const options = parseArguments(process.argv.slice(2));
  await requireRailwayCliVersion("5.30.1");
  options.target = await resolveCanonicalTarget(options.target);
  options.targetDomains = await verifyTargetBaseUrl(options.target, options.baseUrl);
  if (options.mode === "staging") {
    if (
      options.target.project === options.forbiddenTarget.project
      && (
        options.target.environment === options.forbiddenTarget.environment
        || options.target.service === options.forbiddenTarget.service
      )
    ) {
      fail("staging must use both a distinct environment and a distinct service");
    }
  }
  await verifyNoRepositoryDeployTriggers(options.target);
  await verifyRailwayMutationContracts();
  const state = await prepareState(options);
  if (state.bridgeVariable) {
    const observedPrior = await readExactServiceVariable(
      options.target,
      state.bridgeVariable.name,
    );
    if (!state.bridgeVariable.priorValues.includes(observedPrior)) {
      fail("transaction bridge Railway variable does not match the required prior phase");
    }
    state.bridgeVariable.observedPriorValue = observedPrior;
    state.bridgeVariable.changeArmed = true;
    state.phase = "variable_change_armed";
    await atomicWriteJson(options.stateFile, state);
    await setExactServiceVariable(
      options.target,
      state.bridgeVariable.name,
      state.bridgeVariable.desiredValue,
    );
    state.bridgeVariable.verified = true;
    state.phase = "variable_change_verified";
    await atomicWriteJson(options.stateFile, state);
  }
  state.phase = "upload_armed";
  await atomicWriteJson(options.stateFile, state);
  const finalRows = await listDeployments(options.target);
  if (
    state.bridgeVariable
    && await readExactServiceVariable(options.target, state.bridgeVariable.name)
      !== state.bridgeVariable.desiredValue
  ) {
    fail("transaction bridge Railway variable changed immediately before upload");
  }
  const finalIds = finalRows
    .map((row) => row?.id)
    .filter((id) => UUID_PATTERN.test(id || ""));
  if (
    finalIds.length !== state.beforeDeploymentIds.length
    || finalIds.some((id) => !state.beforeDeploymentIds.includes(id))
    || finalRows.some((row) => NONTERMINAL_STATUSES.has(String(row?.status || "").toUpperCase()))
  ) {
    fail("Railway deployment history changed immediately before upload");
  }
  const finalActive = await getActiveDeployments(options.target);
  if (state.legacyTransactionBridge) {
    const liveDomains = await verifyTargetBaseUrl(options.target, options.baseUrl);
    validateLegacyBridgeState(state.legacyTransactionBridge, {
      target: state.target,
      targetDomains: liveDomains,
      prior: state.prior,
      expectedCommit: options.expectedCommit,
      expectedDigest: options.drainAttestationSha256,
      requireUnexpired: true,
    });
  }
  for (const deployment of finalActive) {
    requireDeploymentTarget(deployment, options.target, "final active deployment");
  }
  const finalContenders = finalActive.filter(deploymentOccupiesActiveSet);
  if (
    options.mode === "production"
    && (
      finalContenders.length !== 1
      || finalContenders[0]?.id !== state.prior?.id
      || String(finalContenders[0]?.status || "").toUpperCase() !== "SUCCESS"
      || finalContenders[0]?.deploymentStopped === true
      || !deploymentHasRunningInstance(finalContenders[0])
      || finalContenders[0]?.meta?.imageDigest !== state.prior?.imageDigest
    )
  ) {
    fail("production active deployment changed immediately before upload");
  }
  const earliestCreatedAt = state.startedAtEpochMs - 120_000;
  const deadline = state.startedAtEpochMs + options.timeoutMs;
  const discoveryDeadline = Math.min(
    deadline,
    state.startedAtEpochMs + options.discoveryGraceMs,
  );
  const upload = await runRailway(
    [
      "up",
      "--detach",
      "--json",
      "--message",
      state.message,
      ...targetArgs(options.target),
    ],
    options.uploadTimeoutMs,
  );
  let deploymentId = parseDeploymentId(upload.stdout);
  if (deploymentId && state.beforeDeploymentIds.includes(deploymentId)) {
    fail("Railway upload returned a deployment that predates this release attempt");
  }
  state.candidate.id = deploymentId;
  await atomicWriteJson(options.stateFile, state);
  if (deploymentId) {
    console.error(`Railway accepted deployment ${deploymentId}; waiting for exact terminal state.`);
  } else {
    const reason = upload.timedOut
      ? "upload response timed out"
      : upload.captureExceeded
        ? "upload response exceeded the safe capture limit"
        : upload.spawnError
          ? "Railway CLI could not start"
          : `Railway CLI returned code ${upload.code ?? "unknown"} without a deployment id`;
    console.error(`${reason}; resolving only the unique CI deployment message.`);
  }

  let lastStatus = null;
  let lastListProblem = "deployment has not appeared in Railway yet";
  while (Date.now() < deadline) {
    let queriedRows;
    try {
      queriedRows = await listDeployments(
        options.target,
        Math.min(30_000, Math.max(options.pollMs * 2, 5_000)),
      );
    } catch (error) {
      if (String(error?.message || "") !== "Railway deployment-list query failed") {
        throw error;
      }
      lastListProblem = "Railway deployment-list query failed transiently";
      await sleep(options.pollMs);
      continue;
    }
    const rows = queriedRows;

    if (!deploymentId) {
      const matches = recentMessageMatches(rows, state.message, earliestCreatedAt);
      const newMatches = matches.filter((row) => !state.beforeDeploymentIds.includes(row.id));
      if (newMatches.length > 1) {
        fail("multiple recent Railway deployments matched the unique CI message");
      }
      if (newMatches.length === 1) {
        deploymentId = newMatches[0].id;
        state.candidate.id = deploymentId;
        state.phase = "candidate_bound";
        await atomicWriteJson(options.stateFile, state);
        console.error(`Recovered exact Railway deployment ${deploymentId} from its CI message.`);
      } else if (Date.now() >= discoveryDeadline) {
        fail("Railway did not return or expose one exact deployment for this CI job");
      }
    }
    if (!deploymentId) {
      await sleep(options.pollMs);
      continue;
    }

    const row = rows.find((candidate) => candidate?.id === deploymentId);
    if (!row) {
      lastListProblem = `exact deployment ${deploymentId} is not visible yet`;
      await sleep(options.pollMs);
      continue;
    }
    const observedExact = await getDeployment(deploymentId);
    if (observedExact) {
      requireDeploymentTarget(observedExact, state.target, "observed Railway candidate");
      state.candidate.targetVerified = true;
    }
    rememberObservedCandidate(state, observedExact, row.status);
    await atomicWriteJson(options.stateFile, state);
    const exact = validateCandidate(observedExact, state, earliestCreatedAt);
    state.candidate.identityVerified = true;
    const status = String(exact.status || row.status || "").toUpperCase();
    state.phase = "candidate_bound";
    state.candidate.status = status;
    state.candidate.imageDigest = exact?.meta?.imageDigest || row?.meta?.imageDigest || null;
    state.candidate.snapshotId = exact?.snapshotId || null;
    await atomicWriteJson(options.stateFile, state);
    if (status !== lastStatus) {
      console.error(`Railway deployment ${deploymentId} status: ${status || "missing"}.`);
      lastStatus = status;
    }
    if (status === "SUCCESS") {
      const hasRunningInstance = (exact.instances || []).some(
        (instance) => String(instance?.status || "").toUpperCase() === "RUNNING",
      );
      const deployManifest = exact?.meta?.fileServiceManifest?.deploy;
      const hasExpectedReleaseManifest = (
        deployManifest?.healthcheckPath === "/readyz"
        && deployManifest?.healthcheckTimeout === 180
        && deployManifest?.restartPolicyType === "ON_FAILURE"
        && deployManifest?.restartPolicyMaxRetries === 3
        && (exact?.meta?.volumeMounts || []).includes("/data")
      );
      if (
        !hasRunningInstance
        || !state.candidate.imageDigest
        || !hasExpectedReleaseManifest
      ) {
        fail(
          `successful deployment ${deploymentId} lacks its running image or release manifest`,
        );
      }
      if (state.legacyTransactionBridge) {
        const liveDomains = await verifyTargetBaseUrl(options.target, options.baseUrl);
        validateLegacyBridgeState(state.legacyTransactionBridge, {
          target: state.target,
          targetDomains: liveDomains,
          prior: state.prior,
          expectedCommit: options.expectedCommit,
          expectedDigest: options.drainAttestationSha256,
          requireUnexpired: true,
        });
        const readiness = await fetchHealthSnapshot(options.baseUrl, "/readyz");
        if (readiness.commitSha !== options.expectedCommit) {
          fail("bridge candidate readiness does not prove the expected exact commit");
        }
        validateLegacyBridgeReadiness(readiness, state.legacyTransactionBridge);
        state.candidate.bridgeReadiness = readiness.legacyTransactionBridge;
      }
      state.phase = "candidate_success";
      await atomicWriteJson(options.stateFile, state);
      process.stdout.write(`${deploymentId}\n`);
      return;
    }
    if (FAILED_STATUSES.has(status)) {
      fail(`Railway deployment ${deploymentId} reached terminal status ${status}`);
    }
    if (!NONTERMINAL_STATUSES.has(status)) {
      fail(`Railway deployment ${deploymentId} reported unknown status ${status || "missing"}`);
    }
    await sleep(options.pollMs);
  }
  fail(`timed out waiting for the exact Railway deployment: ${lastListProblem}`);
}

try {
  await main();
} catch (error) {
  const options = (() => {
    try {
      return parseArguments(process.argv.slice(2));
    } catch {
      return null;
    }
  })();
  let cleanupError = null;
  if (options) {
    try {
      const state = await readJson(options.stateFile);
      state.error = String(error.message);
      await atomicWriteJson(options.stateFile, state);
      if (
        state.bridgeVariable?.changeArmed === true
        && state.bridgeVariable?.desiredValue === "false"
        && state.bridgeVariable?.restored !== true
      ) {
        await setExactServiceVariable(
          state.target,
          state.bridgeVariable.name,
          "true",
        );
        state.bridgeVariable.restored = true;
        state.bridgeVariable.verified = false;
        await atomicWriteJson(options.stateFile, state);
      }
      if (
        options.mode === "staging"
        &&
        state.candidate?.id
        && state.candidate?.targetVerified === true
        && state.candidate?.identityVerified === true
        && String(state.candidate.status || "").toUpperCase() !== "SUCCESS"
        && NONTERMINAL_STATUSES.has(String(state.candidate.status || "").toUpperCase())
      ) {
        state.recovery = { action: "candidate_end_armed", at: new Date().toISOString() };
        await atomicWriteJson(options.stateFile, state);
        await endExactDeployment({
          deploymentId: state.candidate.id,
          lastStatus: state.candidate.status,
          pollMs: options.pollMs,
        });
        state.recovery = { action: "candidate_ended", at: new Date().toISOString() };
        state.phase = "recovered";
        await atomicWriteJson(options.stateFile, state);
      }
    } catch (recoveryError) {
      if (recoveryError?.code !== "ENOENT") cleanupError = recoveryError;
    }
  }
  console.error(`Exact Railway deployment failed: ${error.message}`);
  if (cleanupError) {
    console.error(`CRITICAL: exact deployment cleanup failed: ${cleanupError.message}`);
  }
  process.exitCode = 1;
}
