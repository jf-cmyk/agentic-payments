import asyncio
import os
import json
import httpx
from dotenv import load_dotenv

load_dotenv()

async def test():
    api_key = os.getenv("BLOCKSIZE_API_KEY")
    url = "https://data.blocksize.capital/marketdata/v1/api"
    
    methods_to_test = [
        "state_price_latest",
        "reference_price_latest",
        "state_latest",
        "vwap_30min_latest",
        "vwap_30m_latest",
        "vwap_instruments"
    ]
    
    async with httpx.AsyncClient() as client:
        for method in methods_to_test:
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
            print(f"  {res.status_code}: {res.text[:200]}")

asyncio.run(test())
