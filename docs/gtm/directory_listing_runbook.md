# Directory Listing Runbook

Prepared: 2026-04-21

## Goal

Get Blocksize listed anywhere a public remote MCP server can be discovered without relying on direct outreach.

## What is now implemented in the repo

### Public listing surface

- Public remote MCP endpoint at `/mcp/server`
- Public manifest at `/mcp/manifest.json`
- Official MCP Registry `server.json` at repo root and `/server.json`
- Glama claim file at `/.well-known/glama.json`
- Public quickstart page
- Public prompt examples page
- Public privacy policy page
- Public support page

### Product positioning cleanup

- Portal copy now distinguishes:
  - public remote MCP discovery
  - paid HTTP API
  - advanced local MCP
- Removed public claims for unsupported commodity endpoints
- Aligned docs around the actual API and install paths

## Current submission posture by channel

### 1. Official MCP Registry

Status: ready once public deployment is live

What exists:

- `server.json`
- public Streamable HTTP endpoint
- GitLab repository metadata

What still requires approval:

1. Deploy the current changes publicly.
2. Install `mcp-publisher`.
3. Authenticate with the registry publisher flow.
4. Run `mcp-publisher publish`.

Recommended namespace:

- `io.gitlab.jfocke/agentic-payments`

Reason:

- This matches the current GitLab repository namespace and the public metadata served by `/server.json`.

### 2. Smithery

Status: ready once public deployment is live

Docs used:

- Smithery publish docs say URL publishing works for already hosted Streamable HTTP servers.

What still requires approval:

1. Sign in to Smithery.
2. Start the URL-based publish flow at `smithery.ai/new`.
3. Enter the public MCP URL.
4. Complete scanning and metadata confirmation.

### 3. Glama

Status: partially implemented, claim-ready once public deployment is live

What exists:

- `/.well-known/glama.json` response with maintainer email

What still requires approval:

1. Create or sign into a Glama account using the same maintainer email.
2. Wait for Glama to detect the claim file.
3. Claim and enrich the connector listing.

### 4. Anthropic Connectors Directory

Status: artifacts prepared, submission should be treated as policy-risky

Why risky:

- Anthropic's current MCP Directory Policy says MCP servers may not be used to transfer money, cryptocurrency, or other financial assets, or execute financial transactions on behalf of users.
- The public remote MCP surface in this repo is now discovery-only, which reduces risk, but the overall product is still tightly tied to crypto-paid data access.

What still requires approval:

1. Decide whether to submit despite the policy risk.
2. If yes, fill out Anthropic's review form manually.
3. Be ready to provide:
   - remote MCP URL
   - privacy policy
   - support contact
   - prompt examples
   - reviewer guidance

### 5. OpenAI

Status: no public directory flow identified in current docs

What is ready:

- Public remote MCP URL for ChatGPT Developer Mode and remote MCP usage
- OpenAI-style `search` and `fetch` tools on the public discovery server

What still requires approval:

- None for a public listing flow, because no public listing flow was identified.
- Builders can self-serve by importing the remote MCP into ChatGPT Developer Mode.

## Required approval checkpoints

### Approval checkpoint A

Deploy the code publicly so the new MCP endpoint and docs exist on the live Railway URL.

### Approval checkpoint B

Allow installation and use of the official `mcp-publisher` tool so the MCP Registry submission can be executed.

### Approval checkpoint C

Sign into Smithery and complete the URL publishing flow.

### Approval checkpoint D

Sign into Glama with the maintainer email and claim the connector.

### Approval checkpoint E

Decide whether to submit to Anthropic despite the current policy risk around financial flows.

## Source notes

- OpenAI docs support remote MCP in Developer Mode and describe `search` / `fetch` expectations for ChatGPT connectors and deep research.
- Cursor docs say Streamable HTTP is supported for remote MCP servers.
- Anthropic docs say remote MCP servers must be publicly exposed over HTTP and that Streamable HTTP is preferred.
- Anthropic directory policy requires privacy, support, prompt examples, annotations, and a reviewer path.
- Smithery docs say already-hosted Streamable HTTP servers can be published via the URL method.
- Glama docs say ownership can be claimed by publishing `/.well-known/glama.json`.
