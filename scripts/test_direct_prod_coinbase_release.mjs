#!/usr/bin/env node

import assert from "node:assert/strict";
import { mkdtemp, readFile, rm, stat, symlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  DOMAINS,
  GITHUB_SOURCE,
  LEGACY,
  SHADOW_VARIABLES,
  TARGET,
  TARGET_LOCK_PATH,
  defaultRun,
  executeReleaseCommand,
  parseArguments,
  readState,
  summarizeReleaseState,
} from "./direct_prod_coinbase_release.mjs";
import {
  AUDIT_DOMAINS,
  runCoinbaseHotfixAudit,
} from "./audit_coinbase_hotfix.mjs";

const COMMIT = "b".repeat(40);
const TREE = "c".repeat(40);
const CANDIDATE_DIGEST = `sha256:${"a".repeat(64)}`;
const CANDIDATE_ID = "11111111-1111-4111-8111-111111111111";
const SHADOW_ID = "77777777-7777-4777-8777-777777777777";
const ENFORCE_ID = "22222222-2222-4222-8222-222222222222";
const RECOVERY_ID = "33333333-3333-4333-8333-333333333333";
const RETRY_ID = "88888888-8888-4888-8888-888888888888";
const AMBIGUOUS_ID = "99999999-9999-4999-8999-999999999999";
const VOLUME_ID = "44444444-4444-4444-8444-444444444444";
const BACKUP_ID = "55555555-5555-4555-8555-555555555555";
const SNAPSHOT_ID = "66666666-6666-4666-8666-666666666666";
const TRIGGER_ID = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee";
const NOW = Date.parse("2026-08-21T18:00:00.000Z");
const GITHUB_RUN_ID = 123456789;
const GITHUB_RULESET_ID = 987654;

function result(stdout = "", code = 0) {
  return { code, stdout, stderr: "", exceeded: false, timedOut: false, spawnError: null };
}

function json(value) {
  return result(`${JSON.stringify(value)}\n`);
}

function deployment(id, digest, status = "SUCCESS") {
  return {
    id,
    projectId: TARGET.project,
    environmentId: TARGET.environment,
    serviceId: TARGET.service,
    snapshotId: id === LEGACY.deploymentId ? LEGACY.snapshotId : SNAPSHOT_ID,
    status,
    deploymentStopped: false,
    canRollback: true,
    createdAt: new Date(NOW - 60_000).toISOString(),
    meta: { imageDigest: digest, cliMessage: null },
    instances: status === "SUCCESS" ? [{ id: `instance-${id}`, status: "RUNNING" }] : [],
  };
}

function variablesFrom(argv) {
  const values = {};
  for (let index = 0; index < argv.length; index += 1) {
    if (argv[index] === "--raw-var") {
      const [name, ...rest] = argv[index + 1].split("=");
      values[name] = rest.join("=");
    }
  }
  return values;
}

function jsonVariablesFrom(argv) {
  const index = argv.indexOf("--variables");
  return index >= 0 ? JSON.parse(argv[index + 1]) : {};
}

function isSourceConnect(argv) {
  return argv[1] === "environment" && argv[2] === "edit";
}

function fakeRailway(overrides = {}) {
  const state = {
    active: [deployment(LEGACY.deploymentId, LEGACY.imageDigest)],
    history: [{
      id: LEGACY.deploymentId,
      status: "SUCCESS",
      createdAt: new Date(NOW - 60_000).toISOString(),
      meta: { imageDigest: LEGACY.imageDigest },
    }],
    variables: {
      X402_SOLANA_WALLET_ADDRESS: "11111111111111111111111111111111",
      X402_EVM_WALLET_ADDRESS: `0x${"1".repeat(40)}`,
    },
    commands: [],
    redeployQueue: [SHADOW_ID, ENFORCE_ID, RECOVERY_ID, RETRY_ID],
    sourceRepo: null,
    trigger: false,
    checkSuites: false,
    ...overrides,
  };
  const applyDeploymentOverrides = (row) => {
    if (!row) return row;
    let resultRow = state.nonRollbackDeploymentId === row.id
      ? { ...row, canRollback: false }
      : row;
    if (state.targetDriftDeploymentId === row.id) {
      resultRow = { ...resultRow, serviceId: "wrong-service" };
    }
    return resultRow;
  };
  const revealPendingHistory = () => {
    if (!state.pendingHistoryRows?.length) return;
    if ((state.historyVisibilityPollsRemaining || 0) > 0) {
      state.historyVisibilityPollsRemaining -= 1;
      return;
    }
    state.history.unshift(...state.pendingHistoryRows);
    state.pendingHistoryRows = null;
    if (state.pendingSourceActive) {
      for (const prior of state.active) {
        const historical = state.history.find((item) => item.id === prior.id);
        if (historical) historical.status = "REMOVED";
      }
      state.active = [state.pendingSourceActive];
      state.pendingSourceActive = null;
    }
  };
  const queueKnownOverlap = (next, priorActive) => {
    if (!(state.overlapRemaining > 0)) return;
    state.overlapRemaining -= 1;
    const retiring = priorActive.map((row) => ({
      ...row,
      status: "REMOVING",
      instances: [],
    }));
    state.activePollQueue = [...(state.activePollQueue || []), [next, ...retiring]];
  };

  async function run(argv, options = {}) {
    state.commands.push({ argv: [...argv], options: { ...options } });
    if (state.onCommand) await state.onCommand(argv, options, state);
    if (argv[0] === "git" && argv[1] === "rev-parse") {
      return result(`${String(argv[2]).endsWith("^{tree}") ? TREE : COMMIT}\n`);
    }
    if (argv[0] === "git" && argv[1] === "ls-remote") {
      return result(`${state.remoteCommit || COMMIT}\trefs/heads/${GITHUB_SOURCE.branch}\n`);
    }
    if (argv[0] === "git" && argv[1] === "status") return result(state.gitStatus || "");
    if (argv[0] === "git" && argv[1] === "ls-files") {
      return result((state.ignoredFiles || []).join("\0"));
    }
    if (argv[0] === "git" && argv[1] === "archive") return result();
    if (argv[0] === "tar") return result();
    if (argv[0] === "gh" && argv[1] === "api") {
      const endpoint = argv.find((value) => String(value).startsWith("repos/"));
      if (endpoint?.includes("/actions/workflows/")) {
        return json({ workflow_runs: state.githubWorkflowFailure ? [{
          id: GITHUB_RUN_ID,
          head_sha: COMMIT,
          head_branch: GITHUB_SOURCE.branch,
          event: "push",
          status: "completed",
          conclusion: "failure",
        }] : [{
          id: GITHUB_RUN_ID,
          head_sha: COMMIT,
          head_branch: GITHUB_SOURCE.branch,
          event: "push",
          status: "completed",
          conclusion: "success",
        }] });
      }
      if (endpoint?.includes("/rules/branches/")) {
        return json(state.githubMutableBranch ? [] : [
          { type: "update", ruleset_id: GITHUB_RULESET_ID },
          { type: "deletion", ruleset_id: GITHUB_RULESET_ID },
          { type: "non_fast_forward", ruleset_id: GITHUB_RULESET_ID },
        ]);
      }
      if (endpoint?.endsWith(`/rulesets/${GITHUB_RULESET_ID}`)) {
        return json({
          id: GITHUB_RULESET_ID,
          target: "branch",
          enforcement: "active",
          bypass_actors: state.githubRulesetBypass ? [{ actor_type: "RepositoryRole" }] : [],
          conditions: {
            ref_name: {
              include: [`refs/heads/${GITHUB_SOURCE.branch}`],
              exclude: [],
            },
          },
          rules: [
            { type: "update" },
            { type: "deletion" },
            { type: "non_fast_forward" },
          ],
        });
      }
    }
    if (argv[0] === process.execPath && argv[1]?.endsWith("audit_coinbase_hotfix.mjs")) {
      const mode = argv[argv.indexOf("--mode") + 1];
      return json({ passed: true, mode });
    }
    assert.equal(argv[0], "railway", `unexpected executable ${argv[0]}`);
    if (argv[1] === "status") {
      return json({
        id: TARGET.project,
        environments: {
          edges: [{ node: {
            id: TARGET.environment,
            serviceInstances: { edges: [{ node: {
              environmentId: TARGET.environment,
              serviceId: TARGET.service,
            } }] },
          } }],
        },
      });
    }
    if (argv[1] === "deployment" && argv[2] === "list") {
      revealPendingHistory();
      return json(state.history);
    }
    if (argv[1] === "variable" && argv[2] === "list") return json(state.variables);
    if (argv[1] === "variable" && argv[2] === "set") {
      assert(argv.includes("--skip-deploys"), "variable mutation omitted --skip-deploys");
      const name = argv[3];
      state.variables[name] = String(options.stdin || "").trim();
      return json({ ok: true });
    }
    if (argv[1] === "service" && argv[2] === "source" && argv[3] === "disconnect") {
      assert.fail("controller used the service-global source disconnect");
    }
    if (isSourceConnect(argv)) {
      assert.equal(argv[argv.indexOf("--project") + 1], TARGET.project);
      assert.equal(argv[argv.indexOf("--environment") + 1], TARGET.environment);
      assert.deepEqual(JSON.parse(String(options.stdin || "")), {
        services: { [TARGET.service]: { source: {
          repo: GITHUB_SOURCE.repository,
          branch: GITHUB_SOURCE.branch,
          commitSha: COMMIT,
          checkSuites: true,
        } } },
      });
      assert(!argv.includes("--verbose"), "source connection enabled unsafe verbose output");
      if (state.uploadFailure && !state.uploadFailureAfterAccepted) {
        return {
          ...result(state.uploadFailure.stdout || "", 1),
          stderr: state.uploadFailure.stderr || "",
        };
      }
      const priorActive = [...state.active];
      const priorHistory = state.history.map((row) => ({
        ...row,
        meta: { ...(row.meta || {}) },
      }));
      const deploymentStatus = state.nextUploadStatus || "SUCCESS";
      const unhealthySuccess = state.nextUploadUnhealthySuccess === true;
      const uploadDigest = state.nextUploadNoDigest === true ? "" : CANDIDATE_DIGEST;
      const becomesActive = (deploymentStatus === "SUCCESS" && !unhealthySuccess)
        || state.keepUploadTerminalActive
        || state.keepUploadUnhealthyActive;
      state.nextUploadStatus = null;
      state.nextUploadUnhealthySuccess = false;
      state.nextUploadNoDigest = false;
      if (becomesActive) {
        for (const prior of priorActive) {
          const historical = state.history.find((item) => item.id === prior.id);
          if (historical) historical.status = "REMOVED";
        }
        const next = deployment(CANDIDATE_ID, uploadDigest, deploymentStatus);
        Object.assign(next.meta, {
          repo: GITHUB_SOURCE.repository,
          branch: GITHUB_SOURCE.branch,
          commitHash: state.sourceDeploymentCommit || COMMIT,
        });
        if (unhealthySuccess) next.instances = [];
        if (deploymentStatus === "SUCCESS" && !unhealthySuccess) {
          queueKnownOverlap(next, priorActive);
        }
        state.active = [next];
      }
      state.history.unshift({
        id: CANDIDATE_ID,
        status: deploymentStatus,
        createdAt: new Date(NOW).toISOString(),
        meta: {
          imageDigest: uploadDigest || null,
          repo: GITHUB_SOURCE.repository,
          branch: GITHUB_SOURCE.branch,
          commitHash: state.sourceDeploymentCommit || COMMIT,
        },
        unhealthySuccess,
      });
      if (state.uploadHistoryVisibilityPolls > 0) {
        state.pendingHistoryRows = [state.history[0]];
        state.pendingSourceActive = becomesActive ? state.active[0] : null;
        state.history = priorHistory;
        state.active = priorActive;
        state.historyVisibilityPollsRemaining = state.uploadHistoryVisibilityPolls;
      }
      state.sourceRepo = GITHUB_SOURCE.repository;
      state.trigger = state.sourceConnectCreatesTrigger === true;
      state.checkSuites = state.trigger;
      if (state.crashAfterAccepted === "upload") {
        state.crashAfterAccepted = null;
        throw new Error("simulated crash after accepted upload");
      }
      if (state.uploadFailureAfterAccepted) {
        return {
          ...result(state.uploadFailure?.stdout || "", 1),
          stderr: state.uploadFailure?.stderr || "",
        };
      }
      return json({
        staged: true,
        committed: true,
        environmentId: TARGET.environment,
        environmentName: "production",
      });
    }
    if (argv[1] === "api") {
      const query = argv[2];
      const vars = variablesFrom(argv);
      if (query.includes("DirectProdStagedPatch")) {
        return json({ data: { environmentStagedChanges: {
          id: state.stagedPatchId || "<empty>",
          environmentId: TARGET.environment,
          status: "STAGED",
          appliedAt: null,
          patch: state.stagedPatch || {},
        } } });
      }
      if (query.includes("DirectProdStageSourceDisconnect")
        || query.includes("DirectProdCommitSourceDisconnect")) {
        assert.fail("controller used the shared production staged-patch channel");
      }
      if (query.includes("DirectProdCreateTrigger")) {
        assert.match(query,
          /deploymentTriggerCreate\(input:\$input\)/,
          "trigger creation is not bound to one exact input");
        assert.deepEqual(jsonVariablesFrom(argv), { input: {
          projectId: TARGET.project,
          environmentId: TARGET.environment,
          serviceId: TARGET.service,
          repository: GITHUB_SOURCE.repository,
          branch: GITHUB_SOURCE.branch,
          provider: GITHUB_SOURCE.provider,
          checkSuites: true,
        } });
        if (state.triggerCreateFailure && !state.triggerCreateFailureAfterAccepted) {
          return {
            ...result(state.triggerCreateFailure.stdout || "", 1),
            stderr: state.triggerCreateFailure.stderr || "",
          };
        }
        state.trigger = true;
        state.checkSuites = true;
        if (state.crashAfterAccepted === "trigger_create") {
          state.crashAfterAccepted = null;
          throw new Error("simulated crash after accepted trigger creation");
        }
        if (state.triggerCreateFailureAfterAccepted) {
          return {
            ...result(state.triggerCreateFailure?.stdout || "", 1),
            stderr: state.triggerCreateFailure?.stderr || "",
          };
        }
        const id = state.triggerCreateResponseId || TRIGGER_ID;
        return json({ data: { deploymentTriggerCreate: {
          id,
          projectId: TARGET.project,
          environmentId: TARGET.environment,
          serviceId: TARGET.service,
          repository: GITHUB_SOURCE.repository,
          branch: GITHUB_SOURCE.branch,
          provider: GITHUB_SOURCE.provider,
          checkSuites: true,
          validCheckSuites: state.validCheckSuites ?? 1,
        } } });
      }
      if (query.includes("DirectProdDisableTrigger")) {
        assert.match(query,
          /deploymentTriggerDelete\(id:\$id\)/,
          "trigger disable is not bound to one immutable id");
        assert.equal(vars.id, TRIGGER_ID);
        if (state.triggerDeleteFailure && !state.triggerDeleteFailureAfterAccepted) {
          return {
            ...result(state.triggerDeleteFailure.stdout || "", 1),
            stderr: state.triggerDeleteFailure.stderr || "",
          };
        }
        state.trigger = false;
        if (state.triggerDeleteFailureAfterAccepted) {
          return {
            ...result(state.triggerDeleteFailure?.stdout || "", 1),
            stderr: state.triggerDeleteFailure?.stderr || "",
          };
        }
        return json({ data: { deploymentTriggerDelete: true } });
      }
      if (query.includes("DirectProdDomains")) {
        assert.match(query,
          /tcpProxies\(environmentId:\$environmentId,serviceId:\$serviceId\)/,
          "TCP proxy inventory query is not target-bound");
        const customDomains = state.extraDomain
          ? [domainRecord(DOMAINS[0], true), domainRecord(state.extraDomain, true)]
          : [domainRecord(DOMAINS[0], true)];
        const tcpProxies = state.missingTcpProxyInventory
          ? undefined
          : (state.tcpProxy ? [{
            id: "tcp",
            domain: "tcp.example",
            environmentId: state.tcpProxyEnvironmentId || TARGET.environment,
            serviceId: state.tcpProxyServiceId || TARGET.service,
            syncStatus: "ACTIVE",
            proxyPort: 10000,
            applicationPort: 8080,
          }] : []);
        return json({ data: {
          serviceInstance: {
            environmentId: TARGET.environment,
            serviceId: TARGET.service,
            domains: {
              customDomains,
              serviceDomains: [domainRecord(DOMAINS[1], false)],
            },
          },
          ...(tcpProxies === undefined ? {} : { tcpProxies }),
        } });
      }
      if (query.includes("DirectProdAuthority")) {
        assert.match(query,
          /deploymentTriggers\(projectId:\$projectId,environmentId:\$environmentId,serviceId:\$serviceId,first:100\)/,
          "repository-trigger inventory query is not target-environment scoped");
        assert.equal(vars.projectId, TARGET.project);
        assert.equal(vars.environmentId, TARGET.environment);
        assert.equal(vars.serviceId, TARGET.service);
        return json({ data: {
          serviceInstance: {
            environmentId: TARGET.environment,
            serviceId: TARGET.service,
            source: { repo: state.sourceRepo, image: null },
          },
          service: {
            id: TARGET.service,
            projectId: TARGET.project,
          },
          deploymentTriggers: {
            edges: state.trigger ? [{ node: {
              id: state.triggerId || TRIGGER_ID,
              projectId: TARGET.project,
              environmentId: TARGET.environment,
              serviceId: TARGET.service,
              branch: state.triggerBranch || GITHUB_SOURCE.branch,
              repository: state.triggerRepository || GITHUB_SOURCE.repository,
              provider: GITHUB_SOURCE.provider,
              checkSuites: state.checkSuites,
              validCheckSuites: state.validCheckSuites ?? 1,
            } }] : [],
            pageInfo: { hasNextPage: false },
          },
        } });
      }
      if (query.includes("DirectProdWaitForCI")) {
        assert.equal(vars.id, TRIGGER_ID);
        state.checkSuites = true;
        return json({ data: {
          deploymentTriggerUpdate: { id: TRIGGER_ID, checkSuites: true },
        } });
      }
      if (query.includes("DirectProdActive")) {
        const queued = state.activePollQueue?.length ? state.activePollQueue.shift() : state.active;
        return json({ data: { serviceInstance: {
          activeDeployments: queued.map(applyDeploymentOverrides),
        } } });
      }
      if (query.includes("DirectProdExact")) {
        const row = [...state.active, ...state.history.map((item) => ({
          ...deployment(
            item.id,
            Object.hasOwn(item.meta || {}, "imageDigest")
              ? (item.meta.imageDigest || "")
              : LEGACY.imageDigest,
            item.status,
          ),
          createdAt: item.createdAt,
          meta: item.meta,
          ...(item.snapshotId ? { snapshotId: item.snapshotId } : {}),
          ...(item.unhealthySuccess ? { instances: [] } : {}),
        }))].find((item) => item.id === vars.id) || null;
        return json({ data: { deployment: applyDeploymentOverrides(row) } });
      }
      if (query.includes("DirectProdBackup")) {
        const createdAt = new Date(NOW - (state.staleBackup ? 27 : 1) * 60 * 60 * 1000).toISOString();
        return json({ data: {
          volumeInstance: {
            id: VOLUME_ID,
            environmentId: TARGET.environment,
            serviceId: TARGET.service,
            mountPath: "/data",
            state: "READY",
            isPendingDeletion: false,
          },
          volumeInstanceBackupList: [{
            id: BACKUP_ID,
            externalId: "external",
            name: "coinbase-x402-hotfix-preflight",
            createdAt,
            expiresAt: null,
            usedMB: 10,
            scheduleId: null,
          }],
          volumeInstanceBackupScheduleList: [{ id: "schedule", kind: "DAILY", cron: "0 0 * * *" }],
        } });
      }
      if (query.includes("DirectProdRedeploy") || query.includes("DirectProdRollback")) {
        const mutationKind = query.includes("DirectProdRollback") ? "rollback" : "redeploy";
        if (mutationKind === "rollback" && state.rollbackReturn === false) {
          return json({ data: { deploymentRollback: false } });
        }
        const source = [...state.active, ...state.history.map((item) => ({
          ...deployment(item.id, item.meta?.imageDigest || LEGACY.imageDigest),
          meta: item.meta,
        }))].find((item) => item.id === vars.id);
        assert(source, `missing redeploy source ${vars.id}`);
        const id = state.redeployQueue.shift();
        assert(id, "fake redeploy queue exhausted");
        const deploymentStatus = state.nextRedeployStatus || "SUCCESS";
        const unhealthySuccess = state.nextRedeployUnhealthySuccess === true;
        state.nextRedeployStatus = null;
        state.nextRedeployUnhealthySuccess = false;
        const priorActive = [...state.active];
        if ((deploymentStatus === "SUCCESS" && !unhealthySuccess)
          || state.keepTerminalDeploymentActive
          || state.keepUnhealthyDeploymentActive) {
          for (const prior of priorActive) {
            const historical = state.history.find((item) => item.id === prior.id);
            if (historical) historical.status = "REMOVED";
          }
          const next = deployment(
            id,
            mutationKind === "rollback" && state.rollbackExactDigest
              ? state.rollbackExactDigest
              : source.meta.imageDigest,
            deploymentStatus,
          );
          next.snapshotId = mutationKind === "rollback"
            ? (state.rollbackSnapshotId || source.snapshotId)
            : source.snapshotId;
          if (mutationKind === "rollback") {
            next.meta.reason = state.rollbackReason === undefined
              ? "rollback"
              : state.rollbackReason;
          }
          if (unhealthySuccess) next.instances = [];
          if (deploymentStatus === "SUCCESS" && !unhealthySuccess) {
            queueKnownOverlap(next, priorActive);
          }
          state.active = [next];
        }
        const historyRow = {
          id,
          status: deploymentStatus,
          createdAt: new Date(NOW).toISOString(),
          snapshotId: mutationKind === "rollback"
            ? (state.rollbackSnapshotId || source.snapshotId)
            : source.snapshotId,
          meta: {
            imageDigest: mutationKind === "rollback" && state.rollbackDigest
              ? state.rollbackDigest
              : source.meta.imageDigest,
            ...(mutationKind === "rollback" ? {
              reason: state.rollbackReason === undefined ? "rollback" : state.rollbackReason,
            } : {}),
          },
          unhealthySuccess,
        };
        const historyRows = [historyRow];
        if (mutationKind === "rollback" && state.rollbackAmbiguousHistory === true) {
          historyRows.push({
            ...historyRow,
            id: AMBIGUOUS_ID,
          });
        }
        if (!(mutationKind === "rollback" && state.rollbackHistoryNeverVisible === true)) {
          if (mutationKind === "rollback" && state.rollbackHistoryDelayPolls > 0) {
            state.pendingHistoryRows = historyRows;
            state.historyVisibilityPollsRemaining = state.rollbackHistoryDelayPolls;
          } else {
            state.history.unshift(...historyRows);
          }
        }
        if (state.crashAfterAccepted === mutationKind) {
          state.crashAfterAccepted = null;
          throw new Error(`simulated crash after accepted exact ${mutationKind}`);
        }
        return json({ data: mutationKind === "rollback"
          ? { deploymentRollback: true }
          : { deploymentRedeploy: { id, status: deploymentStatus } } });
      }
    }
    throw new Error(`unhandled command: ${argv.join(" ")}`);
  }

  return { state, run };
}

function domainRecord(domain, custom) {
  return {
    domain,
    environmentId: TARGET.environment,
    serviceId: TARGET.service,
    syncStatus: "ACTIVE",
    ...(custom ? { status: {
      verified: true,
      certificateStatus: "CERTIFICATE_STATUS_TYPE_VALID",
    } } : {}),
  };
}

function deploymentHistory(count) {
  return Array.from({ length: count }, (_, index) => ({
    id: `${index.toString(16).padStart(8, "0")}-0000-4000-8000-000000000000`,
    status: "SUCCESS",
    createdAt: new Date(NOW - index * 1000).toISOString(),
    meta: { imageDigest: LEGACY.imageDigest },
  }));
}

function acceptDelayedSourceMutation(state, visibilityPolls) {
  const active = deployment(CANDIDATE_ID, CANDIDATE_DIGEST);
  Object.assign(active.meta, {
    repo: GITHUB_SOURCE.repository,
    branch: GITHUB_SOURCE.branch,
    commitHash: COMMIT,
  });
  state.sourceRepo = GITHUB_SOURCE.repository;
  state.trigger = true;
  state.pendingHistoryRows = [{
    id: CANDIDATE_ID,
    status: "SUCCESS",
    createdAt: new Date(NOW).toISOString(),
    meta: { ...active.meta },
  }];
  state.historyVisibilityPollsRemaining = visibilityPolls;
  state.pendingSourceActive = active;
}

async function tempState() {
  const directory = await mkdtemp(join(tmpdir(), "coinbase-release-test-"));
  return { directory, path: join(directory, "release-state.json") };
}

async function preflightArgs(path) {
  return [
    "preflight", "--state", path, "--commit", COMMIT,
    "--volume-instance-id", VOLUME_ID,
    "--solana-pay-to", "11111111111111111111111111111111",
    "--base-pay-to", `0x${"1".repeat(40)}`,
    "--repo", GITHUB_SOURCE.repository,
    "--branch", GITHUB_SOURCE.branch,
  ];
}

async function runPreflight(fake, path) {
  return executeReleaseCommand(await preflightArgs(path), {
    run: fake.run,
    now: () => NOW,
    sleep: async () => {},
  });
}

async function leaveSourceArmedWithCompletedAbsenceControl(fake, path) {
  fake.state.onCommand = async (argv, _options, commandState) => {
    if (isSourceConnect(argv) && !commandState.crashedBeforeSourceResult) {
      commandState.crashedBeforeSourceResult = true;
      commandState.sourceRepo = null;
      commandState.trigger = false;
      throw new Error("simulated source-connect crash before authority visibility");
    }
  };
  await runPreflight(fake, path);
  await assert.rejects(executeReleaseCommand([
    "deploy-shadow", "--state", path, "--commit", COMMIT, "--yes",
  ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
  /source-connect crash before authority visibility/);

  let crashedAfterControl = false;
  fake.state.onCommand = async (argv) => {
    if (crashedAfterControl || argv[1] !== "api"
      || !argv[2].includes("DirectProdAuthority")) return;
    const journal = await readState(path);
    if (journal.phase === "github_source_connect_armed"
      && journal.sourceTriggerControl?.githubTriggerDisabled === true) {
      crashedAfterControl = true;
      throw new Error("simulated crash after completed absence control");
    }
  };
  await assert.rejects(executeReleaseCommand([
    "recover", "--state", path, "--reason", "persist absence evidence",
    "--poll-seconds", "30", "--yes",
  ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
  /crash after completed absence control/);
  fake.state.onCommand = null;
  const journal = await readState(path);
  assert.equal(journal.phase, "github_source_connect_armed");
  assert.equal(journal.pendingTriggerDisable, undefined);
  assert.equal(journal.sourceTriggerControl.triggerId, null);
  assert.equal(journal.sourceTriggerControl.sourceMetadataRetained, false);
  return journal;
}

const tests = [];
function test(name, fn) { tests.push({ name, fn }); }

test("fixed target, rollback point, and funded command refusal", async () => {
  assert.equal(TARGET.project, "9fc6c062-6d58-4cb9-af11-df68670bfca5");
  assert.equal(TARGET.environment, "9d51961d-759c-441b-be1d-186515b9ed7f");
  assert.equal(TARGET.service, "8853c53e-521e-4876-a796-f94c1adf5700");
  assert.equal(LEGACY.deploymentId, "66920048-282d-45e6-a302-db6da1702058");
  assert.equal(LEGACY.snapshotId, "e37a3aeb-562f-4712-9d37-f68c59c8c648");
  assert.deepEqual(DOMAINS, [
    "mcp.blocksize.info",
    "agentic-payments-production.up.railway.app",
  ]);
  assert.deepEqual(AUDIT_DOMAINS, [
    "https://mcp.blocksize.info",
    "https://agentic-payments-production.up.railway.app",
  ]);
  assert.throws(() => parseArguments(["funded-test"]), /deliberately absent/);
  assert.throws(() => parseArguments(["settle"]), /deliberately absent/);
  assert.throws(() => parseArguments(["payment-smoke"]), /deliberately absent/);
  assert.equal(SHADOW_VARIABLES.X402_PAYMENT_DB_PATH, "/data/x402_payments.sqlite3");
  assert.equal(SHADOW_VARIABLES.X402_ENFORCE_GET_ROUTES, "v1_vwap");
  assert.equal(SHADOW_VARIABLES.X402_PAYMENT_RATE_LIMIT_PER_MINUTE, "12");
  assert.equal(SHADOW_VARIABLES.X402_PAYMENT_RATE_LIMIT_PER_DAY, "200");
  assert.equal(SHADOW_VARIABLES.X402_FACILITATOR_MAX_INFLIGHT, "4");
  assert.equal(Object.hasOwn(SHADOW_VARIABLES, "X402_FACILITATOR_URL"), false);
});

test("hosted audit executes through a symlinked canonical path", async () => {
  const root = await mkdtemp(join(tmpdir(), "blocksize-audit-symlink-"));
  const target = fileURLToPath(new URL("./audit_coinbase_hotfix.mjs", import.meta.url));
  const linked = join(root, "audit_coinbase_hotfix.mjs");
  try {
    await symlink(target, linked);
    const executed = await defaultRun([
      process.execPath,
      linked,
      "--mode", "invalid",
    ], { timeoutMs: 5_000 });
    assert.equal(executed.code, 1);
    assert.match(executed.stderr, /--mode must be shadow or enforce/);
  } finally {
    await rm(root, { recursive: true, force: true });
  }
});

test("release summary reports trigger containment without exposing state internals", async () => {
  const summary = summarizeReleaseState({
    phase: "recovered_legacy",
    rollbackBoundary: "legacy_allowed_before_enforce",
    current: { deploymentId: LEGACY.deploymentId, imageDigest: LEGACY.imageDigest },
    recovery: {
      githubTriggerDisabled: true,
      sourceMetadataRetained: true,
      triggerDeletionMayHaveBeenAttempted: true,
      githubTriggerDisabledAt: "2026-08-21T18:00:00.000Z",
    },
    sourceTriggerControl: {
      githubTriggerDisabled: true,
      triggerId: TRIGGER_ID,
      sourceMetadataRetained: true,
      triggerDeletionMayHaveBeenAttempted: true,
      providerReportedSuccess: false,
      disabledAt: "2026-08-21T18:00:00.000Z",
      privateProviderOutput: "must-not-be-reported",
    },
    unrelatedSecret: "must-not-be-reported",
  });
  assert.equal(summary.githubTriggerDisabled, true);
  assert.equal(summary.sourceMetadataRetained, true);
  assert.equal(summary.triggerDeletionMayHaveBeenAttempted, true);
  assert.equal(summary.triggerId, TRIGGER_ID);
  assert.equal(summary.triggerDisableProviderReportedSuccess, false);
  assert.equal(summary.githubTriggerDisabledAt, "2026-08-21T18:00:00.000Z");
  assert.equal(JSON.stringify(summary).includes("must-not-be-reported"), false);
});

test("preflight binds exact production evidence and atomically writes mode 0600 state", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway();
    const state = await runPreflight(fake, temporary.path);
    assert.equal(state.phase, "preflight_passed");
    assert.equal(state.current.deploymentId, LEGACY.deploymentId);
    assert.equal(state.artifactTree, TREE);
    assert.equal(state.backup.backupId, BACKUP_ID);
    assert.equal((await stat(temporary.path)).mode & 0o777, 0o600);
    assert.deepEqual((await readState(temporary.path)).target, TARGET);
    assert.equal(fake.state.commands.some(({ argv }) => argv.includes("restore")), false);
    assert.equal(fake.state.commands.some(({ argv }) => argv[1] === "variable"
      && argv[2] === "list"), false);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("preflight refuses to overwrite an existing release state", async () => {
  const temporary = await tempState();
  try {
    await runPreflight(fakeRailway(), temporary.path);
    await assert.rejects(runPreflight(fakeRailway(), temporary.path), /state already exists/);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("preflight accepts only retained exact inert GitHub source metadata", async () => {
  const retained = await tempState();
  const drifted = await tempState();
  try {
    const exact = fakeRailway({ sourceRepo: GITHUB_SOURCE.repository, trigger: false });
    const state = await runPreflight(exact, retained.path);
    assert.equal(state.preflightSourceMetadataRetained, true);

    const wrong = fakeRailway({ sourceRepo: "someone/else", trigger: false });
    await assert.rejects(runPreflight(wrong, drifted.path),
      /unexpected service source/);
  } finally {
    await rm(retained.directory, { recursive: true, force: true });
    await rm(drifted.directory, { recursive: true, force: true });
  }
});

test("release state rejects the retired legacy rollback deployment", async () => {
  const temporary = await tempState();
  try {
    await runPreflight(fakeRailway(), temporary.path);
    const state = JSON.parse(await readFile(temporary.path, "utf8"));
    state.legacy.deploymentId = "a676ba77-412b-4ae4-8606-87ade7c9ff53";
    await writeFile(temporary.path, `${JSON.stringify(state)}\n`, { mode: 0o600 });
    await assert.rejects(readState(temporary.path), /legacy rollback point drifted/);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

for (const [label, overrides, pattern] of [
  ["extra domain", { extraDomain: "other.example" }, /domain inventory differs/],
  ["TCP proxy", { tcpProxy: true }, /TCP proxy/],
  ["cross-target TCP proxy", { tcpProxy: true, tcpProxyServiceId: "wrong-service" }, /TCP proxy inventory is not bound/],
  ["missing TCP proxy inventory", { missingTcpProxyInventory: true }, /incomplete TCP proxy inventory/],
  ["repository trigger", { trigger: true }, /auto-deploy trigger/],
  ["unrelated staged environment patch", {
    stagedPatch: { services: { "other-service": { source: { image: "nginx:latest" } } } },
  }, /unrelated staged environment patch/],
  ["remote branch drift", { remoteCommit: "d".repeat(40) }, /exact reviewed commit/],
  ["failed production-branch workflow", { githubWorkflowFailure: true }, /completed successful push workflow/],
  ["mutable production branch", { githubMutableBranch: true }, /not immutable and deletion-protected/],
  ["bypassable production branch", { githubRulesetBypass: true }, /missing, bypassable, or drifted/],
  ["stale backup", { staleBackup: true }, /no completed, locked, named on-demand backup/],
  ["ignored SQLite ledger", { ignoredFiles: ["x402_payments.sqlite3"] }, /ignored database/],
  ["insufficient deployment headroom", { history: deploymentHistory(996) }, /lacks release and recovery headroom/],
]) {
  test(`preflight rejects ${label}`, async () => {
    const temporary = await tempState();
    try {
      const fake = fakeRailway(overrides);
      await assert.rejects(runPreflight(fake, temporary.path), pattern);
    } finally {
      await rm(temporary.directory, { recursive: true, force: true });
    }
  });
}

test("fixed target lock serializes controllers even with different state paths", async () => {
  const first = await tempState();
  const second = await tempState();
  let releaseFirst;
  let markStarted;
  const gate = new Promise((resolvePromise) => { releaseFirst = resolvePromise; });
  const started = new Promise((resolvePromise) => { markStarted = resolvePromise; });
  const fake = fakeRailway();
  const run = async (argv, options) => {
    if (argv[0] === "git" && argv[1] === "rev-parse" && argv[2] === "HEAD") {
      markStarted();
      await gate;
    }
    return fake.run(argv, options);
  };
  try {
    const firstRun = executeReleaseCommand(await preflightArgs(first.path), {
      run,
      now: () => NOW,
      sleep: async () => {},
    });
    await started;
    await assert.rejects(runPreflight(fakeRailway(), second.path), /fixed production target lock/);
    releaseFirst();
    await firstRun;
  } finally {
    releaseFirst();
    await rm(first.directory, { recursive: true, force: true });
    await rm(second.directory, { recursive: true, force: true });
  }
});

test("kernel lock safely reuses a stale PID sentinel without unlinking its inode", async () => {
  const temporary = await tempState();
  try {
    const before = await stat(TARGET_LOCK_PATH);
    await writeFile(TARGET_LOCK_PATH, "2147483647\n", { mode: 0o600 });
    await runPreflight(fakeRailway(), temporary.path);
    const after = await stat(TARGET_LOCK_PATH);
    assert.equal(after.dev, before.dev);
    assert.equal(after.ino, before.ino);
    assert.equal(after.mode & 0o777, 0o600);
    assert.equal((await readFile(TARGET_LOCK_PATH, "utf8")), `${process.pid}\n`);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("shadow deploy stages exact variables, binds one deployment, and requires two-cycle audit", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway();
    await runPreflight(fake, temporary.path);
    const state = await executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    assert.equal(state.phase, "shadow_validated");
    assert.equal(state.candidate.buildDeploymentId, CANDIDATE_ID);
    assert.equal(state.current.deploymentId, SHADOW_ID);
    assert.equal(state.current.imageDigest, CANDIDATE_DIGEST);
    assert.equal(fake.state.variables.X402_PAYMENT_MODE, "shadow");
    for (const [name, value] of Object.entries(SHADOW_VARIABLES)) {
      assert.equal(fake.state.variables[name], value);
    }
    assert.equal(fake.state.variables.RELEASE_GIT_COMMIT, COMMIT);
    assert.equal(fake.state.variables.RELEASE_GIT_REPOSITORY, GITHUB_SOURCE.repository);
    assert.equal(fake.state.variables.RELEASE_GIT_BRANCH, GITHUB_SOURCE.branch);
    assert.equal(fake.state.variables.RELEASE_IMAGE_DIGEST, CANDIDATE_DIGEST);
    assert.equal(fake.state.variables.RELEASE_IDENTITY_PHASE, "bound");
    const releaseVariableWrites = fake.state.commands
      .filter(({ argv }) => argv[1] === "variable" && argv[2] === "set")
      .map(({ argv, options }) => [argv[3], String(options.stdin || "").trim()]);
    assert.deepEqual(
      releaseVariableWrites.filter(([name]) => name === "RELEASE_IMAGE_DIGEST"),
      [
        ["RELEASE_IMAGE_DIGEST", "unbound"],
        ["RELEASE_IMAGE_DIGEST", CANDIDATE_DIGEST],
      ],
    );
    assert.deepEqual(
      releaseVariableWrites.filter(([name]) => name === "RELEASE_IDENTITY_PHASE"),
      [
        ["RELEASE_IDENTITY_PHASE", "source_build"],
        ["RELEASE_IDENTITY_PHASE", "bound"],
      ],
    );
    const sourceConnectIndex = fake.state.commands.findIndex(({ argv }) => isSourceConnect(argv));
    const firstRedeployIndex = fake.state.commands.findIndex(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdRedeploy"));
    const variableWriteIndex = (name, value) => fake.state.commands.findIndex(
      ({ argv, options }) => argv[1] === "variable" && argv[2] === "set"
        && argv[3] === name && String(options.stdin || "").trim() === value,
    );
    for (const [name, value] of [
      ["RELEASE_GIT_COMMIT", COMMIT],
      ["RELEASE_GIT_REPOSITORY", GITHUB_SOURCE.repository],
      ["RELEASE_GIT_BRANCH", GITHUB_SOURCE.branch],
      ["RELEASE_IMAGE_DIGEST", "unbound"],
      ["RELEASE_IDENTITY_PHASE", "source_build"],
    ]) {
      const index = variableWriteIndex(name, value);
      assert(index >= 0 && index < sourceConnectIndex, `${name} was not staged before source connect`);
    }
    for (const [name, value] of [
      ["RELEASE_IMAGE_DIGEST", CANDIDATE_DIGEST],
      ["RELEASE_IDENTITY_PHASE", "bound"],
    ]) {
      const index = variableWriteIndex(name, value);
      assert(index > sourceConnectIndex && index < firstRedeployIndex,
        `${name} was not bound before the exact-image redeploy`);
    }
    const audit = fake.state.commands.find(({ argv }) => argv[1]?.endsWith("audit_coinbase_hotfix.mjs"));
    assert(audit);
    assert.equal(audit.argv[audit.argv.indexOf("--checks") + 1], "2");
    const upload = fake.state.commands.find(({ argv }) => isSourceConnect(argv));
    assert(upload.argv.includes(TARGET.project) && upload.argv.includes(TARGET.environment));
    assert.deepEqual(JSON.parse(upload.options.stdin), {
      services: { [TARGET.service]: { source: {
        repo: GITHUB_SOURCE.repository,
        branch: GITHUB_SOURCE.branch,
        commitSha: COMMIT,
        checkSuites: true,
      } } },
    });
    assert.equal(state.uploadSource.kind, "github");
    assert.equal(state.uploadSource.tree, TREE);
    assert.equal(fake.state.sourceRepo, GITHUB_SOURCE.repository);
    assert.equal(fake.state.trigger, true);
    assert.equal(fake.state.checkSuites, true);
    const triggerCreates = fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdCreateTrigger"));
    assert.equal(triggerCreates.length, 1);
    assert.deepEqual(jsonVariablesFrom(triggerCreates[0].argv), { input: {
      projectId: TARGET.project,
      environmentId: TARGET.environment,
      serviceId: TARGET.service,
      repository: GITHUB_SOURCE.repository,
      branch: GITHUB_SOURCE.branch,
      provider: GITHUB_SOURCE.provider,
      checkSuites: true,
    } });
    assert.equal(state.sourceTrigger.origin, "explicit_create");
    assert.equal(state.sourceTrigger.mutationMayHaveBeenAttempted, true);
    assert.equal(fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdBackup")).length, 2);
    assert.equal(state.backupRevalidatedAt, new Date(NOW).toISOString());
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("shadow deploy accepts an exact trigger created atomically with the source patch", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway({ sourceConnectCreatesTrigger: true });
    await runPreflight(fake, temporary.path);
    const state = await executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    assert.equal(state.phase, "shadow_validated");
    assert.equal(state.sourceTrigger.origin, "source_patch");
    assert.equal(state.sourceTrigger.mutationMayHaveBeenAttempted, false);
    assert.equal(fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdCreateTrigger")).length, 0);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("trigger creation reconciles an accepted mutation after a provider failure", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway({
      triggerCreateFailureAfterAccepted: true,
      triggerCreateFailure: { stderr: "upstream failed after accepting trigger" },
    });
    await runPreflight(fake, temporary.path);
    const state = await executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    assert.equal(state.phase, "shadow_validated");
    assert.equal(state.sourceTrigger.origin, "explicit_create");
    assert.equal(state.sourceTrigger.providerReportedSuccess, false);
    assert.equal(fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdCreateTrigger")).length, 1);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("crash after accepted trigger creation recovers without recreating it", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway({ crashAfterAccepted: "trigger_create" });
    await runPreflight(fake, temporary.path);
    await assert.rejects(executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
    /crash after accepted trigger creation/);
    const armed = await readState(temporary.path);
    assert.equal(armed.phase, "github_source_connect_armed");
    assert.equal(armed.pendingTriggerCreate.sourceDeploymentId, CANDIDATE_ID);
    assert(Number.isFinite(Date.parse(armed.pendingTriggerCreate.dispatchArmedAt)));
    assert.equal(fake.state.trigger, true);

    const state = await executeReleaseCommand([
      "recover", "--state", temporary.path,
      "--reason", "contain accepted trigger create", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    assert.equal(state.phase, "recovered_legacy");
    assert.equal(fake.state.trigger, false);
    assert.equal(state.pendingTriggerCreate, undefined);
    assert.equal(state.triggerCreateProcess.mutationMayHaveBeenAttempted, true);
    assert.equal(fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdCreateTrigger")).length, 1);
    assert.equal(fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdDisableTrigger")).length, 1);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("failed trigger creation stays armed and is never retried by deploy", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway({
      triggerCreateFailure: { stderr: "provider rejected exact trigger" },
    });
    await runPreflight(fake, temporary.path);
    await assert.rejects(executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
    /Railway production trigger creation failed/);
    const state = await readState(temporary.path);
    assert.equal(state.phase, "github_source_connect_armed");
    assert.equal(state.pendingTriggerCreate.providerReportedSuccess, false);
    assert.equal(fake.state.trigger, false);
    assert.equal(fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdCreateTrigger")).length, 1);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("shadow deploy reconciles an exact source mutation after the provider reports failure", async () => {
  const temporary = await tempState();
  const secret = "provider-detail-that-must-not-enter-state";
  try {
    const fake = fakeRailway({
      uploadFailureAfterAccepted: true,
      uploadFailure: {
        stderr: `service connection partially failed token=${secret}`,
      },
    });
    await runPreflight(fake, temporary.path);
    const state = await executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    assert.equal(state.phase, "shadow_validated");
    assert.equal(state.candidate.buildDeploymentId, CANDIDATE_ID);
    assert.deepEqual(state.sourceConnectProcess, {
      providerReportedSuccess: false,
      exactMutationObservedAfterProviderFailure: true,
      reconciledAt: new Date(NOW).toISOString(),
    });
    assert.equal(fake.state.commands.filter(({ argv }) => isSourceConnect(argv)).length, 1);
    assert.equal(fake.state.checkSuites, true);
    assert.doesNotMatch(await readFile(temporary.path, "utf8"), new RegExp(secret));
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("provider-reported source failure observes the final 60-second evidence poll", async () => {
  const temporary = await tempState();
  let sleeps = 0;
  try {
    const fake = fakeRailway({
      uploadFailureAfterAccepted: true,
      uploadFailure: { stderr: "upstream returned an error after mutation" },
      uploadHistoryVisibilityPolls: 2,
    });
    await runPreflight(fake, temporary.path);
    const state = await executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT,
      "--timeout-seconds", "60", "--poll-seconds", "30", "--yes",
    ], {
      run: fake.run,
      now: () => NOW,
      sleep: async () => { sleeps += 1; },
    });
    assert.equal(state.phase, "shadow_validated");
    assert.equal(fake.state.historyVisibilityPollsRemaining, 0);
    assert.equal(sleeps, 4);
    const connectIndex = fake.state.commands.findIndex(({ argv }) => isSourceConnect(argv));
    const postConnectHistoryPolls = fake.state.commands.slice(connectIndex + 1)
      .filter(({ argv }) => argv[1] === "deployment" && argv[2] === "list");
    assert(postConnectHistoryPolls.length >= 3);
    assert.equal(fake.state.commands.filter(({ argv }) => isSourceConnect(argv)).length, 1);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("shadow deploy rejects a source-connected deployment from any other commit", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway({ sourceDeploymentCommit: "d".repeat(40) });
    await runPreflight(fake, temporary.path);
    await assert.rejects(executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
    /does not match the exact repository, branch, and commit/);
    assert.equal((await readState(temporary.path)).phase, "github_source_connect_armed");
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("shadow deploy refuses a Railway trigger with no valid check suite", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway({ validCheckSuites: 0 });
    await runPreflight(fake, temporary.path);
    await assert.rejects(executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
    /valid GitHub check suite/);
    assert.equal((await readState(temporary.path)).phase, "github_source_connect_armed");
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("recovery contains an auto-created trigger with no valid check suite", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway({
      sourceConnectCreatesTrigger: true,
      validCheckSuites: 0,
    });
    await runPreflight(fake, temporary.path);
    await assert.rejects(executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
    /no valid GitHub check suite/);
    const armed = await readState(temporary.path);
    assert.equal(armed.phase, "github_source_connect_armed");
    assert.equal(armed.pendingTriggerCreate.sourceDeploymentId, CANDIDATE_ID);
    assert.equal(armed.pendingTriggerCreate.observedTriggerId, TRIGGER_ID);
    assert.equal(fake.state.trigger, true);

    const state = await executeReleaseCommand([
      "recover", "--state", temporary.path,
      "--reason", "contain invalid auto-created trigger", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    assert.equal(state.phase, "recovered_legacy");
    assert.equal(fake.state.trigger, false);
    assert.equal(fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdCreateTrigger")).length, 0);
    assert.equal(fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdDisableTrigger")).length, 1);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("recovery contains an invalid auto-trigger after an unjournaled source crash", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway({
      sourceConnectCreatesTrigger: true,
      validCheckSuites: 0,
      crashAfterAccepted: "upload",
    });
    await runPreflight(fake, temporary.path);
    await assert.rejects(executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
    /crash after accepted upload/);
    const armed = await readState(temporary.path);
    assert.equal(armed.phase, "github_source_connect_armed");
    assert.equal(armed.pendingTriggerCreate, undefined);
    assert.equal(fake.state.trigger, true);

    const state = await executeReleaseCommand([
      "recover", "--state", temporary.path,
      "--reason", "contain unjournaled invalid auto-trigger", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    assert.equal(state.phase, "recovered_legacy");
    assert.equal(fake.state.trigger, false);
    assert.equal(fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdCreateTrigger")).length, 0);
    assert.equal(fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdDisableTrigger")).length, 1);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("structured shadow upload failure exposes only sanitized Railway JSON fields", async () => {
  const temporary = await tempState();
  const secret = "super-secret-upload-token";
  const bareSecret = "ghp_1234567890abcdefghijklmnopqrstuv";
  const signedUrl = "https://uploads.railway.example/object?X-Amz-Signature=abcdef";
  try {
    const fake = fakeRailway({
      uploadFailure: {
        stdout: JSON.stringify({
          code: "UPLOAD_FAILED",
          error: `connection reset at ${signedUrl}`,
          hint: `token=${secret} path=/Users/operator/private.pem bearer ${bareSecret}`,
        }),
      },
    });
    await runPreflight(fake, temporary.path);
    await assert.rejects(executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }), (error) => {
      assert.match(error.message, /Railway GitHub source connection failed \(exit=1,signal=none/);
      assert.match(error.message, /code=UPLOAD_FAILED/);
      assert.match(error.message, /markers=connection/);
      assert.match(error.message, /errorBytes=\d+; hintBytes=\d+; hasHint=true/);
      assert.doesNotMatch(error.message, /super-secret-upload-token/);
      assert.doesNotMatch(error.message, /ghp_1234567890/);
      assert.doesNotMatch(error.message, /uploads\.railway\.example/);
      assert.doesNotMatch(error.message, /Users\/operator/);
      assert.doesNotMatch(error.message, /connection reset at/);
      return true;
    });
    assert.equal((await readState(temporary.path)).phase, "github_source_connect_armed");
    assert.equal(fake.state.history.length, 1);
    assert.equal(fake.state.commands.filter(({ argv }) => isSourceConnect(argv)).length, 1);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("malformed shadow upload failure retains only markers and byte counts", async () => {
  const temporary = await tempState();
  const secret = "do-not-emit-this-upload-secret";
  try {
    const fake = fakeRailway({
      uploadFailure: {
        stdout: `not-json connection reset token=${secret}`,
        stderr: "socket closed with EOF",
      },
    });
    await runPreflight(fake, temporary.path);
    await assert.rejects(executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }), (error) => {
      assert.match(error.message, /unstructuredMarkers=connection/);
      assert.match(error.message, /stdoutBytes=\d+,stderrBytes=\d+/);
      assert.doesNotMatch(error.message, /diagnosticSha256/);
      assert.doesNotMatch(error.message, /do-not-emit-this-upload-secret/);
      assert.doesNotMatch(error.message, /not-json/);
      return true;
    });
    assert.equal((await readState(temporary.path)).phase, "github_source_connect_armed");
    assert.equal(fake.state.commands.filter(({ argv }) => isSourceConnect(argv)).length, 1);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("structured failure parser uses only one bounded final JSON line", async () => {
  const temporary = await tempState();
  const secretPrefix = "provider-secret-that-must-never-appear-";
  try {
    const fake = fakeRailway({
      uploadFailure: {
        stdout: `${secretPrefix}${"{".repeat(100_000)}\n${JSON.stringify({
          code: "UPLOAD_TIMEOUT",
          error: "connection timeout after upload",
          hint: null,
        })}`,
      },
    });
    await runPreflight(fake, temporary.path);
    await assert.rejects(executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }), (error) => {
      assert.match(error.message, /code=UPLOAD_TIMEOUT; markers=timeout,connection/);
      assert.doesNotMatch(error.message, /provider-secret/);
      assert(error.message.length < 700);
      return true;
    });
    assert.equal((await readState(temporary.path)).phase, "github_source_connect_armed");
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("enforce promotion closes rollback boundary before mutation and redeploys the exact image", async () => {
  const temporary = await tempState();
  try {
    let sawClosedBoundary = false;
    const fake = fakeRailway({
      onCommand: async (argv) => {
        if (argv[1] === "variable" && argv[2] === "set"
          && argv[3] === "X402_PAYMENT_MODE") {
          try {
            const saved = JSON.parse(await readFile(temporary.path, "utf8"));
            if (saved.phase === "enforce_promotion_armed"
              && saved.rollbackBoundary === "same_commit_shadow_only") sawClosedBoundary = true;
          } catch {}
        }
      },
    });
    await runPreflight(fake, temporary.path);
    await executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    const state = await executeReleaseCommand([
      "promote-enforce", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    assert.equal(sawClosedBoundary, true);
    assert.equal(state.phase, "enforce_unfunded_validated");
    assert.equal(state.current.deploymentId, ENFORCE_ID);
    assert.equal(state.current.imageDigest, CANDIDATE_DIGEST);
    assert.equal(state.rollbackBoundary, "same_commit_shadow_only");
    const redeploy = fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdRedeploy")).at(-1);
    assert(redeploy.argv.includes(`id=${SHADOW_ID}`));
    const redeployIndex = fake.state.commands.indexOf(redeploy);
    const enforcePhaseIndex = fake.state.commands.findLastIndex(({ argv, options }) => (
      argv[1] === "variable" && argv[2] === "set" && argv[3] === "RELEASE_IDENTITY_PHASE"
      && String(options.stdin || "").trim() === "bound"
    ));
    assert(enforcePhaseIndex >= 0 && enforcePhaseIndex < redeployIndex);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("enforce promotion refuses before boundary mutation when shadow cannot roll back", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway();
    await runPreflight(fake, temporary.path);
    await executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    fake.state.nonRollbackDeploymentId = SHADOW_ID;
    const before = fake.state.commands.length;
    await assert.rejects(executeReleaseCommand([
      "promote-enforce", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }), /rollback-capable/);
    const state = await readState(temporary.path);
    assert.equal(state.phase, "shadow_validated");
    assert.equal(state.rollbackBoundary, "legacy_allowed_before_enforce");
    const recent = fake.state.commands.slice(before);
    assert.equal(recent.some(({ argv }) => argv[1] === "variable" && argv[2] === "set"), false);
    assert.equal(recent.some(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdRedeploy")), false);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("enforce promotion refuses when Railway Wait for CI is disabled", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway();
    await runPreflight(fake, temporary.path);
    await executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    fake.state.checkSuites = false;
    await assert.rejects(executeReleaseCommand([
      "promote-enforce", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }), /not waiting for GitHub checks/);
    assert.equal((await readState(temporary.path)).rollbackBoundary,
      "legacy_allowed_before_enforce");
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("recovery uses legacy only before enforce and never restores a volume", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway();
    await runPreflight(fake, temporary.path);
    const state = await executeReleaseCommand([
      "recover", "--state", temporary.path, "--reason", "operator requested rollback", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    assert.equal(state.phase, "recovered_legacy");
    assert.equal(state.current.imageDigest, LEGACY.imageDigest);
    assert.equal(state.current.mode, "legacy");
    assert.equal(state.recovery.rollbackRestoresSnapshotVariables, true);
    assert.equal(state.recovery.legacyX402MayRemainLive, true);
    assert.equal(state.recovery.legacyIdentityAudit.deploymentId, state.current.deploymentId);
    assert.equal(state.recovery.legacyIdentityAudit.imageDigest, LEGACY.imageDigest);
    assert.equal(state.recovery.legacyIdentityAudit.railwayStatus, "SUCCESS");
    assert.equal(state.recovery.legacyIdentityAudit.rollbackRestoredSnapshotVariables, true);
    assert.equal(state.recovery.legacyIdentityAudit.legacyX402MayRemainLive, true);
    assert.equal(state.lastAcceptedRedeploy.mutationKind, "rollback");
    assert.equal(state.lastAcceptedRedeploy.sourceSnapshotId, LEGACY.snapshotId);
    assert.equal(Object.hasOwn(fake.state.variables, "X402_PAYMENT_MODE"), false);
    const rollback = fake.state.commands.find(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdRollback"));
    assert.match(rollback.argv[2], /deploymentRollback\(id:\$id\)\}/);
    assert.doesNotMatch(rollback.argv[2], /deploymentRollback\(id:\$id\)\{/);
    const commands = fake.state.commands.map(({ argv }) => argv.join(" ")).join("\n");
    assert.equal(/volume.*restore|backup.*restore/i.test(commands), false);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("recovery disables the exact production trigger and retains inert source metadata", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway();
    await runPreflight(fake, temporary.path);
    await executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    const commandOffset = fake.state.commands.length;
    const state = await executeReleaseCommand([
      "recover", "--state", temporary.path, "--reason", "disable exact trigger", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    const recoveryCommands = fake.state.commands.slice(commandOffset);
    assert.equal(state.phase, "recovered_legacy");
    assert.equal(fake.state.trigger, false);
    assert.equal(fake.state.sourceRepo, GITHUB_SOURCE.repository);
    assert.equal(state.recovery.githubTriggerDisabled, true);
    assert.equal(state.recovery.sourceMetadataRetained, true);
    assert.equal(recoveryCommands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdDisableTrigger")).length, 1);
    assert.equal(recoveryCommands.some(({ argv }) => argv[1] === "service"
      && argv[2] === "source" && argv[3] === "disconnect"), false);
    assert.equal(recoveryCommands.some(({ argv }) => argv[1] === "api"
      && /environmentStageChanges|environmentPatchCommit/.test(argv[2])), false);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("recovery preserves an unrelated staged production patch while disabling the trigger", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway();
    await runPreflight(fake, temporary.path);
    await executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    fake.state.stagedPatch = { variables: { UNRELATED_CHANGE: "keep-me" } };
    fake.state.stagedPatchId = "unrelated-staged-patch";
    const commandOffset = fake.state.commands.length;
    const state = await executeReleaseCommand([
      "recover", "--state", temporary.path,
      "--reason", "preserve unrelated staged patch", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    assert.equal(state.phase, "recovered_legacy");
    assert.deepEqual(fake.state.stagedPatch, {
      variables: { UNRELATED_CHANGE: "keep-me" },
    });
    assert.equal(fake.state.stagedPatchId, "unrelated-staged-patch");
    const recoveryCommands = fake.state.commands.slice(commandOffset);
    assert.equal(recoveryCommands.some(({ argv }) => argv[1] === "api"
      && /environmentStageChanges|environmentPatchCommit/.test(argv[2])), false);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("final recovery containment refuses an unknown active deployment after deleting its trigger", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway();
    await runPreflight(fake, temporary.path);
    await executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    fake.state.onCommand = async (argv, _options, commandState) => {
      if (argv[1] !== "api" || !argv[2].includes("DirectProdAuthority")
        || commandState.injectedUnknownRecoveryActive) return;
      const journal = await readState(temporary.path);
      if (journal.phase !== "recovery_deployment_bound") return;
      commandState.injectedUnknownRecoveryActive = true;
      commandState.trigger = true;
      commandState.sourceRepo = GITHUB_SOURCE.repository;
      commandState.active = [deployment(AMBIGUOUS_ID, CANDIDATE_DIGEST)];
    };

    await assert.rejects(executeReleaseCommand([
      "recover", "--state", temporary.path,
      "--reason", "refuse unknown final active", "--poll-seconds", "30", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
    /trigger disable ended with an unknown active deployment/);
    const state = await readState(temporary.path);
    assert.equal(state.phase, "recovery_deployment_bound");
    assert.equal(state.pendingTriggerDisable.triggerId, TRIGGER_ID);
    assert.equal(fake.state.trigger, false);
    assert.equal(fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdDisableTrigger")).length, 2);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("completed final containment rechecks and refuses an unknown active deployment", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway();
    await runPreflight(fake, temporary.path);
    await executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    fake.state.onCommand = async (argv, _options, commandState) => {
      if (argv[1] !== "api" || !argv[2].includes("DirectProdAuthority")
        || commandState.injectedUnknownRecoveryActive) return;
      const journal = await readState(temporary.path);
      if (journal.phase !== "recovery_deployment_bound") return;
      commandState.injectedUnknownRecoveryActive = true;
      commandState.trigger = false;
      commandState.sourceRepo = GITHUB_SOURCE.repository;
      commandState.active = [deployment(AMBIGUOUS_ID, CANDIDATE_DIGEST)];
    };

    await assert.rejects(executeReleaseCommand([
      "recover", "--state", temporary.path,
      "--reason", "recheck completed final containment", "--poll-seconds", "30", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
    /trigger disable ended with an unknown active deployment/);
    const state = await readState(temporary.path);
    assert.equal(state.phase, "recovery_deployment_bound");
    assert.equal(state.sourceTriggerControl.githubTriggerDisabled, true);
    assert.equal(state.pendingTriggerDisable, undefined);
    assert.equal(fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdDisableTrigger")).length, 1);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("no-source final containment rechecks and refuses an unknown active deployment", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway();
    await runPreflight(fake, temporary.path);
    fake.state.onCommand = async (argv, _options, commandState) => {
      if (argv[1] !== "api" || !argv[2].includes("DirectProdAuthority")
        || commandState.injectedUnknownRecoveryActive) return;
      const journal = await readState(temporary.path);
      if (journal.phase !== "recovery_deployment_bound") return;
      commandState.injectedUnknownRecoveryActive = true;
      commandState.sourceRepo = null;
      commandState.trigger = false;
      commandState.active = [deployment(AMBIGUOUS_ID, CANDIDATE_DIGEST)];
    };

    await assert.rejects(executeReleaseCommand([
      "recover", "--state", temporary.path,
      "--reason", "recheck no-source final containment", "--poll-seconds", "30", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
    /trigger disable ended with an unknown active deployment/);
    const state = await readState(temporary.path);
    assert.equal(state.phase, "recovery_deployment_bound");
    assert.equal(state.pendingTriggerDisable, undefined);
    assert.equal(state.sourceTriggerControl, undefined);
    assert.equal(fake.state.commands.some(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdDisableTrigger")), false);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("trigger disable reconciles an accepted mutation after a sanitized provider failure", async () => {
  const temporary = await tempState();
  const secret = "provider-secret-must-not-persist";
  try {
    const fake = fakeRailway({
      triggerDeleteFailureAfterAccepted: true,
      triggerDeleteFailure: { stderr: `upstream error ${secret}` },
    });
    await runPreflight(fake, temporary.path);
    await executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    const state = await executeReleaseCommand([
      "recover", "--state", temporary.path,
      "--reason", "reconcile accepted trigger disable", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    assert.equal(state.phase, "recovered_legacy");
    assert.equal(state.sourceTriggerControl.providerReportedSuccess, false);
    assert.equal(state.sourceTriggerControl.githubTriggerDisabled, true);
    assert.equal(fake.state.trigger, false);
    assert.doesNotMatch(await readFile(temporary.path, "utf8"), new RegExp(secret));
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("crash after accepted trigger disable resumes without a second delete", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway();
    await runPreflight(fake, temporary.path);
    await executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    fake.state.onCommand = async (argv, _options, commandState) => {
      if (argv[1] === "api" && argv[2].includes("DirectProdDisableTrigger")
        && !commandState.crashedAfterTriggerDisable) {
        const journal = await readState(temporary.path);
        assert.equal(journal.pendingTriggerDisable.triggerId, TRIGGER_ID);
        commandState.crashedAfterTriggerDisable = true;
        commandState.trigger = false;
        throw new Error("simulated crash after accepted trigger disable");
      }
    };
    await assert.rejects(executeReleaseCommand([
      "recover", "--state", temporary.path,
      "--reason", "crash after trigger disable", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
    /simulated crash after accepted trigger disable/);
    assert.equal((await readState(temporary.path)).pendingTriggerDisable.triggerId, TRIGGER_ID);
    const state = await executeReleaseCommand([
      "recover", "--state", temporary.path,
      "--reason", "resume accepted trigger disable", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    assert.equal(state.phase, "recovered_legacy");
    assert.equal(state.pendingTriggerDisable, undefined);
    assert.equal(state.sourceTriggerControl.providerReportedSuccess, null);
    assert.equal(fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdDisableTrigger")).length, 1);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("externally disappeared trigger remains a valid observed-id control outcome", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway();
    await runPreflight(fake, temporary.path);
    await executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    fake.state.onCommand = async (argv, _options, commandState) => {
      if (argv[1] !== "api" || !argv[2].includes("DirectProdAuthority")
        || commandState.externalTriggerDisappearance) return;
      const journal = await readState(temporary.path);
      if (journal.pendingTriggerDisable?.triggerId === TRIGGER_ID) {
        commandState.externalTriggerDisappearance = true;
        commandState.trigger = false;
      }
    };
    const state = await executeReleaseCommand([
      "recover", "--state", temporary.path,
      "--reason", "observe external trigger disappearance", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    assert.equal(state.phase, "recovered_legacy");
    assert.equal(state.sourceTriggerControl.triggerId, TRIGGER_ID);
    assert.equal(state.sourceTriggerControl.triggerDeletionMayHaveBeenAttempted, false);
    assert.equal(state.sourceTriggerControl.providerReportedSuccess, null);
    assert.equal(fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdDisableTrigger")).length, 0);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("failed trigger disable remains armed and retries only on a recovery rerun", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway({
      triggerDeleteFailure: { stderr: "provider rejected trigger deletion" },
    });
    await runPreflight(fake, temporary.path);
    await executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    await assert.rejects(executeReleaseCommand([
      "recover", "--state", temporary.path,
      "--reason", "arm failed trigger disable", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
    /Railway production trigger disable failed/);
    const armed = await readState(temporary.path);
    assert.equal(armed.pendingTriggerDisable.triggerId, TRIGGER_ID);
    assert.equal(fake.state.trigger, true);
    assert.equal(fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdDisableTrigger")).length, 1);
    await assert.rejects(executeReleaseCommand([
      "promote-enforce", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
    /promote-enforce refuses an active production trigger-disable recovery/);
    fake.state.triggerDeleteFailure = null;
    const state = await executeReleaseCommand([
      "recover", "--state", temporary.path,
      "--reason", "retry armed trigger disable", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    assert.equal(state.phase, "recovered_legacy");
    assert.equal(fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdDisableTrigger")).length, 2);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("completed trigger containment blocks enforce promotion across a recovery crash", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway();
    await runPreflight(fake, temporary.path);
    await executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    const shadowState = JSON.parse(await readFile(temporary.path, "utf8"));
    const recovered = await executeReleaseCommand([
      "recover", "--state", temporary.path,
      "--reason", "capture completed containment evidence", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    shadowState.sourceTriggerControl = recovered.sourceTriggerControl;
    await writeFile(temporary.path, `${JSON.stringify(shadowState)}\n`, { mode: 0o600 });
    fake.state.trigger = true;
    fake.state.sourceRepo = GITHUB_SOURCE.repository;
    const commandOffset = fake.state.commands.length;

    await assert.rejects(executeReleaseCommand([
      "promote-enforce", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
    /promote-enforce refuses an active production trigger-disable recovery/);
    assert.equal(fake.state.commands.length, commandOffset);
    assert.equal((await readState(temporary.path)).phase, "shadow_validated");
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("trigger disable detects but never commits a concurrent staged-patch change", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway();
    await runPreflight(fake, temporary.path);
    await executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    fake.state.onCommand = async (argv, _options, commandState) => {
      if (argv[1] === "api" && argv[2].includes("DirectProdDisableTrigger")) {
        commandState.stagedPatch = { variables: { CONCURRENT_CHANGE: "keep-me" } };
        commandState.stagedPatchId = "concurrent-staged-patch";
      }
    };
    await assert.rejects(executeReleaseCommand([
      "recover", "--state", temporary.path,
      "--reason", "concurrent staged patch", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
    /staged patch changed during trigger-disable quiescence/);
    assert.deepEqual(fake.state.stagedPatch, {
      variables: { CONCURRENT_CHANGE: "keep-me" },
    });
    assert.equal(fake.state.trigger, false);
    assert.equal(fake.state.commands.some(({ argv }) => argv[1] === "api"
      && /environmentStageChanges|environmentPatchCommit/.test(argv[2])), false);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("trigger disable refuses a replacement trigger that appears during quiescence", async () => {
  const temporary = await tempState();
  let replaced = false;
  try {
    const fake = fakeRailway();
    await runPreflight(fake, temporary.path);
    await executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    await assert.rejects(executeReleaseCommand([
      "recover", "--state", temporary.path,
      "--reason", "replacement trigger safety test", "--yes",
    ], {
      run: fake.run,
      now: () => NOW,
      sleep: async () => {
        if (!replaced) {
          replaced = true;
          fake.state.trigger = true;
          fake.state.triggerId = "bbbbbbbb-cccc-4ddd-8eee-ffffffffffff";
        }
      },
    }), /trigger was replaced or reappeared/);
    assert.equal((await readState(temporary.path)).pendingTriggerDisable.triggerId, TRIGGER_ID);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("Boolean rollback waits for one delayed history row before binding", async () => {
  const temporary = await tempState();
  let sleeps = 0;
  try {
    const fake = fakeRailway({ rollbackHistoryDelayPolls: 1 });
    await runPreflight(fake, temporary.path);
    const state = await executeReleaseCommand([
      "recover", "--state", temporary.path, "--reason", "observe delayed rollback history",
      "--timeout-seconds", "60", "--poll-seconds", "30", "--yes",
    ], {
      run: fake.run,
      now: () => NOW,
      sleep: async () => { sleeps += 1; },
    });
    assert.equal(state.phase, "recovered_legacy");
    assert.equal(state.current.deploymentId, SHADOW_ID);
    assert.equal(sleeps, 1);
    assert.equal(fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdRollback")).length, 1);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("Boolean false leaves the rollback intent armed without redispatch", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway({ rollbackReturn: false });
    await runPreflight(fake, temporary.path);
    await assert.rejects(executeReleaseCommand([
      "recover", "--state", temporary.path, "--reason", "reject false rollback result", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }), /was not accepted/);
    assert.equal((await readState(temporary.path)).phase, "legacy_recovery_redeploy_armed");
    await assert.rejects(executeReleaseCommand([
      "recover", "--state", temporary.path, "--reason", "do not redispatch false result",
      "--timeout-seconds", "60", "--poll-seconds", "30", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }), /not yet observable/);
    assert.equal(fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdRollback")).length, 1);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

for (const [label, overrides, expectedError] of [
  ["wrong rollback reason", { rollbackReason: "redeploy" }, /not strictly marked as a rollback/],
  ["missing rollback reason", { rollbackReason: null }, /not strictly marked as a rollback/],
  ["wrong history digest", { rollbackDigest: `sha256:${"d".repeat(64)}` }, /history row does not match/],
  ["wrong exact digest", { rollbackExactDigest: `sha256:${"e".repeat(64)}` }, /target-, image-, snapshot-, or reason-drifted/],
  ["wrong snapshot", {
    rollbackSnapshotId: "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
  }, /target-, image-, snapshot-, or reason-drifted/],
  ["wrong target", { targetDriftDeploymentId: SHADOW_ID }, /not bound to the fixed target/],
]) {
  test(`rollback binding fails closed on ${label}`, async () => {
    const temporary = await tempState();
    try {
      const fake = fakeRailway(overrides);
      await runPreflight(fake, temporary.path);
      await assert.rejects(executeReleaseCommand([
        "recover", "--state", temporary.path, "--reason", `reject ${label}`, "--yes",
      ], { run: fake.run, now: () => NOW, sleep: async () => {} }), expectedError);
      assert.equal((await readState(temporary.path)).phase, "legacy_recovery_redeploy_armed");
      await assert.rejects(executeReleaseCommand([
        "recover", "--state", temporary.path, "--reason", `reconcile ${label}`, "--yes",
      ], { run: fake.run, now: () => NOW, sleep: async () => {} }), expectedError);
      assert.equal(fake.state.commands.filter(({ argv }) => argv[1] === "api"
        && argv[2].includes("DirectProdRollback")).length, 1);
    } finally {
      await rm(temporary.directory, { recursive: true, force: true });
    }
  });
}

for (const [label, overrides, expectedError] of [
  ["zero", { rollbackHistoryNeverVisible: true }, /not yet observable/],
  ["multiple", { rollbackAmbiguousHistory: true }, /ambiguous deployment-history delta/],
]) {
  test(`rollback binding retains its armed journal on ${label} history delta`, async () => {
    const temporary = await tempState();
    try {
      const fake = fakeRailway(overrides);
      await runPreflight(fake, temporary.path);
      await assert.rejects(executeReleaseCommand([
        "recover", "--state", temporary.path, "--reason", `reject ${label} history delta`,
        "--timeout-seconds", "60", "--poll-seconds", "30", "--yes",
      ], { run: fake.run, now: () => NOW, sleep: async () => {} }), expectedError);
      assert.equal((await readState(temporary.path)).phase, "legacy_recovery_redeploy_armed");
      await assert.rejects(executeReleaseCommand([
        "recover", "--state", temporary.path, "--reason", `reconcile ${label} history delta`,
        "--yes",
      ], { run: fake.run, now: () => NOW, sleep: async () => {} }), expectedError);
      assert.equal(fake.state.commands.filter(({ argv }) => argv[1] === "api"
        && argv[2].includes("DirectProdRollback")).length, 1);
    } finally {
      await rm(temporary.directory, { recursive: true, force: true });
    }
  });
}

test("post-deploy checks tolerate only the known retiring deployment until singleton convergence", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway({ overlapRemaining: 4 });
    await runPreflight(fake, temporary.path);
    await executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    await executeReleaseCommand([
      "promote-enforce", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    const state = await executeReleaseCommand([
      "recover", "--state", temporary.path, "--reason", "exercise active overlap", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    assert.equal(state.phase, "recovered_same_commit_shadow");
    assert.equal(fake.state.overlapRemaining, 0);
    assert.deepEqual(fake.state.activePollQueue, []);
    assert.equal(fake.state.active.length, 1);
    assert.equal(fake.state.active[0].id, RECOVERY_ID);
    assert.equal(fake.state.sourceRepo, GITHUB_SOURCE.repository);
    assert.equal(fake.state.trigger, false);
    assert.equal(state.recovery.githubTriggerDisabled, true);
    assert.equal(state.recovery.sourceMetadataRetained, true);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("post-enforce recovery can only roll back to same-commit shadow", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway();
    await runPreflight(fake, temporary.path);
    await executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    await executeReleaseCommand([
      "promote-enforce", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    const before = fake.state.commands.length;
    const state = await executeReleaseCommand([
      "recover", "--state", temporary.path, "--reason", "payment outcome must remain durable", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    assert.equal(state.phase, "recovered_same_commit_shadow");
    assert.equal(state.current.imageDigest, CANDIDATE_DIGEST);
    assert.equal(fake.state.variables.X402_PAYMENT_MODE, "shadow");
    const recent = fake.state.commands.slice(before);
    const rollback = recent.find(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdRollback"));
    assert(rollback.argv.includes(`id=${SHADOW_ID}`));
    assert.equal(rollback.argv.includes(`id=${LEGACY.deploymentId}`), false);
    const rollbackIndex = recent.indexOf(rollback);
    const recoveryPhaseIndex = recent.findIndex(({ argv, options }) => (
      argv[1] === "variable" && argv[2] === "set" && argv[3] === "RELEASE_IDENTITY_PHASE"
      && String(options.stdin || "").trim() === "bound"
    ));
    assert(recoveryPhaseIndex >= 0 && recoveryPhaseIndex < rollbackIndex);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("recovery reconciles an accepted shadow upload from its unique commit marker", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway();
    await runPreflight(fake, temporary.path);
    fake.state.crashAfterAccepted = "upload";
    await assert.rejects(executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
    /simulated crash after accepted upload/);
    assert.equal((await readState(temporary.path)).phase, "github_source_connect_armed");
    const state = await executeReleaseCommand([
      "recover", "--state", temporary.path, "--reason", "upload response was interrupted", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    assert.equal(state.phase, "recovered_legacy");
    assert.equal(state.lastAcceptedUpload.deploymentId, CANDIDATE_ID);
    assert.equal(state.lastAcceptedUpload.reconciledAfterCrash, true);
    assert.equal(fake.state.commands.filter(({ argv }) => isSourceConnect(argv)).length, 1);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("recovery reconciles an accepted identity-bound shadow redeploy", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway();
    await runPreflight(fake, temporary.path);
    fake.state.crashAfterAccepted = "redeploy";
    await assert.rejects(executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
    /simulated crash after accepted exact redeploy/);
    assert.equal((await readState(temporary.path)).phase, "shadow_identity_redeploy_armed");
    const state = await executeReleaseCommand([
      "recover", "--state", temporary.path, "--reason", "shadow bind response was interrupted", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    assert.equal(state.phase, "recovered_legacy");
    assert.equal(state.lastCrashReconciliation.purpose, "shadow_identity");
    assert.equal(state.lastCrashReconciliation.deploymentId, SHADOW_ID);
    assert.equal(state.current.imageDigest, LEGACY.imageDigest);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("recovery reconciles an accepted enforce redeploy without reopening legacy rollback", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway();
    await runPreflight(fake, temporary.path);
    await executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    fake.state.crashAfterAccepted = "redeploy";
    await assert.rejects(executeReleaseCommand([
      "promote-enforce", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
    /simulated crash after accepted exact redeploy/);
    const armed = await readState(temporary.path);
    assert.equal(armed.phase, "enforce_redeploy_armed");
    assert.equal(armed.rollbackBoundary, "same_commit_shadow_only");
    const state = await executeReleaseCommand([
      "recover", "--state", temporary.path, "--reason", "enforce response was interrupted", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    assert.equal(state.phase, "recovered_same_commit_shadow");
    assert.equal(state.lastCrashReconciliation.purpose, "enforce");
    assert.equal(state.lastCrashReconciliation.deploymentId, ENFORCE_ID);
    assert.equal(state.current.imageDigest, CANDIDATE_DIGEST);
    const rollback = fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdRollback")).at(-1);
    assert(rollback.argv.includes(`id=${SHADOW_ID}`));
    assert.equal(rollback.argv.includes(`id=${LEGACY.deploymentId}`), false);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("recovery adopts its accepted legacy rollback instead of dispatching twice", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway();
    await runPreflight(fake, temporary.path);
    fake.state.crashAfterAccepted = "rollback";
    await assert.rejects(executeReleaseCommand([
      "recover", "--state", temporary.path, "--reason", "begin crash-safe rollback", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
    /simulated crash after accepted exact rollback/);
    assert.equal((await readState(temporary.path)).phase, "legacy_recovery_redeploy_armed");
    const state = await executeReleaseCommand([
      "recover", "--state", temporary.path, "--reason", "resume crash-safe rollback", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    assert.equal(state.phase, "recovered_legacy");
    assert.equal(state.lastCrashReconciliation.purpose, "legacy_recovery");
    assert.equal(state.current.deploymentId, SHADOW_ID);
    assert.equal(fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdRollback")).length, 1);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("recovery adopts its accepted same-commit shadow rollback without legacy fallback", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway();
    await runPreflight(fake, temporary.path);
    await executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    await executeReleaseCommand([
      "promote-enforce", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    fake.state.crashAfterAccepted = "rollback";
    await assert.rejects(executeReleaseCommand([
      "recover", "--state", temporary.path, "--reason", "begin same-commit recovery", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
    /simulated crash after accepted exact rollback/);
    assert.equal((await readState(temporary.path)).phase,
      "same_commit_shadow_recovery_redeploy_armed");
    const state = await executeReleaseCommand([
      "recover", "--state", temporary.path, "--reason", "resume same-commit recovery", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    assert.equal(state.phase, "recovered_same_commit_shadow");
    assert.equal(state.lastCrashReconciliation.purpose, "same_commit_shadow_recovery");
    assert.equal(state.current.deploymentId, RECOVERY_ID);
    assert.equal(state.current.imageDigest, CANDIDATE_DIGEST);
    const rollbacks = fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdRollback"));
    assert.equal(rollbacks.length, 1);
    assert(rollbacks.at(-1).argv.includes(`id=${SHADOW_ID}`));
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("every terminal bound recovery attempt is journaled before one fresh exact retry", async () => {
  for (const terminalStatus of [
    "FAILED", "CRASHED", "REMOVED", "SKIPPED", "CANCELED", "CANCELLED",
  ]) {
    const temporary = await tempState();
    try {
      const fake = fakeRailway();
      await runPreflight(fake, temporary.path);
      fake.state.nextRedeployStatus = terminalStatus;
      await assert.rejects(executeReleaseCommand([
        "recover", "--state", temporary.path, "--reason", "first recovery attempt", "--yes",
      ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
      new RegExp(`failed with ${terminalStatus}`));
      assert.equal((await readState(temporary.path)).phase, "recovery_deployment_bound");
      const state = await executeReleaseCommand([
        "recover", "--state", temporary.path, "--reason", "retry terminal recovery", "--yes",
      ], { run: fake.run, now: () => NOW, sleep: async () => {} });
      assert.equal(state.phase, "recovered_legacy");
      assert.equal(state.current.deploymentId, ENFORCE_ID);
      assert.equal(state.recovery.failedAttempts.length, 1);
      assert.equal(state.recovery.failedAttempts[0].status, terminalStatus);
      assert.equal(fake.state.commands.filter(({ argv }) => argv[1] === "api"
        && argv[2].includes("DirectProdRollback")).length, 2);
    } finally {
      await rm(temporary.directory, { recursive: true, force: true });
    }
  }
});

test("terminal pending same-commit recovery is reconciled and retried only as shadow", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway();
    await runPreflight(fake, temporary.path);
    await executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    await executeReleaseCommand([
      "promote-enforce", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    fake.state.nextRedeployStatus = "CRASHED";
    fake.state.crashAfterAccepted = "rollback";
    await assert.rejects(executeReleaseCommand([
      "recover", "--state", temporary.path, "--reason", "first shadow recovery", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
    /simulated crash after accepted exact rollback/);
    assert.equal((await readState(temporary.path)).phase,
      "same_commit_shadow_recovery_redeploy_armed");
    const state = await executeReleaseCommand([
      "recover", "--state", temporary.path, "--reason", "retry crashed shadow recovery", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    assert.equal(state.phase, "recovered_same_commit_shadow");
    assert.equal(state.current.deploymentId, RETRY_ID);
    assert.equal(state.recovery.failedAttempts[0].deploymentId, RECOVERY_ID);
    assert.equal(state.recovery.failedAttempts[0].status, "CRASHED");
    const rollbacks = fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdRollback"));
    assert.equal(rollbacks.length, 2);
    assert(rollbacks.every(({ argv }) => argv.includes(`id=${SHADOW_ID}`)));
    assert.equal(rollbacks.some(({ argv }) => argv.includes(
      `id=${LEGACY.deploymentId}`,
    )), false);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("terminal bound same-commit recovery retries only after exact inactive failure", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway();
    await runPreflight(fake, temporary.path);
    await executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    await executeReleaseCommand([
      "promote-enforce", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    fake.state.nextRedeployStatus = "FAILED";
    await assert.rejects(executeReleaseCommand([
      "recover", "--state", temporary.path, "--reason", "first bound shadow recovery", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }), /failed with FAILED/);
    const state = await executeReleaseCommand([
      "recover", "--state", temporary.path, "--reason", "retry failed bound recovery", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    assert.equal(state.phase, "recovered_same_commit_shadow");
    assert.equal(state.current.deploymentId, RETRY_ID);
    assert.equal(state.recovery.failedAttempts[0].deploymentId, RECOVERY_ID);
    assert.equal(state.recovery.failedAttempts[0].status, "FAILED");
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("exact stopped terminal-active recovery is journaled before strict retry", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway({ keepTerminalDeploymentActive: true });
    await runPreflight(fake, temporary.path);
    fake.state.nextRedeployStatus = "FAILED";
    await assert.rejects(executeReleaseCommand([
      "recover", "--state", temporary.path, "--reason", "first failed recovery", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }), /failed with FAILED/);
    const state = await executeReleaseCommand([
      "recover", "--state", temporary.path, "--reason", "retry exact stopped failure", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    assert.equal(state.phase, "recovered_legacy");
    assert.equal(state.recovery.failedAttempts[0].status, "FAILED");
    assert.equal(state.recovery.failedAttempts[0].deploymentId, SHADOW_ID);
    assert.equal(fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdRollback")).length, 2);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("terminal recovery retry refuses an ambiguous active target", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway();
    await runPreflight(fake, temporary.path);
    fake.state.nextRedeployStatus = "FAILED";
    await assert.rejects(executeReleaseCommand([
      "recover", "--state", temporary.path, "--reason", "first failed recovery", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }), /failed with FAILED/);
    fake.state.active.push(deployment(CANDIDATE_ID, CANDIDATE_DIGEST));
    const before = fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdRollback")).length;
    await assert.rejects(executeReleaseCommand([
      "recover", "--state", temporary.path, "--reason", "unsafe retry refused", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }), /ambiguous multi-active target/);
    assert.equal(fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdRollback")).length, before);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("SUCCESS without a running instance is journaled and retried for bound and pending recovery", async () => {
  for (const pendingCrashWindow of [false, true]) {
    const temporary = await tempState();
    try {
      const fake = fakeRailway({ keepUnhealthyDeploymentActive: pendingCrashWindow });
      await runPreflight(fake, temporary.path);
      fake.state.nextRedeployUnhealthySuccess = true;
      if (pendingCrashWindow) fake.state.crashAfterAccepted = "rollback";
      await assert.rejects(executeReleaseCommand([
        "recover", "--state", temporary.path, "--reason", "first unhealthy recovery", "--yes",
      ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
      pendingCrashWindow
        ? /simulated crash after accepted exact rollback/
        : /successful deployment has no single running instance/);
      assert.equal((await readState(temporary.path)).phase,
        pendingCrashWindow ? "legacy_recovery_redeploy_armed" : "recovery_deployment_bound");
      const state = await executeReleaseCommand([
        "recover", "--state", temporary.path, "--reason", "retry unhealthy recovery", "--yes",
      ], { run: fake.run, now: () => NOW, sleep: async () => {} });
      assert.equal(state.phase, "recovered_legacy");
      assert.equal(state.current.deploymentId, ENFORCE_ID);
      assert.equal(state.recovery.failedAttempts.length, 1);
      assert.equal(state.recovery.failedAttempts[0].status, "SUCCESS_UNHEALTHY");
      assert.equal(state.recovery.failedAttempts[0].providerStatus, "SUCCESS");
      assert.equal(fake.state.commands.filter(({ argv }) => argv[1] === "api"
        && argv[2].includes("DirectProdRollback")).length, 2);
    } finally {
      await rm(temporary.directory, { recursive: true, force: true });
    }
  }
});

test("unhealthy-success recovery retry refuses any still-running failed attempt", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway({ keepUnhealthyDeploymentActive: true });
    await runPreflight(fake, temporary.path);
    fake.state.nextRedeployUnhealthySuccess = true;
    await assert.rejects(executeReleaseCommand([
      "recover", "--state", temporary.path, "--reason", "first unhealthy recovery", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
    /successful deployment has no single running instance/);
    fake.state.active[0].instances = [
      { id: "running-one", status: "RUNNING" },
      { id: "running-two", status: "RUNNING" },
    ];
    const before = fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdRollback")).length;
    await assert.rejects(executeReleaseCommand([
      "recover", "--state", temporary.path, "--reason", "running attempt must refuse", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
    /active outcome is not the exact unhealthy deployment/);
    assert.equal(fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdRollback")).length, before);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("every accepted pending mutation reconciles an exact stopped terminal-active outcome", async () => {
  const cases = [
    {
      kind: "upload",
      armedPhase: "github_source_connect_armed",
      finalPhase: "recovered_legacy",
      finalDeploymentId: SHADOW_ID,
      rollbackSource: LEGACY.deploymentId,
    },
    {
      kind: "shadow_identity",
      armedPhase: "shadow_identity_redeploy_armed",
      finalPhase: "recovered_legacy",
      finalDeploymentId: ENFORCE_ID,
      rollbackSource: LEGACY.deploymentId,
    },
    {
      kind: "enforce",
      armedPhase: "enforce_redeploy_armed",
      finalPhase: "recovered_same_commit_shadow",
      finalDeploymentId: RECOVERY_ID,
      rollbackSource: SHADOW_ID,
    },
    {
      kind: "recovery",
      armedPhase: "legacy_recovery_redeploy_armed",
      finalPhase: "recovered_legacy",
      finalDeploymentId: ENFORCE_ID,
      rollbackSource: LEGACY.deploymentId,
    },
  ];
  for (const expected of cases) {
    const temporary = await tempState();
    try {
      const fake = fakeRailway();
      await runPreflight(fake, temporary.path);
      if (expected.kind === "enforce") {
        await executeReleaseCommand([
          "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
        ], { run: fake.run, now: () => NOW, sleep: async () => {} });
      }
      if (expected.kind === "upload") {
        fake.state.nextUploadStatus = "CRASHED";
        fake.state.keepUploadTerminalActive = true;
        fake.state.crashAfterAccepted = "upload";
      } else {
        fake.state.nextRedeployStatus = "CRASHED";
        fake.state.keepTerminalDeploymentActive = true;
        fake.state.crashAfterAccepted = expected.kind === "recovery" ? "rollback" : "redeploy";
      }
      const command = expected.kind === "enforce"
        ? ["promote-enforce", "--state", temporary.path, "--commit", COMMIT, "--yes"]
        : expected.kind === "recovery"
          ? ["recover", "--state", temporary.path, "--reason", "first terminal recovery", "--yes"]
          : ["deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes"];
      await assert.rejects(executeReleaseCommand(command, {
        run: fake.run,
        now: () => NOW,
        sleep: async () => {},
      }), /simulated crash after accepted/);
      assert.equal((await readState(temporary.path)).phase, expected.armedPhase);
      const state = await executeReleaseCommand([
        "recover", "--state", temporary.path, "--reason", "reconcile terminal active", "--yes",
      ], { run: fake.run, now: () => NOW, sleep: async () => {} });
      assert.equal(state.phase, expected.finalPhase);
      assert.equal(state.current.deploymentId, expected.finalDeploymentId);
      const failure = expected.kind === "recovery"
        ? state.recovery.failedAttempts[0]
        : state.lastFailedMutation;
      assert.equal(failure.status, "CRASHED");
      if (expected.kind === "upload") assert.equal(failure.kind, "github_source");
      else assert.equal(failure.purpose, expected.kind === "recovery"
        ? "legacy_recovery"
        : expected.kind);
      const rollback = fake.state.commands.filter(({ argv }) => argv[1] === "api"
        && argv[2].includes("DirectProdRollback")).at(-1);
      assert(rollback.argv.includes(`id=${expected.rollbackSource}`));
    } finally {
      await rm(temporary.directory, { recursive: true, force: true });
    }
  }
});

test("terminal upload build failures without an image reconcile inactive and stopped-active outcomes", async () => {
  for (const keepFailedUploadActive of [false, true]) {
    const temporary = await tempState();
    try {
      const fake = fakeRailway();
      await runPreflight(fake, temporary.path);
      fake.state.nextUploadStatus = keepFailedUploadActive ? "CRASHED" : "FAILED";
      fake.state.nextUploadNoDigest = true;
      fake.state.keepUploadTerminalActive = keepFailedUploadActive;
      fake.state.crashAfterAccepted = "upload";
      await assert.rejects(executeReleaseCommand([
        "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
      ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
      /simulated crash after accepted upload/);
      assert.equal((await readState(temporary.path)).phase, "github_source_connect_armed");
      const state = await executeReleaseCommand([
        "recover", "--state", temporary.path, "--reason", "recover no-image build failure", "--yes",
      ], { run: fake.run, now: () => NOW, sleep: async () => {} });
      assert.equal(state.phase, "recovered_legacy");
      assert.equal(state.current.mode, "legacy");
      assert.equal(state.current.imageDigest, LEGACY.imageDigest);
      assert.equal(state.lastFailedMutation.kind, "github_source");
      assert.equal(state.lastFailedMutation.expectedImageDigest, null);
      assert.equal(state.lastFailedMutation.status,
        keepFailedUploadActive ? "CRASHED" : "FAILED");
      const rollback = fake.state.commands.filter(({ argv }) => argv[1] === "api"
        && argv[2].includes("DirectProdRollback")).at(-1);
      if (keepFailedUploadActive) {
        assert(rollback.argv.includes(`id=${LEGACY.deploymentId}`));
      } else {
        assert.equal(rollback, undefined);
        assert.equal(state.recovery.rollbackPerformed, false);
        assert.equal(state.recovery.githubTriggerDisabled, true);
      }
    } finally {
      await rm(temporary.directory, { recursive: true, force: true });
    }
  }
});

test("pending upload, shadow-identity, and enforce accept only exact stopped unhealthy SUCCESS", async () => {
  for (const expected of [
    {
      kind: "upload",
      finalPhase: "recovered_legacy",
      finalDeploymentId: SHADOW_ID,
      rollbackSource: LEGACY.deploymentId,
    },
    {
      kind: "shadow_identity",
      finalPhase: "recovered_legacy",
      finalDeploymentId: ENFORCE_ID,
      rollbackSource: LEGACY.deploymentId,
    },
    {
      kind: "enforce",
      finalPhase: "recovered_same_commit_shadow",
      finalDeploymentId: RECOVERY_ID,
      rollbackSource: SHADOW_ID,
    },
  ]) {
    const temporary = await tempState();
    try {
      const fake = fakeRailway();
      await runPreflight(fake, temporary.path);
      if (expected.kind === "enforce") {
        await executeReleaseCommand([
          "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
        ], { run: fake.run, now: () => NOW, sleep: async () => {} });
      }
      if (expected.kind === "upload") {
        fake.state.nextUploadUnhealthySuccess = true;
        fake.state.keepUploadUnhealthyActive = true;
        fake.state.crashAfterAccepted = "upload";
      } else {
        fake.state.nextRedeployUnhealthySuccess = true;
        fake.state.keepUnhealthyDeploymentActive = true;
        fake.state.crashAfterAccepted = "redeploy";
      }
      const command = expected.kind === "enforce"
        ? ["promote-enforce", "--state", temporary.path, "--commit", COMMIT, "--yes"]
        : ["deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes"];
      await assert.rejects(executeReleaseCommand(command, {
        run: fake.run,
        now: () => NOW,
        sleep: async () => {},
      }), /simulated crash after accepted/);
      const state = await executeReleaseCommand([
        "recover", "--state", temporary.path, "--reason", "reconcile stopped success", "--yes",
      ], { run: fake.run, now: () => NOW, sleep: async () => {} });
      assert.equal(state.phase, expected.finalPhase);
      assert.equal(state.current.deploymentId, expected.finalDeploymentId);
      assert.equal(state.lastFailedMutation.status, "SUCCESS_UNHEALTHY");
      assert.equal(state.lastFailedMutation.providerStatus, "SUCCESS");
      if (expected.kind === "upload") assert.equal(state.lastFailedMutation.kind, "github_source");
      else assert.equal(state.lastFailedMutation.purpose, expected.kind);
      const rollback = fake.state.commands.filter(({ argv }) => argv[1] === "api"
        && argv[2].includes("DirectProdRollback")).at(-1);
      assert(rollback.argv.includes(`id=${expected.rollbackSource}`));
    } finally {
      await rm(temporary.directory, { recursive: true, force: true });
    }
  }
});

test("same-commit shadow recovery can replace one known unhealthy enforce active", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway();
    await runPreflight(fake, temporary.path);
    await executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    await executeReleaseCommand([
      "promote-enforce", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    fake.state.active = [deployment(ENFORCE_ID, CANDIDATE_DIGEST, "CRASHED")];
    const state = await executeReleaseCommand([
      "recover", "--state", temporary.path, "--reason", "enforce instance is unhealthy", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    assert.equal(state.phase, "recovered_same_commit_shadow");
    assert.equal(state.current.deploymentId, RECOVERY_ID);
    assert.equal(state.current.imageDigest, CANDIDATE_DIGEST);
    const rollback = fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdRollback")).at(-1);
    assert(rollback.argv.includes(`id=${SHADOW_ID}`));
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("recovery can redeploy its exact reviewed source when no deployment is active", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway();
    await runPreflight(fake, temporary.path);
    fake.state.active = [];
    const state = await executeReleaseCommand([
      "recover", "--state", temporary.path, "--reason", "production has no active deployment", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    assert.equal(state.phase, "recovered_legacy");
    assert.equal(state.current.deploymentId, SHADOW_ID);
    assert.equal(state.current.imageDigest, LEGACY.imageDigest);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("recovery refuses unknown, multiple, and target-drifted active deployments", async () => {
  const cases = [
    [[deployment(CANDIDATE_ID, CANDIDATE_DIGEST)], /unknown active deployment/],
    [[
      deployment(LEGACY.deploymentId, LEGACY.imageDigest),
      deployment(CANDIDATE_ID, CANDIDATE_DIGEST),
    ], /ambiguous multi-active target/],
    [[{
      ...deployment(LEGACY.deploymentId, LEGACY.imageDigest),
      projectId: "00000000-0000-4000-8000-000000000000",
    }], /not bound to the fixed target/],
  ];
  for (const [active, expectedError] of cases) {
    const temporary = await tempState();
    try {
      const fake = fakeRailway();
      await runPreflight(fake, temporary.path);
      fake.state.active = active;
      await assert.rejects(executeReleaseCommand([
        "recover", "--state", temporary.path, "--reason", "unsafe active target", "--yes",
      ], { run: fake.run, now: () => NOW, sleep: async () => {} }), expectedError);
    } finally {
      await rm(temporary.directory, { recursive: true, force: true });
    }
  }
});

test("armed source mutation disables authority for a zero-deploy outcome and rejects ambiguity", async () => {
  for (const ambiguous of [false, true]) {
    const temporary = await tempState();
    try {
      const fake = fakeRailway({
        onCommand: async (argv, options, state) => {
          if (isSourceConnect(argv) && !state.crashedBeforeUpload) {
            state.crashedBeforeUpload = true;
            if (ambiguous) {
              state.history.unshift({
                id: CANDIDATE_ID,
                status: "SUCCESS",
                createdAt: new Date(NOW).toISOString(),
                meta: { imageDigest: CANDIDATE_DIGEST, cliMessage: "unrelated" },
              });
              state.history.unshift({
                id: SHADOW_ID,
                status: "SUCCESS",
                createdAt: new Date(NOW).toISOString(),
                meta: { imageDigest: CANDIDATE_DIGEST, cliMessage: "also-unrelated" },
              });
              state.active = [deployment(CANDIDATE_ID, CANDIDATE_DIGEST)];
            }
            throw new Error("simulated crash before upload result");
          }
        },
      });
      await runPreflight(fake, temporary.path);
      await assert.rejects(executeReleaseCommand([
        "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
      ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
      /simulated crash before upload result/);
      const upCalls = fake.state.commands.filter(({ argv }) => isSourceConnect(argv)).length;
      const recovery = executeReleaseCommand([
        "recover", "--state", temporary.path, "--reason", "ambiguous upload outcome", "--yes",
      ], { run: fake.run, now: () => NOW, sleep: async () => {} });
      if (ambiguous) {
        await assert.rejects(recovery, /ambiguous deployment-history delta/);
      } else {
        const state = await recovery;
        assert.equal(state.phase, "recovered_legacy");
        assert.equal(state.recovery.rollbackPerformed, false);
        assert.equal(state.recovery.githubTriggerDisabled, true);
        assert.equal(state.recovery.sourceMetadataRetained, false);
      }
      assert.equal(fake.state.commands.filter(({ argv }) => isSourceConnect(argv)).length, upCalls);
    } finally {
      await rm(temporary.directory, { recursive: true, force: true });
    }
  }
});

test("source recovery catches an accepted deployment that appears during quiescence", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway({
      onCommand: async (argv, options, state) => {
        if (isSourceConnect(argv) && !state.delayedSourceAccepted) {
          state.delayedSourceAccepted = true;
          acceptDelayedSourceMutation(state, 2);
          throw new Error("simulated accepted source with delayed history");
        }
      },
    });
    await runPreflight(fake, temporary.path);
    await assert.rejects(executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }), /delayed history/);
    const state = await executeReleaseCommand([
      "recover", "--state", temporary.path, "--reason", "reconcile delayed source deploy", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    assert.equal(state.phase, "recovered_legacy");
    assert.equal(state.lastAcceptedUpload.deploymentId, CANDIDATE_ID);
    assert.equal(state.recovery.githubTriggerDisabled, true);
    assert.equal(fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdRollback")).length, 1);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("source recovery establishes durable absence before binding an accepted zero-trigger deployment", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway({ crashAfterAccepted: "upload" });
    await runPreflight(fake, temporary.path);
    await assert.rejects(executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
    /crash after accepted upload/);
    fake.state.trigger = false;
    let interruptedAbsence = false;
    await assert.rejects(executeReleaseCommand([
      "recover", "--state", temporary.path,
      "--reason", "journal accepted zero-trigger source", "--poll-seconds", "30", "--yes",
    ], {
      run: fake.run,
      now: () => NOW,
      sleep: async () => {
        const journal = await readState(temporary.path);
        if (!interruptedAbsence && journal.pendingTriggerDisable?.triggerId == null) {
          interruptedAbsence = true;
          throw new Error("simulated crash during accepted-source absence quiescence");
        }
      },
    }), /crash during accepted-source absence quiescence/);
    const armed = await readState(temporary.path);
    assert.equal(armed.pendingTriggerDisable.triggerId, null);
    assert.equal(armed.pendingTriggerDisable.baselineDeploymentIds.includes(CANDIDATE_ID), true);

    const state = await executeReleaseCommand([
      "recover", "--state", temporary.path,
      "--reason", "resume accepted zero-trigger source", "--poll-seconds", "30", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    assert.equal(state.phase, "recovered_legacy");
    assert.equal(state.lastAcceptedUpload.deploymentId, CANDIDATE_ID);
    assert.equal(state.sourceTriggerControl.triggerId, null);
    assert.equal(state.pendingTriggerDisable, undefined);
    assert.equal(fake.state.commands.filter(({ argv }) => isSourceConnect(argv)).length, 1);
    assert.equal(fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdDisableTrigger")).length, 0);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("source recovery supersedes stale absence evidence when exact metadata appears", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway();
    await leaveSourceArmedWithCompletedAbsenceControl(fake, temporary.path);
    acceptDelayedSourceMutation(fake.state, 0);
    fake.state.trigger = false;

    const state = await executeReleaseCommand([
      "recover", "--state", temporary.path,
      "--reason", "supersede stale metadata absence", "--poll-seconds", "30", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    assert.equal(state.phase, "recovered_legacy");
    assert.equal(state.lastAcceptedUpload.deploymentId, CANDIDATE_ID);
    assert.equal(state.sourceTriggerControl.triggerId, null);
    assert.equal(state.sourceTriggerControl.sourceMetadataRetained, true);
    assert.equal(fake.state.commands.filter(({ argv }) => isSourceConnect(argv)).length, 1);
    assert.equal(fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdDisableTrigger")).length, 0);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("source recovery supersedes stale absence evidence and deletes a revealed exact trigger", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway();
    await leaveSourceArmedWithCompletedAbsenceControl(fake, temporary.path);
    acceptDelayedSourceMutation(fake.state, 0);

    const state = await executeReleaseCommand([
      "recover", "--state", temporary.path,
      "--reason", "supersede stale trigger absence", "--poll-seconds", "30", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    assert.equal(state.phase, "recovered_legacy");
    assert.equal(state.lastAcceptedUpload.deploymentId, CANDIDATE_ID);
    assert.equal(state.sourceTriggerControl.triggerId, TRIGGER_ID);
    assert.equal(fake.state.trigger, false);
    assert.equal(fake.state.commands.filter(({ argv }) => isSourceConnect(argv)).length, 1);
    assert.equal(fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdDisableTrigger")).length, 1);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("zero-deploy recovery freshly re-quiesces completed absence evidence", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway();
    await leaveSourceArmedWithCompletedAbsenceControl(fake, temporary.path);
    let interruptedFreshControl = false;
    await assert.rejects(executeReleaseCommand([
      "recover", "--state", temporary.path,
      "--reason", "refresh completed absence evidence", "--poll-seconds", "30", "--yes",
    ], {
      run: fake.run,
      now: () => NOW,
      sleep: async () => {
        const journal = await readState(temporary.path);
        if (!interruptedFreshControl && journal.pendingTriggerDisable?.triggerId == null
          && journal.sourceTriggerControl == null) {
          interruptedFreshControl = true;
          throw new Error("simulated crash during fresh absence control");
        }
      },
    }), /crash during fresh absence control/);
    const armed = await readState(temporary.path);
    assert.equal(armed.phase, "github_source_connect_armed");
    assert.equal(armed.pendingTriggerDisable.triggerId, null);
    assert.equal(armed.sourceTriggerControl, undefined);

    const state = await executeReleaseCommand([
      "recover", "--state", temporary.path,
      "--reason", "complete fresh absence control", "--poll-seconds", "30", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    assert.equal(state.phase, "recovered_legacy");
    assert.equal(fake.state.commands.filter(({ argv }) => isSourceConnect(argv)).length, 1);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("source recovery never completes around a newly visible in-flight source deployment", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway({
      onCommand: async (argv, _options, commandState) => {
        if (isSourceConnect(argv) && !commandState.crashedBeforeSourceResult) {
          commandState.crashedBeforeSourceResult = true;
          commandState.sourceRepo = GITHUB_SOURCE.repository;
          commandState.trigger = true;
          throw new Error("simulated source connection before deployment visibility");
        }
        if (argv[1] === "api" && argv[2].includes("DirectProdDisableTrigger")
          && !commandState.inFlightSourceQueued) {
          commandState.inFlightSourceQueued = true;
          commandState.pendingHistoryRows = [{
            id: CANDIDATE_ID,
            status: "BUILDING",
            createdAt: new Date(NOW).toISOString(),
            meta: {
              imageDigest: null,
              repo: GITHUB_SOURCE.repository,
              branch: GITHUB_SOURCE.branch,
              commitHash: COMMIT,
            },
          }];
          commandState.historyVisibilityPollsRemaining = 0;
        }
      },
    });
    await runPreflight(fake, temporary.path);
    await assert.rejects(executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
    /before deployment visibility/);
    await assert.rejects(executeReleaseCommand([
      "recover", "--state", temporary.path,
      "--reason", "contain late in-flight source", "--poll-seconds", "30", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
    /late source deployment still in flight/);
    const state = await readState(temporary.path);
    assert.equal(state.phase, "github_source_connect_armed");
    assert.equal(state.pendingTriggerDisable.triggerId, TRIGGER_ID);
    assert.equal(fake.state.commands.filter(({ argv }) => isSourceConnect(argv)).length, 1);
    assert.equal(fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdDisableTrigger")).length, 1);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("zero-deploy recovery rejects an in-flight row captured inside the containment baseline", async () => {
  const temporary = await tempState();
  let deploymentListCalls = 0;
  try {
    const fake = fakeRailway({
      onCommand: async (argv, _options, commandState) => {
        if (argv[1] === "deployment" && argv[2] === "list") deploymentListCalls += 1;
        if (isSourceConnect(argv) && !commandState.crashedBeforeSourceResult) {
          commandState.crashedBeforeSourceResult = true;
          commandState.sourceRepo = GITHUB_SOURCE.repository;
          commandState.trigger = true;
          throw new Error("simulated source connection before baseline-gap row");
        }
        if (argv[1] === "api" && argv[2].includes("DirectProdAuthority")
          && deploymentListCalls >= 4 && !commandState.baselineGapRowVisible) {
          commandState.baselineGapRowVisible = true;
          commandState.history.unshift({
            id: CANDIDATE_ID,
            status: "BUILDING",
            createdAt: new Date(NOW).toISOString(),
            meta: {
              imageDigest: null,
              repo: GITHUB_SOURCE.repository,
              branch: GITHUB_SOURCE.branch,
              commitHash: COMMIT,
            },
          });
        }
      },
    });
    await runPreflight(fake, temporary.path);
    await assert.rejects(executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
    /before baseline-gap row/);
    deploymentListCalls = 0;
    await assert.rejects(executeReleaseCommand([
      "recover", "--state", temporary.path,
      "--reason", "fence containment baseline gap", "--poll-seconds", "30", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
    /source-connected deployment appeared after the zero-deploy observation/);
    const state = await readState(temporary.path);
    assert.equal(state.phase, "github_source_connect_armed");
    assert.equal(state.sourceTriggerControl.githubTriggerDisabled, true);
    assert.equal(fake.state.commands.filter(({ argv }) => isSourceConnect(argv)).length, 1);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("source recovery refuses a deployment that appears only after trigger disable", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway({
      onCommand: async (argv, options, state) => {
        if (isSourceConnect(argv) && !state.delayedSourceAccepted) {
          state.delayedSourceAccepted = true;
          acceptDelayedSourceMutation(state, 14);
          throw new Error("simulated very-late source deployment");
        }
      },
    });
    await runPreflight(fake, temporary.path);
    await assert.rejects(executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }), /very-late/);
    await assert.rejects(executeReleaseCommand([
      "recover", "--state", temporary.path, "--reason", "observe post-disable quiescence", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
    /source-connected deployment appeared after the zero-deploy observation|late source-connected deployment|unhandled late source deployment|legacy production changed during source-connect recovery quiescence/);
    assert.equal((await readState(temporary.path)).phase, "github_source_connect_armed");
    assert.equal(fake.state.sourceRepo, GITHUB_SOURCE.repository);
    const state = await executeReleaseCommand([
      "recover", "--state", temporary.path,
      "--reason", "reconcile post-disable deployment", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    assert.equal(state.phase, "recovered_legacy");
    assert.equal(state.lastAcceptedUpload.deploymentId, CANDIDATE_ID);
    assert.equal(fake.state.trigger, false);
    assert.equal(fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdDisableTrigger")).length, 1);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("source recovery observes the full 60-second post-disable window at a 30-second poll", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway({
      onCommand: async (argv, options, state) => {
        if (isSourceConnect(argv) && !state.crashedBeforeSourceResult) {
          state.crashedBeforeSourceResult = true;
          state.sourceRepo = GITHUB_SOURCE.repository;
          state.trigger = true;
          throw new Error("simulated source connection with no immediate deployment");
        }
        if (argv[1] === "api" && argv[2].includes("DirectProdDisableTrigger")
          && !state.lateSourceAccepted) {
          state.lateSourceAccepted = true;
          // The fake reveals this on the third observation: t=0, t=30, t=60.
          acceptDelayedSourceMutation(state, 2);
        }
      },
    });
    await runPreflight(fake, temporary.path);
    await assert.rejects(executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }), /no immediate deployment/);
    await assert.rejects(executeReleaseCommand([
      "recover", "--state", temporary.path, "--reason", "full quiescence window", "--yes",
      "--poll-seconds", "30",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
    /unhandled late source deployment|late source-connected deployment|legacy production changed during source-connect recovery quiescence/);
    assert.equal((await readState(temporary.path)).phase, "github_source_connect_armed");
    assert.equal(fake.state.sourceRepo, GITHUB_SOURCE.repository);
    const state = await executeReleaseCommand([
      "recover", "--state", temporary.path,
      "--reason", "reconcile final-window deployment", "--poll-seconds", "30", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    assert.equal(state.phase, "recovered_legacy");
    assert.equal(state.lastAcceptedUpload.deploymentId, CANDIDATE_ID);
    assert.equal(fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdDisableTrigger")).length, 1);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("source recovery journals a delayed trigger before deleting it on rerun", async () => {
  const temporary = await tempState();
  let delayedTriggerVisible = false;
  try {
    const fake = fakeRailway({
      onCommand: async (argv, _options, commandState) => {
        if (isSourceConnect(argv) && !commandState.crashedBeforeSourceResult) {
          commandState.crashedBeforeSourceResult = true;
          commandState.sourceRepo = GITHUB_SOURCE.repository;
          commandState.trigger = false;
          throw new Error("simulated source metadata before trigger visibility");
        }
      },
    });
    await runPreflight(fake, temporary.path);
    await assert.rejects(executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
    /source metadata before trigger visibility/);
    await assert.rejects(executeReleaseCommand([
      "recover", "--state", temporary.path,
      "--reason", "observe delayed trigger", "--poll-seconds", "30", "--yes",
    ], {
      run: fake.run,
      now: () => NOW,
      sleep: async () => {
        if (delayedTriggerVisible) return;
        const journal = await readState(temporary.path);
        if (Object.hasOwn(journal, "pendingTriggerDisable")
          && journal.pendingTriggerDisable.triggerId == null) {
          delayedTriggerVisible = true;
          fake.state.trigger = true;
        }
      },
    }), /trigger appeared during absence quiescence/);
    assert.equal((await readState(temporary.path)).pendingTriggerDisable.triggerId, TRIGGER_ID);
    assert.equal(fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdDisableTrigger")).length, 0);
    const state = await executeReleaseCommand([
      "recover", "--state", temporary.path,
      "--reason", "delete journaled delayed trigger", "--poll-seconds", "30", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    assert.equal(state.phase, "recovered_legacy");
    assert.equal(fake.state.trigger, false);
    assert.equal(fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdDisableTrigger")).length, 1);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

test("trigger-disable recovery refuses a timeout shorter than the full quiescence window", async () => {
  const temporary = await tempState();
  try {
    const fake = fakeRailway();
    await runPreflight(fake, temporary.path);
    await executeReleaseCommand([
      "deploy-shadow", "--state", temporary.path, "--commit", COMMIT, "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} });
    await assert.rejects(executeReleaseCommand([
      "recover", "--state", temporary.path, "--reason", "reject short timeout",
      "--timeout-seconds", "30", "--poll-seconds", "30", "--yes",
    ], { run: fake.run, now: () => NOW, sleep: async () => {} }),
    /--timeout-seconds is outside its safe range/);
    assert.equal(fake.state.trigger, true);
    assert.equal((await readState(temporary.path)).pendingTriggerDisable, undefined);
    assert.equal(fake.state.commands.filter(({ argv }) => argv[1] === "api"
      && argv[2].includes("DirectProdDisableTrigger")).length, 0);
  } finally {
    await rm(temporary.directory, { recursive: true, force: true });
  }
});

function response(status, payload, headers = {}) {
  return new Response(typeof payload === "string" ? payload : JSON.stringify(payload), {
    status,
    headers,
  });
}

function auditFetchFixture({
  mode = "shadow",
  mutateCounter = false,
  readinessOverrides = {},
} = {}) {
  const calls = [];
  let readinessCalls = 0;
  const fetch = async (input, options = {}) => {
    const url = new URL(String(input));
    const path = `${url.pathname}${url.search}`;
    const method = String(options.method || "GET").toUpperCase();
    calls.push({ url, path, method, headers: options.headers || {}, body: options.body || null });
    if (path === "/health") return response(200, { status: "healthy", commit_sha: COMMIT });
    if (path === "/readyz") {
      readinessCalls += 1;
      const changed = mutateCounter && readinessCalls > 1 ? 1 : 0;
      return response(200, {
        status: "ready",
        ready: true,
        deployment_id: CANDIDATE_ID,
        commit_sha: COMMIT,
        image_digest: CANDIDATE_DIGEST,
        checks: { x402: {
          ready: true,
          mode,
          configuration_valid: true,
          facilitator_ready: true,
          supported_age_seconds: 10,
          supported_networks: [
            "eip155:8453",
            "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
          ],
          challenge_metadata_complete: true,
          unresolved_ledger_entries: 0,
          ledger_durable_path: true,
          verify_calls: changed,
          settle_calls: 0,
          shadow_locked: mode === "shadow",
          allowed_get_routes: ["v1_vwap"],
          payment_rate_limit_per_minute: 12,
          payment_rate_limit_per_day: 200,
          facilitator_max_inflight: 4,
          sdk: { x402: "2.8.0", cdp_sdk: "1.47.1" },
          blockers: [],
          ...readinessOverrides,
        } },
      });
    }
    if (path === "/v1/search?q=BTC") return response(200, { results: [] });
    if (path === "/mcp/server/" && method === "POST") {
      return response(200, `data: ${JSON.stringify({
        jsonrpc: "2.0",
        id: 1,
        result: { protocolVersion: "2025-03-26" },
      })}\n\n`, { "Mcp-Session-Id": "audit-session" });
    }
    if (path === "/mcp/server/" && method === "DELETE") return response(200, "");
    if (path === "/v1/vwap/BTC-USD") {
      const signed = Object.hasOwn(options.headers || {}, "PAYMENT-SIGNATURE");
      if (!signed) {
        const challenge = {
          x402Version: 2,
          resource: {
            url: "https://mcp.blocksize.info/v1/vwap/BTC-USD",
            description: "VWAP",
            mimeType: "application/json",
          },
          accepts: [
            {
              scheme: "exact",
              network: "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp",
              amount: "2000",
              asset: "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v",
              payTo: "11111111111111111111111111111111",
              maxTimeoutSeconds: 30,
              resource: "https://mcp.blocksize.info/v1/vwap/BTC-USD",
              extra: {
                feePayer: "Vote111111111111111111111111111111111111111",
                resource: "https://mcp.blocksize.info/v1/vwap/BTC-USD",
              },
            },
            {
              scheme: "exact",
              network: "eip155:8453",
              amount: "2000",
              asset: "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
              payTo: `0x${"1".repeat(40)}`,
              maxTimeoutSeconds: 60,
              resource: "https://mcp.blocksize.info/v1/vwap/BTC-USD",
              extra: {
                name: "USD Coin",
                version: "2",
                resource: "https://mcp.blocksize.info/v1/vwap/BTC-USD",
              },
            },
          ],
          extensions: { bazaar: {} },
        };
        return response(402, challenge, {
          "PAYMENT-REQUIRED": Buffer.from(JSON.stringify(challenge)).toString("base64"),
          "Cache-Control": "no-store",
        });
      }
      return response(mode === "shadow" ? 503 : 400, {
        code: mode === "shadow" ? "x402_shadow_locked" : "x402_payment_invalid",
      }, { "Cache-Control": "no-store" });
    }
    throw new Error(`unexpected audit request ${method} ${path}`);
  };
  return { fetch, calls };
}

test("hosted audit exercises both domains without a valid payment or facilitator mutation", async () => {
  const fixture = auditFetchFixture();
  const sleeps = [];
  const audit = await runCoinbaseHotfixAudit({
    mode: "shadow",
    deploymentId: CANDIDATE_ID,
    commit: COMMIT,
    expectedImageDigest: CANDIDATE_DIGEST,
    expectedSolanaPayTo: "11111111111111111111111111111111",
    expectedBasePayTo: `0x${"1".repeat(40)}`,
    checks: 2,
    intervalSeconds: 60,
  }, {
    fetch: fixture.fetch,
    sleep: async (milliseconds) => { sleeps.push(milliseconds); },
  });
  assert.equal(audit.passed, true);
  assert.deepEqual(audit.domains, AUDIT_DOMAINS);
  assert.deepEqual(sleeps, [60_000]);
  for (const origin of AUDIT_DOMAINS) {
    assert(fixture.calls.some((call) => call.url.origin === origin
      && call.path === "/v1/search?q=BTC"));
    assert(fixture.calls.some((call) => call.url.origin === origin
      && call.path === "/mcp/server/"));
  }
  assert.equal(fixture.calls.some((call) => /verify|settle/i.test(call.path)), false);
  const signed = fixture.calls.filter((call) => Object.hasOwn(call.headers, "PAYMENT-SIGNATURE"));
  assert(signed.length > 0);
  assert(signed.every((call) => call.headers["PAYMENT-SIGNATURE"]
    === "blocksize-shadow-audit-invalid-not-a-payment"));
});

test("hosted audit fails if its malformed probe changes facilitator counters", async () => {
  const fixture = auditFetchFixture({ mutateCounter: true });
  await assert.rejects(runCoinbaseHotfixAudit({
    mode: "shadow",
    deploymentId: CANDIDATE_ID,
    commit: COMMIT,
    expectedImageDigest: CANDIDATE_DIGEST,
    expectedSolanaPayTo: "11111111111111111111111111111111",
    expectedBasePayTo: `0x${"1".repeat(40)}`,
    checks: 1,
    intervalSeconds: 0,
  }, { fetch: fixture.fetch, sleep: async () => {} }), /caused a facilitator verify or settle call/);
});

test("hosted audit refuses drift in every payment safety invariant", async () => {
  const cases = [
    ["payment_rate_limit_per_minute", 13, /per-minute rate limit/],
    ["payment_rate_limit_per_day", 201, /per-day rate limit/],
    ["facilitator_max_inflight", 5, /concurrency limit/],
    ["ledger_durable_path", false, /exact durable \/data path/],
  ];
  for (const [name, value, expectedError] of cases) {
    const fixture = auditFetchFixture({ readinessOverrides: { [name]: value } });
    await assert.rejects(runCoinbaseHotfixAudit({
      mode: "shadow",
      deploymentId: CANDIDATE_ID,
      commit: COMMIT,
      expectedImageDigest: CANDIDATE_DIGEST,
      expectedSolanaPayTo: "11111111111111111111111111111111",
      expectedBasePayTo: `0x${"1".repeat(40)}`,
      checks: 1,
      intervalSeconds: 0,
    }, { fetch: fixture.fetch, sleep: async () => {} }), expectedError);
  }
});

let passed = 0;
for (const { name, fn } of tests) {
  try {
    await fn();
    passed += 1;
    process.stdout.write(`ok - ${name}\n`);
  } catch (error) {
    process.stderr.write(`not ok - ${name}\n${error.stack || error}\n`);
    process.exitCode = 1;
  }
}

if (process.exitCode) {
  process.stderr.write(`${passed}/${tests.length} tests passed\n`);
} else {
  process.stdout.write(`${passed}/${tests.length} tests passed\n`);
}
