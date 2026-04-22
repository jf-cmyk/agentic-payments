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
  Controls the repository URL advertised by the public MCP manifest and `/server.json`.
- `PUBLIC_REPOSITORY_SOURCE`
  Controls the repository source label in `/server.json`, such as `github` or `gitlab`.
- `PUBLIC_REGISTRY_NAME`
  Controls the registry package name served from `/server.json`.

## Recommended Blocksize cutover

When the custom domain is ready, update the Railway environment with:

```text
PUBLIC_BASE_URL=https://<blocksize-domain>
PUBLIC_REPOSITORY_URL=https://gitlab.com/<group>/<project>
PUBLIC_REPOSITORY_SOURCE=gitlab
```

Only set `PUBLIC_REGISTRY_NAME` once you are ready to publish a new registry identity.

## Current limitation

Several static HTML docs still contain the old Railway and GitHub URLs in their page bodies. The runtime listing surfaces now follow environment configuration, but the static docs should still get a final content pass when the Blocksize domain is chosen.
