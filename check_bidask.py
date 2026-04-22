import asyncio
import os
import httpx
import json
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
                "method": "bidask_getSnapshot",
                "params": {"ticker": "BTCUSD"},
                "id": 1
            }
        )
        data = res.json()
        print(json.dumps(data, indent=2))

asyncio.run(test())
