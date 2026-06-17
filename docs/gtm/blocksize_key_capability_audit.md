# Blocksize Key Capability Audit

Audit date: 2026-06-15

Methodology:

- Used the configured Blocksize API key from local settings.
- Called repo-known JSON-RPC methods and likely state-data method variants.
- Reassessed the public Matrix API docs at
  `https://matrix.blocksize.capital/api-docs.json` against the current gateway
  integration.
- Tested sample symbols through the current client: `BTCUSD`, `ETHUSD`, `SOLUSD`,
  `JUPUSD`, `PYTHUSD`, `MSOLUSD`, `JUPSOLUSD`, `WSTETHUSD`, `EURUSD`,
  `XAUUSD`, and `AAPL`.
- Did not print secrets or authentication material.

## Working with this key

| Capability | RPC method | Status | Notes |
| --- | --- | --- | --- |
| VWAP instrument discovery | `vwap_instruments` | Working | Returns an `instruments` catalog. |
| Bid/ask instrument discovery | `bidask_instruments` | Working | Returns an `instruments` catalog. |
| Current crypto/token VWAP | `vwap_latest` | Working | Confirmed for `BTCUSD`, `ETHUSD`, `SOLUSD`, `JUPUSD`, `PYTHUSD`, and also returned `EURUSD`. |
| Current bid/ask snapshot | `bidask_getSnapshot` | Working | Confirmed for `BTCUSD`, `ETHUSD`, `SOLUSD`, `JUPUSD`, `PYTHUSD`, `EURUSD`, and `XAUUSD`. |
| Metals through bid/ask | `bidask_getSnapshot` | Working | Confirmed `XAUUSD`; `vwap_latest` for `XAUUSD` is not supported. |
| State instrument/pool catalog | `state_instruments` | Working | Returned 223 instruments with pool addresses and networks, including Solana pools. |
| State pool price | `state_pool` | Working by pool/symbol coverage | Public docs expose pool-level HTTP state, not ticker-level state. Confirmed through client-derived state prices for `MSOLUSD`, `JUPSOLUSD`, and `WSTETHUSD`; some individual Solana pools can timeout upstream. |
| 30-minute close | `closingprice_list` | Working | Public docs expose this HTTP method. The client now maps `get_vwap_30min()` to `closingprice_list`. |
| Closing-price trade evidence | `closingprice_trades` | Working | Public docs expose this HTTP method for trade inputs to a close. |

## Not working with this key

| Capability | RPC method tested | Result |
| --- | --- | --- |
| Ticker-level state/reference price | `state_price_latest` | `-32601 method not found`; use `state_instruments` + `state_pool` instead. |
| State price variants | `state_getSnapshot`, `state_snapshot`, `state_getPrice`, `state_latest`, `state_getLatest`, `state_price`, `state_price_getSnapshot`, `state_oracle_latest` | `-32601 method not found` |
| Legacy 30-minute VWAP alias | `vwap_30min_latest` | `-32601 method not found`; use `closingprice_list` instead. |
| 24-hour VWAP over HTTP | `vwap_24h_latest` | `-32601 method not found`; fixed by serving `/v1/vwap24h/{pair}` from the local `fixedvwap_subscribe` websocket cache when enabled. |
| VWAP-window discovery | `vwap_30min_instruments`, `vwap_24h_instruments` | `-32601 method not found` |
| Equity-specific catalog | `bidask_equity_instruments` | `-32601 method not found` |
| Equity price quality for `AAPL` | `bidask_getSnapshot` | Method returns, but parsed `AAPL` bid/ask values were `0.0`; do not position equities as confirmed until symbol coverage is validated. |

## Product decision

Move forward now with products that rely on:

- `vwap_latest`
- `bidask_getSnapshot`
- `vwap_instruments`
- `bidask_instruments`
- `state_instruments`
- `state_pool` for matching protocol/pool state symbols
- `closingprice_list` for optional 30-minute close metrics

Do not make plain ticker-level state price, oracle confidence, 24-hour HTTP
VWAP, perps funding, or generic pool liquidity mandatory in paid products until
the upstream key and docs expose those methods.

The trader products now default to live VWAP, bid/ask, freshness, spread, and
no optional feeds. State instrument/pool coverage, state_pool price, and
closing-price metrics are explicit opt-ins.

Use `POST /v1/capabilities/check` before paid indicator calls to confirm whether
the requested symbols have required current-market coverage and any requested
optional feed coverage. State instrument matching is exact to the requested pair
or common stable-quote variants, so unrelated symbols such as `SOLVBTCUSD` do
not count as `SOLUSD` coverage.

Important state-data distinction:

- `SOLUSD`, `JUPUSD`, and `PYTHUSD` have strong current market-data coverage.
- State-pool coverage is concentrated in protocol/pool symbols such as
  `MSOLUSD`, `JUPSOLUSD`, `JUPUSDUSD`, `JITOSOLUSD`, `BSOLUSD`, `VSOLUSD`,
  and `WSTETHUSD`.
- `state_divergence_indicator` should be demonstrated with state-covered
  symbols such as `MSOLUSD`, not plain `SOLUSD`.
