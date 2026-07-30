#!/usr/bin/env node

import { readFile } from "node:fs/promises";

const baseUrl = "https://staging-candidate.example";
const expectedManifest = JSON.parse(
  await readFile(new URL("../server.json", import.meta.url)),
);
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
const calls = [];

function response(status, payload = "", headers = {}) {
  const body = typeof payload === "string" ? payload : JSON.stringify(payload);
  return new Response(body, { status, headers });
}

function candidateServerJson() {
  return {
    ...expectedManifest,
    homepage: `${baseUrl}/`,
    websiteUrl: `${baseUrl}/`,
    remotes: expectedManifest.remotes.map((remote) => ({
      ...remote,
      url: `${baseUrl}/mcp/server/`,
    })),
  };
}

globalThis.fetch = async (input, options = {}) => {
  const url = new URL(String(input));
  const path = url.pathname;
  const method = String(options.method || "GET").toUpperCase();
  calls.push([method, path]);

  if (method === "GET" && path === "/readyz") {
    return response(200, {
      status: "ready",
      ready: true,
      version: expectedManifest.version,
      commit_sha: null,
      checks: { controlled_candidate: { ready: true } },
    });
  }
  if (method === "GET" && path === "/health") {
    const links = {
      remote_mcp: `${baseUrl}/mcp/server/`,
      anthropic_mcp: `${baseUrl}/anthropic/mcp/`,
      cursor_mcp: `${baseUrl}/cursor/mcp/`,
      openai_mcp: `${baseUrl}/openai/mcp/`,
      manifest: `${baseUrl}/mcp/manifest.json`,
    };
    for (const connector of ["anthropic", "cursor", "openai"]) {
      links[`${connector}_oauth_callback`] = `${baseUrl}/${connector}/mcp/auth/callback`;
    }
    return response(200, {
      status: "healthy",
      version: expectedManifest.version,
      commit_sha: null,
      links,
    });
  }
  if (method === "GET" && path === "/server.json") {
    return response(200, candidateServerJson());
  }
  if (method === "GET" && path === "/mcp/manifest.json") {
    return response(200, {
      links: { homepage: baseUrl, support: `${baseUrl}/support` },
    });
  }
  if (method === "GET" && path === "/sitemap.xml") {
    return response(
      200,
      `<urlset><url><loc>${baseUrl}/</loc></url>`
        + `<url><loc>${baseUrl}/support</loc></url></urlset>`,
    );
  }
  if (method === "GET" && path.startsWith("/.well-known/oauth-protected-resource/")) {
    const connector = path.split("/")[3];
    return response(200, {
      oauth_available: true,
      authorization_servers: [`${baseUrl}/${connector}/mcp`],
    });
  }
  if (method === "GET" && path.startsWith("/.well-known/oauth-authorization-server/")) {
    const connector = path.split("/")[3];
    const prefix = `${baseUrl}/${connector}/mcp`;
    return response(200, {
      oauth_available: true,
      authorization_endpoint: `${prefix}/authorize`,
      token_endpoint: `${prefix}/token`,
      registration_endpoint: `${prefix}/register`,
    });
  }
  if (method === "GET" && path === "/.well-known/x402") {
    return response(200, {
      version: 1,
      resources: [
        `${baseUrl}/v1/vwap/BTC-USD`,
        `${baseUrl}/v1/briefs/market`,
      ],
    });
  }
  if (
    method === "GET"
    && ["/anthropic/mcp/", "/cursor/mcp/", "/openai/mcp/"].includes(path)
  ) {
    return response(
      401,
      { error: "authentication required" },
      { "WWW-Authenticate": 'Bearer resource_metadata="metadata"' },
    );
  }
  if (method === "GET" && (path.endsWith("/authorize") || path.endsWith("/auth/callback"))) {
    return response(400, { error: "controlled OAuth request" });
  }
  if (method === "POST" && (path.endsWith("/token") || path.endsWith("/register"))) {
    return response(400, { error: "controlled OAuth request" });
  }
  if (method === "GET" && path === "/v1/vwap/BTC-USD") {
    return response(
      402,
      { x402Version: 2 },
      { "PAYMENT-REQUIRED": "test", "Cache-Control": "no-store" },
    );
  }
  if (method === "POST" && path === "/v1/briefs/market") {
    return response(
      402,
      { x402Version: 2 },
      { "PAYMENT-REQUIRED": "test", "Cache-Control": "no-store" },
    );
  }
  if (method === "POST" && path === "/mcp/server/") {
    const request = JSON.parse(options.body || "{}");
    if (request.method === "notifications/initialized") return response(202);
    let result;
    if (request.method === "initialize") {
      result = {
        protocolVersion: "2025-03-26",
        serverInfo: { name: "controlled-candidate", version: expectedManifest.version },
      };
    } else if (request.method === "tools/list") {
      result = {
        tools: expectedTools.map((name) => ({
          name,
          annotations: { readOnlyHint: true },
        })),
      };
    } else if (request.method === "tools/call") {
      result = { content: [], isError: false };
    } else {
      return response(400, { error: "unexpected MCP method" });
    }
    return response(
      200,
      `data: ${JSON.stringify({ jsonrpc: "2.0", id: request.id, result })}\n\n`,
      { "Mcp-Session-Id": "controlled-session" },
    );
  }
  if (method === "DELETE" && path === "/mcp/server/") return response(200);
  if (method === "GET" && ["/.env", "/.git/config"].includes(path)) {
    return response(404);
  }
  return response(200, "controlled candidate");
};

process.argv[2] = baseUrl;
process.argv[3] = "";
await import(`./audit_hosted_release.mjs?controlled=${Date.now()}`);
if (process.exitCode) throw new Error("controlled hosted audit failed");

function assertCalled(method, path) {
  if (!calls.some(([actualMethod, actualPath]) => (
    actualMethod === method && actualPath === path
  ))) {
    throw new Error(`${method} ${path} was not exercised`);
  }
}

for (const connector of ["anthropic", "cursor", "openai"]) {
  assertCalled("GET", `/${connector}/mcp/authorize`);
  assertCalled("POST", `/${connector}/mcp/token`);
  assertCalled("POST", `/${connector}/mcp/register`);
  assertCalled("GET", `/${connector}/mcp/auth/callback`);
}
assertCalled("GET", "/v1/vwap/BTC-USD");
assertCalled("POST", "/v1/briefs/market");
console.log(JSON.stringify({ passed: true, calls: calls.length }));
