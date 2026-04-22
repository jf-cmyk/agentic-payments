import asyncio
import os
import httpx
from dotenv import load_dotenv

load_dotenv()

async def test():
    api_key = os.getenv("BLOCKSIZE_API_KEY")
    url = "https://data.blocksize.capital/marketdata/v1/api"
    
    methods = [
        "state_price",
        "statePrice",
        "state",
        "reference_price",
        "referencePrice",
        "reference",
        "get_state_price",
        "getStatePrice",
        "get_reference_price"
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
            data = res.json()
            if "error" not in data or data["error"].get("code") != -32601:
                print(f"  [FOUND] {method}: {str(data)[:100]}")

asyncio.run(test())
