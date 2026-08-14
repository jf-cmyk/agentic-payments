#!/usr/bin/env node

import {
  UUID_PATTERN,
  atomicWriteJson,
  deploymentHasRunningInstance,
  deploymentOccupiesActiveSet,
  expectedLegacyBridgePhase,
  fail,
  fetchHealthSnapshot,
  getActiveDeployments,
  getDeployment,
  listDeployments,
  readJson,
  readExactServiceVariable,
  requireDeploymentTarget,
  validateLegacyBridgeReadiness,
  validateLegacyBridgeState,
  verifyTargetBaseUrl,
} from "./railway_release_control.mjs";

function parseArguments(argv) {
  const values = new Map();
  for (let index = 0; index < argv.length; index += 2) {
    const flag = argv[index];
    const value = argv[index + 1];
    const name = flag?.slice(2);
    if (
      !flag?.startsWith("--")
      || value === undefined
      || !["state-file", "deployment-id", "expected-commit", "drain-attestation-sha256"]
        .includes(name)
      || values.has(name)
    ) fail("invalid acceptance arguments");
    values.set(name, value);
  }
  for (const required of ["state-file", "deployment-id", "expected-commit"]) {
    if (!values.get(required)) fail(`--${required} is required`);
  }
  if (!UUID_PATTERN.test(values.get("deployment-id"))) {
    fail("release acceptance deployment id is invalid");
  }
  if (!/^[0-9a-f]{40}$/.test(values.get("expected-commit"))) {
    fail("--expected-commit must be a full lowercase Git commit SHA");
  }
  return {
    stateFile: values.get("state-file"),
    deploymentId: values.get("deployment-id"),
    expectedCommit: values.get("expected-commit"),
    drainAttestationSha256: values.get("drain-attestation-sha256") || null,
  };
}

async function requireCandidateOnlyHistory(state, deploymentId, label) {
  const rows = await listDeployments(state.target);
  const expectedIds = new Set([...state.beforeDeploymentIds, deploymentId]);
  const actualIds = rows.map((row) => row.id);
  if (
    expectedIds.size !== state.beforeDeploymentIds.length + 1
    || actualIds.length !== expectedIds.size
    || actualIds.some((id) => !expectedIds.has(id))
  ) {
    fail(`Railway deployment history changed ${label}`);
  }
}

const options = parseArguments(process.argv.slice(2));
const state = await readJson(options.stateFile);
if (
  state?.schemaVersion !== 1
  || state?.phase !== "candidate_success"
  || state?.candidate?.id !== options.deploymentId
  || state?.candidate?.status !== "SUCCESS"
  || !state?.candidate?.imageDigest
  || !Array.isArray(state?.beforeDeploymentIds)
  || !state.beforeDeploymentIds.every((id) => UUID_PATTERN.test(id || ""))
  || new Set(state.beforeDeploymentIds).size !== state.beforeDeploymentIds.length
  || state.beforeDeploymentIds.includes(options.deploymentId)
) {
  fail("release state does not prove this exact deployment reached SUCCESS");
}
const expectedBridgePhase = state.mode === "production"
  ? expectedLegacyBridgePhase(state.prior)
  : null;
if (
  Boolean(state.legacyTransactionBridge) !== Boolean(expectedBridgePhase)
  || (state.legacyTransactionBridge && state.legacyTransactionBridge.phase !== expectedBridgePhase)
) {
  fail("release state is missing the transaction bridge required by its prior release");
}
if (state.legacyTransactionBridge) {
  validateLegacyBridgeState(state.legacyTransactionBridge, {
    target: state.target,
    prior: state.prior,
    expectedCommit: options.expectedCommit,
    expectedDigest: options.drainAttestationSha256,
    requireUnexpired: true,
  });
  if (
    !state.bridgeVariable
    || state.bridgeVariable.name !== "LEGACY_TRANSACTION_BRIDGE_LOCK"
    || !["true", "false"].includes(state.bridgeVariable.desiredValue)
    || state.bridgeVariable.verified !== true
    || state.bridgeVariable.restored !== false
    || await readExactServiceVariable(state.target, state.bridgeVariable.name)
      !== state.bridgeVariable.desiredValue
  ) {
    fail("release state does not prove the exact transaction bridge variable mode");
  }
}
const targetDomains = await verifyTargetBaseUrl(state.target, state.baseUrl);
if (state.legacyTransactionBridge) {
  validateLegacyBridgeState(state.legacyTransactionBridge, {
    target: state.target,
    targetDomains,
    prior: state.prior,
    expectedCommit: options.expectedCommit,
    expectedDigest: options.drainAttestationSha256,
    requireUnexpired: true,
  });
}
await requireCandidateOnlyHistory(state, options.deploymentId, "before acceptance readiness");

const exact = requireDeploymentTarget(
  await getDeployment(options.deploymentId),
  state.target,
  "candidate accepted for release",
);
if (
  String(exact?.status || "").toUpperCase() !== "SUCCESS"
  || exact?.deploymentStopped === true
  || exact?.meta?.imageDigest !== state.candidate.imageDigest
  || exact?.meta?.cliMessage !== state.message
  || !deploymentHasRunningInstance(exact)
) {
  fail("exact candidate is no longer the running successful deployment recorded by the gate");
}

const active = await getActiveDeployments(state.target);
for (const deployment of active) {
  requireDeploymentTarget(deployment, state.target, "active Railway deployment");
}
const activeContenders = active.filter(deploymentOccupiesActiveSet);
if (activeContenders.length !== 1 || activeContenders[0].id !== options.deploymentId) {
  fail("exact candidate is not the sole active Railway deployment");
}

const readiness = await fetchHealthSnapshot(state.baseUrl, "/readyz");
if (
  readiness.status !== 200
  || readiness.ready !== true
  || readiness.commitSha !== options.expectedCommit
) {
  fail("final hosted readiness does not prove the expected exact commit");
}
if (state.legacyTransactionBridge) {
  validateLegacyBridgeReadiness(readiness, state.legacyTransactionBridge);
}

const finalExact = requireDeploymentTarget(
  await getDeployment(options.deploymentId),
  state.target,
  "final candidate accepted for release",
);
const finalActive = await getActiveDeployments(state.target);
for (const deployment of finalActive) {
  requireDeploymentTarget(deployment, state.target, "final active Railway deployment");
}
const finalContenders = finalActive.filter(deploymentOccupiesActiveSet);
if (
  String(finalExact?.status || "").toUpperCase() !== "SUCCESS"
  || finalExact?.deploymentStopped === true
  || finalExact?.meta?.imageDigest !== state.candidate.imageDigest
  || !deploymentHasRunningInstance(finalExact)
  || finalContenders.length !== 1
  || finalContenders[0].id !== options.deploymentId
) {
  fail("candidate changed after final readiness and cannot be accepted");
}
await requireCandidateOnlyHistory(state, options.deploymentId, "after acceptance readiness");

state.accepted = true;
state.phase = "accepted";
state.acceptedAt = new Date().toISOString();
state.acceptedCommit = options.expectedCommit;
state.finalReadiness = readiness;
await atomicWriteJson(options.stateFile, state);
console.log(`Accepted exact Railway deployment ${options.deploymentId}.`);
