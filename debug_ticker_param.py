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
                "params": {"ticker": "EURUSD"},
                "id": 1
            }
        )
        print("KEYS:", list(res.json().get('result', {}).keys()))
        snapshot = res.json().get('result', {}).get('snapshot', [])
        print("LENGTH:", len(snapshot))

asyncio.run(debug())
