# RWA Aggregator Buildout

This document tracks the durable path for continuously adding RWA feeds,
venues, and exchanges to the Blocksize market-data aggregator.

## Current Core

- `src/rwa_coverage.py`: coverage catalog, block-size defaults, source-type
  rules, quality thresholds, and venue roadmap.
- `src/rwa_pricing.py`: source-independent VWAP, bid/ask, quality, benchmark
  drift, and outlier calculations.
- `src/rwa_adapters.py`: venue adapter protocol, registry, planned adapter
  metadata, Kraken xStocks REST adapter, Hyperliquid adapters, and Jupiter
  route quote-sweep adapter.
- `src/rwa_aggregator.py`: operational todos, registry status, and
  quality-gated aggregation over normalized observations.
- `src/rwa_symbol_registry.py`: canonical asset and venue registry, symbol
  alias resolution, venue aliases, and coverage lookup across naming
  conventions.
- `src/rwa_non_crypto_feeds.py`: generated VWAP and bid/ask feed catalog for
  all known non-crypto rows, with tokenized-stock exclusion and Blocksize
  comparison targets.
- `src/rwa_blocksize_benchmark.py`: live Blocksize benchmark resolver plus
  Blocksize state-reference methodology for supplemental consensus evidence.
- `src/rwa_equity_universes.py`: sourceability plan for full S&P 500 coverage
  plus APAC, UK/Europe, Canada, Australia, and Singapore listed equities.
- `src/rwa_consensus.py`: consensus source plan, quality-weighted consensus
  metric, source-family weighting, and per-source basis receipts.
- `src/rwa_hyperliquid_discovery.py`: live Hyperliquid `meta`/`spotMeta`
  discovery, crypto/RWA classification, coverage deltas, and report generation
  for all currently tradeable perps and spot pairs.
- `src/rwa_provider_catalog.py`: continuously extensible provider catalog for
  tokenized securities, DEX liquidity, licensed exchanges, vendors, oracles,
  issuers/NAV sources, and futures-derived fair-value providers.
- `src/rwa_dex_allowlist.py`: route/pool allowlist candidates, required DEX
  identifiers, liquidity/price-impact thresholds, blockers, and promotion jobs.
- `src/rwa_source_readiness.py`: credential, identifier, licensing, legal,
  benchmark, storage, scheduler, and production dependency readiness.
- `src/rwa_solana_discovery.py`: live Jupiter/Solana token-mint discovery and
  route allowlist evidence for xStocks, EURC, USDY, OUSG, and Solana DEX
  candidates.

## Public Surfaces

- `GET /v1/rwa/build-plan`: build phases, block sizes, first-wave venues, and
  quality rules.
- `GET /v1/rwa/coverage`: filterable symbol and venue coverage.
- `GET /v1/rwa/assets`: asset sourcing matrix grouped across all registry
  venues.
- `GET /v1/rwa/oracle-parity`: Pyth/Chainlink-style parity targets and
  sourcing gaps.
- `GET /v1/rwa/provider-catalog`: provider catalog ingestion roadmap with
  category/status filters, access requirements, adapter lanes, and promotion
  gates.
- `GET /v1/rwa/source-readiness`: source-readiness checklist for API keys,
  RPC/indexers, token/pool IDs, oracle feed IDs, exchange/vendor licenses,
  issuer access, Blocksize benchmarking, storage, scheduler, and policy gates.
- `GET /v1/rwa/blocksize-state-methodology`: Blocksize state-reference source
  contract, upstream methods, observation shape, and consensus usage rules.
- `GET /v1/rwa/consensus/sources`: primary, oracle, benchmark, futures, NAV,
  issuer, and reserve source layers for consensus metrics.
- `POST /v1/rwa/consensus/calculate`: quality-weighted consensus value,
  reliability score, per-source basis, and inclusion/exclusion flags.
- `GET /v1/rwa/dex-venues`: high-quality DEX route/pool candidates, source
  semantics, seed coverage, and promotion gates.
- `GET /v1/rwa/dex-allowlist`: executable DEX route/pool allowlist with
  candidate filters, required identifiers, blockers, benchmark sources, and
  promotion jobs.
- `GET /v1/rwa/non-crypto-feeds`: generated non-crypto VWAP and bid/ask feed
  definitions, excluding tokenized-stock/xStock rows by default, with
  Blocksize benchmark mapping for comparison.
- `GET /v1/rwa/discovery`: per-feed discovery and promotion audit showing
  token/contract, pool/route, state-instrument, liquidity, freshness,
  manipulation, issuer NAV, Blocksize benchmark, rights, and replayability
  gates before a row can become production live liquidity.
- `GET /v1/rwa/discovery/mitigation-plan`: research-backed blocker mitigation
  plan with affected feed counts, evidence requirements, source-specific
  playbooks, execution phases, and immediate actions.
- `GET /v1/rwa/source-rights`: rights-to-source register separating internal
  benchmark sourcing, production redistribution, provider/license blockers,
  policy acknowledgements, and source-specific next actions.
- `GET /v1/rwa/replay-inventory`: route/pool replay inventory with pool or
  route IDs, token identifiers, fee tiers or curve parameters, context
  slots/blocks, replay payload fields, raw payload availability, and remaining
  promotion blockers.
- `GET /v1/rwa/blocker-resolution`: production blocker-resolution ledger that
  separates resolved rights, replay evidence, source-access gaps, and external
  blockers that still require RPC, issuer, or live-window evidence.
- `GET /v1/rwa/equity-universes`: S&P 500 and global listed-equity
  sourceability, feed shapes, sample symbols, and licensing blockers.
- `GET /v1/rwa/registry`: canonical asset/venue coverage registry with
  optional symbol and venue filters.
- `GET /v1/rwa/resolve`: resolves symbols such as `AAPL`, `AAPL/USD`,
  `AAPLx/USD`, `AAPLUSD`, `EURUSD`, or `PAXG-USDC` into canonical assets and
  venue coverage.
- `GET /v1/rwa/registry/venues`: explains what each venue covers, including
  source tier, supported data, symbols, asset classes, and legal/quality notes.
- `GET /v1/rwa/sourcing/jobs`: per-symbol sourcing queue for oracle-parity
  gaps, grouped by venue, status, and endpoint family.
- `POST /v1/rwa/sourcing/probe`: bounded execution of `ready_to_probe`
  sourcing jobs, with normalized observations, optional depth VWAP,
  real-time quality checks, and optional persistence.
- `GET /v1/rwa/feeds`: adapter registry, readiness, and remaining todos.
- `POST /v1/rwa/vwap/calculate`: block-size VWAP over normalized depth.
- `POST /v1/rwa/bidask/calculate`: normalized bid/ask and spread scoring.
- `POST /v1/rwa/quality/check`: MAD and benchmark-drift checks.
- `POST /v1/rwa/aggregate`: quality-gated consolidation over submitted
  normalized observations.
- `POST /v1/rwa/feeds/promotion-check`: promotion gate for moving feeds
  between trust tiers.
- `GET /v1/rwa/realtime/requirements`: venue and asset freshness/cadence
  thresholds.
- `POST /v1/rwa/realtime/quality`: live usability gate for each submitted
  observation.
- `POST /v1/rwa/observations/store`: replayable observation ledger with raw
  payload hash, normalized hash, quality outputs, and metadata.
- `GET /v1/rwa/observations`: recent replayable observations with symbol,
  venue, and limit filters.
- `GET /v1/rwa/observations/summary`: compact persistence stats by venue and
  symbol.
- `POST /v1/rwa/benchmark/blocksize`: live benchmark check against Blocksize
  market-data feeds used by agentic-payment workflows.

## Adapter Completion Checklist

Every new feed must complete this checklist before it can be promoted:

1. Implement the `RWAFeedAdapter` protocol.
2. Register the adapter in `build_default_registry`.
3. Normalize source type as one of `native_l2`, `native_l1`,
   `synthetic_depth`, `synthetic_l1`, `quote_sweep`,
   `price_stream_no_book`, `licensed_consolidated_tape`,
   `licensed_exchange_feed`, or `nav_reference`.
4. Return venue timestamps for real-time observations.
5. Record `previous_timestamp` or `tick_interval_ms` for cadence checks.
6. Add mocked payload tests for ticker, depth, mark, trade, or quote data.
7. Add a live probe only after API/legal access is confirmed.
8. Run real-time quality checks and benchmark checks before aggregation.
9. Promote from `planned` to `implemented_unprobed`, then `supplemental`, then
   `replacement_candidate` only after backtesting and signoff.

## Remaining To-Dos

| ID | Priority | Status | Scope |
| --- | --- | --- | --- |
| adapter-contract | P0 | complete | Common normalized feed interface |
| registry | P0 | complete | Central venue registry and status surface |
| canonical-symbol-registry | P0 | complete | Cross-venue symbol/alias resolver and venue coverage service |
| non-crypto-feed-catalog | P0 | complete | VWAP/bid-ask feed definitions and Blocksize comparison targets |
| equity-universe-sourcing | P0 | complete | S&P 500, APAC, UK/Europe, Canada, Australia, and Singapore sourceability plan |
| consensus-source-plan | P0 | complete | Primary, oracle, benchmark, futures, NAV, issuer, and reserve source layers |
| consensus-metric | P0 | complete | Quality-weighted consensus value and per-source basis receipt |
| kraken-xstocks-rest | P0 | complete | Ticker and depth adapter with mocked tests |
| aggregation-policy | P0 | complete | Quality-gated aggregation policy |
| provider-catalog-ingestion | P0 | complete | Canonical provider onboarding jobs for tokenized venues, DEXs, licensed exchanges, vendors, oracles, futures, issuers, NAV, and reserve sources |
| source-readiness-config | P0 | complete | Machine-readable dependency checklist for credentials, identifiers, licenses, issuer access, benchmark access, and production controls |
| solana-token-route-discovery | P0 | partial | Jupiter token mint registry and route evidence generated; non-tradable or unverified symbols remain review holds |
| consensus-window-supervisor | P0 | planned | Rolling 1m/5m/30m consensus receipts by feed |
| ostium-adapter | P1 | planned | Builder API bid/mid/ask and simulated depth |
| gains-adapter | P1 | planned | Price stream, recent trades, trade VWAP |
| hyperliquid-paxg-adapter | P1 | complete | Public Hyperliquid `l2Book` bid/ask and block-size VWAP for PAXG/USD |
| hyperliquid-rwa-spot-adapter | P1 | complete | Public Hyperliquid `spotMeta`/`l2Book` coverage for 31 tokenized RWA/traditional spot pairs |
| hyperliquid-tradeable-discovery | P0 | complete | Live Hyperliquid `meta`/`spotMeta` report adds 177 active perps and 310 spot pairs into coverage |
| hyperliquid-generic-adapters | P1 | complete | Generic public `l2Book` adapters for discovered Hyperliquid perp and spot rows |
| dex-pool-allowlist | P0 | complete | Route/pool candidate queue with required identifiers, liquidity, volume, price-impact, freshness, benchmark, and manipulation gates |
| jupiter-router-adapter | P1 | implemented_blocked_on_token_catalog_or_api_key | Jupiter `/swap/v1/quote` bid/ask and marginal quote-sweep VWAP adapter |
| solana-dex-adapters | P1 | partial | Jupiter route adapter implemented; Raydium, Orca, and Meteora pool-state adapters remain planned |
| evm-dex-adapters | P1 | planned | Uniswap, Curve, Balancer, and Aerodrome pool/indexer adapters |
| persistence | P1 | complete | Store raw payload hashes and normalized receipts |
| bounded-probe-runner | P1 | complete | Execute ready sourcing jobs on demand with quality checks |
| scheduler | P1 | planned | Polling/websocket supervisor, retries, health metrics |
| jupiter-xstocks-adapter | P2 | planned | Quote sweeps with route-plan provenance |
| ondo-stocks-adapter | P2 | planned | Whitelisted quote/price stream and catalog |
| backed-xstocks-issuer-reference | P2 | planned | Issuer catalog, token contracts, attestations |
| tradfi-benchmark-reference | P1 | planned | Licensed NBBO/trade/reference data |
| us-equity-consolidated-feed | P0 | planned | Licensed U.S. equity feed for full S&P 500 NBBO/trade VWAP |
| hkex-equity-feed | P1 | planned | HKEX OMD-C/vendor feed for Hong Kong listed equities |
| china-a-share-feed | P1 | planned | SSE/SZSE/China Connect vendor feed for China A-shares |
| krx-equity-feed | P1 | planned | KRX/vendor feed for South Korean listed equities |
| jpx-equity-feed | P1 | planned | JPX/TSE vendor feed for Japanese listed equities |
| twse-equity-feed | P1 | planned | TWSE/TPEx vendor feed for Taiwanese listed equities |
| india-equity-feed | P1 | planned | NSE/BSE vendor feed for Indian listed equities |
| lse-lseg-equity-feed | P1 | planned | LSE/LSEG vendor feed for UK listed equities |
| euronext-xetra-equity-feed | P1 | planned | Euronext and Deutsche Boerse/Xetra feeds for continental Europe |
| tsx-equity-feed | P1 | planned | TSX/TSXV/TMX vendor feed for Canadian listed equities |
| asx-equity-feed | P1 | planned | ASX vendor feed for Australian listed equities |
| sgx-equity-feed | P1 | planned | SGX vendor feed for Singapore listed equities |
| pyth-oracle-reference | P1 | planned | Pyth catalog, confidence, rates, macro, NAV parity |
| chainlink-oracle-reference | P1 | planned | Chainlink heartbeat/deviation, NAV, PoR, tokenized assets |
| bybit-xstocks-adapter | P2 | blocked_on_access | Requires regional/API access confirmation |
| promotion-gates | P0 | complete | Legal, quality, and benchmark promotion workflow |
| blocksize-benchmarking | P0 | complete | Live Blocksize benchmark comparator for sourced data |

## Operating Rule

Do not blend source types silently. True lit order-book depth, synthetic depth,
quote-swept routes, price-stream marks, and NAV references must remain labeled
through every calculation, receipt, and downstream API response.

## Naming Rule

All callers should resolve through `/v1/rwa/resolve` or `/v1/rwa/registry`
before sourcing. The registry canonicalizes venue and ticker naming differences
such as `AAPL`, `AAPL/USD`, `AAPLx/USD`, `AAPLUSD`, `EURUSD`, `PAXG-USDC`,
`Meteora DLMM`, `uniswap v3`, and `jupiter`. Adapters should add native venue
symbols to the canonical registry instead of introducing endpoint-specific
symbol assumptions.

## Non-Crypto Feed Rule

Use `/v1/rwa/non-crypto-feeds` to generate the non-crypto VWAP and bid/ask
feed catalog before live sourcing. It excludes tokenized-stock/xStock rows by
default. Each feed definition includes the source venue, source type, canonical
asset, endpoint template, block-size defaults, and Blocksize benchmark target
where a comparable existing feed exists. Venue observations must still pass
real-time quality, Blocksize comparison, and persistence before aggregation.

## Listed Equity Universe Rule

The full S&P 500 is sourceable, but not from tokenized-stock venues alone. It
requires a licensed U.S. equity consolidated/direct feed with current
constituents, NBBO/quotes, tick trades, corporate actions, and redistribution
terms. APAC, UK/Europe, Canada, and Australia equities are also sourceable, but
each requires the respective exchange/vendor data license before real-time
bid/ask or trade-VWAP feeds can be promoted. Use
`/v1/rwa/equity-universes` to track these universes and generated feed shapes.

## Real-Time Quality Rule

An observation is not real-time usable unless it has a source timestamp, passes
the stricter of venue and asset freshness thresholds, and its tick interval is
inside the venue cadence envelope. Treasury/NAV feeds can be fresh references,
but they must never pass as tick-by-tick feeds.

## Blocksize State Reference Rule

Blocksize state data is a supplemental reference leg, not executable liquidity.
Use `source_type=blocksize_state_reference`, `venue=blocksize_state`, and
`benchmark_service=state` when adding these rows to consensus or benchmark
runs. The value should come from `/v1/state/{pair}`, which prefers
`state_subscribe` cache snapshots and falls back to `state_instruments` plus
`state_pool`. State coverage is symbol-specific; tokenized RWA mappings need an
explicit `benchmark_symbol` or `state_symbol` before use. A fresh state row can
support alignment, fallback context, and divergence detection, but it cannot
replace two independent primary market sources.

The current expansion adds Blocksize state-reference candidates for BUIDL,
OUSG, USDY, TBILL, USTB, USCC, and PAXG. These rows require
`state_instruments` coverage confirmation before live use.

## Oracle Parity Rule

Pyth and Chainlink parity requires more than tokenized equities. The target
coverage includes equities, ETFs, FX, metals, energy commodities, rates, macro
data, NAV, proof-of-reserve, tokenized assets, and benchmark/reference metadata.
Oracle sources are benchmark/reference inputs for parity checks unless a product
license explicitly permits redistribution.

## Provider Catalog Rule

Use `/v1/rwa/provider-catalog` before adding a new venue or source family.
Every source must enter the catalog with an access model, adapter lane,
endpoint family, target source type, licensing/auth status, and promotion gate.
The catalog is intentionally broader than the live adapter registry: it tracks
tokenized-security venues, DEX pools/routes, licensed exchange feeds, market
data vendors, oracle networks, issuer/NAV/reserve references, and futures
fair-value providers before any source is promoted into real-time aggregation.

## Source Readiness Rule

Use `/v1/rwa/source-readiness` before live sourcing or promotion. This endpoint
shows what is still missing without exposing secret values: API keys, RPC and
indexer URLs, verified token mints, pool IDs, oracle feed IDs, exchange/vendor
licenses, issuer or partner access, Blocksize benchmark access, futures model
inputs, production storage, scheduler controls, alerting, and redistribution
policy signoff. A source may be benchmarked in isolation with mocked or bounded
inputs, but it cannot be promoted into real-time consensus until its dependency
rows, identity mappings, legal rights, freshness, replayability, and benchmark
alignment gates pass.

## Solana Discovery Rule

Use `scripts/run_rwa_solana_discovery.py` after `JUPITER_API_KEY` and
`SOLANA_RPC_URL` are configured. The script writes
`reports/rwa_solana_token_mints.json` and
`reports/rwa_jupiter_route_allowlist.json` with token mints, token verification
flags, liquidity/organic-score metadata, route labels, AMM keys, context slots,
price impact, and route-level errors. These artifacts make Jupiter probing
repeatable, but they are not final promotion approval: issuer identity, token
tradability, route concentration, liquidity, manipulation, and Blocksize or
regulated benchmark alignment still need to pass. Solana pool-state adapters
for Raydium, Orca, and Meteora still require dedicated pool/whirlpool/DLMM
identifiers beyond the Jupiter route evidence.

## Sourcing Execution Rule

The sourcing queue is generated from `/v1/rwa/oracle-parity`, discovered
Hyperliquid RWA spot candidates, and the live Hyperliquid tradeable feed
report. Jobs marked `ready_to_probe` can run with implemented adapters. Jobs marked
`planned_adapter` require adapter implementation before live calls. Jobs marked
`blocked_by_auth_or_license` require API keys, venue access, or redistribution
review before execution. Use `include_completed_targets=true` when probing
venues that are already present in coverage, such as Hyperliquid `PAXG/USD`,
`BTC/USD`, or `AAPL/USDC`.

## Hyperliquid Rule

Hyperliquid is implemented in four lanes:

1. `hyperliquid_paxg`: PAXG-specific public REST source using
   `POST https://api.hyperliquid.xyz/info` with
   `{"type":"l2Book","coin":"PAXG"}`. It is a native L1/L2 source for
   `PAXG/USD`, with up to 20 order-book levels per side. It must not be used
   as a generic XAU, XAG, XPT, or XPD venue.
2. `hyperliquid_rwa_spot`: public `spotMeta` plus `l2Book` `@index` source for
   31 active tokenized RWA/traditional spot candidates, including tokenized
   equities, ETFs, fiat/stable assets, `XAUT0`, `THBILL`, and private-market
   candidates. These rows are sourceable, but remain supplemental until
   identity, issuer, liquidity, and Blocksize/regulated-benchmark checks pass.
3. `hyperliquid_perps`: live `meta` discovery for all active perp markets.
   The current report adds 177 active perp rows, including 176 crypto perps and
   `PAXG/USD` as a metal/RWA overlap row; 55 delisted perp markets are retained
   in the report but excluded from coverage rows.
4. `hyperliquid_spot`: live `spotMeta` discovery for all spot pairs. The
   current report adds 310 spot rows, including 263 crypto rows and 34
   RWA/traditional spot rows in broad Hyperliquid spot coverage. These rows use
   `l2Book` `@pair_index` identifiers and remain candidates until freshness,
   depth, manipulation, identity, and benchmark gates pass.

## Consensus Rule

Use `/v1/rwa/consensus/sources` to track every source layer and
`/v1/rwa/consensus/calculate` to calculate the consensus receipt for a feed
window. The consensus value is a quality-weighted mean after timestamp,
spread/depth, confidence, benchmark-drift, and MAD outlier exclusions. Oracle,
futures, Blocksize state, NAV, issuer, and proof-of-reserve rows are valid
supplemental sources, but they remain labeled and cannot masquerade as
executable market liquidity.

## DEX Quality Rule

High-quality DEX data is valuable, but it must never be treated as a native
order book. Jupiter-style router quotes are `quote_sweep` sources. Raydium,
Orca, Meteora, Uniswap, and Aerodrome concentrated-liquidity pools are
`onchain_clmm_pool` sources. Curve and Balancer stable pools are
`onchain_stableswap_pool` sources. Promotion requires verified token contracts,
pool allowlists, minimum liquidity and organic volume, slot/block freshness,
price-impact ceilings, manipulation checks, benchmark comparison, and
replayable route or pool-state payloads.

Use `/v1/rwa/dex-allowlist` to convert DEX seed coverage into executable
promotion jobs. Each candidate must carry the venue, chain, source type,
required pool/route identifiers, asset-class liquidity and volume minimums,
benchmark sources, unresolved blockers, and the promotion gates it must pass
before contributing to consensus or aggregation.

The Jupiter router adapter is implemented as a quote-sweep source using
`/swap/v1/quote`. It becomes probe-ready only when token mints are configured
or `JUPITER_API_KEY` is available for token search. Its returned levels are
marginalized from cumulative route quotes and must remain labeled
`quote_sweep`.

Coverage expansion now includes candidate DEX lanes for TBILL, USTB, and USCC
across Uniswap, Curve, Balancer, and Aerodrome where relevant. These candidates
move the assets out of reference-only planning, but each remains blocked on
token-contract verification, pool discovery, liquidity/volume measurement,
freshness, manipulation checks, and issuer NAV/attestation alignment.

## Discovery Promotion Rule

Every sourced RWA feed starts as candidate or supplemental coverage. A symbol
row, Blocksize state row, token mint, DEX route quote, pool seed, or
point-in-time order-book probe is evidence, not production promotion.

Use `/v1/rwa/discovery` and `scripts/run_rwa_feed_discovery.py` to inspect the
gate state for every generated VWAP and bid/ask feed. Promotion to live
liquidity is blocked until all required gates are `passed`: canonical
identity, venue identifier, token/contract discovery when applicable,
route/pool discovery when applicable, Blocksize state-instrument confirmation
when applicable, liquidity/depth/volume, 30-minute freshness and tick cadence,
manipulation/concentration checks, issuer NAV or reserve alignment when
applicable, Blocksize benchmark alignment, rights/redistribution clearance, and
replayable raw payloads.

For TBILL, USTB, USCC, and Blocksize state-reference rows specifically,
`scripts/run_rwa_blocksize_state_discovery.py` must first confirm matching
`state_instruments`. Even when a state instrument exists, those rows remain
supplemental until state-pool freshness, issuer NAV/attestation alignment,
stale-value/manipulation checks, and benchmark drift checks pass.

Use `/v1/rwa/discovery/mitigation-plan` and
`scripts/run_rwa_discovery_mitigation.py` to turn the audit blockers into
actionable remediation. The mitigation plan maps every gate to root cause,
target state, required evidence, acceptance criteria, and source-specific
implementation notes. Current high-priority mitigations are:

- State rows: keep TBILL, USTB, USCC, BUIDL, OUSG, USDY, and PAXG blocked
  unless live `state_instruments` and `state_pool` evidence matches the symbol.
- DEX rows: verify token mint/contract identity, discover route or pool ids,
  persist replayable quote/pool payloads, and run block-size fillability tests.
- Liquidity rows: capture order-book, route, or pool depth across target
  notionals, then measure fill ratio, slippage, spread, and organic 24h volume.
- Freshness rows: run continuous 30-minute windows for freshness, latency, tick
  cadence, stale gaps, and benchmark drift.
- Manipulation rows: apply route diversity, holder/pool concentration, MAD
  outlier, stale-value, and cross-source deviation checks.
- Fund/NAV rows: reconcile market quotes to issuer/admin NAV, fees, reserves,
  redemption constraints, and attestation timestamps.
- Legal rows: clear provider access, data-plan terms, redistribution rights,
  attribution, retention, and storage limits before production use.

## Rights And Replay Closure

Use `/v1/rwa/source-rights` and `scripts/run_rwa_source_rights.py` to separate
three states that must not be collapsed:

- Technical sourcing: the adapter can query a public/API/RPC source or replay a
  local artifact for internal benchmarking.
- Supplemental consensus: the source can be used only as a labeled reference,
  not executable liquidity.
- Production redistribution: provider/API/RPC/issuer terms, exchange or vendor
  licenses, data retention, attribution, entitlement, and redistribution rights
  are cleared and recorded.

Current rights posture is cleared and evidence-backed by
`reports/rwa_rights_clearance.json`: all registered venues have production
redistribution rights recorded, while source access and technical promotion
remain separately gated. Missing pool allowlists, RPC/API access, issuer/NAV
evidence, replay payloads, live quality windows, and benchmark alignment still
block production promotion.

Use `/v1/rwa/replay-inventory` and `scripts/run_rwa_replay_inventory.py` to
track whether each DEX candidate has the exact replay fields required for
promotion. Jupiter rows can carry route plans, AMM keys, context slots, token
mints, price impact, sweep quotes, and raw payload artifacts. EVM and Solana
pool-state rows remain blocked until pool allowlists provide pool IDs, fee
tiers or curve parameters, token contracts/mints, block or slot state, and
replayable pool-state payloads.

Current blocker-resolution state:

- Rights are cleared for all registered venues and no feed is blocked by
  `rights_and_redistribution`.
- Solana pool discovery has derived pool IDs from Jupiter route plans and
  captured RPC account-state hashes/slots for 21 pools; 12 registry pool rows
  now have replay-ready pool-state evidence pending fee/tick/bin decoders and
  live quality windows.
- EVM public pair discovery has identified 3 pool identities: Aerodrome
  EURC/USDC on Base, Balancer PAXG/USDC on Ethereum, and Uniswap PAXG/USDC on
  Ethereum. Aerodrome EURC/USDC and Uniswap PAXG/USDC now have block-tagged
  RPC pool-state receipts, matching token contracts, fee tiers, tick spacing,
  liquidity, and hashed raw `slot0` payloads. Balancer PAXG/USDC has a block
  receipt but still needs a Balancer weighted-pool decoder for balances and
  weights.
- The replay inventory has 24 replay-ready candidates: 10 Jupiter route rows,
  12 Solana pool-state rows, and 2 EVM CLMM pool-state rows. The remaining
  replay blockers are 31 missing pool allowlist rows, 4 failed Jupiter route
  discoveries, and 1 incomplete Balancer pool-state row.
- Source rights are cleared for all 14 registered venues; 9 are currently
  source-access ready, while the remaining technical access gaps are dedicated
  production EVM RPC/indexer access and issuer NAV/reserve packs.
- No feed is production-promoted until continuous 30-minute freshness,
  liquidity, manipulation, and benchmark windows pass.

## Derivative Venue Expansion

Use `scripts/run_rwa_derivative_venue_discovery.py` to refresh the perp,
options, stock-perp, and yield-market catalog batch requested for Ostium,
Aster, Lighter, Drift, GRVT, dYdX, Extended, Pacifica, ApeX Omni, Vest,
Helix, EnclaveX, SynFutures, MYX, Orderly, Derive, Aevo, Plume, Coinbase
Ventures, Solana, Pendle, Tradible, and Cork.

The current discovery report is written to
`reports/rwa_derivative_venue_discovery.json` and classifies each venue as
public-catalog sourceable, planned RPC/indexer work, or blocked/gated access.
Public catalogs are sourceable candidates from Aster, Lighter, dYdX, Orderly,
Aevo, ApeX Omni, Derive/Lyra, and Pendle. Drift protocol constants are mapped,
but live Drift books/state require Solana RPC, DLOB or market account replay,
and oracle/state timestamps before production use. GRVT, Extended, Pacifica,
Vest, Helix, EnclaveX, SynFutures, MYX, Plume, Solana, Tradible, and Cork are
registry/provider rows until a confirmed API, subgraph, RPC/indexer, or partner
source is configured. Coinbase Ventures is not a trading venue; source
Coinbase market data through concrete Coinbase Exchange/Derivatives or other
licensed venues instead.

Derivative methodology:

- Native perp/futures/order-book rows are first-class derivative-liquidity
  observations, not spot prices.
- For executable derivative VWAP, walk the venue L2 book by block size and
  retain venue market id, contract specs, mark, index/oracle, funding, open
  interest, and raw payload hash.
- For a spot/fair-value proxy, adjust perp/futures mid or VWAP for premium,
  expected funding carry, financing curve, dividends or yield, storage,
  convenience yield, fees, collateral currency, contract multiplier, expiry,
  and roll/settlement calendar as applicable.
- Keep the derived value labeled `futures_fair_value` or derivative-derived
  supplemental evidence until it passes Blocksize benchmark alignment, live
  freshness, liquidity, manipulation, and rights gates.

## Full Roadmap

1. Coverage target: maintain `/v1/rwa/oracle-parity` as the target map for
   Pyth/Chainlink-like breadth across equities, ETFs, FX, metals, energy,
   rates, macro, NAV, and proof-of-reserve.
2. Provider catalog ingestion: run `/v1/rwa/provider-catalog` to expand the
   source universe and separate `ready_to_probe`, `planned_adapter`, and
   `blocked_by_auth_or_license` work across venues, vendors, DEXs, oracles,
   issuers, NAV sources, reserve sources, and futures providers.
3. Source readiness: run `/v1/rwa/source-readiness` to resolve missing keys,
   RPC/indexers, token/pool IDs, oracle feed mappings, exchange/vendor
   licenses, issuer access, Blocksize benchmark access, futures inputs, storage,
   scheduler, alerting, and redistribution-policy gates.
4. Solana discovery: run `scripts/run_rwa_solana_discovery.py` to produce the
   reviewed-candidate Solana token registry and Jupiter route evidence before
   promoting quote-sweep sources.
5. Blocksize state discovery: run
   `scripts/run_rwa_blocksize_state_discovery.py` to confirm state-reference
   rows against `state_instruments`; keep unmatched rows as candidate-only.
6. Feed discovery audit: run `/v1/rwa/discovery` or
   `scripts/run_rwa_feed_discovery.py` to make every sourced feed's blockers
   explicit before live probing or aggregation.
7. Mitigation planning: run `/v1/rwa/discovery/mitigation-plan` or
   `scripts/run_rwa_discovery_mitigation.py` to convert blockers into
   source-specific execution tasks and acceptance criteria.
8. Rights gate: run `/v1/rwa/source-rights` and record provider/API/RPC,
   issuer, exchange, vendor, retention, attribution, and redistribution
   approvals before any production promotion.
9. Replay gate: run `/v1/rwa/replay-inventory` to confirm every route/pool row
   has route plans or pool IDs, token identifiers, fee tiers/curve parameters,
   block/slot state, and raw replay payloads.
10. Sourcing queue: run `/v1/rwa/sourcing/jobs` to create per-symbol/per-venue
   fetch jobs, starting with `ready_to_probe` jobs.
11. Daily RWA feed agent: run `scripts/run_rwa_daily_feed_agent.py` or inspect
   `/v1/rwa/daily-feed-agent` every day. This refreshes RWA.xyz, diffs against
   yesterday's normalized report, writes `reports/rwa_daily_feed_agent.json`,
   writes `reports/rwa_daily_new_tokens.csv`, and creates per-token sourcing
   actions for newly detected contracts.
12. RWA.xyz monitor refresh: run
   `scripts/run_rwa_xyz_monitor_discovery.py` or inspect
   `/v1/rwa/rwa-xyz-monitor` to ingest new tokenized products, issuers,
   token contracts, platforms, networks, total-asset-value context, and
   primary-market metadata. Treat these rows as catalog/reference coverage
   until venue/pool discovery and real-time quality gates pass.
13. Feed generation: run `/v1/rwa/non-crypto-feeds` to create VWAP and bid/ask
   feed definitions for all eligible non-crypto rows, excluding tokenized-stock
   rows unless explicitly approved.
14. Listed equity universe setup: run `/v1/rwa/equity-universes` to plan full
   S&P 500 coverage plus APAC, UK/Europe, Canada, Australia, and Singapore
   equity feeds.
15. Probe execution: run `/v1/rwa/sourcing/probe` for bounded `ready_to_probe`
   jobs. Use `persist: true` for sourcing runs that should enter the replay
   ledger.
16. Derivative venue discovery: run
   `scripts/run_rwa_derivative_venue_discovery.py`, then execute
   `/v1/rwa/sourcing/jobs` for derivative venue jobs to capture market ids,
   books, trades, mark/index, funding, contract specs, and fair-value inputs.
17. DEX quality setup: run `/v1/rwa/dex-venues`, create the token/pool allowlist,
   and wire Jupiter, Raydium, Orca, Meteora, Uniswap, Curve, Balancer, and
   Aerodrome as supplemental DEX sources.
18. Adapter execution: implement and probe adapters in this order:
   Kraken xStocks, Hyperliquid PAXG and RWA spot, Ostium, Gains, Jupiter xStocks, Ondo
   Stocks, global licensed equity feeds, DEX route/pool sources, Backed issuer
   metadata, Treasury NAV, Pyth, Chainlink, Bybit, then derivative venues by
   public-catalog readiness.
19. Real-time gate: every observation must pass `/v1/rwa/realtime/quality`
   before aggregation.
20. Consensus receipt: run `/v1/rwa/consensus/calculate` for each feed window
   to record the consensus value, reliability score, per-source basis, and
   source inclusion flags.
21. Blocksize benchmark: every sourced observation with a comparable Blocksize
   feed must pass `/v1/rwa/benchmark/blocksize`. Use
   `benchmark_service=state` only for Blocksize state-covered symbols.
22. Aggregation: only then feed observations into `/v1/rwa/aggregate`.
23. Promotion: run `/v1/rwa/feeds/promotion-check` before marking any source
   supplemental, benchmark, or replacement-candidate.
24. Persistence: store raw payload hash, normalized observation, real-time
   quality result, Blocksize benchmark result, and promotion decision through
   `/v1/rwa/observations/store`. Benchmark runs can set `persist: true` to
   write comparable observations directly into the replay ledger.

## Blocksize Benchmark Rule

Blocksize remains the benchmark for agentic-payment workflows. Any RWA feed
that maps to an existing Blocksize symbol must be compared in basis points
against the live Blocksize API. `pass` means drift is below warning threshold,
`warn` means usable with caution, and `exclude` means the observation should
not enter consolidated pricing. State-backed Blocksize comparisons are
available for covered `/v1/state/{pair}` symbols and should be labeled
`blocksize_state_reference`.
