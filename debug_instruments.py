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
                "params": {"ticker": "BTCUSD"},
                "id": 1
            }
        )
        data = res.json()
        result = data.get("result", {})
        instruments = result.get("instruments", [])
        
        # See what elements with 'BTC' look like
        btc_items = [x for x in instruments if 'BTC' in x.get('ticker', '').upper()]
        for idx, it in enumerate(btc_items[:10]):
            print(f"[{idx}] {it.get('ticker')}")
            
        btcusd_exact = [x for x in instruments if 'BTCUSD' in x.get('ticker', '').upper()]
        for idx, it in enumerate(btcusd_exact[:10]):
            print(f"\nExact BTCUSD Match: {json.dumps(it, indent=2)}")

asyncio.run(debug())
