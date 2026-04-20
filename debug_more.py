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
                "params": {"ticker": "XAUUSD"},
                "id": 1
            }
        )
        snapshot = res.json().get('result', {}).get('snapshot', [])
        
        for item in snapshot:
            if item.get("ticker", "").upper() == "XAUUSD":
                print("FOUND XAUUSD:", json.dumps(item, indent=2))
                break

asyncio.run(debug())
