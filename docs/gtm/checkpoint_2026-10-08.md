# Checkpoint, 2026-10-08 (start of day)

Start a new chat from this file. It records what is live, what is in flight, what is waiting on whom, and how to continue. Yesterday's checkpoint is `checkpoint_2026-10-07.md`; the longer background is `status_and_open_tasks_2026-10-06.md` and `execution_plan_2026-10-06.md`.

## 1. Production state

| Item | Value |
|---|---|
| Version | 0.6.24, commit 78fb856, healthy, `/readyz` ready |
| Railway | auto-deploys every push to main, no CI wait. `RAILPACK_VERSION` is 0.40.1 and matches `.railway/railway.ts`; the build log reports `railpack-v0.40.1` |
| Merged 7 Oct, all deployed | #78 402 landing page · #84 bundle of #79 example rotation, #80 client errors, #81 Railpack pin, #82 RWA pilot endpoint, #83 shared-pool recovery · #85 OpenAI plugin 0.7.1 · #86 review walkthrough video and pricing-free listing description |
| Resend | `blocksize.info` **Verified** since the evening of 7 Oct. Test email from `Blocksize <hello@blocksize.info>` delivered; the welcome mail to a new signup was delivered the same evening. Tracking subdomain is `track`, never `mcp` |
| Smithery usage ingestion | live: `SMITHERY_QUALIFIED_NAME=blocksize/agentic-payments` and `SMITHERY_API_KEY` set; the first daily collection succeeded at 21:20 UTC on 7 Oct |
| Claude plugin directory | plugin 0.6.0 **published** by Johann on 7 Oct; the MCP connector submission is still in Anthropic review |
| OpenAI plugin directory | **In review** since 7 Oct, plugin `plugin_asdk_app_6ac6c3f81bb08191a09faa3cb61a2df3`, manifest 0.7.1. Business verification accepted, domain verified (`OPENAI_APPS_CHALLENGE_TOKEN` on Railway), MCP authorized with 18 tools discovered, reviewer credentials saved, walkthrough at `https://mcp.blocksize.info/evidence/openai-plugin-review-walkthrough.mp4`. One soft finding ("server instructions need further review") is with OpenAI |
| Cursor marketplace, Pay.sh PR 208, MCP Registry 0.6.24, Glama | unchanged, waiting on the other side |
| Free tier | 30,000 credits a month; 1 grant (owner) plus the OpenAI reviewer demo user; 275 credits used this month |

## 2. What the 7 Oct engineering changed (for anyone reading the dashboard)

- Every 402 now carries `sample_value`, `price` (USDC and credits), `client_snippet` and `free_connector`. Measure `starter_credits` and `free_connector` follow-through after a week.
- Published example instruments rotate weekly on `/.well-known/x402`; example URLs in docs and listings carry `selection_source=published_example_path`; usage insights report `copied_example_calls` and compute `top3_share` on calls that chose their instrument.
- `/mcp/server` accepts `*/*` and partial Accept headers; a session-less call gets a how-to body; paid 400s carry `error_code`, `fix`, `charged: false` and an `example_request`; connector 401s carry a `sign_in` block. Expect the 17 percent client-error share to drop.
- The RWA pilot block moved to `/internal/observability/rwa-pilot`; `/internal/observability/stats` is about 3 MB lighter and the alerts feed no longer computes it.
- The shared free-tier pool now recovers stale reservations on every balance read and `/health` summary.

## 3. Waiting on whom

- **OpenAI review.** Watch for the review mail. On approval: publish from the plugin page, then **re-enable Device Trust in Clerk** (Protect, Rules, Device Trust, Manage); it was switched off on 7 Oct so reviewers can sign in with a password from a new device. On rejection: fix the manifest, rebuild with `scripts/build_agent_skill_packages.py`, re-upload via "Upload new version" on the versioned plugin page.
- **Anthropic connector review**, **Cursor publisher application**, **Pay.sh PR 208**: unchanged.

## 4. Remaining engineering (none urgent)

1. Payment failures (P0 in usage insights): 29 percent of submitted x402 proofs fail (invalid bound signature 39, facilitator unavailable 21 in 30 days). Consider a second facilitator or a buyer-side retry-after.
2. Rate limiting rejects 2.8 percent of non-monitor requests with 429; review the discovery limits.
3. Static examples in connector prompts and the plugin READMEs still lead with BTC; diversify when those files next change.
4. `usage_events` still loads every event of the window into memory for stats; the roll-up past 185 days has not run yet (nothing is that old).
5. The ChatGPT "Add custom MCP server" flow could not be driven from the Chrome extension (the consent opens in a popup); a real ChatGPT screen capture could replace the rendered walkthrough later by swapping the file in `docs/evidence`.

## 5. Working rules confirmed on 7 Oct

- Main is protected: PR only, `test` must pass on a branch up to date with main. Bundle independent, individually green branches into one integration PR before asking Johann to merge (done as #84); serial single PRs cost an 8-minute rerun each.
- Commit with `-c user.name="Johann Focke" -c user.email="jf@blocksize-capital.com"`; fetch GitHub main from the `ghssh` remote (`git fetch ghssh main`), not `origin` (GitLab). Push over SSH; `gh` via `GH_TOKEN=$(gh auth token --user jf-cmyk)`.
- A new file under `docs/evidence` must be added to `ALLOWED_PUBLIC_DOC_FILES` in `scripts/verify_release_artifact.py` or the "Verify release artifact" CI step fails.
- Every Railway variable change triggers a redeploy; after any Railway settings change, check that "Auto deploys when pushed to GitHub" is still on.
- OpenAI's listing checks reject pricing, subscription and promotion wording; the short description is limited to 30 characters; `interface.supportURL`, `logo` and `composerIcon` are required; review test cases and `demo_recording_url` import from the manifest.

## 6. Johann's open actions

1. Watch for the OpenAI and Anthropic review mails; re-enable Clerk Device Trust after OpenAI's decision.
2. Delete the merged local branches and worktrees in the main checkout (`.claude/worktrees/*`), and the local checkpoint copy that blocks `git merge --ff-only ghssh/main` there.
3. Monday: "Allow always" when each routine asks for its command.

## 7. Kickoff prompt for the new chat

```text
Read docs/gtm/checkpoint_2026-10-08.md. Check the OpenAI plugin review status at https://platform.openai.com/plugins in Chrome and the usage dashboard for the first week of the 402 landing page and the client-error fixes, then start on the remaining engineering items in section 4 in order, one PR each, bundling green branches before handing them over. Do not merge, deploy or submit anything external without asking.
```
