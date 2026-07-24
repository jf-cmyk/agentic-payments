# RWA pilot alignment snapshot

Captured: 2026-07-24T04:55:39Z

Status: **point-in-time evidence only — no promotion gate completed**.

| Pilot feed | Comparison reference | Timestamp alignment | Observed basis | Evidence decision |
| --- | --- | ---: | ---: | --- |
| AAPL/USDC on Hyperliquid | Blocksize AAPL bid/ask | Failed: 3,275.25s gap, 90s maximum | +81.15 bps | Not timestamp-aligned |
| PAXG/USDC on Uniswap | Blocksize XAU/USD | Passed: 1.26s gap | -9.13 bps | Pass |
| EURC/USDC on Aerodrome | Blocksize EUR/USD | Passed: 2.44s gap | +0.86 bps | Pass |

The AAPL price difference is retained for audit but is not accepted as alignment evidence because the underlying-equity reference was stale relative to the continuously traded tokenized venue. XAU/USD and EUR/USD are explicit economic proxies, not identical instruments.

The scheduled production pilot now repeats and persists these comparisons every 30 minutes. This advances the evidence window but does not confirm independent source lineage, licensing/redistribution rights, directly matched benchmarks, manipulation resistance, or human promotion approval. Pilot runtime sources remain Hyperliquid, Ethereum RPC and Base RPC; Tiingo is not a runtime dependency.
