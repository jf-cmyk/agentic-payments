# Growth execution tickets

Date: 2026-07-23

This pack translates the approved growth plan into two operating epics: the agent activation/monetization loop and the three-feed RWA promotion pilot. The companion CSV is import-ready and uses local IDs because the authoritative Jira project and issue keys are not connected in this workspace.

## Implemented in the current work package

- Privacy-safe identity attribution for discovery, payment and delivery events.
- Activation, under-three-minute time-to-value, mature seven-day repeat and starter-to-paid metrics.
- Growth Funnel and RWA Pilot sections in the protected production command center.
- Full local integration regression from discovery through starter exhaustion and verified x402 delivery.
- Three-feed RWA capture, raw replay history, monitoring thresholds and non-automatic promotion policy.
- Production background scheduler enabled at a 30-minute interval with observations persisted on the Railway volume.
- Successful scheduled RWA captures are written to the queryable observation ledger as candidate-monitoring records; they cannot auto-promote.
- Configured Ethereum/Base RPC fallback lists are tried before public fallback RPCs.
- One-click first-live-price activation uses a stable browser agent identity and preserves the honest HTTP 402 upgrade path.
- Implementation-ready integrations for LangChain, LlamaIndex, Vercel AI SDK, OpenAI Agents SDK, GOAT and Solana Agent Kit pass repository validation.
- Six public discovery listings were reachable on 2026-07-23; the Awesome MCP submission is merged.
- Weekly growth operating runbook.

## External actions

- Jira import/reconciliation needs the Atlassian project and issue keys.
- Future directory listing edits need the corresponding signed-in accounts; current public listing URLs are live.
- Marketplace-side metrics need APIs or reviewed exports.
- Real post-starter payment validation needs a fresh acquisition environment or a signed payment proof.
- RWA rights and promotion decisions require legal, commercial and named human approval.

## Current RWA pilot state

The latest production capture succeeded for AAPL/USDC, PAXG/USDC and EURC/USDC on 2026-07-23. The first production PAXG attempt exposed a missing Ethereum RPC variable; the configured RPC was added and the next capture passed 3/3. The minimum 14-day and 672-sample gates remain open, and production-promoted expansion feeds remain zero.
