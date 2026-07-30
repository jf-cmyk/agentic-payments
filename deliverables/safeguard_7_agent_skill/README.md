# Safeguard 7 — Agent skill and plugin acceptance

Date: 2026-07-30

## Decision

The OpenAI/Codex, Claude, Cursor, and standalone Agent Skill packages pass the
local source, schema, privacy, safety, and reproducibility gates. They are ready
for controlled signed-in client acceptance, but not for public marketplace
publication or production promotion.

## Defects remediated

- Corrected Claude's ineffective `.mcp.json` by adding the required top-level
  `mcpServers` map.
- Removed Cursor's schema-forbidden `$schema` field and corrected its
  provider-scoped OAuth metadata URL.
- Removed Cursor instructions that deleted a shared auth directory, executed an
  unpinned package, and enabled debug auth output.
- Separated full Cursor plugin installation from MCP-only installation.
- Gave OpenAI's authenticated and public MCP connections distinct identities so
  one cannot overwrite the other.
- Made route-builder steps conditional on the public discovery tools actually
  being available.
- Added tool-output prompt-injection, credential, timestamp, instrument,
  retry, and credit-drain boundaries to the shared skill.
- Removed direct user IDs and email addresses from connector credit responses.
- Added connector privacy coverage, no-model-training disclosure, data terms,
  and support links.
- Added repo marketplace manifests for controlled OpenAI/Codex and Claude
  installation.
- Replaced ad hoc ZIPs with an explicit allowlist, path/symlink/secret checks,
  fixed timestamps, deterministic compression, and a checksum manifest.

## Accepted locally

| Gate | Result |
| --- | --- |
| Three Agent Skill validators | Pass |
| Claude plugin validator | Pass |
| Claude marketplace validator | Pass |
| Cursor official plugin schema | Pass |
| Cursor official marketplace schema | Pass |
| Cross-provider source parity | Pass |
| Deterministic double build | Pass; zero mismatches |
| Package integrity and safe paths | Pass |
| Package, auth, privacy, credit, and connector tests | Pass; 96 tests |
| Public MCP protocol handshake | Pass; MCP 2025-06-18, eight read-only tools |
| Provider OAuth fail-closed challenge | Pass; three `401` responses with scoped metadata |
| Independent skill forward tests | Pass; public-only, adversarial timestamp/prompt, and 12-call credit boundary |

The only focused-suite warning is the existing `websockets.legacy`
deprecation warning.

## Release candidates

- `blocksize-market-data-openai-plugin-0.4.0.zip`
- `blocksize-market-data-claude-plugin-0.3.0.zip`
- `blocksize-market-data-cursor-plugin-1.3.0.zip`
- `use-blocksize-market-data-universal-skill-0.4.0.zip`
- `agent-skill-release-0.4.0.json`

The checksum manifest explicitly identifies this as an unsigned local build.
Artifact signing remains a release gate.

## External blockers

1. Production still reports rolled-back server version `0.6.2`; `/readyz`
   returns `404`.
2. The public GitLab and nested Cursor GitHub branches do not contain these
   corrected versions. No push was performed under the production freeze.
3. A current signed-in OpenAI, Claude, and Cursor client must each complete
   OAuth, list the seven tools, run a free discovery call, inspect credit
   balance, and perform one explicitly authorized live call.
4. Each live result must include the exact instrument, units, timestamp,
   provenance, and correct credit accounting; restart/refresh behavior must
   also pass.
5. The packages must be signed and installed from a clean public source before
   marketplace submission.

## Release boundary

Do not publish, push, or deploy these packages until the production readiness,
signed-client, clean-install, and repository-provenance gates pass under the
manual release safeguard.
