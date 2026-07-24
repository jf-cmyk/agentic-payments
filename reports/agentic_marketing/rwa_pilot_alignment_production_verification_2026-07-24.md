# RWA pilot alignment production verification

Deployment: `11476065-34e2-4f29-81e3-ba9c87366560`

Release commit: `c80041f`

Verified: 2026-07-24

## Production result

The first scheduler cycle captured 3/3 pilot feeds, persisted 3/3 observations, and produced timestamp-aligned comparisons for 3/3. The newest public ledger records contained:

| Feed | Runtime source | Evidence decision | Basis | Promoted |
| --- | --- | --- | ---: | --- |
| AAPL/USDC | Hyperliquid venue order book | Warn | +51.86 bps | No |
| PAXG/USDC | Ethereum RPC pool state | Pass | -18.83 bps | No |
| EURC/USDC | Base RPC pool state | Pass | +1.11 bps | No |

The comparisons are evidence inputs, not a completed independent-benchmark gate. AAPL uses Blocksize AAPL, PAXG uses XAU/USD as an explicit proxy, and EURC uses EUR/USD as an explicit proxy. Direct instrument matching, upstream lineage, benchmark-source independence, rights, full-window stability, depth/manipulation review and human approval remain open.

## Release checks

- Complete hosted smoke suite passed.
- No production HTTP 5xx responses appeared after rollout.
- New deployment logs contained zero low-level HTTP request records and zero credential URL markers.
- `production_promoted=false` remained set on all three ledger records.
- Pilot runtime sources remain Hyperliquid, Ethereum RPC and Base RPC; Tiingo is not a runtime dependency.
