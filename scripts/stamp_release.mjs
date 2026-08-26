#!/usr/bin/env node

import { writeFile } from "node:fs/promises";

const commitSha = (process.argv[2] || "").trim();
const sourceBranch = (process.argv[3] || "").trim();

if (!/^[0-9a-f]{40}$/i.test(commitSha)) {
  throw new Error("stamp_release requires a full 40-character Git commit SHA");
}

const output = new URL("../src/_release_build.json", import.meta.url);
await writeFile(
  output,
  `${JSON.stringify(
    {
      commit_sha: commitSha.toLowerCase(),
      source_branch: sourceBranch || null,
      stamped: true,
    },
    null,
    2,
  )}\n`,
  "utf8",
);
console.log(`Stamped release ${commitSha.toLowerCase()} from ${sourceBranch || "unknown branch"}`);
