#!/usr/bin/env node

/**
 * Read-only hosted audit for the narrow Coinbase x402 hotfix.
 *
 * The only payment-like request deliberately carries an invalid sentinel.  It
 * can prove the shadow lock / local malformed-signature rejection without a
 * signature, wallet, payment amount, facilitator verify, or settlement call.
 */

import { resolve } from "node:path";
import { pathToFileURL } from "node:url";

export const AUDIT_DOMAINS = Object.freeze([
  "https://mcp.blocksize.info",
  "https://agentic-payments-production.up.railway.app",
]);

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const SHA = /^[0-9a-f]{40}$/;
const DIGEST = /^sha256:[0-9a-f]{64}$/;
const INVALID_PAYMENT_SENTINEL = "blocksize-shadow-audit-invalid-not-a-payment";
const SOLANA_NETWORK = "solana:5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp";
const SOLANA_USDC = "EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v";
const BASE_NETWORK = "eip155:8453";
const BASE_USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913";
const CORE_VWAP_ATOMIC_USDC = "2000";
const CANONICAL_VWAP_RESOURCE = "https://mcp.blocksize.info/v1/vwap/BTC-USD";
const SOLANA_ID = /^[1-9A-HJ-NP-Za-km-z]{32,44}$/;
const EVM_ADDRESS = /^0x[0-9a-fA-F]{40}$/;

function fail(message) {
  throw new Error(message);
}

function assert(condition, message) {
  if (!condition) fail(message);
}

function integer(value, label, { minimum = 0, maximum = 10_000 } = {}) {
  assert(/^\d+$/.test(String(value ?? "")), `${label} must be an integer`);
  const parsed = Number(value);
  assert(parsed >= minimum && parsed <= maximum, `${label} is outside its safe range`);
  return parsed;
}

export function parseAuditArguments(argv) {
  const allowed = new Set([
    "mode",
    "deployment-id",
    "commit",
    "expected-image-digest",
    "expected-solana-pay-to",
    "expected-base-pay-to",
    "checks",
    "interval-seconds",
  ]);
  const values = new Map();
  for (let index = 0; index < argv.length; index += 2) {
    const flag = argv[index];
    const value = argv[index + 1];
    assert(flag?.startsWith("--") && value !== undefined,
      "audit arguments must be --name value pairs");
    const name = flag.slice(2);
    assert(allowed.has(name) && !values.has(name), `unexpected or duplicate argument ${flag}`);
    values.set(name, value);
  }
  const mode = values.get("mode");
  assert(mode === "shadow" || mode === "enforce", "--mode must be shadow or enforce");
  const deploymentId = values.get("deployment-id");
  const commit = values.get("commit");
  const expectedImageDigest = String(values.get("expected-image-digest") || "").toLowerCase();
  const expectedSolanaPayTo = String(values.get("expected-solana-pay-to") || "");
  const expectedBasePayTo = String(values.get("expected-base-pay-to") || "");
  assert(UUID.test(deploymentId || ""), "--deployment-id must be a Railway UUID");
  assert(SHA.test(commit || ""), "--commit must be a full lowercase Git SHA");
  assert(DIGEST.test(expectedImageDigest), "--expected-image-digest must be sha256:<64 hex>");
  assert(SOLANA_ID.test(expectedSolanaPayTo), "--expected-solana-pay-to is invalid");
  assert(EVM_ADDRESS.test(expectedBasePayTo) && !/^0x0{40}$/i.test(expectedBasePayTo),
    "--expected-base-pay-to is invalid");
  const checks = integer(values.get("checks") || "1", "--checks", { minimum: 1, maximum: 4 });
  const intervalSeconds = integer(
    values.get("interval-seconds") || (checks > 1 ? "190" : "0"),
    "--interval-seconds",
    { minimum: 0, maximum: 1_800 },
  );
  assert(checks === 1 || intervalSeconds >= 60,
    "multi-check readiness soak must span at least one facilitator refresh interval");
  return {
    mode,
    deploymentId,
    commit,
    expectedImageDigest,
    expectedSolanaPayTo,
    expectedBasePayTo,
    checks,
    intervalSeconds,
  };
}

function noStore(response, label) {
  assert((response.headers.get("cache-control") || "").toLowerCase().includes("no-store"),
    `${label} is missing Cache-Control: no-store`);
}

async function bodyJson(response, label) {
  try {
    return await response.json();
  } catch {
    fail(`${label} did not return JSON`);
  }
}

function paymentCheck(readiness) {
  return readiness?.checks?.x402 || readiness?.checks?.payment || null;
}

function readCounter(check, name) {
  const direct = check?.[name];
  const nested = check?.counters?.[name];
  const value = direct ?? nested;
  assert(Number.isSafeInteger(value) && value >= 0, `readiness ${name} is missing or invalid`);
  return value;
}

function readinessCounters(readiness, mode) {
  const check = paymentCheck(readiness);
  assert(check && check.ready === true, "payment readiness check is not ready");
  assert(check.mode === mode, `payment readiness mode is ${check.mode || "missing"}, expected ${mode}`);
  assert(check.configuration_valid === true, "payment configuration is invalid");
  assert(check.facilitator_ready === true, "Coinbase facilitator /supported is not ready");
  assert(Number.isFinite(check.supported_age_seconds)
    && check.supported_age_seconds >= 0
    && check.supported_age_seconds <= 180,
  "Coinbase facilitator /supported evidence is stale");
  assert(check.unresolved_ledger_entries === 0, "payment ledger contains unresolved entries");
  assert(check.ledger_durable_path === true,
    "payment ledger does not attest the exact durable /data path");
  assert(JSON.stringify([...(check.supported_networks || [])].sort())
    === JSON.stringify([BASE_NETWORK, SOLANA_NETWORK].sort()),
  "readiness does not attest exactly Solana and Base");
  assert(check.challenge_metadata_complete === true,
    "readiness does not attest complete challenge metadata");
  assert(JSON.stringify(check.allowed_get_routes) === JSON.stringify(["v1_vwap"]),
    "enforcement route allowlist is not exactly v1_vwap");
  assert(check.payment_rate_limit_per_minute === 12,
    "payment per-minute rate limit is not exactly 12");
  assert(check.payment_rate_limit_per_day === 200,
    "payment per-day rate limit is not exactly 200");
  assert(check.facilitator_max_inflight === 4,
    "facilitator concurrency limit is not exactly 4");
  assert(check.sdk?.x402 === "2.8.0" && check.sdk?.cdp_sdk === "1.47.1",
    "runtime payment SDK versions drifted");
  assert(Array.isArray(check.blockers) && check.blockers.length === 0,
    "payment readiness reports blockers");
  if (mode === "shadow") {
    assert(check.shadow_locked === true, "shadow readiness does not prove signed-request lock");
  }
  return {
    verify: readCounter(check, "verify_calls"),
    settle: readCounter(check, "settle_calls"),
    unresolved: check.unresolved_ledger_entries,
  };
}

async function fetchBound(fetchFn, origin, path, options = {}) {
  const url = new URL(path, `${origin}/`);
  assert(url.origin === origin, "audit path escaped its reviewed origin");
  return fetchFn(url, {
    redirect: "manual",
    signal: AbortSignal.timeout(30_000),
    ...options,
  });
}

async function fetchReadiness(fetchFn, origin, expected) {
  const response = await fetchBound(fetchFn, origin, "/readyz");
  assert(response.status === 200, `${origin}/readyz returned ${response.status}`);
  const body = await bodyJson(response, `${origin}/readyz`);
  assert(body.ready === true, `${origin}/readyz is not ready`);
  assert(body.deployment_id === expected.deploymentId,
    `${origin}/readyz is served by a different deployment`);
  assert(body.commit_sha === expected.commit, `${origin}/readyz is served by a different commit`);
  assert(String(body.image_digest || "").toLowerCase() === expected.expectedImageDigest,
    `${origin}/readyz is served by a different image`);
  readinessCounters(body, expected.mode);
  return body;
}

async function checkHealth(fetchFn, origin, expected) {
  const response = await fetchBound(fetchFn, origin, "/health");
  assert(response.status === 200, `${origin}/health returned ${response.status}`);
  const body = await bodyJson(response, `${origin}/health`);
  assert(body.status === "healthy", `${origin}/health is not healthy`);
  if (body.commit_sha != null) {
    assert(body.commit_sha === expected.commit, `${origin}/health commit drifted`);
  }
  if (body.deployment_id != null) {
    assert(body.deployment_id === expected.deploymentId, `${origin}/health deployment drifted`);
  }
}

async function checkFreeRoute(fetchFn, origin) {
  const response = await fetchBound(fetchFn, origin, "/v1/search?q=BTC", {
    headers: { Accept: "application/json", "User-Agent": "blocksize-coinbase-hotfix-audit/1" },
  });
  assert(response.status === 200, `${origin} free search returned ${response.status}`);
  assert(!response.headers.get("payment-required"), `${origin} free search unexpectedly challenged payment`);
  await bodyJson(response, `${origin} free search`);
}

function parseMcpPayload(text) {
  const dataLine = text.split(/\r?\n/).find((line) => line.startsWith("data:"));
  const raw = dataLine ? dataLine.slice(5).trim() : text.trim();
  try { return JSON.parse(raw); } catch { fail("MCP initialize returned invalid JSON/SSE"); }
}

async function checkMcp(fetchFn, origin) {
  const endpoint = "/mcp/server/";
  const response = await fetchBound(fetchFn, origin, endpoint, {
    method: "POST",
    headers: {
      Accept: "application/json, text/event-stream",
      "Content-Type": "application/json",
      "User-Agent": "blocksize-coinbase-hotfix-audit/1",
    },
    body: JSON.stringify({
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: {
        protocolVersion: "2025-03-26",
        capabilities: {},
        clientInfo: { name: "coinbase-hotfix-audit", version: "1" },
      },
    }),
  });
  assert(response.status === 200, `${origin} MCP initialize returned ${response.status}`);
  const payload = parseMcpPayload(await response.text());
  assert(payload?.result?.protocolVersion === "2025-03-26", `${origin} MCP protocol drifted`);
  const sessionId = response.headers.get("mcp-session-id");
  if (sessionId) {
    const terminated = await fetchBound(fetchFn, origin, endpoint, {
      method: "DELETE",
      headers: { "Mcp-Session-Id": sessionId },
    });
    assert(terminated.status === 200, `${origin} MCP session cleanup returned ${terminated.status}`);
  }
}

async function checkUnpaidChallenge(fetchFn, origin, expected) {
  const response = await fetchBound(fetchFn, origin, "/v1/vwap/BTC-USD", {
    headers: { Accept: "application/json", "User-Agent": "blocksize-coinbase-hotfix-audit/1" },
  });
  assert(response.status === 402, `${origin} unpaid VWAP returned ${response.status}`);
  noStore(response, `${origin} unpaid VWAP`);
  assert(response.headers.get("payment-required"), `${origin} unpaid VWAP omitted PAYMENT-REQUIRED`);
  const body = await bodyJson(response, `${origin} unpaid VWAP`);
  assert(body.x402Version === 2, `${origin} unpaid VWAP is not x402 v2`);
  assert(body.resource?.url === CANONICAL_VWAP_RESOURCE,
    `${origin} unpaid VWAP has a non-canonical resource URL`);
  assert(Array.isArray(body.accepts) && body.accepts.length === 2,
    `${origin} unpaid VWAP must offer exactly Solana and Base`);
  const byNetwork = new Map(body.accepts.map((item) => [item?.network, item]));
  assert(byNetwork.size === 2 && byNetwork.has(SOLANA_NETWORK) && byNetwork.has(BASE_NETWORK),
    `${origin} unpaid VWAP network inventory drifted`);
  for (const requirement of byNetwork.values()) {
    assert(requirement.scheme === "exact", `${origin} challenge scheme is not exact`);
    assert(requirement.amount === CORE_VWAP_ATOMIC_USDC,
      `${origin} challenge amount is not exactly $0.002 USDC`);
    assert(Number.isSafeInteger(requirement.maxTimeoutSeconds)
      && requirement.maxTimeoutSeconds > 0
      && requirement.maxTimeoutSeconds <= 60,
    `${origin} challenge timeout is invalid`);
  }
  const solana = byNetwork.get(SOLANA_NETWORK);
  assert(solana.asset === SOLANA_USDC, `${origin} Solana asset is not canonical USDC`);
  assert(SOLANA_ID.test(solana.payTo || "")
    && solana.payTo === expected.expectedSolanaPayTo,
  `${origin} Solana payTo differs from the preflight recipient`);
  assert(SOLANA_ID.test(solana.extra?.feePayer || ""),
    `${origin} Solana requirement has no facilitator feePayer`);
  const base = byNetwork.get(BASE_NETWORK);
  assert(String(base.asset || "").toLowerCase() === BASE_USDC.toLowerCase(),
    `${origin} Base asset is not canonical USDC`);
  assert(EVM_ADDRESS.test(base.payTo || "") && !/^0x0{40}$/i.test(base.payTo)
    && base.payTo.toLowerCase() === expected.expectedBasePayTo.toLowerCase(),
  `${origin} Base payTo differs from the preflight recipient`);
  assert(base.extra?.name === "USD Coin" && String(base.extra?.version) === "2",
    `${origin} Base EIP-712 USDC metadata drifted`);

  const encoded = response.headers.get("payment-required");
  let headerBody;
  try {
    headerBody = JSON.parse(Buffer.from(encoded, "base64").toString("utf8"));
  } catch {
    fail(`${origin} PAYMENT-REQUIRED is not base64-encoded JSON`);
  }
  for (const key of ["x402Version", "resource", "accepts", "extensions"]) {
    assert(JSON.stringify(headerBody?.[key]) === JSON.stringify(body[key]),
      `${origin} PAYMENT-REQUIRED header and body disagree on ${key}`);
  }
}

async function checkInvalidSignedRequest(fetchFn, origin, mode) {
  const response = await fetchBound(fetchFn, origin, "/v1/vwap/BTC-USD", {
    headers: {
      Accept: "application/json",
      "PAYMENT-SIGNATURE": INVALID_PAYMENT_SENTINEL,
      "User-Agent": "blocksize-coinbase-hotfix-audit/1",
    },
  });
  if (mode === "shadow") {
    assert(response.status === 503, `${origin} shadow signed request returned ${response.status}`);
  } else {
    assert([400, 402].includes(response.status),
      `${origin} enforce malformed signature returned ${response.status}`);
  }
  noStore(response, `${origin} ${mode} invalid signed request`);
  const body = await bodyJson(response, `${origin} ${mode} invalid signed request`);
  const code = body?.code || body?.error?.code || body?.detail?.code;
  if (mode === "shadow") {
    assert(code === "x402_shadow_locked", `${origin} shadow request did not prove the lock`);
  } else {
    assert(code === "x402_payment_invalid",
      `${origin} enforce request was not rejected locally as malformed`);
  }
}

async function auditOrigin(fetchFn, origin, expected) {
  await checkHealth(fetchFn, origin, expected);
  const beforeBody = await fetchReadiness(fetchFn, origin, expected);
  const before = readinessCounters(beforeBody, expected.mode);
  if (expected.mode === "shadow") {
    assert(before.verify === 0 && before.settle === 0,
      `${origin} shadow mode already reports facilitator verify/settle calls`);
  }
  await checkFreeRoute(fetchFn, origin);
  await checkMcp(fetchFn, origin);
  await checkUnpaidChallenge(fetchFn, origin, expected);
  await checkInvalidSignedRequest(fetchFn, origin, expected.mode);
  const afterBody = await fetchReadiness(fetchFn, origin, expected);
  const after = readinessCounters(afterBody, expected.mode);
  assert(after.verify === before.verify && after.settle === before.settle,
    `${origin} audit caused a facilitator verify or settle call`);
  assert(after.unresolved === 0, `${origin} audit left unresolved payment state`);
  return { origin, verifyCalls: after.verify, settleCalls: after.settle };
}

export async function runCoinbaseHotfixAudit(options, injected = {}) {
  const fetchFn = injected.fetch || globalThis.fetch;
  const sleep = injected.sleep
    || ((milliseconds) => new Promise((done) => setTimeout(done, milliseconds)));
  const cycles = [];
  for (let cycle = 0; cycle < options.checks; cycle += 1) {
    const origins = [];
    for (const origin of AUDIT_DOMAINS) {
      origins.push(await auditOrigin(fetchFn, origin, options));
    }
    cycles.push({ cycle: cycle + 1, origins });
    if (cycle + 1 < options.checks) await sleep(options.intervalSeconds * 1000);
  }
  return {
    passed: true,
    mode: options.mode,
    deploymentId: options.deploymentId,
    commit: options.commit,
    imageDigest: options.expectedImageDigest,
    fundedPaymentSubmitted: false,
    facilitatorVerifyOrSettleCaused: false,
    domains: [...AUDIT_DOMAINS],
    cycles,
  };
}

async function main() {
  const options = parseAuditArguments(process.argv.slice(2));
  const result = await runCoinbaseHotfixAudit(options);
  process.stdout.write(`${JSON.stringify(result)}\n`);
}

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
  main().catch((error) => {
    process.stderr.write(`Coinbase hotfix audit refused: ${error.message}\n`);
    process.exitCode = 1;
  });
}
