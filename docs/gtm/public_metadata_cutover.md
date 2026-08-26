# Public Metadata Cutover

The public listing metadata can now be switched without editing application code.

## Environment variables

- `PUBLIC_BASE_URL`
- `PUBLIC_REPOSITORY_URL`
- `PUBLIC_REPOSITORY_SOURCE`
- `PUBLIC_REGISTRY_NAME`

## What they control

- `PUBLIC_BASE_URL`
  Controls the live homepage, MCP manifest links, `/server.json`, quickstart links, and API docs links.
- `PUBLIC_REPOSITORY_URL`
  Overrides the repository URL advertised by the public MCP manifest and `/server.json`.
  When unset, the approved canonical GitHub repository is used. Set it to an empty
  value only when repository metadata must be intentionally omitted.
- `PUBLIC_REPOSITORY_SOURCE`
  Controls the repository source label in `/server.json`, such as `github` or `git`, when
  `PUBLIC_REPOSITORY_URL` is set.
- `PUBLIC_REGISTRY_NAME`
  Controls the registry package name served from `/server.json`.

## Recommended Blocksize cutover

When the custom domain is ready, update the Railway environment with:

```text
PUBLIC_BASE_URL=https://<blocksize-domain>
# Unset uses the approved canonical GitHub repository.
# Set PUBLIC_REPOSITORY_URL= only to intentionally omit repository metadata.
```

Only set `PUBLIC_REGISTRY_NAME` once you are ready to publish a new registry identity.

## Current limitation

Several static HTML docs still contain the old Railway and GitHub URLs in their page bodies. The runtime listing surfaces now follow environment configuration, but the static docs should still get a final content pass when the Blocksize domain is chosen.
