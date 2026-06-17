from __future__ import annotations

import asyncio
import json
from typing import Any

from src.blocksize_client import BlocksizeAPIError, BlocksizeClient


SAMPLE_SYMBOLS = [
    "BTCUSD",
    "ETHUSD",
    "SOLUSD",
    "JUPUSD",
    "PYTHUSD",
    "MSOLUSD",
    "JUPSOLUSD",
    "WSTETHUSD",
    "EURUSD",
    "XAUUSD",
    "AAPL",
]
STATE_SAMPLE_SYMBOLS = {"MSOLUSD", "JUPSOLUSD", "WSTETHUSD"}

RPC_PROBES: list[tuple[str, dict[str, Any]]] = [
    ("vwap_instruments", {}),
    ("bidask_instruments", {}),
    ("bidask_equity_instruments", {}),
    ("vwap_latest", {"ticker": "SOLUSD"}),
    ("bidask_getSnapshot", {"ticker": "SOLUSD"}),
    ("bidask_getSnapshot", {"ticker": "EURUSD"}),
    ("bidask_getSnapshot", {"ticker": "XAUUSD"}),
    ("bidask_getSnapshot", {"ticker": "AAPL"}),
    ("closingprice_list", {"ts": 1746576000000, "quote": "USD"}),
    ("closingprice_trades", {"base": "SOL", "quote": "USD", "ts": 1746576000000}),
    ("vwap_30min_latest", {"ticker": "SOL"}),
    ("vwap_24h_latest", {"ticker": "SOLUSD"}),
    ("state_price_latest", {"ticker": "SOLUSD"}),
    ("state_instruments", {}),
    ("state_price_instruments", {}),
    ("vwap_30min_instruments", {}),
    ("vwap_24h_instruments", {}),
]


def summarize_result(result: Any) -> dict[str, Any]:
    if isinstance(result, list):
        sample = result[:8]
        return {"type": "list", "count": len(result), "sample": sample}
    if isinstance(result, dict):
        summary: dict[str, Any] = {"type": "object", "keys": sorted(result.keys())[:20]}
        if isinstance(result.get("instruments"), list):
            instruments = result["instruments"]
            summary["instrument_count"] = len(instruments)
            summary["instrument_sample"] = instruments[:5]
        for key in ("ticker", "price", "vwap", "agg_bid_price", "agg_ask_price", "bid", "ask", "timestamp", "ts"):
            if key in result:
                summary[key] = result[key]
        if "snapshot" in result and isinstance(result["snapshot"], list):
            summary["snapshot_count"] = len(result["snapshot"])
            summary["snapshot_sample"] = result["snapshot"][:3]
        return summary
    return {"type": type(result).__name__, "value": result}


async def probe_rpc(client: BlocksizeClient, method: str, params: dict[str, Any]) -> dict[str, Any]:
    try:
        result = await client._rpc_call(method, params)
        return {"method": method, "params": params, "status": "ok", "result": summarize_result(result)}
    except BlocksizeAPIError as exc:
        return {
            "method": method,
            "params": params,
            "status": "error",
            "error_code": exc.code,
            "message": exc.message,
        }
    except Exception as exc:
        return {
            "method": method,
            "params": params,
            "status": "error",
            "error_code": type(exc).__name__,
            "message": str(exc),
        }


async def probe_client_methods(client: BlocksizeClient) -> dict[str, Any]:
    method_results: list[dict[str, Any]] = []
    for symbol in SAMPLE_SYMBOLS:
        item: dict[str, Any] = {"symbol": symbol}
        for label, call in (
            ("vwap_latest", lambda s=symbol: client.get_vwap_latest(s)),
            ("bidask_snapshot", lambda s=symbol: client.get_bidask_snapshot(s)),
        ):
            try:
                data = await call()
                item[label] = {"status": "ok", "data": data.model_dump(mode="json")}
            except BlocksizeAPIError as exc:
                item[label] = {"status": "error", "error_code": exc.code, "message": exc.message}
            except Exception as exc:
                item[label] = {"status": "error", "error_code": type(exc).__name__, "message": str(exc)}
        if symbol in STATE_SAMPLE_SYMBOLS:
            try:
                data = await client.get_state_price(symbol)
                item["state_price_from_pool"] = {"status": "ok", "data": data.model_dump(mode="json")}
            except BlocksizeAPIError as exc:
                item["state_price_from_pool"] = {
                    "status": "error",
                    "error_code": exc.code,
                    "message": exc.message,
                }
            except Exception as exc:
                item["state_price_from_pool"] = {
                    "status": "error",
                    "error_code": type(exc).__name__,
                    "message": str(exc),
                }
        method_results.append(item)
    return {"sample_symbol_methods": method_results}


async def main() -> None:
    client = BlocksizeClient()
    try:
        rpc_results = [await probe_rpc(client, method, params) for method, params in RPC_PROBES]
        try:
            state_instruments = await client.list_state_instruments()
            first_pool = next(
                (
                    (item, pool)
                    for item in state_instruments
                    if isinstance(item, dict)
                    for pool in (item.get("pools") or [])
                    if isinstance(pool, dict) and pool.get("network") and pool.get("address")
                ),
                None,
            )
            if first_pool:
                instrument, pool = first_pool
                rpc_results.append(
                    await probe_rpc(
                        client,
                        "state_pool",
                        {
                            "symbol": instrument.get("symbol"),
                            "network": pool["network"],
                            "pool": pool["address"],
                        },
                    )
                )
        except Exception:
            pass
        method_results = await probe_client_methods(client)
        supported = [
            f"{item['method']} {item['params']}"
            for item in rpc_results
            if item["status"] == "ok"
        ]
        unsupported = [
            {
                "method": item["method"],
                "params": item["params"],
                "error_code": item.get("error_code"),
                "message": item.get("message"),
            }
            for item in rpc_results
            if item["status"] != "ok"
        ]
        print(
            json.dumps(
                {
                    "status": "ok",
                    "methodology": {
                        "type": "blocksize_key_capability_audit_v1",
                        "steps": [
                            "Use the configured Blocksize API key from local settings.",
                            "Call repo-known JSON-RPC methods and likely instrument discovery variants.",
                            "Call high-value sample symbols through current client methods.",
                            "Report returned shape, counts, and errors without printing secrets.",
                        ],
                    },
                    "supported_rpc_probes": supported,
                    "unsupported_rpc_probes": unsupported,
                    "rpc_results": rpc_results,
                    **method_results,
                },
                indent=2,
                sort_keys=True,
            )
        )
    finally:
        await client.close()


if __name__ == "__main__":
    asyncio.run(main())
