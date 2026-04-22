import asyncio
import os
import httpx
from dotenv import load_dotenv

load_dotenv()

async def test():
    api_key = os.getenv("BLOCKSIZE_API_KEY")
    url = "https://data.blocksize.capital/marketdata/v1/api"
    
    suffixes = ["instruments", "latest", "getSnapshot"]
    prefixes = ["vwap", "vwap_30min", "vwap_24h", "state", "state_price", "reference", "reference_price", "bidask"]
    
    async with httpx.AsyncClient() as client:
        for prefix in prefixes:
            for suffix in suffixes:
                method = f"{prefix}_{suffix}"
                res = await client.post(
                    url,
                    headers={"x-api-key": api_key, "Content-Type": "application/json"},
                    json={
                        "jsonrpc": "2.0",
                        "method": method,
                        "params": {"ticker": "BTCUSD"},
                        "id": 1
                    }
                )
                data = res.json()
                if "error" not in data or data["error"].get("code") != -32601:
                    print(f"[FOUND] {method}: {str(data)[:100]}")

asyncio.run(test())
