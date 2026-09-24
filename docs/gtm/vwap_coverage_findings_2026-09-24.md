# Why crypto VWAP audited at 35% and what changed

Date: 2026-09-24. Evidence: `docs/gtm/instrument_quality_audit_2026-09-24.csv`
(7,148 VWAP tickers probed), a direct `vwap_subscribe` websocket probe, and the
upstream API docs at `matrix.blocksize.capital/api-docs.json`.

## Finding

The VWAP engine is honest. The catalog overstates coverage.

- `vwap_instruments` returns every pair for which any venue or pool exists. 63%
  of the 7,148 entries are DEX-only pool pairs (Uniswap v2/v3/v4, Balancer,
  Curve, SushiSwap), many of them single-pool cross pairs such as `1INCHDYDX`
  or `3CRVMIM`.
- 2,391 tickers (33%) return `-32603 internal error: ticker X not found` from
  `vwap_latest`. The websocket `vwap_subscribe` returns no snapshot row for them
  either. The engine has no aggregate for these pairs; they should not be
  listed.
- 2,237 tickers (31%) return a real VWAP whose last print is older than five
  minutes. The websocket re-sends the same old timestamp, so this is genuine
  inactivity, not transport lag. Of these, 556 have a live bid/ask quote.
- Multi-venue, exchange-backed pairs are fine: pairs with three or more CEX
  venues are 94% to 96% fresh within five minutes. Single-venue CEX pairs are
  51% fresh at five minutes and 91% within one hour.

| Catalog slice | Pairs | OK at 5 min | Stale | Not found |
| --- | ---: | ---: | ---: | ---: |
| CEX-backed, 3+ venues | 1,164 | 95% | 5% | 0% |
| CEX-backed, 1 to 2 venues | 1,504 | 61% | 36% | 3% |
| DEX-only, 1 pool | 2,103 | 4% | 20% | 76% |
| DEX-only, 2+ pools | 2,376 | 18% | 55% | 27% |

## What changed in the product

1. **Coverage gate** (`src/vwap_coverage.py`, data in
   `src/data/vwap_live_coverage.json`, written by
   `scripts/audit_instrument_quality.py`). `BlocksizeClient.list_vwap_instruments`
   now drops the 2,391 engine-unknown tickers, so search, instrument lists, the
   public MCP tools, and the pre-payment catalog check never advertise a pair
   that cannot return a price. The gate only removes tickers explicitly marked
   unavailable; a missing file means no filtering.
2. **Honest recommendation.** For crypto pairs whose VWAP is low-activity and
   which have a bid/ask quote, search now recommends `bidask` and reports
   `vwap_activity` plus `vwap_last_trade_age_seconds`. VWAP-only quiet pairs
   are labelled `readiness: low_activity`.
3. **Visible metadata.** `/v1/coverage` and the vwap instrument listing
   (HTTP and MCP) carry `live_coverage_gate` with the audit date and counts.

Effective advertised VWAP catalog: 4,757 pairs, of which 2,520 were fresh
within five minutes at audit time and 2,237 are labelled low activity.

## Upstream follow-ups for Blocksize

- Remove or flag the 2,391 `vwap_instruments` entries that `vwap_latest` does
  not know. The list is the `unavailable` array in the gate file.
- Consider exposing last-trade time or an activity flag on `vwap_instruments`
  so the catalog can self-describe instead of relying on a periodic audit.

## Operating the gate

Re-run `scripts/audit_instrument_quality.py` (full run, about six minutes) to
refresh the CSV, the report, and the gate file together. Commit the regenerated
`src/data/vwap_live_coverage.json`. `VWAP_COVERAGE_GATE_ENABLED=false` turns
the gate off without a deploy.
