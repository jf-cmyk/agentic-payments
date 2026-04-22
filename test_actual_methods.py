import asyncio
import os
import httpx
from dotenv import load_dotenv

load_dotenv()

async def test():
    api_key = os.getenv("BLOCKSIZE_API_KEY")
    url = "https://data.blocksize.capital/marketdata/v1/api"
    
    methods = [
        "vwap_latest",
        "vwap_instruments",
        "vwap_30min_latest",
        "vwap_24h_latest",
        "state_price_latest",
        "bidask_getSnapshot",
        "bidask_instruments"
    ]
    
    async with httpx.AsyncClient() as client:
        for method in methods:
            print(f"[*] Testing {method}...")
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
            print(f"  {res.status_code}: {res.text[:100]}")

asyncio.run(test())
