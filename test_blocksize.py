import os
import httpx
import asyncio
from dotenv import load_dotenv

load_dotenv()

async def test():
    api_key = os.getenv("BLOCKSIZE_API_KEY")
    async with httpx.AsyncClient() as client:
        res = await client.post(
            "https://data.blocksize.capital/marketdata/v1/api",
            headers={"x-api-key": api_key, "Content-Type": "application/json"},
            json={
                "jsonrpc": "2.0",
                "method": "vwap_latest",
                "params": {"ticker": "BTCUSD"},
                "id": 1
            }
        )
        data = res.json()
        print(data)

asyncio.run(test())
