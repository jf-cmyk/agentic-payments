# RWA Coverage and Client-Usage Reconciliation — 2026-07-16

## Decision-useful counts

The catalog has **3,407 token-deployment rows**, **1,150 unique RWA ticker strings**, and **1,025 canonical economic asset IDs**. The 1,025 figure is the distinct `asset_id` field; `rwa_xyz_asset_id` and `rwa_xyz_ticker` are both 1,150 in the current catalog. Deployment rows, wrapper tickers, and canonical underlyings are therefore different grains.

The apparent **168 versus 215** conflict is a clean coverage increment:

- The source-all master has 709 candidate-priced token rows, 168 unique ticker strings, and 90 canonical economic assets.
- The public xStocks probe found 74 exact positive issuer/reference prices. Twenty-seven of those tickers already had another candidate lane; 47 were new.
- Therefore, **168 + 47 = 215** tickers with at least one candidate lane after xStocks. This is not 215 executable feeds.
- The remaining access buckets are mutually exclusive at ticker-string grain: 215 candidate now, 14 mapped-pool/decoder next, 545 venue API or new pool required, and 376 issuer NAV/onchain-rate required. They sum to 1,150.
- **Production promoted remains zero.** No candidate has passed the full freshness, replay, depth, manipulation, rights, benchmark, and consensus gates.

## Semantics inside the 215 candidate count

The 168 pre-xStocks candidates partition by their selected venue into 35 Hyperliquid tokenized/RWA spot book candidates, 33 Jupiter route-quote snapshots, 19 onchain pool current-state candidates, 53 native-L2 perpetual candidates, and 28 Gains/Ostium synthetic or reference derivative candidates. The 47 new xStocks references are a disjoint coverage increment.

From the canonical spot/oracle perspective, the conservative split is:

| Stage | Tickers | What the number permits |
|---|---:|---|
| Spot execution-shaped candidate | 68 | 35 native spot books + 33 router quote snapshots; still not production-ready |
| Reference/current-state/derivative candidate | 147 | 19 pool-state + 53 derivative L2 + 28 synthetic derivatives + 47 issuer references |
| Production-promoted | 0 | Nothing may yet be described as a production replacement oracle |

The 53 derivative books are executable for their own perpetual contracts, not for the tokenized spot wrapper. The 19 current pool adapters are not counted as exact block-size VWAP because the local probe metadata describes synthetic/current-state depth rather than full tick/invariant replay. Jupiter remains a size-specific route quote snapshot, not direct pool-state replay. Public xStocks has neither native source time nor executable depth.

## Actual Tiingo/client denominator and overlap

The supplied workbook contains 40,898 normalized equity tickers and 153 FX pairs, or 41,051 data points. Clients use a **union of 11,472**: 11,395 equities and 77 FX pairs, for 27.95% utilization. Replacement work should target this union and then rank it by revenue/criticality; a revenue weighting is not present in the local files.

Client counts must not be added. Canonical membership is:

| Exclusive membership | Data points |
|---|---:|
| RedStone only | 10,254 |
| Pyth only | 119 |
| Supra only | 2 |
| RedStone + Pyth only | 985 |
| RedStone + Supra only | 3 |
| Pyth + Supra only | 2 |
| RedStone + Pyth + Supra | 107 |
| **Union** | **11,472** |

These exclusive cohorts reproduce the workbook client totals: RedStone 11,349; Pyth 1,213 (1,136 equity + 77 FX); Supra 114; API3 zero. The equity tab has 11,412 used rows but 11,395 canonical tickers, so 17 duplicate normalized rows are correctly removed from the replacement denominator.

## Data-quality judgment

The catalog and access tables are suitable for planning if their grains remain explicit. The highest analytical risk is semantic overstatement: `BidAsk` or `VWAP` labels in candidate masters do not by themselves prove executable spot depth, commercial redistribution rights, or production readiness. The workbook is suitable for client-demand sizing at canonical-ticker/pair grain, but it does not contain client revenue, SLA tier, request volume, or feed criticality; those are required before prioritizing cancellation coverage by value rather than count.

Supporting detail is in `reports/rwa_coverage_and_client_usage_reconciliation_2026-07-16.csv`.
