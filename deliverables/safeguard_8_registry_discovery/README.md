# Safeguard 8 — Registry, discovery, crawler, and command-center acceptance

Date: 2026-07-30

## Decision

The 0.6.5 candidate passes local registry, MCP discovery, crawler, documentation,
and command-center release-truth acceptance. The public ecosystem does not yet
represent that candidate consistently, so registry publication and production
promotion remain blocked.

## Defects remediated

- Made `/mcp/manifest.json` support `GET` and `HEAD` and derive its eight tools,
  JSON schemas, descriptions, and read-only annotations from the running
  FastMCP server rather than a manually maintained list.
- Made the canonical MCP configuration use the hosted Streamable HTTP transport.
- Added deterministic, bounded pagination to product and instrument discovery,
  including `total`, `returned`, `offset`, `limit`, `has_more`, and
  `next_offset` metadata.
- Added catalog provenance, snapshot time, freshness, and content-hash metadata
  without presenting response-assembly time as source freshness.
- Added cache validators to the well-known discovery documents, including
  `ETag`, `Cache-Control`, `HEAD`, and conditional `304` behavior.
- Corrected crawler policy so MCP/API/internal routes are excluded while public
  documentation remains discoverable; sitemap entries now have stable,
  versioned `lastmod` values and public pages have explicit canonical or
  `noindex` policy.
- Repaired broken public documentation and image links, and included the two
  missing manual diagrams in the release allowlist.
- Synchronized the public README, external docs, quickstart, and all provider
  skill copies to the same eight-tool public surface:
  `search_pairs`, `list_instruments`, `get_pricing_info`,
  `get_product_catalog`, `get_workflow_endpoint`,
  `get_market_data_endpoint`, `search`, and `fetch`.
- Separated current 2026-07-30 RWA catalog claims from the explicitly dated
  2026-07-22 historical evidence snapshot.
- Added an independently audited release-truth field to each distribution
  platform in the Product Usage Command Center. Listing presence and traffic no
  longer imply that an external package or registry version is current.
- Removed false public-install assertions for unpublished OpenAI, Claude, and
  Cursor packages and marked older prepaid/bulk GTM material as superseded.

## Accepted locally

| Gate | Result |
| --- | --- |
| Public resource-server suite | Pass; 253 tests |
| Focused registry/discovery/crawler suite | Pass; 33 tests |
| Full repository suite | Pass; 829 tests |
| Manifest-to-runtime tool parity | Pass; exact eight-tool match |
| Registry schema and description integrity | Pass |
| Public links, sitemap URLs, and packaged images | Pass |
| Deterministic package rebuild | Pass; byte-identical archives |
| Command-center release-truth regression | Pass |

The full suite reports two non-failing upstream deprecation warnings: the
`websockets.legacy` namespace and FastMCP's Authlib JOSE import.

## Current external release truth

Observed with read-only probes on 2026-07-30.

| Surface | Observed state | Decision |
| --- | --- | --- |
| Production | `/health` is `200` and version `0.6.2`; `/readyz` is `404` | Online but stale and not readiness-gated |
| Official MCP Registry | Latest is `0.6.3`; older entries identify GitLab, latest omits repository provenance | Active but behind the candidate |
| Canonical GitHub `main` | `server.json` is `0.6.4`; no Actions workflow; `main` is unprotected | Behind and not a protected release source |
| GitLab mirror | `server.json` is `0.6.2`; latest mirror commit is from 2026-05-20 | Historical mirror, not an install source |
| Pay.sh | Four-route x402 baseline is live; expanded listing is not accepted | Keep the verified baseline |
| Smithery and Glama | Reachable, but external product claims are stale | Do not treat listing health as product-truth validation |
| x402scan | Four-route catalog is stale | Refresh only after the canonical release |
| Awesome MCP | Listing is merged but points to a stale wrapper | Correct after the canonical release exists |

## Command-center assessment

The local command center can now distinguish local attributed traffic,
availability of external metrics, listing state, observed version, audit date,
and release truth. That makes it useful for operational review and release
drift detection.

It still cannot establish marketplace conversion or distribution performance:
no authenticated marketplace metric feed is configured, and the updated
release-truth model is not live while production remains on 0.6.2. External
platform entries are point-in-time audit evidence until ingestion and freshness
monitoring are operating.

## Release boundary

Do not publish 0.6.5 registry metadata or provider packages until the clean
source, protected CI, staging, artifact, signed-client, and manual production
gates in Safeguard 9 pass. Publication must happen after production reports the
same version and commit, not before.
