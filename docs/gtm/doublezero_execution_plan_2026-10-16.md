# DoubleZero Edge: execution plan for the demo, the proprietary composite feed and DoubleZero as transport

Date: 2026-10-16. Decision owner: Johann Focke. Companion to
[doublezero_edge_publisher_feasibility_2026-10-16.md](doublezero_edge_publisher_feasibility_2026-10-16.md)
and the measurement in [../../reports/ws_latency_measurement_2026-10-16.md](../../reports/ws_latency_measurement_2026-10-16.md).

## Decision

BlockSize pursues three things from the feasibility study and drops the rest.

| Pursue | Drop |
| --- | --- |
| Phase 0: demo publisher with Andrew McConnell in a private shared repo | Phase 3b: operating venue publishers (Binance or any exchange) on DoubleZero's behalf |
| Phase 2: a production Edge feed of BlockSize's own aggregated data (composite top-of-book, cross-venue VWAP, later the perpetuals composite with book, metals and FX) | Any venue-attributed raw stream, which is what exchange terms prohibit most explicitly |
| Phase 3a: DoubleZero as transport for BlockSize's own regional ingest into the aggregation engine | Any dependency on an exchange licence or a DoubleZero indemnity |

Scope rule that follows from "proprietary data only": every message BlockSize publishes carries a BlockSize-computed value and
aggregate provenance (source counts, method, quality flags, venue-set hash) but never a constituent venue's own price or size.
This keeps the Edge feed inside the redistribution rights BlockSize already relies on for its Pyth and Chainlink publishing,
and it removes the Binance-related questions from the DoubleZero conversation entirely.

Phase 1 (measure and specify) stays, because Phases 2 and 3a both depend on it.

## What the measurement changed

The 16 October capture from Austin (five minutes, BTC/ETH/SOL) settled three design inputs:

| Finding | Consequence for the plan |
| --- | --- |
| Notifications are flushed on one shared 250 ms timer across bidask, vwap and trade | The demo inherits a 0 to 250 ms batch delay. Production must tap the aggregation engine before the timer. |
| End-to-end p50 was 386 ms (trades), 414 ms (bid/ask), 566 ms (VWAP); no VWAP update arrived under 350 ms | The number to quote to DoubleZero is "about 400 to 570 ms today from the US", not 250 ms. |
| Bid/ask `ts` is a shared aggregation-tick stamp, and about half of VWAP rows are re-sends with an old `ts` | The adapter cannot use bid/ask `ts` as the venue timestamp, and it must drop re-sent VWAP rows before sequencing. BlockSize must add a per-row compute timestamp and a per-message send timestamp. |

## Workstream A: demo publisher (Phase 0, weeks 0 to 3)

Goal: a conformance-clean Edge feed of BlockSize aggregated bid/ask and VWAP for BTCUSD, ETHUSD and SOLUSD, publishing from
one host on DoubleZero testnet, built by Andrew McConnell on the public Rust crates, with BlockSize supplying the inputs.

### Shape of the demo

| Element | Choice | Why |
| --- | --- | --- |
| Repository | Private GitHub repo shared between BlockSize and DoubleZero, Rust workspace | Agreed on the call. Adapter code lives in the publisher's own repo per DoubleZero's onboarding guide. |
| Dependencies | `dz-adapter-core`, `dz-publisher-runtime`, `dz-publisher-lowering`, `dz-ingress-core` (websocket), `dz-edge-tob`, `dz-edge-refdata` from `malbeclabs/edge-multicast-ref` at git tag v0.6.0 | Nothing usable is on crates.io. |
| Ingress | BlockSize Matrix WebSocket, `authentication_logon` then `bidask_subscribe` and `vwap_subscribe` | The only transport that exists today for both sides. |
| Aggregated bid/ask | TOB `Quote` (60 bytes) with `Bid Source Count` and `Ask Source Count` filled from BlockSize's constituent counts | The spec already anticipates multi-source quotes. |
| Cross-venue VWAP | Midpoint message, `Method = 255` (custom, documented out of band), `Quality Flags` set from BlockSize freshness and outlier state | Closest existing template for a derived price with provenance. |
| Source ID | Private range 32768 to 65535 for the demo | Permitted for testing; the production Source ID is a Workstream B question. |
| Instrument IDs | `id_allocation = "derived"` (CRC-32 of the ticker) | Lets a second host join later without coordination. |
| Reference data | `InstrumentDefinition` per ticker (asset class 1 Crypto Spot, price and quantity exponents from BlockSize decimals), 30 s cycle, `ManifestSummary` once a second | Required by the two-port model. |
| Liveness | Turn BlockSize's 60 s unchanged-price re-send into `Heartbeat`; emit a snapshot-flagged full refresh every few seconds | No retransmission exists on Edge. |
| Testing | Replay of recorded WebSocket notifications through the adapter, then `dz-conformance` against the live multicast and against a `dz-recorder` pcap | Both tools are public and return CI exit codes. |

### Who does what

| Owner | Deliverable | Due |
| --- | --- | --- |
| BlockSize (Johann) | Enterprise-scoped API key for the demo with permission for DoubleZero engineers to use it; not a Developer trial key (one WebSocket connection, five feeds) | Week 0 |
| BlockSize (Ogulcan or backend) | One-page protocol note: method shapes, `ts` units and semantics per method, the 250 ms batch, the 60 s re-send, the VWAP re-send behaviour, decimals per ticker | Week 0 |
| BlockSize (this repo) | Recorded WebSocket fixtures (raw JSON notifications with receipt timestamps) for BTC/ETH/SOL, 10 to 15 minutes, for the replay tests; the measurement script already has the connection code | Week 1 |
| DoubleZero (Andrew) | Adapter, publisher binary, TOML config, multicast group and ports on testnet, egress host | Weeks 1 to 3 |
| DoubleZero | Engineers in the shared Slack channel | Week 0 |
| Both | Conformance green; first Edge Connect subscriber reading the feed in New York or Tokyo and logging the four timestamps | Week 3 |

Gate to Phase 1: conformance passes, and both sides agree in writing that a composite instrument needs either a Source ID for
BlockSize's aggregation engine or a Composite sibling spec.

## Workstream B: measure and specify (Phase 1, weeks 3 to 8)

Goal: a written latency decomposition, an agreed representation for composite instruments, and written commercial terms.

| Task | Owner | Detail |
| --- | --- | --- |
| European-vantage latency run | BlockSize | Run `scripts/measure_ws_latency.py` from a Frankfurt host and from the Railway region. Splits the remaining 260 to 440 ms into distance versus ingestion and aggregation. |
| Edge end-to-end numbers | Both | Edge Connect subscribers in New York and Tokyo reading the demo feed; compare against a subscription to DoubleZero's own Binance TOB feed as a benchmark input only, never republished. |
| Send and compute timestamps in the Matrix API | BlockSize backend | Per-message server send timestamp and per-row compute timestamp. Makes the Midpoint "Compute minus Book" and "Send minus Compute" terms measurable. |
| Composite representation | Both | Open a pull request to `malbeclabs/edge-feed-spec` for a Composite sibling spec (constituent count, venue-set hash, VWAP window, outlier-rejection flag, method byte) and request a production Source ID for BlockSize's aggregation engine. DoubleZero's guide says this path "costs weeks", so it starts in week 3, not week 8. |
| Commercial terms in writing | Johann with Austin | Builder share and payout currency (USDC option), who posts the bond and on what terms, seat pricing authority, feed-per-metro bond rule, SLA text that the `sla_hash` will reference, retirement and principal return. |
| Hosting decision | BlockSize ops | Two Frankfurt hosts on devices from different contributors, x86-64 Linux, dedicated public IPv4, no NAT, GRE and BGP permitted. Shortlist Equinix FR campus bare metal, Cherry Servers, Latitude. |

Gate to Phase 2: measured decomposition exists, spec direction agreed, terms signed, hosting quoted.

## Workstream C: production composite feed (Phase 2, weeks 8 to 20)

Goal: BlockSize's first paying Edge feed, fed from inside the aggregation engine rather than from the batched WebSocket.

### Products on the feed, in order

| Wave | Product | Edge message | Instruments | Notes |
| --- | --- | --- | --- | --- |
| 1 | Composite top-of-book (aggregated bid, ask, mid, sizes, source counts) | TOB `Quote` or Composite sibling | Top 50 to 100 USD, USDT, USDC pairs by volume, from the 1,821 symbols fresh on both VWAP and bid/ask | Event-driven, unbatched |
| 1 | Cross-venue VWAP with outlier rejection | Midpoint or Composite, quality flags | Same universe | Drop re-sends; publish only on new trade input |
| 2 | Perpetuals composite with aggregated book (planned for about November 2026 internally) | MBP (market by price) with a composite Source ID, or Composite sibling with depth | Major perps | Depends on the internal perps feed landing |
| 2 | Metals and FX reference rates | TOB `Quote`, asset class to be agreed with DoubleZero (the registry has no FX or metals class today) | XAUUSD, XAGUSD, XPTUSD, XPDUSD, major FX | Enterprise-only instruments today |
| 3 | 30-minute and 24-hour VWAP, state prices for Solana and EVM pools | Midpoint, low rate | Oracle-grade references | Suits oracle buyers, not latency buyers |

### Engineering components

| Component | Where | Effort | Notes |
| --- | --- | --- | --- |
| Pre-batch internal tap | BlockSize aggregation engine | 3 to 4 engineer-weeks backend | An unbatched, send-timestamped private WebSocket from the engine to the publisher hosts. DoubleZero's `uds` and `multicast` ingress crates do not exist yet, so a private WebSocket is the pragmatic transport; revisit if `uds` lands. |
| Production adapter | Shared repo, Rust | 2 to 3 engineer-weeks | Extends the demo: Composite spec, quality flags from engine state, re-send filtering, full-refresh cadence, two-host `derived` IDs. |
| Hosts and network | Frankfurt, two contributors | 1 to 2 engineer-weeks ops | `doublezero` package, `doublezero connect multicast --publish`, GRE on `doublezero1`, optional XDP `gre-decap`, NTP or PTP discipline on the publisher clock. |
| Observability | Both hosts | 1 engineer-week | `dz-publisher-metrics` to Prometheus, `dz-recorder` archives, alerting on sequence resets, stale inputs and egress lag. |
| Feed lifecycle | On-chain | Days, gated on DoubleZero | Feed created `Pending` with `spec_id`, `sla_hash`, committed rate; attestor verdict to `Active`; bond posted by DoubleZero or formally waived. |
| Distribution | Edge Connect registry, DoubleZero docs page, seat pricing | Days | Edge Connect lists the feed for subscribers who cannot run multicast. |

Effort total: about 8 to 12 engineer-weeks across backend, Rust and ops, plus hosting.

Gate to Phase 3a: first paying seats; bond posted by DoubleZero or formally waived; two-host redundancy proven.

## Workstream D: DoubleZero as transport for ingest (Phase 3a, weeks 12 to 30)

Goal: fresher inputs to the aggregation engine by moving BlockSize's own venue ingest in-region and carrying it to Frankfurt
over DoubleZero fibre. This is the most durable return: it improves every BlockSize product, including Pyth and Chainlink
publishing, and it survives any change to Edge's economics or token.

| Item | Detail |
| --- | --- |
| Expected gain | About 30 ms one-way on the Tokyo to Frankfurt leg (133.9 ms RTT on DoubleZero versus about 193 ms public) with microsecond jitter; Tokyo venues ingested at about 4 ms p50 instead of about 97 ms |
| Access mode | Permissioned IBRL (unicast) for BlockSize's own hosts, or a quoted private multicast group ("Multicast as a Service", priced on publishers, subscribers and bandwidth). Both are permissioned for non-validators; neither has a public price. |
| Hosts | Ingest hosts in Tokyo (Binance, Bybit via Singapore, OKX via Hong Kong as reachable) and New York (Coinbase, Kraken, Gemini) sending normalised internal messages to Frankfurt over DoubleZero |
| Licensing | None new. BlockSize already ingests these venues; this changes the path, not the data. |
| Prerequisite | Phase 1 decomposition showing how much of the remaining latency is distance |
| Effort | 8 to 12 engineer-weeks plus hosting in two more metros |
| Gate | Quote from DoubleZero; measured gain on the Tokyo leg confirmed on a pilot host before full rollout |

## Requirements for the chosen scope

| Requirement | Demo | Production composite | Transport |
| --- | --- | --- | --- |
| People | DoubleZero's Rust engineer; 0.5 week BlockSize | One Rust engineer, one backend engineer, one Linux network engineer (GRE, BGP, PIM, Prometheus) | Same ops engineer plus backend |
| Hosting | One DoubleZero-provided testnet host | Two Frankfurt hosts, different contributors, public IPv4, no NAT | Plus two in Tokyo and two in New York |
| Bandwidth | Kilobits | About 5 Mbit/s worst case for the full bid/ask universe, far below the lowest tier | Tens of Mbit/s |
| Bond capital | None | About $100,000 in 2Z per feed per metro at the lowest tier, 180-day hold. Accept only if DoubleZero posts it. | None |
| Licensing | None new (BlockSize's own aggregated outputs) | None new, provided no constituent prices are published | None new |
| Tooling | edge-multicast-ref v0.6.0, dz-conformance, dz-recorder, Edge Connect | Plus dz-publisher-metrics, PTP or NTP discipline | doublezero CLI in IBRL mode |

## Questions for DoubleZero, trimmed to this scope

For Andrew McConnell (technical):

1. Will the Source ID registry accept BlockSize's aggregation engine as a source, or does a Composite sibling spec need to merge first, and who reviews it?
2. Which asset class and exponent conventions should metals and FX use, since the registry has only crypto spot, prediction and perpetual classes?
3. When will `allow-staked-feeds` be enabled and who runs the attestor?
4. How many publisher hosts per channel does DoubleZero expect for a builder feed, and must they sit in a DoubleZero metro?
5. Is permissioned IBRL or a private multicast group available to BlockSize for its own ingest, and at what price?
6. Will a `uds` or `multicast` ingress crate ship, or should the internal tap stay on a private WebSocket?

For Austin Federa (commercial):

1. Is the 45 percent builder share contractual, paid in what currency and on what cadence, with a USDC option?
2. Does "DoubleZero posts the bond for first movers" mean a grant, a loan or a guarantee, and is the bond per feed per metro?
3. Who sets seat prices and regional tiers, and how is revenue attributed if the feed is bundled?
4. What SLA text will the `sla_hash` reference, who adjudicates, and how is principal returned at retirement?
5. Can DoubleZero share subscriber counts per feed and which subscribers have asked for cross-venue aggregates or oracle-grade inputs?

Dropped from the list: every Binance and venue-licensing question.

## Risks within this scope, ranked

| Rank | Risk | Mitigation |
| --- | --- | --- |
| 1 | Platform: builder payout and bond terms are pre-launch and discretionary; DoubleZero has changed fee splits unilaterally before | Written terms before Phase 2 spend; no BlockSize capital in the bond; Phase 3a value is independent of Edge |
| 2 | Spec: composite representation is unresolved and the spec path "costs weeks" | Start the spec PR in week 3; ship the demo on TOB plus Midpoint 255 without waiting |
| 3 | Token: rewards accrue in 2Z, at an all-time low with heavy weekly vesting; German business income at receipt | USDC payout clause, or sell on receipt; tax records per the BMF letter of 6 March 2025 |
| 4 | Internal engineering: the pre-batch tap is the hardest piece and sits inside BlockSize's engine | Scope it in Phase 1 with the backend team; the demo does not depend on it |
| 5 | Operational: no retransmission, no publisher SLA, attachment reliability equals the hosting contributor's | Two hosts on different contributors; full refresh cadence; recorder archives |
| 6 | Commercial: head-to-head freshness against DoubleZero's own 4 ms venue feeds | Sell quality (outlier rejection, cross-venue checks, quality flags) to oracle and HIP-3 buyers, not raw speed |
| 7 | Residual licensing: Coinbase's terms treat indexes as "Derived Works" | Publish aggregates only, never constituent prices; this is the same posture as the Pyth and Chainlink publishing today |

## This week

1. Issue the Enterprise-scoped demo key and confirm DoubleZero's engineers may use it.
2. Send Andrew the technical questions above and the protocol note once Ogulcan has reviewed it.
3. Send Austin the commercial questions and ask for a draft of the builder terms.
4. Record the WebSocket fixtures for the replay tests and schedule the European-vantage latency run.
5. Ask the backend team for a first estimate of the pre-batch tap and the send timestamp.
