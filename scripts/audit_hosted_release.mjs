#!/usr/bin/env node

import { readFile } from "node:fs/promises";

const baseUrl = (process.argv[2] || "https://mcp.blocksize.info").replace(/\/$/, "");
const expectedCommit = (process.argv[3] || "").trim().toLowerCase();
const expectedManifest = JSON.parse(await readFile(new URL("../server.json", import.meta.url)));
const protocolVersion = "2025-03-26";
const auditUserAgent = "blocksize-hosted-smoke/1.0";
const auditTimeoutMs = Number(process.env.RELEASE_AUDIT_TIMEOUT_MS || "300000");
if (!Number.isSafeInteger(auditTimeoutMs) || auditTimeoutMs < 30_000 || auditTimeoutMs > 600_000) {
  throw new Error("RELEASE_AUDIT_TIMEOUT_MS must be between 30000 and 600000");
}
const auditDeadline = AbortSignal.timeout(auditTimeoutMs);
const expectedTools = [
  "fetch",
  "get_market_data_endpoint",
  "get_pricing_info",
  "get_product_catalog",
  "get_workflow_endpoint",
  "list_instruments",
  "search",
  "search_pairs",
];
const toolCalls = {
  search_pairs: { query: "BTC", asset_class: "crypto" },
  list_instruments: { service: "vwap" },
  get_pricing_info: {},
  get_product_catalog: {},
  get_workflow_endpoint: { product: "agent_market_brief" },
  get_market_data_endpoint: { service: "vwap", symbol: "BTC-USD" },
  search: { query: "pricing" },
  fetch: { id: "doc:pricing" },
};

if (expectedCommit && !/^[0-9a-f]{40}$/.test(expectedCommit)) {
  throw new Error("expected commit must be a full 40-character Git SHA");
}

function assert(condition, message) {
  if (!condition) throw new Error(message);
}

function sleep(milliseconds) {
  return new Promise((resolve, reject) => {
    if (auditDeadline.aborted) {
      reject(new Error(`hosted release audit exceeded ${auditTimeoutMs}ms`));
      return;
    }
    const onAbort = () => {
      clearTimeout(timer);
      reject(new Error(`hosted release audit exceeded ${auditTimeoutMs}ms`));
    };
    const timer = setTimeout(() => {
      auditDeadline.removeEventListener("abort", onAbort);
      resolve();
    }, milliseconds);
    auditDeadline.addEventListener("abort", onAbort, { once: true });
  });
}

function auditFetch(input, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("User-Agent", auditUserAgent);
  const signal = options.signal
    ? AbortSignal.any([auditDeadline, options.signal])
    : auditDeadline;
  return fetch(input, { ...options, headers, signal });
}

function localPath(url, label) {
  const parsed = new URL(url);
  const expectedOrigin = new URL(baseUrl).origin;
  assert(parsed.origin === expectedOrigin, `${label} points outside the candidate origin`);
  return `${parsed.pathname}${parsed.search}`;
}

function collectHttpUrls(value, destination = new Set()) {
  if (typeof value === "string" && /^https?:\/\//i.test(value)) {
    destination.add(value);
  } else if (Array.isArray(value)) {
    for (const item of value) collectHttpUrls(item, destination);
  } else if (value && typeof value === "object") {
    for (const item of Object.values(value)) collectHttpUrls(item, destination);
  }
  return destination;
}

function rewriteTrackedOrigin(value, trackedOrigin, candidateOrigin) {
  if (typeof value === "string" && /^https?:\/\//i.test(value)) {
    const parsed = new URL(value);
    if (parsed.origin === trackedOrigin) {
      return `${candidateOrigin}${parsed.pathname}${parsed.search}${parsed.hash}`;
    }
    return value;
  }
  if (Array.isArray(value)) {
    return value.map((item) => rewriteTrackedOrigin(item, trackedOrigin, candidateOrigin));
  }
  if (value && typeof value === "object") {
    return Object.fromEntries(
      Object.entries(value).map(([key, item]) => [
        key,
        rewriteTrackedOrigin(item, trackedOrigin, candidateOrigin),
      ]),
    );
  }
  return value;
}

const x402PostBodies = {
  "/v1/briefs/market": { symbols: ["BTCUSD"] },
  "/v1/checks/pre-trade": { symbol: "BTCUSD", side: "buy", notional_usd: 1 },
  "/v1/receipts/price": { symbol: "BTCUSD" },
  "/v1/snapshots/macro": { universe: ["BTCUSD"] },
  "/v1/indicators/token-quality": { symbol: "BTCUSD" },
  "/v1/indicators/state-divergence": { symbol: "MSOLUSD" },
  "/v1/signals/solana-token-brief": { symbols: ["SOLUSD"] },
  "/v1/signals/trader-alpha-pack": { symbols: ["BTCUSD"] },
};
const x402GetPaths = new Set([
  "/v1/vwap/BTC-USD",
  "/v1/bidask/BTC-USD",
  "/v1/state/MSOLUSD",
  "/v1/vwap30m/SOLUSD",
  "/v1/vwap24h/BTCUSD",
  "/v1/bidask/AAPLXUSD",
  "/v1/fx/EURUSD",
  "/v1/metal/XAUUSD",
]);

async function assertX402Challenge(resourceUrl, method) {
  const path = localPath(resourceUrl, "x402 discovery resource");
  const pathname = new URL(resourceUrl).pathname;
  const options = {
    method,
    headers: { Origin: baseUrl },
  };
  if (method === "POST") {
    const requestBody = x402PostBodies[pathname];
    assert(requestBody, `${path}: missing safe POST challenge fixture`);
    options.headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(requestBody);
  }
  const response = await fetchChecked(path, 402, options);
  const body = await response.json();
  assert(body.x402Version === 2, `${path}: response body is not x402 v2`);
  assert(response.headers.get("payment-required"), `${path}: missing PAYMENT-REQUIRED`);
  assert(
    (response.headers.get("cache-control") || "").includes("no-store"),
    `${path}: payment challenge may be cached`,
  );
}

async function fetchOperationalOAuthEndpoint(url, method, label, options = {}) {
  const response = await auditFetch(`${baseUrl}${localPath(url, label)}`, {
    method,
    redirect: "manual",
    signal: AbortSignal.timeout(30_000),
    ...options,
  });
  assert(response.status >= 300 && response.status < 500, `${label} returned ${response.status}`);
  assert(![404, 405].includes(response.status), `${label} route is not mounted`);
  return response;
}

async function waitForCandidate() {
  let lastState = "no response";
  for (let attempt = 1; attempt <= 36; attempt += 1) {
    try {
      const response = await auditFetch(`${baseUrl}/readyz`, {
        signal: AbortSignal.timeout(10_000),
      });
      const body = await response.json();
      const versionMatches = body.version === expectedManifest.version;
      const commitMatches = !expectedCommit || body.commit_sha === expectedCommit;
      if (response.status === 200 && body.ready === true && versionMatches && commitMatches) {
        return body;
      }
      lastState = JSON.stringify({
        status: response.status,
        ready: body.ready,
        version: body.version,
        commit_sha: body.commit_sha,
      });
    } catch (error) {
      lastState = String(error);
    }
    await sleep(5_000);
  }
  throw new Error(`candidate did not become active: ${lastState}`);
}

async function fetchChecked(path, expectedStatus = 200, options = {}) {
  const response = await auditFetch(`${baseUrl}${path}`, {
    redirect: "manual",
    signal: AbortSignal.timeout(30_000),
    ...options,
  });
  assert(
    response.status === expectedStatus,
    `${path}: expected ${expectedStatus}, received ${response.status}`,
  );
  return response;
}

function parseSseJson(text) {
  const line = text.split(/\r?\n/).find((item) => item.startsWith("data:"));
  assert(line, "MCP response omitted an SSE data event");
  return JSON.parse(line.slice(5).trim());
}

async function checkMcp() {
  const endpoint = `${baseUrl}/mcp/server/`;
  const baseHeaders = {
    Accept: "application/json, text/event-stream",
    "Content-Type": "application/json",
  };
  const initialize = await auditFetch(endpoint, {
    method: "POST",
    headers: baseHeaders,
    body: JSON.stringify({
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: {
        protocolVersion,
        capabilities: {},
        clientInfo: { name: "blocksize-hosted-release-gate", version: "1.0" },
      },
    }),
    signal: AbortSignal.timeout(30_000),
  });
  assert(initialize.status === 200, `MCP initialize returned ${initialize.status}`);
  const initializePayload = parseSseJson(await initialize.text());
  const sessionId = initialize.headers.get("mcp-session-id");
  assert(sessionId, "MCP initialize omitted mcp-session-id");
  assert(
    initializePayload.result?.protocolVersion === protocolVersion,
    "MCP negotiated an unexpected protocol version",
  );
  assert(
    initializePayload.result?.serverInfo?.version === expectedManifest.version,
    "MCP server version does not match the release manifest",
  );

  const headers = { ...baseHeaders, "Mcp-Session-Id": sessionId };
  const initialized = await auditFetch(endpoint, {
    method: "POST",
    headers,
    body: JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }),
    signal: AbortSignal.timeout(30_000),
  });
  assert(initialized.status === 202, `MCP initialized returned ${initialized.status}`);

  const toolsResponse = await auditFetch(endpoint, {
    method: "POST",
    headers,
    body: JSON.stringify({ jsonrpc: "2.0", id: 2, method: "tools/list", params: {} }),
    signal: AbortSignal.timeout(30_000),
  });
  assert(toolsResponse.status === 200, `MCP tools/list returned ${toolsResponse.status}`);
  const toolsPayload = parseSseJson(await toolsResponse.text());
  const toolNames = (toolsPayload.result?.tools || []).map((tool) => tool.name).sort();
  assert(JSON.stringify(toolNames) === JSON.stringify(expectedTools), "MCP tool catalog drifted");
  assert(
    (toolsPayload.result?.tools || []).every((tool) => tool.annotations?.readOnlyHint === true),
    "One or more public MCP tools are not marked read-only",
  );

  let requestId = 10;
  for (const [name, args] of Object.entries(toolCalls)) {
    const response = await auditFetch(endpoint, {
      method: "POST",
      headers,
      body: JSON.stringify({
        jsonrpc: "2.0",
        id: requestId++,
        method: "tools/call",
        params: { name, arguments: args },
      }),
      signal: AbortSignal.timeout(30_000),
    });
    assert(response.status === 200, `${name}: MCP tool call returned ${response.status}`);
    const payload = parseSseJson(await response.text());
    assert(payload.result?.isError !== true, `${name}: MCP tool call returned an error`);
  }

  const terminated = await auditFetch(endpoint, {
    method: "DELETE",
    headers,
    signal: AbortSignal.timeout(30_000),
  });
  assert(terminated.status === 200, `MCP session delete returned ${terminated.status}`);
  return { tools: toolNames.length, protocolVersion };
}

async function run() {
  const results = {};
  const readinessBody = await waitForCandidate();
  results.readiness = readinessBody.status;
  assert(
    Object.entries(readinessBody.checks || {}).every(([, check]) => check?.ready === true),
    "one or more readiness dependencies are not ready",
  );

  const health = await fetchChecked("/health");
  const healthBody = await health.json();
  assert(healthBody.status === "healthy", "liveness payload is not healthy");
  assert(healthBody.version === expectedManifest.version, "health version drifted");
  if (expectedCommit) {
    assert(healthBody.commit_sha === expectedCommit, "health commit does not match candidate");
  }

  const protocolHealthLinks = new Set([
    "remote_mcp",
    "anthropic_mcp",
    "cursor_mcp",
    "openai_mcp",
  ]);
  const oauthCallbackLinks = new Set([
    "anthropic_oauth_callback",
    "cursor_oauth_callback",
    "openai_oauth_callback",
  ]);
  for (const [name, url] of Object.entries(healthBody.links || {})) {
    localPath(url, `health link ${name}`);
    if (protocolHealthLinks.has(name) || oauthCallbackLinks.has(name)) continue;
    await fetchChecked(localPath(url, `health link ${name}`));
  }

  const serverJson = await (await fetchChecked("/server.json")).json();
  const trackedOrigin = new URL(expectedManifest.homepage).origin;
  const candidateOrigin = new URL(baseUrl).origin;
  const expectedCandidateManifest = rewriteTrackedOrigin(
    expectedManifest,
    trackedOrigin,
    candidateOrigin,
  );
  assert(
    JSON.stringify(serverJson) === JSON.stringify(expectedCandidateManifest),
    "live server.json invariant fields or candidate URLs drifted",
  );
  assert(serverJson.description.length <= 100, "registry description exceeds 100 characters");
  localPath(serverJson.homepage, "server.json homepage");
  localPath(serverJson.websiteUrl, "server.json websiteUrl");
  for (const remote of serverJson.remotes || []) {
    localPath(remote.url, "server.json remote");
  }

  const portal = await (await fetchChecked("/")).text();
  assert(!portal.includes("/fonts/"), "homepage still requests unshipped fonts");
  const criticalPaths = [
    "/quickstart/remote-mcp",
    "/quickstart/first-price",
    "/prompt-examples",
    "/privacy",
    "/support",
    "/claude-connector",
    "/assets/favicon.ico",
    "/assets/favicon.png",
    "/assets/logo-square.svg",
    "/pdf/Blocksize_Agent_Manual.pdf",
    "/evidence/rwa-coverage-index.html",
    "/evidence/oracle-lineage-index.html",
    "/mcp/manifest.json",
    "/robots.txt",
    "/llms.txt",
    "/data-packages.json",
    "/category-hubs.json",
  ];
  await Promise.all(criticalPaths.map((path) => fetchChecked(path)));
  results.criticalPaths = criticalPaths.length;

  const manifest = await (await fetchChecked("/mcp/manifest.json")).json();
  const manifestLinkUrls = [...collectHttpUrls(manifest.links || {})];
  assert(manifestLinkUrls.length > 0, "MCP manifest contains no links");
  const candidateManifestLinks = manifestLinkUrls.filter(
    (url) => new URL(url).origin === candidateOrigin,
  );
  assert(candidateManifestLinks.length > 0, "MCP manifest has no candidate-origin links");
  await Promise.all(
    candidateManifestLinks.map(async (url) => {
      const path = localPath(url, "MCP manifest link");
      const response = await auditFetch(`${baseUrl}${path}`, {
        redirect: "manual",
        signal: AbortSignal.timeout(30_000),
      });
      assert(response.status === 200, `MCP manifest link ${url} returned ${response.status}`);
    }),
  );
  results.manifestLinks = candidateManifestLinks.length;

  const sitemapText = await (await fetchChecked("/sitemap.xml")).text();
  const sitemapUrls = [...sitemapText.matchAll(/<loc>([^<]+)<\/loc>/g)].map((match) => match[1]);
  assert(sitemapUrls.length > 0, "sitemap contains no URLs");
  await Promise.all(
    sitemapUrls.map(async (url) => {
      const path = localPath(url, "sitemap URL");
      const response = await auditFetch(`${baseUrl}${path}`, {
        redirect: "manual",
        signal: AbortSignal.timeout(30_000),
      });
      assert(response.status === 200, `sitemap URL ${url} returned ${response.status}`);
    }),
  );
  results.sitemapUrls = sitemapUrls.length;

  const metadataPaths = [
    "/.well-known/oauth-protected-resource/anthropic/mcp/",
    "/.well-known/oauth-authorization-server/anthropic/mcp",
    "/.well-known/oauth-protected-resource/cursor/mcp/",
    "/.well-known/oauth-authorization-server/cursor/mcp",
    "/.well-known/oauth-protected-resource/openai/mcp/",
    "/.well-known/oauth-authorization-server/openai/mcp",
  ];
  const oauthMetadata = await Promise.all(
    metadataPaths.map(async (path) => (await fetchChecked(path)).json()),
  );
  for (const metadata of oauthMetadata) {
    assert(metadata.oauth_available === true, "OAuth metadata says OAuth is unavailable");
    for (const authorizationServer of metadata.authorization_servers || []) {
      localPath(authorizationServer, "OAuth authorization server");
    }
    for (const field of ["authorization_endpoint", "token_endpoint", "registration_endpoint"]) {
      if (metadata[field]) localPath(metadata[field], `OAuth ${field}`);
    }
  }
  const authorizationMetadata = oauthMetadata.filter(
    (metadata) => metadata.authorization_endpoint,
  );
  assert(authorizationMetadata.length === 3, "not every connector advertises operational OAuth");
  for (const metadata of authorizationMetadata) {
    await fetchOperationalOAuthEndpoint(
      metadata.authorization_endpoint,
      "GET",
      "OAuth authorize endpoint",
    );
    await fetchOperationalOAuthEndpoint(
      metadata.token_endpoint,
      "POST",
      "OAuth token endpoint",
      {
        headers: { "Content-Type": "application/x-www-form-urlencoded" },
        body: "grant_type=authorization_code",
      },
    );
    await fetchOperationalOAuthEndpoint(
      metadata.registration_endpoint,
      "POST",
      "OAuth registration endpoint",
      {
        headers: { "Content-Type": "application/json" },
        body: "{}",
      },
    );
  }
  for (const callbackName of oauthCallbackLinks) {
    assert(healthBody.links?.[callbackName], `${callbackName} is not advertised`);
    await fetchOperationalOAuthEndpoint(
      healthBody.links[callbackName],
      "GET",
      callbackName,
    );
  }
  for (const path of ["/anthropic/mcp/", "/cursor/mcp/", "/openai/mcp/"]) {
    const response = await fetchChecked(path, 401, {
      headers: { Accept: "application/json, text/event-stream" },
    });
    assert(
      (response.headers.get("www-authenticate") || "").includes("resource_metadata"),
      `${path}: missing OAuth resource metadata challenge`,
    );
  }
  results.oauthConnectors = 3;

  const x402Discovery = await (await fetchChecked("/.well-known/x402")).json();
  assert(x402Discovery.version === 1, "x402 discovery version drifted");
  assert(
    Array.isArray(x402Discovery.resources) && x402Discovery.resources.length > 0,
    "x402 discovery contains no resources",
  );
  for (const resourceUrl of x402Discovery.resources) {
    assert(typeof resourceUrl === "string", "x402 discovery resource must be a URL");
    const resourcePath = localPath(resourceUrl, "x402 discovery resource");
    const pathname = new URL(resourceUrl).pathname;
    const method = x402PostBodies[pathname]
      ? "POST"
      : x402GetPaths.has(pathname)
        ? "GET"
        : null;
    assert(method, `refusing to request unknown x402 discovery resource ${resourcePath}`);
    await assertX402Challenge(resourceUrl, method);
    assert(resourcePath.startsWith("/v1/"), `unexpected x402 resource path ${resourcePath}`);
  }
  results.x402Challenges = x402Discovery.resources.length;

  for (const path of ["/.env", "/.git/config"]) await fetchChecked(path, 404);
  results.mcp = await checkMcp();
  return results;
}

try {
  const results = await run();
  console.log(JSON.stringify({ passed: true, baseUrl, version: expectedManifest.version, results }, null, 2));
} catch (error) {
  console.error(JSON.stringify({ passed: false, baseUrl, error: String(error) }, null, 2));
  process.exitCode = 1;
}
