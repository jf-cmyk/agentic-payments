import asyncio
import os
import json
import httpx
from dotenv import load_dotenv

load_dotenv()

async def debug():
    api_key = os.getenv("BLOCKSIZE_API_KEY")
    url = "https://data.blocksize.capital/marketdata/v1/api"
    
    async with httpx.AsyncClient() as client:
        res = await client.post(
            url,
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            json={
                "jsonrpc": "2.0",
                "method": "bidask_getSnapshot",
                "params": {"ticker": "BTCUSD"},
                "id": 1
            }
        )
        data = res.json()
        result = data.get("result", {})
        instruments = result.get("instruments", [])
        item = next((x for x in instruments if x.get("ticker", "").upper() == "BTCUSD"), {})
        print(f"BTCUSD Extracted Item: {json.dumps(item, indent=2)}")
        
        # Test equity, fx, metal, rate APIs exactly as client does
        methods = [
            ("equity", "bidask_equity_getSnapshot", "AAPL"),
            ("fx", "bidask_fx_getSnapshot", "EURUSD"),
            ("metal", "bidask_metal_getSnapshot", "XAUUSD"),
            ("rate", "bidask_rate_getSnapshot", "Rates.US10Y")
        ]
        
        for name, method, ticker in methods:
            print(f"\nTesting {name} ({method}) for {ticker}...")
            r = await client.post(
                url,
                headers={"x-api-key": api_key, "Content-Type": "application/json"},
                json={"jsonrpc": "2.0", "method": method, "params": {"ticker": ticker}, "id": 1}
            )
            print(json.dumps(r.json(), indent=2))

asyncio.run(debug())
