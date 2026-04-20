import asyncio
import os
import json
import httpx
from dotenv import load_dotenv

load_dotenv()

async def test():
    api_key = os.getenv("BLOCKSIZE_API_KEY")
    url = "https://data.blocksize.capital/marketdata/v1/api"
    
    async with httpx.AsyncClient() as client:
        res = await client.post(
            url,
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            json={
                "jsonrpc": "2.0",
                "method": "vwap_24h_latest",
                "params": {"ticker": "BTCUSD"},
                "id": 1
            }
        )
        print(f"  {res.status_code}: {res.text}")

asyncio.run(test())
