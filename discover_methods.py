import asyncio
import os
import httpx
from dotenv import load_dotenv

load_dotenv()

async def probe():
    api_key = os.getenv("BLOCKSIZE_API_KEY")
    url = "https://data.blocksize.capital/marketdata/v1/api"
    
    suffixes = ["latest", "getSnapshot", "instruments", "getLatest", "snapshot"]
    
    templates = [
        "vwap_30min_{suffix}",
        "vwap30m_{suffix}",
        "vwap_30m_{suffix}",
        "state_{suffix}",
        "state_price_{suffix}",
        "reference_{suffix}",
        "reference_price_{suffix}",
        "price_{suffix}"
    ]
    
    async with httpx.AsyncClient() as client:
        for template in templates:
            for suffix in suffixes:
                method = template.format(suffix=suffix)
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
                    print(f"  [FOUND!!] {method}: {data}")
                

asyncio.run(probe())
