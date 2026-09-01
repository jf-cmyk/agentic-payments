#!/usr/bin/env node

import { readFile } from "node:fs/promises";

const config = await readFile(new URL("../.railway/railway.ts", import.meta.url), "utf8");
const versionMatch = config.match(/railpackVersion:\s*"([^"]+)"/);
if (!versionMatch) throw new Error(".railway/railway.ts does not pin build.railpackVersion");

const expectedVersion = versionMatch[1];
if (!/^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$/.test(expectedVersion)) {
  throw new Error("railpackVersion must use Railway's bare semantic-version syntax");
}

let buildLog = "";
for await (const chunk of process.stdin) buildLog += chunk;
const normalizedLog = buildLog.replace(/\u001b\[[0-9;]*m/g, "");
const versionPatterns = {
  driver: /^using build driver railpack-v([0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?)$/gm,
  prepare: /^(?:\[INFO\] )?\[railway\] prepare railpack-v([0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?)$/gm,
  banner: /^│\s+Railpack\s+([0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?)\s+│$/gm,
  frontend:
    /^(?:\[INFO\] )?resolve image config for docker-image:\/\/ghcr\.io\/railwayapp\/railpack-frontend:v([0-9]+\.[0-9]+\.[0-9]+(?:[-+][0-9A-Za-z.-]+)?)$/gm,
};

for (const [label, pattern] of Object.entries(versionPatterns)) {
  const versions = new Set(Array.from(normalizedLog.matchAll(pattern), (match) => match[1]));
  if (versions.size !== 1 || !versions.has(expectedVersion)) {
    const observed = versions.size > 0 ? [...versions].join(", ") : "missing";
    throw new Error(
      `Railway did not prove the pinned Railpack ${expectedVersion} build; ${label} marker observed ${observed}`,
    );
  }
}

console.log(`Verified Railway built with pinned Railpack ${expectedVersion}.`);
